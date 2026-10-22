# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""Corp SSO for the platform: Keycloak, over plain OpenID Connect.

Two jobs, one confidential client ("memmachine-platform"):

  sign-in   the portal sends people to Keycloak (authorization code flow) and
            checks the ID token that comes back: RS256 against Keycloak's
            published keys, its issuer, audience, expiry and the nonce the portal
            made for that login.
  directory the platform asks Keycloak, with the client's own service account
            (realm-management/view-users, read-only), for a person's employee ID
            by email. That is how an admin's "create account" gets the right ID,
            and how the platform knows what a chat request's user must be.

Keycloak is the authority for who someone is and their employee ID; the
platform's registry keeps a copy, refreshed when an admin syncs an account.
"""
from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import httpx
import jwt

CLOCK_LEEWAY = 30


@dataclass(frozen=True)
class Person:
    email: str
    employee_id: Optional[str]
    name: str
    enabled: bool


class Keycloak:
    def __init__(self, issuer: str, client_id: str, client_secret: str,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self._transport = transport
        self._meta: Optional[dict] = None
        self._jwks: Optional[jwt.PyJWKSet] = None
        self._sa_token: tuple[str, float] = ("", 0.0)

    @property
    def realm(self) -> str:
        return self.issuer.rsplit("/", 1)[-1]

    @property
    def admin_base(self) -> str:
        base = self.issuer.split("/realms/")[0]
        return f"{base}/admin/realms/{self.realm}"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=15, transport=self._transport)

    async def metadata(self) -> dict:
        if self._meta is None:
            async with self._client() as c:
                r = await c.get(f"{self.issuer}/.well-known/openid-configuration")
                r.raise_for_status()
                self._meta = r.json()
        return self._meta

    # ---------- sign-in ----------

    async def login_url(self, redirect_uri: str, state: str, nonce: str) -> str:
        meta = await self.metadata()
        q = urllib.parse.urlencode({
            "client_id": self.client_id, "response_type": "code", "scope": "openid email profile",
            "redirect_uri": redirect_uri, "state": state, "nonce": nonce,
        })
        return f"{meta['authorization_endpoint']}?{q}"

    async def logout_url(self, id_token: str, redirect_uri: str) -> Optional[str]:
        end = (await self.metadata()).get("end_session_endpoint")
        if not end:
            return None
        q = urllib.parse.urlencode({"id_token_hint": id_token, "client_id": self.client_id,
                                    "post_logout_redirect_uri": redirect_uri})
        return f"{end}?{q}"

    async def finish_login(self, code: str, redirect_uri: str, nonce: str) -> tuple[dict, str]:
        """Swap the code for tokens; return the verified ID-token claims and the raw ID token."""
        meta = await self.metadata()
        async with self._client() as c:
            r = await c.post(meta["token_endpoint"], data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "client_id": self.client_id, "client_secret": self.client_secret,
            })
        if r.status_code != 200:
            raise ValueError(f"token exchange failed ({r.status_code})")
        raw = r.json().get("id_token") or ""
        claims = await self.verify_id_token(raw)
        if claims.get("nonce") != nonce:
            raise ValueError("ID token nonce does not match this sign-in")
        return claims, raw

    async def verify_id_token(self, raw: str) -> dict:
        kid = jwt.get_unverified_header(raw).get("kid")
        key = await self._signing_key(kid)
        return jwt.decode(raw, key, algorithms=["RS256"], audience=self.client_id,
                          issuer=self.issuer, leeway=CLOCK_LEEWAY,
                          options={"require": ["exp", "iat", "iss", "aud", "sub"]})

    async def _signing_key(self, kid: Optional[str]):
        for attempt in range(2):
            if self._jwks is None or attempt:
                async with self._client() as c:
                    r = await c.get((await self.metadata())["jwks_uri"])
                    r.raise_for_status()
                    self._jwks = jwt.PyJWKSet.from_dict(r.json())
            for k in self._jwks.keys:
                if k.key_id == kid:
                    return k.key
        raise ValueError("ID token signed with an unknown key")

    # ---------- directory ----------

    async def _service_token(self) -> str:
        token, expires = self._sa_token
        if token and expires > time.time() + 10:
            return token
        meta = await self.metadata()
        async with self._client() as c:
            r = await c.post(meta["token_endpoint"], data={
                "grant_type": "client_credentials",
                "client_id": self.client_id, "client_secret": self.client_secret,
            })
        r.raise_for_status()
        body = r.json()
        self._sa_token = (body["access_token"], time.time() + float(body.get("expires_in", 60)))
        return self._sa_token[0]

    async def person(self, email: str) -> Optional[Person]:
        """The directory entry for this email, or None if Corp SSO has no such person."""
        email = email.strip()
        return await self._find({"email": email, "exact": "true"},
                                lambda p: p.email.lower() == email.lower())

    async def person_by_employee_id(self, employee_id: str) -> Optional[Person]:
        """The directory entry with this employee ID (an attribute search)."""
        employee_id = employee_id.strip()
        return await self._find({"q": f"employee_id:{employee_id}", "exact": "true"},
                                lambda p: (p.employee_id or "").lower() == employee_id.lower())

    async def _find(self, params: dict, match) -> Optional[Person]:
        token = await self._service_token()
        async with self._client() as c:
            r = await c.get(f"{self.admin_base}/users", params=params,
                            headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        for u in r.json():
            if not u.get("email"):
                continue
            attrs = u.get("attributes") or {}
            emp = (attrs.get("employee_id") or [None])[0]
            name = " ".join(x for x in (u.get("firstName"), u.get("lastName")) if x)
            p = Person(email=u["email"], employee_id=emp, name=name, enabled=bool(u.get("enabled", True)))
            if match(p):
                return p
        return None
