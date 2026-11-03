# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""authorize(): check what a LiteLLM hook says about a request, and who it is for.

The hook proposes; the platform verifies. The headers are the shared Platform
API, the same for every front end:

    Authorization: Bearer <service credential>      which LiteLLM is calling
    X-MemMachine-Key-Id: <sha256 of the virtual key> which LiteLLM key was used
    X-MemMachine-Key-Owner: <LiteLLM user_id>        the key's owner, as LiteLLM says
    X-MemMachine-User: <employee ID, or email>       who the hook says the person is
    X-MemMachine-User-Assertion: <signed token>      Open WebUI's proof of that
    X-MemMachine-Project: <project>                  must match the body
    X-MemMachine-Client: claude-code | openwebui     audit only
    X-Request-Id: <LiteLLM request id>               audit only
    X-MemMachine-Request-Started: <epoch seconds>    store only, see _judge_at

and the body carries org_id and project_id, which must be the person's.

Rules, in order. Each refusal names the step that failed.

1. The service credential must be registered.                       401
   With mutual TLS required, the request must also have come over it, with
   a client certificate (signed by the platform's CA; serve.py checks that)
   whose common name is that credential's service label: a credential
   copied off a LiteLLM is no use without that LiteLLM's key.         401
2. A key id must be present.                                        401
3. An unknown key is a person who has not been enrolled (the app has
   already asked LiteLLM about it, see linking.py); a key reconcile
   unlinked because LiteLLM reassigned it stays refused.   not enrolled / 403
4. A personal key is its owner. An assertion beside it is refused (tokens are
   honoured only on the app key they were issued for), and so is naming
   anyone else as the user or the key's owner.                  401 / 403
5. A frontend (app) key must still belong to its service user in LiteLLM:
   X-MemMachine-Key-Owner must be the owner recorded for it, as it must for a
   personal key. Otherwise the key was reassigned.        403 key-reassigned
   An app key names nobody: the assertion is required and verified
   here with that app's own secret and issuer.                      401
   The person is the token's email, which must be enrolled (or, with
   auto-enrolment, known to Corp SSO: enrol.py).                not enrolled
   The hook must name that person in X-MemMachine-User, with a value the token
   itself carries: their employee ID, or their email when they have none.
   A display name someone edited to another employee's ID fails here.  403
6. A disabled user, or a way in the person switched off.        not enrolled
7. The body's org_id must be the person's org.                      403
   X-MemMachine-Project is required and must equal the body's project_id;
   a missing header or a mismatch is a malformed request.           400
   The project must be granted to the person (or their grants are `*`), or
   be the alias of a project shared with them; the namespace passed to core
   is <org>/<project>, the owner's org when shared.    403 project-not-granted

"Not enrolled" is 403 with `X-MemMachine-Decision: not-enrolled`. The hook treats
that one answer as "memory is off for this person"; every other 401/403 is a
refusal. Either way the chat goes on without memory: a refused store is dropped
(403) or kept for replay (401), never shown to the user. The decision
header names the step for the audit: `refused`, `key-reassigned` (LiteLLM's
owner for the key is no longer the one the platform linked it to),
`project-not-granted` (the person exists but may not use that project).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import jwt

from .registry import Namespace, Registry, User

KEY_ID_HEADER = "x-memmachine-key-id"
KEY_OWNER_HEADER = "x-memmachine-key-owner"
USER_HEADER = "x-memmachine-user"
ASSERTION_HEADER = "x-memmachine-user-assertion"
PROJECT_HEADER = "x-memmachine-project"
CLIENT_HEADER = "x-memmachine-client"
REQUEST_ID_HEADER = "x-request-id"
STARTED_AT_HEADER = "x-memmachine-request-started"
DECISION_HEADER = "X-MemMachine-Decision"
NOT_ENROLLED = "not-enrolled"
# A path or method the platform does not serve: 403, never 404, so a caller
# cannot tell a hidden endpoint from a forbidden one.
ENDPOINT_NOT_ALLOWED = "endpoint-not-allowed"
# LiteLLM's owner for the key is not the one the platform linked it to.
KEY_REASSIGNED = "key-reassigned"
# The person is enrolled but the project the hook proposed is not theirs to use.
PROJECT_NOT_GRANTED = "project-not-granted"
# The one refusal the app may answer by enrolling the person (see enrol.py):
# the token verified, its email is just not in the registry.
TOKEN_USER_UNKNOWN = "token user not registered"
# Where serve.py's mutual-TLS listener puts the verified client certificate's
# common name in the ASGI scope. Absent on the plain listener.
CLIENT_CERT_SCOPE = "mock_platform.client_cert_name"

# Clock difference allowed between Open WebUI, which stamps the token, and here.
LEEWAY_SECONDS = 30
# How far back a hook may say a request started. The store path runs after the
# model has answered, so it asks for the token to be judged at request start;
# this bounds how stale that claim may be.
MAX_REQUEST_AGE_SECONDS = 15 * 60


class Denied(Exception):
    def __init__(self, status: int, reason: str, decision: str = "refused", **context):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.decision = decision
        # What was known when the request stopped (service, via, key_label,
        # email), so the audit log can say whose request it was.
        self.context = context


@dataclass(frozen=True)
class Principal:
    user: User
    service: str
    key_label: str
    via: str  # "personal-key" or "app-token"
    client: str = ""
    project_id: str = "default"  # the project this request works in
    org_id: str = ""             # whose org it lives in: the person's, or a sharer's
    shared: bool = False

    def __post_init__(self):
        if not self.org_id:
            object.__setattr__(self, "org_id", self.user.org_id)


def authorize(registry: Registry, headers, now: Optional[float] = None,
              body: Optional[dict] = None, *, mtls: bool = False,
              client_cert_name: Optional[str] = None) -> Principal:
    """Who the request is for. With `body`, also check the namespace it names.
    With `mtls`, the caller's client certificate (`client_cert_name`, its common
    name; None without one) must be the credential's service."""
    now = time.time() if now is None else now

    # 1. Which LiteLLM is calling.
    auth = headers.get("authorization") or ""
    scheme, _, credential = auth.partition(" ")
    service = registry.service_label(credential.strip()) if scheme.lower() == "bearer" else None
    if not service:
        raise Denied(401, "unknown service credential")
    if mtls and client_cert_name != service:
        raise Denied(401, f"client certificate {client_cert_name[:64]!r} is not this credential's service"
                     if client_cert_name else "no client certificate", service=service)

    # 2. Which LiteLLM key the request came in on.
    key_hash = (headers.get(KEY_ID_HEADER) or "").strip().lower()
    if not key_hash:
        raise Denied(401, "no virtual key evidence")

    # 3. Does the platform know that key at all.
    why = registry.revoked(key_hash)
    if why:
        raise Denied(403, f"key reassigned in LiteLLM, per reconcile: {why}", KEY_REASSIGNED,
                     service=service)
    key = registry.key(key_hash)
    if key is None:
        raise Denied(403, "virtual key not registered", NOT_ENROLLED, service=service)

    token = (headers.get(ASSERTION_HEADER) or "").strip()
    claimed = (headers.get(USER_HEADER) or "").strip()
    client = (headers.get(CLIENT_HEADER) or "").strip()[:40]
    ctx = {"service": service, "key_label": key.label}

    if key.kind == "personal":
        # 4. The key is the person.
        user = registry.user(key.user_id)
        ctx.update(via="personal-key", email=user.email)
        if token:
            raise Denied(401, "user token presented on a personal key", **ctx)
        if claimed and not _same(claimed, user.person_id):
            raise Denied(403, "a personal key acts only as its owner", **ctx)
        owner = (headers.get(KEY_OWNER_HEADER) or "").strip()
        if owner and not _same(owner, user.person_id):
            raise Denied(403, "LiteLLM names a different owner for this key", KEY_REASSIGNED, **ctx)
        if not key.enabled:
            raise Denied(403, "key switched off by its owner", NOT_ENROLLED, **ctx)
        via = "personal-key"
    else:
        # 5. The key is an application; the signed token is the person.
        ctx["via"] = "app-token"
        app = registry.app(key.key_hash)
        if app is None:
            raise Denied(401, "app key has no token configuration", **ctx)
        owner = (headers.get(KEY_OWNER_HEADER) or "").strip()
        if not _same(owner, app.owner or ""):
            raise Denied(403, f"LiteLLM names {owner or 'no one'!r} as this app key's owner, "
                              f"not its service user", KEY_REASSIGNED, **ctx)
        if not token:
            raise Denied(401, "app key without a user token", **ctx)
        claims = _verify_token(token, app.jwt_secret, app.issuer, _judge_at(headers, now), ctx)
        ctx["email"] = claims["email"]
        user = registry.user_by_email(claims["email"])
        if user is None:
            raise Denied(403, TOKEN_USER_UNKNOWN, NOT_ENROLLED, **ctx)
        if not claimed:
            raise Denied(401, "a frontend key must name the user", **ctx)
        carried = {str(claims.get(c) or "").strip() for c in ("email", "name")} - {""}
        if not any(_same(claimed, v) for v in carried):
            raise Denied(403, "named user is not in the signed token", **ctx)
        if not _same(claimed, user.person_id):
            raise Denied(403, f"named user is not the verified person ({user.person_id})", **ctx)
        if not user.allow_app:
            raise Denied(403, "Open WebUI access switched off", NOT_ENROLLED, **ctx)
        via = "app-token"

    # 6. Memory switched off for this person.
    if not user.enabled:
        raise Denied(403, "user disabled", NOT_ENROLLED, **ctx)

    # 7. The namespace the hook proposed: its org_id names the person (their
    #    own org), its project is a name in that person's view, which may
    #    resolve into someone else's org when the project was shared.
    ns = Namespace(user.org_id, user.project_id, user.user_id)
    if body is not None:
        org_id = str(body.get("org_id") or "")
        project_id = str(body.get("project_id") or "")
        header_project = (headers.get(PROJECT_HEADER) or "").strip()
        ctx["namespace"] = f"{org_id}/{project_id}"
        if org_id != user.org_id:
            raise Denied(403, f"org_id {org_id!r} is not this person's", **ctx)
        if not header_project:
            raise Denied(400, "X-MemMachine-Project is required", **ctx)
        if header_project != project_id:
            raise Denied(400, "X-MemMachine-Project does not match the body's project_id", **ctx)
        ns = user.resolve(project_id)
        if ns is None:
            raise Denied(403, f"project {project_id!r} is not granted", PROJECT_NOT_GRANTED, **ctx)
        ctx["namespace"] = f"{ns.org_id}/{ns.project_id}"

    return Principal(user=user, service=service, key_label=key.label, via=via, client=client,
                     project_id=ns.project_id, org_id=ns.org_id, shared=ns.shared)


def _same(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def _judge_at(headers, now: float) -> float:
    """The moment the token must have been valid at.

    Search runs before the model, so that is now. Store runs after the answer,
    which can outlast a short-lived token; the hook then sends when the request
    started, and the platform accepts that within MAX_REQUEST_AGE_SECONDS.
    """
    raw = (headers.get(STARTED_AT_HEADER) or "").strip()
    if not raw:
        return now
    try:
        started = float(raw)
    except ValueError:
        raise Denied(401, "malformed request start time") from None
    if started > now + LEEWAY_SECONDS or now - started > MAX_REQUEST_AGE_SECONDS:
        raise Denied(401, "request start time out of range")
    return started


def _verify_token(token: str, secret: str, issuer: str, at: float, ctx: dict) -> dict:
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            issuer=issuer,
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iss", "email"], "verify_exp": False},
        )
    except jwt.PyJWTError as exc:
        raise Denied(401, f"user token rejected: {type(exc).__name__}", **ctx) from None
    try:
        expires_at = float(claims["exp"])
    except (TypeError, ValueError):
        raise Denied(401, "user token has no usable exp", **ctx) from None
    if at > expires_at + LEEWAY_SECONDS:
        raise Denied(401, "user token expired", **ctx)
    email = str(claims.get("email") or "").strip()
    if not email:
        raise Denied(401, "user token carries no email", **ctx)
    return {**claims, "email": email}
