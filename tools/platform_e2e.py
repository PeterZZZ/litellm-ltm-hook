# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""End-to-end checks for platform mode: Corp SSO -> Open WebUI -> LiteLLM (hook) -> mock platform -> core.

Run against a live stack. Nothing here is collected by pytest.

    LITELLM_URL=http://127.0.0.1:4011 OWUI_URL=http://127.0.0.1:8087 \
    PLATFORM_URL=http://127.0.0.1:8100 PLATFORM_DB=/path/registry.sqlite \
    KEYS_FILE=/path/keys.env ACCOUNTS_FILE=/path/accounts.txt \
    OWUI_JWT_SECRET=... PLATFORM_SERVICE_CREDENTIAL=... LITELLM_MASTER_KEY=... \
    python3 tools/platform_e2e.py

KEYS_FILE holds ALICE_/BOB_/CAROL_VIRTUAL_KEY, OPENWEBUI_APP_KEY and
OPENWEBUI_APP_OWNER, the LiteLLM service user that owns the app key (KEY=value
lines); ACCOUNTS_FILE holds "email password" lines for the Corp SSO (Keycloak)
logins, used for both Open WebUI and the portal. OWUI_JWT_SECRET is used only to
mint tokens the way Open WebUI would, for the replay and expiry checks.
PLATFORM_SERVICE_CREDENTIAL is the credential the platform issued to this
LiteLLM (the hook's MEMMACHINE_API_KEY); the checks that need a header the hook
would never send (a wrong key owner, a project that disagrees with the body, a
path the hook does not call) speak to the platform directly as that LiteLLM.
LITELLM_MASTER_KEY creates, blocks, reassigns and deletes the throwaway keys the
key-linking checks need; everything it creates is named after the run and
deleted at the end. Expected: alice (E12345) and bob (E67890) enrolled; carol (E24680) has a
Corp SSO login and a LiteLLM key but no platform account, so her first chat is the
self-enrolment check (her account is deleted again at the end); the hook names Open
WebUI users by the token's name claim, which SSO sets to the employee ID.
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

import jwt

sys.path.insert(0, os.path.dirname(__file__))
from sso_browser import cookie, sso_login  # noqa: E402

LITELLM = os.environ["LITELLM_URL"].rstrip("/")
OWUI = os.environ["OWUI_URL"].rstrip("/")
PLATFORM = os.environ["PLATFORM_URL"].rstrip("/")
MODEL = os.environ.get("E2E_MODEL", "memmachine-glm-5.2")
SECRET = os.environ["OWUI_JWT_SECRET"]
SERVICE = os.environ["PLATFORM_SERVICE_CREDENTIAL"]
MASTER = os.environ["LITELLM_MASTER_KEY"]

keys = dict(line.strip().split("=", 1) for line in open(os.environ["KEYS_FILE"])
            if "=" in line and not line.startswith("#"))
accounts = dict(line.split() for line in open(os.environ["ACCOUNTS_FILE"]) if "@" in line)

# Unique per run, in both the fact and the question: the namespace is shared
# with earlier runs, and a generic question would recall their answers too.
RUN = uuid.uuid4().hex[:6]
FACT = f"PELICAN-{uuid.uuid4().hex[:6].upper()}"
QUESTION = (f"What is the codename of project {RUN}? "
            "If you do not know, answer exactly UNKNOWN.")
EMPLOYEE = {"alice@corp.example": "E12345", "bob@corp.example": "E67890",
            "carol@corp.example": "E24680"}
results = []


def post(url, body, token, headers=None):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:160]


def platform(path, body, key, *, method="POST", owner=None, user=None, token=None,
             project=None):
    """Call the platform as the hook would, as this LiteLLM, on `key`.

    Returns (status, X-MemMachine-Decision, body). What the hook sends is what
    the platform sees, so the headers are the Platform API's, built here.
    """
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {SERVICE}",
         "X-MemMachine-Key-Id": hashlib.sha256(key.encode()).hexdigest(),
         "X-MemMachine-Client": "e2e", "X-Request-Id": f"e2e-{RUN}"}
    for name, value in (("X-MemMachine-Key-Owner", owner), ("X-MemMachine-User", user),
                        ("X-MemMachine-User-Assertion", token), ("X-MemMachine-Project", project)):
        if value is not None:
            h[name] = value
    req = urllib.request.Request(f"{PLATFORM}{path}", method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.headers.get("X-MemMachine-Decision"), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("X-MemMachine-Decision"), e.read().decode()[:200]


def episodes(search_body_text):
    """Episode texts in a core search answer."""
    content = json.loads(search_body_text).get("content") or {}
    lt = (content.get("episodic_memory") or {}).get("long_term_memory") or {}
    return [e.get("content") or "" for e in lt.get("episodes") or []]


def chat(url, token, text, headers=None):
    status, body = post(url, {"model": MODEL, "stream": False, "max_tokens": 2000,
                              "messages": [{"role": "user", "content": text}]}, token, headers)
    answer = body["choices"][0]["message"]["content"] if status == 200 else str(body)
    return status, answer.strip().replace("\n", " ")


def owui_login(email):
    """Sign in to Open WebUI through Corp SSO, as a browser would; return its session token."""
    opener, _ = sso_login(f"{OWUI}/oauth/oidc/login", email, accounts[email])
    return cookie(opener, "token")


def owui_me(token):
    req = urllib.request.Request(f"{OWUI}/api/v1/auths/", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def mint(email, ttl=300, secret=SECRET):
    now = int(time.time())
    return jwt.encode({"sub": "e2e", "email": email, "name": EMPLOYEE.get(email, ""),
                       "iss": "open-webui", "iat": now, "exp": now + ttl},
                      secret, algorithm="HS256")


def check(name, ok, detail):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail[:150]}")


def portal_as(email):
    """Sign in to the portal through Corp SSO as `email`; return a caller for its API."""
    jar, _ = sso_login(f"{PLATFORM}/portal/auth/login", email, accounts[email])

    def portal(path, body=None):
        req = urllib.request.Request(
            f"{PLATFORM}{path}", data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "X-MM-Portal": "1"},
            method="POST" if body is not None else "GET")
        with jar.open(req, timeout=120) as r:
            return json.load(r)
    return portal


def litellm_admin(path, body):
    """LiteLLM's own admin API, with the master key: the keys the linking checks need."""
    status, out = post(f"{LITELLM}{path}", body, MASTER)
    if status != 200:
        raise RuntimeError(f"LiteLLM {path}: {status} {out}")
    return out


def clear_earlier_runs(email):
    """Delete this script's leftovers from earlier runs, through the portal.

    Every run stores its question verbatim, and identical questions from earlier
    runs outrank the new fact in recall; left alone, each run makes the next one
    less likely to pass.
    """
    portal = portal_as(email)
    ids = [i for t in portal("/portal/api/memories")["turns"]
           if "codename of project" in (t["you"] or "") + (t["reply"] or "") for i in t["ids"]]
    if ids:
        portal("/portal/api/memories/delete", {"ids": ids})
    print(f"(cleared {len(ids)} messages left by earlier runs from {email})")


OW = f"{OWUI}/api/chat/completions"
LL = f"{LITELLM}/v1/chat/completions"
clear_earlier_runs("alice@corp.example")
alice, bob, carol = (owui_login(f"{n}@corp.example") for n in ("alice", "bob", "carol"))
me = owui_me(alice)
check("alice signed in to Open WebUI through Corp SSO; name is her employee ID",
      me["email"] == "alice@corp.example" and me["name"] == "E12345", f"{me['email']} name={me['name']}")

s, a = chat(OW, alice, f"Remember this: the codename of project {RUN} is {FACT}.")
check("alice via Open WebUI states a fact", s == 200, a)
time.sleep(8)

s, a = chat(OW, alice, QUESTION)
check("alice via Open WebUI, new chat: recalled", s == 200 and FACT in a, a)

s, a = chat(OW, bob, QUESTION)
check("bob via Open WebUI: not recalled", s == 200 and FACT not in a, a)

s, a = chat(LL, keys["ALICE_VIRTUAL_KEY"], QUESTION)
check("alice's personal key, directly: same namespace, recalled", s == 200 and FACT in a, a)

# Carol has never been enrolled. Corp SSO knows her, so her first chat enrols her
# (Leo, Oct 22): memory on, her own empty namespace, nothing of alice's.
s, a = chat(OW, carol, QUESTION)
check("carol, never enrolled, via Open WebUI: chat works, her own (empty) memory", s == 200 and FACT not in a, a)

s, a = chat(LL, keys["CAROL_VIRTUAL_KEY"], QUESTION)
check("carol's personal key: served in her namespace, not alice's", s == 200 and FACT not in a, a)

s, a = chat(LL, keys["OPENWEBUI_APP_KEY"], QUESTION)
check("app key with no user token: chat works, no memory", s == 200 and FACT not in a, a)

s, a = chat(LL, keys["OPENWEBUI_APP_KEY"], QUESTION,
            {"x-openwebui-user-jwt": mint("alice@corp.example", secret="forged-" + "x" * 40)})
check("app key + forged token: chat works, no memory", s == 200 and FACT not in a, a)

s, a = chat(LL, keys["OPENWEBUI_APP_KEY"], QUESTION,
            {"x-openwebui-user-jwt": mint("alice@corp.example", ttl=-600)})
check("app key + expired token: chat works, no memory", s == 200 and FACT not in a, a)

s, a = chat(LL, keys["BOB_VIRTUAL_KEY"], QUESTION, {"x-openwebui-user-jwt": mint("alice@corp.example")})
check("alice's valid token replayed on bob's key: chat works, no memory", s == 200 and FACT not in a, a)

s, a = chat(LL, keys["ALICE_VIRTUAL_KEY"], QUESTION, {"x-memmachine-org": "E67890"})
check("alice's key + x-memmachine-org: E67890 (bob) -> header ignored, still alice", s == 200 and FACT in a, a)

s, a = post(f"{PLATFORM}/api/v2/memories/search", {"query": "pager"}, "mm-svc-guess")
check("platform called directly with a guessed service credential: 401", s == 401, str(a))

# Alice edits her Open WebUI display name to Bob's employee ID. Open WebUI signs
# whatever name she has, so the token is valid; the platform knows E67890 is not
# the person behind alice@corp.example and refuses. The hook never fails a chat
# over memory: it goes on without any.
post(f"{OWUI}/api/v1/auths/update/profile", {"name": "E67890", "profile_image_url": "/user.png"}, alice)
s, a = chat(OW, alice, QUESTION)
check("alice renamed to bob's employee ID: chat works, no memory", s == 200 and FACT not in a, f"{s} {a}")
alice = owui_login("alice@corp.example")  # signing in again restores the name from SSO
check("signing in again restores her name from Corp SSO", owui_me(alice)["name"] == "E12345",
      owui_me(alice)["name"])

# ---- the Platform API itself: what the hook never sends, and the paths it never calls ----

ALICE = dict(key=keys["ALICE_VIRTUAL_KEY"], owner="E12345", user="E12345")
NS = {"org_id": "E12345", "project_id": "default"}

s, d, body = platform("/api/v2/memories/list", {**NS, "page_size": 500, "page_num": 0},
                      project="default", **ALICE)
check("list on a granted project returns core's answer (the fact stored above is in it)",
      s == 200 and FACT in body, f"{s} {d} {len(body)} bytes")

s, d, body = platform("/api/v2/memories/list", {"org_id": "E12345", "project_id": "payroll"},
                      project="payroll", **ALICE)
check("list on an ungranted project: 403 project-not-granted",
      s == 403 and d == "project-not-granted" and FACT not in body, f"{s} {d} {body}")

s, d, body = platform("/api/v2/memories", None, method="DELETE", **ALICE)
check("DELETE /api/v2/memories: 403 endpoint-not-allowed", s == 403 and d == "endpoint-not-allowed",
      f"{s} {d} {body}")
s, d, body = platform("/api/v2/projects", None, method="GET", **ALICE)
check("GET /api/v2/projects: 403 endpoint-not-allowed, not 404", s == 403 and d == "endpoint-not-allowed",
      f"{s} {d} {body}")

SEARCH = {**NS, "query": QUESTION, "top_k": 5, "types": ["episodic"]}
s1, _, plain = platform("/api/v2/memories/search", SEARCH, project="default", **ALICE)
s2, _, filtered = platform("/api/v2/memories/search", {**SEARCH, "filter": "producer_id = 'nobody'"},
                           project="default", **ALICE)
check("a search body with `filter` reaches core intact: unfiltered finds the fact, filtered finds nothing",
      s1 == 200 and s2 == 200 and any(FACT in e for e in episodes(plain)) and episodes(filtered) == [],
      f"{s1}/{s2}: {len(episodes(plain))} episodes unfiltered, {len(episodes(filtered))} filtered")

# ---- the app key must still be the app's in LiteLLM ----

APP = dict(key=keys["OPENWEBUI_APP_KEY"], user="E12345", project="default")
APP_OWNER = keys.get("OPENWEBUI_APP_OWNER", "")
s, d, body = platform("/api/v2/memories/search", SEARCH, owner=APP_OWNER,
                      token=mint("alice@corp.example"), **APP)
check(f"app key owned by {APP_OWNER!r} in LiteLLM + alice's valid token: served",
      s == 200 and any(FACT in e for e in episodes(body)), f"{s} {d}")
s, d, body = platform("/api/v2/memories/search", SEARCH, owner="E12345",
                      token=mint("alice@corp.example"), **APP)
check("same key, LiteLLM now naming a person as its owner: 403 key-reassigned",
      s == 403 and d == "key-reassigned", f"{s} {d} {body}")
s, d, body = platform("/api/v2/memories/search", SEARCH, owner=APP_OWNER, **APP)
check("app key with the right owner but no token: still 401", s == 401, f"{s} {d} {body}")

if os.environ.get("PLATFORM_DB"):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from mock_platform.registry import Registry

    reg = Registry(os.environ["PLATFORM_DB"])

    # ---- self-enrolment: what carol's first chat did ----
    carol_acct = reg.user_by_email("carol@corp.example")
    enrol_rows = [r for r in reg.audit(300) if r["op"] == "enrol" and r["email"] == "carol@corp.example"]
    check("carol was enrolled on her first chat: employee ID from Corp SSO, default project, audit says enrol",
          carol_acct is not None and carol_acct.employee_id == "E24680" and carol_acct.grants == ("default",)
          and carol_acct.enrolled_by == "openwebui" and any(r["decision"] == "allow" for r in enrol_rows),
          f"account={carol_acct and (carol_acct.employee_id, carol_acct.grants, carol_acct.enrolled_by)} enrol rows={len(enrol_rows)}")
    check("carol's personal key linked itself once she existed",
          reg.key(hashlib.sha256(keys["CAROL_VIRTUAL_KEY"].encode()).hexdigest()) is not None, "")
    ghost = f"ghost-{RUN}@corp.example"
    s, d, body = platform("/api/v2/memories/search", {**SEARCH, "org_id": f"ghost_{RUN}_corp_example"},
                          owner=APP_OWNER, token=mint(ghost), user=ghost, key=keys["OPENWEBUI_APP_KEY"], project="default")
    check("a signed token for someone Corp SSO does not know: not-enrolled, nobody created",
          s == 403 and d == "not-enrolled" and reg.user_by_email(ghost) is None, f"{s} {d} {body}")

    # ---- grants: one org per person, one or more projects inside it ----
    PROJECT = f"research-{RUN}"
    reg.grant("alice@corp.example", PROJECT)
    try:
        s, a = chat(LL, keys["ALICE_VIRTUAL_KEY"], QUESTION, {"x-memmachine-project": PROJECT})
        check(f"alice's key on granted project {PROJECT}: served, in its own namespace (fact not there)",
              s == 200 and FACT not in a, a)
        s, d, body = platform("/api/v2/memories/list", {"org_id": "E12345", "project_id": PROJECT},
                              project=PROJECT, **ALICE)
        check("list on the granted project: core answers for E12345/" + PROJECT, s == 200, f"{s} {d}")
    finally:
        reg.ungrant("alice@corp.example", PROJECT)
    s, d, body = platform("/api/v2/memories/search", {**SEARCH, "project_id": PROJECT},
                          project=PROJECT, **ALICE)
    check("same project after ungrant: 403 project-not-granted",
          s == 403 and d == "project-not-granted", f"{s} {d} {body}")
    s, a = chat(LL, keys["ALICE_VIRTUAL_KEY"], QUESTION, {"x-memmachine-project": "payroll"})
    # The hook never fails a chat over memory: the platform refuses, the chat goes on without any.
    check("alice's key via LiteLLM on ungranted project payroll: chat works, no memory (project-not-granted)",
          s == 200 and FACT not in a
          and any(r["decision"] == "project-not-granted" for r in reg.audit(20)), f"{s} {a}")
    reg.grant("bob@corp.example", "*")
    try:
        s, d, body = platform("/api/v2/memories/list", {"org_id": "E67890", "project_id": f"any-{RUN}"},
                              project=f"any-{RUN}", key=keys["BOB_VIRTUAL_KEY"], owner="E67890")
        check("bob granted *: an arbitrary project name is allowed", s == 200, f"{s} {d}")
    finally:
        reg.ungrant("bob@corp.example", "*")
    s, d, body = platform("/api/v2/memories/search", SEARCH, project="payroll", **ALICE)
    check("X-MemMachine-Project disagreeing with the body's project_id: 400", s == 400, f"{s} {d} {body}")
    s, d, body = platform("/api/v2/memories/search", SEARCH, **ALICE)
    check("no X-MemMachine-Project at all: 400", s == 400, f"{s} {d} {body}")
    newcomer = reg.add_user(f"e2e-{RUN}@corp.example", employee_id=f"E9{RUN[:5].upper()}")
    try:
        check("a newly created person has only `default`", newcomer.grants == ("default",), str(newcomer.grants))
    finally:
        reg.delete_user(newcomer.email)

    # ---- keys the platform has never seen: looked up in LiteLLM, linked on the first call ----
    sha = lambda k: hashlib.sha256(k.encode()).hexdigest()  # noqa: E731
    made_keys, made_users, made_teams = [], [], []

    def new_key(**spec):
        k = litellm_admin("/key/generate", {"key_alias": f"e2e-{RUN}-{len(made_keys)}", **spec})["key"]
        made_keys.append(k)
        return k

    admin_portal = portal_as("admin@corp.example")
    try:
        rotated = new_key(user_id="E12345", key_alias=f"alice-rotated-{RUN}")
        s, a = chat(LL, rotated, QUESTION)
        linked = reg.key(sha(rotated))
        check("alice's brand-new key (owner E12345 in LiteLLM): linked and served on its first call, no Sync",
              s == 200 and FACT in a and linked is not None and linked.user_id == "alice_corp_example",
              f"{s} linked={linked.label if linked else None} {a}")

        stranger_id = f"E8{RUN[:5].upper()}"
        litellm_admin("/user/new", {"user_id": stranger_id, "user_email": f"stranger-{RUN}@corp.example",
                                    "user_role": "internal_user", "auto_create_key": False})
        made_users.append(stranger_id)
        stranger = new_key(user_id=stranger_id)
        s, a = chat(LL, stranger, QUESTION)
        check(f"a key whose LiteLLM owner {stranger_id} nobody enrolled: chat works, no memory, not linked",
              s == 200 and FACT not in a and reg.key(sha(stranger)) is None, a)

        blocked = new_key(user_id="E12345")
        litellm_admin("/key/block", {"key": blocked})
        s, d, body = platform("/api/v2/memories/search", SEARCH, project="default", key=blocked, owner="E12345")
        check("a blocked key of alice's: not linked, not-enrolled",
              s == 403 and d == "not-enrolled" and reg.key(sha(blocked)) is None, f"{s} {d} {body}")

        team_id = litellm_admin("/team/new", {"team_alias": f"e2e-{RUN}"})["team_id"]
        made_teams.append(team_id)
        team_key = new_key(user_id="E12345", team_id=team_id)
        s, d, body = platform("/api/v2/memories/search", SEARCH, project="default", key=team_key, owner="E12345")
        check("a team key, even with alice as its user: not linked, not-enrolled",
              s == 403 and d == "not-enrolled" and reg.key(sha(team_key)) is None, f"{s} {d} {body}")

        made_up = sha(f"never-issued-{RUN}")
        for _ in range(2):
            s, d, body = platform("/api/v2/memories/search", SEARCH, project="default",
                                  key=f"never-issued-{RUN}", owner="E12345")
        lookups = [r for r in reg.audit(200) if r["op"] == "link" and made_up[:12] in (r["reason"] or "")]
        check("a key LiteLLM does not know, asked twice within 60 s: looked up once (miss cached)",
              s == 403 and d == "not-enrolled" and len(lookups) == 1, f"{s} {d} lookups={len(lookups)}")

        litellm_admin("/key/update", {"key": rotated, "user_id": "E67890"})
        s, a = chat(LL, rotated, QUESTION)
        check("alice's linked key reassigned to bob in LiteLLM: chat works, no memory (key-reassigned)",
              s == 200 and FACT not in a
              and any(r["decision"] == "key-reassigned" for r in reg.audit(20)), f"{s} {a}")
        r = admin_portal("/portal/api/admin/reconcile", {})
        s, d, body = platform("/api/v2/memories/search", {**SEARCH, "org_id": "E67890"}, project="default",
                              key=rotated, owner="E67890")
        check("reconcile unlinks the reassigned key; it is not linked to bob on its next call: 403 key-reassigned",
              reg.key(sha(rotated)) is None and s == 403 and d == "key-reassigned",
              f"reconcile unlinked={r['unlinked']}; then {s} {d} {body}")

        gone = new_key(user_id="E12345")
        s, d, _ = platform("/api/v2/memories/search", SEARCH, project="default", key=gone, owner="E12345")
        litellm_admin("/key/delete", {"keys": [gone]})
        made_keys.remove(gone)
        r = admin_portal("/portal/api/admin/reconcile", {})
        check("reconcile drops a key deleted in LiteLLM",
              s == 200 and reg.key(sha(gone)) is None and any("deleted" in u for u in r["unlinked"]),
              f"linked on first call: {s}; reconcile unlinked={r['unlinked']}")
    finally:
        if made_keys:
            litellm_admin("/key/delete", {"keys": made_keys})
        for t in made_teams:
            litellm_admin("/team/delete", {"team_ids": [t]})
        if made_users:
            litellm_admin("/user/delete", {"user_ids": made_users})
        admin_portal("/portal/api/admin/reconcile", {})  # forget the deleted keys now, not in 10 minutes
        print(f"(removed {len(made_keys)} keys, {len(made_teams)} team, {len(made_users)} user made for this run)")

    reg.set_enabled("alice@corp.example", False)
    try:
        s, a = chat(OW, alice, QUESTION)
        check("alice switched off on the platform: chat works, no memory", s == 200 and FACT not in a, a)
        # The store runs after the reply and is judged when it runs: switching
        # her back on before it lands would let that turn be stored.
        time.sleep(8)
    finally:
        reg.set_enabled("alice@corp.example", True)
    s, a = chat(OW, alice, QUESTION)
    check("alice switched back on: recalled again", s == 200 and FACT in a, a)

if os.environ.get("PLATFORM_DB") and reg.user_by_email("carol@corp.example") is not None:
    # Put carol back the way the next run expects her: no account, no memories.
    gone = portal_as("admin@corp.example")("/portal/api/admin/users/delete",
                                           {"email": "carol@corp.example", "delete_memories": True})
    print(f"(removed carol's self-enrolled account again; memories deleted: {gone.get('memories_deleted')})")

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
