# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""End-to-end checks for platform mode: Open WebUI -> LiteLLM (hook) -> mock platform -> core.

Run against a live stack. Nothing here is collected by pytest.

    LITELLM_URL=http://127.0.0.1:4011 OWUI_URL=http://127.0.0.1:8087 \
    PLATFORM_URL=http://127.0.0.1:8100 PLATFORM_DB=/path/registry.sqlite \
    KEYS_FILE=/path/keys.env ACCOUNTS_FILE=/path/accounts.txt \
    OWUI_JWT_SECRET=... \
    python3 tools/platform_e2e.py

KEYS_FILE holds ALICE_/BOB_/CAROL_VIRTUAL_KEY and OPENWEBUI_APP_KEY (KEY=value
lines); ACCOUNTS_FILE holds "email password" lines for the Open WebUI logins.
OWUI_JWT_SECRET is used only to mint tokens the way Open WebUI would, for the
replay and expiry checks. Expected registry: alice and bob enrolled, carol not.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

import jwt

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
    _, body = post(f"{OWUI}/api/v1/auths/signin", {"email": email, "password": accounts[email]}, "-")
    return body["token"]


def mint(email, ttl=300, secret=SECRET):
    now = int(time.time())
    return jwt.encode({"sub": "e2e", "email": email, "iss": "open-webui", "iat": now, "exp": now + ttl},
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
    import http.cookiejar
    jar = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def portal(path, body=None):
        req = urllib.request.Request(
            f"{PLATFORM}{path}", data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "X-MM-Portal": "1"},
            method="POST" if body is not None else "GET")
        with jar.open(req, timeout=120) as r:
            return json.load(r)

    portal("/portal/api/login", {"email": email, "password": accounts[email]})
    ids = [i for t in portal("/portal/api/memories")["turns"]
           if "codename of project" in (t["you"] or "") + (t["reply"] or "") for i in t["ids"]]
    if ids:
        portal("/portal/api/memories/delete", {"ids": ids})
    print(f"(cleared {len(ids)} messages left by earlier runs from {email})")


OW = f"{OWUI}/api/chat/completions"
LL = f"{LITELLM}/v1/chat/completions"
clear_earlier_runs("alice@corp.example")
alice, bob, carol = (owui_login(f"{n}@corp.example") for n in ("alice", "bob", "carol"))

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

s, a = chat(LL, keys["ALICE_VIRTUAL_KEY"], QUESTION, {"x-memmachine-org": "bob_corp_example"})
check("alice's key + x-memmachine-org: bob -> header ignored, still alice", s == 200 and FACT in a, a)

s, a = post(f"{PLATFORM}/api/v2/memories/search", {"query": "pager"}, "mm-svc-guess")
check("platform called directly with a guessed service credential: 401", s == 401, str(a))

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
