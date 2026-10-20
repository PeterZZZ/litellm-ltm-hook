# Setting up the platform-mode test stack

How to stand up the whole chain this branch was tested on, from an empty host:

```
browser ──► Keycloak :8180 (Corp SSO) ◄── sign-in for Open WebUI and the portal
   │
   ├─► Open WebUI :8087 ──(shared app key + signed user token)──► LiteLLM :4011 + hook
   │                                                                  │ X-MemMachine-* headers
   └─► platform portal :8100/portal/                                  ▼
                                                     mock platform :8100 ──► MemMachine core :8090
Claude Code / curl ──(personal virtual key)──► LiteLLM :4011 ──┘
```

Read [`mock_platform/README.md`](./mock_platform/README.md) for what each piece
checks and why; this file is only how to build it. Templates are in
[`deploy/`](./deploy): env files (`deploy/env/*.example`), systemd units
(`deploy/systemd/`), and LiteLLM's config (`deploy/litellm-config.yaml`).

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
| MemMachine core | v2 REST API on :8090 |

## 0. Prerequisites

- **MemMachine core** reachable at `http://127.0.0.1:8090` (`/api/v2/memories`,
  `/search`, `/list`, `/projects`). Setting it up is out of scope here; any core
  with the v2 API and a working embedder will do. The platform is only a
  boundary if core accepts traffic from the platform alone, which a shared test
  host usually cannot enforce.
- A model provider key for `deploy/litellm-config.yaml` (DeepInfra in the
  template; any LiteLLM provider works).
- `uv`, `postgresql`, `openjdk-21-jre-headless`, `curl`, `node` (optional, only
  to syntax-check the portal page).

```bash
sudo apt-get install -y postgresql openjdk-21-jre-headless
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 1. Layout

```bash
ENV_DIR=/opt/ltm-platform-env
sudo mkdir -p $ENV_DIR && sudo chown ubuntu:ubuntu $ENV_DIR
git clone -b feat/mock-platform https://github.com/PeterZZZ/litellm-ltm-hook.git $ENV_DIR/repo
mkdir -p $ENV_DIR/{litellm,openwebui,platform,keycloak,data/openwebui}
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
```

The hook runs inside LiteLLM: link it next to LiteLLM's config so a `git pull`
plus a restart picks up changes.

```bash
ln -s $ENV_DIR/repo/memmachine_hook.py $ENV_DIR/litellm/memmachine_hook.py
cp $ENV_DIR/repo/deploy/litellm-config.yaml $ENV_DIR/litellm/config.yaml
```

## 3. Postgres for LiteLLM's virtual keys

```bash
sudo -u postgres psql -c "CREATE USER ltmplatform WITH PASSWORD '<password>';"
sudo -u postgres psql -c "CREATE DATABASE ltmplatform OWNER ltmplatform;"
```

LiteLLM creates its tables on first start (about a minute).

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

## 6. LiteLLM

Start it with `MEMMACHINE_API_KEY` still a placeholder; it is filled in at step 8.
The hook only calls the platform for `memmachine-` models, so plain models work
meanwhile.

```bash
sudo systemctl enable --now ltmplat-litellm
until curl -sf http://127.0.0.1:4011/health/liveliness >/dev/null; do sleep 3; done
```

## 7. Virtual keys

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

## 8. The platform's registry

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
$A key personal alice@corp.example --key-env ALICE_VIRTUAL_KEY --label alice-personal-e12345
$A key personal bob@corp.example   --key-env BOB_VIRTUAL_KEY   --label bob-personal-e67890
$A show
```

Carol is left out on purpose: the end-to-end checks expect one person with a
Keycloak login and a LiteLLM key but no memory account. Later accounts can be
created from the portal instead (Admin tab); the platform then takes the
employee ID from Keycloak and finds the personal keys in LiteLLM itself.

```bash
sudo systemctl enable --now ltmplat-platform
sudo systemctl restart ltmplat-litellm     # picks up MEMMACHINE_API_KEY
curl -s http://127.0.0.1:8100/portal/api/config   # {"sso":true,...}
```

## 9. Open WebUI

```bash
sudo systemctl enable --now ltmplat-openwebui
until curl -sf http://127.0.0.1:8087/api/config >/dev/null; do sleep 3; done
curl -s http://127.0.0.1:8087/api/config | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["oauth"], d["features"]["enable_login_form"])'
# {'providers': {'oidc': 'Corp SSO'}, ...} False
```

Sign in through "Corp SSO" as `admin@corp.example` first: Open WebUI's first
user becomes its admin.

## 10. Verify

```bash
cd $ENV_DIR/repo
set -a; . $ENV_DIR/openwebui.env; set +a
LITELLM_URL=http://127.0.0.1:4011 OWUI_URL=http://127.0.0.1:8087 PLATFORM_URL=http://127.0.0.1:8100 \
PLATFORM_DB=$ENV_DIR/platform/registry.sqlite KEYS_FILE=$ENV_DIR/keys.env \
ACCOUNTS_FILE=$ENV_DIR/accounts.txt OWUI_JWT_SECRET="$FORWARD_USER_INFO_HEADER_JWT_SECRET" \
  uv run --group platform python3 tools/platform_e2e.py
```

Expect `17/17 passed`. Unit tests, no network:

```bash
MEMMACHINE_LIVE=0 uv run --group platform pytest -q \
  test_hook.py test_identity.py test_platform.py test_platform_mode.py test_portal.py test_sso.py
```

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
  service users included. Keys here are created through the API in step 7.
- A real secret store, TLS, or network rules that make core reachable only
  from the platform.
