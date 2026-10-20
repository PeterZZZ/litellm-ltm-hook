#!/usr/bin/env bash
# The LiteLLM admin's tool: people and their virtual keys. Uses only LiteLLM's
# master key; it never talks to the platform (in production this is the
# customer's AI infra team and their key process).
#
#   tools/litellm-admin.sh add-user ID [EMAIL]      ID and their one key; prints the Claude Code settings
#   tools/litellm-admin.sh rotate-key ID            a new key for ID; the old one stops working
#   tools/litellm-admin.sh remove-user ID           delete ID and their keys (memory stays)
#   tools/litellm-admin.sh block ID | unblock ID    stop or restore ID's keys
#   tools/litellm-admin.sh add-frontend-key NAME    a key with no owner for a frontend's shared
#                                                   connection (e.g. Open WebUI); prints it once
#   tools/litellm-admin.sh remove-frontend-key NAME delete that key
#   tools/litellm-admin.sh keys                     every virtual key: owner, alias, state (hashes only)
#   tools/litellm-admin.sh settings [PROJECT] < KEYFILE   the settings block for a key read from stdin
#
# ID is the person's ID as the hook and the platform name them: their employee ID
# (case kept, e.g. E12345), else their email. EMAIL is stored as the LiteLLM
# user's user_email (the platform finds the person by it; the hook's direct mode
# uses it as the identity).
#
# Settings, from the environment or from LITELLM_ENV (default $ENV_DIR/litellm.env,
# else ./litellm.env; see SETUP.md):
#   LITELLM_MASTER_KEY   required
#   LITELLM_URL          default http://127.0.0.1:${LITELLM_PORT:-4000}
#   CLAUDE_MODEL         default glm-5.2 (Claude Code asks for memmachine-<model>)
#   CLAUDE_MODEL_CONTEXT default 200000 (tokens; the settings leave room for the memory block)
#   LITELLM_SERVICE_USERS default memmachine-platform (never given a personal key)
set -euo pipefail

die()  { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
info() { printf '    %s\n' "$*"; }

env_file="${LITELLM_ENV:-${ENV_DIR:+$ENV_DIR/litellm.env}}"
env_file="${env_file:-./litellm.env}"
if [ -z "${LITELLM_MASTER_KEY:-}" ] && [ -r "$env_file" ]; then
  # Only the two values this tool needs, not the whole file (it holds provider keys).
  eval "$(grep -E '^(LITELLM_MASTER_KEY|LITELLM_PORT)=' "$env_file" | sed 's/^/export /')"
fi
[ -n "${LITELLM_MASTER_KEY:-}" ] || die "LITELLM_MASTER_KEY is not set (and not in $env_file)"
export LITELLM_URL="${LITELLM_URL:-http://127.0.0.1:${LITELLM_PORT:-4000}}"
SERVICE_USERS=" ${LITELLM_SERVICE_USERS:-memmachine-platform} "

# litellm_admin METHOD PATH [JSON]: LiteLLM's admin API with the master key. Prints
# the JSON reply; on an HTTP error prints LiteLLM's message and fails. The key and
# the body (which can hold a key) go through the environment, not argv: any user
# on the host can read a process's arguments.
litellm_admin() {
  LITELLM_ADMIN_BODY="${3:-}" python3 - "$1" "$2" <<'PY'
import json, os, sys, urllib.error, urllib.request
method, path = sys.argv[1], sys.argv[2]
body = os.environ.get("LITELLM_ADMIN_BODY", "").encode() or None
req = urllib.request.Request(
    os.environ["LITELLM_URL"].rstrip("/") + path, body, method=method,
    headers={"Authorization": "Bearer " + os.environ["LITELLM_MASTER_KEY"],
             "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        sys.stdout.write(resp.read().decode())
except urllib.error.HTTPError as exc:
    detail = exc.read().decode(errors="replace")
    try:
        detail = json.loads(detail)["error"]["message"]
    except Exception:
        pass
    sys.exit("LiteLLM %s %s: %s %s" % (method, path, exc.code, str(detail)[:300]))
PY
}

check_id() {
  python3 - "$1" <<'PY' || die "invalid ID '$1': an employee ID or an email (letters, digits, . _ % + @ -)"
import re, sys
sys.exit(0 if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._%+@-]{0,253}", sys.argv[1]) else 1)
PY
  case "$SERVICE_USERS" in *" $1 "*) die "$1 is one of the stack's own users" ;; esac
}
check_project() {
  python3 - "$1" <<'PY' || die "invalid name '$1': letters, digits, _ and -, up to 64"
import re, sys
sys.exit(0 if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}", sys.argv[1]) else 1)
PY
}
# org_of ID: the person's MemMachine org, as the hook makes it (memmachine_hook.org_of).
org_of() { printf '%s' "$1" | sed 's/[^A-Za-z0-9_:-]/_/g; s/^_*//; s/_*$//'; }

keys_of() {  # keys_of ID: ID's key hashes, one per line
  litellm_admin GET "/key/list?user_id=$1&size=100" | python3 -c '
import json, sys
print("\n".join(json.load(sys.stdin)["keys"]))'
}
has_user() { litellm_admin GET "/user/info?user_id=$1" >/dev/null 2>&1; }
new_key() {  # new_key ID [ALIAS]: make ID's key; prints it. Aliases are unique in LiteLLM.
  litellm_admin POST /key/generate "{\"user_id\":\"$1\",\"key_alias\":\"${2:-$1}\"}" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["key"])'
}
delete_keys() {  # delete_keys TOKEN...
  [ $# -gt 0 ] || return 0
  litellm_admin POST /key/delete "{\"keys\":[$(printf '"%s",' "$@" | sed 's/,$//')]}" >/dev/null
}

settings_block() {  # settings_block KEY [PROJECT]
  local model="${CLAUDE_MODEL:-glm-5.2}"
  local window=$(( ${CLAUDE_MODEL_CONTEXT:-200000} - ${MEMMACHINE_INJECT_MAX_TOKENS:-8000} ))
  cat <<JSON
{
  "autoMemoryEnabled": false,
  "env": {
    "ANTHROPIC_BASE_URL": "${LITELLM_URL}",
    "ANTHROPIC_AUTH_TOKEN": "$1",
    "ANTHROPIC_MODEL": "memmachine-${model}",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "${model}",
    "ANTHROPIC_CUSTOM_HEADERS": "x-memmachine-project: ${2:-default}\nx-memmachine-client: claude-code",
    "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "${window}"
  }
}
JSON
}

print_settings() {  # print_settings ID KEY
  cat <<TXT

  The key is shown only now. Put this in $1's ~/.claude/settings.json (merge it
  with what is there), then chmod 600 the file:

TXT
  settings_block "$2" | sed 's/^/    /'
  cat <<TXT

  Memory is in org $(org_of "$1"). Each repo picks its project in its own .claude/settings.json:
      { "env": { "ANTHROPIC_CUSTOM_HEADERS": "x-memmachine-project: <project>" } }
TXT
}

case "${1:-help}" in
add-user)
  id="${2:?usage: litellm-admin.sh add-user ID [EMAIL]}"
  check_id "$id"
  ! has_user "$id" || [ -z "$(keys_of "$id")" ] || die "$id already has a key: rotate-key or remove-user"
  email="${3:-}"
  has_user "$id" || litellm_admin POST /user/new \
    "{\"user_id\":\"$id\",${email:+\"user_email\":\"$email\",}\"user_role\":\"internal_user_viewer\",\"auto_create_key\":false}" \
    >/dev/null
  key=$(new_key "$id")
  info "made $id's key"
  print_settings "$id" "$key" ;;

rotate-key)
  id="${2:?usage: litellm-admin.sh rotate-key ID}"
  check_id "$id"
  has_user "$id" || die "$id is not a gateway user"
  old=$(keys_of "$id")
  # The new key first, under its own alias, so the person always has a key.
  key=$(new_key "$id" "$id@$(date -u +%Y%m%dT%H%M%S)")
  # shellcheck disable=SC2086
  delete_keys $old
  info "replaced $id's key; the old one no longer works"
  print_settings "$id" "$key" ;;

add-frontend-key)
  name="${2:?usage: litellm-admin.sh add-frontend-key NAME}"
  check_project "$name"
  key=$(litellm_admin POST /key/generate "{\"key_alias\":\"$name\"}" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["key"])')
  info "made the frontend key $name (no owner). It is shown only now:"
  echo "    $key"
  info "its hash, for the platform: $(printf '%s' "$key" | sha256sum | cut -c1-64)" ;;

remove-frontend-key)
  name="${2:?usage: litellm-admin.sh remove-frontend-key NAME}"
  t=$(litellm_admin GET "/key/list?key_alias=$name&return_full_object=true&size=100" | python3 -c '
import json, sys
print(" ".join(k["token"] for k in json.load(sys.stdin)["keys"] if not k.get("user_id")))')
  [ -n "$t" ] || die "no frontend key $name"
  # shellcheck disable=SC2086
  delete_keys $t
  info "deleted the frontend key $name" ;;

remove-user)
  id="${2:?usage: litellm-admin.sh remove-user ID}"
  check_id "$id"
  has_user "$id" || die "$id is not a gateway user"
  litellm_admin POST /user/delete "{\"user_ids\":[\"$id\"]}" >/dev/null   # deletes their keys too
  info "deleted $id and their keys; LiteLLM refuses them from now on; memory in org $(org_of "$id") is kept" ;;

block|unblock)
  id="${2:?usage: litellm-admin.sh $1 ID}"
  check_id "$id"
  for t in $(keys_of "$id"); do litellm_admin POST "/key/$1" "{\"key\":\"$t\"}" >/dev/null; done
  info "${1}ed $id's keys" ;;

keys)
  litellm_admin GET "/key/list?return_full_object=true&size=100" | python3 -c '
import json, sys
for k in json.load(sys.stdin)["keys"]:
    state = "blocked" if k.get("blocked") else "team key" if k.get("team_id") else "ok"
    print("    %-24s %-28s %-9s key %s…" % (k.get("user_id") or "(no owner)", k.get("key_alias") or "-",
                                         state, k["token"][:10]))' ;;

settings)
  read -r key || true
  [ -n "${key:-}" ] || die "usage: litellm-admin.sh settings [PROJECT] < KEYFILE"
  [ -z "${2:-}" ] || check_project "$2"
  settings_block "$key" "${2:-}" ;;

*) sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
