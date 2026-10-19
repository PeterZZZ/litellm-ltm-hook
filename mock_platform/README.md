# Mock MemMachine platform

A stand-in for a MemMachine tenant control server. It sits between the LiteLLM
hook and MemMachine core: the hook proposes who the person is and which
namespace they want, and the platform verifies both before anything reaches
core. It exists to pin down the contract between the hook and a future
platform; it is not that platform.

```
            Corp SSO (Keycloak): one sign-in for Open WebUI and the portal
                 │ employee_id claim → Open WebUI user name
Open WebUI ──(shared app key + signed user token)──┐
Claude Code ──(personal virtual key)───────────────┤
                                                   ▼
                                 LiteLLM (hook, MEMMACHINE_PLATFORM=1)
                                   proposes person + namespace, forwards evidence
                                                   │  Authorization: Bearer <service credential>
                                                   │  X-MemMachine-Key-Id / -Key-Owner
                                                   │  X-MemMachine-User / -User-Assertion
                                                   │  X-MemMachine-Project / -Client, X-Request-Id
                                                   │  body: org_id, project_id
                                                   ▼
                                 mock platform  — authorize(), verify, forward
                                                   ▼
                                 MemMachine core /api/v2
```

## Who checks what

| Step | Who | What it proves |
|---|---|---|
| The person signs in | Corp SSO (Keycloak), or the developer's own key | — |
| The virtual key is valid | LiteLLM | The request may use LiteLLM |
| The caller is a known LiteLLM | **Platform**, by service credential | The key id and token below came from a deployment it trusts |
| The key belongs to a person | **Platform**, by registry lookup | Personal key → that person; LiteLLM's owner must agree |
| The token was minted by this key's Open WebUI | **Platform**, with that app's secret | App key + valid token → the token's email → the person |
| The hook named the right person | **Platform** | `X-MemMachine-User` is the person's employee ID (or email), and the token carries it |
| The person has memory on | **Platform**, by registry flag | Otherwise "not enrolled" |
| Namespace | **Platform** checks the hook's proposal | `org_id` is the person's, the project is granted |

The hook never sees a raw virtual key: LiteLLM strips `Authorization` from the
headers it gives callbacks and passes the key only as its SHA-256. The hash is
an identifier, not a secret (it appears in LiteLLM's spend logs), so a key id
is believed only when a registered service credential vouches for it.

## The person: employee ID, else email

A person is named by their **employee ID**, which is also their org
(`E12345/default`). Phase 1, without SSO, has no employee ID; the email stands
in, slugified to MemMachine's character set (`alice_corp_example/default`). The
API is the same either way; only the value the hook sends changes.

For Open WebUI, the hook's adapter reads the person from the signed token
(`MEMMACHINE_OWUI_USER_CLAIM`: `email` in phase 1, `name` with SSO, because Open
WebUI copies Keycloak's `employee_id` into the user's name). It reads without
verifying. The platform verifies the token and then checks the named person
against its registry. That matters: **Open WebUI lets users edit their own
name**, and signs whatever name they have. A user who renames themselves to a
colleague's employee ID gets a valid token naming that ID, and the platform
refuses it, because the token's email belongs to someone else (`named user is
not the verified person`). Signing in again resets the name from Keycloak.

For a personal key, the person is the key's owner. LiteLLM's `user_id` for the
key must be the person's employee ID (or email), as the Claude Code design
assumes.

## Rules (`authorize.py`)

| Situation | Answer |
|---|---|
| Unknown or missing service credential | 401 |
| No key id | 401 |
| Key not registered | 403 `X-MemMachine-Decision: not-enrolled` |
| Personal key | the key's owner |
| Personal key **and** a user token | 401: tokens are honoured only on their app's key |
| Personal key naming someone else, or LiteLLM naming another owner | 403 |
| Personal key switched off by its owner, or Open WebUI access switched off | 403 not-enrolled |
| App key without a token | 401 |
| App key, token fails signature / issuer / expiry / has no email | 401 |
| App key, token's email not registered | 403 not-enrolled |
| App key without `X-MemMachine-User` | 401 |
| `X-MemMachine-User` not in the token, or not the token's person | 403 |
| User disabled | 403 not-enrolled |
| Body `org_id` not the person's, project not granted, or project header disagrees | 403 |

The hook treats **not-enrolled** as "memory is off for this person": the chat
goes on without memory and nothing is stored. Any other refusal is no memory
too, never a failed chat: on search, nothing is injected; a store refused with
403 is dropped, one refused with 401 (a setup problem) is kept in the hook's
dead-letter file for replay. If the platform is down, the chat goes on without
memory and stores are retried.

On store, the token is judged at the time the request started (the hook sends
`X-MemMachine-Request-Started`, accepted up to 15 minutes back), so a
short-lived token that expires while the model answers still lets the turn be
stored. A kept store whose token has expired by the time it is replayed is
dropped.

## Registry

One namespace per person, `org = employee ID` (or the email slug),
`project_id = default`. Emails are matched without regard to case. No shared
namespaces. Memory stored under an org stays there if the person's employee ID
changes: core cannot move it.

```bash
A="python -m mock_platform.admin --db registry.sqlite"
$A service add litellm-prod                     # prints the service credential once
$A user add alice@corp.example --employee-id E12345
$A key personal alice@corp.example --key-env ALICE_VIRTUAL_KEY --label alice-personal
$A key app --key-env OPENWEBUI_APP_KEY --label open-webui-app \
           --secret-env FORWARD_USER_INFO_HEADER_JWT_SECRET
$A user disable alice@corp.example              # memory off for alice, chat unaffected
$A show
```

Keys and secrets are read from environment variables, never from arguments.
A key may be given raw or as LiteLLM's SHA-256.

## Corp SSO (mock Keycloak)

`tools/keycloak_setup.py` builds the realm in a local Keycloak: an
`employee_id` user attribute only admins can edit, the users, and two
confidential clients, `open-webui` and `memmachine-platform`, both with an
`employee_id` claim. The platform's client also has a read-only service account
(`realm-management/view-users`) for employee-ID lookups.

```bash
KEYCLOAK_ADMIN=... KEYCLOAK_ADMIN_PASSWORD=... ACCOUNTS_FILE=accounts.txt \
SECRETS_OUT=keycloak-clients.env python3 tools/keycloak_setup.py
```

Open WebUI:

```bash
OPENID_PROVIDER_URL=http://127.0.0.1:8180/realms/corp/.well-known/openid-configuration
OAUTH_CLIENT_ID=open-webui
OAUTH_CLIENT_SECRET=<from keycloak-clients.env>
OAUTH_PROVIDER_NAME="Corp SSO"
OAUTH_SCOPES="openid email profile"
OAUTH_USERNAME_CLAIM=employee_id      # the user's name becomes the employee ID
OAUTH_UPDATE_NAME_ON_LOGIN=true       # and is reset from Keycloak at every sign-in
OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true
ENABLE_OAUTH_SIGNUP=true
ENABLE_LOGIN_FORM=false               # seeds the config; an existing install keeps
                                      # its stored ui.enable_login_form
```

## Running it

```bash
uv sync --group platform
MOCK_PLATFORM_DB=registry.sqlite MOCK_PLATFORM_CORE_URL=http://127.0.0.1:8090 \
MOCK_PLATFORM_OIDC_ISSUER=http://127.0.0.1:8180/realms/corp \
MOCK_PLATFORM_OIDC_CLIENT_ID=memmachine-platform \
MOCK_PLATFORM_OIDC_CLIENT_SECRET=<from keycloak-clients.env> \
  uv run uvicorn mock_platform.app:app --port 8100
```

Without the `OIDC` variables, set `MOCK_PLATFORM_OWUI_URL` instead and the
portal checks passwords against Open WebUI (phase 1).

And on the LiteLLM side:

```bash
MEMMACHINE_PLATFORM=1
MEMMACHINE_BASE_URL=http://127.0.0.1:8100
MEMMACHINE_API_KEY=<the service credential>
MEMMACHINE_OWUI_USER_CLAIM=name       # with SSO; email (the default) without
# FORWARD_USER_INFO_HEADER_JWT_SECRET must be unset: the platform holds it now
```

Open WebUI keeps `ENABLE_FORWARD_USER_INFO_HEADERS=true` and its
`FORWARD_USER_INFO_HEADER_JWT_SECRET`, which goes to the platform (`key app
--secret-env`) instead of to LiteLLM.

## The portal: access first, memory second

The platform serves a console at `/portal/`. With SSO configured, "Sign in with
Corp SSO" runs the OpenID Connect authorization-code flow against Keycloak: the
portal checks the ID token's RS256 signature against Keycloak's published keys,
its issuer, audience, expiry and the nonce it made for that sign-in, and the
state ties the returning browser to the one that left. Signing out also ends the
Keycloak session. What someone may do here comes from the **platform's** role
(`user` or `admin`), never from Keycloak or Open WebUI.

**Everyone, about themselves (Access tab):**

- who they are, how that was established, their employee ID, role and namespace;
- their namespaces, with memory on/off, stored count, export and delete-all
  (one personal namespace for now; more come with per-project grants);
- every way into their memory, with a switch each: Open WebUI (the app key plus
  their signed token) and each personal LiteLLM key. A switched-off way gets
  "not enrolled": that client keeps working without memory;
- their access history: every decision the platform made about their memory.

The Memories and "What would it recall?" tabs are unchanged.

**Admins, about everyone (Admin tab):**

- create an account by email. The platform takes the employee ID from Corp SSO
  (and refuses an email SSO does not know), and with `MOCK_PLATFORM_LITELLM_URL`
  and `MOCK_PLATFORM_LITELLM_KEY` (a LiteLLM `proxy_admin_viewer` key, read-only)
  finds that person's personal keys in LiteLLM itself: users with that email, or
  whose `user_id` is the employee ID. Team and blocked keys are skipped. "Sync"
  repeats both lookups, updates the employee ID, and unlinks keys LiteLLM no
  longer has;
- delete an account (optionally with its memories), switch memory, change role.
  An admin cannot delete or demote themselves;
- what the platform trusts: LiteLLM deployments, apps with signed tokens, Corp
  SSO, and the LiteLLM lookup;
- the audit log: every authorization decision and admin action, filterable.

Bootstrap the first admin with the CLI: `user add admin@corp.example --role admin
--employee-id E10000`. There is no self-enrolment: a person without an account is
told who the admins are.

Every route acts on the signed-in person's namespace, taken from the registry,
never from the browser; deletes re-check each id against that namespace first.
Sessions live in memory (a restart signs everyone out) in an HttpOnly,
SameSite=Strict cookie, and writes also need an `X-MM-Portal` header.

## What a mock leaves out

- **Core is still reachable directly.** The platform only helps if core accepts
  traffic from the platform alone; on a shared demo host it cannot be closed.
- **Open WebUI's token is HS256**: whoever verifies it can also mint it. Keycloak
  signs RS256, but Open WebUI does not forward Keycloak's token to LiteLLM.
- **The app's signing secret is stored as given** in SQLite. A real platform
  keeps it in a secret store.
- **The employee ID is a copy.** The registry keeps what Keycloak said at
  create or sync time; a change in Keycloak needs a sync.
- **Disabling is judged when each call arrives.** A store that lands after a
  person is re-enabled is stored.
- No shared namespaces, one namespace per person.
- **The portal has no rate limiting**, and pending sign-ins live in memory.
