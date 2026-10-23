# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""My Memory: the platform's console for people and for admins.

Sign-in is Corp SSO (Keycloak), the same sign-in Open WebUI uses: the portal
sends people to Keycloak and checks the ID token that comes back (see sso.py).
Its email and employee ID are looked up in the platform registry exactly as a
chat request's person would be. Without SSO configured, the portal falls back
to checking an email and password against Open WebUI's own sign-in endpoint.
What someone may do here comes from the platform's own role, never from their
Open WebUI or Keycloak role.

Everyone, about themselves:
    who they are and how that was established, their namespace, the ways in that
    can reach it (Open WebUI, each personal key) with a switch for each, their
    access history, and their memories.

Admins, about everyone:
    create an account by email (the platform takes the employee ID from Corp SSO
    and finds that person's personal keys in LiteLLM itself, with a read-only LiteLLM key, so nobody can attach a key
    to the wrong owner), delete one, switch memory or roles, see what the
    platform trusts, and read the audit log of every decision.

Every route about memory acts on the signed-in person's namespace, taken from
the registry, never from the browser; deleting by id re-checks each id against
that namespace first. Sessions are opaque random ids kept in memory (a restart
signs everyone out), in an HttpOnly, SameSite=Strict cookie. State-changing
calls also require an X-MM-Portal header, which a cross-site form cannot send.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Callable, Optional

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from .registry import ANY_PROJECT, Namespace, Registry, User, _ORG_ID_ALLOWED as _BAD_PROJECT

COOKIE = "mm_portal"
# Bumped whenever the page and these routes change together. A tab still running
# an older page sees a different value and reloads itself instead of breaking.
UI_VERSION = "2026-10-22.3"
SESSION_SECONDS = 8 * 60 * 60
LOGIN_SECONDS = 10 * 60  # how long a sign-in may sit at Keycloak
STATE_COOKIE = "mm_portal_login"
log = logging.getLogger("mock_platform.portal")
MAX_EPISODES = 5000
_PAGE = 100
_STATIC = Path(__file__).parent / "portal_static"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Open WebUI's background prompts (chat titles, tags, follow-up suggestions) go
# through the same model and are stored like any turn. They all open this way.
_TASK_PREFIX = re.compile(r"^\s*###\s*Task:", re.I)
_TASK_KINDS = (
    (re.compile(r"\btitle\b", re.I), "chat title"),
    (re.compile(r"\btags?\b", re.I), "chat tags"),
    (re.compile(r"follow-?up", re.I), "follow-up suggestions"),
)


def mount_portal(
    app: FastAPI,
    registry: Registry,
    core_url: str,
    owui_url: Optional[str],
    client: Callable[[], httpx.AsyncClient],
    litellm_url: Optional[str] = None,
    litellm_key: Optional[str] = None,
    sso=None,
    linker=None,
    enroller=None,
) -> None:
    owui_url = (owui_url or "").rstrip("/") or None
    litellm_url = (litellm_url or "").rstrip("/") or None
    # sid -> {"email", "name", "employee_id", "id_token", "via", "expires"}
    sessions: dict[str, dict] = {}
    # state -> (nonce, redirect_uri, expires): sign-ins sent to Keycloak, not back yet
    pending: dict[str, tuple[str, str, float]] = {}

    # ---------- session plumbing ----------

    def session(request: Request, *, write: bool = False) -> dict:
        if write and request.headers.get("x-mm-portal") != "1":
            raise HTTPException(403, "missing X-MM-Portal header")
        sid = request.cookies.get(COOKIE) or ""
        entry = sessions.get(sid)
        if not entry or entry["expires"] < time.time():
            sessions.pop(sid, None)
            raise HTTPException(401, "signed out")
        return entry

    def current(request: Request, *, write: bool = False) -> tuple[str, str]:
        entry = session(request, write=write)
        return entry["email"], entry["name"]

    def start_session(who: dict) -> str:
        sid = secrets.token_urlsafe(32)
        sessions[sid] = {**who, "expires": time.time() + SESSION_SECONDS}
        return sid

    def set_session_cookie(resp: Response, sid: str) -> None:
        resp.set_cookie(COOKIE, sid, max_age=SESSION_SECONDS, httponly=True,
                        samesite="strict", path="/portal")

    def enrolled(request: Request, *, write: bool = False) -> User:
        email, _ = current(request, write=write)
        user = registry.user_by_email(email)
        if user is None:
            raise HTTPException(409, "no memory account for this person")
        return user

    def admin(request: Request, *, write: bool = False) -> User:
        user = enrolled(request, write=write)
        if not user.is_admin:
            raise HTTPException(403, "platform admins only")
        return user

    async def body_of(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "body is not JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be a JSON object")
        return body

    # ---------- MemMachine core ----------

    async def core(path: str, body: dict) -> dict:
        async with client() as c:
            resp = await c.post(f"{core_url}{path}", json=body)
        if resp.status_code >= 400:
            raise HTTPException(502, f"memory service error ({resp.status_code})")
        return resp.json() if resp.content else {}

    def ns_of(user: User, requested) -> Namespace:
        """Where a memory route works: the namespace behind the project name
        the person sent (`project` in the body or query; empty = their default).
        A name that is neither granted in their org nor shared with them is
        refused, so a route can never reach a namespace the person does not hold.
        """
        name = str(requested or "").strip() or user.project_id
        ns = user.resolve(name) if (name == user.project_id or not _BAD_PROJECT.search(name)) else None
        if ns is None:
            raise HTTPException(403, f"project {name!r} is not one of yours")
        return ns

    def projects_of(user: User) -> list[str]:
        """Every project name the person holds: their own, default first, then
        the aliases of projects shared with them. `*` cannot be listed: core
        is only asked about projects named here."""
        own = [user.project_id] + sorted(
            g for g in user.grants if g not in (user.project_id, ANY_PROJECT))
        return own + sorted(sh.alias for sh in user.shares)

    def mine(user: User, ep: dict) -> bool:
        """Did this person write (or receive) this stored message?"""
        return user.org_id in (ep.get("producer_id"), ep.get("produced_for_id"),
                               (ep.get("metadata") or {}).get("user_id"))

    async def episode_count(ns: Namespace) -> int:
        try:
            got = await core("/api/v2/projects/episode_count/get",
                             {"org_id": ns.org_id, "project_id": ns.project_id})
            return int(got.get("count", 0))
        except HTTPException:
            return 0

    async def all_episodes(ns: Namespace) -> list[dict]:
        out: list[dict] = []
        page = 0
        while len(out) < MAX_EPISODES:
            body = await core("/api/v2/memories/list", {"org_id": ns.org_id, "project_id": ns.project_id,
                                                        "page_size": _PAGE, "page_num": page})
            batch = (body.get("content") or {}).get("episodic_memory") or []
            out.extend(batch)
            if len(batch) < _PAGE:
                break
            page += 1
        return out

    async def delete_ids(user: User, ids: list[str], ns: Namespace) -> int:
        """Delete these ids from the namespace. In a project shared with the
        person, only the messages they wrote themselves; the owner deletes any."""
        episodes = await all_episodes(ns)
        allowed = {str(e.get("uid")) for e in episodes if not ns.shared or mine(user, e)}
        ids = [i for i in dict.fromkeys(str(i) for i in ids) if i in allowed]
        for start in range(0, len(ids), 100):
            await core("/api/v2/memories/episodic/delete", {
                "org_id": ns.org_id, "project_id": ns.project_id,
                "episodic_ids": ids[start:start + 100],
            })
        return len(ids)

    # ---------- LiteLLM, read-only ----------

    async def litellm_keys(email: str, employee_id: Optional[str] = None) -> Optional[list[dict]]:
        """Personal keys LiteLLM holds for this person: users with this email, or
        whose LiteLLM user_id is their employee ID.

        None when the platform has no LiteLLM key configured. Team keys and
        blocked keys are left out: they do not name one person.
        """
        if not (litellm_url and litellm_key):
            return None
        auth = {"Authorization": f"Bearer {litellm_key}"}
        found: list[dict] = []
        async with client() as c:
            user_ids, page = [], 1
            while True:
                resp = await c.get(f"{litellm_url}/user/list",
                                   params={"page": page, "page_size": 100}, headers=auth)
                if resp.status_code != 200:
                    raise HTTPException(502, f"LiteLLM user lookup failed ({resp.status_code})")
                data = resp.json()
                user_ids += [u["user_id"] for u in data.get("users") or []
                             if str(u.get("user_email") or "").lower() == email.lower()
                             or (employee_id and str(u.get("user_id") or "") == employee_id)]
                if page >= int(data.get("total_pages") or 1):
                    break
                page += 1
            for uid in user_ids:
                resp = await c.get(f"{litellm_url}/user/info", params={"user_id": uid}, headers=auth)
                if resp.status_code != 200:
                    raise HTTPException(502, f"LiteLLM key lookup failed ({resp.status_code})")
                for k in resp.json().get("keys") or []:
                    if k.get("team_id") or k.get("blocked") or not k.get("token"):
                        continue
                    found.append({"key_hash": k["token"],
                                  "label": k.get("key_alias") or f"{uid}-{k['token'][:6]}",
                                  "litellm_user": uid})
        return found

    async def sync_keys(user: User) -> dict:
        """Link every personal key LiteLLM holds for this person; unlink gone ones."""
        found = await litellm_keys(user.email, user.employee_id)
        if found is None:
            return {"linked": [], "removed": [], "lookup": "not configured"}
        have = {k.key_hash: k for k in registry.personal_keys(user.user_id)}
        linked, removed = [], []
        for k in found:
            if k["key_hash"] in have:
                continue
            existing = registry.key(k["key_hash"])
            if existing is not None:  # already someone else's, or an app key
                continue
            registry.link_personal_key(user.email, k["key_hash"], k["label"])
            linked.append(k["label"])
        live = {k["key_hash"] for k in found}
        for h, k in have.items():
            if h not in live:
                registry.unlink_key(h)
                removed.append(k.label)
        return {"linked": linked, "removed": removed, "lookup": "ok"}

    async def directory(email: str):
        try:
            return await sso.person(email)
        except httpx.HTTPError:
            raise HTTPException(502, "Corp SSO directory is not reachable") from None

    # ---------- shapes for the page ----------

    def access_paths(user: User) -> list[dict]:
        paths = [{"kind": "app", "id": "openwebui", "label": "Open WebUI",
                  "detail": f"shared app key {label} + your signed token",
                  "enabled": user.allow_app} for label in registry.app_labels()[:1]]
        paths += [{"kind": "key", "id": k.key_hash[:16], "label": k.label,
                   "detail": f"personal LiteLLM key · hash {k.key_hash[:12]}…",
                   "enabled": k.enabled} for k in registry.personal_keys(user.user_id)]
        return paths

    def account(user: User, seen: dict[str, float]) -> dict:
        keys = registry.personal_keys(user.user_id)
        return {
            "email": user.email, "employee_id": user.employee_id, "role": user.role,
            "enabled": user.enabled,
            "allow_app": user.allow_app, "namespace": f"{user.org_id}/{user.project_id}",
            "org_id": user.org_id, "grants": list(user.grants),
            "enrolled_by": user.enrolled_by,
            "shared_out": registry.shared_out(user.user_id),
            "shared_in": [f"{sh.alias} = {sh.owner_org}/{sh.project}" for sh in user.shares],
            "keys": [{"label": k.label, "enabled": k.enabled, "hash": k.key_hash[:12]} for k in keys],
            "created_at": user.created_at, "last_seen": seen.get(user.email.lower()),
        }

    async def namespaces_of(user: User) -> list[dict]:
        shared_out = registry.shared_out(user.user_id)
        rows = []
        for project in [user.project_id] + sorted(
                g for g in user.grants if g not in (user.project_id, ANY_PROJECT)):
            rows.append({"name": project, "org_id": user.org_id, "project_id": project,
                         "kind": "Personal", "enabled": user.enabled, "shared": False,
                         "members": shared_out.get(project, 0),
                         "episodes": await episode_count(Namespace(user.org_id, project, user.user_id))})
        for sh in user.shares:
            rows.append({"name": sh.alias, "org_id": sh.owner_org, "project_id": sh.project,
                         "kind": "Shared", "enabled": user.enabled, "shared": True, "new": not sh.seen,
                         "owner": {"email": sh.owner_email, "person_id": sh.owner_person},
                         "shared_by": sh.shared_by, "at": sh.at,
                         "episodes": await episode_count(Namespace(sh.owner_org, sh.project, sh.owner_id, True))})
        return rows

    # ---------- pages ----------

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/portal/")

    @app.get("/portal/", include_in_schema=False)
    async def page():
        return FileResponse(_STATIC / "index.html", headers={"Cache-Control": "no-store"})

    # ---------- sign in / out ----------

    @app.get("/portal/api/config")
    async def config():
        return {"sso": sso is not None, "provider": "Corp SSO" if sso else None,
                "password": sso is None and owui_url is not None, "ui": UI_VERSION}

    def redirect_uri(request: Request) -> str:
        # Back to whichever host the browser used (localhost or 127.0.0.1);
        # Keycloak only accepts the ones registered for this client.
        return str(request.base_url).rstrip("/") + "/portal/auth/callback"

    @app.get("/portal/auth/login", include_in_schema=False)
    async def sso_login(request: Request):
        if sso is None:
            raise HTTPException(404, "single sign-on is not configured")
        now = time.time()
        for st in [k for k, v in pending.items() if v[2] < now]:
            pending.pop(st, None)
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        back = redirect_uri(request)
        pending[state] = (nonce, back, now + LOGIN_SECONDS)
        out = RedirectResponse(await sso.login_url(back, state, nonce), status_code=302)
        # Ties the returning browser to the one that started the sign-in. Lax,
        # because the return is a top-level navigation from Keycloak.
        out.set_cookie(STATE_COOKIE, state, max_age=LOGIN_SECONDS, httponly=True,
                       samesite="lax", path="/portal/auth")
        return out

    @app.get("/portal/auth/callback", include_in_schema=False)
    async def sso_callback(request: Request, state: str = "", code: str = "", error: str = ""):
        if sso is None:
            raise HTTPException(404, "single sign-on is not configured")
        entry = pending.pop(state, None)
        if error or not code or entry is None or entry[2] < time.time() \
                or request.cookies.get(STATE_COOKIE) != state:
            return _bounce("/portal/?signin=failed")
        nonce, back, _ = entry
        try:
            claims, id_token = await sso.finish_login(code, back, nonce)
        except (ValueError, httpx.HTTPError, jwt.PyJWTError) as exc:
            log.info("portal sign-in refused: %s", exc)
            return _bounce("/portal/?signin=failed")
        email = str(claims.get("email") or "")
        if not email or claims.get("email_verified") is False:
            return _bounce("/portal/?signin=failed")
        sid = start_session({
            "email": email, "name": str(claims.get("name") or ""),
            "employee_id": claims.get("employee_id"), "id_token": id_token, "via": "sso",
        })
        out = _bounce("/portal/")
        set_session_cookie(out, sid)
        out.delete_cookie(STATE_COOKIE, path="/portal/auth")
        return out

    @app.post("/portal/api/login")
    async def login(request: Request):
        if sso is not None or owui_url is None:
            raise HTTPException(404, "password sign-in is off; use single sign-on")
        if request.headers.get("x-mm-portal") != "1":
            raise HTTPException(403, "missing X-MM-Portal header")
        body = await body_of(request)
        email = str(body.get("email") or "").strip()
        password = str(body.get("password") or "")
        if not email or not password:
            raise HTTPException(400, "email and password are required")
        async with client() as c:
            try:
                resp = await c.post(f"{owui_url}/api/v1/auths/signin",
                                    json={"email": email, "password": password})
            except httpx.HTTPError:
                raise HTTPException(503, "Open WebUI is not reachable") from None
        if resp.status_code != 200:
            await asyncio.sleep(0.8)  # blunt a password-guessing loop a little
            raise HTTPException(401, "Email or password is incorrect")
        who = resp.json()
        sid = start_session({"email": str(who.get("email") or email),
                             "name": str(who.get("name") or ""), "employee_id": None,
                             "id_token": None, "via": "password"})
        out = JSONResponse({"ok": True})
        set_session_cookie(out, sid)
        return out

    @app.post("/portal/api/logout")
    async def logout(request: Request):
        entry = sessions.pop(request.cookies.get(COOKIE) or "", None) or {}
        logout_url = None
        if sso is not None and entry.get("id_token"):
            # End the Keycloak session too, or "sign in" would log straight back in.
            logout_url = await sso.logout_url(entry["id_token"],
                                              str(request.base_url).rstrip("/") + "/portal/")
        out = JSONResponse({"ok": True, "logout_url": logout_url})
        out.delete_cookie(COOKIE, path="/portal")
        return out

    # ---------- me: identity, namespace, ways in, history ----------

    @app.get("/portal/api/me")
    async def me(request: Request):
        entry = session(request)
        email, name = entry["email"], entry["name"]
        via = ("Corp SSO (Keycloak), ID token verified" if entry.get("via") == "sso"
               else "Open WebUI password check")
        user = registry.user_by_email(email)
        if user is None and entry.get("via") == "sso" and enroller is not None and enroller.configured:
            # Signed in through Corp SSO: that is membership, so enrol them here too.
            user = await enroller.by_email(email, "portal")
        if user is None:
            admins = [u.email for u in registry.users() if u.is_admin]
            return {"email": email, "name": name, "enrolled": False, "admins": admins,
                    "employee_id": entry.get("employee_id"), "signed_in_via": via,
                    "ui": UI_VERSION}
        return {
            "email": email, "name": name, "enrolled": True, "role": user.role, "ui": UI_VERSION,
            "employee_id": user.employee_id, "sso_employee_id": entry.get("employee_id"),
            "signed_in_via": via,
            # One row per project name the person holds: their own (default
            # first), then projects shared with them. `*` cannot be listed.
            "namespaces": await namespaces_of(user),
            "grants": list(user.grants),
            "access": access_paths(user),
        }

    @app.post("/portal/api/switch")
    async def switch(request: Request):
        user = enrolled(request, write=True)
        body = await body_of(request)
        registry.set_enabled(user.email, bool(body.get("enabled")))
        return {"enabled": bool(body.get("enabled"))}

    def change_grant(user: User, project: str, add: bool, *, by: User, allow_any: bool) -> User:
        """Add or remove a project in `user`'s org. The default project stays:
        the portal's memory views live there. `*` is an admin's to give."""
        project = project.strip()
        if not project:
            raise HTTPException(400, "name the project")
        if project == user.project_id:
            raise HTTPException(400, "the default project stays")
        if project == ANY_PROJECT and not allow_any:
            raise HTTPException(400, "only an admin can grant any project")
        try:
            user = registry.grant(user.email, project) if add else registry.ungrant(user.email, project)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        who = "self" if by.user_id == user.user_id else by.email
        registry.record(op="admin:grant" if by.is_admin and who != "self" else "project:" + ("add" if add else "remove"),
                        decision="allow", status=200, email=user.email,
                        reason=f"by {who}; project {project} {'granted' if add else 'removed'}",
                        namespace=f"{user.org_id}/{project}")
        return user

    @app.post("/portal/api/projects")
    async def project_add(request: Request):
        """A person gives themselves a project in their own org."""
        user = enrolled(request, write=True)
        body = await body_of(request)
        user = change_grant(user, str(body.get("project") or ""), True, by=user, allow_any=False)
        return {"grants": list(user.grants)}

    @app.post("/portal/api/projects/remove")
    async def project_remove(request: Request):
        """Take a project off one's own grants. Its memories stay in core."""
        user = enrolled(request, write=True)
        body = await body_of(request)
        user = change_grant(user, str(body.get("project") or ""), False, by=user, allow_any=False)
        return {"grants": list(user.grants)}

    # ---------- sharing: the owner shares a project of their org; members use it by alias ----------

    def own_shareable(user: User, project: str) -> str:
        project = str(project or "").strip()
        if not project or project == user.project_id:
            raise HTTPException(400, "the default project is personal; share another project")
        if not user.allows(project) or _BAD_PROJECT.search(project):
            raise HTTPException(403, f"project {project!r} is not one of yours to share")
        return project

    async def person_for_share(who: str) -> tuple[Optional[User], str]:
        """Resolve an email or employee ID to a person: enrolled already, or
        known to Corp SSO (enrolled now, Oct 22 policy), or nobody."""
        found = registry.user_by_person(who)
        if found is not None:
            return found, "shared"
        if enroller is not None and enroller.configured:
            found = await (enroller.by_email(who, "share") if "@" in who
                           else enroller.by_person(who, "share"))
            if found is not None:
                return found, "shared and enrolled"
        return None, "not found: not enrolled and not in Corp SSO"

    @app.post("/portal/api/projects/share")
    async def project_share(request: Request):
        """Share one of my projects with people named by email or employee ID."""
        owner = enrolled(request, write=True)
        body = await body_of(request)
        project = own_shareable(owner, body.get("project"))
        people = [w.strip() for w in re.split(r"[,\s;]+", str(body.get("people") or "")) if w.strip()]
        if not people:
            raise HTTPException(400, "name at least one person by email or employee ID")
        results = []
        for who in people[:50]:
            member, status = await person_for_share(who)
            if member is None:
                results.append({"who": who, "status": status})
                continue
            try:
                sh = registry.share(owner.email, project, member.email, by=owner.email)
            except ValueError as exc:
                results.append({"who": who, "status": str(exc)})
                continue
            results.append({"who": who, "status": status, "email": member.email, "alias": sh.alias})
            registry.record(op="share", decision="allow", status=200, email=member.email,
                            namespace=f"{owner.org_id}/{project}",
                            reason=f"{owner.email} shared {owner.org_id}/{project} with {member.email} "
                                   f"as {sh.alias}" + ("; enrolled for it" if "enrolled" in status else ""))
        return {"project": project, "results": results,
                "members": registry.members_of(owner.user_id, project)}

    @app.get("/portal/api/projects/members")
    async def project_members(request: Request):
        owner = enrolled(request)
        project = own_shareable(owner, request.query_params.get("project"))
        return {"project": project, "members": registry.members_of(owner.user_id, project)}

    @app.post("/portal/api/projects/unshare")
    async def project_unshare(request: Request):
        owner = enrolled(request, write=True)
        body = await body_of(request)
        project = own_shareable(owner, body.get("project"))
        member = registry.user_by_person(str(body.get("email") or ""))
        if member is None or not registry.unshare(owner.email, project, member.email):
            raise HTTPException(404, "not shared with that person")
        registry.record(op="unshare", decision="allow", status=200, email=member.email,
                        namespace=f"{owner.org_id}/{project}",
                        reason=f"{owner.email} stopped sharing {owner.org_id}/{project} with {member.email}")
        return {"project": project, "members": registry.members_of(owner.user_id, project)}

    @app.post("/portal/api/projects/leave")
    async def project_leave(request: Request):
        """Give up a project someone shared with me. Its memories stay theirs."""
        user = enrolled(request, write=True)
        body = await body_of(request)
        sh = registry.leave(user.email, str(body.get("project") or ""))
        if sh is None:
            raise HTTPException(404, "no such shared project")
        registry.record(op="leave", decision="allow", status=200, email=user.email,
                        namespace=f"{sh.owner_org}/{sh.project}",
                        reason=f"{user.email} left {sh.owner_org}/{sh.project} (alias {sh.alias})")
        return {"ok": True}

    @app.post("/portal/api/projects/rename")
    async def project_rename(request: Request):
        """Rename the alias I use for a shared project."""
        user = enrolled(request, write=True)
        body = await body_of(request)
        try:
            sh = registry.rename_share(user.email, str(body.get("project") or ""), str(body.get("to") or ""))
        except KeyError:
            raise HTTPException(404, "no such shared project") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"alias": sh.alias, "namespace": f"{sh.owner_org}/{sh.project}"}

    @app.post("/portal/api/projects/seen")
    async def project_seen(request: Request):
        user = enrolled(request, write=True)
        body = await body_of(request)
        registry.mark_share_seen(user.user_id, str(body.get("project") or ""))
        return {"ok": True}

    @app.post("/portal/api/access")
    async def access(request: Request):
        user = enrolled(request, write=True)
        body = await body_of(request)
        enabled = bool(body.get("enabled"))
        target = str(body.get("id") or "")
        if target == "openwebui":
            registry.set_allow_app(user.email, enabled)
            return {"ok": True}
        for k in registry.personal_keys(user.user_id):
            if k.key_hash.startswith(target) and len(target) >= 16:
                registry.set_key_enabled(user.user_id, k.key_hash, enabled)
                return {"ok": True}
        raise HTTPException(404, "no such way in on your account")

    @app.get("/portal/api/history")
    async def history(request: Request):
        user = enrolled(request)
        return {"items": registry.audit(limit=100, email=user.email)}

    # ---------- memories ----------

    @app.get("/portal/api/memories")
    async def memories(request: Request):
        """Turns from one project (`?project=`), or from every project the
        person holds (`all`, the default), each turn tagged with its project."""
        user = enrolled(request)
        wanted = str(request.query_params.get("project") or "all").strip()
        projects = projects_of(user) if wanted == "all" else [wanted]
        spaces = [ns_of(user, p) for p in projects]
        found = await asyncio.gather(*(all_episodes(ns) for ns in spaces))
        turns = []
        for name, ns, eps in zip(projects, spaces, found):
            for t in _turns(eps, project=name):
                t["shared"] = ns.shared
                t["mine"] = not ns.shared or any(mine(user, e) for e in eps if str(e.get("uid")) in t["ids"])
                turns.append(t)
            if ns.shared and wanted != "all":
                registry.mark_share_seen(user.user_id, name)
        turns.sort(key=lambda t: t["at"] or "", reverse=True)
        shared = {sh.alias: {"owner": sh.owner_person, "namespace": f"{sh.owner_org}/{sh.project}"}
                  for sh in user.shares}
        return {"projects": projects, "shared": shared, "turns": turns}

    @app.post("/portal/api/memories/delete")
    async def delete(request: Request):
        user = enrolled(request, write=True)
        body = await body_of(request)
        ns = ns_of(user, body.get("project"))
        return {"deleted": await delete_ids(user, list(body.get("ids") or []), ns)}

    @app.post("/portal/api/memories/clear")
    async def clear(request: Request):
        user = enrolled(request, write=True)
        body = await body_of(request)
        ns = ns_of(user, body.get("project"))
        ids = [str(e.get("uid")) for e in await all_episodes(ns)]
        return {"deleted": await delete_ids(user, ids, ns)}

    @app.post("/portal/api/memories/add")
    async def add(request: Request):
        user = enrolled(request, write=True)
        body = await body_of(request)
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "nothing to remember")
        if len(text) > 4000:
            raise HTTPException(400, "keep it under 4000 characters")
        ns = ns_of(user, body.get("project"))
        await core("/api/v2/projects", {"org_id": ns.org_id, "project_id": ns.project_id,
                                        "description": "mock platform"})
        await core("/api/v2/memories", {
            "org_id": ns.org_id, "project_id": ns.project_id, "types": [],
            "messages": [{
                # Provenance as the platform states it for chats: the person's org.
                "content": text, "role": "user", "producer": user.org_id,
                "produced_for": "litellm",
                "metadata": {"user_id": user.person_id, "session_id": user.org_id,
                             "via": "manual", "source": "my-memory"},
            }],
        })
        return {"ok": True}

    @app.post("/portal/api/recall")
    async def recall(request: Request):
        user = enrolled(request, write=True)
        body = await body_of(request)
        query = str(body.get("query") or "").strip()
        if not query:
            raise HTTPException(400, "type something to look up")
        ns = ns_of(user, body.get("project"))
        found = await core("/api/v2/memories/search", {
            "org_id": ns.org_id, "project_id": ns.project_id,
            "query": query, "top_k": 8, "types": ["episodic"],
        })
        episodic = (found.get("content") or {}).get("episodic_memory") or {}
        items = []
        for term in ("long_term_memory", "short_term_memory"):
            for ep in (episodic.get(term) or {}).get("episodes") or []:
                items.append({**_episode(ep), "score": ep.get("score")})
        return {"items": items}

    @app.get("/portal/api/export")
    async def export(request: Request):
        user = enrolled(request)
        ns = ns_of(user, request.query_params.get("project"))
        data = {
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "email": user.email,
            "namespace": f"{ns.org_id}/{ns.project_id}",
            "episodes": [_episode(e) for e in await all_episodes(ns)],
        }
        return Response(json.dumps(data, indent=2, ensure_ascii=False),
                        media_type="application/json",
                        headers={"Content-Disposition":
                                 f'attachment; filename="memory-{ns.org_id}-{ns.project_id}.json"'})

    # ---------- admin ----------

    @app.get("/portal/api/admin/users")
    async def admin_users(request: Request):
        admin(request)
        seen = registry.last_seen()
        users = registry.users()
        return {"users": [account(u, seen) for u in users],
                "litellm_lookup": bool(litellm_url and litellm_key),
                "auto_enrol": bool(enroller is not None and enroller.configured),
                "self_enrolled": sum(1 for u in users if u.enrolled_by)}

    @app.post("/portal/api/admin/users")
    async def admin_create(request: Request):
        actor = admin(request, write=True)
        body = await body_of(request)
        email = str(body.get("email") or "").strip()
        role = str(body.get("role") or "user")
        if not _EMAIL.match(email):
            raise HTTPException(400, "enter a valid email address")
        if role not in ("user", "admin"):
            raise HTTPException(400, "role must be user or admin")
        if registry.user_by_email(email):
            raise HTTPException(409, "that person already has an account")
        employee_id = None
        if sso is not None:
            person = await directory(email)
            if person is None:
                raise HTTPException(404, "Corp SSO has no person with that email")
            email, employee_id = person.email, person.employee_id
        user = registry.add_user(email, role=role, employee_id=employee_id)
        await core("/api/v2/projects", {"org_id": user.org_id, "project_id": user.project_id,
                                        "description": "mock platform"})
        synced = await sync_keys(user)
        registry.record(op="admin:create", decision="allow", status=200, email=user.email,
                        reason=f"by {actor.email}; role {role}; keys {synced['linked'] or 'none'}",
                        namespace=f"{user.org_id}/{user.project_id}")
        return {"account": account(registry.user(user.user_id), {}), "keys": synced}

    @app.post("/portal/api/admin/users/update")
    async def admin_update(request: Request):
        actor = admin(request, write=True)
        body = await body_of(request)
        email = str(body.get("email") or "")
        target = registry.user_by_email(email)
        if target is None:
            raise HTTPException(404, "no such account")
        changes = []
        if "role" in body:
            role = str(body["role"])
            if role not in ("user", "admin"):
                raise HTTPException(400, "role must be user or admin")
            if target.user_id == actor.user_id and role != "admin":
                raise HTTPException(400, "you cannot remove your own admin role")
            registry.set_role(email, role)
            changes.append(f"role {role}")
        if "enabled" in body:
            registry.set_enabled(email, bool(body["enabled"]))
            changes.append("memory " + ("on" if body["enabled"] else "off"))
        registry.record(op="admin:update", decision="allow", status=200, email=target.email,
                        reason=f"by {actor.email}; {', '.join(changes) or 'no change'}")
        return {"account": account(registry.user(target.user_id), registry.last_seen())}

    @app.post("/portal/api/admin/users/grant")
    async def admin_grant(request: Request):
        actor = admin(request, write=True)
        body = await body_of(request)
        target = registry.user_by_email(str(body.get("email") or ""))
        if target is None:
            raise HTTPException(404, "no such account")
        target = change_grant(target, str(body.get("project") or ""), not body.get("remove"),
                              by=actor, allow_any=True)
        return {"account": account(target, registry.last_seen())}

    @app.post("/portal/api/admin/users/sync")
    async def admin_sync(request: Request):
        actor = admin(request, write=True)
        body = await body_of(request)
        target = registry.user_by_email(str(body.get("email") or ""))
        if target is None:
            raise HTTPException(404, "no such account")
        employee = None
        if sso is not None:
            person = await directory(target.email)
            if person is not None and person.employee_id != target.employee_id:
                employee = {"from": target.employee_id, "to": person.employee_id}
                target = registry.set_employee_id(target.email, person.employee_id)
        synced = await sync_keys(target)
        registry.record(op="admin:sync", decision="allow", status=200, email=target.email,
                        reason=f"by {actor.email}; employee ID "
                               f"{(employee or {}).get('to') or 'unchanged'}; linked "
                               f"{synced['linked'] or 'none'}; removed {synced['removed'] or 'none'}",
                        namespace=f"{target.org_id}/{target.project_id}")
        return {**synced, "employee_id": employee}

    @app.post("/portal/api/admin/reconcile")
    async def admin_reconcile(request: Request):
        """What the background job does every few minutes, now."""
        actor = admin(request, write=True)
        if linker is None or not linker.configured:
            raise HTTPException(409, "LiteLLM lookup is not configured")
        try:
            result = await linker.reconcile()
        except (httpx.HTTPError, RuntimeError) as exc:
            raise HTTPException(502, f"LiteLLM not reachable: {exc}") from None
        registry.record(op="admin:reconcile", decision="allow", status=200, email=actor.email,
                        reason=f"by {actor.email}; checked {result['checked']}; linked "
                               f"{result['linked'] or 'none'}; unlinked {result['unlinked'] or 'none'}")
        return result

    @app.post("/portal/api/admin/users/delete")
    async def admin_delete(request: Request):
        actor = admin(request, write=True)
        body = await body_of(request)
        target = registry.user_by_email(str(body.get("email") or ""))
        if target is None:
            raise HTTPException(404, "no such account")
        if target.user_id == actor.user_id:
            raise HTTPException(400, "you cannot delete your own account")
        removed = 0
        if body.get("delete_memories"):
            # Every project of their own org; projects shared with them stay their owners'.
            for project in [target.project_id] + [g for g in target.grants if g not in (target.project_id, ANY_PROJECT)]:
                ns = Namespace(target.org_id, project, target.user_id)
                removed += await delete_ids(target, [str(e.get("uid")) for e in await all_episodes(ns)], ns)
        registry.delete_user(target.email)
        registry.record(op="admin:delete", decision="allow", status=200, email=target.email,
                        reason=f"by {actor.email}; memories deleted: {removed}")
        return {"deleted": target.email, "memories_deleted": removed}

    @app.get("/portal/api/admin/trust")
    async def admin_trust(request: Request):
        admin(request)
        return {
            "services": registry.services(),
            "apps": registry.app_keys(),
            "litellm": {"url": litellm_url, "configured": bool(litellm_url and litellm_key),
                        "role": "proxy_admin_viewer (read-only)",
                        "linking": "unknown personal keys are linked on their first call; "
                                   "reconcile re-reads every key every 10 minutes"},
            "identity": ("Corp SSO (portal sign-in, employee IDs); " if sso else
                         "Open WebUI password check (portal); ")
                        + "Open WebUI's signed HS256 token (chat)",
            "sso": ({"issuer": sso.issuer, "client": sso.client_id,
                     "directory": "service account, realm-management/view-users (read-only)",
                     "auto_enrol": bool(enroller is not None and enroller.configured)}
                    if sso else None),
        }

    @app.get("/portal/api/admin/audit")
    async def admin_audit(request: Request):
        admin(request)
        return {"items": registry.audit(limit=300)}


def _bounce(to: str) -> Response:
    """Land on `to` with a page of our own rather than a redirect.

    After Keycloak, the browser is in a navigation that started on another site;
    a SameSite=Strict session cookie would not be sent on a redirect within it.
    A same-origin page that moves on starts a fresh, same-site navigation.
    """
    page = (f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={to}">'
            f'<title>Signing in</title><p>Signing in&hellip; <a href="{to}">continue</a></p>')
    return Response(page, media_type="text/html", headers={"Cache-Control": "no-store"})


# ---------- shaping core's episodes for people ----------

def _episode(ep: dict) -> dict:
    meta = ep.get("metadata") or {}
    return {
        "id": str(ep.get("uid")),
        "role": ep.get("producer_role") or "user",
        "text": ep.get("content") or "",
        "at": ep.get("created_at"),
        "source": _source(meta),
    }


def _source(meta: dict) -> str:
    via = meta.get("via")
    if via == "app-token":
        return "Open WebUI"
    if via == "personal-key":
        return f"Key · {meta.get('source') or 'personal'}"
    if via == "manual":
        return "Added by you"
    return "Chat"


def _task_kind(text: str) -> Optional[str]:
    if not _TASK_PREFIX.match(text or ""):
        return None
    head = text[:200]
    for pattern, kind in _TASK_KINDS:
        if pattern.search(head):
            return kind
    return "background task"


def _turns(episodes: list[dict], project: Optional[str] = None) -> list[dict]:
    """Pair each stored user message with the reply stored beside it, newest first."""
    eps = sorted(episodes, key=lambda e: (e.get("created_at") or "", _num(e.get("uid"))))
    turns, i = [], 0
    while i < len(eps):
        cur = _episode(eps[i])
        nxt = _episode(eps[i + 1]) if i + 1 < len(eps) else None
        if (cur["role"] == "user" and nxt and nxt["role"] == "assistant"
                and _close(eps[i], eps[i + 1])):
            pair, i = (cur, nxt), i + 2
        else:
            pair, i = (cur, None) if cur["role"] == "user" else (None, cur), i + 1
        you, reply = pair
        first = you or reply
        turns.append({
            "id": first["id"],
            "ids": [e["id"] for e in pair if e],
            "you": you["text"] if you else None,
            "reply": reply["text"] if reply else None,
            "at": first["at"],
            "source": first["source"],
            "automatic": _task_kind(you["text"]) if you else None,
            "project": project,
        })
    turns.reverse()
    return turns


def _close(a: dict, b: dict) -> bool:
    return (a.get("created_at") or "")[:19] == (b.get("created_at") or "")[:19] or \
        abs(_num(a.get("uid")) - _num(b.get("uid"))) == 1


def _num(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0
