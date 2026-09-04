"""Strands agent hosted on AgentCore Runtime that queries Databricks Genie
through Amazon Bedrock AgentCore Gateway.

The gateway handles all auth complexity:
  - Inbound:  Cognito JWT validates agent requests
  - Outbound: OAuth2 M2M (client credentials) authenticates with Databricks

Deployed with the AgentCore CLI, not run directly:

    agentcore configure --entrypoint genie_agent.py
    agentcore deploy

Configuration comes from the environment -- GATEWAY_URL, COGNITO_TOKEN_ENDPOINT,
COGNITO_CLIENT_ID, COGNITO_CLIENT_SECRET, COGNITO_SCOPE -- falling back to
gateway_config.json (written by deploy.py) for local runs only. See _load_config.
"""

import json
import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from config import MODEL_ID, STATE_FILE, SYSTEM_PROMPT
from gateway_setup import GatewaySetup
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

app = BedrockAgentCoreApp()

_CONFIG = None

_ENV_KEYS = ("GATEWAY_URL", "COGNITO_TOKEN_ENDPOINT", "COGNITO_CLIENT_ID", "COGNITO_CLIENT_SECRET", "COGNITO_SCOPE")


def _load_config() -> dict:
    """Resolve runtime config, preferring the environment over the state file.

    The deployed agent should not depend on gateway_config.json. That file is gitignored,
    so the build may exclude it from the image (the agent then fails at first use), and if
    it IS included the Cognito client secret is baked into an ECR layer. Passing the five
    values below as Runtime environment variables avoids both -- see the README.

    gateway_config.json remains the local-development path, so `python invoke.py` and a
    locally-run entrypoint keep working with no extra setup.

    Loaded lazily and cached: doing this at import time turned any config problem into a
    container that crash-loops before the entrypoint registers, with no usable error.
    """
    global _CONFIG
    if _CONFIG is not None:
        return _CONFIG
    if os.environ.get("GATEWAY_URL"):
        missing = [k for k in _ENV_KEYS if not os.environ.get(k)]
        if missing:
            raise RuntimeError(
                "GATEWAY_URL is set, so this agent is using environment configuration, "
                f"but these are missing: {', '.join(missing)}"
            )
        _CONFIG = {
            "gateway_url": os.environ["GATEWAY_URL"],
            "client_info": {
                "token_endpoint": os.environ["COGNITO_TOKEN_ENDPOINT"],
                "client_id": os.environ["COGNITO_CLIENT_ID"],
                "client_secret": os.environ["COGNITO_CLIENT_SECRET"],
                "scope": os.environ["COGNITO_SCOPE"],
            },
        }
        return _CONFIG
    try:
        with open(STATE_FILE) as f:
            _CONFIG = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(
            f"No configuration found. Set {', '.join(_ENV_KEYS)} on the Runtime "
            f"(recommended for deployment), or run deploy.py locally to write {STATE_FILE}."
        ) from None
    return _CONFIG


@app.entrypoint
def invoke(payload, context):
    """AgentCore Runtime entrypoint.

    The Cognito token and the MCP session are established per invocation, not once
    at cold start. Client-credentials tokens expire (Cognito defaults to 60 minutes)
    while a warm container lives far longer, so a cold-start token left every request
    after expiry failing with a 401 -- and because the transport factory closed over
    that one token, even an MCP reconnect re-sent the stale value. Paying a little
    setup latency per call is the correct trade.
    """
    config = _load_config()
    token = GatewaySetup.get_access_token(config["client_info"])
    mcp = MCPClient(
        lambda: streamablehttp_client(
            config["gateway_url"],
            headers={"Authorization": f"Bearer {token}"},
        )
    )
    mcp.start()
    try:
        tools = mcp.list_tools_sync()
        agent = Agent(
            # No region_name: strands already resolves it as
            # region_name or session.region_name or AWS_REGION or its own default, so
            # passing config.AWS_REGION would have made a hardcoded us-east-1 default
            # authoritative inside the container, and passing the session value is a no-op.
            model=BedrockModel(model_id=MODEL_ID),
            tools=tools,
            system_prompt=SYSTEM_PROMPT,
        )
        result = agent(payload.get("prompt", ""))
        return {"result": result.message}
    finally:
        mcp.stop(None, None, None)


if __name__ == "__main__":
    app.run()
