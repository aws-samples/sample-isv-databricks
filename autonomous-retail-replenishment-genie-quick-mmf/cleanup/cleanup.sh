#!/usr/bin/env bash
# Autonomous Supply Chain — full teardown (AWS CLI + Databricks CLI).
#
# Reads values from .supply-chain-automation-env (+ scripts/.env.generated), AUTO-DISCOVERS the
# per-resource ids by name, shows you exactly what will be deleted, and asks for confirmation
# before deleting anything. Delete order is dependency-safe (dependents before dependencies).
#
#   source .supply-chain-automation-env
#   ./cleanup/cleanup.sh                 # discover -> confirm -> delete
#   ASSUME_YES=1 ./cleanup/cleanup.sh     # non-interactive (skip the confirm prompt)
#
# Per-resource teardown only. The BILLABLE/standing account-level deletes (Quick subscription,
# S3 Tables IAM role, Databricks OAuth app) stay commented at the bottom — run by hand when fully done.
set -uo pipefail   # NOTE: no -e — a already-deleted resource must not abort the rest of the teardown

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../scripts/lib.sh
source "${SCRIPT_DIR}/../scripts/lib.sh"
load_generated

# ---- required base values -------------------------------------------------
require_vars \
  "ACCOUNT_ID:aws sts get-caller-identity" \
  "REGION:your Amazon Quick region" \
  "AWS_PROFILE_SC:your AWS CLI profile" \
  "DBX_PROFILE:your Databricks workspace CLI profile"

AWS_PROFILE="$AWS_PROFILE_SC"
ORDER_API_REGION="${ORDER_API_REGION:-$REGION}"
S3T_BUCKET="${S3T_BUCKET:-}"
q() { aws quicksight "$@" --aws-account-id "$ACCOUNT_ID" --region "$REGION" --profile "$AWS_PROFILE"; }

echo "=== Discovering resources to delete (account ${ACCOUNT_ID}, region ${REGION}) ==="

# ---- auto-discover ids (prefer .env.generated, else look up by name) ------
FLOW_ID="${FLOW_ID:-$(q list-flows --query "FlowSummaryList[?Name=='Supply Chain Replenishment Flow'].FlowId | [0]" --output text 2>/dev/null)}"
SPACE_ID="${QUICK_SUPPLIER_SPACE_ID:-supplier-availability-space}"
DATASET_ID="${DATASET_ID:-$(q list-data-sets --query "DataSetSummaries[?Name=='supplier_availability'].DataSetId | [0]" --output text 2>/dev/null)}"
# analysis auto-created when you click Visualize on the dataset (name starts 'supplier_availability')
ANALYSIS_ID="${ANALYSIS_ID:-$(q list-analyses --query "AnalysisSummaryList[?starts_with(Name, 'supplier_availability')].AnalysisId | [0]" --output text 2>/dev/null)}"
DATA_SOURCE_ID="${DATA_SOURCE_ID:-$(q list-data-sources --query "DataSources[?Name=='Supplier Availability S3 Tables'].DataSourceId | [0]" --output text 2>/dev/null)}"
GENIE_MCP_CONNECTOR_ID="${GENIE_MCP_CONNECTOR_ID:-$(q list-action-connectors --query "ActionConnectorSummaries[?Type=='MODEL_CONTEXT_PROTOCOL'].ActionConnectorId | [0]" --output text 2>/dev/null)}"
OPENAPI_CONNECTOR_ID="${OPENAPI_ACTION_CONNECTOR_ID:-$(q list-action-connectors --query "ActionConnectorSummaries[?Type=='OPEN_API'].ActionConnectorId | [0]" --output text 2>/dev/null)}"
# Harvest the Databricks OAuth client_id from the MCP connector NOW, while it still exists (the connector
# is deleted in the per-resource teardown below). describe returns the ClientId, not the secret. This lets
# us delete the Databricks account-level OAuth app afterward without the user supplying the id by hand.
if [[ -z "${OAUTH_CLIENT_ID:-}" && -n "$GENIE_MCP_CONNECTOR_ID" && "$GENIE_MCP_CONNECTOR_ID" != "None" ]]; then
  OAUTH_CLIENT_ID="$(q describe-action-connector --action-connector-id "$GENIE_MCP_CONNECTOR_ID" \
    --query "ActionConnector.AuthenticationConfig.AuthenticationMetadata.AuthorizationCodeGrantMetadata.ReadAuthorizationCodeGrantDetails.ClientId" \
    --output text 2>/dev/null)"
  [[ "$OAUTH_CLIENT_ID" == "None" ]] && OAUTH_CLIENT_ID=""
fi
# Databricks ids: use env if set, else auto-discover by name/title (symmetric with the Quick lookups above).
GENIE_SPACE_TITLE="${GENIE_SPACE_TITLE:-Supply Chain Demand Forecasting (Chronos-2)}"
WAREHOUSE_NAME="${WAREHOUSE_NAME:-Supply Chain Serverless Warehouse}"

# genie_ids_by_title TITLE: print every space_id whose title is exactly TITLE, one per line, reading
# ALL pages of list-spaces (one page holds at most 100). Returns 1 if any page cannot be read, so a
# partial listing is never reported as a complete one.
genie_ids_by_title() {
  local title="$1" token="" page pages=0
  while (( pages < 100 )); do
    local args=(genie list-spaces --profile "$DBX_PROFILE" --page-size 100 --output json)
    [[ -n "$token" ]] && args+=(--page-token "$token")
    page="$(databricks "${args[@]}" 2>/dev/null)" || return 1
    jq -r --arg t "$title" '.spaces[]? | select(.title==$t) | .space_id' <<<"$page" || return 1
    token="$(jq -r '.next_page_token // empty' <<<"$page")"
    [[ -n "$token" ]] || return 0
    pages=$((pages + 1))
  done
  return 1   # still paging after 100 pages: treat as unreadable rather than loop forever
}

# The Agent is trashed only by id: GENIE_SPACE_ID, which setup_databricks.sh records for the Agent it
# creates and the console path sets by hand. An Agent found only by its title is listed with the
# command to trash it, and kept: in a shared workspace even the only Agent with the scripted title
# can be someone else's.
GENIE_SPACE_NOTE=""
GENIE_FOUND_BY_TITLE=""
if [[ -z "${GENIE_SPACE_ID:-}" ]]; then
  # awk drops an id already seen on an earlier page; pipefail (set above) still carries a failed
  # listing through it.
  if ! GENIE_FOUND_BY_TITLE="$(genie_ids_by_title "$GENIE_SPACE_TITLE" | awk '!seen[$0]++')"; then
    GENIE_FOUND_BY_TITLE=""
    GENIE_SPACE_NOTE="<could not list Genie Agents; set GENIE_SPACE_ID; will skip>"
  fi
fi
GENIE_SPACE_ID="${GENIE_SPACE_ID:-}"

# The warehouse is deleted only if setup_databricks.sh CREATED it, which it records as
# WAREHOUSE_CREATED_BY_SETUP. WAREHOUSE_ID alone proves nothing: setup saves it for a warehouse the
# reader asked it to reuse too. Any other warehouse found here, by id or by the scripted name, is
# listed and kept; a shared workspace can hold several warehouses with that name.
if [[ -n "${WAREHOUSE_ID:-}" ]]; then
  WAREHOUSE_FOUND="$WAREHOUSE_ID"
else
  WAREHOUSE_FOUND="$(databricks warehouses list --profile "$DBX_PROFILE" --output json 2>/dev/null \
    | jq -r --arg n "$WAREHOUSE_NAME" '.[]? | select(.name==$n) | .id')"
fi
WAREHOUSE_TO_DELETE="${WAREHOUSE_CREATED_BY_SETUP:-}"
WAREHOUSE_KEPT="$(awk -v d="$WAREHOUSE_TO_DELETE" 'NF && $0 != d' <<<"$WAREHOUSE_FOUND")"

# normalize "None"/empty from the CLI to empty
for v in FLOW_ID DATASET_ID ANALYSIS_ID DATA_SOURCE_ID GENIE_MCP_CONNECTOR_ID OPENAPI_CONNECTOR_ID; do
  [[ "${!v}" == "None" ]] && printf -v "$v" '%s' ""
done

# ---- show the blast radius ------------------------------------------------
show() { printf '  %-26s %s\n' "$1" "${2:-<not found — will skip>}"; }
echo
echo "The following will be DELETED (blank = skipped):"
show "Quick flow"            "$FLOW_ID"
show "Quick space"           "$SPACE_ID"
show "Quick analysis"        "$ANALYSIS_ID"
show "Quick dataset"         "$DATASET_ID"
show "Quick data source"     "$DATA_SOURCE_ID"
show "Quick MCP connector"   "$GENIE_MCP_CONNECTOR_ID"
show "Quick OpenAPI connector" "$OPENAPI_CONNECTOR_ID"
show "S3 Tables bucket"      "${S3T_BUCKET:+$S3T_BUCKET (table+namespace+bucket)}"
show "Order API stack"       "supplier-order-api (region ${ORDER_API_REGION})"
# When nothing will be deleted but something was found, say so instead of show's "not found".
GENIE_PREVIEW="${GENIE_SPACE_ID:-$GENIE_SPACE_NOTE}"
[[ -z "$GENIE_PREVIEW" && -n "$GENIE_FOUND_BY_TITLE" ]] && GENIE_PREVIEW="<no recorded id; will keep the Agent listed below>"
WAREHOUSE_PREVIEW="$WAREHOUSE_TO_DELETE"
[[ -z "$WAREHOUSE_PREVIEW" && -n "$WAREHOUSE_KEPT" ]] && WAREHOUSE_PREVIEW="<no record that setup created one; will keep the warehouse listed below>"
show "Genie Agent (Databricks)" "$GENIE_PREVIEW"
[[ -n "$GENIE_FOUND_BY_TITLE" ]] && show "Genie Agent KEPT" "$(paste -s -d ' ' - <<<"$GENIE_FOUND_BY_TITLE") (found by title only)"
show "SQL warehouse (Databricks)" "$WAREHOUSE_PREVIEW"
[[ -n "$WAREHOUSE_KEPT" ]] && show "SQL warehouse KEPT" "$(paste -s -d ' ' - <<<"$WAREHOUSE_KEPT") (cannot confirm setup created it)"
show "Databricks catalog"    "mmf (--force cascade)"
echo
echo ">>> This is PER-RESOURCE teardown. The billable Quick subscription, S3 Tables IAM role, and"
echo ">>> Databricks OAuth app are NOT touched (see commented section at the bottom)."
echo
confirm "Delete all of the above?" || { echo "Aborted — nothing deleted."; exit 0; }

# ---- delete helpers -------------------------------------------------------
# del LABEL CMD... — run a delete, classify the result into a clean one-line status:
#   deleted        success
#   already gone   resource not found / does not exist (nothing to do — not an error)
#   FAILED         anything else (prints the real message so it isn't hidden)
del() {
  local label="$1"; shift
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  if [[ $rc -eq 0 ]]; then
    echo "  ✓ ${label}: deleted"
  elif grep -qiE 'not.?found|does not exist|ResourceNotFound|NoSuchEntity|no.*exist' <<<"$out"; then
    echo "  – ${label}: already gone"
  else
    echo "  ✗ ${label}: FAILED — $(head -1 <<<"$out")"
  fi
}
# skip LABEL — resource id was blank (nothing discovered)
skip() { echo "  – ${1}: not present"; }
# kept LABEL REASON CMD — for each id on stdin: found but not deleted, and the command to delete it by hand
kept() {
  local label="$1" reason="$2" cmd="$3" id
  while read -r id; do
    [[ -n "$id" ]] || continue
    echo "  – ${label} ${id}: kept, ${reason}. If it is yours and demo-only:"
    echo "      ${cmd} ${id} --profile ${DBX_PROFILE}"
  done
}

echo; echo "=== Deleting demo resources ==="
[[ -n "$FLOW_ID" ]]                && del "Quick flow"            q delete-flow            --flow-id "$FLOW_ID"                       || skip "Quick flow"
[[ -n "$SPACE_ID" ]]              && del "Quick space"           q delete-space           --space-id "$SPACE_ID"                     || skip "Quick space"
[[ -n "$ANALYSIS_ID" ]]          && del "Quick analysis"        q delete-analysis        --analysis-id "$ANALYSIS_ID"               || skip "Quick analysis"
[[ -n "$DATASET_ID" ]]           && del "Quick dataset"         q delete-data-set        --data-set-id "$DATASET_ID"                || skip "Quick dataset"
[[ -n "$DATA_SOURCE_ID" ]]       && del "Quick data source"     q delete-data-source     --data-source-id "$DATA_SOURCE_ID"         || skip "Quick data source"
[[ -n "$GENIE_MCP_CONNECTOR_ID" ]] && del "Quick MCP connector"   q delete-action-connector --action-connector-id "$GENIE_MCP_CONNECTOR_ID" || skip "Quick MCP connector"
[[ -n "$OPENAPI_CONNECTOR_ID" ]] && del "Quick OpenAPI connector" q delete-action-connector --action-connector-id "$OPENAPI_CONNECTOR_ID"   || skip "Quick OpenAPI connector"

if [[ -n "$S3T_BUCKET" ]]; then
  BARN="arn:aws:s3tables:$REGION:$ACCOUNT_ID:bucket/$S3T_BUCKET"
  del "S3 Tables table"     aws s3tables delete-table        --table-bucket-arn "$BARN" --namespace supply_chain --name supplier_availability --region "$REGION" --profile "$AWS_PROFILE"
  del "S3 Tables namespace" aws s3tables delete-namespace    --table-bucket-arn "$BARN" --namespace supply_chain --region "$REGION" --profile "$AWS_PROFILE"
  del "S3 Tables bucket"    aws s3tables delete-table-bucket --table-bucket-arn "$BARN" --region "$REGION" --profile "$AWS_PROFILE"
else
  skip "S3 Tables bucket"
fi

del "Order API stack" aws cloudformation delete-stack --stack-name supplier-order-api --region "$ORDER_API_REGION" --profile "$AWS_PROFILE"
aws cloudformation wait stack-delete-complete --stack-name supplier-order-api --region "$ORDER_API_REGION" --profile "$AWS_PROFILE" 2>/dev/null || true

if [[ -n "$GENIE_SPACE_ID" ]]; then
  del "Genie Agent (Databricks)" databricks genie trash-space "$GENIE_SPACE_ID" --profile "$DBX_PROFILE"
elif [[ -n "$GENIE_SPACE_NOTE" ]]; then
  echo "  – Genie Agent (Databricks): skipped ${GENIE_SPACE_NOTE}"
elif [[ -z "$GENIE_FOUND_BY_TITLE" ]]; then
  skip "Genie Agent (Databricks)"
fi
kept "Genie Agent" "found by title only" "databricks genie trash-space" <<<"$GENIE_FOUND_BY_TITLE"
if [[ -n "$WAREHOUSE_TO_DELETE" ]]; then
  del "SQL warehouse (Databricks)" databricks warehouses delete "$WAREHOUSE_TO_DELETE" --profile "$DBX_PROFILE"
elif [[ -z "$WAREHOUSE_KEPT" ]]; then
  skip "SQL warehouse (Databricks)"
fi
kept "SQL warehouse" "cleanup cannot confirm setup_databricks.sh created it" "databricks warehouses delete" <<<"$WAREHOUSE_KEPT"
del "Databricks catalog mmf" databricks catalogs delete mmf --force --profile "$DBX_PROFILE"

echo; echo "=== Per-resource teardown complete. ==="

# ---------------------------------------------------------------------------
# Amazon Quick subscription (account-wide, IRREVERSIBLE — home region is permanent).
# Flow: scan the account for assets -> if it contains ONLY demo assets, ask the user
# to confirm that, then ask whether to delete the subscription; delete only on yes.
# Fail-safe: any non-demo asset, or any asset type that cannot be enumerated, means
# "not demo-only" and the subscription is left untouched (no env-var trigger).
# ---------------------------------------------------------------------------
# remove_quick_iam_role — delete the Quick-only S3 Tables IAM role. Called whenever the Quick
# subscription is gone (just-deleted OR already UNSUBSCRIBED): the role exists solely to serve Quick's
# S3 Tables access, so once there is no subscription it is orphaned. No-op if the role doesn't exist.
remove_quick_iam_role() {
  local role="aws-quicksight-s3-tables-role-v0" pol="arn:aws:iam::${ACCOUNT_ID}:policy/service-role/AWSQuickSightS3TablesAccess"
  if ! aws iam get-role --role-name "$role" --profile "$AWS_PROFILE" >/dev/null 2>&1; then
    echo "  – Quick S3 Tables IAM role: not present"
    return 0
  fi
  echo "Removing the Quick S3 Tables IAM role (exists only for Quick's S3 Tables access)..."
  del "IAM detach policy" aws iam detach-role-policy --role-name "$role" --policy-arn "$pol" --profile "$AWS_PROFILE"
  del "IAM role"          aws iam delete-role        --role-name "$role" --profile "$AWS_PROFILE"
  del "IAM policy"        aws iam delete-policy       --policy-arn "$pol" --profile "$AWS_PROFILE"
}

subscription_check_and_delete() {
  log "Amazon Quick subscription — scan for demo-only, then confirm"

  # Short-circuit if there is no subscription to act on (e.g. already deleted, or never provisioned).
  # Without this, every list-* below ERRs and the scan misreports "assets beyond this demo".
  local sub_status
  sub_status="$(aws quicksight describe-account-subscription --aws-account-id "$ACCOUNT_ID" \
    --region "$REGION" --profile "$AWS_PROFILE" --query 'AccountInfo.AccountSubscriptionStatus' \
    --output text 2>/dev/null)"
  if [[ "$sub_status" != "ACCOUNT_CREATED" ]]; then
    echo "  – No active Quick subscription in ${REGION} (status: ${sub_status:-none}). Nothing to delete."
    # Subscription already gone -> clean up the now-orphaned Quick S3 Tables IAM role too.
    remove_quick_iam_role
    return 0
  fi

  local demo_ok=1 extras=""

  # Amazon Quick SEEDS built-in sample flows in every account (Whiteboard Notes, RFP Response, etc.);
  # those plus this demo's own flow are NOT "other apps' assets". We count only flows that are neither
  # a known sample NOR the demo flow. Demo creates NO dashboards/topics/folders; data sets/sources/
  # connectors/analysis/flow are deleted in the teardown above, so all should be 0/sample-only now.
  # Any leftover non-demo/non-sample asset, or any un-enumerable type (ERR), means NOT demo-only.
  local SAMPLE_FLOWS='Whiteboard Notes Generator|Flows Idea Generator|5-Why Root Cause Analysis|Marketing Video Analyzer|Social Media Content Creator|Job Description Generator|Product Launch Email Generator|Customer Interest Optimizer|Flows Prompt Helper|RFP Response Generator'
  local dash topics folders dsets dsrc conns ana flows
  dash="$(q list-dashboards --query 'length(DashboardSummaryList)' --output text 2>/dev/null || echo ERR)"
  topics="$(q list-topics --query 'length(TopicsSummaries)' --output text 2>/dev/null || echo ERR)"
  folders="$(q list-folders --query 'length(FolderSummaryList)' --output text 2>/dev/null || echo ERR)"
  dsets="$(q list-data-sets --query 'length(DataSetSummaries)' --output text 2>/dev/null || echo ERR)"
  dsrc="$(q list-data-sources --query 'length(DataSources)' --output text 2>/dev/null || echo ERR)"
  conns="$(q list-action-connectors --query 'length(ActionConnectorSummaries)' --output text 2>/dev/null || echo ERR)"
  # analyses/flows: count only names that are NOT the demo's and NOT a known Quick sample flow.
  # (grep -c exits 1 on zero matches, so count with awk to distinguish "0" from a real error.)
  local ana_names flow_names
  if ana_names="$(q list-analyses --query "AnalysisSummaryList[].Name" --output text 2>/dev/null)"; then
    ana="$(printf '%s' "$ana_names" | tr '\t' '\n' | grep -vE "^supplier_availability" | grep -c '[^[:space:]]' || true)"
  else ana=ERR; fi
  if flow_names="$(q list-flows --query "FlowSummaryList[].Name" --output text 2>/dev/null)"; then
    flows="$(printf '%s' "$flow_names" | tr '\t' '\n' | grep -vxE "(${SAMPLE_FLOWS}|Supply Chain Replenishment Flow)" | grep -c '[^[:space:]]' || true)"
  else flows=ERR; fi

  echo "Current Quick inventory (after per-resource teardown):"
  printf '  dashboards=%s  analyses=%s  topics=%s  folders=%s\n' "$dash" "$ana" "$topics" "$folders"
  printf '  data_sets=%s  data_sources=%s  action_connectors=%s  flows=%s\n' "$dsets" "$dsrc" "$conns" "$flows"

  for pair in "dashboards:$dash" "analyses:$ana" "topics:$topics" "folders:$folders" \
              "data_sets:$dsets" "data_sources:$dsrc" "action_connectors:$conns" "flows:$flows"; do
    local name="${pair%%:*}" val="${pair#*:}"
    if [[ "$val" == "ERR" ]]; then demo_ok=0; extras+=" ${name}(could-not-enumerate)"; fi
    if [[ "$val" =~ ^[0-9]+$ && "$val" -gt 0 ]]; then demo_ok=0; extras+=" ${name}=${val}"; fi
  done

  if [[ $demo_ok -ne 1 ]]; then
    echo
    echo "This Quick subscription contains assets beyond this demo (or a type that could not be verified):${extras}"
    echo "Those may belong to other applications — the subscription will NOT be deleted."
    return 0
  fi

  echo
  echo "This Quick subscription appears to contain ONLY this demo's assets."
  echo "Users who would lose Quick access if it is deleted:"
  q list-users --namespace default --query "UserList[].UserName" --output text 2>/dev/null | sed 's/^/  /' || echo "  (could not list users)"
  echo
  # Confirm 1 — the user vouches it is demo-only (the scan can't see every possible asset type).
  confirm "Confirm this Quick subscription contains ONLY this demo's assets?" || {
    echo "Not confirmed — subscription kept."; return 0; }
  # Confirm 2 — deleting is account-wide and IRREVERSIBLE (home region is permanent).
  echo "NOTE: deleting removes the entire Quick account for ${ACCOUNT_ID}; the home region (${REGION}) is permanent."
  confirm "Delete the Amazon Quick subscription now?" || {
    echo "Aborted — subscription kept."; return 0; }

  echo "Disabling termination protection, then deleting the subscription..."
  aws quicksight update-account-settings --aws-account-id "$ACCOUNT_ID" --default-namespace default \
    --no-termination-protection-enabled --region "$REGION" --profile "$AWS_PROFILE" || true
  aws quicksight delete-account-subscription --aws-account-id "$ACCOUNT_ID" \
    --region "$REGION" --profile "$AWS_PROFILE"
  echo "Subscription deletion requested."

  # Subscription is gone -> remove the now-orphaned Quick-only S3 Tables IAM role.
  remove_quick_iam_role
}

subscription_check_and_delete

# ---------------------------------------------------------------------------
# Databricks OAuth app (Quick->Genie connector). It lives in the Databricks ACCOUNT (not AWS), so
# deleting the Quick subscription never removes it. Offer to delete it on confirmation; needs the
# account-admin profile. The client id comes from .env.generated (setup saved it) or list below.
# ---------------------------------------------------------------------------
echo
if [[ -n "${OAUTH_CLIENT_ID:-}" && -n "${DBX_ACCOUNT_PROFILE:-}" ]]; then
  if confirm "Delete the Databricks OAuth app (client_id ${OAUTH_CLIENT_ID}) used by the Quick->Genie connector?"; then
    del "Databricks OAuth app" databricks account custom-app-integration delete "$OAUTH_CLIENT_ID" --profile "$DBX_ACCOUNT_PROFILE"
  else
    echo "  – Databricks OAuth app: kept."
  fi
else
  echo "Databricks OAuth app — NOT deleted (need OAUTH_CLIENT_ID + DBX_ACCOUNT_PROFILE). It lives in the"
  echo "Databricks account, so the Quick subscription delete does not remove it. To delete it by hand:"
  echo "  # find the client id:"
  echo "  databricks account custom-app-integration list --profile <acct-admin-profile>"
  echo "  databricks account custom-app-integration delete <OAUTH_CLIENT_ID> --profile <acct-admin-profile>"
fi
