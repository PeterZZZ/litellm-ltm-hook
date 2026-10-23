# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""Mock MemMachine platform: a tenant control server in front of core.

It serves exactly three core paths, at the same URLs, so a hook in platform
mode only changes MEMMACHINE_BASE_URL and the headers it sends:

    POST /api/v2/memories/search   search
    POST /api/v2/memories          store
    POST /api/v2/memories/list     list

Each request is authorized (see authorize.py): the hook proposes org_id and
project_id in the body and the platform checks they are the person's before
anything reaches core. Bodies are forwarded as received, with the verified
org_id and project_id in place of the proposed ones; the producer of a stored
turn is still set here, since provenance is the platform's to state. Every
other path or method is 403 with `X-MemMachine-Decision: endpoint-not-allowed`,
not 404, so a caller cannot tell a hidden endpoint from a forbidden one.

    MOCK_PLATFORM_DB=/path/registry.sqlite \
    MOCK_PLATFORM_CORE_URL=http://127.0.0.1:8090 \
    uvicorn mock_platform.app:app --port 8100
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .authorize import (DECISION_HEADER, ENDPOINT_NOT_ALLOWED, KEY_ID_HEADER, KEY_OWNER_HEADER,
                        NOT_ENROLLED, REQUEST_ID_HEADER, TOKEN_USER_UNKNOWN, Denied, Principal,
                        authorize)
from .enrol import Enroller
from .linking import RECONCILE_SECONDS, KeyLinker
from .registry import Registry

log = logging.getLogger("mock_platform")

_ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def create_app(
    registry: Registry,
    core_url: str,
    *,
    timeout: float = 60.0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    owui_url: Optional[str] = None,
    litellm_url: Optional[str] = None,
    litellm_key: Optional[str] = None,
    sso=None,
    linker: Optional[KeyLinker] = None,
    reconcile_seconds: float = RECONCILE_SECONDS,
    auto_enrol: bool = True,
    enroller: Optional[Enroller] = None,
) -> FastAPI:
    """The platform API; with `owui_url` or `sso`, also the portal at /portal/.

    With `litellm_url` and `litellm_key` (a read-only LiteLLM identity) the
    platform links unknown personal keys itself on their first call and
    reconciles its keys with LiteLLM every `reconcile_seconds` (0: never).
    With `sso` and `auto_enrol`, a person Corp SSO knows is enrolled on their
    first call instead of being refused as not-enrolled (enrol.py).
    """
    core_url = core_url.rstrip("/")
    known_projects: set[tuple[str, str]] = set()

    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=transport)

    linker = linker or KeyLinker(registry, litellm_url, litellm_key, client)
    enroller = enroller or Enroller(registry, sso, enabled=auto_enrol)
    if enroller.configured:
        linker.enrol = enroller.by_person

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = None
        if linker.configured and reconcile_seconds > 0:
            task = asyncio.create_task(_reconcile_loop(linker, reconcile_seconds))
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="MemMachine mock platform", docs_url=None, redoc_url=None,
                  lifespan=lifespan)

    async def decide(request: Request, op: str, body: dict) -> Principal:
        request_id = (request.headers.get(REQUEST_ID_HEADER) or "")[:80]
        key_hash = (request.headers.get(KEY_ID_HEADER) or "").strip().lower()
        if key_hash and registry.key(key_hash) is None and registry.service_label(
                _bearer(request.headers.get("authorization"))):
            # A key the platform has never seen, vouched for by a known LiteLLM:
            # ask that LiteLLM whose it is before deciding.
            await linker.link_unknown(key_hash, (request.headers.get(KEY_OWNER_HEADER) or "").strip())
        try:
            try:
                principal = authorize(registry, request.headers, body=body)
            except Denied as first:
                # A verified token naming someone the registry does not know:
                # enrol them if Corp SSO knows them, then decide again.
                email = first.context.get("email")
                if not (first.decision == NOT_ENROLLED and first.reason == TOKEN_USER_UNKNOWN
                        and email and enroller.configured):
                    raise
                if await enroller.by_email(email, "openwebui") is None:
                    raise
                principal = authorize(registry, request.headers, body=body)
        except Denied as exc:
            log.info("op=%s decision=%s status=%s reason=%r namespace=%s request=%s",
                     op, exc.decision, exc.status, exc.reason,
                     exc.context.get("namespace") or "-", request_id)
            registry.record(op=op, decision=exc.decision, status=exc.status, reason=exc.reason,
                            **exc.context)
            raise
        u = principal.user
        log.info(
            "op=%s decision=allow service=%s via=%s client=%s key=%s user=%s person=%s "
            "org=%s project=%s request=%s",
            op, principal.service, principal.via, principal.client or "-", principal.key_label,
            u.email, u.person_id, principal.org_id, principal.project_id, request_id,
        )
        registry.record(op=op, decision="allow", status=200, service=principal.service,
                        via=principal.via, key_label=principal.key_label, email=u.email,
                        namespace=f"{principal.org_id}/{principal.project_id}")
        return principal

    @app.exception_handler(Denied)
    async def _denied(_request: Request, exc: Denied):
        return JSONResponse(
            {"detail": exc.reason},
            status_code=exc.status,
            headers={DECISION_HEADER: exc.decision},
        )

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    async def forward(request: Request, op: str, path: str) -> Response:
        """Authorize, then relay the body as received under the verified namespace."""
        body = await _json_object(request)
        principal = await decide(request, op, body)
        payload = {**body, "org_id": principal.org_id, "project_id": principal.project_id}
        async with client() as c:
            resp = await c.post(f"{core_url}{path}", json=payload)
        return _relay(resp)

    @app.post("/api/v2/memories/search")
    async def search(request: Request):
        return await forward(request, "search", "/api/v2/memories/search")

    @app.post("/api/v2/memories/list")
    async def list_memories(request: Request):
        return await forward(request, "list", "/api/v2/memories/list")

    @app.post("/api/v2/memories")
    async def store(request: Request):
        body = await _json_object(request)
        principal = await decide(request, "store", body)
        user = principal.user
        messages = []
        for m in body.get("messages") or []:
            if not isinstance(m, dict):
                continue
            m = dict(m)
            # Provenance is the platform's to state, not the caller's.
            if m.get("role") == "assistant":
                m["produced_for"] = user.org_id
            else:
                m["producer"] = user.org_id
            meta = dict(m.get("metadata") or {})
            meta["user_id"] = user.person_id
            # Where the memory came from, for the person reviewing it later.
            meta["via"] = principal.via
            meta["source"] = principal.key_label
            if principal.client:
                meta["client"] = principal.client
            if not meta.get("session_id") or meta.get("session_id") == "default":
                meta["session_id"] = user.org_id
            m["metadata"] = meta
            messages.append(m)
        payload = {**body, "org_id": principal.org_id, "project_id": principal.project_id,
                   "messages": messages}
        async with client() as c:
            await _ensure_project(c, principal.org_id, principal.project_id)
            resp = await c.post(f"{core_url}/api/v2/memories", json=payload)
        return _relay(resp)

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

    if owui_url or sso:
        from .portal import mount_portal

        mount_portal(app, registry, core_url, owui_url, client, litellm_url, litellm_key, sso=sso,
                     linker=linker, enroller=enroller)

    # Registered last, so every path and method above wins. The portal keeps
    # its own answers for its own unknown paths.
    @app.api_route("/{path:path}", methods=_ALL_METHODS, include_in_schema=False)
    async def not_allowed(request: Request, path: str):
        if path == "portal" or path.startswith("portal/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        op = f"{request.method} /{path}"[:80]
        request_id = (request.headers.get(REQUEST_ID_HEADER) or "")[:80]
        log.info("op=%r decision=%s status=403 request=%s", op, ENDPOINT_NOT_ALLOWED, request_id)
        if request.headers.get("authorization"):
            # An API client's attempt is worth a line in the audit; a browser
            # asking for a favicon is not.
            registry.record(op=op, decision=ENDPOINT_NOT_ALLOWED, status=403,
                            reason="endpoint not served by the platform")
        return JSONResponse({"detail": "endpoint not served by the platform"}, status_code=403,
                            headers={DECISION_HEADER: ENDPOINT_NOT_ALLOWED})

    return app


def _bearer(authorization: Optional[str]) -> str:
    scheme, _, credential = (authorization or "").partition(" ")
    return credential.strip() if scheme.lower() == "bearer" else ""


async def _reconcile_loop(linker: KeyLinker, every: float) -> None:
    while True:
        await asyncio.sleep(every)
        try:
            await linker.reconcile()
        except Exception as exc:  # keep the loop alive; the next run may succeed
            log.warning("reconcile failed: %s: %s", type(exc).__name__, exc)


async def _json_object(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise Denied(400, "body is not JSON") from None
    if not isinstance(body, dict):
        raise Denied(400, "body must be a JSON object")
    return body


def _relay(resp: httpx.Response) -> Response:
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
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
        # A read-only LiteLLM identity, so an admin can enrol people by email and
        # the platform finds their keys itself instead of trusting what it is told.
        litellm_url=os.environ.get("MOCK_PLATFORM_LITELLM_URL") or None,
        litellm_key=os.environ.get("MOCK_PLATFORM_LITELLM_KEY") or None,
        sso=_sso_from_env(),
        reconcile_seconds=float(os.environ.get("MOCK_PLATFORM_RECONCILE_SECONDS", RECONCILE_SECONDS)),
        # People Corp SSO knows enrol themselves on first use (Leo, Oct 22).
        auto_enrol=os.environ.get("MOCK_PLATFORM_AUTO_ENROL", "1").strip() != "0",
    )


def _sso_from_env():
    """Corp SSO (Keycloak) for the portal's sign-in and employee-ID lookups."""
    issuer = os.environ.get("MOCK_PLATFORM_OIDC_ISSUER")
    if not issuer:
        return None
    from .sso import Keycloak

    return Keycloak(
        issuer,
        os.environ["MOCK_PLATFORM_OIDC_CLIENT_ID"],
        os.environ["MOCK_PLATFORM_OIDC_CLIENT_SECRET"],
    )


app = _from_env() if os.environ.get("MOCK_PLATFORM_DB") else None
