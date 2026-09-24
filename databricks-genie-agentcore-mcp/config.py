"""Shared configuration for the Databricks Genie via AgentCore Gateway sample.

All values come from environment variables so no credentials are stored in the
repo. DATABRICKS_HOST, DATABRICKS_CLIENT_ID and GENIE_SPACE_ID are always required.
For the OAuth secret, supply EITHER the plaintext DATABRICKS_CLIENT_SECRET OR a
Secrets Manager reference DATABRICKS_SECRET_ARN (the production path); see README.md.

    export DATABRICKS_HOST="https://dbc-xxxxxxxx-xxxx.cloud.databricks.com"
    export DATABRICKS_CLIENT_ID="<service principal application ID>"
    export DATABRICKS_CLIENT_SECRET="<OAuth M2M secret>"   # or DATABRICKS_SECRET_ARN
    export GENIE_SPACE_ID="<Genie space ID>"
    export AWS_REGION="us-east-1"
"""

import os
import re
import sys

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

# Optional production path for the OAuth M2M secret. When DATABRICKS_SECRET_ARN is set,
# deploy.py registers the credential provider with clientSecretSource="EXTERNAL" and points
# it at this Secrets Manager secret (see secrets_setup.py) instead of passing the plaintext
# DATABRICKS_CLIENT_SECRET inline. The secret is a JSON document; DATABRICKS_SECRET_JSON_KEY
# names the key that holds the client secret value. With the ARN set, DATABRICKS_CLIENT_SECRET
# is not needed for deploy.py -- the plaintext never has to live in .env or the shell.
DATABRICKS_SECRET_ARN = os.environ.get("DATABRICKS_SECRET_ARN", "")
DATABRICKS_SECRET_JSON_KEY = os.environ.get("DATABRICKS_SECRET_JSON_KEY", "client_secret")
# Whether the operator set the key explicitly (vs. taking the default). deploy.py adopts the
# key secrets_setup.py recorded for the secret UNLESS it was set here, so the ARN and its key
# stay together across the two processes without the operator re-exporting the key.
DATABRICKS_SECRET_JSON_KEY_SET = "DATABRICKS_SECRET_JSON_KEY" in os.environ
# Name of the Secrets Manager secret that secrets_setup.py creates/updates. Only used by
# secrets_setup.py; deploy.py references the secret by ARN via DATABRICKS_SECRET_ARN.
DATABRICKS_SECRET_NAME = os.environ.get(
    "DATABRICKS_SECRET_NAME", "databricks-genie-agentcore/oauth-client-secret"
)
# DATABRICKS_SECRET_ARN is fed verbatim into an IAM policy Resource in deploy.py step 4. A bare
# secret name passes CreateOauth2CredentialProvider but is rejected by put_role_policy AFTER the
# Cognito pool, IAM role, gateway and provider are already built -- so validate its shape up front.
# The name charset is botocore's for this API (excludes '*', whitespace and quotes), so a wildcard
# ARN like ...:secret:* cannot slip through and become an account-wide GetSecretValue grant. The
# 6-char random suffix AWS appends is REQUIRED: a suffix-less ARN is accepted as a SecretId but the
# IAM Resource then matches no secret, so the read is denied and tool calls 403 ~an hour after READY.
# ('-' is placed last in the class so it is a literal, not a range.)
_SECRET_ARN_RE = re.compile(
    r"^arn:aws[a-z0-9-]*:secretsmanager:[a-z0-9-]+:\d{12}:secret:"
    r"[a-zA-Z0-9_/+=.@!-]+-[A-Za-z0-9]{6}$"
)

# Used only by generate_data.py to load the sample dataset. The warehouse is
# optional: if unset, generate_data.py resolves the one backing GENIE_SPACE_ID.
DATABRICKS_WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")
DATABRICKS_CATALOG = os.environ.get("DATABRICKS_CATALOG", "genie_demo")
DATABRICKS_SCHEMA = os.environ.get("DATABRICKS_SCHEMA", "sales")

# Optional separate identity for generate_data.py's DDL (CREATE CATALOG/SCHEMA/TABLE),
# which the query service principal usually can't do. Keeping it distinct from
# DATABRICKS_CLIENT_ID/SECRET means the seeding admin is never written into the gateway's
# outbound credential provider by deploy.py. If unset, generate_data.py falls back to the
# query service principal. This identity only seeds data; it is never used by the gateway.
DATABRICKS_SEED_CLIENT_ID = os.environ.get("DATABRICKS_SEED_CLIENT_ID", "")
DATABRICKS_SEED_CLIENT_SECRET = os.environ.get("DATABRICKS_SEED_CLIENT_SECRET", "")

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
            ("GENIE_SPACE_ID", GENIE_SPACE_ID),
        )
        if not value
    ]
    # The OAuth secret may come from either the plaintext env var or a Secrets Manager
    # reference (DATABRICKS_SECRET_ARN, the production path). At least one must be present.
    if not DATABRICKS_CLIENT_SECRET and not DATABRICKS_SECRET_ARN:
        missing.append("DATABRICKS_CLIENT_SECRET or DATABRICKS_SECRET_ARN")
    if missing:
        raise SystemExit(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + "\nSee the Configuration section of README.md."
        )
    # A malformed ARN would otherwise fail deep in deploy.py step 4, after four resources
    # exist. Reject it here, before anything is created.
    if DATABRICKS_SECRET_ARN and not _SECRET_ARN_RE.match(DATABRICKS_SECRET_ARN):
        raise SystemExit(
            f"DATABRICKS_SECRET_ARN is not a Secrets Manager ARN: {DATABRICKS_SECRET_ARN!r}\n"
            "It is used verbatim as an IAM policy Resource; a bare name is accepted by the "
            "credential-provider API but rejected at deploy step 4, after the gateway stack is "
            "already built. Expected arn:aws:secretsmanager:<region>:<account>:secret:<name>-<suffix> "
            "(with the 6-character suffix AWS assigns; no '*' or whitespace). "
            "Run `python secrets_setup.py` and copy the ARN it prints."
        )
    # jsonKey rides into clientSecretConfig; botocore rejects an empty or >128-char value at
    # deploy step 3, after the pool/role/gateway exist. An empty value also defeats the default
    # and writes a secret keyed "". Only meaningful on the EXTERNAL (ARN) path.
    if DATABRICKS_SECRET_ARN and not (1 <= len(DATABRICKS_SECRET_JSON_KEY) <= 128):
        raise SystemExit(
            f"DATABRICKS_SECRET_JSON_KEY must be 1-128 characters, got {len(DATABRICKS_SECRET_JSON_KEY)}. "
            "It names the key inside the Secrets Manager JSON that holds the OAuth secret."
        )
    # Both set is allowed (the ARN wins in deploy.py), but a stale or typo'd ARN alongside a
    # working plaintext silently takes the EXTERNAL path and only fails at invocation. Warn.
    if DATABRICKS_CLIENT_SECRET and DATABRICKS_SECRET_ARN:
        print(
            "Note: both DATABRICKS_CLIENT_SECRET and DATABRICKS_SECRET_ARN are set; deploy.py "
            "uses the ARN (EXTERNAL path) and ignores the plaintext. Unset one to be explicit.",
            file=sys.stderr,
        )


def genie_mcp_url() -> str:
    """Databricks-managed Genie MCP endpoint for the configured space."""
    return f"{DATABRICKS_HOST}/api/2.0/mcp/genie/{GENIE_SPACE_ID}"
