# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""Delete all episodic memories in a MemMachine project.

Useful for wiping test data before/after e2e test runs, or for resetting a user's
org. Lists episodes via POST /api/v2/memories/list (paginated, no vector search)
and deletes each by uid.

Target org_id is determined in priority order: --org, --user, then MEMMACHINE_ORG_ID.
With --user, the org_id is slugified from the user identifier the same way the hook
does it (e.g. alice@example.com -> alice_example_com), so you can pass the same
value the hook sees as the user_id.

Env vars (defaults shown):
  MEMMACHINE_BASE_URL   required (e.g. http://memmachine.example.com)
  MEMMACHINE_ORG_ID     no default (use --org or --user instead)
  MEMMACHINE_PROJECT_ID default default

Options:
  --org ORG_ID   target this raw org_id directly
  --user USER_ID derive org_id from this user identifier (slugified)
  --dry-run      list what would be deleted, delete nothing
  --keep USER    comma-separated producer_ids to preserve (e.g. alice@example.com)

Run:
  python3 tools/cleanup_memories.py --user alice@example.com
  python3 tools/cleanup_memories.py --org alice_example_com
  python3 tools/cleanup_memories.py --user alice@example.com --dry-run
  python3 tools/cleanup_memories.py --user alice@example.com --keep alice@example.com,bob@example.com
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("MEMMACHINE_BASE_URL", "").rstrip("/")
PROJ = os.environ.get("MEMMACHINE_PROJECT_ID", "default")
if not BASE:
    print("ERROR: MEMMACHINE_BASE_URL is required (e.g. http://memmachine.example.com)",
          file=sys.stderr)
    sys.exit(2)

# Mirror of memmachine_hook._to_org_id: MemMachine org_id only allows
# letters, digits, _, -, :, and Unicode (CJK). Replace everything else with _.
_ORG_ID_ALLOWED = re.compile(r"[^A-Za-z0-9_:\-]")


def _to_org_id(user_id: str) -> str:
    slug = _ORG_ID_ALLOWED.sub("_", str(user_id)).strip("_")
    return slug or "default"


args = sys.argv[1:]
dry_run = "--dry-run" in args
keep = set()
raw_org = os.environ.get("MEMMACHINE_ORG_ID", "")
override_org = None
override_user = None

i = 0
while i < len(args):
    a = args[i]
    if a == "--keep" and i + 1 < len(args):
        keep = {x.strip() for x in args[i + 1].split(",") if x.strip()}
        i += 2
        continue
    if a == "--org" and i + 1 < len(args):
        override_org = args[i + 1]
        i += 2
        continue
    if a == "--user" and i + 1 < len(args):
        override_user = args[i + 1]
        i += 2
        continue
    if a == "--dry-run":
        i += 1
        continue
    print(f"unknown arg: {a!r}", file=sys.stderr)
    sys.exit(2)

# Priority: --org > --user > env var
if override_org:
    ORG = override_org
elif override_user:
    ORG = _to_org_id(override_user)
elif raw_org:
    ORG = raw_org
else:
    print("ERROR: specify --org ORG_ID or --user USER_ID (or set MEMMACHINE_ORG_ID)", file=sys.stderr)
    sys.exit(2)


def req(url, method="GET", body=None):
    h = {"Content-Type": "application/json"}
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


def collect_episodes():
    """List all episodic memories via /api/v2/memories/list (paginated)."""
    eps = []
    page_num = 0
    page_size = 100
    status = 200
    while True:
        s, body = req(
            f"{BASE}/api/v2/memories/list",
            method="POST",
            body={"org_id": ORG, "project_id": PROJ,
                  "page_size": page_size, "page_num": page_num},
        )
        status = s
        if s != 200:
            break
        content = body.get("content") or {}
        page = content.get("episodic_memory") or []
        eps.extend(page)
        if len(page) < page_size:
            break  # last page
        page_num += 1
    return status, eps


print(f"=== list memories (org={ORG!r}, project={PROJ!r}) ===")
status, eps = collect_episodes()
print(f"  status={status}, total={len(eps)} episodes")
if dry_run:
    print("  (dry-run mode: nothing will be deleted)")
if keep:
    print(f"  keeping producer_ids: {keep}")

to_delete = []
for e in eps:
    pid = e.get("producer_id")
    uid = e.get("uid")
    content = (e.get("content") or "")[:50]
    if pid in keep:
        print(f"  [KEEP] uid={uid} producer={pid}: {content}")
    else:
        to_delete.append((uid, pid, content))
        print(f"  [DROP] uid={uid} producer={pid}: {content}")

if dry_run:
    print(f"\n=== dry-run: would delete {len(to_delete)} episodes ===")
    sys.exit(0)

print(f"\n=== deleting {len(to_delete)} episodes ===")
deleted = 0
for uid, pid, _ in to_delete:
    status, body = req(
        f"{BASE}/api/v2/memories/episodic/delete",
        method="POST",
        body={"org_id": ORG, "project_id": PROJ, "episodic_id": str(uid)},
    )
    if status == 204:
        deleted += 1
    else:
        print(f"  uid={uid} delete failed: status={status}, body={json.dumps(body)[:100]}",
              file=sys.stderr)
print(f"  deleted: {deleted}/{len(to_delete)}")

print("\n=== verify after deletion ===")
status, eps_after = collect_episodes()
print(f"  remaining: {len(eps_after)} episodes")
for e in eps_after:
    print(f"    uid={e.get('uid')}, producer={e.get('producer_id')}, "
          f"content={(e.get('content') or '')[:50]}")
