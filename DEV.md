# DEV — implementation notes for `memmachine_hook.py`

These notes cover the current MemMachine hook in `litellm-ltm-hook`. They document details discovered during real-deployment debugging; future hooks may have different behavior and constraints. README points here instead of reproducing the content.

## 1. Reasoning models return `content=null`

Backend models like Qwen3.5-122B and Kimi-K3 are reasoning models. With a small `max_tokens`, the response's `choices[0].message.content` is `null` and the answer is in `reasoning_content`. The hook falls back to `reasoning_content` / `reasoning` when `content` is empty:

```python
assistant_text = _content_to_text(msg_content)
if not assistant_text.strip():
    reasoning = getattr(message, "reasoning_content", None) or ...
    assistant_text = _content_to_text(reasoning)
```

**Operational note**: clients must pass enough `max_tokens` to let reasoning finish and the final content emerge. If reasoning is truncated mid-thought, `content` stays `null` and the stored "assistant answer" is just a reasoning fragment.

## 2. `model_group_alias` resolves before the store hook runs

LiteLLM's router resolves `model_group_alias` **between** the pre-call hook and the store hook:

| Hook | `model` value | Example |
|------|---------------|---------|
| `async_pre_call_hook` | original requested name | `memmachine-llm-medium` |
| `async_log_success_event` | resolved backend name | `Qwen/Qwen3.5-122B-A10B-FP8` |

The prefix check in `_in_scope()` must use the **original** name or the store path would be silently skipped. The original name is preserved in `metadata.model_group`:

```python
model_name = data.get("model_group") or data.get("model") or kwargs.get("model")
```

## 3. Store hook gets auth info as flattened fields, not an object

`async_pre_call_hook` receives `user_api_key_dict` (a `UserAPIKeyAuth` object) directly, so `getattr(user_api_key_dict, "user_email", None)` works. But `async_log_success_event` does **not** receive it — `kwargs`, `kwargs.litellm_params`, and `kwargs.litellm_params.metadata` all lack a `user_api_key_dict` key.

Instead, LiteLLM flattens the auth info into individual fields inside `kwargs.litellm_params.metadata`:

```
metadata keys: [..., 'user_api_key_user_id', ..., 'user_api_key_user_email',
              'user_api_key_hash', ...]
```

`_identity()` accepts the same authenticated email in either shape:

```python
getattr(user_api_key_dict, "user_email", None)  # pre-call path
metadata.get("user_api_key_user_email")         # store path (flattened)
```

It deliberately ignores request headers/body metadata, virtual-key `user_id` as an identity source. Missing email raises instead of falling through to a shared `"default"` user. The email is slugified and that canonical value is used by `producer`, `produced_for`, and the default personal org.

## 4. MemMachine API quirks (self-hosted, `/api/v2` prefix)

- Responses live under `body["content"]`, not at the top level.
- `org_id` / `project_id` only accept letters, digits, `_`, `-`, `:`, and Unicode (CJK). No `@`, `.`, `/`, or other symbols. `producer` / `produced_for` could accept raw emails, but the hook intentionally uses the same slugified virtual-key email as its canonical user ID.
- The hook deliberately omits `filter`: episodic memory is already isolated by the selected `org_id + project_id`, while `producer_id` records provenance inside that namespace. This preserves other participants' and assistant episodes in shared projects.
- The hook also omits semantic `set_metadata`, so MemMachine v0.3.9 uses its default org-set + project-set search scope. A message is associated with org, project, and producer-based user sets, but semantic extraction runs only for categories configured on each set; the v0.3.9 defaults configure `profile_prompt` for project sets and no default categories for org/user sets.
- Because no type-specific filter is needed, `episodic`, `semantic`, mixed, and all-type (`types:[]`) searches use the same single API call.
- `POST /api/v2/memories/search` can return 500 on edge/empty queries. The test/cleanup tools use `POST /api/v2/memories/list` (paginated, no vector search) instead — more reliable for "fetch all" operations.
- Self-hosted uses `/api/v2` prefix and has no top-level `user_id`/`agent_id` fields (the cloud `api.memmachine.ai` uses `/v2` and does — don't mix them up).

## 5. Injection placement (per route)

On all three routes the memory block is injected the same way: a `role:system` message **immediately before the last user message**, not at the front. Putting it at the front would change the prompt prefix every turn and invalidate the vLLM prefix cache for the whole prompt.

- **OpenAI Chat Completions** (`/v1/chat/completions`) and **Anthropic Messages** (`/v1/messages`): the list lives under `data["messages"]`.
- **OpenAI Responses** (`/v1/responses`): the list lives under `data["input"]` (string or list of `{role, content}`). The hook writes back to the same key it read from.

A client-supplied top-level `system` field (Anthropic) or `instructions` field (Responses) is left untouched — the memory block goes into the conversation list as a separate `role:system` entry, and LiteLLM passes both to the backend.

LiteLLM normalizes Anthropic messages to OpenAI shape (content as string) and `/v1/responses` `input` (string or list of `{role, content}`) to `messages` *before* the pre-call hook runs, so `_last_user_index` / `_content_to_text` are shared across all three routes.

## 6. Re-injection guard (and the per-turn "branch")

The injected `<memory>` block is **ephemeral to a single request**. It is added to `data["messages"]` (OpenAI Chat / Anthropic) or `data["input"]` (OpenAI Responses) inside `async_pre_call_hook`, sent to the LLM, and then discarded — LiteLLM does not persist the modified request anywhere, and the client only receives the assistant's answer, not the `<memory>` block. So the next request from the client carries the **clean** history without any `<memory>` block, and the hook searches and injects fresh again. Each turn produces a temporary "branch" of the prompt that lives only until the LLM answer returns.

This means **every turn re-searches and re-injects** under normal multi-turn flow. The re-injection guard in `async_pre_call_hook` (which skips re-injection if the incoming history already contains a `<memory>` block) only fires for an edge case: when the **client itself** sends back a history that already has a `<memory>` block (e.g. a client that caches the modified messages, or a replay of the same request). The guard's real job is to avoid *double* injection in that scenario — not to suppress per-turn re-search.

(Note: the vLLM prefix-cache trade-off described by this guard only matters for that edge case. The main flow re-searches every turn; the cache impact is bounded because the `<memory>` block is inserted just before the last user message, so the prefix up to `[user1, assistant1, …]` is still cacheable.)

## 7. Anthropic `/v1/messages` route quirks

The Anthropic route (`call_type="anthropic_messages"`) needs two fallbacks beyond the OpenAI Chat route:

- **Model name recovery**: `kwargs.litellm_params.metadata.model_group` is `None` on the Anthropic route. The original requested name lives in `litellm_params.litellm_metadata.model_group` (and also `litellm_params.proxy_server_request.body.model`). `async_log_success_event` checks them in that order before falling back to `kwargs.model` (the post-alias backend name).
- **Auth fields**: `user_api_key_user_email` lives in `litellm_params.metadata` on the OpenAI route and in `litellm_params.litellm_metadata` on the Anthropic route. `_identity()` reads from `meta` = `{**litellm_metadata, **metadata}` so both routes work.

## 8. Where LiteLLM's OpenAI-type normalization falls short

LiteLLM's promise is that any client protocol gets normalized to the OpenAI Chat Completions shape, so a hook can read `messages`/`choices` once. That holds for Anthropic, but **the OpenAI Responses route (`/v1/responses`) is normalized less completely than Anthropic**, which is counter-intuitive. Two gaps remain, both specific to the Responses route.

### Gap 1: request shape — `input` is not normalized to `messages` in `async_pre_call_hook`

Verified by probing `data` shape in `async_pre_call_hook` across all three routes:

| Route | pre_call `data` | store `kwargs` |
|-------|-----------------|----------------|
| `/v1/chat/completions` (`acompletion`) | `messages` (role=user, content=str) | `messages` |
| `/v1/messages` (`anthropic_messages`) | `messages` (role=user, content=str) — normalized from Anthropic shape | `messages` |
| `/v1/responses` (`aresponses`) | **`input`** (role=user, content=str) — **not normalized** | `messages` — normalized |

In the **store** hook, all three routes arrive as `kwargs["messages"]` and the shared logic works. In the **pre-call** hook, Chat Completions and Anthropic already have `messages` (Anthropic got normalized), but Responses still carries `input` un-normalized. That asymmetry is the only reason `_responses_input_to_messages()` exists — it's a LiteLLM-side gap, not a protocol difference. (The hook also writes the injected `role:system` block back to whichever key it read from — `messages` or `input` — so the injection itself is uniform.)

If LiteLLM normalized `/v1/responses` `input` → `messages` in `async_pre_call_hook` too (it already does so before the store hook), the hook could read `data["messages"]` uniformly and drop `_responses_input_to_messages()` along with the `is_responses` input-reading branch. Worth filing upstream.

### Gap 2: response shape — `ResponsesAPIResponse` is not normalized to `choices[0].message.content`

`response_obj` on the Responses route is a `ResponsesAPIResponse` with an `output` list (items typed `reasoning`, `message`, etc.), not a `ModelResponse` with `choices[0].message.content`. `_extract_responses_assistant_text()` walks `output`, skips `reasoning` items, and joins `content[].text` from `message` items. The Chat/Anthropic path keeps using `choices[0].message.content` (with the reasoning-content fallback from section 1).

If LiteLLM normalized the Responses response into the same `choices[0].message.content` shape it already produces for Chat/Anthropic, the hook could drop the `is_responses` response-parsing branch and `_extract_responses_assistant_text()`.

### Why the asymmetry?

Anthropic is a *different* protocol that LiteLLM actively translates to OpenAI shape, so the translation is applied uniformly. The Responses API *is* OpenAI's own newer format, so LiteLLM appears to treat it more passively — it normalizes `input` only at the store-hook boundary and leaves the `ResponsesAPIResponse` mostly intact, perhaps to preserve Responses-specific fields (`reasoning`, tool calls, `previous_response_id`, etc.) that don't map cleanly onto `choices`. The net effect for the hook is that the Responses route needs two small shims (request normalization in pre-call, response extraction in store) that the Anthropic route does not.

## 9. Failure policy

Search runs before the LLM call, so client/request errors can still be returned to the caller. Store runs in `async_log_success_event` after the LLM response and therefore cannot change that response.

| Failure | Search | Store |
|---|---|---|
| 400/401/403/422 and other non-404/408/429 4xx | Fail-closed with the same status and a sanitized message | Detect and log; fail-open |
| 404 | Treat as an empty/new namespace; fail-open | Detect and log; fail-open |
| 408 or client timeout | Treat as temporary unavailability; fail-open | Detect and log; fail-open |
| 429 | Treat as temporary unavailability; fail-open | Detect and log; fail-open |
| timeout/transport/5xx | Log and fail-open | Detect and log; fail-open |
| malformed/unexpected response | Log and fail-open | Log and fail-open |

LiteLLM 1.97.0 lets exceptions from `CustomLogger.async_pre_call_hook` propagate to its proxy request processor. That processor preserves the integer `status_code` attribute of a general exception when it constructs the OpenAI-compatible `ProxyException`. The hook therefore raises its dependency-free `MemMachineRequestError` for fail-closed search responses; importing FastAPI directly would break environments that install only LiteLLM core without proxy extras.

Failure logs use the standard `memmachine_hook.errors` logger independently of `MEMMACHINE_DEBUG`. They contain only operation, category, status, org/project, request ID, and exception type; upstream response bodies, queries, authorization headers, and keys are not logged. Inline retries are intentionally omitted: search retries add user-facing latency, while reliable store retries need a future durable queue.

## 10. Planned identity/Authz integration (deferred)

The PoC currently derives the canonical user from the virtual-key owner email and accepts `x-memmachine-org` / `x-memmachine-project` as unverified namespace selectors. The raw MemMachine API has no authorization layer yet.

Deferred design items:

- Reconsider a server-managed virtual-key metadata field such as `memmachine_username`. This would provide a GitHub-style handle independent of email; a user's default personal org would have the same handle. Until this is explicitly adopted, email remains the only identity source.
- When MemMachine Authz exists, associate each LiteLLM virtual key with a MemMachine Authz credential. A direct metadata secret is acceptable only as an initial PoC; replace it with an opaque credential reference plus a protected secret store before production.
- Route hook search/store calls through Authz, and restrict raw MemMachine API access to Authz. Authz must validate the associated credential against the requested org/project.
- Define an Authz preflight contract if write permission must be reported before the LLM call. Without preflight, a store-time 401/403 can only be logged because the response has already been delivered.
- Require Authz to use 401/403 for authentication/authorization failures. A 404 must mean an authorized but absent MemMachine namespace so the hook can safely treat it as empty memory.

## Known limitations

- **Claude Code user-message wrappers** — when `MEMMACHINE_CLAUDE_CODE_STRIP_SYSTEM_TAGS=1` (the default), requests with a `User-Agent` beginning `claude-cli/` have complete, line-leading paired XML-like blocks removed from the text used for memory search and storage. This is intentionally tag-name agnostic so Claude Code can add wrapper names without requiring a hook update. It does not mutate the prompt sent to the LLM, and it cannot distinguish an unwrapped synthetic request (such as an automatic recap) from a real user request.
- **Semantic memory availability** — semantic memory is currently `[]` on this deployment, so its enabled behavior is covered by mocked tests. The hook intentionally accepts MemMachine's default org-set + project-set search scope; enabling org-level categories may surface semantic features derived from other projects in the same org.
- **Episodic recall includes every producer in the namespace** — user, collaborator, and assistant episodes in the selected org/project are all eligible. `producer_id` remains stored for provenance and explicit administrative queries.
- **Org/project selectors are not authorized yet** — `x-memmachine-org` and `x-memmachine-project` are accepted directly for PoC use. Do not treat this as a production isolation boundary until the planned Authz gateway validates access.
- **Store path is async** — `async_log_success_event` runs after the response completes. If the LLM call fails, nothing is stored (intentional).
- **Store failures are fail-open** — the hook calls `raise_for_status()` and logs MemMachine 4xx/5xx responses through the standard error logger, but the exception is swallowed because the LLM response has already completed. Durable retry/alerting and optional Authz write preflight remain deferred above.
- **Streaming** — `async_log_success_event` is still called after streaming finishes. The pre-call injection is unaffected by streaming.
