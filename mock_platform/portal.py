# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""My Memory: a page where a person sees and manages their own memory.

Sign-in uses the person's Open WebUI account. The portal sends the email and
password to Open WebUI's own sign-in endpoint and never stores either; a 200
from Open WebUI is the proof of identity, and its email is looked up in the
platform registry exactly as a chat request's token would be.

Every route below acts on the signed-in person's namespace only, which comes
from the registry and never from the browser. Deleting by id re-checks that each
id belongs to that namespace before core is asked.

Sessions are opaque random ids kept in memory (a restart signs everyone out),
in an HttpOnly, SameSite=Strict cookie. State-changing calls also require an
X-MM-Portal header, which a cross-site form cannot send.
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
) -> None:
    owui_url = owui_url.rstrip("/")
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
            raise HTTPException(409, "memory is not set up for this account")
        return user

    async def core(path: str, body: dict) -> dict:
        async with client() as c:
            resp = await c.post(f"{core_url}{path}", json=body)
        if resp.status_code >= 400:
            raise HTTPException(502, f"memory service error ({resp.status_code})")
        return resp.json() if resp.content else {}

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
        body = await request.json()
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

    # ---------- who am I, and my space ----------

    @app.get("/portal/api/me")
    async def me(request: Request):
        email, name = current(request)
        user = registry.user_by_email(email)
        if user is None:
            return {"email": email, "name": name, "enrolled": False}
        count = await core("/api/v2/projects/episode_count/get",
                           {"org_id": user.org_id, "project_id": user.project_id})
        access = [{"kind": "app", "label": "Open WebUI", "detail": label}
                  for label in registry.app_labels()]
        access += [{"kind": "key", "label": "Personal LiteLLM key", "detail": k.label}
                   for k in registry.personal_keys(user.user_id)]
        return {
            "email": email, "name": name, "enrolled": True, "enabled": user.enabled,
            "space": {"org_id": user.org_id, "project_id": user.project_id,
                      "kind": "Personal", "episodes": count.get("count", 0)},
            "access": access,
        }

    @app.post("/portal/api/enroll")
    async def enroll(request: Request):
        email, _ = current(request, write=True)
        if registry.user_by_email(email) is None:
            registry.add_user(email)
        return {"ok": True}

    @app.post("/portal/api/switch")
    async def switch(request: Request):
        user = enrolled(request, write=True)
        body = await request.json()
        registry.set_enabled(user.email, bool(body.get("enabled")))
        return {"enabled": bool(body.get("enabled"))}

    # ---------- memories ----------

    @app.get("/portal/api/memories")
    async def memories(request: Request):
        user = enrolled(request)
        return {"turns": _turns(await all_episodes(user))}

    @app.post("/portal/api/memories/delete")
    async def delete(request: Request):
        user = enrolled(request, write=True)
        body = await request.json()
        return {"deleted": await delete_ids(user, list(body.get("ids") or []))}

    @app.post("/portal/api/memories/clear")
    async def clear(request: Request):
        user = enrolled(request, write=True)
        ids = [str(e.get("uid")) for e in await all_episodes(user)]
        return {"deleted": await delete_ids(user, ids)}

    @app.post("/portal/api/memories/add")
    async def add(request: Request):
        user = enrolled(request, write=True)
        body = await request.json()
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
        body = await request.json()
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
