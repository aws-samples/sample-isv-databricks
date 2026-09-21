"""Unit tests for the config and gateway-wiring guarantees this sample relies on.

Companion to test_cleanup_contract.py. These cover the small, high-leverage pieces
whose failure is silent or only surfaces mid-deployment against a live account:

    - require_databricks_config()   fail-fast on missing env, naming every gap, and the
                                    either/or between the plaintext secret and a SM ARN
    - genie_mcp_url()               the exact Databricks-managed MCP endpoint shape
    - client_secret_config()        the MANAGED (inline) vs EXTERNAL (Secrets Manager
                                    reference) provider-config fragment
    - create_credential_provider()  the fail-fast guard on a missing secret ARN, the two
                                    response shapes it must accept, and that EXTERNAL mode
                                    scopes the grant to the ARN we provisioned
    - grant_oauth_permissions()     the IAM policy shape -- notably that the secret
                                    read is scoped to one ARN and never falls back to "*"
    - secrets_setup.py              the secret JSON payload, create-vs-adopt bookkeeping,
                                    and the --delete guard that refuses a secret we did not create

No test framework and no new dependency beyond the sample's own requirements.txt (the
tests import the sample's modules, which import boto3/requests/yaml), no AWS account, no
network:

    pip install -r requirements.txt
    python -m unittest test_config_and_gateway -v
"""

import contextlib
import io
import json
import unittest
from unittest import mock

import config
import deploy
import secrets_setup
from gateway_setup import GatewaySetup


class RequireDatabricksConfigTest(unittest.TestCase):
    """require_databricks_config() must fail fast and name every missing variable.

    The OAuth secret may come from EITHER the plaintext DATABRICKS_CLIENT_SECRET or a
    Secrets Manager reference DATABRICKS_SECRET_ARN (the production path), so the guard
    requires exactly one of the two -- not the plaintext specifically.
    """

    # The three always-required values plus the plaintext secret; DATABRICKS_SECRET_ARN is
    # pinned to "" so the either/or is deterministic regardless of the caller's environment.
    _ALWAYS = {
        "DATABRICKS_HOST": "https://dbc-x.cloud.databricks.com",
        "DATABRICKS_CLIENT_ID": "client-id",
        "GENIE_SPACE_ID": "space-id",
    }
    _ALL_PRESENT = dict(_ALWAYS, DATABRICKS_CLIENT_SECRET="secret", DATABRICKS_SECRET_ARN="")

    def _patch(self, values):
        """Patch the config globals for the duration of one test."""
        for name, value in values.items():
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_all_present_does_not_raise(self):
        self._patch(self._ALL_PRESENT)
        config.require_databricks_config()  # no exception

    def test_each_always_required_value_is_named(self):
        for missing in self._ALWAYS:
            values = dict(self._ALL_PRESENT, **{missing: ""})
            # Patch per iteration (context manager) so each set of globals is
            # unwound before the next, rather than stacking to the end of the method.
            with self.subTest(missing=missing), mock.patch.multiple(config, **values):
                with self.assertRaises(SystemExit) as ctx:
                    config.require_databricks_config()
                self.assertIn(missing, str(ctx.exception))

    def test_secret_arn_satisfies_without_plaintext(self):
        # The whole point of the production path: no plaintext secret in the environment.
        values = dict(self._ALL_PRESENT, DATABRICKS_CLIENT_SECRET="", DATABRICKS_SECRET_ARN="arn:aws:secretsmanager:::secret:x")
        with mock.patch.multiple(config, **values):
            config.require_databricks_config()  # no exception

    def test_neither_secret_nor_arn_aborts_and_names_both(self):
        values = dict(self._ALL_PRESENT, DATABRICKS_CLIENT_SECRET="", DATABRICKS_SECRET_ARN="")
        with mock.patch.multiple(config, **values):
            with self.assertRaises(SystemExit) as ctx:
                config.require_databricks_config()
        message = str(ctx.exception)
        self.assertIn("DATABRICKS_CLIENT_SECRET", message)
        self.assertIn("DATABRICKS_SECRET_ARN", message)

    def test_all_missing_are_listed_together(self):
        self._patch({name: "" for name in self._ALL_PRESENT})
        with self.assertRaises(SystemExit) as ctx:
            config.require_databricks_config()
        message = str(ctx.exception)
        for name in self._ALWAYS:
            self.assertIn(name, message)


class GenieMcpUrlTest(unittest.TestCase):
    """genie_mcp_url() must produce the Databricks-managed Genie MCP endpoint."""

    def test_builds_managed_endpoint_for_space(self):
        with mock.patch.object(config, "DATABRICKS_HOST", "https://dbc-x.cloud.databricks.com"), \
             mock.patch.object(config, "GENIE_SPACE_ID", "01f000abc"):
            self.assertEqual(
                config.genie_mcp_url(),
                "https://dbc-x.cloud.databricks.com/api/2.0/mcp/genie/01f000abc",
            )

    def test_reflects_configured_space(self):
        with mock.patch.object(config, "DATABRICKS_HOST", "https://host"), \
             mock.patch.object(config, "GENIE_SPACE_ID", "space-42"):
            self.assertTrue(config.genie_mcp_url().endswith("/api/2.0/mcp/genie/space-42"))


class _FakeAgentCore:
    """Captures the kwargs passed to create_oauth2_credential_provider and returns a canned response."""

    def __init__(self, response):
        self._response = response
        self.create_kwargs = None

    def create_oauth2_credential_provider(self, **kwargs):
        self.create_kwargs = kwargs
        return self._response


class CreateCredentialProviderSecretArnTest(unittest.TestCase):
    """create_credential_provider() resolves the secret ARN across response shapes,
    and fails loudly rather than silently dropping the secret-read grant.

    These pin the default MANAGED path (no DATABRICKS_SECRET_ARN), where the ARN is only
    knowable from the response; the EXTERNAL path is covered separately below.
    """

    _FakeAgentCore = _FakeAgentCore

    def _create(self, fake):
        """Call create_credential_provider in MANAGED mode, swallowing its progress prints."""
        # Pin MANAGED regardless of the caller's environment: an exported DATABRICKS_SECRET_ARN
        # would otherwise switch these to the EXTERNAL path and change ARN resolution.
        with mock.patch.object(deploy, "DATABRICKS_SECRET_ARN", ""), \
             contextlib.redirect_stdout(io.StringIO()):
            return deploy.create_credential_provider(fake)

    def test_flat_secret_arn(self):
        fake = self._FakeAgentCore(
            {"credentialProviderArn": "arn:prov", "secretArn": "arn:aws:secretsmanager:...:secret:x"}
        )
        provider_arn, secret_arn = self._create(fake)
        self.assertEqual(provider_arn, "arn:prov")
        self.assertEqual(secret_arn, "arn:aws:secretsmanager:...:secret:x")

    def test_nested_client_secret_arn(self):
        fake = self._FakeAgentCore(
            {"credentialProviderArn": "arn:prov", "clientSecretArn": {"secretArn": "arn:nested"}}
        )
        _, secret_arn = self._create(fake)
        self.assertEqual(secret_arn, "arn:nested")

    def test_flat_arn_wins_over_nested(self):
        fake = self._FakeAgentCore(
            {
                "credentialProviderArn": "arn:prov",
                "secretArn": "arn:flat",
                "clientSecretArn": {"secretArn": "arn:nested"},
            }
        )
        _, secret_arn = self._create(fake)
        self.assertEqual(secret_arn, "arn:flat")

    def test_missing_secret_arn_aborts(self):
        fake = self._FakeAgentCore({"credentialProviderArn": "arn:prov"})
        with self.assertRaises(SystemExit) as ctx:
            self._create(fake)
        self.assertIn("secret ARN", str(ctx.exception))


class ClientSecretConfigTest(unittest.TestCase):
    """client_secret_config() must emit the inline (MANAGED) shape by default and the
    Secrets Manager reference (EXTERNAL) shape when DATABRICKS_SECRET_ARN is set."""

    def test_managed_inline_when_no_arn(self):
        with mock.patch.multiple(
            deploy, DATABRICKS_SECRET_ARN="", DATABRICKS_CLIENT_SECRET="the-secret"
        ):
            cfg = deploy.client_secret_config()
        self.assertEqual(cfg, {"clientSecret": "the-secret"})
        # Never leak the external-reference keys into the inline path.
        self.assertNotIn("clientSecretSource", cfg)
        self.assertNotIn("clientSecretConfig", cfg)

    def test_external_reference_when_arn_set(self):
        arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:db-abcde"
        with mock.patch.multiple(
            deploy,
            DATABRICKS_SECRET_ARN=arn,
            DATABRICKS_SECRET_JSON_KEY="client_secret",
            DATABRICKS_CLIENT_SECRET="ignored-in-external-mode",
        ):
            cfg = deploy.client_secret_config()
        self.assertEqual(
            cfg,
            {
                "clientSecretSource": "EXTERNAL",
                "clientSecretConfig": {"secretId": arn, "jsonKey": "client_secret"},
            },
        )
        # The plaintext must NOT be sent when referencing an external secret.
        self.assertNotIn("clientSecret", cfg)


class CreateCredentialProviderExternalTest(unittest.TestCase):
    """In EXTERNAL mode the provider call must carry the reference (not the plaintext),
    and the grant must be scoped to the ARN we provisioned regardless of the response."""

    _ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:db-abcde"

    def _create(self, response):
        fake = _FakeAgentCore(response)
        with mock.patch.multiple(
            deploy,
            DATABRICKS_SECRET_ARN=self._ARN,
            DATABRICKS_SECRET_JSON_KEY="client_secret",
            DATABRICKS_CLIENT_SECRET="should-not-be-sent",
        ), contextlib.redirect_stdout(io.StringIO()):
            provider_arn, secret_arn = deploy.create_credential_provider(fake)
        return fake, provider_arn, secret_arn

    def test_call_references_secret_and_omits_plaintext(self):
        fake, _, _ = self._create({"credentialProviderArn": "arn:prov"})
        custom = fake.create_kwargs["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]
        self.assertEqual(custom["clientSecretSource"], "EXTERNAL")
        self.assertEqual(custom["clientSecretConfig"], {"secretId": self._ARN, "jsonKey": "client_secret"})
        self.assertNotIn("clientSecret", custom)

    def test_grant_scoped_to_provisioned_arn_even_if_response_omits_it(self):
        # MANAGED mode aborts when the response carries no ARN; EXTERNAL must NOT -- we own
        # the ARN, so the grant is scoped to it directly rather than from the response.
        _, provider_arn, secret_arn = self._create({"credentialProviderArn": "arn:prov"})
        self.assertEqual(provider_arn, "arn:prov")
        self.assertEqual(secret_arn, self._ARN)


class GrantOauthPermissionsPolicyTest(unittest.TestCase):
    """grant_oauth_permissions() writes an IAM policy that (a) grants the workload-token
    and token-exchange actions and (b) scopes the secret read to one ARN, never '*'."""

    class _FakeIam:
        def __init__(self):
            self.put_role_policy_kwargs = None

        def put_role_policy(self, **kwargs):
            self.put_role_policy_kwargs = kwargs

    def _run(self, secret_arn):
        """Invoke the method on a GatewaySetup built without boto3, return the policy dict."""
        setup = GatewaySetup.__new__(GatewaySetup)  # skip __init__ (it calls boto3 + STS)
        setup.region = "us-west-2"
        setup.account_id = "123456789012"
        setup.iam = self._FakeIam()
        # grant_oauth_permissions sleeps 10s for IAM propagation and prints progress;
        # skip the sleep and swallow the print in a unit test.
        with mock.patch("gateway_setup.time.sleep"), contextlib.redirect_stdout(io.StringIO()):
            setup.grant_oauth_permissions(
                role_arn="arn:aws:iam::123456789012:role/DatabricksGenieGatewayRole",
                policy_name="DatabricksGenieOAuthAccess",
                provider_arn="arn:aws:bedrock-agentcore:us-west-2:123456789012:token-vault/default/oauth2credentialprovider/x",
                secret_arn=secret_arn,
            )
        kwargs = setup.iam.put_role_policy_kwargs
        self.assertIsNotNone(kwargs, "put_role_policy was never called")
        return kwargs, json.loads(kwargs["PolicyDocument"])

    def test_policy_is_well_formed(self):
        kwargs, doc = self._run(secret_arn="arn:aws:secretsmanager:us-west-2:123456789012:secret:db")
        self.assertEqual(kwargs["RoleName"], "DatabricksGenieGatewayRole")
        self.assertEqual(kwargs["PolicyName"], "DatabricksGenieOAuthAccess")
        self.assertEqual(doc["Version"], "2012-10-17")

    def test_grants_workload_and_token_exchange_actions(self):
        _, doc = self._run(secret_arn="arn:aws:secretsmanager:us-west-2:123456789012:secret:db")
        actions = set()
        for stmt in doc["Statement"]:
            action = stmt["Action"]
            actions.update(action if isinstance(action, list) else [action])
        self.assertIn("bedrock-agentcore:GetWorkloadAccessToken", actions)
        self.assertIn("bedrock-agentcore:GetWorkloadAccessTokenForJWT", actions)
        self.assertIn("bedrock-agentcore:GetResourceOauth2Token", actions)

    def test_secret_read_scoped_to_arn_not_wildcard(self):
        arn = "arn:aws:secretsmanager:us-west-2:123456789012:secret:db"
        _, doc = self._run(secret_arn=arn)
        secret_stmts = [
            s for s in doc["Statement"] if "secretsmanager:GetSecretValue" in _actions_of(s)
        ]
        self.assertEqual(len(secret_stmts), 1)
        self.assertEqual(secret_stmts[0]["Resource"], arn)
        # The whole reason the ARN is threaded through: never grant read on every secret.
        # Guard the two ways that regresses — a bare "*" anywhere in the policy, or a
        # wildcard-suffixed secret ARN — neither of which the exact assertEqual above rules
        # out on its own if the resource were built differently.
        for stmt in doc["Statement"]:
            self.assertNotIn("*", _resources_of(stmt))
        self.assertFalse(secret_stmts[0]["Resource"].endswith("*"))

    def test_no_secret_statement_when_arn_absent(self):
        # Defence in depth: create_credential_provider's guard makes an empty secret_arn
        # unreachable in the deploy flow, but grant_oauth_permissions must still degrade
        # safely (omit the statement, never widen to "*") if ever called with one.
        _, doc = self._run(secret_arn="")
        for stmt in doc["Statement"]:
            self.assertNotIn("secretsmanager:GetSecretValue", _actions_of(stmt))


class _FakeSecretsManager:
    """Minimal in-memory Secrets Manager stand-in for secrets_setup.py."""

    class ResourceNotFoundException(Exception):
        pass

    def __init__(self, existing=None):
        # existing: {name: arn} for secrets that already exist.
        self._store = dict(existing or {})
        self.exceptions = self  # so client.exceptions.ResourceNotFoundException resolves
        self.calls = []

    def describe_secret(self, SecretId):
        self.calls.append(("describe_secret", SecretId))
        if SecretId not in self._store:
            raise self.ResourceNotFoundException(SecretId)
        return {"ARN": self._store[SecretId]}

    def create_secret(self, Name, SecretString, Description=None):
        arn = f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{Name}-abcde"
        self._store[Name] = arn
        self.calls.append(("create_secret", Name, SecretString))
        return {"ARN": arn}

    def put_secret_value(self, SecretId, SecretString):
        self.calls.append(("put_secret_value", SecretId, SecretString))
        return {"ARN": self._store[SecretId]}

    def delete_secret(self, **kwargs):
        self.calls.append(("delete_secret", kwargs))
        self._store.pop(kwargs["SecretId"], None)
        return {}

    def _kinds(self):
        return [c[0] for c in self.calls]


class SecretsSetupTest(unittest.TestCase):
    """secrets_setup.py: the stored payload, create-vs-adopt bookkeeping, and the
    --delete guard that refuses to remove a secret this script did not create."""

    _NAME = "databricks-genie-agentcore/oauth-client-secret"

    def setUp(self):
        # Keep the ownership state in memory; never touch secret_state.json on disk.
        self._state = {}
        self._patch(secrets_setup, "read_secret_state", lambda: dict(self._state))
        self._patch(secrets_setup, "write_secret_state", self._state.update)
        self._patch(secrets_setup, "clear_secret_state", self._state.clear)

    def _patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)

    def test_build_secret_string_wraps_value_under_json_key(self):
        self.assertEqual(
            json.loads(secrets_setup.build_secret_string("s3cr3t", "client_secret")),
            {"client_secret": "s3cr3t"},
        )

    def test_provision_creates_when_absent_and_records_ownership(self):
        client = _FakeSecretsManager()
        self._patch(secrets_setup, "DATABRICKS_CLIENT_SECRET", "s3cr3t")
        arn = self._run(secrets_setup.provision, client, self._NAME, "client_secret")
        self.assertIn("create_secret", client._kinds())
        self.assertTrue(self._state["created"])
        self.assertEqual(self._state["secret_arn"], arn)
        # Stored payload is JSON under the configured key.
        stored = next(c[2] for c in client.calls if c[0] == "create_secret")
        self.assertEqual(json.loads(stored), {"client_secret": "s3cr3t"})

    def test_provision_adopts_existing_and_does_not_claim_ownership(self):
        arn = f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{self._NAME}-abcde"
        client = _FakeSecretsManager(existing={self._NAME: arn})
        self._patch(secrets_setup, "DATABRICKS_CLIENT_SECRET", "s3cr3t")
        self._run(secrets_setup.provision, client, self._NAME, "client_secret")
        self.assertIn("put_secret_value", client._kinds())
        self.assertNotIn("create_secret", client._kinds())
        self.assertFalse(self._state["created"])  # pre-existing: we won't --delete it

    def test_provision_aborts_without_plaintext(self):
        self._patch(secrets_setup, "DATABRICKS_CLIENT_SECRET", "")
        with self.assertRaises(SystemExit) as ctx:
            self._run(secrets_setup.provision, _FakeSecretsManager(), self._NAME, "client_secret")
        self.assertIn("DATABRICKS_CLIENT_SECRET", str(ctx.exception))

    def test_delete_refuses_secret_we_did_not_create(self):
        arn = f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{self._NAME}-abcde"
        client = _FakeSecretsManager(existing={self._NAME: arn})
        self._state.update({"secret_arn": arn, "created": False})  # adopted, not ours
        with self.assertRaises(SystemExit) as ctx:
            self._run(secrets_setup.delete, client, self._NAME, force=False, assume_yes=True)
        self.assertIn("no record that this script created it", str(ctx.exception))
        self.assertNotIn("delete_secret", client._kinds())

    def test_delete_uses_recovery_window_by_default(self):
        arn = f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{self._NAME}-abcde"
        client = _FakeSecretsManager(existing={self._NAME: arn})
        self._state.update({"secret_arn": arn, "created": True})
        self._run(secrets_setup.delete, client, self._NAME, force=False, assume_yes=True)
        kwargs = next(c[1] for c in client.calls if c[0] == "delete_secret")
        self.assertEqual(kwargs.get("RecoveryWindowInDays"), 30)
        self.assertNotIn("ForceDeleteWithoutRecovery", kwargs)

    def test_delete_force_skips_recovery_window(self):
        arn = f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{self._NAME}-abcde"
        client = _FakeSecretsManager(existing={self._NAME: arn})
        self._state.update({"secret_arn": arn, "created": True})
        self._run(secrets_setup.delete, client, self._NAME, force=True, assume_yes=True)
        kwargs = next(c[1] for c in client.calls if c[0] == "delete_secret")
        self.assertTrue(kwargs.get("ForceDeleteWithoutRecovery"))
        self.assertNotIn("RecoveryWindowInDays", kwargs)


def _actions_of(statement):
    action = statement.get("Action", [])
    return action if isinstance(action, list) else [action]


def _resources_of(statement):
    resource = statement.get("Resource", [])
    return resource if isinstance(resource, list) else [resource]


if __name__ == "__main__":
    unittest.main()
