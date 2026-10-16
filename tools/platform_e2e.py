# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""End-to-end checks for platform mode: Corp SSO -> Open WebUI -> LiteLLM (hook) -> mock platform -> core.

Run against a live stack. Nothing here is collected by pytest.

    LITELLM_URL=http://127.0.0.1:4011 OWUI_URL=http://127.0.0.1:8087 \
    PLATFORM_URL=http://127.0.0.1:8100 PLATFORM_DB=/path/registry.sqlite \
    KEYS_FILE=/path/keys.env ACCOUNTS_FILE=/path/accounts.txt \
    OWUI_JWT_SECRET=... \
    python3 tools/platform_e2e.py

KEYS_FILE holds ALICE_/BOB_/CAROL_VIRTUAL_KEY and OPENWEBUI_APP_KEY (KEY=value
lines); ACCOUNTS_FILE holds "email password" lines for the Corp SSO (Keycloak)
logins, used for both Open WebUI and the portal. OWUI_JWT_SECRET is used only to
mint tokens the way Open WebUI would, for the replay and expiry checks. Expected:
alice (E12345) and bob (E67890) enrolled, carol (E24680) not; the hook names
Open WebUI users by the token's name claim, which SSO sets to the employee ID.
"""
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


def clear_earlier_runs(email):
    """Delete this script's leftovers from earlier runs, through the portal.

    Every run stores its question verbatim, and identical questions from earlier
    runs outrank the new fact in recall; left alone, each run makes the next one
    less likely to pass.
    """
    jar, _ = sso_login(f"{PLATFORM}/portal/auth/login", email, accounts[email])

    def portal(path, body=None):
        req = urllib.request.Request(
            f"{PLATFORM}{path}", data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "X-MM-Portal": "1"},
            method="POST" if body is not None else "GET")
        with jar.open(req, timeout=120) as r:
            return json.load(r)

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

s, a = chat(OW, carol, QUESTION)
check("carol (not enrolled) via Open WebUI: chat works, no memory", s == 200 and FACT not in a, a)

s, a = chat(LL, keys["CAROL_VIRTUAL_KEY"], QUESTION)
check("carol's personal key (not registered): chat works, no memory", s == 200 and FACT not in a, a)

s, a = chat(LL, keys["OPENWEBUI_APP_KEY"], QUESTION)
check("app key with no user token: 401", s == 401, a)

s, a = chat(LL, keys["OPENWEBUI_APP_KEY"], QUESTION,
            {"x-openwebui-user-jwt": mint("alice@corp.example", secret="forged-" + "x" * 40)})
check("app key + forged token: 401", s == 401, a)

s, a = chat(LL, keys["OPENWEBUI_APP_KEY"], QUESTION,
            {"x-openwebui-user-jwt": mint("alice@corp.example", ttl=-600)})
check("app key + expired token: 401", s == 401, a)

s, a = chat(LL, keys["BOB_VIRTUAL_KEY"], QUESTION, {"x-openwebui-user-jwt": mint("alice@corp.example")})
check("alice's valid token replayed on bob's key: 401", s == 401, a)

s, a = chat(LL, keys["ALICE_VIRTUAL_KEY"], QUESTION, {"x-memmachine-org": "E67890"})
check("alice's key + x-memmachine-org: E67890 (bob) -> header ignored, still alice", s == 200 and FACT in a, a)

s, a = post(f"{PLATFORM}/api/v2/memories/search", {"query": "pager"}, "mm-svc-guess")
check("platform called directly with a guessed service credential: 401", s == 401, str(a))

# Alice edits her Open WebUI display name to Bob's employee ID. Open WebUI signs
# whatever name she has, so the token is valid; the platform knows E67890 is not
# the person behind alice@corp.example and refuses.
post(f"{OWUI}/api/v1/auths/update/profile", {"name": "E67890", "profile_image_url": "/user.png"}, alice)
s, a = chat(OW, alice, QUESTION)
check("alice renamed to bob's employee ID: refused", s != 200 and FACT not in a, f"{s} {a}")
alice = owui_login("alice@corp.example")  # signing in again restores the name from SSO
check("signing in again restores her name from Corp SSO", owui_me(alice)["name"] == "E12345",
      owui_me(alice)["name"])

if os.environ.get("PLATFORM_DB"):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from mock_platform.registry import Registry

    reg = Registry(os.environ["PLATFORM_DB"])
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

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
