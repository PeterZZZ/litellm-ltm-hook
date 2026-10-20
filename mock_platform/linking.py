# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""Keys the platform learns about from LiteLLM itself.

A personal key the platform has never seen is looked up in LiteLLM once, with
the platform's own read-only key, and linked to its owner if that owner is a
known person. The call that brought the key is then served; nobody presses
Sync first. Grants belong to the person, so a rotated key inherits them.

The platform believes LiteLLM about who owns a key, never the caller: the
owner the hook names (X-MemMachine-Key-Owner) must agree with LiteLLM's record,
and the record must name a person, not a service user. A key that is blocked,
expired or a team key names no one person and is not linked.

Reconcile runs every few minutes and on an admin's request: it re-reads every
key LiteLLM holds and drops linked keys that are gone, blocked, expired or
whose owner changed, and links new keys of known people. A personal key
unlinked because its owner changed stays refused (`key-reassigned`) until an
admin syncs the new owner's account: a reassignment is an admin's to accept.
An app key is shared by everyone behind that app, so it is suspended rather
than dropped: its registration stays, it is refused while LiteLLM names anyone
but its service user, and it serves again as soon as that is put right.

What is deliberately not here: creating a person the first time an unknown
owner turns up (Crystal's "default space for everyone"). That is Leo's
decision; `person_for_unknown_owner` is where it would go.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx

from .authorize import KEY_REASSIGNED, NOT_ENROLLED
from .registry import Key, Registry, User

log = logging.getLogger("mock_platform.linking")

# How long a failed lookup is remembered, so a bad key does not hit LiteLLM on
# every request.
MISS_SECONDS = 60.0
# How often reconcile runs on its own.
RECONCILE_SECONDS = 600.0


class KeyLinker:
    def __init__(self, registry: Registry, litellm_url: Optional[str], litellm_key: Optional[str],
                 client: Callable[[], httpx.AsyncClient], *, miss_seconds: float = MISS_SECONDS,
                 now: Callable[[], float] = time.time):
        self.registry = registry
        self.litellm_url = (litellm_url or "").rstrip("/") or None
        self.litellm_key = litellm_key or None
        self.client = client
        self.miss_seconds = miss_seconds
        self.now = now
        self._misses: dict[str, float] = {}  # key hash -> when the miss may be retried

    @property
    def configured(self) -> bool:
        return bool(self.litellm_url and self.litellm_key)

    # ---- on a request ----

    async def link_unknown(self, key_hash: str, owner: str) -> Optional[Key]:
        """A key the registry does not hold: look it up in LiteLLM once; link it
        if LiteLLM says it is a known person's. None when it is not linked."""
        if not self.configured or not key_hash:
            return None
        if self.registry.revoked(key_hash):
            # Unlinked by reconcile because LiteLLM reassigned it; an admin's
            # Sync on the new owner is the step that accepts that.
            return None
        if self._misses.get(key_hash, 0) > self.now():
            return None
        reason = await self._try_link(key_hash, owner)
        if reason is None:
            return self.registry.key(key_hash)
        self._misses[key_hash] = self.now() + self.miss_seconds
        if len(self._misses) > 1000:
            cutoff = self.now()
            self._misses = {h: t for h, t in self._misses.items() if t > cutoff}
        log.info("link key=%s… decision=%s reason=%r", key_hash[:12], NOT_ENROLLED, reason)
        self.registry.record(op="link", decision=NOT_ENROLLED, status=403,
                             reason=f"key {key_hash[:12]}…: {reason}")
        return None

    async def _try_link(self, key_hash: str, owner: str) -> Optional[str]:
        """Link the key, or say in a few words why not."""
        async with self.client() as c:
            try:
                resp = await c.get(f"{self.litellm_url}/key/info", params={"key": key_hash},
                                   headers=self._auth())
            except httpx.HTTPError as exc:
                return f"LiteLLM not reachable ({type(exc).__name__})"
            if resp.status_code == 404:
                return "LiteLLM does not know this key"
            if resp.status_code != 200:
                return f"LiteLLM key lookup failed ({resp.status_code})"
            info = (resp.json() or {}).get("info") or {}
            reason = _unusable(info)
            if reason:
                return reason
            litellm_owner = str(info.get("user_id") or "").strip()
            if not litellm_owner:
                return "the key has no owner in LiteLLM"
            if litellm_owner.lower() != (owner or "").strip().lower():
                return f"LiteLLM names {litellm_owner!r} as the owner, the hook said {owner!r}"
            if litellm_owner.lower() in self.registry.app_owners():
                return f"{litellm_owner!r} is a service user, not a person"
        user = self._person(litellm_owner)
        if user is None:
            user = self.person_for_unknown_owner(litellm_owner, info)
        if user is None:
            known = self.registry.user_by_person(litellm_owner)
            if known is not None:
                return (f"LiteLLM names the owner {litellm_owner!r}; the platform knows that "
                        f"person as {known.person_id!r}, which the hook would have to send")
            return f"owner {litellm_owner!r} is not an enrolled person"
        label = str(info.get("key_alias") or "").strip() or f"{litellm_owner}-{key_hash[:6]}"
        try:
            self.registry.link_personal_key(user.email, key_hash, label)
        except sqlite3.IntegrityError:
            # Linked by a concurrent request a moment ago; same answer.
            pass
        log.info("link key=%s… decision=allow person=%s label=%s", key_hash[:12], user.person_id, label)
        self.registry.record(op="link", decision="allow", status=200, email=user.email,
                             key_label=label, via="personal-key",
                             reason=f"linked on first call; LiteLLM owner {litellm_owner}")
        return None

    def _person(self, litellm_owner: str) -> Optional[User]:
        """The person behind a LiteLLM user_id: their employee ID, or their email
        when the platform has no employee ID for them. That is also how the hook
        names them, so a key linked here is a key authorize() will serve; a
        LiteLLM user named by the email of someone known by employee ID is
        looked up by either identifier but not linked."""
        user = self.registry.user_by_person(litellm_owner)
        if user is None or litellm_owner.strip().lower() != user.person_id.lower():
            return None
        return user

    def person_for_unknown_owner(self, litellm_owner: str, info: dict) -> Optional[User]:
        """Where creating a person for an owner the registry does not know would go.

        Crystal asked for a default space for everyone without an admin step;
        whether the platform does that is Leo's decision. Until then an unknown
        owner stays not enrolled.
        """
        return None

    # ---- every few minutes, and on request ----

    async def reconcile(self) -> dict:
        """Re-read every key LiteLLM holds; drop what no longer names the person
        it was linked to; link new keys of known people."""
        if not self.configured:
            return {"lookup": "not configured", "linked": [], "unlinked": [], "checked": 0}
        async with self.client() as c:
            live = await self._all_keys(c)
            linked, unlinked = [], []
            for key in self.registry.keys():
                info = live.get(key.key_hash)
                why = self._why_unlink(key, info)
                if why and key.kind == "app" and info is not None:
                    # Shared by everyone behind the app: suspend, do not forget.
                    if not self.registry.revoked(key.key_hash):
                        self.registry.suspend_key(key.key_hash, why)
                        unlinked.append(f"{key.label} (suspended: {why})")
                        self.registry.record(op="reconcile", decision=KEY_REASSIGNED, status=403,
                                             key_label=key.label, reason=f"suspended: {why}")
                elif why:
                    self.registry.unlink_key(key.key_hash, reason=why)
                    unlinked.append(f"{key.label} ({why})")
                    self.registry.record(op="reconcile", decision=KEY_REASSIGNED if "owner" in why
                                         else NOT_ENROLLED, status=403, key_label=key.label,
                                         email=self._email_of(key), reason=f"unlinked: {why}")
                elif key.kind == "app" and self.registry.revoked(key.key_hash):
                    self.registry.forget_revoked(key.key_hash)
                    linked.append(f"{key.label} (suspension lifted)")
                    self.registry.record(op="reconcile", decision="allow", status=200,
                                         key_label=key.label,
                                         reason="suspension lifted: LiteLLM names its service user again")
            for h in self.registry.revoked_keys():
                if h not in live:
                    self.registry.forget_revoked(h)
            for h, info in live.items():
                if self.registry.key(h) is not None or self.registry.revoked(h) or _unusable(info):
                    continue
                owner = str(info.get("user_id") or "").strip()
                if not owner or owner.lower() in self.registry.app_owners():
                    continue
                user = self._person(owner)
                if user is None:
                    continue
                label = str(info.get("key_alias") or "").strip() or f"{owner}-{h[:6]}"
                self.registry.link_personal_key(user.email, h, label)
                linked.append(label)
                self.registry.record(op="reconcile", decision="allow", status=200, email=user.email,
                                     key_label=label, via="personal-key",
                                     reason=f"linked; LiteLLM owner {owner}")
        self._misses.clear()
        log.info("reconcile checked=%d linked=%s unlinked=%s", len(live), linked, unlinked)
        return {"lookup": "ok", "linked": linked, "unlinked": unlinked, "checked": len(live)}

    def _why_unlink(self, key: Key, info: Optional[dict]) -> Optional[str]:
        if info is None:
            return "deleted in LiteLLM"
        owner = str(info.get("user_id") or "").strip()
        if key.kind == "app":
            app = self.registry.app(key.key_hash)
            recorded = (app.owner if app else "") or ""
            if owner.lower() != recorded.lower():
                return f"owner changed to {owner or 'no one'!r} in LiteLLM"
            return None
        reason = _unusable(info)
        if reason:
            return reason
        user = self.registry.user(key.user_id) if key.user_id else None
        if user is None:
            return "its person is gone"
        if owner.lower() != user.person_id.lower():
            return f"owner changed to {owner or 'no one'!r} in LiteLLM"
        return None

    def _email_of(self, key: Key) -> Optional[str]:
        user = self.registry.user(key.user_id) if key.user_id else None
        return user.email if user else None

    async def _all_keys(self, c: httpx.AsyncClient) -> dict[str, dict]:
        keys: dict[str, dict] = {}
        page = 1
        while True:
            resp = await c.get(f"{self.litellm_url}/key/list",
                               params={"page": page, "size": 100, "return_full_object": "true"},
                               headers=self._auth())
            if resp.status_code != 200:
                raise RuntimeError(f"LiteLLM key list failed ({resp.status_code})")
            data = resp.json() or {}
            for k in data.get("keys") or []:
                if isinstance(k, dict) and k.get("token"):
                    keys[str(k["token"]).lower()] = k
            if page >= int(data.get("total_pages") or 1):
                break
            page += 1
        return keys

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.litellm_key}"}


def _unusable(info: dict) -> Optional[str]:
    """Why a LiteLLM key record names no one person right now."""
    if info.get("blocked"):
        return "blocked in LiteLLM"
    if info.get("team_id"):
        return "a team key, not a person's"
    expires = info.get("expires")
    if expires:
        try:
            at = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
        except ValueError:
            return "expiry not readable"
        if at <= datetime.now(timezone.utc):
            return "expired in LiteLLM"
    return None
