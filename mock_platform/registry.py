# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""The mock platform's registry: who exists, which keys they hold, who may call.

SQLite, eight tables, nothing a real tenant control server would not also need:

    services  one row per LiteLLM deployment allowed to call the platform. Only
              the SHA-256 of its credential is stored, as LiteLLM stores keys.
    users     one row per person: their employee ID (from the SSO directory),
              their org and default project, their platform role (user or
              admin), whether memory is on, and whether requests from Open
              WebUI may reach it.
    grants    which projects a person may use inside their org: one row per
              project name, or the single entry `*` for any project. A new
              person gets `default`. The hook proposes a project per request;
              the platform allows it only if granted.
    shares    a project of one person's org that another person may use, under
              a name (alias) in that person's own view: the hook sends the
              alias, the platform resolves it to <owner org>/<project>. The
              owner shares and unshares; the member may leave. `default` is
              never shared. Members read and write; the producer stamped on
              every message says who wrote what.
    keys      LiteLLM virtual keys the platform knows, by their SHA-256 hash,
              which is the only form the hook ever sees. A *personal* key names
              its owner and can be switched off by them. An *app* key (Open
              WebUI's shared key) names nobody, so a request on it must carry a
              signed user token instead.
    revoked   keys reconcile found reassigned in LiteLLM: refused
              (`key-reassigned`) rather than served as whoever LiteLLM now
              names. A personal key is dropped and stays refused until an
              admin syncs the new owner's account; an app key keeps its
              registration (secret, issuer, owner) and is suspended until
              LiteLLM's owner is its service user again.
    apps      for each app key, how to verify the tokens that may ride on it,
              and which LiteLLM service user owns the key: LiteLLM's owner must
              still be that user on every call, as it must for personal keys.
    audit     every authorization decision, allowed or not, newest kept.

A person is named by their employee ID, which is also their org; until one is
known (phase 1, no SSO) the email stands in, slugified for the org. Memory
lives in `<org>/<project>`: one org per person, one or more projects in it.

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
CREATE TABLE IF NOT EXISTS revoked (
    key_hash TEXT PRIMARY KEY,
    reason   TEXT NOT NULL,
    at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    project TEXT NOT NULL,
    PRIMARY KEY (user_id, project)
);
CREATE TABLE IF NOT EXISTS shares (
    owner_id  TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    project   TEXT NOT NULL,
    member_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    alias     TEXT NOT NULL,
    shared_by TEXT,
    at        REAL NOT NULL,
    seen      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (member_id, alias),
    UNIQUE (owner_id, project, member_id)
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
    # NULL = created by an admin or the CLI; else how the person enrolled
    # themselves on first use ("openwebui", "personal-key", "portal").
    ("users", "enrolled_by", "TEXT"),
)
AUDIT_KEEP = 5000
ROLES = ("user", "admin")
ANY_PROJECT = "*"

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
class Share:
    """A project of `owner`'s org that a member uses under `alias`."""
    owner_id: str
    owner_org: str
    owner_email: str
    owner_person: str
    project: str
    alias: str
    seen: bool
    at: float
    shared_by: Optional[str] = None


@dataclass(frozen=True)
class Namespace:
    """Where a project name a person uses actually lives in core."""
    org_id: str
    project_id: str
    owner_id: str          # the person whose org it is
    shared: bool = False   # True when it is someone else's org


@dataclass(frozen=True)
class User:
    user_id: str
    email: str
    org_id: str
    project_id: str  # the default project, where the portal looks
    enabled: bool
    role: str = "user"
    allow_app: bool = True
    created_at: Optional[float] = None
    employee_id: Optional[str] = None
    enrolled_by: Optional[str] = None
    grants: tuple[str, ...] = ()
    shares: tuple[Share, ...] = ()  # projects of other people's orgs, by alias

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def allows(self, project: str) -> bool:
        """May this person use `project` inside their own org?"""
        return bool(project) and (ANY_PROJECT in self.grants or project in self.grants)

    def resolve(self, name: str) -> Optional[Namespace]:
        """The namespace behind a project name this person sends: their own
        org when the name is granted there, else a project shared with them
        under that alias, else nothing."""
        if self.allows(name):
            return Namespace(self.org_id, name, self.user_id)
        for sh in self.shares:
            if sh.alias == name:
                return Namespace(sh.owner_org, sh.project, sh.owner_id, shared=True)
        return None

    def share_named(self, alias: str) -> Optional[Share]:
        return next((sh for sh in self.shares if sh.alias == alias), None)

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
        # People enrolled before grants existed keep the one project they had.
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO grants (user_id, project) SELECT user_id, project_id "
                "FROM users WHERE user_id NOT IN (SELECT user_id FROM grants)")

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

    def revoked(self, key_hash_value: str) -> Optional[str]:
        """Why this key was unlinked, if reconcile unlinked it for a reassignment."""
        row = self._db.execute("SELECT reason FROM revoked WHERE key_hash = ?",
                               (key_hash_value,)).fetchone()
        return row["reason"] if row else None

    def revoked_keys(self) -> list[str]:
        return [r["key_hash"] for r in self._db.execute("SELECT key_hash FROM revoked")]

    def forget_revoked(self, key_hash_value: str) -> None:
        """The key is gone from LiteLLM, or an app key is its service user's again."""
        with self._db:
            self._db.execute("DELETE FROM revoked WHERE key_hash = ?", (key_hash_value,))

    def suspend_key(self, key_hash_value: str, reason: str) -> None:
        """Keep the key's registration but refuse it (`key-reassigned`) until lifted."""
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO revoked (key_hash, reason, at) VALUES (?, ?, ?)",
                             (key_hash_value, reason, time.time()))

    def keys(self) -> list[Key]:
        return [_key(r) for r in self._db.execute("SELECT * FROM keys ORDER BY label")]

    def app(self, key_hash_value: str) -> Optional[App]:
        row = self._db.execute(
            "SELECT * FROM apps WHERE key_hash = ?", (key_hash_value,)
        ).fetchone()
        return App(**dict(row)) if row else None

    def user(self, user_id: str) -> Optional[User]:
        row = self._db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return self._user(row)

    def user_by_email(self, email: str) -> Optional[User]:
        # Emails are matched without regard to case here, unlike the hook's own
        # identity path: the platform owns the mapping, so it can normalise once
        # without moving anyone's memory.
        row = self._db.execute(
            "SELECT * FROM users WHERE lower(email) = lower(?)", (email.strip(),)
        ).fetchone()
        return self._user(row)

    def user_by_person(self, person: str) -> Optional[User]:
        """By employee ID or email, whichever `person` is; case does not matter."""
        person = person.strip()
        if not person:
            return None
        row = self._db.execute(
            "SELECT * FROM users WHERE lower(employee_id) = lower(?) OR lower(email) = lower(?)",
            (person, person)).fetchone()
        return self._user(row)

    def _user(self, row) -> Optional[User]:
        if row is None:
            return None
        grants = tuple(r["project"] for r in self._db.execute(
            "SELECT project FROM grants WHERE user_id = ? ORDER BY project", (row["user_id"],)))
        shares = tuple(Share(owner_id=r["owner_id"], owner_org=r["org_id"], owner_email=r["email"],
                             owner_person=r["employee_id"] or r["email"], project=r["project"],
                             alias=r["alias"], seen=bool(r["seen"]), at=r["at"], shared_by=r["shared_by"])
                       for r in self._db.execute(
            "SELECT s.*, o.org_id, o.email, o.employee_id FROM shares s "
            "JOIN users o ON o.user_id = s.owner_id WHERE s.member_id = ? ORDER BY s.alias",
            (row["user_id"],)))
        return _user(row, grants, shares)

    def personal_keys(self, user_id: str) -> list[Key]:
        rows = self._db.execute(
            "SELECT * FROM keys WHERE kind = 'personal' AND user_id = ? ORDER BY label", (user_id,)
        ).fetchall()
        return [_key(r) for r in rows]

    def users(self) -> list[User]:
        return [self._user(r) for r in self._db.execute("SELECT * FROM users ORDER BY email")]

    def app_keys(self) -> list[dict]:
        return [dict(r) for r in self._db.execute(
            "SELECT k.label, a.issuer, a.owner, substr(k.key_hash, 1, 12) AS key_hash, "
            "r.reason AS suspended FROM keys k JOIN apps a ON a.key_hash = k.key_hash "
            "LEFT JOIN revoked r ON r.key_hash = k.key_hash ORDER BY k.label")]

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
                 employee_id: Optional[str] = None, enrolled_by: Optional[str] = None) -> User:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        email = email.strip()
        user_id = slug(email)
        employee_id = (employee_id or "").strip() or None
        with self._db:
            self._db.execute(
                "INSERT INTO users (user_id, email, employee_id, org_id, project_id, role, "
                "created_at, enrolled_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, email, employee_id, slug(employee_id or email), project_id, role,
                 time.time(), enrolled_by or None),
            )
            self._db.execute("INSERT INTO grants (user_id, project) VALUES (?, ?)",
                             (user_id, project_id))
        return self.user(user_id)

    def grant(self, email: str, project: str) -> User:
        """Let the person use `project` in their org; `*` means any project."""
        user = self._require(email)
        project = project.strip()
        if project != ANY_PROJECT and (not project or _ORG_ID_ALLOWED.search(project)):
            raise ValueError("a project name uses letters, digits, _ - and : (or * for any)")
        with self._db:
            self._db.execute("INSERT OR IGNORE INTO grants (user_id, project) VALUES (?, ?)",
                             (user.user_id, project))
        return self.user(user.user_id)

    def ungrant(self, email: str, project: str) -> User:
        """Take a project away; nobody it was shared with keeps it either."""
        user = self._require(email)
        with self._db:
            self._db.execute("DELETE FROM grants WHERE user_id = ? AND project = ?",
                             (user.user_id, project.strip()))
            self._db.execute("DELETE FROM shares WHERE owner_id = ? AND project = ?",
                             (user.user_id, project.strip()))
        return self.user(user.user_id)

    # ---- sharing: a project of one org, used by another person under an alias ----

    def share(self, owner_email: str, project: str, member_email: str,
              by: Optional[str] = None) -> Share:
        """Let `member` use `project` of `owner`'s org. The member sees it under
        the project's own name, or `<project>-by-<owner>` if that name is taken."""
        owner = self._require(owner_email)
        member = self._require(member_email)
        project = project.strip()
        if project == ANY_PROJECT or not project or _ORG_ID_ALLOWED.search(project):
            raise ValueError("name one project")
        if project == owner.project_id:
            raise ValueError("the default project is personal and cannot be shared")
        if not owner.allows(project):
            raise ValueError(f"{owner.email} does not hold project {project!r}")
        if member.user_id == owner.user_id:
            raise ValueError("you already own it")
        existing = next((sh for sh in member.shares
                         if sh.owner_id == owner.user_id and sh.project == project), None)
        if existing is not None:
            return existing
        alias = project
        if member.resolve(alias) is not None:
            alias = f"{project}-by-{slug(owner.person_id)}"
            if member.resolve(alias) is not None:
                raise ValueError(f"{member.email} already has a project called {alias!r}")
        with self._db:
            self._db.execute(
                "INSERT INTO shares (owner_id, project, member_id, alias, shared_by, at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (owner.user_id, project, member.user_id, alias, by or owner.email, time.time()))
        return self.user(member.user_id).share_named(alias)

    def unshare(self, owner_email: str, project: str, member_email: str) -> bool:
        owner = self._require(owner_email)
        member = self._require(member_email)
        with self._db:
            cur = self._db.execute(
                "DELETE FROM shares WHERE owner_id = ? AND project = ? AND member_id = ?",
                (owner.user_id, project.strip(), member.user_id))
        return cur.rowcount == 1

    def leave(self, member_email: str, alias: str) -> Optional[Share]:
        member = self._require(member_email)
        sh = member.share_named(alias.strip())
        if sh is None:
            return None
        with self._db:
            self._db.execute("DELETE FROM shares WHERE member_id = ? AND alias = ?",
                             (member.user_id, sh.alias))
        return sh

    def rename_share(self, member_email: str, alias: str, new_alias: str) -> Share:
        member = self._require(member_email)
        sh = member.share_named(alias.strip())
        new_alias = new_alias.strip()
        if sh is None:
            raise KeyError(alias)
        if not new_alias or _ORG_ID_ALLOWED.search(new_alias):
            raise ValueError("a project name uses letters, digits, _ - and :")
        if new_alias != sh.alias and member.resolve(new_alias) is not None:
            raise ValueError(f"you already have a project called {new_alias!r}")
        with self._db:
            self._db.execute("UPDATE shares SET alias = ? WHERE member_id = ? AND alias = ?",
                             (new_alias, member.user_id, sh.alias))
        return self.user(member.user_id).share_named(new_alias)

    def mark_share_seen(self, member_id: str, alias: str) -> None:
        with self._db:
            self._db.execute("UPDATE shares SET seen = 1 WHERE member_id = ? AND alias = ?",
                             (member_id, alias))

    def members_of(self, owner_id: str, project: str) -> list[dict]:
        """Who a project of this org is shared with."""
        return [dict(r) for r in self._db.execute(
            "SELECT m.email, m.employee_id, s.alias, s.at, s.seen, s.shared_by FROM shares s "
            "JOIN users m ON m.user_id = s.member_id WHERE s.owner_id = ? AND s.project = ? "
            "ORDER BY m.email", (owner_id, project))]

    def shared_out(self, owner_id: str) -> dict[str, int]:
        """Project -> how many people it is shared with, for this org."""
        return {r["project"]: r["n"] for r in self._db.execute(
            "SELECT project, count(*) AS n FROM shares WHERE owner_id = ? GROUP BY project",
            (owner_id,))}

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
            self._db.execute("DELETE FROM grants WHERE user_id = ?", (user.user_id,))
            self._db.execute("DELETE FROM shares WHERE owner_id = ? OR member_id = ?",
                             (user.user_id, user.user_id))
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
        """Link a key to a person. An admin (or their Sync) linking a key that
        reconcile revoked accepts the reassignment, so the mark goes."""
        user = self._require(email)
        h = key_hash(raw_or_hash)
        with self._db:
            self._db.execute(
                "INSERT INTO keys (key_hash, kind, user_id, label) VALUES (?, 'personal', ?, ?)",
                (h, user.user_id, label),
            )
            self._db.execute("DELETE FROM revoked WHERE key_hash = ?", (h,))
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
            self._db.execute("DELETE FROM revoked WHERE key_hash = ?", (h,))
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

    def unlink_key(self, raw_or_hash: str, reason: Optional[str] = None) -> None:
        """Forget a key. With a `reason` naming an owner change, the key is also
        marked revoked so it is not linked again on its next call."""
        h = key_hash(raw_or_hash)
        with self._db:
            self._db.execute("DELETE FROM apps WHERE key_hash = ?", (h,))
            self._db.execute("DELETE FROM keys WHERE key_hash = ?", (h,))
            if reason and "owner" in reason:
                self._db.execute("INSERT OR REPLACE INTO revoked (key_hash, reason, at) VALUES (?, ?, ?)",
                                 (h, reason, time.time()))

    def dump(self) -> dict:
        q = lambda sql: [dict(r) for r in self._db.execute(sql)]  # noqa: E731
        return {
            "services": q("SELECT label FROM services ORDER BY label"),
            "users": [{"user_id": u.user_id, "email": u.email, "employee_id": u.employee_id,
                       "role": u.role, "org_id": u.org_id, "project_id": u.project_id,
                       "grants": list(u.grants), "enabled": u.enabled, "allow_app": u.allow_app,
                       "enrolled_by": u.enrolled_by,
                       "shared_with_me": [f"{sh.alias} = {sh.owner_org}/{sh.project}" for sh in u.shares]}
                      for u in self.users()],
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


def _user(row, grants: tuple[str, ...] = (), shares: tuple[Share, ...] = ()) -> Optional[User]:
    if row is None:
        return None
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    d["allow_app"] = bool(d.get("allow_app", 1))
    return User(**d, grants=grants, shares=shares)


def _key(row) -> Optional[Key]:
    if row is None:
        return None
    d = dict(row)
    d["enabled"] = bool(d.get("enabled", 1))
    return Key(**d)
