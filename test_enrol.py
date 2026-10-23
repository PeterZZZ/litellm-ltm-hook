# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for enrolment on first use (mock_platform/enrol.py): a person Corp
SSO knows is created on their first call, from Open WebUI's signed token, from
a personal key's LiteLLM owner, or at portal sign-in; everyone else stays
not-enrolled.

Keycloak's directory, LiteLLM and core are one httpx.MockTransport.

    uv run --group platform pytest test_enrol.py -v
"""
import json
import time

import httpx
import pytest

pytest.importorskip("fastapi", reason="the mock platform needs the 'platform' dependency group")
jwt = pytest.importorskip("jwt")

from fastapi.testclient import TestClient  # noqa: E402

from mock_platform.app import create_app  # noqa: E402
from mock_platform.enrol import Enroller  # noqa: E402
from mock_platform.registry import Registry, sha256  # noqa: E402
from mock_platform.sso import Keycloak  # noqa: E402

ISSUER = "http://kc.test/realms/corp"
SECRET = "owui-signing-secret-for-tests-0123456789"
APP_KEY = "sk-open-webui-app"
VIEWER = "sk-viewer"
DIRECTORY = {
    "alice@corp.example": ("E12345", True),
    "dana@corp.example": ("E24680", True),
    "gone@corp.example": ("E31415", False),  # disabled in Keycloak
}


def token(email, name=None, ttl=300):
    now = int(time.time())
    return jwt.encode({"sub": "u", "email": email, "name": name if name is not None else DIRECTORY.get(email, ("",))[0],
                       "iss": "open-webui", "iat": now, "exp": now + ttl}, SECRET, algorithm="HS256")


@pytest.fixture
def world(tmp_path):
    reg = Registry(str(tmp_path / "reg.sqlite"))
    service = reg.add_service("litellm-test")
    reg.add_user("root@corp.example", role="admin", employee_id="E10000")
    reg.add_user("alice@corp.example", employee_id="E12345")
    reg.add_app_key(APP_KEY, "open-webui-app", SECRET, "open-webui", owner="svc-owui")
    litellm = {sha256("sk-dana"): {"key_alias": "dana-personal", "user_id": "E24680", "team_id": None,
                                   "blocked": None, "expires": None}}
    calls = []
    clock = {"now": 1_000_000.0}

    def transport(request: httpx.Request):
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "token_endpoint": f"{ISSUER}/token",
                                             "authorization_endpoint": f"{ISSUER}/auth",
                                             "jwks_uri": f"{ISSUER}/certs"})
        if path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "sa-token", "expires_in": 300})
        if path == "/admin/realms/corp/users":
            calls.append(("directory", dict(request.url.params)))
            assert request.headers["authorization"] == "Bearer sa-token"
            q = request.url.params
            hits = []
            for email, (emp, enabled) in DIRECTORY.items():
                if q.get("email", "").lower() == email or q.get("q") == f"employee_id:{emp}":
                    hits.append({"email": email, "enabled": enabled, "firstName": email.split("@")[0],
                                 "attributes": {"employee_id": [emp]}})
            return httpx.Response(200, json=hits)
        if request.url.host == "litellm.test":
            calls.append(("litellm", path))
            if path == "/key/info":
                info = litellm.get(request.url.params["key"])
                return httpx.Response(200, json={"info": info}) if info else httpx.Response(404, json={})
            if path == "/key/list":
                return httpx.Response(200, json={"keys": [{"token": k, **v} for k, v in litellm.items()], "total_pages": 1})
            return httpx.Response(404, json={})
        calls.append(("core", path))
        if path == "/api/v2/memories/search":
            return httpx.Response(200, json={"status": 0, "content": {}})
        if path == "/api/v2/projects/episode_count/get":
            return httpx.Response(200, json={"count": 0})
        return httpx.Response(200, json={})

    mock = httpx.MockTransport(transport)
    sso = Keycloak(ISSUER, "memmachine-platform", "kc-secret", transport=mock)

    def client():
        return httpx.AsyncClient(transport=mock)

    def build(auto_enrol=True):
        enroller = Enroller(reg, sso, enabled=auto_enrol, now=lambda: clock["now"])
        app = create_app(reg, "http://core.test", transport=mock, litellm_url="http://litellm.test",
                         litellm_key=VIEWER, sso=sso, reconcile_seconds=0, auto_enrol=auto_enrol,
                         enroller=enroller)
        return TestClient(app)

    return type("W", (), {"reg": reg, "service": service, "client": build(), "build": build,
                          "calls": calls, "clock": clock})


def as_owui(world, email, name=None, org=None, project="default"):
    emp = DIRECTORY.get(email, (None,))[0]
    return world.client.post(
        "/api/v2/memories/search",
        json={"org_id": org or emp or "x", "project_id": project, "query": "x"},
        headers={"Authorization": f"Bearer {world.service}", "X-MemMachine-Key-Id": sha256(APP_KEY),
                 "X-MemMachine-Key-Owner": "svc-owui", "X-MemMachine-User": name or emp or email,
                 "X-MemMachine-User-Assertion": token(email, name), "X-MemMachine-Project": project})


def with_key(world, key, owner, org):
    return world.client.post(
        "/api/v2/memories/search",
        json={"org_id": org, "project_id": "default", "query": "x"},
        headers={"Authorization": f"Bearer {world.service}", "X-MemMachine-Key-Id": sha256(key),
                 "X-MemMachine-Key-Owner": owner, "X-MemMachine-Project": "default"})


def decision(r):
    return r.headers.get("x-memmachine-decision")


def directory_calls(world):
    return [c for c in world.calls if c[0] == "directory"]


class TestOpenWebUI:
    def test_person_sso_knows_is_enrolled_and_served_on_the_first_chat(self, world):
        r = as_owui(world, "dana@corp.example")
        assert r.status_code == 200
        dana = world.reg.user_by_email("dana@corp.example")
        assert dana.employee_id == "E24680" and dana.org_id == "E24680"
        assert dana.grants == ("default",) and dana.enrolled_by == "openwebui" and dana.enabled
        row = next(a for a in world.reg.audit(10) if a["op"] == "enrol")
        assert row["decision"] == "allow" and row["email"] == "dana@corp.example"
        assert "E24680" in row["reason"] and row["namespace"] == "E24680/default"
        assert len(directory_calls(world)) == 1

    def test_second_chat_does_not_ask_the_directory(self, world):
        as_owui(world, "dana@corp.example")
        as_owui(world, "dana@corp.example")
        assert len(directory_calls(world)) == 1

    def test_stranger_to_sso_stays_not_enrolled(self, world):
        r = as_owui(world, "ghost@corp.example", name="E99999", org="E99999")
        assert r.status_code == 403 and decision(r) == "not-enrolled"
        assert world.reg.user_by_email("ghost@corp.example") is None
        row = next(a for a in world.reg.audit(10) if a["op"] == "enrol")
        assert row["decision"] == "not-enrolled" and "not in the Corp SSO directory" in row["reason"]

    def test_failed_lookup_is_cached_for_60s(self, world):
        for _ in range(3):
            as_owui(world, "ghost@corp.example", name="E99999", org="E99999")
        assert len(directory_calls(world)) == 1
        world.clock["now"] += 61
        as_owui(world, "ghost@corp.example", name="E99999", org="E99999")
        assert len(directory_calls(world)) == 2

    def test_disabled_in_sso_is_not_enrolled(self, world):
        r = as_owui(world, "gone@corp.example")
        assert r.status_code == 403 and decision(r) == "not-enrolled"
        assert world.reg.user_by_email("gone@corp.example") is None

    def test_forged_token_still_enrols_nobody(self, world):
        bad = jwt.encode({"email": "dana@corp.example", "name": "E24680", "iss": "open-webui",
                          "exp": int(time.time()) + 300}, "wrong-secret-0123456789abcdef", algorithm="HS256")
        r = world.client.post("/api/v2/memories/search", json={"org_id": "E24680", "project_id": "default", "query": "x"},
                              headers={"Authorization": f"Bearer {world.service}", "X-MemMachine-Key-Id": sha256(APP_KEY),
                                       "X-MemMachine-Key-Owner": "svc-owui", "X-MemMachine-User": "E24680",
                                       "X-MemMachine-User-Assertion": bad, "X-MemMachine-Project": "default"})
        assert r.status_code == 401
        assert world.reg.user_by_email("dana@corp.example") is None and directory_calls(world) == []

    def test_the_rest_of_authorize_still_applies_after_enrolment(self, world):
        # Enrolled on the spot, but naming the wrong person in X-MemMachine-User is still refused.
        r = as_owui(world, "dana@corp.example", name="E12345", org="E24680")
        assert r.status_code == 403 and decision(r) == "refused"
        assert world.reg.user_by_email("dana@corp.example") is not None

    def test_switched_off_means_admin_only(self, world):
        world.client = world.build(auto_enrol=False)
        r = as_owui(world, "dana@corp.example")
        assert r.status_code == 403 and decision(r) == "not-enrolled"
        assert world.reg.user_by_email("dana@corp.example") is None and directory_calls(world) == []


class TestPersonalKey:
    def test_key_of_a_person_sso_knows_enrols_and_links_them(self, world):
        r = with_key(world, "sk-dana", "E24680", "E24680")
        assert r.status_code == 200
        dana = world.reg.user_by_email("dana@corp.example")
        assert dana.enrolled_by == "personal-key" and dana.employee_id == "E24680"
        assert world.reg.key(sha256("sk-dana")).user_id == dana.user_id
        assert [c for c in world.calls if c[0] == "directory"][0][1] == {"q": "employee_id:E24680", "exact": "true"}

    def test_key_of_nobody_sso_knows_is_not_enrolled(self, world):
        world.reg  # noqa: B018
        r = with_key(world, "sk-dana", "E99999", "E99999")  # the hook names an owner LiteLLM does not
        assert r.status_code == 403 and decision(r) == "not-enrolled"
        assert world.reg.user_by_email("dana@corp.example") is None


class TestPortal:
    def test_admin_list_shows_who_enrolled_themselves(self, world):
        as_owui(world, "dana@corp.example")
        with_key(world, "sk-dana", "E24680", "E24680")  # already enrolled: just links the key
        accounts = {u.email: u for u in world.reg.users()}
        assert accounts["dana@corp.example"].enrolled_by == "openwebui"
        assert accounts["alice@corp.example"].enrolled_by is None
        assert world.reg.dump()["users"][1]["enrolled_by"] == "openwebui"


def test_enroller_without_a_directory_enrols_nobody(tmp_path):
    reg = Registry(str(tmp_path / "r.sqlite"))
    e = Enroller(reg, None)
    assert not e.configured
