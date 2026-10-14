# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""authorize(): turn the evidence a LiteLLM hook forwards into one namespace.

The hook decides nothing. It forwards three things and the platform decides:

    Authorization: Bearer <service credential>   which LiteLLM is calling
    X-MM-Key-Hash: <sha256 of the virtual key>    which LiteLLM key the caller used
    X-MM-User-Token: <Open WebUI signed token>    who the person is, on an app key

Rules, in order. Each refusal names the step that failed.

1. The service credential must be registered.                       401
2. A key hash must be present.                                      401
3. An unknown key is a person who has not been enrolled.        not enrolled
4. A personal key names its owner. A user token beside it is refused:
   tokens are honoured only on the app key they were issued for.    401
5. An app key names nobody, so a user token is required, and is verified
   here with that app's own secret and issuer.                      401
   Its email must belong to a registered user.                  not enrolled
6. A disabled user is not enrolled.                             not enrolled

"Not enrolled" is 403 with `X-MM-Decision: not-enrolled`. The hook treats that
one answer as "memory is off for this person" and carries on without memory;
every other 401/403 is a refusal it passes to the caller.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import jwt

from .registry import Registry, User

KEY_HASH_HEADER = "x-mm-key-hash"
USER_TOKEN_HEADER = "x-mm-user-token"
STARTED_AT_HEADER = "x-mm-request-started"
NOT_ENROLLED = "not-enrolled"

# Clock difference allowed between Open WebUI, which stamps the token, and here.
LEEWAY_SECONDS = 30
# How far back a hook may say a request started. The store path runs after the
# model has answered, so it asks for the token to be judged at request start;
# this bounds how stale that claim may be.
MAX_REQUEST_AGE_SECONDS = 15 * 60


class Denied(Exception):
    def __init__(self, status: int, reason: str, decision: str = "refused"):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.decision = decision


@dataclass(frozen=True)
class Principal:
    user: User
    service: str
    key_label: str
    via: str  # "personal-key" or "app-token"


def authorize(registry: Registry, headers, now: Optional[float] = None) -> Principal:
    now = time.time() if now is None else now

    # 1. Which LiteLLM is calling.
    auth = headers.get("authorization") or ""
    scheme, _, credential = auth.partition(" ")
    service = registry.service_label(credential.strip()) if scheme.lower() == "bearer" else None
    if not service:
        raise Denied(401, "unknown service credential")

    # 2. Which LiteLLM key the request came in on.
    key_hash = (headers.get(KEY_HASH_HEADER) or "").strip().lower()
    if not key_hash:
        raise Denied(401, "no virtual key evidence")

    # 3. Does the platform know that key at all.
    key = registry.key(key_hash)
    if key is None:
        raise Denied(403, "virtual key not registered", NOT_ENROLLED)

    token = (headers.get(USER_TOKEN_HEADER) or "").strip()

    if key.kind == "personal":
        # 4. The key is the person.
        if token:
            raise Denied(401, "user token presented on a personal key")
        user = registry.user(key.user_id)
        via = "personal-key"
    else:
        # 5. The key is an application; the token is the person.
        if not token:
            raise Denied(401, "app key without a user token")
        app = registry.app(key.key_hash)
        if app is None:
            raise Denied(401, "app key has no token configuration")
        email = _verify_token(token, app.jwt_secret, app.issuer, _judge_at(headers, now))
        user = registry.user_by_email(email)
        if user is None:
            raise Denied(403, "token user not registered", NOT_ENROLLED)
        via = "app-token"

    # 6. Memory switched off for this person.
    if not user.enabled:
        raise Denied(403, "user disabled", NOT_ENROLLED)

    return Principal(user=user, service=service, key_label=key.label, via=via)


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


def _verify_token(token: str, secret: str, issuer: str, at: float) -> str:
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
        raise Denied(401, f"user token rejected: {type(exc).__name__}") from None
    try:
        expires_at = float(claims["exp"])
    except (TypeError, ValueError):
        raise Denied(401, "user token has no usable exp") from None
    if at > expires_at + LEEWAY_SECONDS:
        raise Denied(401, "user token expired")
    email = str(claims.get("email") or "").strip()
    if not email:
        raise Denied(401, "user token carries no email")
    return email
