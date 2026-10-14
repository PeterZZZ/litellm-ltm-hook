# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the hook in platform mode (MEMMACHINE_PLATFORM=1).

The hook must forward evidence, decide nothing, never send a raw key, and treat
the platform's "not enrolled" answer as memory being off.

    uv run pytest test_platform_mode.py -v
"""
import asyncio
import datetime
import hashlib
import json
import os

os.environ.setdefault("MEMMACHINE_BASE_URL", "http://memmachine.unit.test")

import httpx
import pytest

import memmachine_hook as h

SERVICE = "mm-svc-test-credential"
KEY_HASH = hashlib.sha256(b"sk-alice").hexdigest()
TOKEN = "eyJ.header.payload"  # opaque to the hook: it must not try to verify it

FOUND = {"status": 0, "content": {"episodic_memory": {
    "long_term_memory": {"episodes": [{"content": "alice likes cats"}]}}}}


@pytest.fixture(autouse=True)
def platform_mode(monkeypatch):
    monkeypatch.setattr(h, "PLATFORM_MODE", True)
    monkeypatch.setattr(h, "API_KEY", SERVICE)
    monkeypatch.setattr(h, "FORWARD_USER_INFO_HEADER_JWT_SECRET", None)
    monkeypatch.setattr(h, "MODEL_PREFIX", "memmachine-")


def respond(monkeypatch, status=200, body=None, headers=None):
    seen = []

    def handler(request):
        seen.append({
            "url": str(request.url),
            "headers": dict(request.headers),
            "json": json.loads(request.content),
        })
        return httpx.Response(status, json=body if body is not None else {}, headers=headers or {},
                              request=request)

    real_init = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    return seen


class Key:
    def __init__(self, api_key=KEY_HASH, email=None):
        self.api_key = api_key
        self.user_email = email
        self.user_id = None


def pre_call(headers=None, key=None):
    data = {
        "model": "memmachine-glm",
        "messages": [{"role": "user", "content": "what do I like?"}],
        "proxy_server_request": {"headers": dict(headers or {})},
        "litellm_call_id": "call-1",
    }
    return asyncio.run(h.MemMachineHook().async_pre_call_hook(key or Key(), None, data, "acompletion"))


class Choice:
    class message:
        content = "Cats, noted."


class Reply:
    choices = [Choice()]


def store(headers=None, key_hash=KEY_HASH, started=None):
    kwargs = {
        "messages": [{"role": "user", "content": "I like cats"}],
        "model": "glm",
        "litellm_params": {
            "metadata": {"model_group": "memmachine-glm", "user_api_key_hash": key_hash},
            "proxy_server_request": {"headers": dict(headers or {})},
        },
    }
    asyncio.run(h.MemMachineHook().async_log_success_event(kwargs, Reply(), started, None))


class TestSearch:
    def test_forwards_evidence_and_no_namespace(self, monkeypatch):
        seen = respond(monkeypatch, body=FOUND, headers={"X-MM-Org": "alice_corp_example"})
        data = pre_call({"x-openwebui-user-jwt": TOKEN, "x-memmachine-org": "bob_corp_example"})
        sent = seen[0]
        assert sent["headers"]["authorization"] == f"Bearer {SERVICE}"
        assert sent["headers"]["x-mm-key-hash"] == KEY_HASH
        assert sent["headers"]["x-mm-user-token"] == TOKEN
        assert "org_id" not in sent["json"] and "project_id" not in sent["json"]
        assert "bob_corp_example" not in json.dumps(sent)
        assert "alice likes cats" in data["messages"][0]["content"]

    def test_app_key_without_email_does_not_raise(self, monkeypatch):
        # The shared Open WebUI key has no user_email. Outside platform mode that
        # is a hard error; here identity is the platform's problem.
        respond(monkeypatch, body=FOUND)
        pre_call({"x-openwebui-user-jwt": TOKEN}, Key(email=None))

    def test_raw_key_is_never_forwarded(self, monkeypatch):
        seen = respond(monkeypatch, body=FOUND)
        pre_call(key=Key(api_key="sk-raw-secret"))
        sent = seen[0]["headers"]["x-mm-key-hash"]
        assert sent == hashlib.sha256(b"sk-raw-secret").hexdigest()
        assert "sk-raw-secret" not in json.dumps(seen)

    def test_not_enrolled_continues_without_memory(self, monkeypatch):
        respond(monkeypatch, status=403, body={"detail": "x"}, headers={"X-MM-Decision": "not-enrolled"})
        data = pre_call()
        assert data["messages"] == [{"role": "user", "content": "what do I like?"}]

    @pytest.mark.parametrize("status", [401, 403])
    def test_refusal_reaches_the_caller(self, monkeypatch, status):
        respond(monkeypatch, status=status, body={"detail": "x"}, headers={"X-MM-Decision": "refused"})
        with pytest.raises(h.MemMachineRequestError) as exc:
            pre_call()
        assert exc.value.status_code == status

    def test_platform_down_continues_without_memory(self, monkeypatch):
        def boom(request):
            raise httpx.ConnectError("down", request=request)

        real_init = httpx.AsyncClient.__init__
        monkeypatch.setattr(httpx.AsyncClient, "__init__",
                            lambda self, *a, **k: real_init(self, *a, **{**k, "transport": httpx.MockTransport(boom)}))
        data = pre_call()
        assert len(data["messages"]) == 1


class TestStore:
    def test_forwards_evidence_and_start_time(self, monkeypatch):
        seen = respond(monkeypatch, body={"results": []})
        started = datetime.datetime.now()
        store({"x-openwebui-user-jwt": TOKEN}, started=started)
        sent = seen[0]
        assert sent["url"].endswith("/api/v2/memories")
        assert sent["headers"]["x-mm-key-hash"] == KEY_HASH
        assert sent["headers"]["x-mm-user-token"] == TOKEN
        assert float(sent["headers"]["x-mm-request-started"]) == pytest.approx(started.timestamp(), abs=0.01)
        assert "org_id" not in sent["json"]
        assert [m["role"] for m in sent["json"]["messages"]] == ["user", "assistant"]

    def test_not_enrolled_is_not_an_error(self, monkeypatch):
        respond(monkeypatch, status=403, body={}, headers={"X-MM-Decision": "not-enrolled"})
        errors = []
        monkeypatch.setattr(h, "_log_error", lambda **f: errors.append(f))
        store()
        assert errors == []


def test_platform_mode_refuses_a_local_signing_secret(monkeypatch, tmp_path):
    """The secret belongs to the platform: importing with both set must fail."""
    import importlib
    import sys

    env = {**os.environ, "MEMMACHINE_PLATFORM": "1", "MEMMACHINE_API_KEY": SERVICE,
           "FORWARD_USER_INFO_HEADER_JWT_SECRET": "x" * 40}
    monkeypatch.setattr(os, "environ", env)
    sys.modules.pop("memmachine_hook_copy", None)
    spec = importlib.util.spec_from_file_location("memmachine_hook_copy", h.__file__)
    module = importlib.util.module_from_spec(spec)
    with pytest.raises(RuntimeError, match="belongs there only"):
        spec.loader.exec_module(module)
