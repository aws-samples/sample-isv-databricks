#!/usr/bin/env bash
# Databricks-side setup for the autonomous supply chain solution.
#
# Runs the CLI-automatable Databricks steps in order:
#   1. Import the forecasting notebooks (Git folder)
#   2. Run notebooks 01 -> 02 -> 04 -> 03 (delegates to notebooks/run_notebook.sh)
#   3. Create the Genie Agent (SQL warehouse + genie create-space from genie/genie_space.json)
#   4. Register the Databricks OAuth app for Amazon Quick (account-admin)
#
# All values come from .supply-chain-automation-env (source it first). No hardcoded values.
# Requires: databricks CLI v0.299.0+, jq. Run from the repository root.
#
# Usage:
#   source .supply-chain-automation-env
#   ./scripts/setup_databricks.sh                 # all phases
#   ./scripts/setup_databricks.sh notebooks        # just import + run notebooks
#   ./scripts/setup_databricks.sh genie            # just the Genie Agent
#   ./scripts/setup_databricks.sh oauth            # just the OAuth app
set -euo pipefail

# ---- shared helpers + persisted values ------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"
load_generated   # pick up ids saved by earlier phases/runs

# ---- resolve inputs (preflight: report ALL missing at once) ---------------
require_vars \
  "DBX_PROFILE:workspace CLI profile — databricks auth login" \
  "WORKSPACE_USER:from databricks auth describe (the User line)" \
  "WORKSPACE_HOST:workspace host, no https:// (dbc-xxxx.cloud.databricks.com)"

REPO_URL="${REPO_URL:-https://github.com/databricks-industry-solutions/many-model-forecasting.git}"
NB_ROOT="${NB_ROOT:-/Workspace/Users/${WORKSPACE_USER}}"
REPO_NAME="${REPO_NAME:-many-model-forecasting}"
RUN_NB="${SCRIPT_DIR}/../notebooks/run_notebook.sh"
GENIE_ARTIFACT="${GENIE_ARTIFACT:-genie/genie_space.json}"
PHASE="${1:-all}"
# log() comes from lib.sh

# ---- 1. import notebooks --------------------------------------------------
import_notebooks() {
  log "1. Import forecasting notebooks -> ${NB_ROOT}/${REPO_NAME}"
  if databricks workspace list "${NB_ROOT}/${REPO_NAME}" --profile "$DBX_PROFILE" >/dev/null 2>&1; then
    echo "Git folder already present at ${NB_ROOT}/${REPO_NAME} — skipping import."
  else
    databricks repos create "$REPO_URL" gitHub \
      --path "${NB_ROOT}/${REPO_NAME}" --profile "$DBX_PROFILE"
  fi
  databricks workspace list "${NB_ROOT}/${REPO_NAME}/examples/fresh_retail_net" --profile "$DBX_PROFILE"
}

# ---- 2. run notebooks 01 -> 02 -> 04 -> 03 --------------------------------
run_notebooks() {
  log "2. Run notebooks 01 -> 02 -> 04 -> 03 (serverless; 02 on GPU)"
  [[ -x "$RUN_NB" ]] || { echo "ERROR: $RUN_NB not found/executable" >&2; exit 1; }
  for nb in 01 02 04 03; do
    echo ">>> notebook ${nb}"
    "$RUN_NB" "$nb"
  done
}

# ---- 3. Genie Agent -------------------------------------------------------
create_genie() {
  log "3. Create the Genie Agent"
  [[ -f "$GENIE_ARTIFACT" ]] || { echo "ERROR: $GENIE_ARTIFACT not found (run from repo root)" >&2; exit 1; }

  # Reuse an existing serverless warehouse if WAREHOUSE_ID is set; otherwise create one.
  if [[ -z "${WAREHOUSE_ID:-}" ]]; then
    echo "Creating a serverless SQL warehouse..."
    WAREHOUSE_ID="$(databricks warehouses create --profile "$DBX_PROFILE" \
      --name "Supply Chain Serverless Warehouse" --cluster-size "Small" \
      --warehouse-type PRO --enable-serverless-compute --enable-photon \
      --auto-stop-mins 10 --max-num-clusters 1 --output json | jq -r '.id')"
    echo "Created warehouse: ${WAREHOUSE_ID}"
  else
    echo "Using existing WAREHOUSE_ID=${WAREHOUSE_ID}"
  fi

  BODY="$(mktemp -t genie_body_XXXX).json"
  trap 'rm -f "$BODY"' RETURN
  jq -n --arg wh "$WAREHOUSE_ID" \
        --arg path "/Workspace/Users/${WORKSPACE_USER}" \
        --arg title "Supply Chain Demand Forecasting (Chronos-2)" \
        --arg space "$(jq -c . "$GENIE_ARTIFACT")" \
        '{warehouse_id:$wh, serialized_space:$space, title:$title, parent_path:$path}' > "$BODY"

  SPACE_ID="$(databricks genie create-space --json "@${BODY}" --profile "$DBX_PROFILE" \
    --output json | jq -r '.space_id')"
  echo "Created Genie space_id: ${SPACE_ID}"
  save_var GENIE_SPACE_ID "$SPACE_ID"     # persisted for the Quick MCP connector step
  save_var WAREHOUSE_ID "$WAREHOUSE_ID"
  databricks genie get-space "$SPACE_ID" --profile "$DBX_PROFILE" \
    --output json | jq -r '{space_id, title, warehouse_id}'
}

# ---- 4. OAuth app (account-admin) -----------------------------------------
create_oauth() {
  log "4. Register the Databricks OAuth app for Amazon Quick (account-admin)"
  require_vars \
    "DBX_ACCOUNT_PROFILE:account-admin profile" \
    "DBX_ACCOUNT_ID:Databricks account id" \
    "REGION:your Amazon Quick region"
  echo "This is an ACCOUNT-level action; ${DBX_ACCOUNT_PROFILE} must be account-admin."
  echo "If not logged in, run: databricks auth login --host https://accounts.cloud.databricks.com --account-id \$DBX_ACCOUNT_ID --profile \$DBX_ACCOUNT_PROFILE"
  OUT="$(databricks account custom-app-integration create --profile "$DBX_ACCOUNT_PROFILE" --json "$(jq -n \
    --arg redir "https://${REGION}.quicksight.aws.amazon.com/sn/oauthcallback" \
    '{name:"Amazon Quick Connector", confidential:true, redirect_urls:[$redir],
      scopes:["all-apis","offline_access","openid","email","profile"],
      token_access_policy:{access_token_ttl_in_minutes:1440, refresh_token_ttl_in_minutes:129600}}')")"
  CLIENT_ID="$(echo "$OUT" | jq -r '.client_id')"
  save_var OAUTH_CLIENT_ID "$CLIENT_ID"    # persisted for the Quick MCP connector step (id only)
  echo
  echo ">>> COPY THE client_secret NOW — Databricks shows it once and never again."
  echo ">>> Store it in a secrets manager; paste it into the Quick MCP connector (console step)."
  echo ">>> The secret is NOT saved to disk here (only the client_id is). Do NOT commit or paste it in chat."
  echo "client_secret (copy now, then clear your terminal scrollback):"
  echo "$OUT" | jq -r '.client_secret'
}

case "$PHASE" in
  all)       import_notebooks; run_notebooks; create_genie; create_oauth ;;
  notebooks) import_notebooks; run_notebooks ;;
  genie)     create_genie ;;
  oauth)     create_oauth ;;
  *) echo "Usage: $0 [all|notebooks|genie|oauth]" >&2; exit 2 ;;
esac

log "Databricks setup phase '${PHASE}' complete."
echo "Next: the Amazon Quick console steps (MCP + OpenAPI connectors, S3 Tables grant, dataset)"
echo "then ./scripts/setup_aws.sh for the CLI-automatable AWS/Quick steps."
