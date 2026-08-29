"""Strands agent hosted on AgentCore Runtime that queries Databricks Genie
through Amazon Bedrock AgentCore Gateway.

The gateway handles all auth complexity:
  - Inbound:  Cognito JWT validates agent requests
  - Outbound: OAuth2 M2M (client credentials) authenticates with Databricks

Deployed with the AgentCore CLI, not run directly:

    agentcore configure --entrypoint genie_agent.py
    agentcore deploy

Requires gateway_config.json in the same directory (written by deploy.py).
"""

import json

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from config import AWS_REGION, MODEL_ID, STATE_FILE, SYSTEM_PROMPT
from gateway_setup import GatewaySetup
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

app = BedrockAgentCoreApp()

# STATE_FILE is anchored to this file's directory. A bare open("gateway_config.json")
# resolves against the container's working directory instead, which crash-looped the
# runtime at cold start with an unhandled FileNotFoundError.
with open(STATE_FILE) as f:
    config = json.load(f)


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
            model=BedrockModel(model_id=MODEL_ID, region_name=AWS_REGION),
            tools=tools,
            system_prompt=SYSTEM_PROMPT,
        )
        result = agent(payload.get("prompt", ""))
        return {"result": result.message}
    finally:
        mcp.stop(None, None, None)


if __name__ == "__main__":
    app.run()
