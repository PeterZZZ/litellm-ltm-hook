# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Corp SSO in the portal (mock_platform/sso.py and the portal's
Keycloak sign-in): the authorization-code flow, ID-token checks, and employee IDs
taken from the directory.

Keycloak and MemMachine core are one httpx.MockTransport; ID tokens are signed
with an RSA key made per test run, so nothing touches the network.

    uv run --group platform pytest test_sso.py -v
"""
import json
import time
import urllib.parse

import httpx
import pytest

pytest.importorskip("fastapi", reason="the portal needs the 'platform' dependency group")
jwt = pytest.importorskip("jwt")
rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")

from fastapi.testclient import TestClient  # noqa: E402

from mock_platform.app import create_app  # noqa: E402
from mock_platform.registry import Registry  # noqa: E402
from mock_platform.sso import Keycloak  # noqa: E402

ISSUER = "http://kc.test/realms/corp"
CLIENT = "memmachine-platform"
W = {"X-MM-Portal": "1"}
DIRECTORY = {
    "root@corp.example": {"employee_id": ["E10000"], "firstName": "Root"},
    "alice@corp.example": {"employee_id": ["E12345"], "firstName": "Alice"},
    "dana@corp.example": {"employee_id": ["E24680"], "firstName": "Dana"},
}


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


SIGNING, STRANGER = _key(), _key()


def jwk(private, kid):
    d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    return {**d, "kid": kid, "use": "sig", "alg": "RS256"}


def id_token(email, nonce, *, key=SIGNING, kid="k1", aud=CLIENT, iss=ISSUER, ttl=300, **extra):
    now = int(time.time())
    claims = {"iss": iss, "aud": aud, "sub": f"kc-{email}", "iat": now, "exp": now + ttl,
              "nonce": nonce, "email": email, "email_verified": True, "name": email.split("@")[0],
              "employee_id": (DIRECTORY.get(email, {}).get("employee_id") or [None])[0], **extra}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def world(tmp_path):
    reg = Registry(str(tmp_path / "reg.sqlite"))
    reg.add_user("root@corp.example", role="admin", employee_id="E10000")
    reg.add_user("alice@corp.example")  # enrolled before SSO: no employee ID yet
    codes = {}  # code -> id token the token endpoint will hand back

    def transport(request: httpx.Request):
        url, path = str(request.url), request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
                "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
                "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
                "end_session_endpoint": f"{ISSUER}/protocol/openid-connect/logout",
            })
        if path.endswith("/certs"):
            return httpx.Response(200, json={"keys": [jwk(SIGNING, "k1")]})
        if path.endswith("/token"):
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            if form.get("client_secret") != "kc-secret":
                return httpx.Response(401, json={"error": "unauthorized_client"})
            if form["grant_type"] == "client_credentials":
                return httpx.Response(200, json={"access_token": "sa-token", "expires_in": 300})
            if form["code"] in codes:
                return httpx.Response(200, json={"id_token": codes.pop(form["code"])})
            return httpx.Response(400, json={"error": "invalid_grant"})
        if path == "/admin/realms/corp/users":
            assert request.headers["authorization"] == "Bearer sa-token"
            email = request.url.params["email"]
            if email not in DIRECTORY:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{"email": email, "enabled": True,
                                              "firstName": DIRECTORY[email]["firstName"],
                                              "attributes": {"employee_id": DIRECTORY[email]["employee_id"]}}])
        if url.startswith("http://core.test"):
            if path == "/api/v2/projects/episode_count/get":
                return httpx.Response(200, json={"count": 0})
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})

    mock = httpx.MockTransport(transport)
    sso = Keycloak(ISSUER, CLIENT, "kc-secret", transport=mock)
    app = create_app(reg, "http://core.test", transport=mock, sso=sso)
    return type("W", (), {"reg": reg, "codes": codes, "client": TestClient(app), "sso": sso})


def start(world):
    r = world.client.get("/portal/auth/login", follow_redirects=False)
    assert r.status_code == 302
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(r.headers["location"]).query))
    return q


def sign_in(world, email, **token_kw):
    q = start(world)
    world.codes["c1"] = id_token(email, q["nonce"], **token_kw)
    return world.client.get(f"/portal/auth/callback?state={q['state']}&code=c1")


class TestSignIn:
    def test_config_offers_sso_and_no_password(self, world):
        assert world.client.get("/portal/api/config").json()["sso"] is True
        r = world.client.post("/portal/api/login", headers=W, json={"email": "a", "password": "b"})
        assert r.status_code == 404

    def test_login_redirects_to_keycloak(self, world):
        q = start(world)
        assert q["client_id"] == CLIENT and q["response_type"] == "code"
        assert q["redirect_uri"].endswith("/portal/auth/callback")
        assert q["state"] and q["nonce"] and q["state"] != q["nonce"]

    def test_callback_starts_a_session(self, world):
        r = sign_in(world, "root@corp.example")
        assert r.status_code == 200 and "mm_portal=" in r.headers["set-cookie"]
        me = world.client.get("/portal/api/me").json()
        assert me["email"] == "root@corp.example" and me["employee_id"] == "E10000"
        assert "Corp SSO" in me["signed_in_via"]

    def test_unknown_state_is_refused(self, world):
        start(world)
        r = world.client.get("/portal/auth/callback?state=forged&code=c1")
        assert "signin=failed" in r.text
        assert world.client.get("/portal/api/me").status_code == 401

    def test_state_is_single_use(self, world):
        q = start(world)
        world.codes["c1"] = id_token("root@corp.example", q["nonce"])
        world.client.get(f"/portal/auth/callback?state={q['state']}&code=c1")
        world.client.cookies.clear()
        world.codes["c1"] = id_token("root@corp.example", q["nonce"])
        world.client.get(f"/portal/auth/callback?state={q['state']}&code=c1")
        assert world.client.get("/portal/api/me").status_code == 401

    @pytest.mark.parametrize("bad", [
        {"key": STRANGER},
        {"aud": "open-webui"},
        {"iss": "http://evil.test/realms/corp"},
        {"ttl": -600},
    ], ids=["foreign-key", "other-client", "other-issuer", "expired"])
    def test_bad_id_tokens_are_refused(self, world, bad):
        r = sign_in(world, "root@corp.example", **bad)
        assert "signin=failed" in r.text
        assert world.client.get("/portal/api/me").status_code == 401

    def test_nonce_must_match(self, world):
        q = start(world)
        world.codes["c1"] = id_token("root@corp.example", "another-login")
        world.client.get(f"/portal/auth/callback?state={q['state']}&code=c1")
        assert world.client.get("/portal/api/me").status_code == 401

    def test_logout_ends_the_keycloak_session_too(self, world):
        sign_in(world, "root@corp.example")
        out = world.client.post("/portal/api/logout", headers=W, json={}).json()
        assert out["logout_url"].startswith(f"{ISSUER}/protocol/openid-connect/logout?")
        assert world.client.get("/portal/api/me").status_code == 401


class TestDirectory:
    def test_create_takes_the_employee_id_from_sso(self, world):
        sign_in(world, "root@corp.example")
        r = world.client.post("/portal/api/admin/users", headers=W, json={"email": "dana@corp.example"})
        assert r.status_code == 200
        dana = world.reg.user_by_email("dana@corp.example")
        assert dana.employee_id == "E24680" and dana.org_id == "E24680"

    def test_create_refuses_someone_sso_does_not_know(self, world):
        sign_in(world, "root@corp.example")
        r = world.client.post("/portal/api/admin/users", headers=W, json={"email": "ghost@corp.example"})
        assert r.status_code == 404
        assert world.reg.user_by_email("ghost@corp.example") is None

    def test_sync_fills_in_the_employee_id(self, world):
        sign_in(world, "root@corp.example")
        assert world.reg.user_by_email("alice@corp.example").org_id == "alice_corp_example"
        r = world.client.post("/portal/api/admin/users/sync", headers=W,
                              json={"email": "alice@corp.example"})
        assert r.json()["employee_id"] == {"from": None, "to": "E12345"}
        alice = world.reg.user_by_email("alice@corp.example")
        assert (alice.employee_id, alice.org_id) == ("E12345", "E12345")
