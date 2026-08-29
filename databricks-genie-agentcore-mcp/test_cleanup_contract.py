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
    def __init__(self):
        self.calls = []
        self.exceptions = mock.Mock(NoSuchEntityException=_NoSuchEntityException)

    def delete_role_policy(self, RoleName, PolicyName):  # noqa: N803
        self.calls.append(("delete_role_policy", RoleName))
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
    def run_cleanup(self, state, targets=None, list_raises=False):
        """Run cleanup.main() against a stubbed AWS and return the fakes."""
        agentcore = FakeAgentCore(targets=targets, list_raises=list_raises)
        iam, cognito = FakeIam(), FakeCognito()

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
