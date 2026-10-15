# Mock MemMachine platform

A stand-in for a MemMachine tenant control server. It sits between the LiteLLM
hook and MemMachine core, and it — not the hook — decides who the person is and
which memory namespace they get. It exists to pin down the contract between the
hook and a future platform; it is not that platform.

```
Open WebUI ──(shared app key + signed user token)──┐
Claude Code ──(personal virtual key)───────────────┤
                                                   ▼
                                 LiteLLM (hook, MEMMACHINE_PLATFORM=1)
                                   forwards evidence, decides nothing
                                                   │  Authorization: Bearer <service credential>
                                                   │  X-MM-Key-Hash: <sha256 of the virtual key>
                                                   │  X-MM-User-Token: <Open WebUI token, unverified>
                                                   ▼
                                 mock platform  — authorize(), rewrite, forward
                                                   ▼
                                 MemMachine core /api/v2
```

## Who checks what

| Step | Who | What it proves |
|---|---|---|
| The person signs in | Open WebUI (SSO) or the developer's own key | — |
| The virtual key is valid | LiteLLM | The request may use LiteLLM |
| The caller is a known LiteLLM | **Platform**, by service credential | The key hash and token below came from a deployment it trusts |
| The key belongs to a person | **Platform**, by registry lookup | Personal key → that person |
| The token was minted by this key's Open WebUI | **Platform**, with that app's secret | App key + valid token → the token's person |
| The person has memory on | **Platform**, by registry flag | Otherwise "not enrolled" |
| Namespace, producer | **Platform** | Clients cannot choose either |

The hook never sees a raw virtual key: LiteLLM strips `Authorization` from the
headers it gives callbacks and passes the key only as its SHA-256. The hash is
an identifier, not a secret — it appears in LiteLLM's spend logs — so a key hash
is believed only when a registered service credential vouches for it. Trust in
the person's identity therefore rests on LiteLLM for personal keys, and on Open
WebUI's signature for app keys.

## Rules (`authorize.py`)

| Situation | Answer |
|---|---|
| Unknown or missing service credential | 401 |
| No key hash | 401 |
| Key not registered | 403 `X-MM-Decision: not-enrolled` |
| Personal key | the key's owner |
| Personal key switched off by its owner, or Open WebUI access switched off | 403 not-enrolled |
| Personal key **and** a user token | 401 — tokens are honoured only on their app's key, so a token cannot be replayed on someone else's key |
| App key without a token | 401 |
| App key, token fails signature / issuer / expiry / has no email | 401 |
| App key, token's email not registered | 403 not-enrolled |
| User disabled | 403 not-enrolled |

The hook treats **not-enrolled** as "memory is off for this person": the chat
goes on without memory and nothing is stored. Every other 401/403 reaches the
caller, as the hook's failure policy already does for core. If the platform is
down, the hook fails open as before.

On store, the token is judged at the time the request started (the hook sends
`X-MM-Request-Started`, accepted up to 15 minutes back), so a short-lived token
that expires while the model answers still lets the turn be stored.

## Registry

One namespace per person: `org_id = slug(email)`, `project_id = default` — the
same names the hook derives on its own, so memory carries over between modes.
Emails are matched without regard to case. No shared namespaces.

```bash
A="python -m mock_platform.admin --db registry.sqlite"
$A service add litellm-prod                     # prints the service credential once
$A user add alice@corp.example
$A key personal alice@corp.example --key-env ALICE_VIRTUAL_KEY --label alice-personal
$A key app --key-env OPENWEBUI_APP_KEY --label open-webui-app \
           --secret-env FORWARD_USER_INFO_HEADER_JWT_SECRET
$A user disable alice@corp.example              # memory off for alice, chat unaffected
$A show
```

Keys and secrets are read from environment variables, never from arguments.
A key may be given raw or as LiteLLM's SHA-256.

## Running it

```bash
uv sync --group platform
MOCK_PLATFORM_DB=registry.sqlite MOCK_PLATFORM_CORE_URL=http://127.0.0.1:8090 \
MOCK_PLATFORM_OWUI_URL=http://127.0.0.1:8080 \
  uv run uvicorn mock_platform.app:app --port 8100
```

And on the LiteLLM side:

```bash
MEMMACHINE_PLATFORM=1
MEMMACHINE_BASE_URL=http://127.0.0.1:8100
MEMMACHINE_API_KEY=<the service credential>
# FORWARD_USER_INFO_HEADER_JWT_SECRET must be unset: the platform holds it now
```

Open WebUI is unchanged: `ENABLE_FORWARD_USER_INFO_HEADERS=true` and its
`FORWARD_USER_INFO_HEADER_JWT_SECRET`, which goes to the platform (`key app
--secret-env`) instead of to LiteLLM.

## The portal: access first, memory second

With `MOCK_PLATFORM_OWUI_URL` set, the platform serves a console at
`/portal/`. People sign in with their Open WebUI account: the email and password
go to Open WebUI's own sign-in endpoint and are never stored, and the answer is
mapped through the registry like a chat token's email. What someone may do here
comes from the **platform's** role (`user` or `admin`), never from Open WebUI.

**Everyone, about themselves (Access tab):**

- who they are and how that was established, their role, their namespace;
- their namespaces, with memory on/off, stored count, export and delete-all
  (one personal namespace for now; more come with per-project grants);
- every way into their memory, with a switch each: Open WebUI (the app key plus
  their signed token) and each personal LiteLLM key. A switched-off way gets
  "not enrolled": that client keeps working without memory;
- their access history: every decision the platform made about their memory.

The Memories and "What would it recall?" tabs are unchanged.

**Admins, about everyone (Admin tab):**

- create an account by email. With `MOCK_PLATFORM_LITELLM_URL` and
  `MOCK_PLATFORM_LITELLM_KEY` (a LiteLLM `proxy_admin_viewer` key, read-only) the
  platform looks up that person's personal keys in LiteLLM itself and links
  them, so a key is never attached to the wrong owner. Team and blocked keys are
  skipped. "Sync keys" repeats the lookup and unlinks keys LiteLLM no longer has;
- delete an account (optionally with its memories), switch memory, change role.
  An admin cannot delete or demote themselves;
- what the platform trusts: LiteLLM deployments, apps with signed tokens, and
  the LiteLLM lookup;
- the audit log: every authorization decision and admin action, filterable.

Bootstrap the first admin with the CLI: `user add admin@corp.example --role admin`
(or `user role <email> admin`). There is no self-enrolment: a person without an
account is told who the admins are.

Every route acts on the signed-in person's namespace, taken from the registry,
never from the browser; deletes re-check each id against that namespace first.
Sessions live in memory (a restart signs everyone out) in an HttpOnly,
SameSite=Strict cookie, and writes also need an `X-MM-Portal` header.

## What a mock leaves out

- **Core is still reachable directly.** The platform only helps if core accepts
  traffic from the platform alone; on a shared demo host it cannot be closed.
- **The app's signing secret is stored as given** in SQLite. A real platform
  keeps it in a secret store.
- **Disabling is judged when each call arrives.** A store that lands after a
  person is re-enabled is stored.
- **No admin console** (the CLI stands in), no shared namespaces, one namespace per person.
- **The portal has no rate limiting** beyond a short delay after a failed sign-in.
