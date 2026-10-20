# Setting up the platform-mode test stack

How to stand up the whole chain this branch was tested on, from an empty host:

```
browser ──► Keycloak :8180 (Corp SSO) ◄── sign-in for Open WebUI and the portal
   │
   ├─► Open WebUI :8087 ──(shared app key + signed user token)──► LiteLLM :4011 + hook
   │                                                                  │ X-MemMachine-* headers
   └─► platform portal :8100/portal/                                  ▼
                                                     mock platform :8100 ──► MemMachine core :8090
Claude Code / curl ──(personal virtual key)──► LiteLLM :4011 ──┘              │ Postgres + Qdrant
                                                      ▲                       │ embedder + LLM
                                                      └───────────────────────┘ (plain model names)
```

Read [`mock_platform/README.md`](./mock_platform/README.md) for what each piece
checks and why; this file is only how to build it. Templates are in
[`deploy/`](./deploy): env files (`deploy/env/*.example`), systemd units
(`deploy/systemd/`), LiteLLM's config (`deploy/litellm-config.yaml`) and
MemMachine core's (`deploy/memmachine-cfg.yml`).

Everything below assumes `ENV_DIR=/opt/ltm-platform-env`, the repo checked out at
`$ENV_DIR/repo`, services running as user `ubuntu`, and every port bound to
127.0.0.1. Change the paths in the units if yours differ.

## Versions this was tested with

| Component | Version |
|---|---|
| Ubuntu | 26.04 LTS (AWS t3.xlarge, 16 GB) |
| Python | 3.12 for each venv, via `uv` |
| LiteLLM | 1.100.1 (`litellm[proxy]`), Postgres 18 for its keys |
| Open WebUI | 0.11.3 |
| Keycloak | 26.7.5, OpenJDK 21 |
| MemMachine core | `MemMachine/MemMachine` at commit `0b15fad` (v2 REST API on :8090), Postgres 18 + pgvector 0.8.1, Qdrant 1.19.0 |
| Models | chat `deepinfra/zai-org/GLM-5.2`, embeddings `openai/text-embedding-3-small`, both through LiteLLM |

## 0. Prerequisites

- **Two provider keys**: one for the chat model (DeepInfra in the template; any
  LiteLLM provider works) and an OpenAI key for `text-embedding-3-small`, which
  MemMachine core embeds with. Both go into `litellm.env` only (step 4); nothing
  else in the stack sees them, because core reaches both models through
  LiteLLM.
- **MemMachine core** is built here (step 6) with Postgres and Qdrant on the
  same host. If you already run a core with the v2 API (`/api/v2/memories`,
  `/search`, `/list`, `/projects`), skip step 6 and point `platform.env`'s
  `MOCK_PLATFORM_CORE_URL` at it. Either way, the platform is only a boundary if
  core accepts traffic from the platform alone, which a shared test host usually
  cannot enforce.
- `uv`, `git`, `postgresql` with `pgvector`, `openjdk-21-jre-headless`, `curl`,
  `node` (optional, only to syntax-check the portal page).

```bash
sudo apt-get install -y git postgresql postgresql-18-pgvector openjdk-21-jre-headless
curl -LsSf https://astral.sh/uv/install.sh | sh
```

If your Ubuntu ships another Postgres major, use the matching
`postgresql-<major>-pgvector` package.

## 1. Layout

```bash
ENV_DIR=/opt/ltm-platform-env
sudo mkdir -p $ENV_DIR && sudo chown ubuntu:ubuntu $ENV_DIR
git clone -b feat/mock-platform https://github.com/PeterZZZ/litellm-ltm-hook.git $ENV_DIR/repo
mkdir -p $ENV_DIR/{bin,litellm,openwebui,platform,keycloak,memmachine,data/openwebui,data/memmachine,data/qdrant}
```

## 2. Install

```bash
cd $ENV_DIR
uv venv litellm/.venv --python 3.12   && VIRTUAL_ENV=litellm/.venv   uv pip install 'litellm[proxy]==1.100.1' prisma
uv venv openwebui/.venv --python 3.12 && VIRTUAL_ENV=openwebui/.venv uv pip install 'open-webui==0.11.3'
uv venv platform/.venv --python 3.12  && VIRTUAL_ENV=platform/.venv  uv pip install fastapi uvicorn httpx 'pyjwt[crypto]'

# LiteLLM's database client is generated from the schema shipped inside litellm.
S=$(litellm/.venv/bin/python -c "import litellm,os; print(os.path.join(os.path.dirname(litellm.__file__),'proxy','schema.prisma'))")
PATH=$ENV_DIR/litellm/.venv/bin:$PATH litellm/.venv/bin/prisma generate --schema "$S"

cd keycloak
curl -sSLO https://github.com/keycloak/keycloak/releases/download/26.7.5/keycloak-26.7.5.tar.gz
curl -sSL https://github.com/keycloak/keycloak/releases/download/26.7.5/keycloak-26.7.5.tar.gz.sha1
sha1sum keycloak-26.7.5.tar.gz          # must match the line above
tar xzf keycloak-26.7.5.tar.gz && rm keycloak-26.7.5.tar.gz

# MemMachine core: the upstream repo at the commit this stack was tested with,
# installed into its own venv by uv (the workspace's lock file pins everything).
cd $ENV_DIR/memmachine
git clone https://github.com/MemMachine/MemMachine.git src
(cd src && git checkout 0b15fad && uv sync --package memmachine-server)
ls src/.venv/bin/memmachine-server

# Qdrant, a single binary.
cd $ENV_DIR/bin
curl -sSL https://github.com/qdrant/qdrant/releases/download/v1.19.0/qdrant-x86_64-unknown-linux-gnu.tar.gz | tar xz
./qdrant --version
```

The hook runs inside LiteLLM: link it next to LiteLLM's config so a `git pull`
plus a restart picks up changes.

```bash
ln -s $ENV_DIR/repo/memmachine_hook.py $ENV_DIR/litellm/memmachine_hook.py
cp $ENV_DIR/repo/deploy/litellm-config.yaml $ENV_DIR/litellm/config.yaml
```

## 3. Postgres: one database for LiteLLM's keys, one for MemMachine

Two roles, two databases, two generated passwords. Keep the passwords only in
the files named here.

```bash
LITELLM_DB_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
MEMMACHINE_DB_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
sudo -u postgres psql -c "CREATE USER ltmplatform WITH PASSWORD '$LITELLM_DB_PASSWORD';"
sudo -u postgres psql -c "CREATE DATABASE ltmplatform OWNER ltmplatform;"
sudo -u postgres psql -c "CREATE USER memmachine WITH PASSWORD '$MEMMACHINE_DB_PASSWORD';"
sudo -u postgres psql -c "CREATE DATABASE memmachine OWNER memmachine;"
sudo -u postgres psql -d memmachine -c "CREATE EXTENSION IF NOT EXISTS vector;"
echo "litellm.env    DATABASE_URL=postgresql://ltmplatform:$LITELLM_DB_PASSWORD@127.0.0.1:5432/ltmplatform"
echo "memmachine/cfg.yml  password: $MEMMACHINE_DB_PASSWORD"
```

The two `echo` lines are what goes into `litellm.env` (step 4) and
`memmachine/cfg.yml` (step 6). LiteLLM and core each create their own tables on
first start (LiteLLM takes about a minute).

## 4. Secrets

Generate each once and keep them only in the env files (mode 600):

```bash
python3 -c 'import secrets; print("sk-" + secrets.token_urlsafe(24))'   # LITELLM_MASTER_KEY
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'           # WEBUI_SECRET_KEY
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'           # FORWARD_USER_INFO_HEADER_JWT_SECRET
python3 -c 'import secrets; print(secrets.token_urlsafe(18))'           # KC_BOOTSTRAP_ADMIN_PASSWORD
```

Copy each `deploy/env/*.example` to `$ENV_DIR/` without `.example`, fill in what
you have so far, and `chmod 600 $ENV_DIR/*.env $ENV_DIR/accounts.txt`. The rest
(client secrets, virtual keys, the service credential) are produced by the
steps below; come back and fill them in as they appear.

```bash
cd $ENV_DIR/repo/deploy/env
for f in *.example; do cp -n "$f" "$ENV_DIR/${f%.example}"; done
cp -n $ENV_DIR/repo/deploy/memmachine-cfg.yml $ENV_DIR/memmachine/cfg.yml
chmod 600 $ENV_DIR/*.env $ENV_DIR/accounts.txt $ENV_DIR/memmachine/cfg.yml
```

The provider keys go into `litellm.env` and nowhere else: `DEEPINFRA_API_KEY`
for the chat model and `OPENAI_API_KEY` for the embedding model. Open WebUI,
the platform and core all reach the models through LiteLLM with keys LiteLLM
issues, so a provider key never leaves that file. The LiteLLM master key from
above goes into `litellm.env` and, as the embedder's and LLM's `api_key`, into
`memmachine/cfg.yml`; the Postgres passwords from step 3 go where its `echo`
lines said.

`accounts.txt` holds the test people's Keycloak passwords (`email password` per
line); pick them now.

## 5. Keycloak and the `corp` realm

```bash
sudo cp $ENV_DIR/repo/deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ltmplat-keycloak
until curl -sf http://127.0.0.1:8180/realms/master/.well-known/openid-configuration >/dev/null; do sleep 3; done

set -a; . $ENV_DIR/keycloak.env; set +a
KEYCLOAK_ADMIN=$KC_BOOTSTRAP_ADMIN_USERNAME KEYCLOAK_ADMIN_PASSWORD=$KC_BOOTSTRAP_ADMIN_PASSWORD \
ACCOUNTS_FILE=$ENV_DIR/accounts.txt SECRETS_OUT=$ENV_DIR/keycloak-clients.env \
  python3 $ENV_DIR/repo/tools/keycloak_setup.py
```

This creates realm `corp`, an admin-only `employee_id` attribute, the clients
`open-webui` and `memmachine-platform` (both with an `employee_id` claim; the
platform's also has a read-only service account), and the users in `USERS` at
the top of the script (admin E10000, alice E12345, bob E67890, carol E24680).
Copy the two secrets from `keycloak-clients.env` into `openwebui.env`
(`OAUTH_CLIENT_SECRET`) and `platform.env` (`MOCK_PLATFORM_OIDC_CLIENT_SECRET`).
The script is idempotent; rerun it after editing `USERS`.

## 6. MemMachine core

Core needs Postgres (step 3), Qdrant, and an OpenAI-compatible endpoint for its
embedder and LLM, which is this stack's LiteLLM on :4011 with the master key
(`memmachine/cfg.yml`, filled in at step 4). The model names there are the plain
ones, so the hook inside LiteLLM ignores core's own calls and core never
recurses into memory. Core starts before LiteLLM is up; it just fails
individual embed calls until step 7.

```bash
sudo systemctl enable --now ltmplat-qdrant
until curl -sf http://127.0.0.1:6333/ >/dev/null; do sleep 2; done
sudo systemctl enable --now ltmplat-memmachine
until curl -sf http://127.0.0.1:8090/openapi.json >/dev/null; do sleep 3; done
curl -s http://127.0.0.1:8090/openapi.json | grep -o '"/api/v2/memories/[a-z]*"' | sort -u
# "/api/v2/memories/list" "/api/v2/memories/search" ... = the v2 API the platform forwards to
```

Logs are in `$ENV_DIR/data/memmachine/memmachine.log`; `journalctl -u
ltmplat-memmachine` shows start-up errors (a wrong database password or a
missing `vector` extension both fail there).

## 7. LiteLLM

Start it with `MEMMACHINE_API_KEY` still a placeholder; it is filled in at step 9.
The hook only calls the platform for `memmachine-` models, so plain models work
meanwhile.

```bash
sudo systemctl enable --now ltmplat-litellm
until curl -sf http://127.0.0.1:4011/health/liveliness >/dev/null; do sleep 3; done
# Both provider keys work: a chat completion and an embedding through the gateway.
set -a; . $ENV_DIR/litellm.env; set +a
curl -s http://127.0.0.1:4011/v1/embeddings -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"text-embedding-3-small","input":"hello"}' | head -c 80; echo
curl -s http://127.0.0.1:4011/v1/chat/completions -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"Say OK."}]}' | head -c 200; echo
```

## 8. Virtual keys

```bash
set -a; . $ENV_DIR/litellm.env; set +a
L=http://127.0.0.1:4011; H=(-H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json")

# Open WebUI's one shared key, owned by a LiteLLM service user (never a person).
# -> openwebui-connection.env OPENAI_API_KEYS, keys.env OPENWEBUI_APP_KEY and OPENWEBUI_APP_OWNER=open-webui
curl -s "${H[@]}" $L/user/new -d '{"user_id": "open-webui", "user_role": "internal_user_viewer", "auto_create_key": false}'
curl -s "${H[@]}" $L/key/generate -d '{"user_id": "open-webui", "key_alias": "open-webui-app"}'

# The platform's read-only key, for finding people's personal keys. -> platform.env MOCK_PLATFORM_LITELLM_KEY
curl -s "${H[@]}" $L/user/new -d '{"user_id": "memmachine-platform", "user_role": "proxy_admin_viewer", "auto_create_key": false}'
curl -s "${H[@]}" $L/key/generate -d '{"user_id": "memmachine-platform", "key_alias": "memmachine-platform-viewer"}'

# Personal keys, Claude Code style: LiteLLM user_id = employee ID. -> keys.env
for p in "E12345 alice" "E67890 bob" "E24680 carol"; do set -- $p
  curl -s "${H[@]}" $L/user/new -d "{\"user_id\": \"$1\", \"user_email\": \"$2@corp.example\", \"user_role\": \"internal_user\", \"auto_create_key\": false}"
  curl -s "${H[@]}" $L/key/generate -d "{\"user_id\": \"$1\", \"key_alias\": \"$2-personal-${1,,}\"}"
done
```

Each `/key/generate` prints the key once, in `"key"`.

## 9. The platform's registry

```bash
cd $ENV_DIR/repo
A="$ENV_DIR/platform/.venv/bin/python -m mock_platform.admin --db $ENV_DIR/platform/registry.sqlite"
set -a; . $ENV_DIR/keys.env; . $ENV_DIR/openwebui.env; set +a

$A service add litellm-ltm-platform-env       # prints mm-svc-... once -> litellm.env MEMMACHINE_API_KEY
$A key app --key-env OPENWEBUI_APP_KEY --label open-webui-app \
           --secret-env FORWARD_USER_INFO_HEADER_JWT_SECRET --issuer open-webui \
           --owner open-webui                   # the key's LiteLLM user, checked on every call
$A user add admin@corp.example --role admin --employee-id E10000
$A user add alice@corp.example --employee-id E12345
$A user add bob@corp.example   --employee-id E67890
$A show
```

Nobody's personal key is linked by hand: with `MOCK_PLATFORM_LITELLM_KEY` set,
the platform asks LiteLLM about a key the first time it is used and links it to
its owner if that owner is enrolled (alice's and bob's keys, created in step 8
with `user_id` = employee ID, link themselves on their first chat). Every 10
minutes, and from the Admin tab's "Reconcile" button, it re-reads LiteLLM's key
list and drops keys that are gone, blocked or reassigned.

Carol is left out on purpose: the end-to-end checks expect one person with a
Keycloak login and a LiteLLM key but no memory account, so her key is never
linked. Later accounts can be
created from the portal instead (Admin tab); the platform then takes the
employee ID from Keycloak and finds the personal keys in LiteLLM itself.

```bash
sudo systemctl enable --now ltmplat-platform
sudo systemctl restart ltmplat-litellm     # picks up MEMMACHINE_API_KEY
curl -s http://127.0.0.1:8100/portal/api/config   # {"sso":true,...}
```

## 10. Open WebUI

```bash
sudo systemctl enable --now ltmplat-openwebui
until curl -sf http://127.0.0.1:8087/api/config >/dev/null; do sleep 3; done
curl -s http://127.0.0.1:8087/api/config | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["oauth"], d["features"]["enable_login_form"])'
# {'providers': {'oidc': 'Corp SSO'}, ...} False
```

Sign in through "Corp SSO" as `admin@corp.example` first: Open WebUI's first
user becomes its admin.

## 11. Verify

```bash
cd $ENV_DIR/repo
set -a; . $ENV_DIR/openwebui.env; . $ENV_DIR/litellm.env; set +a
LITELLM_URL=http://127.0.0.1:4011 OWUI_URL=http://127.0.0.1:8087 PLATFORM_URL=http://127.0.0.1:8100 \
PLATFORM_DB=$ENV_DIR/platform/registry.sqlite KEYS_FILE=$ENV_DIR/keys.env \
ACCOUNTS_FILE=$ENV_DIR/accounts.txt OWUI_JWT_SECRET="$FORWARD_USER_INFO_HEADER_JWT_SECRET" \
PLATFORM_SERVICE_CREDENTIAL="$MEMMACHINE_API_KEY" LITELLM_MASTER_KEY="$LITELLM_MASTER_KEY" \
  uv run --group platform python3 tools/platform_e2e.py
```

Expect `41/41 passed` (identity, the Platform API itself, grants, key linking
and reconcile; the script makes and removes a few throwaway LiteLLM keys with
the master key). Unit tests, no network:

```bash
MEMMACHINE_LIVE=0 uv run --group platform pytest -q \
  test_hook.py test_identity.py test_platform.py test_platform_mode.py test_portal.py test_sso.py test_linking.py
```

Last tested at commit `fd9d1eb` (rows 7, 9, 2, 3 plus the portal fixes that followed)
on branch `feat/mock-platform`; the shared VM should run that or later.

To use it from a laptop, forward every port the browser touches; Keycloak's
must be included, because sign-in redirects the browser there. Use
`http://127.0.0.1:<port>` in the browser, not `localhost`.

```bash
ssh -L 8087:127.0.0.1:8087 -L 8100:127.0.0.1:8100 -L 8180:127.0.0.1:8180 -L 4011:127.0.0.1:4011 <host>
```

LiteLLM's own dashboard is at `http://127.0.0.1:4011/ui` (user `admin`, password
= the master key). It has no SSO in this stack.

## Pitfalls we hit

| Symptom | Cause | Fix |
|---|---|---|
| Open WebUI still shows the password form | `ENABLE_LOGIN_FORM`, `DEFAULT_USER_ROLE`, task model and similar settings only seed Open WebUI's stored config on its **first** start | On an existing install, change them in Admin Settings, or stop Open WebUI and update the `config` table row (e.g. `ui.enable_login_form` → `false`) |
| Users see "Model not found" | Models from a connection are admin-only by default | `BYPASS_MODEL_ACCESS_CONTROL=true`, or grant read on each model to users |
| New SSO users cannot chat | `DEFAULT_USER_ROLE` defaults to `pending` | `DEFAULT_USER_ROLE=user` before first start, or approve them as admin |
| Chat titles and tags show up as memories | Open WebUI's background tasks used a `memmachine-` model | Point the task model at the plain model (`TASK_MODEL_EXTERNAL=glm-5.2`) |
| Open WebUI writes to Postgres | It inherited LiteLLM's `DATABASE_URL` | Set its own `sqlite:///` `DATABASE_URL` in `openwebui.env` |
| LiteLLM refuses to start: "must not be set with MEMMACHINE_PLATFORM=1" | `FORWARD_USER_INFO_HEADER_JWT_SECRET` reached LiteLLM | Leave it empty in `litellm.env`; it belongs to Open WebUI and the platform only |
| `/user/new` returns 409 | Another LiteLLM user already has that email | Delete or rename the old user first |
| `employee_id` missing from tokens | Keycloak's declarative user profile drops undeclared attributes | `keycloak_setup.py` declares it; rerun the script |
| Scripted Keycloak login gets 400 | Keycloak's cookies are Secure; Python's cookie jar won't send them over http | Use `tools/sso_browser.py`; browsers accept them on 127.0.0.1 |
| Portal or Open WebUI sign-in loops | Mixed `localhost` and `127.0.0.1`: different cookies | Use `127.0.0.1` throughout |
| A person's chat is refused: "named user is not the verified person" | Hook names people by `name`, but the registry has no employee ID for them (or the reverse) | Admin tab → Sync (pulls the employee ID from Keycloak), or align `MEMMACHINE_OWUI_USER_CLAIM` |
| Earlier memories gone after adding employee IDs | The org moved from the email slug to the employee ID | Expected: core cannot move memory; the old namespace is untouched |
| Portal tab blank after an upgrade | The tab is running the older page | Reload; the page also reloads itself when it detects a new version |

## Not in this stack

- **SSO on LiteLLM's dashboard** (developers creating their own keys). LiteLLM's
  free tier refuses it beyond 5 users, and it counts every LiteLLM user row,
  service users included. Keys here are created through the API in step 8.
- A real secret store, TLS, or network rules that make core reachable only
  from the platform.
