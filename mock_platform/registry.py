# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""The mock platform's registry: who exists, which keys they hold, who may call.

SQLite, five tables, nothing a real tenant control server would not also need:

    services  one row per LiteLLM deployment allowed to call the platform. Only
              the SHA-256 of its credential is stored, as LiteLLM stores keys.
    users     one row per person: their employee ID (from the SSO directory),
              the one namespace this mock gives them, their platform role (user
              or admin), whether memory is on, and whether requests from Open
              WebUI may reach it.
    keys      LiteLLM virtual keys the platform knows, by their SHA-256 hash,
              which is the only form the hook ever sees. A *personal* key names
              its owner and can be switched off by them. An *app* key (Open
              WebUI's shared key) names nobody, so a request on it must carry a
              signed user token instead.
    apps      for each app key, how to verify the tokens that may ride on it,
              and which LiteLLM service user owns the key: LiteLLM's owner must
              still be that user on every call, as it must for personal keys.
    audit     every authorization decision, allowed or not, newest kept.

A person is named by their employee ID, which is also their org; until one is
known (phase 1, no SSO) the email stands in, slugified for the org.

The platform role is the platform's own: being an Open WebUI admin grants nothing
here. The app signing secret is stored as given; a real platform would keep it
in a secret store.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
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
CREATE TABLE IF NOT EXISTS audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        REAL NOT NULL,
    op        TEXT NOT NULL,
    decision  TEXT NOT NULL,
    status    INTEGER NOT NULL,
    reason    TEXT,
    service   TEXT,
    via       TEXT,
    key_label TEXT,
    email     TEXT,
    namespace TEXT
);
CREATE INDEX IF NOT EXISTS audit_email ON audit (email, id);
"""

# Columns added after the first release: (table, column, definition). Applied in
# place to an existing registry so nobody has to re-enrol.
_MIGRATIONS = (
    ("users", "role", "TEXT NOT NULL DEFAULT 'user'"),
    ("users", "allow_app", "INTEGER NOT NULL DEFAULT 1"),
    ("users", "created_at", "REAL"),
    ("keys", "enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("users", "employee_id", "TEXT"),
    ("apps", "owner", "TEXT"),
)
AUDIT_KEEP = 5000
ROLES = ("user", "admin")

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
    role: str = "user"
    allow_app: bool = True
    created_at: Optional[float] = None
    employee_id: Optional[str] = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def person_id(self) -> str:
        """Who this is, as the hook names them: the employee ID, else the email."""
        return self.employee_id or self.email


@dataclass(frozen=True)
class Key:
    key_hash: str
    kind: str
    user_id: Optional[str]
    label: str
    enabled: bool = True


@dataclass(frozen=True)
class App:
    key_hash: str
    jwt_secret: str
    issuer: str
    # LiteLLM's user_id for the key: a service user, never a person. None when
    # the key has no owner in LiteLLM; an owner appearing later is a reassignment.
    owner: Optional[str] = None


class Registry:
    def __init__(self, path: str):
        self.path = path
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)
        for table, column, definition in _MIGRATIONS:
            have = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                with self._db:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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
        return _key(row)

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

    def personal_keys(self, user_id: str) -> list[Key]:
        rows = self._db.execute(
            "SELECT * FROM keys WHERE kind = 'personal' AND user_id = ? ORDER BY label", (user_id,)
        ).fetchall()
        return [_key(r) for r in rows]

    def users(self) -> list[User]:
        return [_user(r) for r in self._db.execute("SELECT * FROM users ORDER BY email")]

    def app_keys(self) -> list[dict]:
        return [dict(r) for r in self._db.execute(
            "SELECT k.label, a.issuer, a.owner, substr(k.key_hash, 1, 12) AS key_hash "
            "FROM keys k JOIN apps a ON a.key_hash = k.key_hash ORDER BY k.label")]

    def app_owners(self) -> set[str]:
        """LiteLLM users that own app keys: service users, never a person."""
        return {r["owner"].lower() for r in self._db.execute(
            "SELECT owner FROM apps WHERE owner IS NOT NULL AND owner != ''")}

    def app_labels(self) -> list[str]:
        return [a["label"] for a in self.app_keys()]

    def services(self) -> list[str]:
        return [r["label"] for r in self._db.execute("SELECT label FROM services ORDER BY label")]

    # ---- audit ----

    def record(self, **entry) -> None:
        cols = ("op", "decision", "status", "reason", "service", "via", "key_label",
                "email", "namespace")
        with self._db:
            self._db.execute(
                f"INSERT INTO audit (at, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
                (time.time(), *(entry.get(c) for c in cols)),
            )
            self._db.execute(
                "DELETE FROM audit WHERE id <= (SELECT max(id) FROM audit) - ?", (AUDIT_KEEP,)
            )

    def audit(self, limit: int = 100, email: Optional[str] = None) -> list[dict]:
        if email:
            rows = self._db.execute(
                "SELECT * FROM audit WHERE lower(email) = lower(?) ORDER BY id DESC LIMIT ?",
                (email, limit))
        else:
            rows = self._db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def last_seen(self) -> dict[str, float]:
        return {r["email"].lower(): r["at"] for r in self._db.execute(
            "SELECT email, max(at) AS at FROM audit WHERE email IS NOT NULL "
            "AND decision = 'allow' GROUP BY lower(email)")}

    # ---- writes, used by the admin CLI and the portal ----

    def add_service(self, label: str) -> str:
        credential = "mm-svc-" + secrets.token_urlsafe(32)
        with self._db:
            self._db.execute(
                "INSERT INTO services (credential_hash, label) VALUES (?, ?)",
                (sha256(credential), label),
            )
        return credential

    def add_user(self, email: str, project_id: str = "default", role: str = "user",
                 employee_id: Optional[str] = None) -> User:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        email = email.strip()
        user_id = slug(email)
        employee_id = (employee_id or "").strip() or None
        with self._db:
            self._db.execute(
                "INSERT INTO users (user_id, email, employee_id, org_id, project_id, role, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, email, employee_id, slug(employee_id or email), project_id, role,
                 time.time()),
            )
        return self.user(user_id)

    def set_employee_id(self, email: str, employee_id: Optional[str]) -> User:
        """Record the person's employee ID; their org follows it.

        Memory already stored under the old org stays there: core cannot move it.
        """
        employee_id = (employee_id or "").strip() or None
        user = self._require(email)
        with self._db:
            self._db.execute(
                "UPDATE users SET employee_id = ?, org_id = ? WHERE user_id = ?",
                (employee_id, slug(employee_id or user.email), user.user_id),
            )
        return self.user(user.user_id)

    def delete_user(self, email: str) -> User:
        user = self._require(email)
        with self._db:
            self._db.execute("DELETE FROM keys WHERE kind = 'personal' AND user_id = ?", (user.user_id,))
            self._db.execute("DELETE FROM users WHERE user_id = ?", (user.user_id,))
        return user

    def set_enabled(self, email: str, enabled: bool) -> User:
        return self._set(email, "enabled", int(enabled))

    def set_allow_app(self, email: str, allowed: bool) -> User:
        return self._set(email, "allow_app", int(allowed))

    def set_role(self, email: str, role: str) -> User:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        return self._set(email, "role", role)

    def set_key_enabled(self, user_id: str, key_hash_value: str, enabled: bool) -> Key:
        with self._db:
            cur = self._db.execute(
                "UPDATE keys SET enabled = ? WHERE key_hash = ? AND kind = 'personal' AND user_id = ?",
                (int(enabled), key_hash_value, user_id),
            )
        if cur.rowcount != 1:
            raise KeyError(key_hash_value)
        return self.key(key_hash_value)

    def link_personal_key(self, email: str, raw_or_hash: str, label: str) -> Key:
        user = self._require(email)
        h = key_hash(raw_or_hash)
        with self._db:
            self._db.execute(
                "INSERT INTO keys (key_hash, kind, user_id, label) VALUES (?, 'personal', ?, ?)",
                (h, user.user_id, label),
            )
        return self.key(h)

    def add_app_key(self, raw_or_hash: str, label: str, jwt_secret: str, issuer: str,
                    owner: Optional[str] = None) -> Key:
        h = key_hash(raw_or_hash)
        with self._db:
            self._db.execute(
                "INSERT INTO keys (key_hash, kind, user_id, label) VALUES (?, 'app', NULL, ?)",
                (h, label),
            )
            self._db.execute(
                "INSERT INTO apps (key_hash, jwt_secret, issuer, owner) VALUES (?, ?, ?, ?)",
                (h, jwt_secret, issuer, (owner or "").strip() or None),
            )
        return self.key(h)

    def set_app_owner(self, raw_or_hash: str, owner: Optional[str]) -> App:
        """Record which LiteLLM service user owns an app key."""
        h = key_hash(raw_or_hash)
        with self._db:
            cur = self._db.execute("UPDATE apps SET owner = ? WHERE key_hash = ?",
                                   ((owner or "").strip() or None, h))
        if cur.rowcount != 1:
            raise KeyError(h)
        return self.app(h)

    def unlink_key(self, raw_or_hash: str) -> None:
        h = key_hash(raw_or_hash)
        with self._db:
            self._db.execute("DELETE FROM apps WHERE key_hash = ?", (h,))
            self._db.execute("DELETE FROM keys WHERE key_hash = ?", (h,))

    def dump(self) -> dict:
        q = lambda sql: [dict(r) for r in self._db.execute(sql)]  # noqa: E731
        return {
            "services": q("SELECT label FROM services ORDER BY label"),
            "users": q("SELECT user_id, email, employee_id, role, org_id, project_id, enabled, "
                       "allow_app "
                       "FROM users ORDER BY email"),
            "keys": q(
                "SELECT label, kind, user_id, enabled, substr(key_hash, 1, 12) AS key_hash "
                "FROM keys ORDER BY label"
            ),
        }

    def _require(self, email: str) -> User:
        user = self.user_by_email(email)
        if user is None:
            raise KeyError(email)
        return user

    def _set(self, email: str, column: str, value) -> User:
        user = self._require(email)
        with self._db:
            self._db.execute(f"UPDATE users SET {column} = ? WHERE user_id = ?", (value, user.user_id))
        return self.user(user.user_id)


def _user(row) -> Optional[User]:
    if row is None:
        return None
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    d["allow_app"] = bool(d.get("allow_app", 1))
    return User(**d)


def _key(row) -> Optional[Key]:
    if row is None:
        return None
    d = dict(row)
    d["enabled"] = bool(d.get("enabled", 1))
    return Key(**d)
