"""Shared configuration for the Databricks Genie via AgentCore Gateway sample.

All values come from environment variables so no credentials are stored in the
repo. The four Databricks values are required; see README.md for how to obtain
each one.

    export DATABRICKS_HOST="https://dbc-xxxxxxxx-xxxx.cloud.databricks.com"
    export DATABRICKS_CLIENT_ID="<service principal application ID>"
    export DATABRICKS_CLIENT_SECRET="<OAuth M2M secret>"
    export GENIE_SPACE_ID="<Genie space ID>"
    export AWS_REGION="us-east-1"
"""

import os

try:
    from dotenv import load_dotenv
except ImportError:  # optional dependency; `export`-only workflows still work
    load_dotenv = None

if load_dotenv is not None:
    # .env.example tells the reader to copy it to .env. Nothing loaded that file, so
    # following the instruction produced "Missing required environment variable(s)"
    # even with every value filled in correctly.
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# --- Databricks -------------------------------------------------------------
DATABRICKS_HOST = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
DATABRICKS_CLIENT_ID = os.environ.get("DATABRICKS_CLIENT_ID", "")
DATABRICKS_CLIENT_SECRET = os.environ.get("DATABRICKS_CLIENT_SECRET", "")
GENIE_SPACE_ID = os.environ.get("GENIE_SPACE_ID", "")

# --- AWS --------------------------------------------------------------------
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
# A "global." cross-region inference profile, chosen deliberately over a "us." one: the
# us.* profiles only resolve in US regions. Note that the current Anthropic profiles carry
# no date/version suffix -- "global.anthropic.claude-sonnet-5" is the whole id, verified
# ACTIVE and callable in us-east-1 via get-inference-profile and a converse call.
# Bedrock can also gate an older model line on an account that has not called it recently,
# which surfaces as ResourceNotFoundException on the first question rather than as a
# model-access error, so a current model is the safer default for a fresh account.
# List what your own account can call with: aws bedrock list-inference-profiles
MODEL_ID = os.environ.get("MODEL_ID", "global.anthropic.claude-sonnet-5")

# --- Resource names ---------------------------------------------------------
# Fixed names, deliberately: they keep the walkthrough readable and cleanup unambiguous.
# The consequence is that ONE deployment per AWS account is supported. A second deployment
# -- in this or any other region -- adopts this role name and overwrites the shared inline
# policy under IAM_POLICY_NAME, which breaks the first deployment's tool calls. deploy.py
# fails loudly when it detects an adopted role from another region; it cannot detect a
# concurrent deployment in the same region, so do not run two.
GATEWAY_NAME = "DatabricksGenieGateway"
TARGET_NAME = "DatabricksGenie"
CREDENTIAL_PROVIDER_NAME = "databricks-genie-oauth"
IAM_POLICY_NAME = "DatabricksGenieOAuthAccess"

# --- Local state ------------------------------------------------------------
# Written by deploy.py, read by invoke.py / cleanup.py and by the deployed agent.
STATE_FILE = os.path.join(os.path.dirname(__file__), "gateway_config.json")

SYSTEM_PROMPT = (
    "You answer business questions by calling the Databricks Genie tool exposed "
    "through the gateway. Genie returns governed, lakehouse-native SQL answers. "
    "Be concise and present results in a readable format."
)


def require_databricks_config() -> None:
    """Fail fast with an actionable message if any Databricks value is missing."""
    missing = [
        name
        for name, value in (
            ("DATABRICKS_HOST", DATABRICKS_HOST),
            ("DATABRICKS_CLIENT_ID", DATABRICKS_CLIENT_ID),
            ("DATABRICKS_CLIENT_SECRET", DATABRICKS_CLIENT_SECRET),
            ("GENIE_SPACE_ID", GENIE_SPACE_ID),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + "\nSee the Configuration section of README.md."
        )


def genie_mcp_url() -> str:
    """Databricks-managed Genie MCP endpoint for the configured space."""
    return f"{DATABRICKS_HOST}/api/2.0/mcp/genie/{GENIE_SPACE_ID}"
