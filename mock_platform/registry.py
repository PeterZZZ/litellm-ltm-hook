# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""The mock platform's registry: who exists, which keys they hold, who may call.

SQLite, four tables, nothing a real tenant control server would not also need:

    services  one row per LiteLLM deployment allowed to call the platform. Only
              the SHA-256 of its credential is stored, as LiteLLM stores keys.
    users     one row per person, with the one namespace this mock gives them.
    keys      LiteLLM virtual keys the platform knows, by their SHA-256 hash,
              which is the only form the hook ever sees. A *personal* key names
              its owner. An *app* key (Open WebUI's shared key) names nobody, so
              a request on it must carry a signed user token instead.
    apps      for each app key, how to verify the tokens that may ride on it.

The app signing secret is stored as given. That is acceptable for a mock and is
the first thing a real platform would move into a secret store.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS services (
    credential_hash TEXT PRIMARY KEY,
    label           TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    email      TEXT NOT NULL UNIQUE,
    org_id     TEXT NOT NULL,
    project_id TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS keys (
    key_hash TEXT PRIMARY KEY,
    kind     TEXT NOT NULL CHECK (kind IN ('personal', 'app')),
    user_id  TEXT REFERENCES users(user_id),
    label    TEXT NOT NULL,
    CHECK ((kind = 'personal') = (user_id IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS apps (
    key_hash   TEXT PRIMARY KEY REFERENCES keys(key_hash),
    jwt_secret TEXT NOT NULL,
    issuer     TEXT NOT NULL
);
"""

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
# MemMachine's org_id/project_id character set, as the hook slugifies them.
_ORG_ID_ALLOWED = re.compile(r"[^A-Za-z0-9_:\-]")


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def key_hash(raw_or_hash: str) -> str:
    """Accept a raw `sk-` key or its SHA-256, return the SHA-256.

    LiteLLM stores and exposes virtual keys as the hex SHA-256 of the raw key, so
    an admin may register either the key they were shown or the hash LiteLLM's
    /key/list reports.
    """
    value = raw_or_hash.strip()
    return value if _HEX64.match(value) else sha256(value)


def slug(value: str) -> str:
    return _ORG_ID_ALLOWED.sub("_", value).strip("_") or "user"


@dataclass(frozen=True)
class User:
    user_id: str
    email: str
    org_id: str
    project_id: str
    enabled: bool


@dataclass(frozen=True)
class Key:
    key_hash: str
    kind: str
    user_id: Optional[str]
    label: str


@dataclass(frozen=True)
class App:
    key_hash: str
    jwt_secret: str
    issuer: str


class Registry:
    def __init__(self, path: str):
        self.path = path
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)

    # ---- reads, used on every request ----

    def service_label(self, credential: str) -> Optional[str]:
        row = self._db.execute(
            "SELECT label FROM services WHERE credential_hash = ?", (sha256(credential),)
        ).fetchone()
        return row["label"] if row else None

    def key(self, key_hash_value: str) -> Optional[Key]:
        row = self._db.execute(
            "SELECT * FROM keys WHERE key_hash = ?", (key_hash_value,)
        ).fetchone()
        return Key(**dict(row)) if row else None

    def app(self, key_hash_value: str) -> Optional[App]:
        row = self._db.execute(
            "SELECT * FROM apps WHERE key_hash = ?", (key_hash_value,)
        ).fetchone()
        return App(**dict(row)) if row else None

    def user(self, user_id: str) -> Optional[User]:
        row = self._db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return _user(row)

    def user_by_email(self, email: str) -> Optional[User]:
        # Emails are matched without regard to case here, unlike the hook's own
        # identity path: the platform owns the mapping, so it can normalise once
        # without moving anyone's memory.
        row = self._db.execute(
            "SELECT * FROM users WHERE lower(email) = lower(?)", (email.strip(),)
        ).fetchone()
        return _user(row)

    # ---- writes, used by the admin CLI ----

    def add_service(self, label: str) -> str:
        credential = "mm-svc-" + secrets.token_urlsafe(32)
        with self._db:
            self._db.execute(
                "INSERT INTO services (credential_hash, label) VALUES (?, ?)",
                (sha256(credential), label),
            )
        return credential

    def add_user(self, email: str, project_id: str = "default") -> User:
        email = email.strip()
        user_id = slug(email)
        with self._db:
            self._db.execute(
                "INSERT INTO users (user_id, email, org_id, project_id) VALUES (?, ?, ?, ?)",
                (user_id, email, user_id, project_id),
            )
        return self.user(user_id)

    def set_enabled(self, email: str, enabled: bool) -> User:
        user = self.user_by_email(email)
        if user is None:
            raise KeyError(email)
        with self._db:
            self._db.execute(
                "UPDATE users SET enabled = ? WHERE user_id = ?", (int(enabled), user.user_id)
            )
        return self.user(user.user_id)

    def link_personal_key(self, email: str, raw_or_hash: str, label: str) -> Key:
        user = self.user_by_email(email)
        if user is None:
            raise KeyError(email)
        h = key_hash(raw_or_hash)
        with self._db:
            self._db.execute(
                "INSERT INTO keys (key_hash, kind, user_id, label) VALUES (?, 'personal', ?, ?)",
                (h, user.user_id, label),
            )
        return self.key(h)

    def add_app_key(self, raw_or_hash: str, label: str, jwt_secret: str, issuer: str) -> Key:
        h = key_hash(raw_or_hash)
        with self._db:
            self._db.execute(
                "INSERT INTO keys (key_hash, kind, user_id, label) VALUES (?, 'app', NULL, ?)",
                (h, label),
            )
            self._db.execute(
                "INSERT INTO apps (key_hash, jwt_secret, issuer) VALUES (?, ?, ?)",
                (h, jwt_secret, issuer),
            )
        return self.key(h)

    def unlink_key(self, raw_or_hash: str) -> None:
        h = key_hash(raw_or_hash)
        with self._db:
            self._db.execute("DELETE FROM apps WHERE key_hash = ?", (h,))
            self._db.execute("DELETE FROM keys WHERE key_hash = ?", (h,))

    def dump(self) -> dict:
        q = lambda sql: [dict(r) for r in self._db.execute(sql)]  # noqa: E731
        return {
            "services": q("SELECT label FROM services ORDER BY label"),
            "users": q("SELECT user_id, email, org_id, project_id, enabled FROM users ORDER BY email"),
            "keys": q(
                "SELECT label, kind, user_id, substr(key_hash, 1, 12) AS key_hash "
                "FROM keys ORDER BY label"
            ),
        }


def _user(row) -> Optional[User]:
    if row is None:
        return None
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    return User(**d)
