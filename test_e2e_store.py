# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""E2E test: store path through the litellm proxy + MemMachine.

Verifies that a chat completion through the litellm proxy (with a virtual key)
gets stored in MemMachine with the virtual-key owner's slugified email as `producer_id`,
in that user's default personal org.

Env vars (all required unless a default is shown):
  LITELLM_BASE_URL          litellm proxy URL (e.g. http://litellm.example.com)
  MEMMACHINE_BASE_URL       MemMachine URL (e.g. http://memmachine.example.com)
  LITELLM_MASTER_KEY        litellm master key (for /v1/models listing)
  LITELLM_TEST_VIRTUAL_KEY  virtual key whose owner's email we expect to see
  MEMMACHINE_TEST_MODEL     default memmachine-llm-medium
  MEMMACHINE_TEST_PROJECT   default default
  MEMMACHINE_TEST_EXPECTED_USER  virtual-key owner email; slugified into the expected
                            producer_id and default org_id we check against

Run:
  LITELLM_BASE_URL=http://litellm.example.com \
  MEMMACHINE_BASE_URL=http://memmachine.example.com \
  LITELLM_MASTER_KEY=sk-... LITELLM_TEST_VIRTUAL_KEY=sk-... \
  MEMMACHINE_TEST_EXPECTED_USER=alice@example.com \
  python3 test_e2e_store.py

Exits 0 on success, 1 on failure.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

LITELLM = os.environ.get("LITELLM_BASE_URL", "").rstrip("/")
MEMMACHINE = os.environ.get("MEMMACHINE_BASE_URL", "").rstrip("/")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
VIRTUAL_KEY = os.environ.get("LITELLM_TEST_VIRTUAL_KEY", "")
MODEL = os.environ.get("MEMMACHINE_TEST_MODEL", "memmachine-llm-medium")
PROJ = os.environ.get("MEMMACHINE_TEST_PROJECT", "default")
EXPECTED_USER = os.environ.get("MEMMACHINE_TEST_EXPECTED_USER", "")

# Mirror of memmachine_hook._to_org_id: MemMachine org_id allows letters, digits,
# _, -, :, Unicode only. Replace everything else with _.
_ORG_ID_ALLOWED = re.compile(r"[^A-Za-z0-9_:\-]")


def _to_org_id(user_id):
    slug = _ORG_ID_ALLOWED.sub("_", str(user_id)).strip("_")
    return slug or "default"


CANONICAL_USER = _to_org_id(EXPECTED_USER)
ORG = CANONICAL_USER

MISSING = [n for n, v in [("LITELLM_BASE_URL", LITELLM),
                          ("MEMMACHINE_BASE_URL", MEMMACHINE),
                          ("LITELLM_MASTER_KEY", MASTER_KEY),
                          ("LITELLM_TEST_VIRTUAL_KEY", VIRTUAL_KEY),
                          ("MEMMACHINE_TEST_EXPECTED_USER", EXPECTED_USER)] if not v]
if MISSING:
    print(f"ERROR: missing env vars: {MISSING}", file=sys.stderr)
    sys.exit(2)


def req(url, method="GET", body=None, headers=None):
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {"raw": "non-json"}


print("=== 1) litellm health ===")
status, body = req(f"{LITELLM}/health/liveliness")
print(f"  {status}: {body if isinstance(body, str) else json.dumps(body)[:100]}")
if status != 200:
    print("  container not up yet. retry later.", file=sys.stderr)
    sys.exit(1)

print("\n=== 2) registered models ===")
status, body = req(f"{LITELLM}/v1/models", headers={"Authorization": f"Bearer {MASTER_KEY}"})
models = body.get("data", []) if isinstance(body, dict) else []
mm_models = [m["id"] for m in models if m.get("id", "").startswith("memmachine-")]
print(f"  models: {[m.get('id') for m in models]}")
print(f"  memmachine-* models: {mm_models}")
if not mm_models:
    print(f"  WARN: no memmachine-* alias. check Web UI model_group_alias", file=sys.stderr)

print(f"\n=== 3) chat completion to {MODEL} (store trigger) ===")
status, body = req(
    f"{LITELLM}/v1/chat/completions",
    method="POST",
    headers={"Authorization": f"Bearer {VIRTUAL_KEY}"},
    body={
        "model": MODEL,
        "messages": [{"role": "user", "content": "e2e store test: my favorite color is purple. remember it."}],
        "max_tokens": 3000,
    },
)
if status == 200:
    try:
        content = body.get("choices", [{}])[0].get("message", {}).get("content") or ""
        print(f"  status=200, content={content[:50]!r}")
    except Exception as e:
        print(f"  status=200, parse error: {e}, body={json.dumps(body)[:300]}", file=sys.stderr)
        sys.exit(1)
else:
    print(f"  status={status}, body={json.dumps(body)[:300]}", file=sys.stderr)
    sys.exit(1)

print("\n=== 4) verify storage in MemMachine (producer_id) ===")
print("  waiting 5s for indexing...")
time.sleep(5)

# Use /api/v2/memories/list (paginated, no vector search) to verify storage.
# Vector search (/memories/search) can return 500 on empty/edge queries and is
# not needed here — we just want to confirm the episode was stored with the
# right producer_id.
eps = []
page_num = 0
page_size = 100
while True:
    status, body = req(
        f"{MEMMACHINE}/api/v2/memories/list",
        method="POST",
        body={"org_id": ORG, "project_id": PROJ,
              "page_size": page_size, "page_num": page_num},
    )
    if status != 200:
        break
    page = (body.get("content") or {}).get("episodic_memory") or []
    eps.extend(page)
    if len(page) < page_size:
        break
    page_num += 1
print(f"  status={status}, episodes={len(eps)}")
producers = set()
for e in eps[-5:]:
    pid = e.get("producer_id")
    producers.add(pid)
    print(f"    producer_id={pid!r}, role={e.get('producer_role')}, content={(e.get('content') or '')[:70]}")

print(f"\n=== result ===")
print(f"  observed producer_ids: {producers}")
print(f"  expected canonical user: {CANONICAL_USER!r}")
if CANONICAL_USER in producers:
    print("  PASS: user_email extracted, slugified, and stored as producer_id")
    sys.exit(0)
elif producers == {"default"} or producers == {None} or not producers:
    print("  FAIL: producer_id is 'default'/empty — user_email extraction failed", file=sys.stderr)
    sys.exit(1)
else:
    print(f"  FAIL: unexpected producers: {producers}", file=sys.stderr)
    sys.exit(1)
