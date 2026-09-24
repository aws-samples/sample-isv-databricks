"""Create the AgentCore Gateway and register Databricks Genie as an MCP target.

Steps performed:
    1. Verify AWS credentials
    2. Create the gateway with a Cognito authorizer (inbound auth)
    3. Create the Databricks OAuth2 M2M credential provider (outbound auth)
    4. Grant the gateway role permission to use that provider
    5. Register the Genie MCP endpoint as a target, wait for it, sync the tool surface
    6. Write gateway_config.json for invoke.py / genie_agent.py / cleanup.py

Usage:
    python deploy.py
"""

import json
import os
import sys
import time

import boto3
from config import (
    AWS_REGION,
    CREDENTIAL_PROVIDER_NAME,
    DATABRICKS_CLIENT_ID,
    DATABRICKS_CLIENT_SECRET,
    DATABRICKS_HOST,
    DATABRICKS_SECRET_ARN,
    DATABRICKS_SECRET_JSON_KEY,
    DATABRICKS_SECRET_JSON_KEY_SET,
    GATEWAY_NAME,
    GENIE_SPACE_ID,
    IAM_POLICY_NAME,
    STATE_FILE,
    TARGET_NAME,
    genie_mcp_url,
    require_databricks_config,
)
from gateway_setup import GatewaySetup


def banner(step: str) -> None:
    print("=" * 60)
    print(step)
    print("=" * 60)


def _initial_state(prior_state: dict | None) -> dict:
    """Build the state dict for this run, carrying forward what already exists.

    Starting every field at None meant the first persist of a re-run erased the resources
    the previous attempt had recorded. A deploy that created the role and then died before
    the gateway left role_arn=null on disk, so cleanup skipped IAM entirely and orphaned
    the role -- and the next run read no owned_role and reported it adopted, defeating the
    ownership guard by a different route.
    """
    prior_state = prior_state or {}
    state = {
        "gateway_id": None,
        "gateway_url": None,
        "target_id": None,
        "provider_arn": None,
        "genie_space_id": GENIE_SPACE_ID,
        "region": AWS_REGION,
        "client_info": None,
        "role_arn": None,
        "databricks_host": DATABRICKS_HOST,
    }
    for key in ("client_info", "role_arn", "owned_role", "provider_arn", "target_id"):
        if prior_state.get(key) is not None:
            state[key] = prior_state[key]
    return state


def write_state(config: dict) -> None:
    """Persist gateway_config.json for invoke.py / genie_agent.py / cleanup.py."""
    # Write-then-rename: this now runs several times per deploy, and a Ctrl-C partway
    # through a plain write leaves a truncated file that hides a live deployment.
    tmp = f"{STATE_FILE}.tmp"
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, STATE_FILE)
    print(f"  Wrote {STATE_FILE}")


def create_gateway(setup: GatewaySetup, persist, prior_state=None) -> dict:
    """Create the Cognito authorizer, the IAM role and the MCP gateway.

    Persists the state file after EACH resource is created. Previously all five
    resources (pool, domain, resource server, app client, role) were built before
    the first write, so a CreateGateway failure -- a throttle, a quota, or a
    protocolConfiguration validation error -- exited with real resources and no
    state, and cleanup.py then refused to run at all.
    """
    state = _initial_state(prior_state)

    print("Creating Cognito authorizer (inbound auth)...")
    def _record_pool(partial: dict) -> None:
        # Contract rule 1: the pool and its domain are recorded the moment they exist,
        # before the ACTIVE wait -- which can fail and previously stranded both.
        state["client_info"] = dict(partial)
        persist(state)

    prior_owned = bool((prior_state.get("client_info") or {}).get("owned_pool"))
    state["client_info"] = setup.create_cognito_authorizer(
        GATEWAY_NAME, on_created=_record_pool, previously_owned=prior_owned
    )
    persist(state)

    print("Creating gateway execution role...")

    def _record_role(role_arn: str, owned: bool) -> None:
        # Contract rule 1, same reason as _record_pool: the role is recorded the moment
        # CreateRole returns, before the 10s propagation sleep inside create_gateway_role.
        state["role_arn"], state["owned_role"] = role_arn, owned
        persist(state)

    prior_owned_role = bool(prior_state.get("owned_role"))
    state["role_arn"], state["owned_role"] = setup.create_gateway_role(
        GATEWAY_NAME, on_created=_record_role, previously_owned=prior_owned_role
    )
    persist(state)

    print("Creating gateway...")
    gateway = setup.create_mcp_gateway(GATEWAY_NAME, state["role_arn"], state["client_info"])
    state["gateway_id"] = gateway["gatewayId"]
    state["gateway_url"] = gateway["gatewayUrl"]
    persist(state)

    print("  Waiting 30s for IAM propagation...")
    time.sleep(30)
    return state


# secrets_setup.py records the key it wrote the client secret under, next to gateway_config.json.
SECRET_STATE_FILE = os.path.join(os.path.dirname(STATE_FILE), "secret_state.json")


def recorded_secret_state() -> dict:
    """secrets_setup.py's record for the provisioned secret, or {} if absent/unreadable.

    Catches OSError (not just FileNotFoundError) and a non-object JSON body, so a permission
    error, a directory in the path, or a state file holding `[1,2]`/`"a"`/`null` degrades to
    {} rather than crashing the caller on `.get`.
    """
    try:
        with open(SECRET_STATE_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_secret_json_key() -> str:
    """Return the jsonKey deploy should register, defaulting it from secret_state.json.

    The ARN and the key that indexes it must travel together across two processes, but the
    README exports only DATABRICKS_SECRET_ARN into the deploy shell -- so a custom key used at
    provision time silently reverts to the "client_secret" default here, the provider reads a
    key that isn't in the secret, and every tool call 403s from the first one after READY.
    secret_state.json records the key secrets_setup.py actually wrote. When it describes THIS
    ARN, adopt that key unless the operator set DATABRICKS_SECRET_JSON_KEY explicitly; defaulting
    from the record removes the drift class rather than guarding it. An explicit value that
    disagrees is honored -- provision() merges, so an older key is usually still present -- with
    a warning rather than an abort, since the previous guard rejected deploys that would work.
    """
    if not DATABRICKS_SECRET_ARN:
        return DATABRICKS_SECRET_JSON_KEY
    state = recorded_secret_state()
    if state.get("secret_arn") != DATABRICKS_SECRET_ARN:
        return DATABRICKS_SECRET_JSON_KEY  # record is for a different/unknown secret
    recorded = state.get("json_key")
    if not recorded:
        return DATABRICKS_SECRET_JSON_KEY
    if not DATABRICKS_SECRET_JSON_KEY_SET:
        if recorded != DATABRICKS_SECRET_JSON_KEY:
            print(
                f"  Using jsonKey {recorded!r} recorded by secrets_setup.py for this secret "
                "(export DATABRICKS_SECRET_JSON_KEY to override)."
            )
        return recorded
    if recorded != DATABRICKS_SECRET_JSON_KEY:
        print(
            f"Note: DATABRICKS_SECRET_JSON_KEY={DATABRICKS_SECRET_JSON_KEY!r} differs from the key "
            f"{recorded!r} secrets_setup.py recorded for this secret. Proceeding with your explicit "
            "value; if tool calls 403 after READY, the secret has no such key.",
            file=sys.stderr,
        )
    return DATABRICKS_SECRET_JSON_KEY


def client_secret_config() -> dict:
    """Return the clientSecret* fragment of the provider config for the active secret source.

    Two mutually exclusive shapes the API accepts under customOauth2ProviderConfig:

    - MANAGED (default): pass the plaintext `clientSecret`. AgentCore stores it in a
      Secrets Manager secret it creates and owns, and returns that ARN.
    - EXTERNAL: pass `clientSecretSource="EXTERNAL"` + `clientSecretConfig` referencing a
      Secrets Manager secret you already provisioned (see secrets_setup.py). The plaintext
      never passes through deploy.py or lives in .env. AgentCore references the secret;
      it does not own or delete it.

    Selected by DATABRICKS_SECRET_ARN: set -> EXTERNAL, unset -> MANAGED.
    """
    if DATABRICKS_SECRET_ARN:
        return {
            "clientSecretSource": "EXTERNAL",
            "clientSecretConfig": {
                "secretId": DATABRICKS_SECRET_ARN,
                "jsonKey": DATABRICKS_SECRET_JSON_KEY,
            },
        }
    return {"clientSecret": DATABRICKS_CLIENT_SECRET}


def create_credential_provider(agentcore) -> tuple:
    """Register Databricks OAuth2 client-credentials as an outbound provider."""
    token_endpoint = f"{DATABRICKS_HOST}/oidc/v1/token"
    # Databricks publishes these separately: /oidc/v1/token and /oidc/v1/authorize
    # (confirmed via the workspace's /oidc/.well-known/oauth-authorization-server).
    # Unused under CLIENT_CREDENTIALS, but pointing it at the token endpoint broke
    # the authorization-code path the README points readers toward.
    authorization_endpoint = f"{DATABRICKS_HOST}/oidc/v1/authorize"

    if DATABRICKS_SECRET_ARN:
        print(f"Creating Databricks OAuth2 credential provider (secret from {DATABRICKS_SECRET_ARN})...")
    else:
        print("Creating Databricks OAuth2 credential provider (secret managed by AgentCore)...")
    # Deliberately no pre-emptive delete here. A live target holds a
    # credentialProviderConfigurations reference to this provider, so removing it would
    # break a working deployment before anything is recreated -- and the name is shared
    # across deployments in an account. Surface the conflict and let cleanup.py own
    # teardown (contract rule 6).
    provider = agentcore.create_oauth2_credential_provider(
        name=CREDENTIAL_PROVIDER_NAME,
        credentialProviderVendor="CustomOauth2",
        oauth2ProviderConfigInput={
            "customOauth2ProviderConfig": {
                "oauthDiscovery": {
                    "authorizationServerMetadata": {
                        "issuer": DATABRICKS_HOST,
                        "tokenEndpoint": token_endpoint,
                        "authorizationEndpoint": authorization_endpoint,
                    }
                },
                "clientId": DATABRICKS_CLIENT_ID,
                **client_secret_config(),
            }
        },
    )
    provider_arn = provider["credentialProviderArn"]
    # In EXTERNAL mode we provisioned the secret ourselves, so its ARN is authoritative --
    # scope the step-4 grant to it directly rather than trusting the response shape. In
    # MANAGED mode AgentCore owns the secret and only the response reveals its ARN:
    # CreateOauth2CredentialProviderResponse.clientSecretArn is a REQUIRED member of shape
    # Secret={secretArn}, so read it directly. (There is no flat 'secretArn' member.)
    if DATABRICKS_SECRET_ARN:
        secret_arn = DATABRICKS_SECRET_ARN
    else:
        client_secret_arn = provider.get("clientSecretArn")
        secret_arn = client_secret_arn.get("secretArn", "") if isinstance(client_secret_arn, dict) else ""
    if not secret_arn:
        # An empty ARN would drop the secretsmanager:GetSecretValue grant in step 4, the
        # target would still reach READY, and every tool call would then 403 at invocation
        # with nothing pointing at the cause. Fail loudly here instead.
        raise SystemExit(
            "Credential provider returned no secret ARN under 'clientSecretArn.secretArn'. "
            "The AgentCore response shape may have changed. Cannot scope the gateway role's "
            "secret read — aborting before the target is built."
        )
    print(f"  Credential provider ARN: {provider_arn}")
    return provider_arn, secret_arn


def grant_gateway_permissions(setup: GatewaySetup, role_arn: str, provider_arn: str, secret_arn: str) -> None:
    """Allow the gateway role to mint workload tokens and read the DB secret."""
    print("Updating gateway role permissions...")
    setup.grant_oauth_permissions(role_arn, IAM_POLICY_NAME, provider_arn, secret_arn)


def register_genie_target(agentcore, gateway_id: str, provider_arn: str, on_created=None) -> str:
    """Register the Databricks-managed Genie MCP server as a gateway target."""
    mcp_url = genie_mcp_url()
    print(f"Registering Genie MCP target: {mcp_url}")

    target = agentcore.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=TARGET_NAME,
        description=f"Databricks Genie space {GENIE_SPACE_ID} as MCP tool",
        targetConfiguration={"mcp": {"mcpServer": {"endpoint": mcp_url}}},
        credentialProviderConfigurations=[
            {
                "credentialProviderType": "OAUTH",
                "credentialProvider": {
                    "oauthCredentialProvider": {
                        "providerArn": provider_arn,
                        "grantType": "CLIENT_CREDENTIALS",
                        # Scope the token to Genie only, not all-apis.
                        "scopes": ["genie"],
                    }
                },
            }
        ],
    )
    target_id = target["targetId"]
    print(f"  Target ID: {target_id}")
    if on_created is not None:
        on_created(target_id)  # contract rule 1: recorded before the READY wait can fail

    # The API reports status in upper case (CREATING / READY / FAILED), so
    # compare case-insensitively — SynchronizeGatewayTargets rejects a target
    # that is still CREATING.
    print("Waiting for target to be ready...")
    status = ""
    for _ in range(60):
        status = agentcore.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id).get("status") or ""
        if status.upper() not in ("CREATING", "UPDATING", "SYNCHRONIZING"):
            break
        time.sleep(5)
    print(f"  Target status: {status}")

    if status.upper() != "READY":
        raise SystemExit(
            f"Target did not reach READY (status: {status}). Check the gateway "
            "role permissions from step 4 and the Databricks service principal "
            "credentials, then re-run."
        )

    print("Synchronizing tools from Databricks...")
    agentcore.synchronize_gateway_targets(gatewayIdentifier=gateway_id, targetIdList=[target_id])
    print("  Tools synchronized.")
    return target_id


def deploy() -> None:
    require_databricks_config()
    # EXTERNAL path: resolve the jsonKey before building anything -- adopt the key
    # secrets_setup.py recorded for this ARN unless the operator set one explicitly, so a
    # drift between provisioning and deploy can't surface as a 403 on the first tool call.
    global DATABRICKS_SECRET_JSON_KEY
    DATABRICKS_SECRET_JSON_KEY = resolve_secret_json_key()

    # Contract rule 3. Checked before anything is created: there is no reuse path for a
    # gateway or a target, so a re-run cannot succeed -- and its first state write would
    # record gateway_id=None, hiding a live gateway and target from cleanup.py.
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                existing = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            # Do NOT treat an unreadable file as "no deployment" -- a truncated write is
            # exactly the case most likely to be hiding a live gateway.
            raise SystemExit(
                f"{STATE_FILE} exists but could not be read ({exc}). It may be a truncated "
                "write hiding a live deployment. Inspect it, or move it aside if you are "
                "certain it is stale."
            ) from None
        if existing.get("gateway_id"):
            raise SystemExit(
                f"{STATE_FILE} already records gateway {existing['gateway_id']}.\n"
                "Run `python cleanup.py` first, or move that file aside if you know it is stale."
            )

    banner("STEP 1: Verify AWS Credentials")
    identity = boto3.client("sts").get_caller_identity()
    print(f"  Account: {identity['Account']}")
    print(f"  ARN:     {identity['Arn']}")
    print(f"  Region:  {AWS_REGION}")

    setup = GatewaySetup(AWS_REGION)
    agentcore = setup.client

    banner("STEP 2: Create AgentCore Gateway")
    # create_gateway persists after every resource, so any failure inside step 2
    # still leaves cleanup.py enough state to tear down what already exists.
    config = create_gateway(setup, write_state, prior_state=existing if os.path.exists(STATE_FILE) else {})
    gateway_id = config["gateway_id"]

    banner("STEP 3: Create Databricks OAuth2 Credential Provider")
    provider_arn, secret_arn = create_credential_provider(agentcore)

    config["provider_arn"] = provider_arn
    write_state(config)

    banner("STEP 4: Grant Gateway Role Permissions")
    grant_gateway_permissions(setup, config["role_arn"], provider_arn, secret_arn)

    banner("STEP 5: Register Databricks Genie MCP Target")
    def _record_target(tid: str) -> None:
        config["target_id"] = tid
        write_state(config)

    target_id = register_genie_target(agentcore, gateway_id, provider_arn, _record_target)

    banner("STEP 6: Save Configuration")
    config["target_id"] = target_id
    config["provider_arn"] = provider_arn
    write_state(config)

    print()
    print("Deployment complete. Next: python invoke.py")


if __name__ == "__main__":
    deploy()
