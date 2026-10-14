# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the My Memory portal (mock_platform/portal.py).

Open WebUI's sign-in and MemMachine core are one httpx.MockTransport, so
nothing here touches the network.

    uv run --group platform pytest test_portal.py -v
"""
import json

import httpx
import pytest

pytest.importorskip("fastapi", reason="the portal needs the 'platform' dependency group")

from fastapi.testclient import TestClient  # noqa: E402

from mock_platform.app import create_app  # noqa: E402
from mock_platform.authorize import authorize, Denied  # noqa: E402
from mock_platform.portal import _turns  # noqa: E402
from mock_platform.registry import Registry, sha256  # noqa: E402

PASSWORDS = {"alice@corp.example": "alice-pw", "bob@corp.example": "bob-pw",
             "carol@corp.example": "carol-pw"}
W = {"X-MM-Portal": "1"}


def ep(uid, role, text, ns, at="2026-10-14T10:00:00.000000Z", via=None):
    return {"uid": str(uid), "content": text, "producer_role": role, "created_at": at,
            "session_key": ns, "metadata": {"via": via} if via else {}}


@pytest.fixture
def world(tmp_path):
    reg = Registry(str(tmp_path / "reg.sqlite"))
    reg.add_user("alice@corp.example")
    reg.add_user("bob@corp.example")
    svc = reg.add_service("litellm-test")
    reg.link_personal_key("alice@corp.example", "sk-alice", "alice-personal")
    store = {
        "alice_corp_example/default": [
            ep(1, "user", "I ride a Kona bike", "a", via="app-token"),
            ep(2, "assistant", "Noted, a Kona.", "a", via="app-token"),
            ep(3, "user", "### Task: Generate a concise title for the chat", "a",
               at="2026-10-14T11:00:00.000000Z"),
            ep(4, "assistant", '{"title":"Bike"}', "a", at="2026-10-14T11:00:00.000000Z"),
        ],
        "bob_corp_example/default": [ep(50, "user", "bob's secret", "b")],
    }
    calls = []

    def transport(request: httpx.Request):
        body = json.loads(request.content or b"{}")
        path = request.url.path
        calls.append((path, body))
        if path == "/api/v1/auths/signin":
            if PASSWORDS.get(body.get("email")) == body.get("password"):
                return httpx.Response(200, json={"email": body["email"], "name": "N", "token": "t"})
            return httpx.Response(400, json={"detail": "bad"})
        ns = f"{body.get('org_id')}/{body.get('project_id')}"
        if path == "/api/v2/memories/list":
            items = store.get(ns, [])
            start = body["page_size"] * body["page_num"]
            return httpx.Response(200, json={"content": {"episodic_memory": items[start:start + body["page_size"]]}})
        if path == "/api/v2/projects/episode_count/get":
            return httpx.Response(200, json={"count": len(store.get(ns, []))})
        if path == "/api/v2/memories/episodic/delete":
            gone = set(body["episodic_ids"])
            store[ns] = [e for e in store.get(ns, []) if e["uid"] not in gone]
            return httpx.Response(200, json={})
        if path == "/api/v2/memories/search":
            return httpx.Response(200, json={"content": {"episodic_memory": {
                "long_term_memory": {"episodes": [{**store[ns][0], "score": 0.5}]}}}})
        return httpx.Response(200, json={})

    app = create_app(reg, "http://core.test", transport=httpx.MockTransport(transport),
                     owui_url="http://owui.test")
    return type("W", (), {"reg": reg, "svc": svc, "store": store, "calls": calls,
                          "client": TestClient(app)})


def login(world, email="alice@corp.example", password=None):
    return world.client.post("/portal/api/login", headers=W,
                             json={"email": email, "password": password or PASSWORDS[email]})


class TestSignIn:
    def test_wrong_password_is_refused(self, world):
        assert login(world, password="nope").status_code == 401
        assert world.client.get("/portal/api/me").status_code == 401

    def test_password_goes_to_open_webui_only(self, world):
        login(world)
        signins = [b for p, b in world.calls if p == "/api/v1/auths/signin"]
        assert len(signins) == 1
        assert all("alice-pw" not in json.dumps(b) for p, b in world.calls if p != "/api/v1/auths/signin")

    def test_session_cookie_is_httponly_and_strict(self, world):
        cookie = login(world).headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie

    def test_me_shows_own_namespace(self, world):
        login(world)
        me = world.client.get("/portal/api/me").json()
        assert me["space"]["org_id"] == "alice_corp_example"
        assert {a["detail"] for a in me["access"]} == {"alice-personal"}

    def test_logout_ends_session(self, world):
        login(world)
        world.client.post("/portal/api/logout", headers=W)
        assert world.client.get("/portal/api/me").status_code == 401


class TestOwnMemoryOnly:
    def test_lists_only_own_namespace(self, world):
        login(world)
        text = json.dumps(world.client.get("/portal/api/memories").json())
        assert "Kona" in text and "bob's secret" not in text

    def test_cannot_delete_someone_elses_ids(self, world):
        login(world)
        r = world.client.post("/portal/api/memories/delete", headers=W, json={"ids": ["1", "50"]})
        assert r.json()["deleted"] == 1
        assert [e["uid"] for e in world.store["bob_corp_example/default"]] == ["50"]
        deletes = [b for p, b in world.calls if p == "/api/v2/memories/episodic/delete"]
        assert deletes[-1]["episodic_ids"] == ["1"]
        assert deletes[-1]["org_id"] == "alice_corp_example"

    def test_writes_need_the_portal_header(self, world):
        login(world)
        r = world.client.post("/portal/api/memories/delete", json={"ids": ["1"]})
        assert r.status_code == 403
        assert len(world.store["alice_corp_example/default"]) == 4

    def test_add_is_attributed_to_the_signed_in_person(self, world):
        login(world)
        world.client.post("/portal/api/memories/add", headers=W, json={"text": "I prefer tea"})
        add = [b for p, b in world.calls if p == "/api/v2/memories"][-1]
        assert add["org_id"] == "alice_corp_example"
        assert add["messages"][0]["producer"] == "alice_corp_example"
        assert add["messages"][0]["metadata"]["via"] == "manual"

    def test_clear_removes_only_own(self, world):
        login(world)
        world.client.post("/portal/api/memories/clear", headers=W)
        assert world.store["alice_corp_example/default"] == []
        assert len(world.store["bob_corp_example/default"]) == 1


class TestSwitchAndEnroll:
    def test_switch_off_makes_chat_requests_not_enrolled(self, world):
        login(world)
        world.client.post("/portal/api/switch", headers=W, json={"enabled": False})
        with pytest.raises(Denied) as exc:
            authorize(world.reg, {"authorization": f"Bearer {world.svc}",
                                  "x-mm-key-hash": sha256("sk-alice")})
        assert exc.value.decision == "not-enrolled"
        world.client.post("/portal/api/switch", headers=W, json={"enabled": True})
        authorize(world.reg, {"authorization": f"Bearer {world.svc}",
                              "x-mm-key-hash": sha256("sk-alice")})

    def test_unregistered_person_can_turn_memory_on(self, world):
        login(world, "carol@corp.example")
        assert world.client.get("/portal/api/me").json()["enrolled"] is False
        assert world.client.get("/portal/api/memories").status_code == 409
        world.client.post("/portal/api/enroll", headers=W)
        me = world.client.get("/portal/api/me").json()
        assert me["enrolled"] and me["space"]["org_id"] == "carol_corp_example"


def test_turns_pair_and_flag_automatic():
    turns = _turns([
        ep(1, "user", "I ride a Kona", "a", via="app-token"),
        ep(2, "assistant", "Noted.", "a", via="app-token"),
        ep(3, "user", "### Task: Generate a concise title", "a", at="2026-10-14T11:00:00Z"),
        ep(4, "assistant", "{}", "a", at="2026-10-14T11:00:00Z"),
        ep(5, "user", "a note I added", "a", at="2026-10-14T12:00:00Z", via="manual"),
    ])
    assert [t["ids"] for t in turns] == [["5"], ["3", "4"], ["1", "2"]]
    assert turns[0]["source"] == "Added by you" and turns[0]["reply"] is None
    assert turns[1]["automatic"] == "chat title"
    assert turns[2]["source"] == "Open WebUI" and turns[2]["reply"] == "Noted."
