# litellm-ltm-hook

Callback hooks for connecting the LiteLLM proxy to long-term memory (LTM) solutions.

The repository currently provides a hook for [MemMachine](https://github.com/MemMachine/MemMachine). Its broader scope allows hooks for other LTM solutions to be added if needed; no additional integrations are currently planned.

The setup, configuration, and behavior described below apply to the MemMachine hook.

## MemMachine hook

For requests handled by the MemMachine hook:

- **Before** the LLM call: relevant memories for the current user are searched and injected as a `<memory>` system block — a `role:system` message inserted right before the last user message, on all three routes (`/v1/chat/completions`, `/v1/messages`, `/v1/responses`).
- **After** the response: the user's message and the assistant's reply are stored back to the selected MemMachine `(org, project)` namespace.

The hook is a core plus one adapter per frontend (`memmachine_adapters/`): the core proposes who the person is, talks to the MemMachine Platform, and stores with retries; an adapter decides what a turn is for its frontend. **Claude Code** (`claude_code.py`) has its own turn model: one search per typed prompt, shared by every request of the turn, one stored pair per prompt, subagent hand-backs held until the turn is done. **Open WebUI and every other client** (`openwebui.py`) get one turn per request, as described here. Memory never fails a request: a refused or failed search means no memory for that prompt; a failed store is retried and then kept in a dead-letter file for `python memmachine_hook.py replay` (one the platform refuses with 403 is dropped).

Clients don't need to change — OpenAI Chat Completions (`/v1/chat/completions`), OpenAI Responses (`/v1/responses`), and Anthropic Messages (`/v1/messages`) all work. The hook talks to the MemMachine Platform, never to MemMachine directly: it proposes who the person is (the virtual key's owner, or — for an application that reaches LiteLLM through one shared virtual key — the user named in Open WebUI's signed token) and the project (`x-memmachine-project`, else `MEMMACHINE_PROJECT_ID`), and the Platform decides.

## Files

| Path | Purpose |
|------|---------|
| `memmachine_hook.py` | MemMachine callback hook: the core (identity, Platform client, search, store, retries, dead letters, trace). Its docstring lists every setting |
| `memmachine_adapters/` | One adapter per frontend: `claude_code.py` (Claude Code's turn model), `openwebui.py` (one request = one turn; Open WebUI and every other client) |
| `test_hook.py`, `fixtures/` | Unit tests (httpx-mocked): both adapters, identity, refusals, retries, replay; recorded Claude Code traffic |
| `tools/litellm-admin.sh` | LiteLLM admin: people and their virtual keys (add, rotate, block, remove), Open WebUI's shared key, the Claude Code settings block |
| `test_e2e_store.py` | E2E: store path through the litellm proxy → MemMachine |
| `test_e2e_recall.py` | E2E: 2-turn recall through the litellm proxy → MemMachine |
| `tools/cleanup_memories.py` | Utility: delete all episodic memories in a MemMachine project |
| `mock_platform/` | Mock MemMachine platform: authenticates the calling LiteLLM and decides identity and namespace (see its README) |
| `DEV.md` | MemMachine hook implementation notes & known limitations |
| `TESTS.md` | MemMachine hook test configuration and usage |

## MemMachine hook setup

### 1. Mount the hook into the LiteLLM container

The hook file and the `memmachine_adapters/` folder need to be on the LiteLLM proxy's Python import path, side by side, so the `callbacks` entry can import them. Standard pattern: mount both under `/app` (LiteLLM's working directory).

`docker-compose.yml` (snippet — keep your existing service config):
```yaml
services:
  litellm:
    volumes:
      - ./memmachine_hook.py:/app/memmachine_hook.py:ro
      - ./memmachine_adapters:/app/memmachine_adapters:ro
      - ./config.yaml:/app/config.yaml:ro
    command:
      - "--config"
      - "/app/config.yaml"
      - "--port"
      - "4000"
    environment:
      MEMMACHINE_BASE_URL: "http://platform.example.com"   # the MemMachine Platform
      MEMMACHINE_API_KEY: "<service credential>"          # issued by the Platform
      MEMMACHINE_PROJECT_ID: "default"
      MEMMACHINE_AGENT_ID: "litellm"
      MEMMACHINE_TOP_K: "8"
      MEMMACHINE_TIMEOUT: "10"
      MEMMACHINE_SEARCH_TYPES: "episodic"
      MEMMACHINE_MODEL_PREFIX: "memmachine-"
      MEMMACHINE_DEBUG: "0"          # set "1" when debugging
```

### 2. Register the callback in `config.yaml`

```yaml
litellm_settings:
  callbacks:
    - memmachine_hook.memmachine_handler
```

If your existing config already has callbacks, add this entry to the list — don't replace.

### 3. Route only specific models through the memory layer

By default (`MEMMACHINE_MODEL_PREFIX=memmachine-`), only model names starting with `memmachine-` get the hook. To expose a "memory-enabled" variant of a model without duplicating the model definition, add a `model_group_alias` in the proxy's `router_settings` (via config.yaml or the admin UI):

```yaml
router_settings:
  model_group_alias:
    "memmachine-llm-medium": "Qwen/Qwen3.5-122B-A10B-FP8"   # one line per model
```

Clients calling `llm-medium` get the plain model; clients calling `memmachine-llm-medium` get the same backend with the memory hook active.

> Note: the hook detects the prefix **before** the proxy resolves the alias. For store events (`async_log_success_event`), LiteLLM has already resolved the alias (`kwargs.model` is the backend name), so the hook looks up `metadata.model_group` to recover the original requested name. See "Design notes" below.

### 4. Restart LiteLLM

```bash
docker compose up -d
docker logs -f <litellm-container>   # look for "Initialized Callbacks" with MemMachineHook
```

## Environment variables

The main ones; `memmachine_hook.py`'s docstring has every setting (store retries, dead-letter path, trace, Claude Code's options). Names in brackets are older names from the Claude Code side, still read when the main name is unset.

| Var | Default | Notes |
|-----|---------|-------|
| `MEMMACHINE_BASE_URL` | — (required, no default) | The MemMachine Platform's URL. Must be set in `.env` or the real environment; the hook refuses to start without it. |
| `MEMMACHINE_API_KEY` [`MEMMACHINE_SERVICE_CREDENTIAL`] | — (required) | This LiteLLM's service credential, issued by the Platform; the hook refuses to start without it |
| `MEMMACHINE_PROJECT_ID` [`MEMMACHINE_DEFAULT_PROJECT`] | `default` | Default project when `x-memmachine-project` is absent |
| `MEMMACHINE_AGENT_ID` | `litellm` | Used as `producer` for assistant utterances (Open WebUI adapter) |
| `MEMMACHINE_CLAUDE_CODE_AGENT_ID` | `claude-code` | The Claude Code adapter's agent id and `X-MemMachine-Client` |
| `MEMMACHINE_TOP_K` | `8` | Search result count |
| `MEMMACHINE_TIMEOUT` [`MEMMACHINE_SEARCH_TIMEOUT`] | `10` | Seconds a search may take before the prompt goes on without memory (stores: `MEMMACHINE_STORE_TIMEOUT`, 30) |
| `MEMMACHINE_MODELS` | (empty) | Optional comma-separated allowlist; empty = rely on prefix only |
| `MEMMACHINE_MODEL_PREFIX` | `memmachine-` | Only model names with this prefix get the hook; empty string disables the check (applies to all models) |
| `MEMMACHINE_SEARCH_TYPES` | `episodic` | `episodic`, `semantic`, or a comma-separated combination. `all`, `*`, empty, and `[]` request all types; unknown values fail at startup |
| `MEMMACHINE_DEBUG` | (off) | `1` enables verbose stderr diagnostics. Failures always use the standard logger regardless of this setting. |
| `MEMMACHINE_FORWARDED_USER_JWT_HEADER` [`MEMMACHINE_USER_ASSERTION_HEADER`] | `x-openwebui-user-jwt` | Header carrying Open WebUI's signed user token |
| `MEMMACHINE_OWUI_USER_CLAIM` [`MEMMACHINE_USER_ASSERTION_CLAIM`] | `email` | The token claim naming the person (`name` with Corp SSO) |

`FORWARD_USER_INFO_HEADER_JWT_SECRET` (Open WebUI's signing secret) must **not** be set for LiteLLM: it belongs to Open WebUI and the Platform, and the hook refuses to start with it (or with `OPENWEBUI_USER_JWT_SECRET` or a `PLATFORM_FRONTEND_SECRET_*` variable) in its environment.

## How user identity works

The hook does not decide identity or namespace; the Platform does. The adapters
only decide what a turn is. For each in-scope request the hook speaks the shared
Platform API (`X-MemMachine-*` headers). It proposes:

- the person: the user named by Open WebUI's signed token (the
  `MEMMACHINE_OWUI_USER_CLAIM` claim, read without checking the token), else the
  virtual key's owner in LiteLLM. A key with no owner, or a team key with no
  token, gets no memory;
- the org: that person's ID, with characters MemMachine IDs do not allow replaced
  by `_` and leading or trailing `_` removed (the Platform's `slug` rule);
- the project: `x-memmachine-project`, else `MEMMACHINE_PROJECT_ID`.

and forwards the virtual key's hash and owner, Open WebUI's signed token as is,
and its own service credential. Stores also carry `X-MemMachine-Request-Started`,
so the Platform judges a token at the time the request began. The Platform
verifies all of it: the service credential, the key, the token's signature, and
whether the person may use the project. Its refusal means no memory for that
request; a `403` with `X-MemMachine-Decision: not-enrolled` means memory is off
for that person, and the store is dropped.

Open WebUI reaches LiteLLM through one shared virtual key, so the key names the
application, not the person; its signed token (`ENABLE_FORWARD_USER_INFO_HEADERS=true`
in Open WebUI) is what tells one person from the next. An unsigned header such as
`x-openwebui-user-email` is never used to name the person.

Per-request controls:

- `x-memmachine-project: <project>` — request another project; defaults to `MEMMACHINE_PROJECT_ID`. The Platform decides whether it is granted
- `x-memmachine-disable: 1` — skip the hook for this request
- `x-memmachine-client: claude-code` or `openwebui` — the frontend naming itself: the hook uses that adapter (the turn model) and tells the Platform that label. Optional (without it, or with another name, each adapter recognises its own requests); never identity

A token is a bearer credential for that user's memory until it expires, and it
travels as a request header, so it reaches anything LiteLLM passes request
headers to — including logging callbacks such as Langfuse and spend logs. Keep
the token lifetime short, and treat logs that capture request headers
accordingly.

`mock_platform/` is a minimal Platform for development, with a mock Keycloak for
SSO; see [`mock_platform/README.md`](./mock_platform/README.md). To build the
whole test stack (Keycloak, LiteLLM, Open WebUI, Platform) on a host, follow
[`SETUP.md`](./SETUP.md).

## Design notes

Load-bearing implementation details (reasoning-model fallback, `model_group_alias` timing, flattened auth fields, MemMachine API quirks, injection placement, re-injection guard) and current limitations live in [`DEV.md`](./DEV.md). README keeps only what an operator needs to deploy and verify.

## Testing

See [`TESTS.md`](./TESTS.md) for full env-var references and examples. Quick reference:

```bash
# unit (no MemMachine needed)
uv run pytest test_hook.py -q

# e2e store path (needs litellm proxy + virtual key + MemMachine up)
LITELLM_MASTER_KEY=sk-... LITELLM_TEST_VIRTUAL_KEY=sk-... \
MEMMACHINE_TEST_EXPECTED_USER=alice@example.com \
python3 test_e2e_store.py

# e2e 2-turn recall
LITELLM_TEST_VIRTUAL_KEY=sk-... python3 test_e2e_recall.py

# wipe all episodic memories in a project (paginated list → delete by uid)
python3 tools/cleanup_memories.py            # --dry-run, --keep USER supported
```

Note: `test_e2e_store.py` verifies storage via `POST /api/v2/memories/list` (not `/search`) — the search endpoint can 500 on edge queries, and list is more reliable for a "did it store?" check.

## Verifying the deployment

After restart, sanity-check in order:

1. **LiteLLM startup log** — `docker logs <litellm>` should show `Initialized Callbacks` containing a `memmachine_hook.MemMachineHook` instance, with no import errors.
2. **2-turn recall** — send a fact, then ask a question that needs it:
   ```bash
   curl http://litellm.example.com/v1/chat/completions \
     -H "Authorization: Bearer <VIRTUAL_KEY>" -H "Content-Type: application/json" \
     -d '{"model":"memmachine-llm-medium","messages":[{"role":"user","content":"I like cats. Remember it."}]}'
   # wait ~5s for indexing
   curl http://litellm.example.com/v1/chat/completions \
     -H "Authorization: Bearer <VIRTUAL_KEY>" -H "Content-Type: application/json" \
     -d '{"model":"memmachine-llm-medium","messages":[{"role":"user","content":"What animal do I like?"}]}'
   ```
   The second response should mention cats.
3. **Logs** — failures are emitted through the standard Python logger even when debug is off. Set `MEMMACHINE_DEBUG=1` temporarily for verbose hook traces such as `search ok` and `store ok`; `MEMMACHINE_TRACE` (on by default) logs one line per memory step (turn, search, store) with LiteLLM's request id, ids only.

## References

- LiteLLM call hooks: <https://docs.litellm.ai/docs/proxy/call_hooks>
- LiteLLM custom callbacks (`CustomLogger`): <https://docs.litellm.ai/docs/observability/custom_callback>
- MemMachine REST API: <https://docs.memmachine.ai/api_reference>
- MemMachine GitHub: <https://github.com/MemMachine/MemMachine>
- Self-hosted OpenAPI spec: `GET http://memmachine.example.com/openapi.json`

## License

[Apache License 2.0](LICENSE)
