"""Invoke the agent deployed on AgentCore Runtime.

Run after `agentcore deploy`. Reads the runtime ARN from the .bedrock_agentcore.yaml
the starter toolkit generates.

Usage:
    python invoke_runtime.py
    python invoke_runtime.py "Break down sales by region for the last fiscal year."
"""

import argparse
import json
import uuid

import boto3
import yaml

DEFAULT_PROMPT = "What were our top 5 products by revenue last quarter?"


def new_session_id() -> str:
    """A fresh runtime session per invocation.

    runtimeSessionId is sticky: reusing one literal id meant every run rejoined the
    same session and answered with the previous question's context still in scope,
    and once that session hit its maximum lifetime the call failed outright.
    invoke_agent_runtime requires at least 33 characters, which this satisfies.
    """
    return f"genie-{uuid.uuid4().hex}"


def resolve_agent_arn() -> str:
    """Read the deployed runtime ARN from the starter toolkit's config file."""
    try:
        with open(".bedrock_agentcore.yaml") as f:
            ac_config = yaml.safe_load(f)
    except FileNotFoundError:
        raise SystemExit(".bedrock_agentcore.yaml not found — run `agentcore configure` and `agentcore deploy` first.")

    # The toolkit stores the ARN per-agent under
    # agents.<agent_name>.bedrock_agentcore.agent_arn
    agents = ac_config.get("agents", {})
    default_agent = ac_config.get("default_agent")
    agent_spec = agents.get(default_agent) or next(iter(agents.values()), {})
    agent_arn = (agent_spec.get("bedrock_agentcore") or {}).get("agent_arn", "")

    if not agent_arn:
        raise SystemExit("Agent ARN not found — run `agentcore deploy` first.")
    # main() reads the region out of field 3 of this ARN, so a truncated or hand-edited
    # value has to fail here with something actionable rather than as an IndexError.
    if not agent_arn.startswith("arn:") or len(agent_arn.split(":")) < 6:
        raise SystemExit(f"agent_arn in .bedrock_agentcore.yaml is not a full ARN: {agent_arn!r}")
    return agent_arn


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT)
    args = parser.parse_args()

    agent_arn = resolve_agent_arn()
    print(f"Invoking {agent_arn}")

    # Take the region from the ARN we just resolved rather than from AWS_REGION.
    # The README warns that `agentcore configure` may deploy to a different region
    # than the gateway; the authoritative value is already in hand.
    runtime = boto3.client("bedrock-agentcore", region_name=agent_arn.split(":")[3])
    response = runtime.invoke_agent_runtime(
        agentRuntimeArn=agent_arn,
        runtimeSessionId=new_session_id(),
        payload=json.dumps({"prompt": args.prompt}).encode(),
        qualifier="DEFAULT",
    )
    print("Agent response:")
    print(json.dumps(json.loads(response["response"].read()), indent=2))


if __name__ == "__main__":
    main()
