#!/usr/bin/env bash
# AWS + Amazon Quick setup for the autonomous supply chain solution (CLI-automatable steps).
#
# Phases (run in order):
#   order-api   Deploy the Supplier Order API (CloudFormation) and print ApiBaseUrl
#   feed        Create the S3 Tables bucket and load the supplier feed (PyIceberg via uv)
#   quick-account  Provision the Amazon Quick Enterprise subscription
#   datasource  Create the S3 Tables data source and share it to both identities
#   space       Create the supplier space and add the dataset (DATA_SET)
#   flow        Fill placeholders in flow/flow_definition.json, create-flow, share it
#
# CONSOLE-ONLY steps are NOT scripted (the CLI rejects them) — the script STOPS and tells you
# which console step to do. See docs/QUICK_CONSOLE_GUIDE.md:
#   - S3 Tables resource-access grant (before 'datasource')
#   - Genie MCP connector, OpenAPI connector (before 'flow')
#   - supplier_availability dataset (before 'space')
#
# All values come from .supply-chain-automation-env. No hardcoded values.
# Requires: AWS CLI v2.36.2+, jq, uv (or Python 3.11). Run from the repository root.
#
# Usage:
#   source .supply-chain-automation-env
#   ./scripts/setup_aws.sh order-api
#   ./scripts/setup_aws.sh feed
#   ... (run phases in order, doing the console steps in between)
set -euo pipefail

# ---- shared helpers + persisted values ------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"
load_generated   # pick up ids saved by earlier phases (data source, space, flow, connectors)

# base vars every phase needs
require_vars \
  "AWS_PROFILE_SC:your AWS CLI profile" \
  "ACCOUNT_ID:aws sts get-caller-identity" \
  "REGION:the single AWS region for the whole solution"

BUCKET_ARN="arn:aws:s3tables:${REGION}:${ACCOUNT_ID}:bucket/${S3T_BUCKET:-}"
DATA_SOURCE_ID="${DATA_SOURCE_ID:-supplier-availability-s3t}"
SPACE_ID="${QUICK_SUPPLIER_SPACE_ID:-supplier-availability-space}"
PHASE="${1:-}"

# log() comes from lib.sh
stop() { printf '\n*** CONSOLE STEP REQUIRED ***\n%s\nSee docs/QUICK_CONSOLE_GUIDE.md. Re-run this script for the next phase when done.\n' "$1"; }

order_api() {
  log "Deploy the Supplier Order API (CloudFormation)"
  aws cloudformation deploy --template-file order-api/supplier-order-api.yaml \
    --stack-name supplier-order-api --capabilities CAPABILITY_NAMED_IAM \
    --parameter-overrides ApiKeyValue="$(openssl rand -hex 24)" \
    --region "$REGION" --profile "$AWS_PROFILE_SC"
  local api_base_url
  api_base_url="$(aws cloudformation describe-stacks --stack-name supplier-order-api --region "$REGION" \
    --profile "$AWS_PROFILE_SC" --query "Stacks[0].Outputs[?OutputKey=='ApiBaseUrl'].OutputValue" --output text)"
  save_var API_BASE_URL "$api_base_url"   # the OpenAPI connector's Base URL
  echo "ApiBaseUrl: ${api_base_url}"
}

feed() {
  log "Create S3 Tables bucket + load supplier feed"
  # The loader reads the (product_id, city_id) keys from the retailer's Databricks table
  # (mmf.fresh_retail_net.daily_sales_raw) via the SQL warehouse -- no third-party dataset download.
  require_vars "S3T_BUCKET:name for the S3 Tables supplier bucket" \
    "DBX_PROFILE:authenticated Databricks CLI profile (loader reads product/location keys)" \
    "WAREHOUSE_ID:Serverless SQL warehouse id the loader queries"
  aws s3tables create-table-bucket --name "$S3T_BUCKET" --region "$REGION" --profile "$AWS_PROFILE_SC" 2>&1 \
    | grep -v 'BucketAlreadyExists' || echo "(bucket may already exist — continuing)"
  eval "$(AWS_PROFILE="$AWS_PROFILE_SC" aws configure export-credentials --format env)"
  AWS_ACCOUNT_ID="$ACCOUNT_ID" AWS_REGION="$REGION" S3T_BUCKET="$S3T_BUCKET" \
    DBX_PROFILE="$DBX_PROFILE" WAREHOUSE_ID="$WAREHOUSE_ID" \
    uv run --python 3.11 \
    --with 'pyiceberg[pyarrow]>=0.9,<0.10' --with 'pyarrow>=17,<22' \
    --with boto3 --with requests \
    supplier-feed/load_supplier_availability.py
  aws s3tables list-tables --table-bucket-arn "$BUCKET_ARN" --namespace supply_chain \
    --region "$REGION" --profile "$AWS_PROFILE_SC"
}

account() {
  log "Provision the Amazon Quick Enterprise account (adopt existing, or create)"
  # A Quick subscription is ACCOUNT-WIDE (one per AWS account). Adopt a pre-existing one rather than
  # error, and record whether WE created it so cleanup only ever deletes a demo-provisioned subscription.
  local status
  status="$(aws quicksight describe-account-subscription --aws-account-id "$ACCOUNT_ID" \
    --region "$REGION" --profile "$AWS_PROFILE_SC" \
    --query 'AccountInfo.AccountSubscriptionStatus' --output text 2>/dev/null || true)"
  if [[ "$status" == "ACCOUNT_CREATED" ]]; then
    local name edition
    name="$(aws quicksight describe-account-subscription --aws-account-id "$ACCOUNT_ID" --region "$REGION" \
      --profile "$AWS_PROFILE_SC" --query 'AccountInfo.AccountName' --output text 2>/dev/null)"
    edition="$(aws quicksight describe-account-subscription --aws-account-id "$ACCOUNT_ID" --region "$REGION" \
      --profile "$AWS_PROFILE_SC" --query 'AccountInfo.Edition' --output text 2>/dev/null)"
    echo "Existing Quick subscription found: '${name}' (${edition}). Adopting it — NOT creating a new one."
    echo "NOTE: a Quick subscription is account-wide; it may be used by other apps. This demo will NOT"
    echo "      delete it on cleanup (only resources the demo creates are torn down)."
    save_var DEMO_CREATED_SUBSCRIPTION false
  else
    require_vars "QUICK_ACCOUNT_NAME:Quick account directory name" "NOTIFICATION_EMAIL:Quick notification email"
    aws quicksight create-account-subscription --edition ENTERPRISE \
      --authentication-method IAM_AND_QUICKSIGHT --aws-account-id "$ACCOUNT_ID" \
      --account-name "$QUICK_ACCOUNT_NAME" --notification-email "$NOTIFICATION_EMAIL" \
      --region "$REGION" --profile "$AWS_PROFILE_SC"
    echo "Created a new Quick Enterprise subscription."
    save_var DEMO_CREATED_SUBSCRIPTION true   # cleanup may delete it (demo-provisioned)
  fi
  echo "CLI Quick user (owns CLI-created resources):"
  aws quicksight list-users --aws-account-id "$ACCOUNT_ID" --namespace default \
    --region "$REGION" --profile "$AWS_PROFILE_SC" \
    --query "UserList[].{User:UserName,Role:Role}" --output table 2>/dev/null || true
}

datasource() {
  stop "Before this phase: grant Amazon Quick access to the S3 Tables bucket in the CONSOLE
(Manage Account -> AWS Resources -> keep 'Use Quick-managed role' -> Allow access & autodiscovery ->
 Amazon S3 Tables -> Select S3 table buckets from current Quick account and Region -> pick bucket -> Save).
Do NOT hand-create aws-quicksight-s3-tables-role-v0."
  log "Create the S3 Tables data source"
  : "${S3T_BUCKET:?Set S3T_BUCKET}"
  aws quicksight create-data-source --aws-account-id "$ACCOUNT_ID" --data-source-id "$DATA_SOURCE_ID" \
    --name "Supplier Availability S3 Tables" --type S3_TABLES \
    --data-source-parameters "{\"S3TablesParameters\":{\"TableBucketArn\":\"${BUCKET_ARN}\"}}" \
    --region "$REGION" --profile "$AWS_PROFILE_SC" 2>&1 | grep -v 'ResourceExistsException' || echo "(data source may already exist)"
  echo "Waiting for CREATION_SUCCESSFUL..."
  until [[ "$(aws quicksight describe-data-source --aws-account-id "$ACCOUNT_ID" --data-source-id "$DATA_SOURCE_ID" \
    --region "$REGION" --profile "$AWS_PROFILE_SC" --query 'DataSource.Status' --output text 2>/dev/null)" == "CREATION_SUCCESSFUL" ]]; do
    sleep 5; printf '.'
  done; echo " ready."
  save_var DATA_SOURCE_ID "$DATA_SOURCE_ID"
  # A CLI-created data source is owned only by the CLI user. Grant owner access to the CLI user and
  # (if different) the console user you name, so it is usable from the console too.
  share_cli_and_console data-source "$DATA_SOURCE_ID"
  # The two action connectors are created in the CONSOLE (owned by the console user), but the flow
  # runs as the CLI user — so the CLI user must also be granted on them or the flow's Detect/Submit/
  # Ticket steps fail at runtime with "You don't have access to this action." Share them here too, once
  # their ids are known (no-op if a connector id isn't set yet).
  [[ -n "${GENIE_MCP_CONNECTOR_ID:-}" ]]     && share_cli_and_console connector "$GENIE_MCP_CONNECTOR_ID"
  [[ -n "${OPENAPI_ACTION_CONNECTOR_ID:-}" ]] && share_cli_and_console connector "$OPENAPI_ACTION_CONNECTOR_ID"
}

space() {
  stop "Before this phase: create the supplier_availability DATASET in the CONSOLE
(Datasets -> New dataset -> data source 'Supplier Availability S3 Tables' -> Direct Query -> Visualize),
then set DATASET_ID in your env."
  log "Create the supplier space + add the dataset"
  require_vars "DATASET_ID:from aws quicksight list-data-sets (console dataset step)"
  aws quicksight create-space --aws-account-id "$ACCOUNT_ID" --space-id "$SPACE_ID" \
    --name "Supplier Availability" \
    --description "Supplier availability data for the autonomous replenishment flow" \
    --region "$REGION" --profile "$AWS_PROFILE_SC" 2>&1 | grep -v 'ResourceExistsException' || echo "(space may already exist)"
  aws quicksight update-space-resources --aws-account-id "$ACCOUNT_ID" --space-id "$SPACE_ID" \
    --add-resources "ResourceType=DATA_SET,ResourceDetails={resourceArn=arn:aws:quicksight:${REGION}:${ACCOUNT_ID}:dataset/${DATASET_ID}}" \
    --region "$REGION" --profile "$AWS_PROFILE_SC"
  aws quicksight list-space-resources --aws-account-id "$ACCOUNT_ID" --space-id "$SPACE_ID" \
    --region "$REGION" --profile "$AWS_PROFILE_SC" --query "SpaceResources"
  save_var QUICK_SUPPLIER_SPACE_ID "$SPACE_ID"
  # Grant owner access to the CLI user and (if different) the console user you name.
  share_cli_and_console space "$SPACE_ID"
}

flow() {
  stop "Before this phase: create the Genie MCP connector AND the OpenAPI connector in the CONSOLE,
then collect their ids (list-action-connectors) and the two OpenAPI action ids."
  log "Create the flow"
  require_vars \
    "GENIE_MCP_CONNECTOR_ID:list-action-connectors (MODEL_CONTEXT_PROTOCOL row)" \
    "OPENAPI_ACTION_CONNECTOR_ID:list-action-connectors (OPEN_API row)" \
    "OPENAPI_SUBMIT_ORDER_ACTION_ID:connector Test action (SubmitOrder)" \
    "OPENAPI_CREATE_TICKET_ACTION_ID:connector Test action (CreateTicket)"
  RESOLVED="$(mktemp -t flow_def_XXXX).json"
  trap 'rm -f "$RESOLVED"' RETURN
  jq --arg acct "$ACCOUNT_ID" --arg region "$REGION" \
     --arg genie "$GENIE_MCP_CONNECTOR_ID" --arg openapi "$OPENAPI_ACTION_CONNECTOR_ID" \
     --arg submit "$OPENAPI_SUBMIT_ORDER_ACTION_ID" --arg ticket "$OPENAPI_CREATE_TICKET_ACTION_ID" \
     --arg space "$SPACE_ID" \
     '.FlowDefinition | walk(if type=="string" then
        gsub("<ACCOUNT_ID>";$acct)|gsub("<REGION>";$region)
        |gsub("<GENIE_MCP_CONNECTOR_ID>";$genie)|gsub("<OPENAPI_ACTION_CONNECTOR_ID>";$openapi)
        |gsub("<OPENAPI_SUBMIT_ORDER_ACTION_ID>";$submit)|gsub("<OPENAPI_CREATE_TICKET_ACTION_ID>";$ticket)
        |gsub("<QUICK_SUPPLIER_SPACE_ID>";$space) else . end)' \
     flow/flow_definition.json > "$RESOLVED"
  if grep -oE '<[A-Z_]+>' "$RESOLVED" | sort -u | grep .; then
    echo "ERROR: unresolved placeholders above — set the missing env vars." >&2; exit 1
  fi
  FLOW_ID="$(aws quicksight create-flow --aws-account-id "$ACCOUNT_ID" \
    --name "Supply Chain Replenishment Flow" \
    --description "Autonomous demand-surge replenishment: detect surges via Genie, look up suppliers, submit orders or raise tickets" \
    --flow-definition "file://${RESOLVED}" --region "$REGION" --profile "$AWS_PROFILE_SC" \
    --query 'FlowId' --output text)"
  save_var FLOW_ID "$FLOW_ID"
  echo "Created FLOW_ID=${FLOW_ID} (PUBLISHED)."
  # Grant owner access to the CLI user and (if different) the console user you name.
  share_cli_and_console flow "$FLOW_ID"
  echo ">>> Then open the flow in the console and schedule it (Run with no confirmation)."
}

case "$PHASE" in
  order-api)     order_api ;;
  feed)          feed ;;
  quick-account) account ;;
  datasource)    datasource ;;
  space)         space ;;
  flow)          flow ;;
  *) echo "Usage: $0 [order-api|feed|quick-account|datasource|space|flow]" >&2
     echo "Run phases in order; do the console steps (guide) in between." >&2; exit 2 ;;
esac
