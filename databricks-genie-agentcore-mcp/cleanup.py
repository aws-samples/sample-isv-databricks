"""Delete every AWS resource this sample created.

Removes the gateway target, the Databricks OAuth2 credential provider and the
gateway itself, then deletes the local state file. Run `agentcore destroy`
separately to remove the deployed Runtime agent.

Usage:
    python cleanup.py
    python cleanup.py --yes    # skip the confirmation prompt
"""

import argparse
import json
import os
import time

import boto3
from config import CREDENTIAL_PROVIDER_NAME, IAM_POLICY_NAME, STATE_FILE


def _discover_target_ids(agentcore, gateway_id: str, recorded_id: str | None) -> list:
    """Return the targets actually attached to the gateway.

    Falls back to the id recorded in the state file only if the API call fails,
    so a state file that never recorded a target cannot hide a real one.
    """
    try:
        items = agentcore.list_gateway_targets(gatewayIdentifier=gateway_id).get("items", [])
        found = [t["targetId"] for t in items if t.get("targetId")]
        if recorded_id and recorded_id not in found:
            found.append(recorded_id)
        return found
    except Exception as exc:  # noqa: BLE001
        print(f"Could not list gateway targets ({exc}); falling back to the state file.")
        return [recorded_id] if recorded_id else []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="delete without confirmation")
    args = parser.parse_args()

    try:
        with open(STATE_FILE) as f:
            config = json.load(f)
    except FileNotFoundError:
        raise SystemExit(f"{STATE_FILE} not found — nothing to clean up.")

    # May be None: deploy.py now persists the Cognito pool and IAM role before the
    # gateway exists, so cleanup must be able to tear those down on their own.
    gateway_id = config.get("gateway_id")
    # Never trust the state file for the target list. deploy.py records target_id
    # only after the target reaches READY, so its own documented failure -- the
    # target that never gets there, e.g. a missing warehouse grant -- leaves a
    # real target in AWS with "target_id": null on disk. Skipping the delete in
    # that case left the gateway permanently undeletable, because DeleteGateway
    # refuses while any target is still attached. Ask AWS what is actually there
    # and fall back to the state file only if that call fails.
    recorded_id = config.get("target_id")

    agentcore_preview = boto3.client("bedrock-agentcore-control", region_name=config["region"])
    target_ids = _discover_target_ids(agentcore_preview, gateway_id, recorded_id) if gateway_id else []

    print("This will delete:")
    for tid in target_ids:
        print(f"  Gateway target        {tid}")
    print(f"  Credential provider   {CREDENTIAL_PROVIDER_NAME}")
    if gateway_id:
        print(f"  Gateway               {gateway_id}")
    if config.get("role_arn"):
        print(f"  IAM role              {config['role_arn'].split('/')[-1]}")
    if (config.get("client_info") or {}).get("user_pool_id"):
        print(f"  Cognito user pool     {config['client_info']['user_pool_id']}")
    if not args.yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        raise SystemExit("Aborted.")

    agentcore = agentcore_preview
    # Teardown is best effort: each delete below catches broadly on purpose so that
    # one failure cannot abandon the resources after it. Failures are collected and
    # the state file is kept, so the command can be re-run to finish the job. That is
    # why BLE001 is suppressed rather than narrowing to botocore exception types --
    # doing so would let, say, an AttributeError from an older boto3 abort the drain.
    failures = []

    for tid in target_ids:
        try:
            agentcore.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=tid)
            print(f"Deleted gateway target {tid}.")
        except Exception as exc:  # noqa: BLE001
            print(f"Could not delete gateway target {tid}: {exc}")
            failures.append(f"gateway target {tid}")

    # Target deletion is asynchronous, so drain BEFORE touching the credential
    # provider: while a target still exists it holds a credentialProviderConfigurations
    # reference to that provider, and the delete fails with a conflict.
    print("Waiting for targets to detach...")
    drained = not gateway_id
    for _ in range(30):
        try:
            remaining = agentcore.list_gateway_targets(gatewayIdentifier=gateway_id).get("items", [])
        except Exception as exc:  # noqa: BLE001
            # Do not treat an API error as "drained" -- that raced ahead to
            # DeleteGateway and failed on a dependency. Keep waiting instead.
            print(f"  could not list targets ({exc}); retrying")
            time.sleep(5)
            continue
        if not remaining:
            drained = True
            break
        time.sleep(5)
    if not drained:
        print("  targets still attached after waiting; gateway delete may fail")

    try:
        agentcore.delete_oauth2_credential_provider(name=CREDENTIAL_PROVIDER_NAME)
        print("Deleted credential provider.")
    except Exception as exc:  # noqa: BLE001
        print(f"Could not delete credential provider: {exc}")
        failures.append("credential provider")

    if gateway_id:
        try:
            agentcore.delete_gateway(gatewayIdentifier=gateway_id)
            print("Deleted gateway.")
        except Exception as exc:  # noqa: BLE001
            print(f"Could not delete gateway: {exc}")
            failures.append("gateway")

    # The gateway execution role and the Cognito user pool are created by
    # deploy.py, so remove them here too.
    role_arn = config.get("role_arn", "")
    if role_arn:
        role_name = role_arn.split("/")[-1]
        iam = boto3.client("iam")
        # Separate calls on purpose. A deploy that aborted before step 4 never
        # attached the inline policy, so delete_role_policy raises NoSuchEntity --
        # and sharing one try block meant delete_role never ran, orphaning the role
        # and making every cleanup re-run fail identically forever.
        try:
            iam.delete_role_policy(RoleName=role_name, PolicyName=IAM_POLICY_NAME)
        except iam.exceptions.NoSuchEntityException:
            pass  # never attached, or already gone
        except Exception as exc:  # noqa: BLE001
            print(f"Could not delete inline policy on {role_name}: {exc}")
        try:
            iam.delete_role(RoleName=role_name)
            print(f"Deleted IAM role {role_name}.")
        except iam.exceptions.NoSuchEntityException:
            print(f"IAM role {role_name} already gone.")
        except Exception as exc:  # noqa: BLE001
            print(f"Could not delete IAM role {role_name}: {exc}")
            failures.append("IAM role")

    client_info = config.get("client_info") or {}
    pool_id = client_info.get("user_pool_id", "")
    if pool_id:
        cognito = boto3.client("cognito-idp", region_name=config["region"])
        # The hosted-UI domain must go before the pool, otherwise DeleteUserPool
        # fails with "It has a domain configured that should be deleted first".
        domain = client_info.get("domain", "")
        if domain:
            try:
                cognito.delete_user_pool_domain(Domain=domain, UserPoolId=pool_id)
                print(f"Deleted Cognito domain {domain}.")
                time.sleep(10)
            except Exception as exc:  # noqa: BLE001
                print(f"Could not delete Cognito domain {domain}: {exc}")
        try:
            cognito.delete_user_pool(UserPoolId=pool_id)
            print(f"Deleted Cognito user pool {pool_id}.")
        except Exception as exc:  # noqa: BLE001
            print(f"Could not delete Cognito user pool {pool_id}: {exc}")
            failures.append("Cognito user pool")

    if failures:
        # Keep the state file so the command can be re-run to finish the job.
        print(f"\nLeft {STATE_FILE} in place — re-run `python cleanup.py` to retry: " + ", ".join(failures))
    else:
        os.remove(STATE_FILE)
        print(f"Removed {STATE_FILE}")

    print("\nRun `agentcore destroy` to remove the deployed Runtime agent.")


if __name__ == "__main__":
    main()
