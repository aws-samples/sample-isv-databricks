"""Unit tests for the config and gateway-wiring guarantees this sample relies on.

Companion to test_cleanup_contract.py. These cover the small, high-leverage pieces
whose failure is silent or only surfaces mid-deployment against a live account:

    - require_databricks_config()   fail-fast on missing env, naming every gap, and the
                                    either/or between the plaintext secret and a SM ARN
    - genie_mcp_url()               the exact Databricks-managed MCP endpoint shape
    - client_secret_config()        the MANAGED (inline) vs EXTERNAL (Secrets Manager
                                    reference) provider-config fragment
    - create_credential_provider()  the fail-fast guard on a missing secret ARN, reading it off
                                    the real response shape (clientSecretArn.secretArn), and that
                                    EXTERNAL mode scopes the grant to the ARN we provisioned
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

    # A well-formed Secrets Manager ARN (region + 12-digit account + name + 6-char suffix).
    _VALID_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:db-AbCdEf"

    def test_secret_arn_satisfies_without_plaintext(self):
        # The whole point of the production path: no plaintext secret in the environment.
        values = dict(self._ALL_PRESENT, DATABRICKS_CLIENT_SECRET="", DATABRICKS_SECRET_ARN=self._VALID_ARN)
        with mock.patch.multiple(config, **values):
            config.require_databricks_config()  # no exception

    def test_valid_arn_accepts_other_partitions(self):
        # The validator must not over-reject gov/cn/iso partitions.
        for arn in (
            "arn:aws-us-gov:secretsmanager:us-gov-west-1:123456789012:secret:db-AbCdEf",
            "arn:aws-cn:secretsmanager:cn-north-1:123456789012:secret:db-AbCdEf",
        ):
            values = dict(self._ALL_PRESENT, DATABRICKS_CLIENT_SECRET="", DATABRICKS_SECRET_ARN=arn)
            with self.subTest(arn=arn), mock.patch.multiple(config, **values):
                config.require_databricks_config()  # no exception

    def test_malformed_secret_arn_aborts_before_deploy(self):
        # A bare name (or any non-ARN) is accepted by the credential-provider API but fails at
        # deploy step 4, after four resources exist. It must be rejected up front instead.
        for bad in ("databricks-genie-agentcore/oauth-client-secret",  # bare name
                    "arn:aws:secretsmanager:::secret:x",               # no region/account
                    "arn:aws:s3:::bucket/key"):                         # wrong service
            values = dict(self._ALL_PRESENT, DATABRICKS_CLIENT_SECRET="", DATABRICKS_SECRET_ARN=bad)
            with self.subTest(bad=bad), mock.patch.multiple(config, **values):
                with self.assertRaises(SystemExit) as ctx:
                    config.require_databricks_config()
                self.assertIn("DATABRICKS_SECRET_ARN", str(ctx.exception))

    def test_wildcard_arn_rejected_so_it_never_reaches_the_iam_policy(self):
        # A wildcard ARN is well-formed but would become an account-wide GetSecretValue grant
        # in deploy step 4 -- the '*' passes an unanchored/.+ pattern but must be rejected here.
        for wild in (
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:*",
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:db-*",
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:db AbCdEf",  # whitespace
        ):
            values = dict(self._ALL_PRESENT, DATABRICKS_CLIENT_SECRET="", DATABRICKS_SECRET_ARN=wild)
            with self.subTest(wild=wild), mock.patch.multiple(config, **values):
                with self.assertRaises(SystemExit):
                    config.require_databricks_config()

    def test_suffixless_arn_rejected(self):
        # A suffix-less ARN is accepted as a SecretId but the IAM Resource then matches no secret,
        # so the read is denied and tool calls 403 from the first one. Reject it up front.
        bad = "arn:aws:secretsmanager:us-east-1:123456789012:secret:db-oauth"
        values = dict(self._ALL_PRESENT, DATABRICKS_CLIENT_SECRET="", DATABRICKS_SECRET_ARN=bad)
        with mock.patch.multiple(config, **values):
            with self.assertRaises(SystemExit):
                config.require_databricks_config()

    def test_trailing_newline_arn_rejected(self):
        # The pattern must end in \Z, not $: $ also matches just before a final newline, so a
        # copy-pasted "arn:...-AbCdEf\n" would validate and the newline would ride into the IAM
        # Resource in deploy step 4, where it matches no secret. A \n anywhere else is covered
        # by the charset; this pins the one position $ would have let through.
        for bad in (self._VALID_ARN + "\n", self._VALID_ARN + "\n\n", self._VALID_ARN + "\r\n"):
            values = dict(self._ALL_PRESENT, DATABRICKS_CLIENT_SECRET="", DATABRICKS_SECRET_ARN=bad)
            with self.subTest(bad=repr(bad)), mock.patch.multiple(config, **values):
                with self.assertRaises(SystemExit) as ctx:
                    config.require_databricks_config()
                self.assertIn("DATABRICKS_SECRET_ARN", str(ctx.exception))

    def test_empty_json_key_rejected_when_arn_set(self):
        # An empty jsonKey defeats the client_secret default and is rejected by botocore at deploy
        # step 3, after the pool/role/gateway exist. Reject it up front alongside the ARN.
        values = dict(
            self._ALL_PRESENT,
            DATABRICKS_CLIENT_SECRET="",
            DATABRICKS_SECRET_ARN=self._VALID_ARN,
            DATABRICKS_SECRET_JSON_KEY="",
        )
        with mock.patch.multiple(config, **values):
            with self.assertRaises(SystemExit) as ctx:
                config.require_databricks_config()
        self.assertIn("DATABRICKS_SECRET_JSON_KEY", str(ctx.exception))

    def test_warns_when_both_secret_and_arn_set(self):
        values = dict(self._ALL_PRESENT, DATABRICKS_CLIENT_SECRET="secret", DATABRICKS_SECRET_ARN=self._VALID_ARN)
        stderr = io.StringIO()
        with mock.patch.multiple(config, **values), contextlib.redirect_stderr(stderr):
            config.require_databricks_config()  # no exception -- both is allowed
        self.assertIn("both", stderr.getvalue().lower())

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
    """create_credential_provider() reads the secret ARN off the real response shape, and
    fails loudly rather than silently dropping the secret-read grant.

    These pin the default MANAGED path (no DATABRICKS_SECRET_ARN), where the ARN is only
    knowable from the response. CreateOauth2CredentialProviderResponse carries it as the
    REQUIRED member clientSecretArn of shape Secret={secretArn}; there is no flat 'secretArn'
    member, so we read clientSecretArn.secretArn only. The EXTERNAL path is covered below.
    """

    _FakeAgentCore = _FakeAgentCore

    def _create(self, fake):
        """Call create_credential_provider in MANAGED mode, swallowing its progress prints."""
        # Pin MANAGED regardless of the caller's environment: an exported DATABRICKS_SECRET_ARN
        # would otherwise switch these to the EXTERNAL path and change ARN resolution.
        with mock.patch.object(deploy, "DATABRICKS_SECRET_ARN", ""), \
             contextlib.redirect_stdout(io.StringIO()):
            return deploy.create_credential_provider(fake)

    def test_reads_client_secret_arn(self):
        fake = self._FakeAgentCore(
            {"credentialProviderArn": "arn:prov", "clientSecretArn": {"secretArn": "arn:nested"}}
        )
        provider_arn, secret_arn = self._create(fake)
        self.assertEqual(provider_arn, "arn:prov")
        self.assertEqual(secret_arn, "arn:nested")

    def test_missing_client_secret_arn_aborts(self):
        fake = self._FakeAgentCore({"credentialProviderArn": "arn:prov"})
        with self.assertRaises(SystemExit) as ctx:
            self._create(fake)
        self.assertIn("secret ARN", str(ctx.exception))

    def test_malformed_client_secret_arn_aborts(self):
        # Present but not the Secret={secretArn} shape (e.g. a bare string, or missing secretArn):
        # must not crash and must not silently drop the grant.
        for bad in ("not-a-dict", {}, {"other": "x"}):
            fake = self._FakeAgentCore({"credentialProviderArn": "arn:prov", "clientSecretArn": bad})
            with self.subTest(bad=bad), self.assertRaises(SystemExit) as ctx:
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

    def __init__(self, existing=None, values=None, deleted=(), binary=None):
        # existing: {name: arn} for secrets that already exist.
        # values:   {name: SecretString} for the current stored value (for the merge path).
        # deleted:  names whose describe_secret should report a DeletedDate (recovery window).
        # binary:   {name: bytes} whose get_secret_value returns SecretBinary and no SecretString.
        self._store = dict(existing or {})
        self._values = dict(values or {})
        self._deleted = set(deleted)
        self._binary = dict(binary or {})
        self.exceptions = self  # so client.exceptions.ResourceNotFoundException resolves
        self.calls = []

    def describe_secret(self, SecretId):
        self.calls.append(("describe_secret", SecretId))
        if SecretId not in self._store:
            raise self.ResourceNotFoundException(SecretId)
        out = {"ARN": self._store[SecretId]}
        if SecretId in self._deleted:
            out["DeletedDate"] = "2026-01-01T00:00:00+00:00"
        return out

    def get_secret_value(self, SecretId):
        # Mirror the real API: a version carries either SecretString or SecretBinary, never both,
        # and a binary secret's response has NO SecretString key at all.
        self.calls.append(("get_secret_value", SecretId))
        if SecretId in self._binary:
            return {"SecretBinary": self._binary[SecretId]}
        if SecretId in self._values:
            return {"SecretString": self._values[SecretId]}
        return {}

    def create_secret(self, Name, SecretString, Description=None):
        arn = f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{Name}-abcde"
        self._store[Name] = arn
        self._values[Name] = SecretString
        self.calls.append(("create_secret", Name, SecretString))
        return {"ARN": arn}

    def put_secret_value(self, SecretId, SecretString):
        self._values[SecretId] = SecretString
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
        existing = json.dumps({"client_secret": "old"})
        client = _FakeSecretsManager(existing={self._NAME: arn}, values={self._NAME: existing})
        self._patch(secrets_setup, "DATABRICKS_CLIENT_SECRET", "s3cr3t")
        self._run(secrets_setup.provision, client, self._NAME, "client_secret")
        self.assertIn("put_secret_value", client._kinds())
        self.assertNotIn("create_secret", client._kinds())
        self.assertFalse(self._state["created"])  # pre-existing: we won't --delete it

    def test_provision_merges_into_existing_document_preserving_siblings(self):
        # Adopting an existing multi-field secret must set only our key and leave the rest —
        # a full replace silently deletes sibling keys the operator may rely on.
        arn = f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{self._NAME}-abcde"
        existing = json.dumps({"client_secret": "old", "other_key": "keep-me"})
        client = _FakeSecretsManager(existing={self._NAME: arn}, values={self._NAME: existing})
        self._patch(secrets_setup, "DATABRICKS_CLIENT_SECRET", "new-secret")
        self._run(secrets_setup.provision, client, self._NAME, "client_secret")
        stored = next(c[2] for c in client.calls if c[0] == "put_secret_value")
        self.assertEqual(
            json.loads(stored), {"client_secret": "new-secret", "other_key": "keep-me"}
        )

    def test_provision_refuses_to_clobber_non_json_secret(self):
        arn = f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{self._NAME}-abcde"
        client = _FakeSecretsManager(existing={self._NAME: arn}, values={self._NAME: "plain-not-json"})
        self._patch(secrets_setup, "DATABRICKS_CLIENT_SECRET", "s3cr3t")
        with self.assertRaises(SystemExit) as ctx:
            self._run(secrets_setup.provision, client, self._NAME, "client_secret")
        self.assertIn("not JSON", str(ctx.exception))
        self.assertNotIn("put_secret_value", client._kinds())  # nothing written

    def test_provision_refuses_non_object_json_secret(self):
        # Valid JSON but not an object (a bare string / array / number / null) cannot be merged
        # into and would otherwise raise a raw TypeError on item assignment. Refuse cleanly.
        arn = f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{self._NAME}-abcde"
        for body in ('"just-a-string"', "[1, 2]", "42", "null"):
            client = _FakeSecretsManager(existing={self._NAME: arn}, values={self._NAME: body})
            self._patch(secrets_setup, "DATABRICKS_CLIENT_SECRET", "s3cr3t")
            with self.subTest(body=body), self.assertRaises(SystemExit):
                self._run(secrets_setup.provision, client, self._NAME, "client_secret")
            self.assertNotIn("put_secret_value", client._kinds())  # nothing written

    def test_provision_refuses_binary_secret(self):
        # A binary secret (keystore/cert) has no SecretString; merging a JSON document in would
        # replace it wholesale. Same data-loss class as non-JSON, reached through another door.
        arn = f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{self._NAME}-abcde"
        client = _FakeSecretsManager(existing={self._NAME: arn}, binary={self._NAME: b"\x00keystore"})
        self._patch(secrets_setup, "DATABRICKS_CLIENT_SECRET", "s3cr3t")
        with self.assertRaises(SystemExit) as ctx:
            self._run(secrets_setup.provision, client, self._NAME, "client_secret")
        self.assertIn("binary", str(ctx.exception).lower())
        self.assertNotIn("put_secret_value", client._kinds())  # nothing written

    def test_provision_adoption_notice_prints_before_the_write(self):
        # The "Adopting" notice must precede the put; printing it after the write would tell the
        # operator we adopted only once the change already landed.
        arn = f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{self._NAME}-abcde"
        client = _FakeSecretsManager(
            existing={self._NAME: arn}, values={self._NAME: json.dumps({"client_secret": "old"})}
        )
        self._patch(secrets_setup, "DATABRICKS_CLIENT_SECRET", "s3cr3t")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            secrets_setup.provision(client, self._NAME, "client_secret")
        text = out.getvalue()
        self.assertIn("Adopting", text)
        self.assertLess(text.index("Adopting"), text.index("Updated secret"))

    def test_provision_warns_before_orphaning_a_different_created_secret(self):
        # The single-slot state file records secret A (created by us). Provisioning a NEW secret B
        # replaces that record; the operator must be warned that A is no longer --deletable.
        other_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:other-AbCdEf"
        self._state.update({"secret_arn": other_arn, "name": "other/secret", "created": True})
        client = _FakeSecretsManager()  # self._NAME does not exist -> create path
        self._patch(secrets_setup, "DATABRICKS_CLIENT_SECRET", "s3cr3t")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            secrets_setup.provision(client, self._NAME, "client_secret")
        self.assertIn("other/secret", out.getvalue())
        self.assertIn("Warning", out.getvalue())

    def test_provision_on_secret_in_recovery_window_tells_user_to_restore(self):
        # describe_secret still resolves a secret scheduled for deletion; provision must not
        # take the adopt branch and crash on put_secret_value (the README's --delete/re-run path).
        arn = f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{self._NAME}-abcde"
        client = _FakeSecretsManager(existing={self._NAME: arn}, deleted={self._NAME})
        self._patch(secrets_setup, "DATABRICKS_CLIENT_SECRET", "s3cr3t")
        with self.assertRaises(SystemExit) as ctx:
            self._run(secrets_setup.provision, client, self._NAME, "client_secret")
        self.assertIn("restore-secret", str(ctx.exception))
        self.assertNotIn("put_secret_value", client._kinds())

    def test_custom_json_key_flows_through_both_sides(self):
        # The key secrets_setup.py writes into the secret JSON and the key deploy.py hands
        # the provider in clientSecretConfig.jsonKey must be the SAME key, or the provider
        # reads a field that isn't there and every tool call 403s after READY. Exercise a
        # NON-default key so a hardcoded "client_secret" on either side is caught.
        custom = "db_oauth_secret"
        client = _FakeSecretsManager()
        self._patch(secrets_setup, "DATABRICKS_CLIENT_SECRET", "s3cr3t")
        self._run(secrets_setup.provision, client, self._NAME, custom)
        stored = next(c[2] for c in client.calls if c[0] == "create_secret")
        self.assertEqual(json.loads(stored), {custom: "s3cr3t"})  # secrets_setup writes it

        arn = self._state["secret_arn"]
        with mock.patch.multiple(
            deploy, DATABRICKS_SECRET_ARN=arn, DATABRICKS_SECRET_JSON_KEY=custom
        ):
            cfg = deploy.client_secret_config()
        self.assertEqual(cfg["clientSecretConfig"]["jsonKey"], custom)  # deploy references it
        # And the two sides agree on the key -- the whole point of the check.
        self.assertEqual(json.loads(stored).get(cfg["clientSecretConfig"]["jsonKey"]), "s3cr3t")

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

    def test_delete_missing_name_preserves_record_for_other_secret(self):
        # State records a secret THIS script created under _NAME. Running --delete against a
        # different, nonexistent name must NOT wipe that record (else the real one is orphaned).
        created_arn = f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{self._NAME}-abcde"
        self._state.update({"secret_arn": created_arn, "name": self._NAME, "created": True})
        other_name = "some/other-secret"
        client = _FakeSecretsManager()  # other_name does not exist
        self._run(secrets_setup.delete, client, other_name, force=False, assume_yes=True)
        self.assertEqual(self._state.get("name"), self._NAME)  # record survives
        self.assertTrue(self._state.get("created"))

    def test_delete_on_secret_already_in_recovery_window_is_noop(self):
        # describe_secret still resolves a secret scheduled for deletion; calling delete_secret
        # again would raise InvalidRequestException AFTER the operator answered the prompt.
        arn = f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{self._NAME}-abcde"
        client = _FakeSecretsManager(existing={self._NAME: arn}, deleted={self._NAME})
        self._state.update({"secret_arn": arn, "name": self._NAME, "created": True})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            secrets_setup.delete(client, self._NAME, force=False, assume_yes=True)
        self.assertIn("restore-secret", out.getvalue())
        self.assertNotIn("delete_secret", client._kinds())  # not called again


class ResolveSecretJsonKeyTest(unittest.TestCase):
    """deploy.resolve_secret_json_key() defaults the jsonKey from secret_state.json so the ARN
    and its key stay together across the two processes. It adopts the recorded key when the
    operator did not set one explicitly (removing the cross-process drift class), honors an
    explicit override with a warning rather than an abort, and touches nothing on the MANAGED
    path or when the record describes a different secret."""

    _ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:db-AbCdEf"

    def _run(self, arn, deploy_key, state, explicit):
        with mock.patch.object(deploy, "DATABRICKS_SECRET_ARN", arn), \
             mock.patch.object(deploy, "DATABRICKS_SECRET_JSON_KEY", deploy_key), \
             mock.patch.object(deploy, "DATABRICKS_SECRET_JSON_KEY_SET", explicit), \
             mock.patch.object(deploy, "recorded_secret_state", lambda: state), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return deploy.resolve_secret_json_key()

    def test_no_arn_returns_key_unchanged(self):
        # MANAGED path: no record consulted, key returned as-is.
        self.assertEqual(self._run("", "anything", {"secret_arn": "x", "json_key": "y"}, False), "anything")

    def test_adopts_recorded_key_when_not_explicit(self):
        key = self._run(self._ARN, "client_secret",
                        {"secret_arn": self._ARN, "json_key": "db_oauth_secret"}, explicit=False)
        self.assertEqual(key, "db_oauth_secret")  # the drift the guard used to abort on, now resolved

    def test_matching_recorded_key_returns_it(self):
        key = self._run(self._ARN, "db_oauth_secret",
                        {"secret_arn": self._ARN, "json_key": "db_oauth_secret"}, explicit=False)
        self.assertEqual(key, "db_oauth_secret")

    def test_explicit_override_is_honored_not_aborted(self):
        # An explicit key that disagrees with the record must be used (merge leaves both keys),
        # not rejected -- the old guard aborted deploys that would have worked.
        key = self._run(self._ARN, "client_secret",
                        {"secret_arn": self._ARN, "json_key": "db_oauth_secret"}, explicit=True)
        self.assertEqual(key, "client_secret")

    def test_explicit_override_warns_on_stderr(self):
        with mock.patch.object(deploy, "DATABRICKS_SECRET_ARN", self._ARN), \
             mock.patch.object(deploy, "DATABRICKS_SECRET_JSON_KEY", "client_secret"), \
             mock.patch.object(deploy, "DATABRICKS_SECRET_JSON_KEY_SET", True), \
             mock.patch.object(deploy, "recorded_secret_state",
                               lambda: {"secret_arn": self._ARN, "json_key": "db_oauth_secret"}):
            stderr = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                deploy.resolve_secret_json_key()
            self.assertIn("db_oauth_secret", stderr.getvalue())

    def test_state_for_a_different_secret_returns_deploy_key(self):
        other = "arn:aws:secretsmanager:us-east-1:123456789012:secret:other-XyZ123"
        key = self._run(self._ARN, "client_secret",
                        {"secret_arn": other, "json_key": "db_oauth_secret"}, explicit=False)
        self.assertEqual(key, "client_secret")

    def test_missing_state_returns_deploy_key(self):
        self.assertEqual(self._run(self._ARN, "client_secret", {}, explicit=False), "client_secret")

    def test_empty_recorded_key_returns_deploy_key(self):
        # A falsy recorded key must not override -- fall back to the configured key.
        key = self._run(self._ARN, "client_secret",
                        {"secret_arn": self._ARN, "json_key": ""}, explicit=False)
        self.assertEqual(key, "client_secret")


class RecordedSecretStateTest(unittest.TestCase):
    """deploy.recorded_secret_state() degrades to {} on a missing, unreadable, or non-object
    state file rather than crashing the caller's .get()."""

    def _read(self, contents):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = f"{d}/secret_state.json"
            if contents is not None:
                with open(path, "w") as f:
                    f.write(contents)
            with mock.patch.object(deploy, "SECRET_STATE_FILE", path):
                return deploy.recorded_secret_state()

    def test_missing_file_is_empty(self):
        self.assertEqual(self._read(None), {})

    def test_non_object_json_is_empty(self):
        for body in ("[1, 2]", '"a"', "null", "123"):
            with self.subTest(body=body):
                self.assertEqual(self._read(body), {})

    def test_malformed_json_is_empty(self):
        self.assertEqual(self._read("{not json"), {})

    def test_object_is_returned(self):
        self.assertEqual(self._read('{"json_key": "k"}'), {"json_key": "k"})


def _actions_of(statement):
    action = statement.get("Action", [])
    return action if isinstance(action, list) else [action]


def _resources_of(statement):
    resource = statement.get("Resource", [])
    return resource if isinstance(resource, list) else [resource]


if __name__ == "__main__":
    unittest.main()
