#!/usr/bin/env bash
# Shared helpers for the setup/cleanup orchestration scripts.
# Source this after sourcing .supply-chain-automation-env:
#   source .supply-chain-automation-env
#   source scripts/lib.sh
#
# Provides:
#   require_vars NAME[:hint] ...   preflight: fail loud listing ALL missing vars at once
#   save_var NAME VALUE            persist a discovered/collected value to scripts/.env.generated
#   load_generated                 load previously-saved values (call early in each script)
#   confirm "prompt"               y/N gate (returns 0 on yes); auto-yes if ASSUME_YES=1

# Location of the cross-invocation value store (gitignored via .env.*).
_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GENERATED_ENV="${GENERATED_ENV:-${_LIB_DIR}/.env.generated}"

# log MESSAGE — section header. Defined here so every script shares it and none falls through
# to the macOS /usr/bin/log binary (which happens if a script calls log() without defining it).
log() { printf '\n=== %s ===\n' "$*"; }

# load_generated — source the generated env file if it exists (values from prior phases).
load_generated() {
  if [[ -f "$GENERATED_ENV" ]]; then
    # shellcheck disable=SC1090
    source "$GENERATED_ENV"
  fi
}

# save_var NAME VALUE — upsert NAME=VALUE into the generated env file and export it now.
save_var() {
  local name="$1" value="$2"
  [[ -n "$name" ]] || { echo "save_var: missing name" >&2; return 1; }
  touch "$GENERATED_ENV"
  # remove any existing line for this var, then append the new one
  local tmp; tmp="$(mktemp)"
  grep -v -E "^export ${name}=" "$GENERATED_ENV" > "$tmp" 2>/dev/null || true
  printf 'export %s=%q\n' "$name" "$value" >> "$tmp"
  mv "$tmp" "$GENERATED_ENV"
  export "${name}=${value}"
  echo "  saved ${name}=${value}  (-> ${GENERATED_ENV##*/})"
}

# require_vars NAME[:hint] ...  — verify all are set & non-empty; if any missing, print each
# with its hint and exit 1. Checks EVERYTHING before failing (no mid-phase surprises).
require_vars() {
  local missing=0 spec name hint
  for spec in "$@"; do
    name="${spec%%:*}"
    hint=""; [[ "$spec" == *:* ]] && hint="${spec#*:}"
    if [[ -z "${!name:-}" ]]; then
      if [[ $missing -eq 0 ]]; then echo "Missing required values:" >&2; fi
      missing=1
      if [[ -n "$hint" ]]; then printf '  - %s   (%s)\n' "$name" "$hint" >&2
      else printf '  - %s\n' "$name" >&2; fi
    fi
  done
  if [[ $missing -eq 1 ]]; then
    echo >&2
    echo "Set them in .supply-chain-automation-env (build values) or let a prior phase save them" >&2
    echo "to ${GENERATED_ENV##*/}, then re-run. Console-collected ids: use save_var or export before running." >&2
    exit 1
  fi
}

# share_cli_and_console KIND ID — grant the owner action set on a CLI-created Quick resource to the
# CLI user (auto-detected creator) and, if different, the console user. Quick ties a resource to the
# identity that created it (the CLI user); a DIFFERENT console sign-in cannot see or use it until
# shared. Least-privilege: grants to exactly those two identities, never account-wide.
#
# Identifying the console user: NO AWS API returns "who is signed into the browser" to the CLI
# principal (sts get-caller-identity only ever returns the CLI user; list-users doesn't flag the live
# session). The reliable signal is to run `aws sts get-caller-identity` FROM INSIDE the console — open
# CloudShell in the Amazon Quick/AWS console and run it there; because CloudShell inherits the console
# session, its caller ARN IS the console identity. The operator pastes that ARN once:
#
#     export CONSOLE_CALLER_ARN='arn:aws:sts::<acct>:assumed-role/<Role>/<session>'   # or .../user/<name>
#
# This helper parses the Quick user path from that ARN (assumed-role/<Role>/<session> -> <Role>/<session>;
# user/<name> -> <name>), resolves it to the full Quick user ARN via list-users, and caches it in
# CONSOLE_QUICK_USER_ARN so later phases reuse it with no re-entry. If CONSOLE_CALLER_ARN is unset and
# the account has exactly one non-CLI Quick user, that user is used automatically; otherwise the helper
# grants the CLI user only and tells you how to supply the console ARN.
# Uses a file:// JSON payload so the (long) grant argument never depends on shell line-wrapping.
#   KIND = data-source | space | flow | connector
share_cli_and_console() {
  local kind="$1" id="$2" verb id_flag actions
  case "$kind" in
    data-source) verb="update-data-source-permissions"; id_flag="--data-source-id"
      actions='"quicksight:DescribeDataSource","quicksight:DescribeDataSourcePermissions","quicksight:PassDataSource","quicksight:UpdateDataSource","quicksight:DeleteDataSource","quicksight:UpdateDataSourcePermissions"' ;;
    space) verb="update-space-permissions"; id_flag="--space-id"
      actions='"quicksight:DescribeSpace","quicksight:UpdateSpace","quicksight:DeleteSpace","quicksight:DescribeSpacePermissions","quicksight:UpdateSpacePermissions"' ;;
    flow) verb="update-flow-permissions"; id_flag="--flow-id"
      # Flows require the action set to EXACTLY match the OWNER role (per AWS docs) — no arbitrary subset.
      actions='"quicksight:PublishFlow","quicksight:GetFlow","quicksight:UpdateFlowPermissions","quicksight:GetFlowSession","quicksight:StartFlowSession","quicksight:StopFlowSession","quicksight:UpdateFlowSession","quicksight:UnpublishFlow","quicksight:GetFlowStages","quicksight:DeleteFlow","quicksight:DescribeFlowPermissions","quicksight:UpdateFlow","quicksight:CreatePresignedUrl"' ;;
    connector) verb="update-action-connector-permissions"; id_flag="--action-connector-id"
      # Connectors are created in the CONSOLE (owned by the console user); the flow runs as the CLI
      # user, so the CLI user must also be granted access or the flow's connector steps fail at runtime.
      actions='"quicksight:UpdateActionConnector","quicksight:DescribeActionConnector","quicksight:DescribeActionConnectorPermissions","quicksight:DeleteActionConnector","quicksight:UpdateActionConnectorPermissions","quicksight:ListActionConnectors"' ;;
    *) echo "  share_cli_and_console: unknown kind '${kind}'" >&2; return 1 ;;
  esac

  # full user list (Name + ARN), used for matching and for helpful diagnostics
  local users_json cli_login cli_arn
  users_json="$(aws quicksight list-users --aws-account-id "$ACCOUNT_ID" --namespace default \
    --region "$REGION" --profile "$AWS_PROFILE_SC" --query 'UserList[].{Name:UserName,Arn:Arn}' --output json 2>/dev/null || true)"
  if [[ -z "$users_json" || "$users_json" == "[]" ]]; then
    echo "  (list-users returned no users — skipping share of ${kind} ${id}; grant manually if needed)" >&2
    return 0
  fi
  # CLI user = the Quick user whose name ends with the IAM entity from the caller identity
  cli_login="$(aws sts get-caller-identity --profile "$AWS_PROFILE_SC" --query 'Arn' --output text 2>/dev/null | sed 's|.*/||')"
  cli_arn="$(echo "$users_json" | jq -r --arg u "$cli_login" '.[] | select(.Name|endswith($u)) | .Arn' | head -1)"
  [[ -n "$cli_arn" ]] || cli_arn="$(echo "$users_json" | jq -r '.[0].Arn')"   # fallback: first user

  # Resolve the console user. Precedence:
  #   1. CONSOLE_QUICK_USER_ARN cached from an earlier phase -> reuse, no work.
  #   2. CONSOLE_CALLER_ARN pasted by the operator (from `aws sts get-caller-identity` in console
  #      CloudShell) -> parse its Quick user path and resolve to the full Quick ARN via list-users.
  #   3. Exactly one non-CLI Quick user exists -> use it automatically.
  #   4. Otherwise -> grant the CLI user only, and explain how to supply CONSOLE_CALLER_ARN.
  local console_arn="${CONSOLE_QUICK_USER_ARN:-}"
  if [[ -n "$console_arn" ]]; then
    echo "  (reusing console user '$(echo "$console_arn" | sed 's|.*/||')')"
  elif [[ -n "${CONSOLE_CALLER_ARN:-}" ]]; then
    # assumed-role/<Role>/<session> -> <Role>/<session> ; user/<name> -> <name>
    local console_path; console_path="$(echo "$CONSOLE_CALLER_ARN" | sed -E 's|.*:assumed-role/||; s|.*:user/||')"
    console_arn="$(echo "$users_json" | jq -r --arg p "$console_path" '.[] | select(.Name == $p or (.Name|endswith($p))) | .Arn' | head -1)"
    if [[ -n "$console_arn" ]]; then
      echo "  console user from CONSOLE_CALLER_ARN: $(echo "$console_arn" | sed 's|.*/||')"
      save_var CONSOLE_QUICK_USER_ARN "$console_arn" >/dev/null
    else
      echo "  WARN: CONSOLE_CALLER_ARN path '${console_path}' not found in Quick users — is that identity signed into the Quick console yet? Granting CLI user only." >&2
    fi
  else
    local others; others="$(echo "$users_json" | jq -r --arg c "$cli_arn" '.[] | select(.Arn != $c) | .Arn')"
    local n; n="$(echo "$others" | grep -c . || true)"
    if [[ "$n" -eq 0 ]]; then
      echo "  (only one Quick identity — granting the CLI user; nothing else to share to)"
    elif [[ "$n" -eq 1 ]]; then
      console_arn="$others"
      echo "  detected console user (only non-CLI Quick user): $(echo "$console_arn" | sed 's|.*/||')"
      save_var CONSOLE_QUICK_USER_ARN "$console_arn" >/dev/null
    else
      echo "  NOTE: ${n} non-CLI Quick users exist — can't auto-pick your console user." >&2
      echo "  In the AWS console open CloudShell, run 'aws sts get-caller-identity', and re-run with" >&2
      echo "    export CONSOLE_CALLER_ARN='<the Arn it prints>'   (granting CLI user only for now)." >&2
    fi
  fi

  # assemble the principal set (CLI user always; console user if detected and different)
  local -a principals=( "$cli_arn" )
  [[ -n "$console_arn" && "$console_arn" != "$cli_arn" ]] && principals+=( "$console_arn" )
  if [[ ${#principals[@]} -eq 1 ]]; then
    echo "  granting owner access to the CLI user only (single identity / no distinct console user)."
  fi

  local perms; perms="$(mktemp -t qs_perms_XXXX).json"
  { printf '['
    local first=1 p
    for p in "${principals[@]}"; do
      [[ $first -eq 1 ]] || printf ','
      printf '{"Principal":"%s","Actions":[%s]}' "$p" "$actions"; first=0
    done
    printf ']\n'
  } > "$perms"
  if aws quicksight "$verb" --aws-account-id "$ACCOUNT_ID" "$id_flag" "$id" \
      --region "$REGION" --profile "$AWS_PROFILE_SC" --grant-permissions "file://${perms}" >/dev/null; then
    echo "  shared ${kind} ${id} with ${#principals[@]} identity(ies)."
  else
    echo "  WARN: auto-share of ${kind} ${id} failed — grant manually (see docs)" >&2
  fi
  rm -f "$perms"
}

# confirm "prompt" — returns 0 if the user answers y/yes. Honors ASSUME_YES=1 for non-interactive runs.
confirm() {
  local prompt="${1:-Proceed?}" ans
  if [[ "${ASSUME_YES:-0}" == "1" ]]; then echo "${prompt} [auto-yes]"; return 0; fi
  read -r -p "${prompt} [y/N] " ans
  [[ "$ans" == "y" || "$ans" == "Y" || "$ans" == "yes" ]]
}
