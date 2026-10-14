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
APP_KEY = "sk-open-webui-app"
STRANGER_KEY = "sk-not-registered"

EMPTY_SEARCH = {
    "status": 0,
    "content": {"episodic_memory": {"long_term_memory": {"episodes": []}}},
}


@pytest.fixture
def world(tmp_path):
    reg = Registry(str(tmp_path / "reg.sqlite"))
    service = reg.add_service("litellm-test")
    reg.add_user("alice@corp.example")
    reg.add_user("bob@corp.example")
    reg.add_user("dora@corp.example")
    reg.link_personal_key("alice@corp.example", ALICE_KEY, "alice-personal")
    reg.link_personal_key("bob@corp.example", BOB_KEY, "bob-personal")
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


def token(email="alice@corp.example", *, secret=SECRET, issuer="open-webui", ttl=300, iat=None):
    now = int(time.time() if iat is None else iat)
    claims = {"sub": "uuid-1", "email": email, "iss": issuer, "iat": now, "exp": now + ttl}
    return jwt.encode(claims, secret, algorithm="HS256")


def headers(world, key=None, user_token=None, service=None, started=None):
    h = {"Authorization": f"Bearer {service or world.service}"}
    if key is not None:
        h["X-MM-Key-Hash"] = sha256(key)
    if user_token is not None:
        h["X-MM-User-Token"] = user_token
    if started is not None:
        h["X-MM-Request-Started"] = str(started)
    return h


def search(world, **kw):
    body = kw.pop("body", {"query": "what do I like?", "top_k": 5, "types": ["episodic"]})
    return world.client.post("/api/v2/memories/search", json=body, headers=headers(world, **kw))


def store(world, **kw):
    body = kw.pop("body", None) or {
        "types": [],
        "messages": [
            {"content": "I like cats", "producer": "", "produced_for": "litellm",
             "role": "user", "metadata": {"user_id": "", "session_id": ""}},
            {"content": "Noted.", "producer": "litellm", "produced_for": "",
             "role": "assistant", "metadata": {"user_id": "", "session_id": ""}},
        ],
    }
    return world.client.post("/api/v2/memories", json=body, headers=headers(world, **kw))


def decision(resp):
    return resp.headers.get("x-mm-decision")


class TestWhoIsCalling:
    def test_unknown_service_credential_is_401(self, world):
        r = search(world, key=ALICE_KEY, service="mm-svc-forged")
        assert r.status_code == 401 and decision(r) == "refused"
        assert world.core == []

    def test_missing_authorization_is_401(self, world):
        r = world.client.post("/api/v2/memories/search", json={"query": "x"},
                              headers={"X-MM-Key-Hash": sha256(ALICE_KEY)})
        assert r.status_code == 401

    def test_no_key_evidence_is_401(self, world):
        assert search(world).status_code == 401


class TestPersonalKey:
    def test_resolves_to_owner_namespace(self, world):
        r = search(world, key=ALICE_KEY)
        assert r.status_code == 200
        assert r.headers["x-mm-org"] == "alice_corp_example"
        assert world.core[-1]["json"]["org_id"] == "alice_corp_example"
        assert world.core[-1]["json"]["project_id"] == "default"

    def test_token_on_personal_key_is_refused(self, world):
        # Alice's token replayed on Bob's key must not reach either namespace.
        r = search(world, key=BOB_KEY, user_token=token("alice@corp.example"))
        assert r.status_code == 401 and decision(r) == "refused"
        assert world.core == []

    def test_client_namespace_in_body_is_overwritten(self, world):
        body = {"query": "x", "top_k": 1, "types": [], "org_id": "bob_corp_example",
                "project_id": "secret", "filter": "producer_id = 'bob'"}
        r = search(world, key=ALICE_KEY, body=body)
        assert r.status_code == 200
        sent = world.core[-1]["json"]
        assert sent["org_id"] == "alice_corp_example" and sent["project_id"] == "default"
        assert "filter" not in sent


class TestAppKey:
    def test_token_resolves_to_token_user(self, world):
        r = search(world, key=APP_KEY, user_token=token("bob@corp.example"))
        assert r.status_code == 200
        assert world.core[-1]["json"]["org_id"] == "bob_corp_example"

    def test_email_matching_ignores_case(self, world):
        r = search(world, key=APP_KEY, user_token=token("Bob@Corp.Example"))
        assert r.status_code == 200
        assert world.core[-1]["json"]["org_id"] == "bob_corp_example"

    def test_app_key_without_token_is_401(self, world):
        assert search(world, key=APP_KEY).status_code == 401

    @pytest.mark.parametrize("bad", [
        lambda: token(secret="another-secret-entirely-0123456789"),
        lambda: token(issuer="someone-else"),
        lambda: token(ttl=-600),
        lambda: jwt.encode({"email": "alice@corp.example", "iss": "open-webui",
                            "exp": int(time.time()) + 300}, "", algorithm="none"),
        lambda: jwt.encode({"iss": "open-webui", "exp": int(time.time()) + 300},
                           SECRET, algorithm="HS256"),
        lambda: "not-a-token",
    ], ids=["wrong-secret", "wrong-issuer", "expired", "alg-none", "no-email", "garbage"])
    def test_bad_tokens_are_401(self, world, bad):
        r = search(world, key=APP_KEY, user_token=bad())
        assert r.status_code == 401 and decision(r) == "refused"
        assert world.core == []


class TestNotEnrolled:
    def test_unknown_key(self, world):
        r = search(world, key=STRANGER_KEY)
        assert r.status_code == 403 and decision(r) == "not-enrolled"

    def test_unregistered_token_user(self, world):
        r = search(world, key=APP_KEY, user_token=token("carol@corp.example"))
        assert r.status_code == 403 and decision(r) == "not-enrolled"

    def test_disabled_user(self, world):
        r = search(world, key=APP_KEY, user_token=token("dora@corp.example"))
        assert r.status_code == 403 and decision(r) == "not-enrolled"

    def test_re_enabling_restores_access(self, world):
        world.reg.set_enabled("dora@corp.example", True)
        assert search(world, key=APP_KEY, user_token=token("dora@corp.example")).status_code == 200


class TestStore:
    def test_platform_sets_namespace_and_provenance(self, world):
        r = store(world, key=APP_KEY, user_token=token("alice@corp.example"))
        assert r.status_code == 200
        project_call, add_call = world.core[0], world.core[-1]
        assert project_call["path"] == "/api/v2/projects"
        sent = add_call["json"]
        assert (sent["org_id"], sent["project_id"]) == ("alice_corp_example", "default")
        user_msg, assistant_msg = sent["messages"]
        assert user_msg["producer"] == "alice_corp_example"
        assert assistant_msg["produced_for"] == "alice_corp_example"
        assert assistant_msg["producer"] == "litellm"
        assert user_msg["metadata"]["user_id"] == "alice_corp_example"
        assert user_msg["metadata"]["session_id"] == "alice_corp_example"

    def test_forged_producer_is_overwritten(self, world):
        body = {"types": [], "messages": [
            {"content": "hi", "producer": "bob_corp_example", "role": "user", "metadata": {}}]}
        store(world, key=ALICE_KEY, body=body)
        assert world.core[-1]["json"]["messages"][0]["producer"] == "alice_corp_example"

    def test_project_is_created_once(self, world):
        store(world, key=ALICE_KEY)
        store(world, key=ALICE_KEY)
        assert [c["path"] for c in world.core].count("/api/v2/projects") == 1

    def test_token_expired_after_start_is_judged_at_start(self, world):
        # A short token that expired while the model was answering still stores.
        started = time.time() - 120
        t = token("alice@corp.example", iat=started, ttl=60)
        assert store(world, key=APP_KEY, user_token=t, started=started).status_code == 200

    def test_start_time_too_old_is_refused(self, world):
        started = time.time() - MAX_REQUEST_AGE_SECONDS - 60
        t = token("alice@corp.example", iat=started, ttl=MAX_REQUEST_AGE_SECONDS * 2)
        assert store(world, key=APP_KEY, user_token=t, started=started).status_code == 401


def test_other_core_paths_are_not_exposed(world):
    r = world.client.post("/api/v2/memories/delete", json={}, headers=headers(world, key=ALICE_KEY))
    assert r.status_code in (404, 405)
    assert world.core == []
