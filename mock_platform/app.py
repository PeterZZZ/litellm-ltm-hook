# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""Mock MemMachine platform: a tenant control server in front of core.

It serves the two core paths the hook uses, at the same URLs, so a hook in
platform mode only changes MEMMACHINE_BASE_URL and the headers it sends:

    POST /api/v2/memories/search
    POST /api/v2/memories

Each request is authorized (see authorize.py), then rewritten before it goes to
core: org_id and project_id come from the platform's registry, never from the
client, and the producer of a stored user turn is the platform's user id. Only
the fields the hook is known to send are passed on. Every other path is 404.

    MOCK_PLATFORM_DB=/path/registry.sqlite \
    MOCK_PLATFORM_CORE_URL=http://127.0.0.1:8090 \
    uvicorn mock_platform.app:app --port 8100
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .authorize import NOT_ENROLLED, Denied, Principal, authorize
from .registry import Registry

log = logging.getLogger("mock_platform")

_SEARCH_FIELDS = ("query", "top_k", "types")
_STORE_MESSAGE_FIELDS = ("content", "role", "producer", "produced_for", "metadata", "timestamp")


def create_app(
    registry: Registry,
    core_url: str,
    *,
    timeout: float = 60.0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    owui_url: Optional[str] = None,
) -> FastAPI:
    """The platform API; with `owui_url`, also the My Memory portal at /portal/."""
    app = FastAPI(title="MemMachine mock platform", docs_url=None, redoc_url=None)
    core_url = core_url.rstrip("/")
    known_projects: set[tuple[str, str]] = set()

    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=transport)

    def decide(request: Request, op: str) -> Principal:
        try:
            principal = authorize(registry, request.headers)
        except Denied as exc:
            log.info("op=%s decision=%s status=%s reason=%r", op, exc.decision, exc.status, exc.reason)
            raise
        u = principal.user
        log.info(
            "op=%s decision=allow service=%s via=%s key=%s user=%s namespace=%s/%s",
            op, principal.service, principal.via, principal.key_label, u.email, u.org_id, u.project_id,
        )
        return principal

    @app.exception_handler(Denied)
    async def _denied(_request: Request, exc: Denied):
        return JSONResponse(
            {"detail": exc.reason},
            status_code=exc.status,
            headers={"X-MM-Decision": exc.decision},
        )

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.post("/api/v2/memories/search")
    async def search(request: Request):
        principal = decide(request, "search")
        body = await _json_object(request)
        payload = {k: body[k] for k in _SEARCH_FIELDS if k in body}
        payload.update(org_id=principal.user.org_id, project_id=principal.user.project_id)
        async with client() as c:
            resp = await c.post(f"{core_url}/api/v2/memories/search", json=payload)
        return _relay(resp, principal)

    @app.post("/api/v2/memories")
    async def store(request: Request):
        principal = decide(request, "store")
        body = await _json_object(request)
        user = principal.user
        messages = []
        for m in body.get("messages") or []:
            if not isinstance(m, dict):
                continue
            m = {k: m[k] for k in _STORE_MESSAGE_FIELDS if k in m}
            # Provenance is the platform's to state, not the caller's.
            if m.get("role") == "assistant":
                m["produced_for"] = user.user_id
            else:
                m["producer"] = user.user_id
            meta = dict(m.get("metadata") or {})
            meta["user_id"] = user.user_id
            # Where the memory came from, for the person reviewing it later.
            meta["via"] = principal.via
            meta["source"] = principal.key_label
            if not meta.get("session_id") or meta.get("session_id") == "default":
                meta["session_id"] = user.user_id
            m["metadata"] = meta
            messages.append(m)
        payload = {
            "org_id": user.org_id,
            "project_id": user.project_id,
            "types": body.get("types", []),
            "messages": messages,
        }
        async with client() as c:
            await _ensure_project(c, user.org_id, user.project_id)
            resp = await c.post(f"{core_url}/api/v2/memories", json=payload)
        return _relay(resp, principal)

    async def _ensure_project(c: httpx.AsyncClient, org_id: str, project_id: str) -> None:
        if (org_id, project_id) in known_projects:
            return
        resp = await c.post(
            f"{core_url}/api/v2/projects",
            json={
                "org_id": org_id,
                "project_id": project_id,
                "description": f"mock platform: {org_id}/{project_id}",
            },
        )
        # 409: it already exists. Anything else is left for the store to report.
        if resp.status_code in (200, 201, 409):
            known_projects.add((org_id, project_id))

    if owui_url:
        from .portal import mount_portal

        mount_portal(app, registry, core_url, owui_url, client)

    return app


async def _json_object(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise Denied(400, "body is not JSON") from None
    if not isinstance(body, dict):
        raise Denied(400, "body must be a JSON object")
    return body


def _relay(resp: httpx.Response, principal: Principal) -> Response:
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
        headers={
            "X-MM-Org": principal.user.org_id,
            "X-MM-Project": principal.user.project_id,
        },
    )


def _from_env() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    db = os.environ.get("MOCK_PLATFORM_DB")
    core = os.environ.get("MOCK_PLATFORM_CORE_URL")
    if not db or not core:
        raise RuntimeError("MOCK_PLATFORM_DB and MOCK_PLATFORM_CORE_URL are required")
    return create_app(
        Registry(db),
        core,
        timeout=float(os.environ.get("MOCK_PLATFORM_CORE_TIMEOUT", "60")),
        owui_url=os.environ.get("MOCK_PLATFORM_OWUI_URL") or None,
    )


app = _from_env() if os.environ.get("MOCK_PLATFORM_DB") else None
