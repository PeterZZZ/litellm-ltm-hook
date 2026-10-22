# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the platform portal (mock_platform/portal.py).

Open WebUI's sign-in, LiteLLM's read-only user/key API and MemMachine core are
one httpx.MockTransport, so nothing here touches the network.

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
             "carol@corp.example": "carol-pw", "root@corp.example": "root-pw",
             "dana@corp.example": "dana-pw"}
VIEWER = "sk-viewer"
W = {"X-MM-Portal": "1"}
LITELLM_USERS = [
    {"user_id": "dana", "user_email": "dana@corp.example"},
    {"user_id": "alice", "user_email": "alice@corp.example"},
]
LITELLM_KEYS = {
    "dana": [{"token": sha256("sk-dana"), "key_alias": "dana-personal", "team_id": None},
             {"token": sha256("sk-team"), "key_alias": "team-key", "team_id": "t1"}],
    "alice": [{"token": sha256("sk-alice"), "key_alias": "alice-personal", "team_id": None}],
}


def ep(uid, role, text, ns, at="2026-10-14T10:00:00.000000Z", via=None):
    return {"uid": str(uid), "content": text, "producer_role": role, "created_at": at,
            "session_key": ns, "metadata": {"via": via} if via else {}}


@pytest.fixture
def world(tmp_path):
    reg = Registry(str(tmp_path / "reg.sqlite"))
    reg.add_user("root@corp.example", role="admin")
    reg.add_user("alice@corp.example")
    reg.add_user("bob@corp.example")
    svc = reg.add_service("litellm-test")
    reg.link_personal_key("alice@corp.example", "sk-alice", "alice-personal")
    reg.add_app_key("sk-app", "open-webui-app", "s" * 40, "open-webui")
    store = {
        "alice_corp_example/default": [
            ep(1, "user", "I ride a Kona bike", "a", via="app-token"),
            ep(2, "assistant", "Noted, a Kona.", "a", via="app-token"),
        ],
        "bob_corp_example/default": [ep(50, "user", "bob's secret", "b")],
        "dana_corp_example/default": [ep(70, "user", "dana's note", "d")],
    }
    calls = []

    def transport(request: httpx.Request):
        path = request.url.path
        if request.method == "GET":
            calls.append((path, dict(request.url.params), request.headers.get("authorization")))
            if request.headers.get("authorization") != f"Bearer {VIEWER}":
                return httpx.Response(401, json={})
            if path == "/user/list":
                return httpx.Response(200, json={"users": LITELLM_USERS, "total_pages": 1})
            if path == "/user/info":
                return httpx.Response(200, json={"keys": LITELLM_KEYS.get(request.url.params["user_id"], [])})
            return httpx.Response(404, json={})
        body = json.loads(request.content or b"{}")
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
        return httpx.Response(200, json={})

    app = create_app(reg, "http://core.test", transport=httpx.MockTransport(transport),
                     owui_url="http://owui.test", litellm_url="http://litellm.test",
                     litellm_key=VIEWER)
    return type("W", (), {"reg": reg, "svc": svc, "store": store, "calls": calls,
                          "client": TestClient(app)})


def login(world, email="alice@corp.example", password=None):
    return world.client.post("/portal/api/login", headers=W,
                             json={"email": email, "password": password or PASSWORDS[email]})


def evidence(world, key, token=None, user=None):
    h = {"authorization": f"Bearer {world.svc}", "x-memmachine-key-id": sha256(key),
         "x-memmachine-project": "default"}
    if token:
        h["x-memmachine-user-assertion"] = token
    if user:
        h["x-memmachine-user"] = user
    return h


ALICE_SEARCH = {"query": "x", "org_id": "alice_corp_example", "project_id": "default"}


class TestSignIn:
    def test_wrong_password_is_refused(self, world):
        assert login(world, password="nope").status_code == 401
        assert world.client.get("/portal/api/me").status_code == 401

    def test_password_goes_to_open_webui_only(self, world):
        login(world)
        assert all("alice-pw" not in json.dumps(c) for c in world.calls if c[0] != "/api/v1/auths/signin")

    def test_session_cookie_is_httponly_and_strict(self, world):
        cookie = login(world).headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie

    def test_me_shows_identity_namespace_and_ways_in(self, world):
        login(world)
        me = world.client.get("/portal/api/me").json()
        assert me["role"] == "user"
        assert me["namespaces"][0]["org_id"] == "alice_corp_example"
        assert me["grants"] == ["default"]
        assert {a["label"] for a in me["access"]} == {"Open WebUI", "alice-personal"}

    def test_no_account_names_the_admins(self, world):
        login(world, "carol@corp.example")
        me = world.client.get("/portal/api/me").json()
        assert me["enrolled"] is False and me["admins"] == ["root@corp.example"]
        assert world.client.get("/portal/api/memories").status_code == 409


class TestGrantedProjects:
    def test_each_granted_project_is_a_namespace_row(self, world):
        world.reg.grant("alice@corp.example", "research")
        world.store["alice_corp_example/research"] = [ep(9, "user", "research note", "r")]
        login(world)
        me = world.client.get("/portal/api/me").json()
        assert [(n["project_id"], n["episodes"]) for n in me["namespaces"]] == [("default", 2), ("research", 1)]

    def test_star_is_not_listed(self, world):
        world.reg.grant("alice@corp.example", "*")
        login(world)
        assert [n["project_id"] for n in world.client.get("/portal/api/me").json()["namespaces"]] == ["default"]

    def test_export_and_clear_work_per_project(self, world):
        world.reg.grant("alice@corp.example", "research")
        world.store["alice_corp_example/research"] = [ep(9, "user", "research note", "r")]
        login(world)
        exported = world.client.get("/portal/api/export?project=research").json()
        assert exported["namespace"] == "alice_corp_example/research"
        assert [e["text"] for e in exported["episodes"]] == ["research note"]
        r = world.client.post("/portal/api/memories/clear", headers=W, json={"project": "research"})
        assert r.json()["deleted"] == 1
        assert world.store["alice_corp_example/research"] == []
        assert len(world.store["alice_corp_example/default"]) == 2  # untouched

    def test_an_ungranted_project_is_refused(self, world):
        login(world)
        assert world.client.get("/portal/api/export?project=research").status_code == 403
        assert world.client.get("/portal/api/memories?project=research").status_code == 403
        r = world.client.post("/portal/api/memories/clear", headers=W, json={"project": "research"})
        assert r.status_code == 403


class TestGrantEditing:
    def test_person_adds_and_removes_own_project(self, world):
        login(world)
        r = world.client.post("/portal/api/projects", headers=W, json={"project": "research"})
        assert r.status_code == 200 and r.json()["grants"] == ["default", "research"]
        assert world.reg.user_by_email("alice@corp.example").allows("research")
        last = world.reg.audit(1)[0]
        assert last["op"] == "project:add" and last["namespace"] == "alice_corp_example/research"
        r = world.client.post("/portal/api/projects/remove", headers=W, json={"project": "research"})
        assert r.json()["grants"] == ["default"]

    def test_default_cannot_be_removed_and_star_is_admins(self, world):
        login(world)
        assert world.client.post("/portal/api/projects/remove", headers=W, json={"project": "default"}).status_code == 400
        assert world.client.post("/portal/api/projects", headers=W, json={"project": "*"}).status_code == 400
        assert world.client.post("/portal/api/projects", headers=W, json={"project": "pay roll"}).status_code == 400
        assert world.reg.user_by_email("alice@corp.example").grants == ("default",)

    def test_admin_grants_and_removes_for_anyone(self, world):
        login(world, "root@corp.example")
        r = world.client.post("/portal/api/admin/users/grant", headers=W,
                              json={"email": "bob@corp.example", "project": "*"})
        assert r.status_code == 200 and r.json()["account"]["grants"] == ["*", "default"]
        assert world.reg.audit(1)[0]["op"] == "admin:grant"
        r = world.client.post("/portal/api/admin/users/grant", headers=W,
                              json={"email": "bob@corp.example", "project": "*", "remove": True})
        assert r.json()["account"]["grants"] == ["default"]
        assert world.client.post("/portal/api/admin/users/grant", headers=W,
                                 json={"email": "nobody@corp.example", "project": "x"}).status_code == 404

    def test_non_admin_cannot_use_the_admin_route(self, world):
        login(world)
        r = world.client.post("/portal/api/admin/users/grant", headers=W,
                              json={"email": "bob@corp.example", "project": "research"})
        assert r.status_code == 403
        assert world.reg.user_by_email("bob@corp.example").grants == ("default",)


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

    def test_writes_need_the_portal_header(self, world):
        login(world)
        assert world.client.post("/portal/api/memories/delete", json={"ids": ["1"]}).status_code == 403


class TestWaysIn:
    def test_switching_off_open_webui_stops_the_app_path_only(self, world, monkeypatch):
        import mock_platform.authorize as az
        monkeypatch.setattr(az, "_verify_token", lambda *a, **k: {"email": "alice@corp.example"})
        login(world)
        world.client.post("/portal/api/access", headers=W, json={"id": "openwebui", "enabled": False})
        with pytest.raises(Denied) as exc:
            authorize(world.reg, evidence(world, "sk-app", token="t", user="alice@corp.example"))
        assert exc.value.decision == "not-enrolled"
        assert authorize(world.reg, evidence(world, "sk-alice")).user.email == "alice@corp.example"

    def test_switching_off_a_key_stops_that_key_only(self, world):
        login(world)
        key_id = sha256("sk-alice")[:16]
        world.client.post("/portal/api/access", headers=W, json={"id": key_id, "enabled": False})
        with pytest.raises(Denied) as exc:
            authorize(world.reg, evidence(world, "sk-alice"))
        assert exc.value.decision == "not-enrolled" and "switched off" in exc.value.reason

    def test_cannot_switch_someone_elses_key(self, world):
        world.reg.add_user("dana@corp.example")
        world.reg.link_personal_key("dana@corp.example", "sk-dana", "dana-personal")
        login(world)
        r = world.client.post("/portal/api/access", headers=W,
                              json={"id": sha256("sk-dana")[:16], "enabled": False})
        assert r.status_code == 404
        assert world.reg.key(sha256("sk-dana")).enabled

    def test_memory_switch(self, world):
        login(world)
        world.client.post("/portal/api/switch", headers=W, json={"enabled": False})
        with pytest.raises(Denied):
            authorize(world.reg, evidence(world, "sk-alice"))


class TestAdmin:
    def test_non_admin_is_refused(self, world):
        login(world)
        assert world.client.get("/portal/api/admin/users").status_code == 403
        assert world.client.post("/portal/api/admin/users", headers=W,
                                  json={"email": "x@corp.example"}).status_code == 403

    def test_open_webui_role_grants_nothing(self, world):
        # Only the registry's role counts; nothing from Open WebUI is consulted.
        login(world)
        assert world.client.get("/portal/api/admin/audit").status_code == 403

    def test_create_finds_personal_keys_in_litellm(self, world):
        login(world, "root@corp.example")
        r = world.client.post("/portal/api/admin/users", headers=W,
                              json={"email": "dana@corp.example", "role": "user"})
        assert r.status_code == 200
        assert r.json()["keys"]["linked"] == ["dana-personal"]   # team key left out
        assert authorize(world.reg, evidence(world, "sk-dana")).user.email == "dana@corp.example"
        assert all(c[2] == f"Bearer {VIEWER}" for c in world.calls if len(c) == 3)

    def test_create_refuses_duplicates_and_bad_email(self, world):
        login(world, "root@corp.example")
        assert world.client.post("/portal/api/admin/users", headers=W,
                                  json={"email": "alice@corp.example"}).status_code == 409
        assert world.client.post("/portal/api/admin/users", headers=W,
                                  json={"email": "not-an-email"}).status_code == 400

    def test_delete_removes_access_and_optionally_memory(self, world):
        login(world, "root@corp.example")
        r = world.client.post("/portal/api/admin/users/delete", headers=W,
                              json={"email": "alice@corp.example", "delete_memories": True})
        assert r.json()["memories_deleted"] == 2
        assert world.reg.user_by_email("alice@corp.example") is None
        with pytest.raises(Denied) as exc:
            authorize(world.reg, evidence(world, "sk-alice"))
        assert exc.value.decision == "not-enrolled"

    def test_delete_keeps_memory_unless_asked(self, world):
        login(world, "root@corp.example")
        world.client.post("/portal/api/admin/users/delete", headers=W, json={"email": "bob@corp.example"})
        assert len(world.store["bob_corp_example/default"]) == 1

    def test_admin_cannot_delete_or_demote_self(self, world):
        login(world, "root@corp.example")
        assert world.client.post("/portal/api/admin/users/delete", headers=W,
                                  json={"email": "root@corp.example"}).status_code == 400
        assert world.client.post("/portal/api/admin/users/update", headers=W,
                                  json={"email": "root@corp.example", "role": "user"}).status_code == 400

    def test_update_role_and_memory(self, world):
        login(world, "root@corp.example")
        world.client.post("/portal/api/admin/users/update", headers=W,
                          json={"email": "bob@corp.example", "role": "admin", "enabled": False})
        bob = world.reg.user_by_email("bob@corp.example")
        assert bob.is_admin and not bob.enabled

    def test_audit_records_decisions_and_admin_actions(self, world):
        world.client.post("/api/v2/memories/search", json=ALICE_SEARCH,
                          headers=evidence(world, "sk-alice"))
        world.client.post("/api/v2/memories/search", json=ALICE_SEARCH,
                          headers={**evidence(world, "sk-alice"), "authorization": "Bearer nope"})
        login(world, "root@corp.example")
        world.client.post("/portal/api/admin/users/update", headers=W,
                          json={"email": "bob@corp.example", "enabled": False})
        items = world.client.get("/portal/api/admin/audit").json()["items"]
        ops = [(i["op"], i["decision"]) for i in items]
        assert ("search", "allow") in ops and ("search", "refused") in ops
        assert ("admin:update", "allow") in ops
        allowed = next(i for i in items if i["op"] == "search" and i["decision"] == "allow")
        assert allowed["email"] == "alice@corp.example" and allowed["via"] == "personal-key"

    def test_history_shows_only_own_decisions(self, world):
        world.client.post("/api/v2/memories/search", json=ALICE_SEARCH, headers=evidence(world, "sk-alice"))
        world.reg.record(op="search", decision="allow", status=200, email="bob@corp.example")
        login(world)
        items = world.client.get("/portal/api/history").json()["items"]
        assert items and all(i["email"] == "alice@corp.example" for i in items)


def test_turns_pair_and_flag_automatic():
    turns = _turns([
        ep(1, "user", "I ride a Kona", "a", via="app-token"),
        ep(2, "assistant", "Noted.", "a", via="app-token"),
        ep(3, "user", "### Task: Generate a concise title", "a", at="2026-10-14T11:00:00Z"),
        ep(4, "assistant", "{}", "a", at="2026-10-14T11:00:00Z"),
        ep(5, "user", "a note I added", "a", at="2026-10-14T12:00:00Z", via="manual"),
    ])
    assert [t["ids"] for t in turns] == [["5"], ["3", "4"], ["1", "2"]]
    assert turns[1]["automatic"] == "chat title"
    assert turns[2]["source"] == "Open WebUI"
