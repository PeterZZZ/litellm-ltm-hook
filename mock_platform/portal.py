# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""My Memory: the platform's console for people and for admins.

Sign-in uses the person's Open WebUI account. The portal sends the email and
password to Open WebUI's own sign-in endpoint and never stores either; a 200
from Open WebUI is the proof of identity, and its email is looked up in the
platform registry exactly as a chat request's token would be. What someone may
do here comes from the platform's own role, never from their Open WebUI role.

Everyone, about themselves:
    who they are and how that was established, their namespace, the ways in that
    can reach it (Open WebUI, each personal key) with a switch for each, their
    access history, and their memories.

Admins, about everyone:
    create an account by email (the platform finds that person's personal keys
    in LiteLLM itself, with a read-only LiteLLM key, so nobody can attach a key
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
import re
import secrets
import time
from pathlib import Path
from typing import Callable, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from .registry import Registry, User

COOKIE = "mm_portal"
SESSION_SECONDS = 8 * 60 * 60
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
    owui_url: str,
    client: Callable[[], httpx.AsyncClient],
    litellm_url: Optional[str] = None,
    litellm_key: Optional[str] = None,
) -> None:
    owui_url = owui_url.rstrip("/")
    litellm_url = (litellm_url or "").rstrip("/") or None
    sessions: dict[str, tuple[str, str, float]] = {}  # sid -> (email, name, expires)

    # ---------- session plumbing ----------

    def current(request: Request, *, write: bool = False) -> tuple[str, str]:
        if write and request.headers.get("x-mm-portal") != "1":
            raise HTTPException(403, "missing X-MM-Portal header")
        sid = request.cookies.get(COOKIE) or ""
        entry = sessions.get(sid)
        if not entry or entry[2] < time.time():
            sessions.pop(sid, None)
            raise HTTPException(401, "signed out")
        return entry[0], entry[1]

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

    async def episode_count(user: User) -> int:
        try:
            got = await core("/api/v2/projects/episode_count/get",
                             {"org_id": user.org_id, "project_id": user.project_id})
            return int(got.get("count", 0))
        except HTTPException:
            return 0

    async def all_episodes(user: User) -> list[dict]:
        ns = {"org_id": user.org_id, "project_id": user.project_id}
        out: list[dict] = []
        page = 0
        while len(out) < MAX_EPISODES:
            body = await core("/api/v2/memories/list", {**ns, "page_size": _PAGE, "page_num": page})
            batch = (body.get("content") or {}).get("episodic_memory") or []
            out.extend(batch)
            if len(batch) < _PAGE:
                break
            page += 1
        return out

    async def delete_ids(user: User, ids: list[str]) -> int:
        owned = {str(e.get("uid")) for e in await all_episodes(user)}
        ids = [i for i in dict.fromkeys(str(i) for i in ids) if i in owned]
        for start in range(0, len(ids), 100):
            await core("/api/v2/memories/episodic/delete", {
                "org_id": user.org_id, "project_id": user.project_id,
                "episodic_ids": ids[start:start + 100],
            })
        return len(ids)

    # ---------- LiteLLM, read-only ----------

    async def litellm_keys(email: str) -> Optional[list[dict]]:
        """Personal keys LiteLLM holds for the user(s) with this email.

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
                             if str(u.get("user_email") or "").lower() == email.lower()]
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
        found = await litellm_keys(user.email)
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
            "email": user.email, "role": user.role, "enabled": user.enabled,
            "allow_app": user.allow_app, "namespace": f"{user.org_id}/{user.project_id}",
            "keys": [{"label": k.label, "enabled": k.enabled, "hash": k.key_hash[:12]} for k in keys],
            "created_at": user.created_at, "last_seen": seen.get(user.email.lower()),
        }

    # ---------- pages ----------

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/portal/")

    @app.get("/portal/", include_in_schema=False)
    async def page():
        return FileResponse(_STATIC / "index.html", headers={"Cache-Control": "no-store"})

    # ---------- sign in / out ----------

    @app.post("/portal/api/login")
    async def login(request: Request):
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
        sid = secrets.token_urlsafe(32)
        sessions[sid] = (str(who.get("email") or email), str(who.get("name") or ""),
                         time.time() + SESSION_SECONDS)
        out = JSONResponse({"ok": True})
        out.set_cookie(COOKIE, sid, max_age=SESSION_SECONDS, httponly=True,
                       samesite="strict", path="/portal")
        return out

    @app.post("/portal/api/logout")
    async def logout(request: Request):
        sessions.pop(request.cookies.get(COOKIE) or "", None)
        out = JSONResponse({"ok": True})
        out.delete_cookie(COOKIE, path="/portal")
        return out

    # ---------- me: identity, namespace, ways in, history ----------

    @app.get("/portal/api/me")
    async def me(request: Request):
        email, name = current(request)
        user = registry.user_by_email(email)
        if user is None:
            admins = [u.email for u in registry.users() if u.is_admin]
            return {"email": email, "name": name, "enrolled": False, "admins": admins}
        return {
            "email": email, "name": name, "enrolled": True, "role": user.role,
            "signed_in_via": "Open WebUI password check",
            "namespaces": [{
                "name": user.project_id, "org_id": user.org_id, "project_id": user.project_id,
                "kind": "Personal", "enabled": user.enabled,
                "episodes": await episode_count(user),
            }],
            "access": access_paths(user),
        }

    @app.post("/portal/api/switch")
    async def switch(request: Request):
        user = enrolled(request, write=True)
        body = await body_of(request)
        registry.set_enabled(user.email, bool(body.get("enabled")))
        return {"enabled": bool(body.get("enabled"))}

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
        user = enrolled(request)
        return {"turns": _turns(await all_episodes(user))}

    @app.post("/portal/api/memories/delete")
    async def delete(request: Request):
        user = enrolled(request, write=True)
        body = await body_of(request)
        return {"deleted": await delete_ids(user, list(body.get("ids") or []))}

    @app.post("/portal/api/memories/clear")
    async def clear(request: Request):
        user = enrolled(request, write=True)
        ids = [str(e.get("uid")) for e in await all_episodes(user)]
        return {"deleted": await delete_ids(user, ids)}

    @app.post("/portal/api/memories/add")
    async def add(request: Request):
        user = enrolled(request, write=True)
        body = await body_of(request)
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "nothing to remember")
        if len(text) > 4000:
            raise HTTPException(400, "keep it under 4000 characters")
        await core("/api/v2/projects", {"org_id": user.org_id, "project_id": user.project_id,
                                        "description": "mock platform"})
        await core("/api/v2/memories", {
            "org_id": user.org_id, "project_id": user.project_id, "types": [],
            "messages": [{
                "content": text, "role": "user", "producer": user.user_id,
                "produced_for": "litellm",
                "metadata": {"user_id": user.user_id, "session_id": user.user_id,
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
        found = await core("/api/v2/memories/search", {
            "org_id": user.org_id, "project_id": user.project_id,
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
        data = {
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "email": user.email,
            "namespace": f"{user.org_id}/{user.project_id}",
            "episodes": [_episode(e) for e in await all_episodes(user)],
        }
        return Response(json.dumps(data, indent=2, ensure_ascii=False),
                        media_type="application/json",
                        headers={"Content-Disposition":
                                 f'attachment; filename="memory-{user.org_id}.json"'})

    # ---------- admin ----------

    @app.get("/portal/api/admin/users")
    async def admin_users(request: Request):
        admin(request)
        seen = registry.last_seen()
        return {"users": [account(u, seen) for u in registry.users()],
                "litellm_lookup": bool(litellm_url and litellm_key)}

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
        user = registry.add_user(email, role=role)
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

    @app.post("/portal/api/admin/users/sync")
    async def admin_sync(request: Request):
        actor = admin(request, write=True)
        body = await body_of(request)
        target = registry.user_by_email(str(body.get("email") or ""))
        if target is None:
            raise HTTPException(404, "no such account")
        synced = await sync_keys(target)
        registry.record(op="admin:sync-keys", decision="allow", status=200, email=target.email,
                        reason=f"by {actor.email}; linked {synced['linked'] or 'none'}; "
                               f"removed {synced['removed'] or 'none'}")
        return synced

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
            removed = await delete_ids(target, [str(e.get("uid")) for e in await all_episodes(target)])
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
                        "role": "proxy_admin_viewer (read-only)"},
            "identity": "Open WebUI password check (portal); signed HS256 token (chat)",
        }

    @app.get("/portal/api/admin/audit")
    async def admin_audit(request: Request):
        admin(request)
        return {"items": registry.audit(limit=300)}


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


def _turns(episodes: list[dict]) -> list[dict]:
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
