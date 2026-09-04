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
    def __init__(self, policy_attached=False):
        self.calls = []
        self._policy_attached = policy_attached
        self.exceptions = mock.Mock(NoSuchEntityException=_NoSuchEntityException)

    def delete_role_policy(self, RoleName, PolicyName):  # noqa: N803
        self.calls.append(("delete_role_policy", RoleName))
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
    def run_cleanup(self, state, targets=None, list_raises=False, policy_attached=False):
        """Run cleanup.main() against a stubbed AWS and return the fakes."""
        agentcore = FakeAgentCore(targets=targets, list_raises=list_raises)
        iam, cognito = FakeIam(policy_attached=policy_attached), FakeCognito()

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

    def test_adopted_role_is_kept_but_this_samples_policy_is_removed(self):
        """Rule 2 blocks deleting a role we only adopted -- but the grant this sample
        attached must still go, or a role we do not own keeps standing read access to the
        secret and credential provider this same cleanup just deleted."""
        role = "agentcore-DatabricksGenieGateway-role"
        _, iam, _, _ = self.run_cleanup(self.state(owned_role=False), policy_attached=True)
        self.assertIn(("delete_role_policy", role), iam.calls)
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


if __name__ == "__main__":
    unittest.main()


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

    def _make_setup(self):
        import gateway_setup  # noqa: PLC0415 - imported here so the module stays light

        fake_iam = self._Iam()

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


class DomainWaitTest(unittest.TestCase):
    """The domain wait must survive a blip and still be bounded.

    A transient DescribeUserPoolDomain error used to set status=None and fall into the
    `if not status: return False` guard, so the first throttle aborted the deploy and sent
    the user to tear down a domain that was seconds from ACTIVE. No test covered that path,
    which is why the whole suite passed over it.
    """

    class _Boom(Exception):
        def __init__(self, code):
            super().__init__(code)
            self.response = {"Error": {"Code": code}}

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

    def test_a_transient_error_is_retried_until_the_domain_is_active(self):
        seq = [self._Boom("TooManyRequestsException"), self._Boom("InternalErrorException"),
               {"DomainDescription": {"Status": "ACTIVE"}}]

        def describe(Domain):  # noqa: N803 - boto3 casing
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        gs, setup = self._setup(mock.Mock(describe_user_pool_domain=describe))
        with mock.patch.object(gs.time, "sleep"):
            self.assertTrue(setup._wait_for_domain("dom-1", timeout=900),
                            "a throttle before ACTIVE must not abort the deploy")
        self.assertEqual(seq, [], "the wait stopped polling instead of retrying")

    def test_a_terminal_error_fails_fast(self):
        cognito = mock.Mock(describe_user_pool_domain=mock.Mock(side_effect=self._Boom("AccessDeniedException")))
        gs, setup = self._setup(cognito)
        with mock.patch.object(gs.time, "sleep"):
            self.assertFalse(setup._wait_for_domain("dom-1", timeout=900))
        self.assertEqual(cognito.describe_user_pool_domain.call_count, 1, "a denied describe must not be retried")

    def test_permanent_transient_errors_still_terminate(self):
        cognito = mock.Mock(describe_user_pool_domain=mock.Mock(side_effect=self._Boom("TooManyRequestsException")))
        gs, setup = self._setup(cognito)
        with mock.patch.object(gs.time, "sleep"):
            self.assertFalse(setup._wait_for_domain("dom-1", timeout=60))
        self.assertLessEqual(cognito.describe_user_pool_domain.call_count, 5, "retry loop is not bounded by timeout")

    def test_a_non_service_error_is_not_retried(self):
        """A fault in this call path is not a throttle. Retrying it burned the full
        15-minute window and then reported a timeout instead of the actual error."""
        cognito = mock.Mock(describe_user_pool_domain=mock.Mock(side_effect=TypeError("bad response shape")))
        gs, setup = self._setup(cognito)
        with mock.patch.object(gs.time, "sleep"):
            self.assertFalse(setup._wait_for_domain("dom-1", timeout=900))
        self.assertEqual(cognito.describe_user_pool_domain.call_count, 1,
                         "a programming error must fail fast, not retry for the whole timeout")

