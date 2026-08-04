#!/usr/bin/env bash
# Run one Many Model Forecasting notebook as a one-time Databricks Serverless job.
#
# Submits the notebook with the Databricks CLI, captures the run id, polls until the
# run terminates, and reports the result. Serverless GPU (notebook 02) is inherited
# automatically from the notebook's own compute metadata — no accelerator block needed.
#
# Prerequisites (set once, then `source` it — see the walkthrough):
#   DBX_PROFILE      Databricks workspace CLI profile (from `databricks auth login`)
#   WORKSPACE_USER   the identity that profile authenticates as (e.g. you@example.com)
# Both come from .supply-chain-automation-env. Requires: databricks CLI, jq.
#
# The notebook location is derived from (override any of these if you imported the
# Git folder elsewhere; defaults match this solution's import step):
#   NB_ROOT      workspace root that holds the Git folder  [default /Workspace/Users/$WORKSPACE_USER]
#   REPO_NAME    the imported Git folder name              [default many-model-forecasting]
#   REPO_SUBDIR  path within the repo to the notebooks     [default examples/fresh_retail_net]
#
# Usage:
#   ./run_notebook.sh 01          # run notebook 01 by number
#   ./run_notebook.sh 02          # notebook 02 (Serverless GPU, inherited)
#   ./run_notebook.sh 04_genie_views_setup   # or by full notebook name
#
# Run order for this solution: 01 -> 02 -> 04 -> 03.
#
# Notebook 04 exposes a MODEL_FILTER variable. This solution uses Chronos-2 only, so
# before submitting 04 this script exports the notebook, sets MODEL_FILTER accordingly,
# and imports it back. Override the value (or expose all models) with MODEL_FILTER:
#   ./run_notebook.sh 04                 # sets MODEL_FILTER="Chronos2" (default)
#   MODEL_FILTER=TimesFM ./run_notebook.sh 04
#   MODEL_FILTER=None    ./run_notebook.sh 04   # expose every model MMF ran
set -euo pipefail

# ---- resolve inputs -------------------------------------------------------
: "${DBX_PROFILE:?Set DBX_PROFILE (source .supply-chain-automation-env)}"
: "${WORKSPACE_USER:?Set WORKSPACE_USER (source .supply-chain-automation-env)}"

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <notebook>   e.g. $0 01   or   $0 04_genie_views_setup" >&2
  exit 2
fi
ARG="$1"

# Map a bare number (01..04) to the full notebook name; otherwise use the arg as-is.
case "$ARG" in
  01) NB="01_fresh_retail_net_data_prep" ;;
  02) NB="02_fresh_retail_net_mmf_forecast" ;;
  03) NB="03_build_product_location_dims" ;;
  04) NB="04_genie_views_setup" ;;
  *)  NB="$ARG" ;;
esac

# Notebook location — each segment overridable; defaults match the import step.
NB_ROOT="${NB_ROOT:-/Workspace/Users/${WORKSPACE_USER}}"
REPO_NAME="${REPO_NAME:-many-model-forecasting}"
REPO_SUBDIR="${REPO_SUBDIR:-examples/fresh_retail_net}"
NB_PATH="${NB_ROOT}/${REPO_NAME}/${REPO_SUBDIR}/${NB}"
POLL_SECONDS="${POLL_SECONDS:-20}"

echo "Notebook : ${NB}"
echo "Path     : ${NB_PATH}"
echo "Profile  : ${DBX_PROFILE}"

# ---- notebook 04: set MODEL_FILTER before submitting ----------------------
# This solution uses Chronos-2 only. Export the notebook, set the MODEL_FILTER
# line, import it back (overwrite). Default is "Chronos2"; MODEL_FILTER=None
# exposes every model. Idempotent: matches whatever the line is currently set to.
if [[ "$NB" == "04_genie_views_setup" ]]; then
  FILTER="${MODEL_FILTER:-Chronos2}"
  if [[ "$FILTER" == "None" ]]; then
    REPL='MODEL_FILTER = None'
  else
    REPL="MODEL_FILTER = \"${FILTER}\""
  fi
  echo "Setting MODEL_FILTER -> ${REPL#MODEL_FILTER = }"
  TMP_NB="$(mktemp -t nb04_XXXX).py"
  databricks workspace export "$NB_PATH" --format SOURCE --profile "$DBX_PROFILE" > "$TMP_NB"
  # Replace the whole MODEL_FILTER assignment line (value + any trailing comment) with REPL.
  # Matches the top-level `MODEL_FILTER = ...` config line only (not the f-string uses below it).
  sed -E "s/^MODEL_FILTER = .*/${REPL}/" "$TMP_NB" > "${TMP_NB}.out"
  if ! grep -qx "${REPL}" "${TMP_NB}.out"; then
    echo "ERROR: could not set MODEL_FILTER in notebook 04 (no 'MODEL_FILTER = ...' line matched)." >&2
    echo "       Open ${NB_PATH} and confirm the configuration cell still defines MODEL_FILTER." >&2
    rm -f "$TMP_NB" "${TMP_NB}.out"
    exit 1
  fi
  mv "${TMP_NB}.out" "$TMP_NB"
  databricks workspace import "$NB_PATH" --file "$TMP_NB" --format SOURCE \
    --language PYTHON --overwrite --profile "$DBX_PROFILE"
  rm -f "$TMP_NB"
  echo "MODEL_FILTER applied to notebook 04."
fi

# ---- submit ---------------------------------------------------------------
# A notebook task with no cluster block runs on Serverless by default.
# `databricks jobs submit --json` takes an inline string or @file (not stdin),
# so write the request body to a temp file and pass it by path.
SUBMIT_FILE="$(mktemp -t submit_"${NB}"_XXXX).json"
trap 'rm -f "$SUBMIT_FILE"' EXIT
jq -n --arg name "run-${NB}" --arg key "${NB}" --arg path "$NB_PATH" \
  '{run_name:$name, tasks:[{task_key:($key|gsub("[^A-Za-z0-9_]";"_")), notebook_task:{notebook_path:$path}}]}' \
  > "$SUBMIT_FILE"

RUN_ID="$(databricks jobs submit --json "@${SUBMIT_FILE}" --profile "$DBX_PROFILE" --no-wait --output json \
  | jq -r '.run_id')"

if [[ -z "$RUN_ID" || "$RUN_ID" == "null" ]]; then
  echo "ERROR: submit did not return a run_id" >&2
  exit 1
fi
echo "Submitted run_id: ${RUN_ID}"

# ---- poll to completion ---------------------------------------------------
echo -n "Waiting for the run to finish"
while true; do
  STATE="$(databricks jobs get-run "$RUN_ID" --profile "$DBX_PROFILE" --output json \
    | jq -r '.state.life_cycle_state')"
  case "$STATE" in
    TERMINATED|SKIPPED|INTERNAL_ERROR) break ;;
    *) echo -n "." ; sleep "$POLL_SECONDS" ;;
  esac
done
echo

RESULT="$(databricks jobs get-run "$RUN_ID" --profile "$DBX_PROFILE" --output json \
  | jq -r '.state.result_state')"
echo "life_cycle_state: ${STATE}"
echo "result_state    : ${RESULT}"

if [[ "$STATE" == "TERMINATED" && "$RESULT" == "SUCCESS" ]]; then
  echo "OK: ${NB} completed successfully."
  exit 0
fi
echo "FAILED: ${NB} ended ${STATE}/${RESULT}. Inspect the run:" >&2
echo "  databricks jobs get-run ${RUN_ID} --profile ${DBX_PROFILE} --output json | jq '.state, .tasks[].state'" >&2
exit 1
