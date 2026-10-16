# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the mock platform: every authorize() rule, and what reaches core.

Core is an httpx.MockTransport that records requests, so nothing here touches
the network.

    uv run --group platform pytest test_platform.py -v
"""
import json
import time

import httpx
import pytest

pytest.importorskip("fastapi", reason="the mock platform needs the 'platform' dependency group")
jwt = pytest.importorskip("jwt")

from fastapi.testclient import TestClient  # noqa: E402

from mock_platform.app import create_app  # noqa: E402
from mock_platform.authorize import MAX_REQUEST_AGE_SECONDS  # noqa: E402
from mock_platform.registry import Registry, sha256  # noqa: E402

SECRET = "owui-signing-secret-for-tests-0123456789"
ALICE_KEY = "sk-alice-personal"
BOB_KEY = "sk-bob-personal"
ERIN_KEY = "sk-erin-personal"
APP_KEY = "sk-open-webui-app"
STRANGER_KEY = "sk-not-registered"

# Alice and Bob have employee IDs (Corp SSO); Erin has none, so her email stands in.
PEOPLE = {
    "alice@corp.example": "E12345",
    "bob@corp.example": "E67890",
    "dora@corp.example": "E11111",
    "erin@corp.example": None,
}
ORG = {"alice@corp.example": "E12345", "bob@corp.example": "E67890",
       "dora@corp.example": "E11111", "erin@corp.example": "erin_corp_example"}

EMPTY_SEARCH = {
    "status": 0,
    "content": {"episodic_memory": {"long_term_memory": {"episodes": []}}},
}


@pytest.fixture
def world(tmp_path):
    reg = Registry(str(tmp_path / "reg.sqlite"))
    service = reg.add_service("litellm-test")
    for email, emp in PEOPLE.items():
        reg.add_user(email, employee_id=emp)
    reg.link_personal_key("alice@corp.example", ALICE_KEY, "alice-personal")
    reg.link_personal_key("bob@corp.example", BOB_KEY, "bob-personal")
    reg.link_personal_key("erin@corp.example", ERIN_KEY, "erin-personal")
    reg.add_app_key(APP_KEY, "open-webui-app", SECRET, "open-webui")
    reg.set_enabled("dora@corp.example", False)

    core_requests = []

    def core(request: httpx.Request):
        core_requests.append({"path": request.url.path, "json": json.loads(request.content)})
        if request.url.path == "/api/v2/memories/search":
            return httpx.Response(200, json=EMPTY_SEARCH)
        return httpx.Response(200, json={"results": []})

    app = create_app(reg, "http://core.test", transport=httpx.MockTransport(core))
    client = TestClient(app)
    return type("World", (), {"reg": reg, "service": service, "client": client, "core": core_requests})


def token(email="alice@corp.example", *, name=None, secret=SECRET, issuer="open-webui",
          ttl=300, iat=None):
    """Open WebUI's forwarded token. With Corp SSO, `name` is the employee ID."""
    now = int(time.time() if iat is None else iat)
    claims = {"sub": "uuid-1", "email": email, "name": name if name is not None else PEOPLE.get(email) or "",
              "role": "user", "iss": issuer, "iat": now, "exp": now + ttl}
    return jwt.encode(claims, secret, algorithm="HS256")


def headers(world, key=None, user_token=None, user=None, owner=None, project=None,
            service=None, started=None):
    h = {"Authorization": f"Bearer {service or world.service}", "X-Request-Id": "req-1"}
    if key is not None:
        h["X-MemMachine-Key-Id"] = sha256(key)
    if owner is not None:
        h["X-MemMachine-Key-Owner"] = owner
    if user_token is not None:
        h["X-MemMachine-User-Assertion"] = user_token
    if user is not None:
        h["X-MemMachine-User"] = user
    if project is not None:
        h["X-MemMachine-Project"] = project
    if started is not None:
        h["X-MemMachine-Request-Started"] = str(started)
    return h


def ns(org, project="default"):
    return {"org_id": org, "project_id": project}


def search(world, org=None, project="default", body=None, project_header=None, **kw):
    body = body or {**ns(org or "", project), "query": "what do I like?", "top_k": 5,
                    "types": ["episodic"]}
    return world.client.post("/api/v2/memories/search", json=body,
                             headers=headers(world, project=project_header, **kw))


def store(world, org=None, project="default", body=None, **kw):
    body = body or {
        **ns(org or "", project),
        "types": [],
        "messages": [
            {"content": "I like cats", "producer": "", "produced_for": "litellm",
             "role": "user", "metadata": {"user_id": "", "session_id": ""}},
            {"content": "Noted.", "producer": "litellm", "produced_for": "",
             "role": "assistant", "metadata": {"user_id": "", "session_id": ""}},
        ],
    }
    return world.client.post("/api/v2/memories", json=body, headers=headers(world, **kw))


def as_owui(email, **kw):
    """What the hook sends for an Open WebUI chat when it names people by employee ID."""
    t = token(email)
    return {"key": APP_KEY, "user_token": t, "user": PEOPLE[email] or email, "org": ORG[email], **kw}


def decision(resp):
    return resp.headers.get("x-memmachine-decision")


class TestWhoIsCalling:
    def test_unknown_service_credential_is_401(self, world):
        r = search(world, key=ALICE_KEY, org="E12345", service="mm-svc-forged")
        assert r.status_code == 401 and decision(r) == "refused"
        assert world.core == []

    def test_missing_authorization_is_401(self, world):
        r = world.client.post("/api/v2/memories/search", json={"query": "x"},
                              headers={"X-MemMachine-Key-Id": sha256(ALICE_KEY)})
        assert r.status_code == 401

    def test_no_key_evidence_is_401(self, world):
        assert search(world, org="E12345").status_code == 401


class TestPersonalKey:
    def test_resolves_to_owner_namespace(self, world):
        r = search(world, key=ALICE_KEY, owner="E12345", user="E12345", org="E12345")
        assert r.status_code == 200
        assert world.core[-1]["json"]["org_id"] == "E12345"
        assert world.core[-1]["json"]["project_id"] == "default"

    def test_email_stands_in_without_employee_id(self, world):
        r = search(world, key=ERIN_KEY, owner="erin@corp.example", org="erin_corp_example")
        assert r.status_code == 200
        assert world.core[-1]["json"]["org_id"] == "erin_corp_example"

    def test_token_on_personal_key_is_refused(self, world):
        # Alice's token replayed on Bob's key must not reach either namespace.
        r = search(world, key=BOB_KEY, user_token=token("alice@corp.example"), org="E12345")
        assert r.status_code == 401 and decision(r) == "refused"
        assert world.core == []

    def test_naming_someone_else_is_refused(self, world):
        r = search(world, key=BOB_KEY, user="E12345", org="E12345")
        assert r.status_code == 403 and decision(r) == "refused"
        assert world.core == []

    def test_litellm_owner_must_be_the_linked_person(self, world):
        # The key was reassigned in LiteLLM after the platform linked it.
        r = search(world, key=ALICE_KEY, owner="E67890", org="E12345")
        assert r.status_code == 403 and decision(r) == "refused"

    def test_another_persons_org_is_refused(self, world):
        r = search(world, key=ALICE_KEY, org="E67890")
        assert r.status_code == 403 and decision(r) == "refused"
        assert world.core == []

    def test_only_known_fields_reach_core(self, world):
        body = {**ns("E12345"), "query": "x", "top_k": 1, "types": [],
                "filter": "producer_id = 'bob'"}
        r = search(world, key=ALICE_KEY, body=body)
        assert r.status_code == 200
        assert "filter" not in world.core[-1]["json"]


class TestAppKey:
    def test_token_resolves_to_token_user(self, world):
        r = search(world, **as_owui("bob@corp.example"))
        assert r.status_code == 200
        assert world.core[-1]["json"]["org_id"] == "E67890"

    def test_email_matching_ignores_case(self, world):
        r = search(world, key=APP_KEY, user_token=token("Bob@Corp.Example", name="E67890"),
                   user="E67890", org="E67890")
        assert r.status_code == 200

    def test_email_stands_in_without_employee_id(self, world):
        r = search(world, **as_owui("erin@corp.example"))
        assert r.status_code == 200
        assert world.core[-1]["json"]["org_id"] == "erin_corp_example"

    def test_app_key_without_token_is_401(self, world):
        assert search(world, key=APP_KEY, user="E12345", org="E12345").status_code == 401

    def test_app_key_without_named_user_is_401(self, world):
        r = search(world, key=APP_KEY, user_token=token("alice@corp.example"), org="E12345")
        assert r.status_code == 401 and decision(r) == "refused"

    def test_renamed_display_name_is_refused(self, world):
        # Alice edits her Open WebUI name to Bob's employee ID. Open WebUI signs
        # whatever name she has, so the token is valid; it is still Alice's email.
        t = token("alice@corp.example", name="E67890")
        r = search(world, key=APP_KEY, user_token=t, user="E67890", org="E67890")
        assert r.status_code == 403 and decision(r) == "refused"
        assert world.core == []

    def test_named_user_must_be_in_the_token(self, world):
        # A hook that names someone the token does not mention.
        t = token("alice@corp.example", name="")
        r = search(world, key=APP_KEY, user_token=t, user="E12345", org="E12345")
        assert r.status_code == 403 and decision(r) == "refused"

    def test_email_is_refused_once_an_employee_id_is_known(self, world):
        # A hook still in phase-1 mode names Alice by email; the platform knows E12345.
        r = search(world, key=APP_KEY, user_token=token("alice@corp.example"),
                   user="alice@corp.example", org="alice_corp_example")
        assert r.status_code == 403

    @pytest.mark.parametrize("bad", [
        lambda: token(secret="another-secret-entirely-0123456789"),
        lambda: token(issuer="someone-else"),
        lambda: token(ttl=-600),
        lambda: jwt.encode({"email": "alice@corp.example", "name": "E12345", "iss": "open-webui",
                            "exp": int(time.time()) + 300}, "", algorithm="none"),
        lambda: jwt.encode({"iss": "open-webui", "name": "E12345", "exp": int(time.time()) + 300},
                           SECRET, algorithm="HS256"),
        lambda: "not-a-token",
    ], ids=["wrong-secret", "wrong-issuer", "expired", "alg-none", "no-email", "garbage"])
    def test_bad_tokens_are_401(self, world, bad):
        r = search(world, key=APP_KEY, user_token=bad(), user="E12345", org="E12345")
        assert r.status_code == 401 and decision(r) == "refused"
        assert world.core == []


class TestNamespace:
    def test_project_not_granted_is_403(self, world):
        r = search(world, **as_owui("alice@corp.example", project="payroll"))
        assert r.status_code == 403 and decision(r) == "refused"
        assert world.core == []

    def test_project_header_must_match_body(self, world):
        r = search(world, key=APP_KEY, user_token=token("alice@corp.example"), user="E12345",
                   org="E12345", project="default", project_header="payroll")
        assert r.status_code == 403

    def test_missing_org_is_403(self, world):
        r = search(world, key=ALICE_KEY, org="")
        assert r.status_code == 403


class TestNotEnrolled:
    def test_unknown_key(self, world):
        r = search(world, key=STRANGER_KEY, org="x")
        assert r.status_code == 403 and decision(r) == "not-enrolled"

    def test_unregistered_token_user(self, world):
        r = search(world, key=APP_KEY, user_token=token("carol@corp.example", name="E24680"),
                   user="E24680", org="E24680")
        assert r.status_code == 403 and decision(r) == "not-enrolled"

    def test_disabled_user(self, world):
        r = search(world, **as_owui("dora@corp.example"))
        assert r.status_code == 403 and decision(r) == "not-enrolled"

    def test_re_enabling_restores_access(self, world):
        world.reg.set_enabled("dora@corp.example", True)
        assert search(world, **as_owui("dora@corp.example")).status_code == 200


class TestStore:
    def test_namespace_verified_and_provenance_set(self, world):
        r = store(world, **as_owui("alice@corp.example"))
        assert r.status_code == 200
        project_call, add_call = world.core[0], world.core[-1]
        assert project_call["path"] == "/api/v2/projects"
        sent = add_call["json"]
        assert (sent["org_id"], sent["project_id"]) == ("E12345", "default")
        user_msg, assistant_msg = sent["messages"]
        assert user_msg["producer"] == "E12345"
        assert assistant_msg["produced_for"] == "E12345"
        assert assistant_msg["producer"] == "litellm"
        assert user_msg["metadata"]["user_id"] == "E12345"
        assert user_msg["metadata"]["session_id"] == "E12345"

    def test_forged_producer_is_overwritten(self, world):
        body = {**ns("E12345"), "types": [], "messages": [
            {"content": "hi", "producer": "E67890", "role": "user", "metadata": {}}]}
        store(world, key=ALICE_KEY, body=body)
        assert world.core[-1]["json"]["messages"][0]["producer"] == "E12345"

    def test_store_into_another_org_is_refused(self, world):
        r = store(world, key=ALICE_KEY, org="E67890")
        assert r.status_code == 403
        assert world.core == []

    def test_project_is_created_once(self, world):
        store(world, key=ALICE_KEY, org="E12345")
        store(world, key=ALICE_KEY, org="E12345")
        assert [c["path"] for c in world.core].count("/api/v2/projects") == 1

    def test_token_expired_after_start_is_judged_at_start(self, world):
        # A short token that expired while the model was answering still stores.
        started = time.time() - 120
        t = token("alice@corp.example", iat=started, ttl=60)
        r = store(world, key=APP_KEY, user_token=t, user="E12345", org="E12345", started=started)
        assert r.status_code == 200

    def test_start_time_too_old_is_refused(self, world):
        started = time.time() - MAX_REQUEST_AGE_SECONDS - 60
        t = token("alice@corp.example", iat=started, ttl=MAX_REQUEST_AGE_SECONDS * 2)
        r = store(world, key=APP_KEY, user_token=t, user="E12345", org="E12345", started=started)
        assert r.status_code == 401


def test_other_core_paths_are_not_exposed(world):
    r = world.client.post("/api/v2/memories/delete", json={}, headers=headers(world, key=ALICE_KEY))
    assert r.status_code in (404, 405)
    assert world.core == []
