"""Gateway, IAM and Cognito helpers built directly on the bedrock-agentcore SDKs.

Uses `boto3` clients for `bedrock-agentcore-control`, `iam` and `cognito-idp`
rather than the deprecated starter toolkit. Follows the same approach as
01-features/07-centralize-and-govern-your-ai-infrastructure/01-gateway.
"""

import json
import secrets
import time

import boto3
import requests

MCP_PROTOCOL_VERSION = "2025-11-25"


class GatewaySetup:
    """Thin wrapper around the bedrock-agentcore-control boto3 client."""

    def __init__(self, region: str):
        self.region = region
        self.client = boto3.client("bedrock-agentcore-control", region_name=region)
        self.iam = boto3.client("iam")
        self.sts = boto3.client("sts")
        self.cognito = boto3.client("cognito-idp", region_name=region)
        self.account_id = self.sts.get_caller_identity()["Account"]

    # --- inbound auth (Cognito) --------------------------------------------

    def _find_existing_pool(self, pool_name: str):
        """Return the id of an existing user pool with this name, or None."""
        paginator = self.cognito.get_paginator("list_user_pools")
        for page in paginator.paginate(MaxResults=60):
            for pool in page.get("UserPools", []):
                if pool.get("Name") == pool_name:
                    return pool["Id"]
        return None

    def _wait_for_domain(self, domain: str, timeout: int = 900) -> bool:
        """Block until the hosted-UI domain is ACTIVE.

        CreateUserPoolDomain returns as soon as the request is accepted, but the
        backing CloudFront distribution takes minutes. The token endpoint lives on
        that domain, so returning early meant the very next step (minting a token)
        failed with a bare DNS error that pointed nowhere near Cognito.
        """
        waited = 0
        while waited < timeout:
            try:
                status = (
                    self.cognito.describe_user_pool_domain(Domain=domain)
                    .get("DomainDescription", {})
                    .get("Status")
                )
            except Exception:  # noqa: BLE001
                status = None
            if status == "ACTIVE":
                return True
            if status == "FAILED":
                return False
            if waited % 60 == 0:
                print(f"  waiting for Cognito domain to become ACTIVE ({status or 'pending'})...")
            time.sleep(15)
            waited += 15
        return False

    def create_cognito_authorizer(self, name: str) -> dict:
        """Create (or reuse) a Cognito user pool + M2M client for inbound auth.

        Reuses an existing pool of the same name so that re-running deploy.py does
        not leak a fresh pool and hosted-UI domain on every retry -- deploy.py's own
        failure message tells the user to re-run, and each retry previously stranded
        resources that appeared in no state file.

        Returns the values needed to configure a CUSTOM_JWT authorizer and to
        mint access tokens for calling the gateway.
        """
        pool_name = f"{name}-pool"
        pool_id = self._find_existing_pool(pool_name)
        reused_pool = pool_id is not None
        if reused_pool:
            print(f"  Reusing existing Cognito user pool: {pool_id}")
        else:
            pool_id = self.cognito.create_user_pool(PoolName=pool_name)["UserPool"]["Id"]

        domain = None
        if reused_pool:
            existing = self.cognito.describe_user_pool(UserPoolId=pool_id)["UserPool"]
            domain = existing.get("Domain") or None
        if not domain:
            domain = f"{name.lower()}-{secrets.token_hex(4)}"
            self.cognito.create_user_pool_domain(Domain=domain, UserPoolId=pool_id)
        if not self._wait_for_domain(domain):
            raise SystemExit(
                f"Cognito domain {domain} did not become ACTIVE. The gateway token endpoint "
                "lives on this domain, so deployment cannot continue. Check the domain in the "
                "Cognito console and re-run."
            )

        # A resource server defines the scope the M2M client requests.
        scope_name = "invoke"
        resource_server_id = f"{name.lower()}-api"
        try:
            self.cognito.create_resource_server(
                UserPoolId=pool_id,
                Identifier=resource_server_id,
                Name=f"{name} API",
                Scopes=[{"ScopeName": scope_name, "ScopeDescription": "Invoke gateway"}],
            )
        except self.cognito.exceptions.InvalidParameterException:
            pass  # already exists on a reused pool
        scope = f"{resource_server_id}/{scope_name}"

        client = self.cognito.create_user_pool_client(
            UserPoolId=pool_id,
            ClientName=f"{name}-client",
            GenerateSecret=True,
            AllowedOAuthFlows=["client_credentials"],
            AllowedOAuthScopes=[scope],
            AllowedOAuthFlowsUserPoolClient=True,
            SupportedIdentityProviders=["COGNITO"],
            ExplicitAuthFlows=["ALLOW_REFRESH_TOKEN_AUTH"],
        )["UserPoolClient"]

        discovery_url = f"https://cognito-idp.{self.region}.amazonaws.com/{pool_id}/.well-known/openid-configuration"
        client_info = {
            "user_pool_id": pool_id,
            "domain": domain,
            "client_id": client["ClientId"],
            "client_secret": client["ClientSecret"],
            "token_endpoint": f"https://{domain}.auth.{self.region}.amazoncognito.com/oauth2/token",
            "scope": scope,
            "discovery_url": discovery_url,
        }
        print(f"  Cognito user pool: {pool_id}")
        return client_info

    @staticmethod
    def get_access_token(client_info: dict) -> str:
        """Mint a client-credentials access token for calling the gateway."""
        response = requests.post(
            client_info["token_endpoint"],
            data={
                "grant_type": "client_credentials",
                "client_id": client_info["client_id"],
                "client_secret": client_info["client_secret"],
                "scope": client_info["scope"],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()["access_token"]

    # --- IAM ---------------------------------------------------------------

    def create_gateway_role(self, gateway_name: str) -> str:
        """Create the gateway execution role, scoped to OAuth outbound targets."""
        role_name = f"agentcore-{gateway_name}-role"
        assume_role_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                    "Condition": {
                        "StringEquals": {"aws:SourceAccount": self.account_id},
                        "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{self.region}:{self.account_id}:*"},
                    },
                }
            ],
        }

        try:
            role = self.iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(assume_role_policy),
            )
            print(f"  Created IAM role: {role_name}")
            time.sleep(10)  # let the role propagate
            return role["Role"]["Arn"]
        except self.iam.exceptions.EntityAlreadyExistsException:
            self.iam.update_assume_role_policy(RoleName=role_name, PolicyDocument=json.dumps(assume_role_policy))
            print(f"  IAM role already exists: {role_name} (trust policy refreshed)")
            return self.iam.get_role(RoleName=role_name)["Role"]["Arn"]

    def grant_oauth_permissions(self, role_arn: str, policy_name: str, provider_arn: str, secret_arn: str) -> None:
        """Allow the gateway role to fetch the Databricks token and its secret.

        Without this the target still reaches READY, but every tool call fails.
        """
        role_name = role_arn.split("/")[-1]
        arn_prefix = f"arn:aws:bedrock-agentcore:{self.region}:{self.account_id}"
        statements = [
            {
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                ],
                "Resource": [
                    f"{arn_prefix}:workload-identity-directory/default",
                    f"{arn_prefix}:workload-identity-directory/default/workload-identity/*",
                ],
            },
            {
                # GetResourceOauth2Token is authorized against several
                # resources in turn — the credential provider, the token
                # vault that holds it, AND the gateway's workload identity.
                # Scoping it to the provider alone fails at tool-invocation
                # time with a 403 from AgentCredentialProvider.
                "Effect": "Allow",
                "Action": "bedrock-agentcore:GetResourceOauth2Token",
                "Resource": [
                    provider_arn,
                    f"{arn_prefix}:token-vault/default",
                    f"{arn_prefix}:workload-identity-directory/default",
                    f"{arn_prefix}:workload-identity-directory/default/workload-identity/*",
                ],
            },
        ]
        # Only grant secret read when we have the specific ARN — never fall
        # back to "*", which would let the role read every secret in the account.
        if secret_arn:
            statements.append(
                {
                    "Effect": "Allow",
                    "Action": "secretsmanager:GetSecretValue",
                    "Resource": secret_arn,
                }
            )
        policy_doc = json.dumps({"Version": "2012-10-17", "Statement": statements})
        self.iam.put_role_policy(RoleName=role_name, PolicyName=policy_name, PolicyDocument=policy_doc)
        print(f"  Updated role: {role_name}")
        time.sleep(10)

    # --- gateway -----------------------------------------------------------

    def create_mcp_gateway(self, name: str, role_arn: str, client_info: dict) -> dict:
        """Create an MCP gateway with a Cognito CUSTOM_JWT inbound authorizer."""
        gateway = self.client.create_gateway(
            name=name,
            roleArn=role_arn,
            protocolType="MCP",
            description="Databricks Genie exposed as a governed MCP tool",
            protocolConfiguration={"mcp": {"supportedVersions": [MCP_PROTOCOL_VERSION]}},
            authorizerType="CUSTOM_JWT",
            authorizerConfiguration={
                "customJWTAuthorizer": {
                    "allowedClients": [client_info["client_id"]],
                    "discoveryUrl": client_info["discovery_url"],
                }
            },
            # DEBUG returns verbose gateway errors, which is what makes the grant
            # mistakes in the README diagnosable. Lower it before production use --
            # it surfaces internal error detail to callers.
            exceptionLevel="DEBUG",
        )
        print(f"  Gateway URL: {gateway['gatewayUrl']}")
        print(f"  Gateway ID:  {gateway['gatewayId']}")
        return gateway
