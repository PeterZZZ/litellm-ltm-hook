# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""Enrolment on first use: a person Corp SSO knows gets memory without an admin.

Decided Oct 22 (Leo): signing in through Corp SSO proves membership of the
organisation, so the platform should not make an admin create every account.
When a request arrives for a person the registry does not know, and the
evidence for who they are is sound (Open WebUI's signed token, or a personal
key whose LiteLLM owner names them), the platform asks the Corp SSO directory.
A person the directory knows and has not disabled is created on the spot, with
the employee ID the directory holds and the `default` project, and that same
request is served. The audit log says `enrol`, so admins see who arrived; the
Admin tab marks the account as self-enrolled.

Without a directory (no MOCK_PLATFORM_OIDC_* settings) nobody is enrolled this
way: a signed token proves what Open WebUI believes, not membership.
MOCK_PLATFORM_AUTO_ENROL=0 turns it off and restores admin-only enrolment.
A failed lookup is remembered for 60 s so an unknown email does not hit the
directory on every request.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Callable, Optional

import httpx

from .authorize import NOT_ENROLLED
from .registry import Registry, User

log = logging.getLogger("mock_platform.enrol")

MISS_SECONDS = 60.0


class Enroller:
    def __init__(self, registry: Registry, sso=None, *, enabled: bool = True,
                 miss_seconds: float = MISS_SECONDS, now: Callable[[], float] = time.time):
        self.registry = registry
        self.sso = sso
        self.enabled = enabled
        self.miss_seconds = miss_seconds
        self.now = now
        self._misses: dict[str, float] = {}

    @property
    def configured(self) -> bool:
        return self.enabled and self.sso is not None

    async def by_email(self, email: str, via: str) -> Optional[User]:
        """Enrol the person behind a verified email, if Corp SSO knows them."""
        email = (email or "").strip()
        if not email or not self.configured or self._missed(email):
            return None
        try:
            entry = await self.sso.person(email)
        except httpx.HTTPError as exc:
            return self._miss(email, f"Corp SSO directory not reachable ({type(exc).__name__})")
        return self._admit(email, entry, via)

    async def by_person(self, person: str, via: str) -> Optional[User]:
        """Enrol by employee ID or email, whichever LiteLLM named as a key's owner."""
        person = (person or "").strip()
        if not person or not self.configured or self._missed(person):
            return None
        try:
            entry = (await self.sso.person(person) if "@" in person
                     else await self.sso.person_by_employee_id(person))
        except httpx.HTTPError as exc:
            return self._miss(person, f"Corp SSO directory not reachable ({type(exc).__name__})")
        return self._admit(person, entry, via)

    def _admit(self, who: str, entry, via: str) -> Optional[User]:
        if entry is None:
            return self._miss(who, "not in the Corp SSO directory")
        if not entry.enabled:
            return self._miss(who, "disabled in Corp SSO")
        existing = self.registry.user_by_email(entry.email)
        if existing is not None:
            return existing
        try:
            user = self.registry.add_user(entry.email, employee_id=entry.employee_id, enrolled_by=via)
        except sqlite3.IntegrityError:
            # A concurrent request enrolled them a moment ago.
            return self.registry.user_by_email(entry.email)
        log.info("enrol person=%s employee_id=%s via=%s", user.email, user.employee_id, via)
        self.registry.record(op="enrol", decision="allow", status=200, email=user.email, via=via,
                             namespace=f"{user.org_id}/{user.project_id}",
                             reason=f"enrolled on first use via {via}; Corp SSO employee ID "
                                    f"{user.employee_id or 'none'}")
        return user

    def _missed(self, who: str) -> bool:
        return self._misses.get(who.lower(), 0) > self.now()

    def _miss(self, who: str, reason: str) -> None:
        self._misses[who.lower()] = self.now() + self.miss_seconds
        if len(self._misses) > 1000:
            cutoff = self.now()
            self._misses = {k: t for k, t in self._misses.items() if t > cutoff}
        log.info("enrol person=%s decision=%s reason=%r", who, NOT_ENROLLED, reason)
        self.registry.record(op="enrol", decision=NOT_ENROLLED, status=403,
                             email=who if "@" in who else None,
                             reason=f"{who}: {reason}")
        return None
