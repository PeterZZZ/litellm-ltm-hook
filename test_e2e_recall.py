# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""E2E test: recall path through the litellm proxy + MemMachine.

Two-turn flow:
  Turn 1: tell the model a fact (stored in MemMachine via async_log_success_event).
  Turn 2: ask a question that should be answerable from that fact.
          The hook's async_pre_call_hook should search MemMachine, inject a
          <memory> block, and the model should recall the fact.

Env vars (all required unless a default is shown):
  LITELLM_BASE_URL          litellm proxy URL (e.g. http://litellm.example.com)
  LITELLM_TEST_VIRTUAL_KEY  virtual key to authenticate the request
  MEMMACHINE_TEST_MODEL     default memmachine-llm-medium
  MEMMACHINE_TEST_FACT      fact to plant (default: "my favorite color is purple")
  MEMMACHINE_TEST_RECALL_QUERY  default "what is my favorite color?"
  MEMMACHINE_TEST_RECALL_KEYWORD  substring expected in the recall answer
                            (default "purple")

Run:
  LITELLM_BASE_URL=http://litellm.example.com \
  LITELLM_TEST_VIRTUAL_KEY=sk-... python3 test_e2e_recall.py

Exits 0 on success, 1 on failure.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

LITELLM = os.environ.get("LITELLM_BASE_URL", "").rstrip("/")
VIRTUAL_KEY = os.environ.get("LITELLM_TEST_VIRTUAL_KEY", "")
MODEL = os.environ.get("MEMMACHINE_TEST_MODEL", "memmachine-llm-medium")
FACT = os.environ.get("MEMMACHINE_TEST_FACT", "my favorite color is purple")
RECALL_QUERY = os.environ.get("MEMMACHINE_TEST_RECALL_QUERY", "what is my favorite color?")
RECALL_KEYWORD = os.environ.get("MEMMACHINE_TEST_RECALL_KEYWORD", "purple")

_missing = [n for n, v in [("LITELLM_BASE_URL", LITELLM),
                          ("LITELLM_TEST_VIRTUAL_KEY", VIRTUAL_KEY)] if not v]
if _missing:
    print(f"ERROR: missing env vars: {_missing}", file=sys.stderr)
    sys.exit(2)


def req(url, body=None, headers=None):
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(r) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {"raw": "non-json"}


print("=== turn 1: plant fact ===")
print(f"  fact: {FACT!r}")
status, body = req(
    f"{LITELLM}/v1/chat/completions",
    body={
        "model": MODEL,
        "messages": [{"role": "user", "content": f"e2e recall test: {FACT}. remember it."}],
        "max_tokens": 3000,
    },
    headers={"Authorization": f"Bearer {VIRTUAL_KEY}"},
)
if status != 200:
    print(f"  turn 1 failed: status={status}, body={json.dumps(body)[:300]}", file=sys.stderr)
    sys.exit(1)
content1 = body.get("choices", [{}])[0].get("message", {}).get("content") or ""
print(f"  turn 1 status=200, content={content1[:50]!r}")

print("\n  waiting 5s for indexing before recall...")
time.sleep(5)

print(f"\n=== turn 2: recall ===")
print(f"  query: {RECALL_QUERY!r}")
status, body = req(
    f"{LITELLM}/v1/chat/completions",
    body={
        "model": MODEL,
        "messages": [{"role": "user", "content": RECALL_QUERY}],
        "max_tokens": 3000,
    },
    headers={"Authorization": f"Bearer {VIRTUAL_KEY}"},
)
if status != 200:
    print(f"  turn 2 failed: status={status}, body={json.dumps(body)[:300]}", file=sys.stderr)
    sys.exit(1)
content2 = body.get("choices", [{}])[0].get("message", {}).get("content") or ""
print(f"  turn 2 status=200")
print(f"  answer: {content2!r}")

print(f"\n=== result ===")
print(f"  looking for keyword {RECALL_KEYWORD!r} in answer")
if RECALL_KEYWORD.lower() in content2.lower():
    print("  PASS: model recalled the fact — memory injection working")
    sys.exit(0)
else:
    print(f"  FAIL: keyword {RECALL_KEYWORD!r} not in answer", file=sys.stderr)
    print(f"  (if turn 1 just stored, turn 2 may have raced the index — try again)", file=sys.stderr)
    sys.exit(1)
