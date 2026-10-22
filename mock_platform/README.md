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
| Namespace | **Platform** checks the hook's proposal | `org_id` is the person's, the project is one of their grants |

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
| Key not registered, and LiteLLM does not say it is an enrolled person's (see below) | 403 `X-MemMachine-Decision: not-enrolled` |
| Key reconcile unlinked because LiteLLM reassigned it | 403 `key-reassigned` |
| Personal key | the key's owner |
| Personal key **and** a user token | 401: tokens are honoured only on their app's key |
| Personal key naming someone else | 403 |
| LiteLLM naming another owner for a personal key, or for an app key anyone but its service user | 403 `key-reassigned` |
| Personal key switched off by its owner, or Open WebUI access switched off | 403 not-enrolled |
| App key without a token | 401 |
| App key, token fails signature / issuer / expiry / has no email | 401 |
| App key, token's email not registered and not in Corp SSO either | 403 not-enrolled (someone SSO knows is enrolled on the spot and served) |
| App key without `X-MemMachine-User` | 401 |
| `X-MemMachine-User` not in the token, or not the token's person | 403 |
| User disabled | 403 not-enrolled |
| Body `org_id` not the person's | 403 |
| `X-MemMachine-Project` missing, or not the body's `project_id` | 400 |
| Project neither granted in the person's org nor the alias of a project shared with them | 403 `project-not-granted` |
| Any path or method other than the three below | 403 `endpoint-not-allowed` |

The platform serves exactly three core paths and forwards their bodies as
received, with the verified `org_id` and `project_id` in place of the proposed
ones: `POST /api/v2/memories/search`, `POST /api/v2/memories` (store) and
`POST /api/v2/memories/list`. Everything else, including deletes and project
administration, answers 403 rather than 404, so a caller cannot tell a hidden
endpoint from a forbidden one. The portal has its own path to core for a
person's own deletes.

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

One org per person, `org = employee ID` (or the email slug), and one or more
**projects** inside it: the person's grants. A new person gets `default`; the
single grant `*` means any project. The hook proposes a project per request
(`X-MemMachine-Project`, equal to the body's `project_id`; the Claude Code
adapter sends one per repository, the chat adapter `default`) and the platform
allows it only if granted, then passes `<org>/<project>` to core, creating the
project there before the first store. Emails are matched without regard to
case. Memory stored under an org stays there if the person's employee ID
changes: core cannot move it.

**Sharing.** A person shares a project of their own org with teammates, by
email or employee ID, from the portal; `default` is never shared. The member
sees it under its name (or `<project>-by-<owner>` if they already have that
name, and they can rename it) and uses it like any project: the hook sends
that name as `X-MemMachine-Project`, the platform resolves it to
`<owner org>/<project>` and core never knows the difference. Members read and
write; every message is stamped with its writer, so a member can delete only
what they wrote and the owner anything. The owner unshares, the member can
leave, and the owner removing the project ends every share; the next call is
`project-not-granted`. Someone Corp SSO knows but who has no account yet is
enrolled when shared with. No acceptance step, no email: the member's Access
tab shows the project as new on their next sign-in. The audit log records
`share`, `unshare` and `leave` under the member's email.

```bash
A="python -m mock_platform.admin --db registry.sqlite"
$A service add litellm-prod                     # prints the service credential once
$A user add alice@corp.example --employee-id E12345
$A key personal alice@corp.example --key-env ALICE_VIRTUAL_KEY --label alice-personal
$A key app --key-env OPENWEBUI_APP_KEY --label open-webui-app \
           --secret-env FORWARD_USER_INFO_HEADER_JWT_SECRET --owner open-webui
$A user grant alice@corp.example research       # alice may also use E12345/research
$A user ungrant alice@corp.example research
$A user list                                    # everyone, with org, grants and keys
$A user disable alice@corp.example              # memory off for alice, chat unaffected
$A show
```

Linking a personal key by hand is the exception. With `MOCK_PLATFORM_LITELLM_URL`
and `MOCK_PLATFORM_LITELLM_KEY` set, **a personal key the platform has never
seen is looked up in LiteLLM on its first call** (`GET /key/info` with the
read-only key) and, if LiteLLM's owner for it is an enrolled person, linked and
served on that same call: a rotated key just works, and inherits the person's
grants. The platform believes LiteLLM, never the caller: LiteLLM's `user_id`
must be the person as the platform names them (employee ID, or email without
one) and must agree with `X-MemMachine-Key-Owner`; a blocked, expired or team
key, or one owned by a service user, is not linked. An unknown owner stays
not-enrolled (`KeyLinker.person_for_unknown_owner` is where auto-creating a
person would go, pending Leo). A failed lookup is remembered for 60 s so a bad
key does not hit LiteLLM on every request.

**Reconcile** runs every `MOCK_PLATFORM_RECONCILE_SECONDS` (default 600) and
from the Admin tab's button: it re-reads `/key/list`, drops linked keys that
are deleted, blocked, expired or whose owner changed, and links new keys of
known people. A personal key unlinked because LiteLLM reassigned it is refused
with `key-reassigned` rather than linked to whoever LiteLLM now names, until an
admin syncs that person's account. An app key, shared by everyone behind the
app, is **suspended** instead: its registration stays, it is refused while
LiteLLM names anyone but its service user, and the next reconcile lifts the
suspension once that is put right.

Keys and secrets are read from environment variables, never from arguments.
A key may be given raw or as LiteLLM's SHA-256. `--owner` is the LiteLLM user
the app key belongs to (a service user such as `open-webui`, never a person);
every call on that key must still name it in `X-MemMachine-Key-Owner`, so an
admin reassigning the key to a person in LiteLLM is noticed, not trusted.

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
- their namespaces: one row per granted project (`<org>/<project>`), each with
  its stored count, export and delete-all; memory on/off is per person. They
  can add a project to their own org and remove one again (its memories stay;
  the default project stays granted). The Memories tab shows every project
  with a project filter; adding a memory and the recall preview pick one;
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
  longer has; "Reconcile keys with LiteLLM" does for everyone what the
  background job does every 10 minutes;
- delete an account (optionally with its memories), switch memory, change role,
  grant or remove a project for anyone (`*` for any project). An admin cannot
  delete or demote themselves;
- what the platform trusts: LiteLLM deployments, apps with signed tokens, Corp
  SSO, and the LiteLLM lookup;
- the audit log: every authorization decision and admin action, filterable.

Bootstrap the first admin with the CLI: `user add admin@corp.example --role admin
--employee-id E10000`.

**Enrolment on first use** (`MOCK_PLATFORM_AUTO_ENROL=1`, the default when Corp
SSO is configured): a person the registry does not know, arriving with a valid
Open WebUI token, a personal key whose LiteLLM owner names them, or a portal
sign-in, is looked up in the Corp SSO directory. Someone the directory knows
and has not disabled is created on the spot with the employee ID the directory
holds and a `default` project, and that same request is served. Signing in
through Corp SSO is what proves membership, so no admin step is needed; the
audit log says `enrol` and the Admin tab marks the account self-enrolled. A
failed lookup is remembered for 60 s. With `MOCK_PLATFORM_AUTO_ENROL=0`, or
without a directory, a person without an account gets no memory until an admin
creates one, and the portal tells them who the admins are.

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
- One org per person, however many projects; sharing is one role (read and
  write), no read-only and no project admins yet.
- **The portal has no rate limiting**, and pending sign-ins live in memory.
