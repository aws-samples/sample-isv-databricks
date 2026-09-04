"""Pins the ownership and state contract documented at the top of cleanup.py.

These are the rules whose violation stranded billable AWS resources, and every one of
them is invisible without either a live AWS account or a stub. Uses only the standard
library so the sample gains no test dependency:

    python -m unittest test_cleanup_contract -v
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import cleanup


class _NoSuchEntityException(Exception):
    pass


class _ResourceNotFoundException(Exception):
    pass


class _EntityAlreadyExistsException(Exception):
    pass


class FakeAgentCore:
    def __init__(self, targets=None, list_raises=False):
        self._targets = list(targets or [])
        self._list_raises = list_raises
        self.calls = []
        self.exceptions = mock.Mock(ConflictException=Exception)

    def list_gateway_targets(self, gatewayIdentifier):  # noqa: N803 - boto3 casing
        self.calls.append(("list_gateway_targets", gatewayIdentifier))
        if self._list_raises:
            raise RuntimeError("throttled")
        return {"items": [{"targetId": t} for t in self._targets]}

    def delete_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        self.calls.append(("delete_gateway_target", targetId))
        self._targets = [t for t in self._targets if t != targetId]

    def delete_oauth2_credential_provider(self, name):
        self.calls.append(("delete_oauth2_credential_provider", name))

    def delete_gateway(self, gatewayIdentifier):  # noqa: N803
        self.calls.append(("delete_gateway", gatewayIdentifier))


class FakeIam:
    def __init__(self, policy_attached=False, policy_error=None, policy_doc=None):
        self.calls = []
        self._policy_attached = policy_attached
        self._policy_error = policy_error
        self._policy_doc = policy_doc
        self.exceptions = mock.Mock(NoSuchEntityException=_NoSuchEntityException)

    def get_role_policy(self, RoleName, PolicyName):  # noqa: N803
        self.calls.append(("get_role_policy", RoleName))
        if self._policy_doc is None:
            raise _NoSuchEntityException("no such policy")
        return {"PolicyDocument": self._policy_doc}

    def delete_role_policy(self, RoleName, PolicyName):  # noqa: N803
        self.calls.append(("delete_role_policy", RoleName))
        if self._policy_error:
            raise self._policy_error
        if not self._policy_attached:
            raise _NoSuchEntityException("policy was never attached")

    def delete_role(self, RoleName):  # noqa: N803
        self.calls.append(("delete_role", RoleName))


class FakeCognito:
    def __init__(self):
        self.calls = []

    def delete_user_pool_domain(self, Domain, UserPoolId):  # noqa: N803
        self.calls.append(("delete_user_pool_domain", Domain))

    def delete_user_pool(self, UserPoolId):  # noqa: N803
        self.calls.append(("delete_user_pool", UserPoolId))


class ContractTest(unittest.TestCase):
    def run_cleanup(self, state, targets=None, list_raises=False, policy_attached=False,
                    policy_error=None, policy_doc=None):
        """Run cleanup.main() against a stubbed AWS and return the fakes."""
        agentcore = FakeAgentCore(targets=targets, list_raises=list_raises)
        iam = FakeIam(policy_attached=policy_attached, policy_error=policy_error, policy_doc=policy_doc)
        cognito = FakeCognito()

        def fake_client(service, **kwargs):
            return {"bedrock-agentcore-control": agentcore, "iam": iam, "cognito-idp": cognito}[service]

        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        try:
            with (
                mock.patch.object(cleanup, "STATE_FILE", path),
                mock.patch.object(cleanup.boto3, "client", side_effect=fake_client),
                mock.patch.object(cleanup.time, "sleep"),
                mock.patch.object(sys, "argv", ["cleanup.py", "--yes"]),
            ):
                cleanup.main()
            return agentcore, iam, cognito, os.path.exists(path)
        finally:
            if os.path.exists(path):
                os.remove(path)

    @staticmethod
    def state(**overrides):
        base = {
            "gateway_id": "gw-1",
            "target_id": None,
            "provider_arn": "arn:aws:bedrock-agentcore:us-west-2:1:token-vault/default/oauth2credentialprovider/databricks-genie-oauth",
            "region": "us-west-2",
            "role_arn": "arn:aws:iam::1:role/agentcore-DatabricksGenieGateway-role",
            "client_info": {"user_pool_id": "pool-1", "domain": "dom-1", "owned_pool": True},
        }
        base.update(overrides)
        return base

    # --- rule 4: targets come from AWS, not the state file --------------------
    def test_deletes_target_aws_reports_even_when_state_recorded_none(self):
        """The undeletable-gateway case: deploy died before recording target_id."""
        agentcore, _, _, _ = self.run_cleanup(self.state(target_id=None), targets=["REAL"])
        self.assertIn(("delete_gateway_target", "REAL"), agentcore.calls)

    def test_does_not_resurrect_a_target_aws_no_longer_reports(self):
        """A successful list is authoritative, so a partial cleanup can converge."""
        agentcore, _, _, removed = self.run_cleanup(self.state(target_id="STALE"), targets=[])
        self.assertNotIn(("delete_gateway_target", "STALE"), agentcore.calls)
        self.assertFalse(removed, "state file should be gone after a clean run")

    def test_falls_back_to_state_only_when_the_list_call_fails(self):
        agentcore, _, _, _ = self.run_cleanup(self.state(target_id="FROM-STATE"), list_raises=True)
        self.assertIn(("delete_gateway_target", "FROM-STATE"), agentcore.calls)

    # --- rule 2: never delete what we merely adopted --------------------------
    def test_adopted_cognito_pool_is_not_deleted(self):
        _, _, cognito, _ = self.run_cleanup(
            self.state(client_info={"user_pool_id": "pool-1", "domain": "dom-1", "owned_pool": False})
        )
        self.assertEqual(cognito.calls, [], "an adopted pool must be left alone")

    def test_owned_cognito_pool_is_deleted_domain_first(self):
        _, _, cognito, _ = self.run_cleanup(self.state())
        self.assertEqual(
            [c[0] for c in cognito.calls], ["delete_user_pool_domain", "delete_user_pool"]
        )

    # --- rule 6: only delete a provider this state file records ---------------
    def test_provider_not_deleted_when_state_records_none(self):
        agentcore, _, _, _ = self.run_cleanup(self.state(provider_arn=None), targets=["T1"])
        self.assertFalse(
            [c for c in agentcore.calls if c[0] == "delete_oauth2_credential_provider"],
            "deleting by bare name can destroy another deployment's provider",
        )

    # --- rule 5: dependency order --------------------------------------------
    def test_provider_deleted_after_targets_and_gateway_last(self):
        agentcore, _, _, _ = self.run_cleanup(self.state(), targets=["T1"])
        order = [c[0] for c in agentcore.calls]
        self.assertLess(
            order.index("delete_gateway_target"), order.index("delete_oauth2_credential_provider")
        )
        self.assertLess(
            order.index("delete_oauth2_credential_provider"), order.index("delete_gateway")
        )

    # --- rule 7: idempotent, and the IAM split -------------------------------
    def test_role_is_deleted_even_when_the_inline_policy_never_existed(self):
        """Sharing one try block here orphaned the role and looped forever."""
        _, iam, _, removed = self.run_cleanup(self.state())
        self.assertIn("delete_role", [c[0] for c in iam.calls])
        self.assertFalse(removed, "a fully successful run removes the state file")

    def test_a_failed_policy_delete_keeps_the_state_file(self):
        """Rule 7: an unexpected failure must leave state on disk to re-run against.
        Printing and moving on removed the file while the grant was still attached."""
        _, _, _, state_kept = self.run_cleanup(
            self.state(owned_role=True), policy_attached=True,
            policy_error=PermissionError("iam:DeleteRolePolicy denied"))
        self.assertTrue(state_kept, "state file was removed despite a failed policy delete")

    def test_our_own_policy_on_an_adopted_role_is_removed(self):
        """The document names the provider recorded in state, so the grant is ours: leaving it
        would keep GetWorkloadAccessToken and GetResourceOauth2Token on token-vault/default,
        which cleanup never deletes, attached to a role we do not own."""
        role = "agentcore-DatabricksGenieGateway-role"
        doc = {"Statement": [{"Action": "bedrock-agentcore:GetResourceOauth2Token",
                             "Resource": ["arn:aws:bedrock-agentcore:us-west-2:1:token-vault/default/oauth2credentialprovider/databricks-genie-oauth", "arn:aws:bedrock-agentcore:us-west-2:1:token-vault/default"]}]}
        _, iam, _, _ = self.run_cleanup(self.state(owned_role=False), policy_doc=doc)
        self.assertIn(("delete_role_policy", role), iam.calls)
        self.assertNotIn(("delete_role", role), iam.calls)

    def test_another_deployments_policy_is_not_removed(self):
        """A different provider ARN means another deployment wrote it -- deleting it there
        strips that deployment's only token-minting grant."""
        role = "agentcore-DatabricksGenieGateway-role"
        doc = {"Statement": [{"Action": "bedrock-agentcore:GetResourceOauth2Token",
                             "Resource": ["arn:aws:bedrock-agentcore:eu-west-1:9:token-vault/default/oauth2credentialprovider/other"]}]}
        _, iam, _, _ = self.run_cleanup(self.state(owned_role=False), policy_doc=doc)
        self.assertNotIn(("delete_role_policy", role), iam.calls)
        self.assertNotIn(("delete_role", role), iam.calls)

    def test_an_adopted_role_with_no_policy_is_left_alone(self):
        """No document attached, so there is nothing to decide and nothing to delete."""
        role = "agentcore-DatabricksGenieGateway-role"
        _, iam, _, _ = self.run_cleanup(self.state(owned_role=False), policy_doc=None)
        self.assertNotIn(("delete_role_policy", role), iam.calls)
        self.assertNotIn(("delete_role", role), iam.calls)

    def test_no_gateway_recorded_still_tears_down_pool_and_role(self):
        """deploy.py records the pool and role before the gateway exists."""
        agentcore, iam, cognito, removed = self.run_cleanup(self.state(gateway_id=None, provider_arn=None))
        self.assertEqual(agentcore.calls, [], "no gateway means no gateway API calls at all")
        self.assertIn("delete_role", [c[0] for c in iam.calls])
        self.assertIn("delete_user_pool", [c[0] for c in cognito.calls])
        self.assertFalse(removed)


class DeployGuardTest(unittest.TestCase):
    # --- rule 3: refuse to start over a live deployment ----------------------
    def test_deploy_refuses_when_state_already_records_a_gateway(self):
        # Restore env and sys.modules, or this test leaks into whatever runs after it and
        # the suite silently becomes order-dependent.
        env = mock.patch.dict(
            os.environ,
            {
                "DATABRICKS_HOST": "https://example.cloud.databricks.com",
                "DATABRICKS_CLIENT_ID": "cid",
                "DATABRICKS_CLIENT_SECRET": "sec",
                "GENIE_SPACE_ID": "space",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        saved = {m: sys.modules.get(m) for m in ("config", "deploy")}

        def _restore():
            for m, mod in saved.items():
                if mod is None:
                    sys.modules.pop(m, None)
                else:
                    sys.modules[m] = mod

        self.addCleanup(_restore)
        for mod in ("config", "deploy"):
            sys.modules.pop(mod, None)
        import deploy  # noqa: PLC0415 - re-imported after the env is set

        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"gateway_id": "gw-live"}, f)
        try:
            with mock.patch.object(deploy, "STATE_FILE", path), self.assertRaises(SystemExit) as ctx:
                deploy.deploy()
            self.assertIn("gw-live", str(ctx.exception))
            self.assertIn("cleanup.py", str(ctx.exception))
        finally:
            os.remove(path)




class RoleOwnershipTest(unittest.TestCase):
    """Ownership must never downgrade when deploy re-runs over its own IAM role.

    Deploy #1 can create the role, record owned_role=true, then fail before the gateway
    exists. gateway_id is still null so rule 3 permits a re-run, and that re-run finds
    its OWN role via EntityAlreadyExists. Reporting that as adopted made cleanup print
    "this sample did not create it" forever, leaving a role nothing could remove.
    """

    class _Iam:
        def __init__(self):
            self.exceptions = mock.Mock(EntityAlreadyExistsException=_EntityAlreadyExistsException)

        def create_role(self, **kwargs):
            raise _EntityAlreadyExistsException("role already exists")

        def update_assume_role_policy(self, **kwargs):
            pass

        def get_role(self, RoleName):  # noqa: N803 - boto3 casing
            return {"Role": {"Arn": f"arn:aws:iam::1:role/{RoleName}"}}

    def _make_setup(self, fake_iam=None):
        import gateway_setup  # noqa: PLC0415 - imported here so the module stays light

        fake_iam = fake_iam or self._Iam()

        def fake_client(service, **kwargs):
            if service == "iam":
                return fake_iam
            if service == "sts":
                return mock.Mock(get_caller_identity=mock.Mock(return_value={"Account": "1"}))
            return mock.Mock()

        with mock.patch.object(gateway_setup.boto3, "client", side_effect=fake_client):
            return gateway_setup.GatewaySetup("us-west-2")

    def test_a_role_this_sample_created_is_still_owned_after_a_re_run(self):
        _, owned = self._make_setup().create_gateway_role("G", previously_owned=True)
        self.assertTrue(owned, "cleanup would refuse to delete a role this sample created")

    def test_a_genuinely_pre_existing_role_is_not_claimed(self):
        _, owned = self._make_setup().create_gateway_role("G", previously_owned=False)
        self.assertFalse(owned, "cleanup would delete a role belonging to something else")

    def test_an_adopted_roles_trust_policy_is_not_rewritten(self):
        """The trust policy scopes aws:SourceArn to this region, but role names are global.
        Refreshing a role we did not create locks another region's deployment out of
        assuming it, and cleanup never restores it."""
        iam = self._Iam()
        iam.update_assume_role_policy = mock.Mock()
        setup = self._make_setup(iam)
        setup.create_gateway_role("G", previously_owned=False)
        iam.update_assume_role_policy.assert_not_called()
        setup.create_gateway_role("G", previously_owned=True)
        iam.update_assume_role_policy.assert_called_once()


class DomainWaitTest(unittest.TestCase):
    """The domain wait must ride out blips, fail fast on faults, and stay bounded.

    Two earlier two-way splits got this wrong in opposite directions: the first mapped a
    transient error onto `status = None` and fell into `if not status: return False`, and the
    replacement treated "no error code" as fatal -- which is every botocore connection reset
    and timeout, the exact blips this wait exists to survive. Hence real exception types here.
    """

    @staticmethod
    def _client_error(code):
        from botocore.exceptions import ClientError  # noqa: PLC0415

        return ClientError({"Error": {"Code": code}}, "DescribeUserPoolDomain")

    def _setup(self, cognito):
        import gateway_setup  # noqa: PLC0415

        def fake_client(service, **kwargs):
            if service == "cognito-idp":
                return cognito
            if service == "sts":
                return mock.Mock(get_caller_identity=mock.Mock(return_value={"Account": "1"}))
            return mock.Mock()

        with mock.patch.object(gateway_setup.boto3, "client", side_effect=fake_client):
            return gateway_setup, gateway_setup.GatewaySetup("us-west-2")

    def _run(self, side_effect, timeout=900):
        cognito = mock.Mock(describe_user_pool_domain=mock.Mock(side_effect=side_effect))
        gs, setup = self._setup(cognito)
        with mock.patch.object(gs.time, "sleep"):
            return setup._wait_for_domain("dom-1", timeout=timeout), cognito

    def test_a_service_throttle_is_retried_until_active(self):
        ok, cognito = self._run([self._client_error("TooManyRequestsException"),
                                 {"DomainDescription": {"Status": "ACTIVE"}}])
        self.assertTrue(ok, "a throttle before ACTIVE must not abort the deploy")
        self.assertEqual(cognito.describe_user_pool_domain.call_count, 2)

    def test_a_network_fault_is_retried_until_active(self):
        from botocore.exceptions import ConnectTimeoutError  # noqa: PLC0415

        ok, cognito = self._run([ConnectTimeoutError(endpoint_url="https://cognito-idp"),
                                 {"DomainDescription": {"Status": "ACTIVE"}}])
        self.assertTrue(ok, "a connection timeout carries no error code and must still retry")
        self.assertEqual(cognito.describe_user_pool_domain.call_count, 2)

    def test_a_terminal_service_error_fails_fast(self):
        ok, cognito = self._run(self._client_error("AccessDeniedException"))
        self.assertFalse(ok)
        self.assertEqual(cognito.describe_user_pool_domain.call_count, 1, "a denied describe must not retry")

    def test_a_fault_in_the_call_path_is_not_retried(self):
        ok, cognito = self._run(TypeError("bad response shape"))
        self.assertFalse(ok)
        self.assertEqual(cognito.describe_user_pool_domain.call_count, 1,
                         "a programming error must fail fast, not retry for the whole timeout")

    def test_missing_credentials_is_not_retried(self):
        from botocore.exceptions import NoCredentialsError  # noqa: PLC0415

        ok, cognito = self._run(NoCredentialsError())
        self.assertFalse(ok, "BotoCoreError, but not a network fault -- retrying just burns the window")
        self.assertEqual(cognito.describe_user_pool_domain.call_count, 1)

    def test_permanent_throttling_still_terminates(self):
        ok, cognito = self._run(self._client_error("TooManyRequestsException"), timeout=60)
        self.assertFalse(ok)
        self.assertLessEqual(cognito.describe_user_pool_domain.call_count, 5, "retry loop is not bounded")


class InitialStateTest(unittest.TestCase):
    """A re-run's first persist must not erase what the previous attempt recorded."""

    def _initial_state(self, prior):
        import deploy  # noqa: PLC0415

        return deploy._initial_state(prior)

    def test_a_prior_role_is_carried_forward(self):
        s = self._initial_state({"role_arn": "arn:aws:iam::1:role/r", "owned_role": True,
                                 "client_info": {"user_pool_id": "pool-1", "owned_pool": True}})
        self.assertEqual(s["role_arn"], "arn:aws:iam::1:role/r")
        self.assertTrue(s["owned_role"], "ownership must survive the first write of a re-run")
        self.assertEqual(s["client_info"]["user_pool_id"], "pool-1")

    def test_a_first_run_starts_empty(self):
        s = self._initial_state(None)
        self.assertIsNone(s["role_arn"])
        self.assertIsNone(s["client_info"])
        self.assertIsNone(s["gateway_id"])


class ArnGuardTest(unittest.TestCase):
    """main() reads the region out of field 3, so a malformed ARN must fail readably."""

    def _resolve(self, arn):
        import invoke_runtime  # noqa: PLC0415

        cfg = {"default_agent": "a", "agents": {"a": {"bedrock_agentcore": {"agent_arn": arn}}}}
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            try:
                os.chdir(d)
                with open(".bedrock_agentcore.yaml", "w") as f:
                    f.write(json.dumps(cfg))  # valid YAML
                return invoke_runtime.resolve_agent_arn()
            finally:
                os.chdir(cwd)

    def test_a_full_arn_is_accepted(self):
        arn = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/x"
        self.assertEqual(self._resolve(arn), arn)

    def test_an_empty_region_field_is_rejected(self):
        with self.assertRaises(SystemExit):
            self._resolve("arn:aws:bedrock-agentcore::123456789012:runtime/x")

    def test_a_bare_id_is_rejected(self):
        with self.assertRaises(SystemExit):
            self._resolve("my-agent")


class AdoptedTrustPolicyTest(unittest.TestCase):
    """An adopted role whose trust policy excludes this region must fail at deploy."""

    def _setup(self, trust_doc):
        import gateway_setup  # noqa: PLC0415

        iam = mock.Mock()
        iam.exceptions = mock.Mock(EntityAlreadyExistsException=_EntityAlreadyExistsException)
        iam.create_role.side_effect = _EntityAlreadyExistsException("exists")
        iam.get_role.return_value = {"Role": {"Arn": "arn:aws:iam::1:role/r",
                                              "AssumeRolePolicyDocument": trust_doc}}

        def fake_client(service, **kwargs):
            if service == "iam":
                return iam
            if service == "sts":
                return mock.Mock(get_caller_identity=mock.Mock(return_value={"Account": "1"}))
            return mock.Mock()

        with mock.patch.object(gateway_setup.boto3, "client", side_effect=fake_client):
            return gateway_setup.GatewaySetup("us-west-2")

    def test_a_role_scoped_to_another_region_fails_loudly(self):
        doc = {"Statement": [{"Condition": {"ArnLike": {
            "aws:SourceArn": "arn:aws:bedrock-agentcore:us-east-1:1:*"}}}]}
        setup = self._setup(doc)
        with self.assertRaises(SystemExit) as ctx:
            setup.create_gateway_role("G", previously_owned=False)
        self.assertIn("us-west-2", str(ctx.exception))

    def test_a_role_scoped_to_this_region_is_accepted(self):
        doc = {"Statement": [{"Condition": {"ArnLike": {
            "aws:SourceArn": "arn:aws:bedrock-agentcore:us-west-2:1:*"}}}]}
        _, owned = self._setup(doc).create_gateway_role("G", previously_owned=False)
        self.assertFalse(owned)


if __name__ == "__main__":
    unittest.main()
