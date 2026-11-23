"""Signer beside an Open WebUI older than 0.10: signs the user it forwards.

Open WebUI before 0.10 forwards its signed-in user only as plain
X-OpenWebUI-User-* headers (ENABLE_FORWARD_USER_INFO_HEADERS); the signed token
in X-OpenWebUI-User-Jwt, which FORWARD_USER_INFO_HEADER_JWT_SECRET turns on,
arrived in 0.10. The hook and the platform accept the token only, since a plain
header is whatever its sender says. This sits between Open WebUI and LiteLLM on
Open WebUI's own host, holds Open WebUI's signing secret as Open WebUI itself
would, and mints the token 0.10 would have minted, with the same claims, so
nothing downstream changes. The plain headers are trusted here only because
they arrive over loopback from this host's Open WebUI; nothing else reaches
this port, and they are not passed on. Delete it after upgrading Open WebUI.

Environment: FORWARD_USER_INFO_HEADER_JWT_SECRET (required, Open WebUI's secret),
SIGNER_PORT (default 4012), SIGNER_UPSTREAM (default http://127.0.0.1:4011),
FORWARD_USER_INFO_HEADER_JWT_EXPIRES_SECONDS (default 300, as Open WebUI's).
"""
import os
import time

import httpx
import jwt
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

SECRET = os.environ["FORWARD_USER_INFO_HEADER_JWT_SECRET"].strip()
UPSTREAM = os.environ.get("SIGNER_UPSTREAM", "http://127.0.0.1:4011").rstrip("/")
PORT = int(os.environ.get("SIGNER_PORT", "4012"))
EXPIRES = int(os.environ.get("FORWARD_USER_INFO_HEADER_JWT_EXPIRES_SECONDS", "300"))
JWT_HEADER = os.environ.get("FORWARD_USER_INFO_HEADER_JWT", "X-OpenWebUI-User-Jwt")
PLAIN = {"x-openwebui-user-name", "x-openwebui-user-id", "x-openwebui-user-email", "x-openwebui-user-role"}
# Owned by the connection, not the request, so rebuilt on each side.
HOP = {"host", "content-length", "transfer-encoding", "connection", "keep-alive"}

client = httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(600, connect=10))


def mint(headers) -> str | None:
    email = headers.get("x-openwebui-user-email", "").strip()
    if not email:
        return None
    now = int(time.time())
    return jwt.encode(
        {
            "sub": headers.get("x-openwebui-user-id", ""),
            "email": email,
            "name": headers.get("x-openwebui-user-name", "").strip(),
            "role": headers.get("x-openwebui-user-role", ""),
            "iss": "open-webui",
            "iat": now,
            "exp": now + EXPIRES,
        },
        SECRET,
        algorithm="HS256",
    )


async def proxy(request: Request):
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP and k.lower() not in PLAIN}
    token = mint(request.headers)
    if token:
        headers[JWT_HEADER] = token
    url = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    upstream = client.build_request(request.method, url, headers=headers, content=await request.body())
    resp = await client.send(upstream, stream=True)
    out = {k: v for k, v in resp.headers.items() if k.lower() not in HOP}
    return StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, headers=out,
                             background=BackgroundTask(resp.aclose))


app = Starlette(routes=[Route("/{path:path}", proxy, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
