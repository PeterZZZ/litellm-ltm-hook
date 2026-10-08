# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0

"""Unit + live tests for memmachine_hook.

Unit tests mock httpx and verify the hook's parsing/injection/store logic.
Live tests call the real MEMMACHINE_BASE_URL to verify the add->search->inject flow.

Run:
  uv run pytest test_hook.py -v                       # unit + live
  MEMMACHINE_LIVE=0 uv run pytest test_hook.py -v      # unit only
"""
import asyncio
import json
import logging
import os
import types

# The hook requires MEMMACHINE_BASE_URL at import time. Unit tests don't make
# real HTTP calls (httpx is mocked), so set a placeholder if the real env var
# isn't already set. Live tests require the real value to be present and will
# skip themselves if it's missing.
os.environ.setdefault("MEMMACHINE_BASE_URL", "http://memmachine.unit.test")

import httpx
import pytest

import memmachine_hook as h


# ---------- live on/off ----------
LIVE = os.environ.get("MEMMACHINE_LIVE", "1") == "1"
# Live tests target the real MemMachine server. The hook itself requires
# MEMMACHINE_BASE_URL, so live tests reuse the same env var rather than
# hardcoding a host (falling back to a placeholder only to print a clear error).
LIVE_BASE_URL = os.environ.get("MEMMACHINE_BASE_URL", "").rstrip("/")
LIVE_BASE_URL = LIVE_BASE_URL if LIVE_BASE_URL != "http://memmachine.unit.test" else ""


# ====================================================================
# Unit tests: response parsing/extraction
# ====================================================================
class TestParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("episodic", ["episodic"]),
            ("SEMANTIC", ["semantic"]),
            ("episodic, semantic,episodic", ["episodic", "semantic"]),
            ("all", []),
            ("*", []),
            ("", []),
            ("[]", []),
        ],
    )
    def test_parse_search_types(self, raw, expected):
        assert h._parse_search_types(raw) == expected

    def test_parse_search_types_rejects_unknown_values(self):
        with pytest.raises(ValueError, match="short_term"):
            h._parse_search_types("episodic,short_term")

    def test_extract_episodic_long_and_short_and_summary(self):
        episodic = {
            "long_term_memory": {
                "episodes": [
                    {"content": "I am Kim and I like blue.", "producer_id": "alice"},
                    {"content": "I follow a vegan diet.", "producer_id": "alice"},
                ]
            },
            "short_term_memory": {
                "episodes": [{"content": "blue just mentioned"}],
                "episode_summary": ["", "blue/vegan conversation summary"],
            },
        }
        out = h._extract_episodic_texts(episodic)
        assert "I am Kim and I like blue." in out
        assert "I follow a vegan diet." in out
        assert "blue just mentioned" in out
        assert "blue/vegan conversation summary" in out
        assert "" not in out  # empty summaries are excluded

    def test_extract_episodic_empty(self):
        assert h._extract_episodic_texts(None) == []
        assert h._extract_episodic_texts({"long_term_memory": {"episodes": []},
                                          "short_term_memory": {"episodes": [], "episode_summary": [""]}}) == []

    def test_extract_semantic_value(self):
        semantic = [
            {"category": "preference", "tag": "color", "feature_name": "favorite_color", "value": "blue"},
            {"category": "diet", "tag": "food", "feature_name": "diet", "value": "vegan"},
        ]
        out = h._extract_semantic_texts(semantic)
        assert out == ["blue", "vegan"]

    def test_real_search_response_shape(self):
        """Parsing must read under `content`, matching the real server response shape."""
        body = {
            "status": 0,
            "content": {
                "episodic_memory": {
                    "long_term_memory": {"episodes": [{"content": "episode body"}]},
                    "short_term_memory": {"episodes": [], "episode_summary": [""]},
                },
                "semantic_memory": [],
            },
        }
        content = body.get("content") or {}
        lines = h._extract_semantic_texts(content.get("semantic_memory")) + \
            h._extract_episodic_texts(content.get("episodic_memory"))
        assert lines == ["episode body"]
        # regression guard for an old bug: reading at the top level would yield an empty list
        # (the impl correctly reads under `content`)
        assert body.get("semantic_memory") is None
        assert body.get("episodic_memory") is None


# ====================================================================
# Unit tests: Claude Code memory-text cleanup
# ====================================================================
class TestClaudeCodeCleanup:
    def test_detects_claude_cli_user_agent_case_insensitively(self):
        data = {
            "proxy_server_request": {
                "headers": {"User-Agent": "claude-cli/2.1.77 (external, cli)"}
            }
        }
        assert h._is_claude_code_request(data) is True

    @pytest.mark.parametrize(
        "user_agent",
        ["aichat/0.30.0", "claude-code/2.1.77", "", None],
    )
    def test_does_not_treat_other_user_agents_as_claude_code(self, user_agent):
        data = {"proxy_server_request": {"headers": {"user-agent": user_agent}}}
        assert h._is_claude_code_request(data) is False

    def test_strips_unknown_complete_wrapper_blocks_and_keeps_plain_text(self):
        text = (
            "<future-wrapper source=\"claude\">hidden context</future-wrapper>\n\n"
            "<command-name>/model</command-name>\n"
            "<command-message>model</command-message>\n"
            "What is my favorite jelly?"
        )
        assert h._strip_claude_code_system_tags(text) == "What is my favorite jelly?"

    def test_preserves_incomplete_tag(self):
        text = "<system-reminder>not closed\nWhat is my favorite jelly?"
        assert h._strip_claude_code_system_tags(text) == text

    def test_search_uses_clean_text_but_preserves_original_prompt(self, monkeypatch):
        monkeypatch.setattr(h, "CLAUDE_CODE_STRIP_SYSTEM_TAGS", True)
        captured = {}
        _mock_search(
            monkeypatch,
            {"status": 0, "content": {}},
            captured,
        )
        original = (
            "<system-reminder>private instructions</system-reminder>\n\n"
            "<new-claude-tag>synthetic</new-claude-tag>\n"
            "What is my favorite jelly?"
        )
        data = {
            "messages": [{"role": "user", "content": original}],
            "model": "memmachine-m",
            "proxy_server_request": {
                "headers": {"user-agent": "claude-cli/9.9.9 (external, cli)"}
            },
        }

        out = _run(
            _make_handler().async_pre_call_hook(
                FakeUserApiKey("alice"), None, data, "anthropic_messages"
            )
        )

        assert captured["search_req"]["json"]["query"] == "What is my favorite jelly?"
        assert out["messages"][0]["content"] == original

    def test_cleanup_can_be_disabled(self, monkeypatch):
        monkeypatch.setattr(h, "CLAUDE_CODE_STRIP_SYSTEM_TAGS", False)
        content = "<system-reminder>keep</system-reminder>\nquestion"
        data = {
            "proxy_server_request": {
                "headers": {"user-agent": "claude-cli/2.1.77"}
            }
        }
        assert h._memory_user_text(data, content) == content


# ====================================================================
# Unit tests: injection placement
# ====================================================================
def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


class FakeUserApiKey:
    def __init__(self, email=None, uid="ignored-user-id"):
        self.user_email = email
        self.user_id = uid


def _make_handler():
    return h.MemMachineHook()


def _mock_search(monkeypatch, body, captured, status_code=200):
    """Patch httpx.AsyncClient so its .post() captures the request and returns `body`.

    httpx 0.28 no longer lets monkeypatch replace `AsyncClient.post` directly (the
    instance method is bound and ignores class-level patches). We instead inject a
    `MockTransport` via the client's `__init__` so every request the hook makes flows
    through our handler.
    """
    async def handler(request):
        captured["search_req"] = {"url": str(request.url), "json": json.loads(request.content)}
        return httpx.Response(status_code, json=body, request=request)

    _patch_transport(monkeypatch, handler)


def _mock_store(monkeypatch, captured, status_code=200):
    """Same transport-injection pattern as _mock_search, but for the store path.

    Captures the store URL and payload, and returns the requested HTTP status.
    """
    async def handler(request):
        captured["store_url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            status_code,
            json={"results": [{"uid": "1"}]} if status_code < 400 else {"error": "store failed"},
            request=request,
        )

    _patch_transport(monkeypatch, handler)


def _mock_http_error(monkeypatch, exc):
    """Make every httpx request raise `exc` (for failure-path tests)."""
    def handler(request):
        raise exc

    _patch_transport(monkeypatch, handler)


def _capture_error_logs(monkeypatch):
    records = []

    def recorder(**fields):
        records.append(fields)

    monkeypatch.setattr(h, "_log_error", recorder)
    return records


def _patch_transport(monkeypatch, handler):
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


class TestInjection:
    def test_injects_system_block_right_before_last_user(self, monkeypatch):
        body = {"status": 0, "content": {
            "episodic_memory": {"long_term_memory": {"episodes": [{"content": "user likes blue"}]},
                                "short_term_memory": {"episodes": [], "episode_summary": [""]}},
            "semantic_memory": [],
        }}
        captured = {}
        _mock_search(monkeypatch, body, captured)

        data = {
            "messages": [
                {"role": "system", "content": "you are a helper"},
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "yes"},
                {"role": "user", "content": "what color do I like?"},
            ],
            "model": "memmachine-gpt-test",
        }
        out = _run(_make_handler().async_pre_call_hook(FakeUserApiKey("alice"), None, data, "completion"))

        msgs = out["messages"]
        # a system memory block must be inserted right before the last user message
        assert msgs[-1]["role"] == "user"
        assert msgs[-1]["content"] == "what color do I like?"
        assert msgs[-2]["role"] == "system"
        assert h.MEMORY_MARKER in msgs[-2]["content"]
        assert "user likes blue" in msgs[-2]["content"]
        # the prefix (system + previous conversation) stays unchanged
        assert msgs[0]["content"] == "you are a helper"
        assert len(msgs) == 5  # original 4 + injected 1

    def test_anthropic_route_injects_role_system_into_messages(self, monkeypatch):
        """Anthropic /v1/messages route injects a role:system block into messages,
        same as the OpenAI Chat Completions route (LiteLLM >= 1.98 preserves it)."""
        body = {"status": 0, "content": {
            "episodic_memory": {"long_term_memory": {"episodes": [{"content": "user likes blue"}]},
                                "short_term_memory": {"episodes": [], "episode_summary": [""]}},
            "semantic_memory": [],
        }}
        captured = {}
        _mock_search(monkeypatch, body, captured)

        data = {
            "messages": [{"role": "user", "content": "what color do I like?"}],
            "model": "memmachine-gpt-test",
        }
        out = _run(_make_handler().async_pre_call_hook(
            FakeUserApiKey("alice"), None, data, "anthropic_messages"))

        # a role:system block is inserted right before the last user message
        msgs = out["messages"]
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert h.MEMORY_MARKER in msgs[0]["content"]
        assert "user likes blue" in msgs[0]["content"]
        assert msgs[1] == {"role": "user", "content": "what color do I like?"}

    def test_anthropic_route_preserves_existing_system_field(self, monkeypatch):
        """A client-supplied top-level `system` field is left untouched; the memory
        block goes into messages as a role:system entry."""
        body = {"status": 0, "content": {
            "episodic_memory": {"long_term_memory": {"episodes": [{"content": "past memory"}]},
                                "short_term_memory": {"episodes": [], "episode_summary": [""]}},
            "semantic_memory": [],
        }}
        captured = {}
        _mock_search(monkeypatch, body, captured)

        data = {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "memmachine-gpt-test",
            "system": "you are a helper",
        }
        out = _run(_make_handler().async_pre_call_hook(
            FakeUserApiKey("alice"), None, data, "anthropic_messages"))

        # existing system field stays as-is
        assert out["system"] == "you are a helper"
        # memory block is in messages, not in system
        msgs = out["messages"]
        assert msgs[0]["role"] == "system"
        assert h.MEMORY_MARKER in msgs[0]["content"]
        assert "past memory" in msgs[0]["content"]

    def test_responses_route_injects_role_system_into_input(self, monkeypatch):
        """OpenAI /v1/responses route injects a role:system block into `input`,
        same placement as the other routes (role:system right before the last
        user item)."""
        body = {"status": 0, "content": {
            "episodic_memory": {"long_term_memory": {"episodes": [{"content": "user likes blue"}]},
                                "short_term_memory": {"episodes": [], "episode_summary": [""]}},
            "semantic_memory": [],
        }}
        captured = {}
        _mock_search(monkeypatch, body, captured)

        data = {
            "input": [{"role": "user", "content": "what color do I like?"}],
            "model": "memmachine-gpt-test",
        }
        out = _run(_make_handler().async_pre_call_hook(
            FakeUserApiKey("alice"), None, data, "aresponses"))

        # a role:system block is inserted into `input` right before the last user item
        inp = out["input"]
        assert len(inp) == 2
        assert inp[0]["role"] == "system"
        assert h.MEMORY_MARKER in inp[0]["content"]
        assert "user likes blue" in inp[0]["content"]
        assert inp[1] == {"role": "user", "content": "what color do I like?"}

    def test_responses_route_string_input(self, monkeypatch):
        """A bare string `input` is a valid prompt and must drive search, and the
        memory block is inserted as a role:system entry before it."""
        body = {"status": 0, "content": {
            "episodic_memory": {"long_term_memory": {"episodes": [{"content": "user likes blue"}]},
                                "short_term_memory": {"episodes": [], "episode_summary": [""]}},
            "semantic_memory": [],
        }}
        captured = {}
        _mock_search(monkeypatch, body, captured)

        data = {"input": "what color do I like?", "model": "memmachine-gpt-test"}
        out = _run(_make_handler().async_pre_call_hook(
            FakeUserApiKey("alice"), None, data, "aresponses"))

        # search was called with the string input as the query
        assert captured["search_req"]["json"]["query"] == "what color do I like?"
        # input is now a list: [system memory, user]
        inp = out["input"]
        assert len(inp) == 2
        assert inp[0]["role"] == "system"
        assert h.MEMORY_MARKER in inp[0]["content"]
        assert "user likes blue" in inp[0]["content"]
        assert inp[1] == {"role": "user", "content": "what color do I like?"}

    def test_responses_route_preserves_existing_instructions(self, monkeypatch):
        """A client-supplied top-level `instructions` field is left untouched; the
        memory block goes into `input` as a role:system entry."""
        body = {"status": 0, "content": {
            "episodic_memory": {"long_term_memory": {"episodes": [{"content": "past memory"}]},
                                "short_term_memory": {"episodes": [], "episode_summary": [""]}},
            "semantic_memory": [],
        }}
        captured = {}
        _mock_search(monkeypatch, body, captured)

        data = {
            "input": [{"role": "user", "content": "hi"}],
            "model": "memmachine-gpt-test",
            "instructions": "you are a helper",
        }
        out = _run(_make_handler().async_pre_call_hook(
            FakeUserApiKey("alice"), None, data, "aresponses"))

        # existing instructions field stays as-is
        assert out["instructions"] == "you are a helper"
        # memory block is in `input`, not in `instructions`
        inp = out["input"]
        assert inp[0]["role"] == "system"
        assert h.MEMORY_MARKER in inp[0]["content"]
        assert "past memory" in inp[0]["content"]

    def test_unsupported_call_type_returns_unchanged(self, monkeypatch):
        """Unsupported call_type (e.g. embeddings) returns data unchanged with no injection."""
        async def boom(request):
            raise AssertionError("search should NOT be called for unsupported call_type")
        _patch_transport(monkeypatch, boom)
        data = {"messages": [{"role": "user", "content": "hi"}], "model": "memmachine-m"}
        out = _run(_make_handler().async_pre_call_hook(FakeUserApiKey("alice"), None, data, "embeddings"))
        assert out is data

    def test_no_user_message_returns_unchanged(self, monkeypatch):
        captured = {}
        _mock_search(monkeypatch, {"status": 0, "content": {"episodic_memory": {}, "semantic_memory": []}}, captured)
        data = {"messages": [{"role": "system", "content": "x"}], "model": "memmachine-m"}
        out = _run(_make_handler().async_pre_call_hook(FakeUserApiKey("alice"), None, data, "completion"))
        assert out["messages"] == data["messages"]

    def test_empty_results_returns_unchanged(self, monkeypatch):
        body = {"status": 0, "content": {
            "episodic_memory": {"long_term_memory": {"episodes": []},
                               "short_term_memory": {"episodes": [], "episode_summary": [""]}},
            "semantic_memory": [],
        }}
        captured = {}
        _mock_search(monkeypatch, body, captured)
        data = {"messages": [{"role": "user", "content": "hi"}], "model": "memmachine-m"}
        out = _run(_make_handler().async_pre_call_hook(FakeUserApiKey("alice"), None, data, "completion"))
        assert out["messages"] == [{"role": "user", "content": "hi"}]

    def test_episodic_only_uses_namespace_without_filter(self, monkeypatch):
        monkeypatch.setattr(h, "SEARCH_TYPES", ["episodic"])
        body = {"status": 0, "content": {"episodic_memory": {"long_term_memory": {"episodes": []},
                                                              "short_term_memory": {"episodes": [], "episode_summary": [""]}},
                                        "semantic_memory": []}}
        captured = {}
        _mock_search(monkeypatch, body, captured)
        data = {"messages": [{"role": "user", "content": "hi"}], "model": "memmachine-m"}
        _run(_make_handler().async_pre_call_hook(FakeUserApiKey("alice"), None, data, "completion"))
        payload = captured["search_req"]["json"]
        assert payload["types"] == ["episodic"]
        assert "filter" not in payload
        assert "set_metadata" not in payload
        assert payload["org_id"] == "alice"

    def test_semantic_only_uses_default_org_and_project_sets(self, monkeypatch):
        monkeypatch.setattr(h, "SEARCH_TYPES", ["semantic"])
        body = {"status": 0, "content": {
            "episodic_memory": None,
            "semantic_memory": [{"value": "project-wide semantic fact"}],
        }}
        captured = {}
        _mock_search(monkeypatch, body, captured)
        data = {"messages": [{"role": "user", "content": "hi"}], "model": "memmachine-m"}
        out = _run(_make_handler().async_pre_call_hook(
            FakeUserApiKey("alice"), None, data, "completion"))
        payload = captured["search_req"]["json"]
        assert payload["types"] == ["semantic"]
        assert "filter" not in payload
        assert "set_metadata" not in payload
        assert "project-wide semantic fact" in out["messages"][0]["content"]

    @pytest.mark.parametrize("search_types", [[], ["episodic", "semantic"]])
    def test_all_or_mixed_preserves_all_namespace_episodic_results(
        self, monkeypatch, search_types
    ):
        monkeypatch.setattr(h, "SEARCH_TYPES", search_types)
        body = {"status": 0, "content": {
            "episodic_memory": {
                "long_term_memory": {"episodes": [
                    {"content": "alice episodic fact", "producer_id": "alice"},
                    {"content": "bob episodic fact", "producer_id": "bob"},
                    {"content": "assistant episodic fact", "producer_id": h.AGENT_ID},
                ]},
                "short_term_memory": {"episodes": [], "episode_summary": [""]},
            },
            "semantic_memory": [{"value": "project-wide semantic fact"}],
        }}
        captured = {}
        _mock_search(monkeypatch, body, captured)
        data = {"messages": [{"role": "user", "content": "hi"}], "model": "memmachine-m"}
        out = _run(_make_handler().async_pre_call_hook(
            FakeUserApiKey("alice"), None, data, "completion"))

        payload = captured["search_req"]["json"]
        assert payload["types"] == search_types
        assert "filter" not in payload
        assert "set_metadata" not in payload
        memory = out["messages"][0]["content"]
        assert "alice episodic fact" in memory
        assert "bob episodic fact" in memory
        assert "assistant episodic fact" in memory
        assert "project-wide semantic fact" in memory
        assert "do not assume every item describes the current user" in memory

    def test_virtual_key_email_is_slugified_for_user_and_default_org(self, monkeypatch):
        body = {"status": 0, "content": {"episodic_memory": {"long_term_memory": {"episodes": []},
                                                              "short_term_memory": {"episodes": [], "episode_summary": [""]}},
                                        "semantic_memory": []}}
        captured = {}
        _mock_search(monkeypatch, body, captured)
        data = {"messages": [{"role": "user", "content": "hi"}], "model": "memmachine-m",
                "user": "body-user",
                "metadata": {"user_id": "request-metadata-user"},
                "proxy_server_request": {"headers": {"x-memmachine-user": "header-user"}}}
        _run(_make_handler().async_pre_call_hook(
            FakeUserApiKey("alice@example.com"), None, data, "completion"))
        payload = captured["search_req"]["json"]
        assert payload["org_id"] == "alice_example_com"
        assert payload["project_id"] == h.PROJECT_ID
        assert "filter" not in payload

    def test_org_and_project_headers_override_defaults(self, monkeypatch):
        body = {"status": 0, "content": {"episodic_memory": {"long_term_memory": {"episodes": []},
                                                              "short_term_memory": {"episodes": [], "episode_summary": [""]}},
                                        "semantic_memory": []}}
        captured = {}
        _mock_search(monkeypatch, body, captured)
        data = {"messages": [{"role": "user", "content": "hi"}], "model": "memmachine-m",
                "proxy_server_request": {"headers": {
                    "x-memmachine-org": "shared-org",
                    "x-memmachine-project": "shared-project",
                }}}
        _run(_make_handler().async_pre_call_hook(
            FakeUserApiKey("alice@example.com"), None, data, "completion"))
        payload = captured["search_req"]["json"]
        assert payload["org_id"] == "shared-org"
        assert payload["project_id"] == "shared-project"
        assert "filter" not in payload

    def test_missing_virtual_key_email_does_not_fall_back_to_request_identity(self):
        data = {"user": "body-user", "metadata": {"user_id": "metadata-user"},
                "proxy_server_request": {"headers": {"x-memmachine-user": "header-user"}}}
        with pytest.raises(ValueError, match="user_email"):
            h._identity(data, FakeUserApiKey(None))

    def test_does_not_reinject_if_memory_block_already_present(self, monkeypatch):
        # search must NOT be called: if the transport fires, the test fails
        def boom(request):
            raise AssertionError("search should NOT be called when memory block already present")
        _patch_transport(monkeypatch, boom)

        data = {
            "messages": [
                {"role": "system", "content": f"{h.MEMORY_MARKER}\nprevious memory\n</memory>"},
                {"role": "user", "content": "let me ask again"},
            ],
            "model": "memmachine-m",
        }
        out = _run(_make_handler().async_pre_call_hook(FakeUserApiKey("alice"), None, data, "completion"))
        assert out["messages"] == data["messages"]

    @pytest.mark.parametrize(
        ("status_code", "expected_detail", "category"),
        [
            (400, "Invalid MemMachine memory request", "invalid_request"),
            (401, "MemMachine authentication failed", "authentication"),
            (
                403,
                "MemMachine access denied for the requested memory namespace",
                "authorization",
            ),
            (422, "Invalid MemMachine memory request", "invalid_request"),
        ],
    )
    def test_search_client_error_is_fail_closed_with_sanitized_status(
        self, monkeypatch, status_code, expected_detail, category
    ):
        captured = {}
        records = _capture_error_logs(monkeypatch)
        _mock_search(
            monkeypatch,
            {"error": "upstream secret must not escape"},
            captured,
            status_code=status_code,
        )
        data = {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "memmachine-m",
            "litellm_call_id": "call-123",
        }

        with pytest.raises(h.MemMachineRequestError) as exc_info:
            _run(
                _make_handler().async_pre_call_hook(
                    FakeUserApiKey("alice"), None, data, "completion"
                )
            )

        assert exc_info.value.status_code == status_code
        assert exc_info.value.detail == expected_detail
        assert "upstream secret" not in str(exc_info.value)
        assert records == [{
            "operation": "search",
            "category": category,
            "status_code": status_code,
            "org_id": "alice",
            "project_id": h.PROJECT_ID,
            "request_id": "call-123",
            "error_type": "HTTPStatusError",
        }]

    @pytest.mark.parametrize(
        ("status_code", "category"),
        [
            (404, "not_found"),
            (408, "timeout"),
            (429, "rate_limited"),
            (500, "upstream"),
        ],
    )
    def test_search_availability_error_is_fail_open(
        self, monkeypatch, status_code, category
    ):
        captured = {}
        records = _capture_error_logs(monkeypatch)
        _mock_search(
            monkeypatch,
            {"error": "unavailable"},
            captured,
            status_code=status_code,
        )
        data = {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "memmachine-m",
        }

        out = _run(
            _make_handler().async_pre_call_hook(
                FakeUserApiKey("alice"), None, data, "completion"
            )
        )

        assert out["messages"] == data["messages"]
        assert records[0]["operation"] == "search"
        assert records[0]["category"] == category
        assert records[0]["status_code"] == status_code

    @pytest.mark.parametrize(
        ("transport_error", "category"),
        [
            (httpx.ConnectError("boom"), "transport"),
            (httpx.ReadTimeout("slow upstream"), "timeout"),
        ],
    )
    def test_search_transport_failure_returns_unchanged(
        self, monkeypatch, transport_error, category
    ):
        records = _capture_error_logs(monkeypatch)
        _mock_http_error(monkeypatch, transport_error)
        data = {"messages": [{"role": "user", "content": "hi"}], "model": "memmachine-m"}
        out = _run(_make_handler().async_pre_call_hook(FakeUserApiKey("alice"), None, data, "completion"))
        assert out["messages"] == data["messages"]
        assert records[0]["category"] == category

    def test_malformed_search_response_is_logged_and_fail_open(self, monkeypatch):
        captured = {}
        records = _capture_error_logs(monkeypatch)
        _mock_search(monkeypatch, {"content": ["not", "an", "object"]}, captured)
        data = {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "memmachine-m",
        }

        out = _run(
            _make_handler().async_pre_call_hook(
                FakeUserApiKey("alice"), None, data, "completion"
            )
        )

        assert out["messages"] == data["messages"]
        assert records[0]["operation"] == "search"
        assert records[0]["category"] == "unexpected"
        assert records[0]["error_type"] == "ValueError"

    def test_error_log_is_independent_of_debug(self, monkeypatch, caplog):
        monkeypatch.setattr(h, "DEBUG", False)

        with caplog.at_level(logging.ERROR, logger=h._ERROR_LOGGER.name):
            h._log_error(
                operation="search",
                category="authorization",
                status_code=403,
                org_id="shared-org",
                project_id="private-project",
                request_id="call-123",
                error_type="HTTPStatusError",
            )

        assert "MemMachine operation=search category=authorization status=403" in caplog.text
        assert "request_id='call-123'" in caplog.text


# ====================================================================
# Unit tests: store payload
# ====================================================================
class TestStorage:
    def test_claude_code_store_uses_clean_user_text(self, monkeypatch):
        monkeypatch.setattr(h, "CLAUDE_CODE_STRIP_SYSTEM_TAGS", True)
        captured = {}

        class FakeChoice:
            class message:
                content = "I will remember that."

        class FakeResp:
            choices = [FakeChoice()]

        _mock_store(monkeypatch, captured)
        original = (
            "<system-reminder>synthetic context</system-reminder>\n"
            "My favorite jelly is MyGummy."
        )
        kwargs = {
            "messages": [{"role": "user", "content": original}],
            "model": "nvidia/backend",
            "litellm_params": {
                "metadata": {
                    "model_group": "memmachine-m",
                    "user_api_key_user_email": "alice@example.com",
                },
                "proxy_server_request": {
                    "headers": {"user-agent": "claude-cli/2.1.77 (external, cli)"}
                },
            },
        }

        _run(_make_handler().async_log_success_event(kwargs, FakeResp(), None, None))

        assert captured["payload"]["messages"][0]["content"] == (
            "My favorite jelly is MyGummy."
        )

    def test_store_payload_producer_is_user_id(self, monkeypatch):
        captured = {}

        class FakeChoice:
            class message:
                content = "I'll remember that."
            finish_reason = "stop"

        class FakeResp:
            choices = [FakeChoice()]

        _mock_store(monkeypatch, captured)

        kwargs = {
            "messages": [{"role": "user", "content": "I like blue"}],
            "model": "memmachine-m",
            "litellm_params": {"metadata": {"user_api_key_user_email": "alice"}},
            "proxy_server_request": {"headers": {}},
            "user": "ignored-body-user",
        }
        _run(_make_handler().async_log_success_event(kwargs, FakeResp(), None, None))

        assert captured["store_url"].endswith("/api/v2/memories")
        msgs = captured["payload"]["messages"]
        assert len(msgs) == 2
        assert msgs[0]["producer"] == "alice"      # user utterance producer = user_id
        assert msgs[0]["produced_for"] == h.AGENT_ID
        assert msgs[0]["content"] == "I like blue"
        assert msgs[1]["producer"] == h.AGENT_ID
        assert msgs[1]["content"] == "I'll remember that."
        assert captured["payload"]["types"] == []
        assert captured["payload"]["project_id"] == h.PROJECT_ID
        # org_id is derived from user_id: alice -> alice
        assert captured["payload"]["org_id"] == "alice"

    def test_store_payload_slugifies_email_to_org_id(self, monkeypatch):
        """An email-shaped user_id is slugified into org_id (alice@example.com -> alice_example_com)."""
        captured = {}

        class FakeChoice:
            class message:
                content = "ok"
            finish_reason = "stop"

        class FakeResp:
            choices = [FakeChoice()]

        _mock_store(monkeypatch, captured)

        kwargs = {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "memmachine-m",
            "litellm_params": {"metadata": {"user_api_key_user_email": "alice@example.com"}},
            "proxy_server_request": {"headers": {}},
            "user": "ignored-body-user",
        }
        _run(_make_handler().async_log_success_event(kwargs, FakeResp(), None, None))

        assert captured["payload"]["org_id"] == "alice_example_com"
        assert captured["payload"]["messages"][0]["producer"] == "alice_example_com"
        assert captured["payload"]["messages"][1]["produced_for"] == "alice_example_com"

    @pytest.mark.parametrize(
        ("status_code", "category"),
        [
            (400, "invalid_request"),
            (401, "authentication"),
            (403, "authorization"),
            (404, "not_found"),
            (408, "timeout"),
            (429, "rate_limited"),
            (500, "upstream"),
        ],
    )
    def test_store_http_error_is_detected_and_remains_fail_open(
        self, monkeypatch, status_code, category
    ):
        captured = {}
        records = _capture_error_logs(monkeypatch)

        class FakeChoice:
            class message:
                content = "ok"

        class FakeResp:
            choices = [FakeChoice()]

        _mock_store(monkeypatch, captured, status_code=status_code)
        kwargs = {
            "messages": [{"role": "user", "content": "remember this"}],
            "model": "memmachine-m",
            "litellm_params": {"metadata": {"user_api_key_user_email": "alice"}},
            "proxy_server_request": {"headers": {}},
        }

        result = _run(
            _make_handler().async_log_success_event(kwargs, FakeResp(), None, None)
        )

        assert result is None
        assert captured["payload"]["messages"][0]["content"] == "remember this"
        assert records[0]["operation"] == "store"
        assert records[0]["category"] == category
        assert records[0]["status_code"] == status_code
        assert records[0]["error_type"] == "HTTPStatusError"

    @pytest.mark.parametrize(
        ("transport_error", "category"),
        [
            (httpx.ConnectError("store unavailable"), "transport"),
            (httpx.ReadTimeout("store timed out"), "timeout"),
        ],
    )
    def test_store_transport_error_remains_fail_open(
        self, monkeypatch, transport_error, category
    ):
        records = _capture_error_logs(monkeypatch)

        class FakeChoice:
            class message:
                content = "ok"

        class FakeResp:
            choices = [FakeChoice()]

        _mock_http_error(monkeypatch, transport_error)
        kwargs = {
            "messages": [{"role": "user", "content": "remember this"}],
            "model": "memmachine-m",
            "litellm_params": {"metadata": {"user_api_key_user_email": "alice"}},
            "proxy_server_request": {"headers": {}},
        }

        result = _run(
            _make_handler().async_log_success_event(kwargs, FakeResp(), None, None)
        )

        assert result is None
        assert records[0]["operation"] == "store"
        assert records[0]["category"] == category
        assert records[0].get("status_code") is None
        assert records[0]["error_type"] == type(transport_error).__name__

    def test_store_falls_back_to_litellm_metadata_for_anthropic_route(self, monkeypatch):
        """Anthropic /v1/messages route: when metadata.model_group is empty, the
        original requested model name is recovered from
        litellm_params.litellm_metadata.model_group (or proxy_server_request.body.model)
        so _in_scope passes and store fires."""
        captured = {}

        class FakeChoice:
            class message:
                content = "ok"
            finish_reason = "stop"

        class FakeResp:
            choices = [FakeChoice()]

        _mock_store(monkeypatch, captured)

        kwargs = {
            "messages": [{"role": "user", "content": "hi"}],
            # backend model name after the router resolves model_group_alias (no memmachine- prefix)
            "model": "Qwen/Qwen3.5-122B-A10B-FP8",
            "litellm_params": {
                # OpenAI route uses `metadata`; Anthropic route uses `litellm_metadata`
                "metadata": {},
                "litellm_metadata": {
                    "model_group": "memmachine-llm-medium",
                    "user_api_key_user_email": "alice@example.com",
                },
                "proxy_server_request": {
                    "body": {"model": "memmachine-llm-medium"},
                },
            },
            "user": "alice@example.com",
        }
        _run(_make_handler().async_log_success_event(kwargs, FakeResp(), None, None))

        # store must fire (model_group recovered from litellm_metadata -> _in_scope passes)
        assert "payload" in captured, "store should fire — model_group recovered from litellm_metadata"
        assert captured["payload"]["org_id"] == "alice_example_com"
        assert captured["payload"]["messages"][0]["producer"] == "alice_example_com"

    def test_store_uses_org_and_project_headers(self, monkeypatch):
        captured = {}

        class FakeChoice:
            class message:
                content = "ok"

        class FakeResp:
            choices = [FakeChoice()]

        _mock_store(monkeypatch, captured)
        kwargs = {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "memmachine-m",
            "litellm_params": {
                "metadata": {"user_api_key_user_email": "alice@example.com"},
                "proxy_server_request": {"headers": {
                    "x-memmachine-org": "shared-org",
                    "x-memmachine-project": "shared-project",
                }},
            },
        }
        _run(_make_handler().async_log_success_event(kwargs, FakeResp(), None, None))

        assert captured["payload"]["org_id"] == "shared-org"
        assert captured["payload"]["project_id"] == "shared-project"
        assert captured["payload"]["messages"][0]["producer"] == "alice_example_com"

    def test_store_parses_responses_api_output_message(self, monkeypatch):
        """For /v1/responses, the assistant text comes from output[type=message].content[].text,
        not choices[0].message.content. Reasoning items must be skipped."""
        captured = {}

        # Simulate a ResponsesAPIResponse-shaped object:
        # output = [ResponseReasoningItem(...), ResponseOutputMessage(type="message", content=[...])]
        class ReasoningContent:
            def __init__(self, t): self.text = t
        class ReasoningItem:
            type = "reasoning"
            def __init__(self): self.content = [ReasoningContent("internal reasoning")]
        class MessageContent:
            def __init__(self, t): self.text = t
        class MessageItem:
            type = "message"
            def __init__(self): self.content = [MessageContent("final answer text")]
        class ResponsesResp:
            output = [ReasoningItem(), MessageItem()]

        class FakeResp:
            output = [ReasoningItem(), MessageItem()]

        _mock_store(monkeypatch, captured)

        kwargs = {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "Qwen/Qwen3.5-122B-A10B-FP8",
            "call_type": "aresponses",
            "litellm_params": {
                "metadata": {},
                "litellm_metadata": {
                    "model_group": "memmachine-llm-medium",
                    "user_api_key_user_email": "alice@example.com",
                },
                "proxy_server_request": {"body": {"model": "memmachine-llm-medium"}},
            },
            "user": "alice@example.com",
        }
        _run(_make_handler().async_log_success_event(kwargs, FakeResp(), None, None))

        assert "payload" in captured
        msgs = captured["payload"]["messages"]
        assert msgs[0]["content"] == "hi"
        # assistant text is the final message, NOT the reasoning
        assert msgs[1]["producer"] == h.AGENT_ID
        assert msgs[1]["content"] == "final answer text"

    def test_extract_responses_assistant_text_picks_message_only(self):
        """_extract_responses_assistant_text must skip reasoning items and join message text."""
        class C:
            def __init__(self, t): self.text = t
        class ReasoningItem:
            type = "reasoning"
            def __init__(self): self.content = [C("thinking")]
        class MessageItem:
            type = "message"
            def __init__(self): self.content = [C("answer1"), C("answer2")]
        class Resp:
            output = [ReasoningItem(), MessageItem()]

        assert h._extract_responses_assistant_text(Resp()) == "answer1\nanswer2"

    def test_extract_responses_assistant_text_empty_when_no_message(self):
        class ReasoningItem:
            type = "reasoning"
            def __init__(self): self.content = []
        class Resp:
            output = [ReasoningItem()]
        assert h._extract_responses_assistant_text(Resp()) == ""
        assert h._extract_responses_assistant_text(None) == ""

    def test_to_org_id_slugify_rules(self):
        assert h._to_org_id("alice@example.com") == "alice_example_com"
        assert h._to_org_id("alice") == "alice"
        assert h._to_org_id("7f3c-1a2b-9a2b") == "7f3c-1a2b-9a2b"
        assert h._to_org_id("a.b+c") == "a_b_c"
        assert h._to_org_id("") == "default"
        assert h._to_org_id("@@") == "default"


# ====================================================================
# Live tests: against the real MemMachine server at MEMMACHINE_BASE_URL
# ====================================================================
@pytest.mark.skipif(not LIVE, reason="MEMMACHINE_LIVE!=1")
@pytest.mark.skipif(not LIVE_BASE_URL, reason="MEMMACHINE_BASE_URL not set")
class TestLive:
    PROJ = "hook"

    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch):
        # live tests reset the hook globals to the test namespace
        monkeypatch.setattr(h, "BASE_URL", LIVE_BASE_URL)
        monkeypatch.setattr(h, "PROJECT_ID", self.PROJ)

    def test_live_add_then_search_then_inject(self):
        """Real-server add -> search -> inject flow."""
        user = "live-user-bob"
        org = h._to_org_id(user)  # == "live-user-bob"
        # 1) add (direct API call) — store into the user's org
        add_payload = {
            "org_id": org, "project_id": self.PROJ, "types": [],
            "messages": [
                {"content": "My name is Bob and I like jajangmyeon.", "producer": user, "produced_for": h.AGENT_ID,
                 "role": "user", "metadata": {"user_id": user, "session_id": "ls1", "agent_id": h.AGENT_ID}},
                {"content": "Bob, I'll remember that you like jajangmyeon.", "producer": h.AGENT_ID, "produced_for": user,
                 "role": "assistant", "metadata": {"user_id": user, "session_id": "ls1", "agent_id": h.AGENT_ID}},
            ],
        }
        with httpx.Client(timeout=20) as c:
            r = c.post(f"{h.BASE_URL}/api/v2/memories", json=add_payload, headers=h._headers())
            assert r.status_code == 200, r.text
            uids = [x.get("uid") for x in r.json().get("results", [])]
            assert len(uids) == 2

        # wait for indexing
        import time
        time.sleep(3)

        # 2) hook pre_call flow (real search) -> inject
        # _identity derives user_id=user, org_id=live-user-bob
        data = {
            "messages": [{"role": "user", "content": "what food do I like?"}],
            "model": "memmachine-m", "proxy_server_request": {"headers": {}}, "user": user,
        }
        out = _run(_make_handler().async_pre_call_hook(FakeUserApiKey(user), None, data, "completion"))
        msgs = out["messages"]
        injected = [m for m in msgs if m["role"] == "system" and h.MEMORY_MARKER in m["content"]]
        assert injected, "memory block must be injected"
        assert any("jajangmyeon" in m["content"] for m in injected), "Bob's jajangmyeon preference must surface from memory"

        # 3) cleanup — remove all leftover episodes in this user's org
        with httpx.Client(timeout=20) as c:
            r2 = c.post(f"{h.BASE_URL}/api/v2/memories/search", json={
                "org_id": org, "project_id": self.PROJ, "query": ".", "top_k": 50,
                "types": ["episodic"], "filter": f"producer_id='{user}'"}, headers=h._headers())
            for e in r2.json()["content"]["episodic_memory"]["long_term_memory"]["episodes"]:
                c.post(f"{h.BASE_URL}/api/v2/memories/episodic/delete",
                       json={"org_id": org, "project_id": self.PROJ, "episodic_id": str(e["uid"])},
                       headers=h._headers())
            # also clean up assistant utterances (producer=AGENT_ID, produced_for=user)
            r3 = c.post(f"{h.BASE_URL}/api/v2/memories/search", json={
                "org_id": org, "project_id": self.PROJ, "query": ".", "top_k": 50,
                "types": ["episodic"], "filter": f"produced_for_id='{user}'"}, headers=h._headers())
            for e in r3.json()["content"]["episodic_memory"]["long_term_memory"]["episodes"]:
                if e.get("producer_id") == h.AGENT_ID:
                    c.post(f"{h.BASE_URL}/api/v2/memories/episodic/delete",
                           json={"org_id": org, "project_id": self.PROJ, "episodic_id": str(e["uid"])},
                           headers=h._headers())

    def test_live_user_isolation(self):
        """Searching alice's memories must not mix in bob's memories.

        New isolation model: each user stores memories in their own org
        (alice -> org=live-iso-alice, bob -> org=live-iso-bob). The org_id itself
        guarantees separation, independent of the producer_id filter.
        """
        alice, bob = "live-iso-alice", "live-iso-bob"
        org_a, org_b = h._to_org_id(alice), h._to_org_id(bob)
        with httpx.Client(timeout=20) as c:
            for user, fact in [(alice, "Alice likes purple"), (bob, "Bob likes orange")]:
                org = h._to_org_id(user)
                c.post(f"{h.BASE_URL}/api/v2/memories", json={
                    "org_id": org, "project_id": self.PROJ, "types": [],
                    "messages": [{"content": fact, "producer": user, "produced_for": h.AGENT_ID,
                                  "role": "user", "metadata": {"user_id": user, "session_id": "iso", "agent_id": h.AGENT_ID}}],
                }, headers=h._headers())
        import time; time.sleep(3)

        # search alice's org -> only purple, no orange (orange lives in a different org)
        r = httpx.post(f"{h.BASE_URL}/api/v2/memories/search", json={
            "org_id": org_a, "project_id": self.PROJ, "query": "favorite color",
            "top_k": 5, "types": ["episodic"]},
            headers=h._headers(), timeout=20)
        assert r.status_code == 200, r.text
        eps = r.json()["content"]["episodic_memory"]["long_term_memory"]["episodes"]
        contents = " ".join(e["content"] for e in eps)
        assert "purple" in contents
        assert "orange" not in contents, "bob's memories must not leak into alice's search"

        # cleanup — each user's org separately
        with httpx.Client(timeout=20) as c:
            for user, org in [(alice, org_a), (bob, org_b)]:
                r2 = c.post(f"{h.BASE_URL}/api/v2/memories/search", json={
                    "org_id": org, "project_id": self.PROJ, "query": "color", "top_k": 10,
                    "types": ["episodic"], "filter": f"producer_id='{user}'"}, headers=h._headers())
                for e in r2.json()["content"]["episodic_memory"]["long_term_memory"]["episodes"]:
                    c.post(f"{h.BASE_URL}/api/v2/memories/episodic/delete",
                           json={"org_id": org, "project_id": self.PROJ, "episodic_id": str(e["uid"])},
                           headers=h._headers())
