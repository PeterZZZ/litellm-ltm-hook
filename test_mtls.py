# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""Mutual TLS between the hook and the mock platform, over real sockets.

The platform runs both listeners (serve.py) on 127.0.0.1 in a background thread;
core is an httpx.MockTransport. The hook's own search path is the client.

    uv run --group platform pytest test_mtls.py -v
"""
import asyncio
import os
import stat
import threading
import time

os.environ.setdefault("MEMMACHINE_BASE_URL", "http://platform.unit.test")
os.environ.setdefault("MEMMACHINE_API_KEY", "svc-litellm")

import httpx
import pytest

pytest.importorskip("fastapi", reason="the mock platform needs the 'platform' dependency group")
pytest.importorskip("cryptography", reason="certs.py needs the 'platform' dependency group")
import uvicorn  # noqa: E402

import memmachine_hook as h  # noqa: E402
from mock_platform import certs, serve  # noqa: E402
from mock_platform.app import create_app  # noqa: E402
from mock_platform.registry import Registry, sha256  # noqa: E402

ALICE_KEY = "sk-alice-personal"
ALICE = "E12345"
EMPTY_SEARCH = {"status": 0, "content": {"episodic_memory": {"long_term_memory": {"episodes": []}}}}


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("pki")
    certs.make_ca(d)
    certs.issue_server(d, ["127.0.0.1", "localhost"])
    certs.issue_client(d, "litellm-main")
    certs.issue_client(d, "litellm-other")
    # A LiteLLM whose certificate comes from somewhere else.
    stranger = d / "stranger"
    certs.make_ca(stranger)
    certs.issue_client(stranger, "litellm-main")
    return d


@pytest.fixture(scope="module")
def platform(pki, tmp_path_factory):
    reg = Registry(str(tmp_path_factory.mktemp("reg") / "reg.sqlite"))
    credential = reg.add_service("litellm-main")
    other_credential = reg.add_service("litellm-other")
    reg.add_user("alice@corp.example", employee_id=ALICE)
    reg.link_personal_key("alice@corp.example", ALICE_KEY, "alice-personal")

    def core(request: httpx.Request):
        return httpx.Response(200, json=EMPTY_SEARCH)

    app = create_app(reg, "http://core.test", transport=httpx.MockTransport(core), mtls=True)
    plain = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    mtls = uvicorn.Server(serve.mtls_config(app, "127.0.0.1", 0, str(pki / "platform.crt"),
                                            str(pki / "platform.key"), str(pki / "ca.crt"),
                                            log_level="warning"))
    thread = threading.Thread(target=asyncio.run, args=(serve.serve(plain, mtls),), daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not (plain.started and mtls.started):
        assert time.time() < deadline, "platform did not start"
        time.sleep(0.05)
    port = lambda s: s.servers[0].sockets[0].getsockname()[1]  # noqa: E731
    yield type("Platform", (), {
        "plain": f"http://127.0.0.1:{port(plain)}", "mtls": f"https://127.0.0.1:{port(mtls)}",
        "credential": credential, "other_credential": other_credential, "reg": reg,
    })
    plain.should_exit = True
    thread.join(10)


def tls(pki, client="litellm-main"):
    if client is None:
        return h.platform_tls(str(pki / "ca.crt"))
    base = pki / "stranger" if client == "stranger" else pki
    name = "litellm-main" if client == "stranger" else client
    return h.platform_tls(str(pki / "ca.crt"), str(base / f"{name}.crt"), str(base / f"{name}.key"))


def search(base, verify, credential):
    body = {"org_id": ALICE, "project_id": "default", "query": "cats", "top_k": 5, "types": ["episodic"]}
    headers = {"Authorization": f"Bearer {credential}", "X-MemMachine-Key-Id": sha256(ALICE_KEY),
               "X-MemMachine-Key-Owner": ALICE, "X-MemMachine-Project": "default"}
    with httpx.Client(verify=verify, timeout=5) as c:
        return c.post(f"{base}/api/v2/memories/search", json=body, headers=headers)


# ---------- certs.py ----------

def test_keys_are_private_and_nothing_is_overwritten(pki):
    assert stat.S_IMODE((pki / "ca.key").stat().st_mode) == 0o600
    assert stat.S_IMODE((pki / "litellm-main.key").stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        certs.issue_client(pki, "litellm-main")


def test_a_client_certificate_names_its_service(pki):
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    cert = x509.load_pem_x509_certificate((pki / "litellm-main.crt").read_bytes())
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "litellm-main"
    assert ExtendedKeyUsageOID.CLIENT_AUTH in cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value


def litellm_csr(directory, label, extra_name=None):
    """What a LiteLLM's operator does on its own host: a key that stays there, and
    a signing request for the platform's CA."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, label)]
    if extra_name:
        attrs.append(x509.NameAttribute(NameOID.COMMON_NAME, extra_name))
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name(attrs)).sign(key, hashes.SHA256())
    (directory / f"{label}.key").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return csr.public_bytes(serialization.Encoding.PEM)


def test_a_signed_request_gets_in_and_its_key_never_left_the_litellm(pki, platform, tmp_path):
    litellm_host, ca_host = tmp_path / "litellm", tmp_path / "issued"
    litellm_host.mkdir()
    csr = litellm_csr(litellm_host, "litellm-main")
    crt = certs.sign_client(pki, csr, platform.reg.services(), ca_host)
    assert [f.name for f in ca_host.iterdir()] == ["litellm-main.crt"]
    verify = h.platform_tls(str(pki / "ca.crt"), str(crt), str(litellm_host / "litellm-main.key"))
    assert search(platform.mtls, verify, platform.credential).status_code == 200


@pytest.mark.parametrize("label, extra, why", [("litellm-unknown", None, "not a service in the registry"),
                                               ("litellm-main", "litellm-other", "exactly one common name")])
def test_the_ca_signs_only_a_registered_service_name(pki, platform, tmp_path, label, extra, why):
    csr = litellm_csr(tmp_path, label, extra)
    with pytest.raises(ValueError, match=why):
        certs.sign_client(pki, csr, platform.reg.services(), tmp_path / "issued")
    assert not (tmp_path / "issued").exists()


# ---------- the platform ----------

def test_the_litellm_with_its_own_certificate_gets_in(pki, platform):
    resp = search(platform.mtls, tls(pki), platform.credential)
    assert resp.status_code == 200, resp.text


def test_a_copied_credential_without_its_litellms_certificate_is_refused(pki, platform):
    # litellm-other's certificate, litellm-main's credential.
    resp = search(platform.mtls, tls(pki, "litellm-other"), platform.credential)
    assert resp.status_code == 401
    assert "not this credential's service" in resp.json()["detail"]


def test_the_plain_listener_does_not_serve_the_api(platform):
    resp = search(platform.plain, True, platform.credential)
    assert resp.status_code == 401 and resp.json()["detail"] == "no client certificate"


def test_the_plain_listener_still_serves_browsers(platform):
    assert httpx.get(f"{platform.plain}/healthz").json() == {"ok": True}


def test_no_client_certificate_no_connection(pki, platform):
    with pytest.raises(httpx.TransportError):
        search(platform.mtls, tls(pki, None), platform.credential)


def test_a_certificate_from_another_ca_no_connection(pki, platform):
    with pytest.raises(httpx.TransportError):
        search(platform.mtls, tls(pki, "stranger"), platform.credential)


def test_the_hook_does_not_talk_to_a_platform_it_cannot_verify(pki, platform):
    # The system's CAs do not know the mock CA.
    stranger_ca = h.platform_tls(str(pki / "stranger" / "ca.crt"), str(pki / "litellm-main.crt"),
                                 str(pki / "litellm-main.key"))
    with pytest.raises(httpx.TransportError):
        search(platform.mtls, stranger_ca, platform.credential)


# ---------- the hook ----------

def test_the_hooks_search_goes_over_mutual_tls(pki, platform, tmp_path, monkeypatch):
    monkeypatch.setattr(h, "BASE_URL", platform.mtls)
    monkeypatch.setattr(h, "SERVICE_CREDENTIAL", platform.credential)
    monkeypatch.setattr(h, "PLATFORM_TLS", tls(pki))
    hook = h.MemMachineHook(dead_letter=tmp_path / "dead.jsonl")
    turn = h.Turn(ALICE, "s1", "p1", "cats", ALICE, "default", sha256(ALICE_KEY), 0.0, owner=ALICE)
    body = asyncio.run(hook._post_search(turn, "cats", "req-mtls"))
    assert body["content"] == EMPTY_SEARCH["content"]

    monkeypatch.setattr(h, "PLATFORM_TLS", tls(pki, "litellm-other"))
    with pytest.raises(httpx.HTTPStatusError) as exc:
        asyncio.run(hook._post_search(turn, "cats", "req-mtls"))
    assert exc.value.response.status_code == 401
