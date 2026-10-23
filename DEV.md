# DEV — implementation notes for `memmachine_hook.py`

These notes cover the current MemMachine hook in `litellm-ltm-hook`. They document details discovered during real-deployment debugging; future hooks may have different behavior and constraints. README points here instead of reproducing the content.

The hook is a core (`memmachine_hook.py`) plus adapters (`memmachine_adapters/`). Sections 1, 5, 6 and 8 describe the Open WebUI adapter (`openwebui.py`, one request = one turn, every client but Claude Code); Claude Code's adapter (`claude_code.py`) has its own turn model, described in its docstring and the core's. LiteLLM loads the hook by file path without registering the module, so the core registers a stand-in for itself before importing the adapters (`default_adapters()`); without it they would load a second copy.

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

The prefix check (`_in_scope()`) runs in the pre-call hook, on the original name; a turn that starts there is stored whatever the store event's `model` says. Where the requested name is needed after the call, it is in `metadata.model_group` (`request_model()`).

## 3. Store hook gets auth info as flattened fields, not an object

`async_pre_call_hook` receives `user_api_key_dict` (a `UserAPIKeyAuth` object) directly, so `getattr(user_api_key_dict, "user_email", None)` works. But `async_log_success_event` does **not** receive it — `kwargs`, `kwargs.litellm_params`, and `kwargs.litellm_params.metadata` all lack a `user_api_key_dict` key.

Instead, LiteLLM flattens the auth info into individual fields inside `kwargs.litellm_params.metadata`:

```
metadata keys: [..., 'user_api_key_user_id', ..., 'user_api_key_user_email',
              'user_api_key_hash', ...]
```

So identity is resolved once, in the pre-call hook, from `user_api_key_dict`, and kept with the turn until it is stored; the store event only finds the turn again (by the key's hash, `user_api_key_hash`, and LiteLLM's call id). `request_identity()` proposes the person to the Platform: the user named by Open WebUI's signed token, else the virtual key's owner (`user_id`). Request body metadata and unsigned headers are never identity sources. A request with no usable person gets no memory instead of falling through to a shared `"default"` user.

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
- **Auth fields**: `user_api_key_user_email` lives in `litellm_params.metadata` on the OpenAI route and in `litellm_params.litellm_metadata` on the Anthropic route. `request_meta()` reads from `{**litellm_metadata, **metadata}` so both routes work (the store event finds its turn by `user_api_key_hash` there).

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

Memory never fails the user's request, for every adapter: the core catches every search failure and every hook bug, and the request goes to the model unchanged.

| Failure | Search | Store |
|---|---|---|
| 401/403 (refused) | No memory for this prompt; logged once per key, project and status; not counted as an outage | 403: dropped (a later grant must not bring back what was said while refused). 401 (setup): kept in the dead-letter file for `replay` |
| 403 + `X-MemMachine-Decision: not-enrolled` (platform) | No memory: memory is off for this person | Dropped |
| 404 | An empty or new namespace: no memory, not an outage | Dead-lettered without retry |
| other 4xx | No memory; counts toward the circuit breaker | Dead-lettered without retry (the same request would fail again) |
| 408, 429, timeout, transport, 5xx | No memory; counts toward the circuit breaker (`MEMMACHINE_BREAKER_FAILURES` in a row skip searches for `MEMMACHINE_BREAKER_COOLDOWN`) | Retried with backoff (`MEMMACHINE_STORE_RETRIES`), then dead-lettered |
| malformed/unexpected response | No memory; logged | Retried, then dead-lettered |

The dead-letter file (`MEMMACHINE_DEAD_LETTER`, mode 0600) holds prompts and answers; `python memmachine_hook.py replay` stores them again (a 403, or a 401 for a frontend user whose signed token has expired, drops them). Stores held or in flight are also written to `<dead-letter>.pending`, so a restart does not lose them.

Failure logs use the standard `memmachine_hook.errors` logger independently of `MEMMACHINE_DEBUG`. They contain only operation, category, status, org/project, request ID, and exception type; upstream response bodies, queries, authorization headers, and keys are not logged.

## 10. Identity and authorization

The hook's search and store calls go through the MemMachine Platform, which authenticates the LiteLLM deployment (service credential), the virtual key and Open WebUI's signed token, and authorizes the requested org/project (`mock_platform/` in development). The hook never calls MemMachine directly.

Deferred design items:

- Define an Authz preflight contract if write permission must be reported before the LLM call. Without preflight, a store-time 401/403 can only be logged because the response has already been delivered.
- Require Authz to use 401/403 for authentication/authorization failures. A 404 must mean an authorized but absent MemMachine namespace so the hook can safely treat it as empty memory.

## Known limitations

- **Claude Code user-message wrappers** — Claude Code's requests go to its own adapter, which strips its wrapper blocks (`<system-reminder>` and the like) from the text used for memory search and storage, without changing the prompt sent to the LLM.
- **Semantic memory availability** — semantic memory is currently `[]` on this deployment, so its enabled behavior is covered by mocked tests. The hook intentionally accepts MemMachine's default org-set + project-set search scope; enabling org-level categories may surface semantic features derived from other projects in the same org.
- **Episodic recall includes every producer in the namespace** — user, collaborator, and assistant episodes in the selected org/project are all eligible. `producer_id` remains stored for provenance and explicit administrative queries.
- **Store path is async** — `async_log_success_event` runs after the response completes. If the LLM call fails, nothing is stored (intentional).
- **Store failures never reach the user** — the LLM response has already completed; failed stores are retried and dead-lettered (section 9). Alerting and an Authz write preflight remain deferred above.
- **Streaming** — `async_log_success_event` is still called after streaming finishes. The pre-call injection is unaffected by streaming.
