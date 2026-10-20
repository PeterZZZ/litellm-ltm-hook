# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for key linking (mock_platform/linking.py): a key the platform
has never seen is looked up in LiteLLM on its first call and linked to its
owner; reconcile keeps the registry in step with LiteLLM afterwards.

LiteLLM, core and Open WebUI's sign-in are one httpx.MockTransport.

    uv run --group platform pytest test_linking.py -v
"""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

pytest.importorskip("fastapi", reason="the mock platform needs the 'platform' dependency group")

from fastapi.testclient import TestClient  # noqa: E402

from mock_platform.app import create_app  # noqa: E402
from mock_platform.linking import KeyLinker  # noqa: E402
from mock_platform.registry import Registry, sha256  # noqa: E402

VIEWER = "sk-viewer"
APP_KEY = "sk-open-webui-app"
APP_OWNER = "svc-open-webui"
W = {"X-MM-Portal": "1"}


def h(key):
    return sha256(key)


def soon(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def world(tmp_path):
    reg = Registry(str(tmp_path / "reg.sqlite"))
    service = reg.add_service("litellm-test")
    reg.add_user("root@corp.example", role="admin", employee_id="E10000")
    reg.add_user("alice@corp.example", employee_id="E12345")
    reg.add_user("bob@corp.example", employee_id="E67890")
    reg.add_user("erin@corp.example")  # no employee ID: her email names her
    reg.link_personal_key("alice@corp.example", "sk-alice-old", "alice-old")
    reg.add_app_key(APP_KEY, "open-webui-app", "s" * 40, "open-webui", owner=APP_OWNER)

    # What LiteLLM holds: hash -> /key/info record; user_id -> email.
    litellm = {
        h("sk-alice-old"): {"key_alias": "alice-old", "user_id": "E12345", "team_id": None, "blocked": None, "expires": None},
        h("sk-alice-new"): {"key_alias": "alice-new", "user_id": "E12345", "team_id": None, "blocked": None, "expires": None},
        h("sk-alice-by-email"): {"key_alias": "alice-by-email", "user_id": "alice@corp.example", "team_id": None, "blocked": None, "expires": None},
        h("sk-erin"): {"key_alias": "erin-personal", "user_id": "erin@corp.example", "team_id": None, "blocked": None, "expires": None},
        h("sk-stranger"): {"key_alias": "stranger", "user_id": "E99999", "team_id": None, "blocked": None, "expires": None},
        h("sk-blocked"): {"key_alias": "blocked", "user_id": "E12345", "team_id": None, "blocked": True, "expires": None},
        h("sk-team"): {"key_alias": "team", "user_id": "E12345", "team_id": "t-1", "blocked": None, "expires": None},
        h("sk-expired"): {"key_alias": "expired", "user_id": "E12345", "team_id": None, "blocked": None, "expires": soon(-60)},
        h("sk-fresh"): {"key_alias": "fresh", "user_id": "E12345", "team_id": None, "blocked": None, "expires": soon(3600)},
        h("sk-service"): {"key_alias": "service", "user_id": APP_OWNER, "team_id": None, "blocked": None, "expires": None},
        h(APP_KEY): {"key_alias": "open-webui-app", "user_id": APP_OWNER, "team_id": None, "blocked": None, "expires": None},
    }
    calls = []
    clock = {"now": 1_000_000.0}

    def transport(request: httpx.Request):
        path = request.url.path
        if request.url.host == "litellm.test":
            calls.append((path, dict(request.url.params)))
            if request.headers.get("authorization") != f"Bearer {VIEWER}":
                return httpx.Response(401, json={})
            if path == "/key/info":
                info = litellm.get(request.url.params["key"])
                return (httpx.Response(200, json={"key": request.url.params["key"], "info": info}) if info
                        else httpx.Response(404, json={"error": "Key not found in database"}))
            if path == "/key/list":
                return httpx.Response(200, json={"keys": [{"token": k, **v} for k, v in litellm.items()],
                                                 "total_pages": 1})
            return httpx.Response(404, json={})
        if path == "/api/v1/auths/signin":
            body = json.loads(request.content)
            return httpx.Response(200, json={"email": body["email"], "name": "N", "token": "t"})
        calls.append((path, json.loads(request.content or b"{}")))
        if path == "/api/v2/memories/search":
            return httpx.Response(200, json={"status": 0, "content": {}})
        return httpx.Response(200, json={})

    def client():
        return httpx.AsyncClient(transport=httpx.MockTransport(transport))

    linker = KeyLinker(reg, "http://litellm.test", VIEWER, client, now=lambda: clock["now"])
    app = create_app(reg, "http://core.test", transport=httpx.MockTransport(transport),
                     owui_url="http://owui.test", litellm_url="http://litellm.test",
                     litellm_key=VIEWER, linker=linker, reconcile_seconds=0)
    return type("W", (), {"reg": reg, "service": service, "client": TestClient(app), "calls": calls,
                          "litellm": litellm, "clock": clock, "linker": linker})


def search(world, key, owner, project="default", org="E12345"):
    return world.client.post(
        "/api/v2/memories/search",
        json={"org_id": org, "project_id": project, "query": "x"},
        headers={"Authorization": f"Bearer {world.service}", "X-MemMachine-Key-Id": h(key),
                 "X-MemMachine-Key-Owner": owner, "X-MemMachine-Project": project})


def decision(r):
    return r.headers.get("x-memmachine-decision")


def key_info_calls(world):
    return [c for c in world.calls if c[0] == "/key/info"]


class TestFirstCall:
    def test_known_owner_is_linked_and_served_on_the_first_call(self, world):
        r = search(world, "sk-alice-new", "E12345")
        assert r.status_code == 200
        key = world.reg.key(h("sk-alice-new"))
        assert key.kind == "personal" and key.label == "alice-new"
        assert world.reg.user(key.user_id).email == "alice@corp.example"
        link = next(a for a in world.reg.audit(5) if a["op"] == "link")
        assert link["decision"] == "allow" and link["email"] == "alice@corp.example"
        assert len(key_info_calls(world)) == 1

    def test_second_call_does_not_ask_litellm_again(self, world):
        search(world, "sk-alice-new", "E12345")
        search(world, "sk-alice-new", "E12345")
        assert len(key_info_calls(world)) == 1

    def test_email_names_a_person_without_an_employee_id(self, world):
        r = search(world, "sk-erin", "erin@corp.example", org="erin_corp_example")
        assert r.status_code == 200
        assert world.reg.user(world.reg.key(h("sk-erin")).user_id).email == "erin@corp.example"

    def test_email_of_a_person_known_by_employee_id_is_found_but_not_linked(self, world):
        # The hook would name alice@corp.example; the platform knows E12345 and
        # refuses that pairing, so linking would only move the refusal.
        r = search(world, "sk-alice-by-email", "alice@corp.example", org="alice_corp_example")
        assert r.status_code == 403 and decision(r) == "not-enrolled"
        assert world.reg.key(h("sk-alice-by-email")) is None
        assert "knows that person as 'E12345'" in next(a for a in world.reg.audit(5) if a["op"] == "link")["reason"]

    def test_a_rotated_key_inherits_the_persons_grants(self, world):
        world.reg.grant("alice@corp.example", "research")
        r = search(world, "sk-alice-new", "E12345", project="research")
        assert r.status_code == 200
        assert search(world, "sk-alice-new", "E12345", project="payroll").status_code == 403

    def test_unknown_owner_is_not_enrolled(self, world):
        r = search(world, "sk-stranger", "E99999", org="E99999")
        assert r.status_code == 403 and decision(r) == "not-enrolled"
        assert world.reg.key(h("sk-stranger")) is None
        link = next(a for a in world.reg.audit(5) if a["op"] == "link")
        assert link["decision"] == "not-enrolled" and "not an enrolled person" in link["reason"]

    @pytest.mark.parametrize("key,why", [
        ("sk-blocked", "blocked"), ("sk-team", "team key"), ("sk-expired", "expired"),
    ])
    def test_unusable_keys_are_not_linked(self, world, key, why):
        r = search(world, key, "E12345")
        assert r.status_code == 403 and decision(r) == "not-enrolled"
        assert world.reg.key(h(key)) is None
        assert why in next(a for a in world.reg.audit(5) if a["op"] == "link")["reason"]

    def test_key_with_an_expiry_still_ahead_is_linked(self, world):
        assert search(world, "sk-fresh", "E12345").status_code == 200

    def test_service_users_key_is_not_a_persons(self, world):
        r = search(world, "sk-service", APP_OWNER, org="x")
        assert r.status_code == 403 and decision(r) == "not-enrolled"
        assert world.reg.key(h("sk-service")) is None

    def test_hooks_owner_must_agree_with_litellm(self, world):
        # The hook names Bob; LiteLLM says the key is Alice's. Neither gets it.
        r = search(world, "sk-alice-new", "E67890", org="E67890")
        assert r.status_code == 403 and decision(r) == "not-enrolled"
        assert world.reg.key(h("sk-alice-new")) is None

    def test_key_litellm_does_not_know_is_not_enrolled(self, world):
        r = search(world, "sk-made-up", "E12345")
        assert r.status_code == 403 and decision(r) == "not-enrolled"

    def test_without_a_service_credential_nothing_is_looked_up(self, world):
        r = world.client.post("/api/v2/memories/search", json={"query": "x"},
                              headers={"Authorization": "Bearer mm-svc-guess",
                                       "X-MemMachine-Key-Id": h("sk-alice-new"),
                                       "X-MemMachine-Key-Owner": "E12345"})
        assert r.status_code == 401
        assert key_info_calls(world) == []


class TestMissCache:
    def test_failed_lookup_is_not_repeated_within_60s(self, world):
        for _ in range(3):
            r = search(world, "sk-stranger", "E99999", org="E99999")
            assert decision(r) == "not-enrolled"
        assert len(key_info_calls(world)) == 1
        assert len([a for a in world.reg.audit(20) if a["op"] == "link"]) == 1

    def test_failed_lookup_is_retried_after_60s(self, world):
        search(world, "sk-stranger", "E99999", org="E99999")
        world.clock["now"] += 61
        search(world, "sk-stranger", "E99999", org="E99999")
        assert len(key_info_calls(world)) == 2

    def test_enrolling_the_owner_lets_the_retry_link(self, world):
        search(world, "sk-stranger", "E99999", org="E99999")
        world.reg.add_user("stranger@corp.example", employee_id="E99999")
        assert decision(search(world, "sk-stranger", "E99999", org="E99999")) == "not-enrolled"
        world.clock["now"] += 61
        assert search(world, "sk-stranger", "E99999", org="E99999").status_code == 200


class TestReconcile:
    def run(self, world):
        import asyncio
        return asyncio.run(world.linker.reconcile())

    def test_deleted_key_is_dropped(self, world):
        del world.litellm[h("sk-alice-old")]
        result = self.run(world)
        assert world.reg.key(h("sk-alice-old")) is None
        assert result["unlinked"] == ["alice-old (deleted in LiteLLM)"]
        assert world.reg.revoked(h("sk-alice-old")) is None

    def test_reassigned_key_is_unlinked_and_stays_refused(self, world):
        world.litellm[h("sk-alice-old")]["user_id"] = "E67890"
        # Before reconcile the per-call owner check already refuses it.
        r = search(world, "sk-alice-old", "E67890", org="E67890")
        assert r.status_code == 403 and decision(r) == "key-reassigned"
        self.run(world)
        assert world.reg.key(h("sk-alice-old")) is None
        # After: not linked again to Bob on its next call, even though LiteLLM says it is his.
        r = search(world, "sk-alice-old", "E67890", org="E67890")
        assert r.status_code == 403 and decision(r) == "key-reassigned"
        assert world.reg.key(h("sk-alice-old")) is None
        row = next(a for a in world.reg.audit(20) if a["op"] == "reconcile" and a["key_label"] == "alice-old")
        assert row["decision"] == "key-reassigned" and "owner changed" in row["reason"]

    def test_admin_sync_accepts_the_reassignment(self, world):
        world.litellm[h("sk-alice-old")]["user_id"] = "E67890"
        self.run(world)
        assert world.reg.revoked(h("sk-alice-old"))
        world.reg.link_personal_key("bob@corp.example", "sk-alice-old", "alice-old")  # what Sync does
        assert world.reg.revoked(h("sk-alice-old")) is None
        assert search(world, "sk-alice-old", "E67890", org="E67890").status_code == 200

    def test_a_revoked_key_deleted_in_litellm_is_forgotten(self, world):
        world.litellm[h("sk-alice-old")]["user_id"] = "E67890"
        self.run(world)
        assert world.reg.revoked(h("sk-alice-old"))
        del world.litellm[h("sk-alice-old")]
        self.run(world)
        assert world.reg.revoked(h("sk-alice-old")) is None

    def test_blocked_key_is_dropped_and_linked_again_once_unblocked(self, world):
        world.litellm[h("sk-alice-old")]["blocked"] = True
        self.run(world)
        assert world.reg.key(h("sk-alice-old")) is None and world.reg.revoked(h("sk-alice-old")) is None
        world.litellm[h("sk-alice-old")]["blocked"] = None
        assert search(world, "sk-alice-old", "E12345").status_code == 200

    def test_new_keys_of_known_people_are_linked(self, world):
        result = self.run(world)
        assert set(result["linked"]) == {"alice-new", "erin-personal", "fresh"}
        assert world.reg.key(h("sk-alice-by-email")) is None
        assert world.reg.key(h("sk-stranger")) is None   # unknown owner
        assert world.reg.key(h("sk-service")) is None    # service user
        assert world.reg.key(h("sk-team")) is None

    def test_app_key_reassigned_in_litellm_is_suspended_not_forgotten(self, world):
        world.litellm[h(APP_KEY)]["user_id"] = "E67890"
        result = self.run(world)
        # The registration (secret, issuer, owner) stays; the key is refused.
        assert world.reg.app(h(APP_KEY)).owner == APP_OWNER
        assert result["unlinked"] == ["open-webui-app (suspended: owner changed to 'E67890' in LiteLLM)"]
        assert world.reg.app_keys()[0]["suspended"].startswith("owner changed")
        r = search(world, APP_KEY, "E67890", org="E67890")
        assert r.status_code == 403 and decision(r) == "key-reassigned"
        assert self.run(world)["unlinked"] == []  # said once, not every run

    def test_app_key_serves_again_once_litellm_is_put_right(self, world):
        world.litellm[h(APP_KEY)]["user_id"] = "E67890"
        self.run(world)
        world.litellm[h(APP_KEY)]["user_id"] = APP_OWNER
        # Even before reconcile the per-call check passes nothing: still suspended.
        assert decision(search(world, APP_KEY, APP_OWNER)) == "key-reassigned"
        result = self.run(world)
        assert result["linked"] == ["open-webui-app (suspension lifted)"]
        assert world.reg.app_keys()[0]["suspended"] is None
        # Served again: a token is still required, so the right owner alone is 401, not 403.
        assert search(world, APP_KEY, APP_OWNER).status_code == 401

    def test_app_key_deleted_in_litellm_is_forgotten(self, world):
        del world.litellm[h(APP_KEY)]
        self.run(world)
        assert world.reg.key(h(APP_KEY)) is None and world.reg.app(h(APP_KEY)) is None

    def test_re_registering_an_app_key_lifts_its_suspension(self, world):
        world.litellm[h(APP_KEY)]["user_id"] = "E67890"
        self.run(world)
        world.reg.unlink_key(APP_KEY)
        world.reg.add_app_key(APP_KEY, "open-webui-app", "s" * 40, "open-webui", owner=APP_OWNER)
        assert world.reg.revoked(h(APP_KEY)) is None

    def test_app_key_still_its_service_users_is_kept(self, world):
        self.run(world)
        assert world.reg.app(h(APP_KEY)).owner == APP_OWNER

    def test_reconcile_clears_the_miss_cache(self, world):
        search(world, "sk-stranger", "E99999", org="E99999")
        self.run(world)
        search(world, "sk-stranger", "E99999", org="E99999")
        assert len(key_info_calls(world)) == 2


class TestPortalButton:
    def login(self, world, email):
        return world.client.post("/portal/api/login", headers=W, json={"email": email, "password": "x"})

    def test_admin_can_reconcile_now(self, world):
        del world.litellm[h("sk-alice-old")]
        self.login(world, "root@corp.example")
        r = world.client.post("/portal/api/admin/reconcile", headers=W, json={})
        assert r.status_code == 200
        assert r.json()["unlinked"] == ["alice-old (deleted in LiteLLM)"]
        assert world.reg.audit(1)[0]["op"] == "admin:reconcile"

    def test_users_cannot(self, world):
        self.login(world, "alice@corp.example")
        assert world.client.post("/portal/api/admin/reconcile", headers=W, json={}).status_code == 403


def test_not_configured_means_no_linking(tmp_path):
    reg = Registry(str(tmp_path / "r.sqlite"))
    svc = reg.add_service("s")
    reg.add_user("alice@corp.example", employee_id="E12345")
    app = create_app(reg, "http://core.test", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={})))
    r = TestClient(app).post("/api/v2/memories/search", json={"org_id": "E12345", "project_id": "default"},
                             headers={"Authorization": f"Bearer {svc}", "X-MemMachine-Key-Id": h("sk-x"),
                                      "X-MemMachine-Key-Owner": "E12345", "X-MemMachine-Project": "default"})
    assert r.status_code == 403 and r.headers["x-memmachine-decision"] == "not-enrolled"
