"""Delete every AWS resource this sample created.

Ownership and state contract
----------------------------
`gateway_config.json` is the record of what this sample created, in this account and
region. Both deploy.py and cleanup.py hold to the following, and the rules exist because
each one corresponds to a way teardown previously stranded a billable resource:

1. An identifier is written to the state file as soon as the creating call returns --
   never after a later wait or readiness check, which can fail.
2. Each adopted-or-created resource records whether this sample created it (`owned_pool`).
   Cleanup deletes only what it owns; a pre-existing resource it merely reused is left.
3. deploy.py refuses to start when the state file already records a gateway. There is no
   reuse path for a gateway or a target, so a re-run cannot succeed, and starting one
   would overwrite the record of a live deployment.
4. Cleanup derives the live target list from AWS. The state file is a fallback used only
   when that call fails, because a state file that never recorded a target must not be
   able to hide a real one.
5. Deletion follows dependency order: targets, drain, credential provider, gateway, IAM
   role, Cognito pool.
6. Cleanup only deletes a credential provider the state file actually records. The name is
   shared across deployments, so deleting by bare name can destroy another one.
7. Cleanup is idempotent. A resource that is already gone is a success, not a failure. Only
   a genuine error keeps the state file, so a re-run can converge instead of looping.

Run `agentcore destroy` separately to remove the deployed Runtime agent.

Usage:
    python cleanup.py
    python cleanup.py --yes    # skip the confirmation prompt
"""

import argparse
import json
import os
import time

import boto3
from config import IAM_POLICY_NAME, STATE_FILE

# Errors that mean "already gone", which rule 7 treats as success.
_GONE = ("ResourceNotFoundException", "NoSuchEntity", "ResourceNotFound")


def _already_gone(exc: Exception) -> bool:
    name = type(exc).__name__
    return any(m in name for m in _GONE) or any(m in str(exc) for m in _GONE)


def _discover_target_ids(agentcore, gateway_id: str, recorded_id) -> list:
    """Return the targets attached to the gateway, per contract rule 4.

    On a successful list, return exactly what AWS reports -- do NOT re-add a recorded
    id that AWS no longer knows about, or a partially-completed cleanup would retry a
    deleted target forever and never converge.
    """
    try:
        items = agentcore.list_gateway_targets(gatewayIdentifier=gateway_id).get("items", [])
        return [t["targetId"] for t in items if t.get("targetId")]
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
        raise SystemExit(f"{STATE_FILE} not found — nothing to clean up.") from None

    # May be absent: deploy.py records the Cognito pool and IAM role before the gateway
    # exists, so cleanup must be able to tear those down on their own (contract rule 1).
    gateway_id = config.get("gateway_id")
    provider_arn = config.get("provider_arn")
    client_info = config.get("client_info") or {}
    pool_id = client_info.get("user_pool_id") or ""
    # Absent in state files written before this field existed; assume ownership then,
    # which matches the old behaviour of always creating the pool.
    owns_pool = client_info.get("owned_pool", True)

    agentcore = boto3.client("bedrock-agentcore-control", region_name=config["region"])
    target_ids = _discover_target_ids(agentcore, gateway_id, config.get("target_id")) if gateway_id else []

    print("This will delete:")
    for tid in target_ids:
        print(f"  Gateway target        {tid}")
    if provider_arn:
        print(f"  Credential provider   {provider_arn.rsplit('/', 1)[-1]}")
    if gateway_id:
        print(f"  Gateway               {gateway_id}")
    if config.get("role_arn"):
        print(f"  IAM role              {config['role_arn'].split('/')[-1]}")
    if pool_id and owns_pool:
        print(f"  Cognito user pool     {pool_id}")
    elif pool_id:
        print(f"  Cognito user pool     {pool_id}  (adopted, will NOT be deleted)")
    if not args.yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        raise SystemExit("Aborted.")

    # Teardown is best effort: each delete catches broadly on purpose so one failure
    # cannot abandon the resources after it. Failures are collected and the state file is
    # kept, so the command can be re-run to finish the job. That is why BLE001 is
    # suppressed rather than narrowing to botocore exception types -- doing so would let,
    # say, an AttributeError from an older boto3 abort the drain.
    failures = []

    for tid in target_ids:
        try:
            agentcore.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=tid)
            print(f"Deleted gateway target {tid}.")
        except Exception as exc:  # noqa: BLE001
            if _already_gone(exc):
                print(f"Gateway target {tid} already gone.")
            else:
                print(f"Could not delete gateway target {tid}: {exc}")
                failures.append(f"gateway target {tid}")

    # Target deletion is asynchronous, so drain BEFORE touching the credential provider:
    # while a target still exists it holds a credentialProviderConfigurations reference to
    # that provider, and the delete fails with a conflict (contract rule 5).
    if gateway_id and target_ids:
        print("Waiting for targets to detach...")
        drained = False
        for _ in range(30):
            try:
                remaining = agentcore.list_gateway_targets(gatewayIdentifier=gateway_id).get("items", [])
            except Exception as exc:  # noqa: BLE001
                # An API error is not "drained" -- racing ahead to DeleteGateway would
                # fail on the still-attached dependency.
                print(f"  could not list targets ({exc}); retrying")
                time.sleep(5)
                continue
            if not remaining:
                drained = True
                break
            time.sleep(5)
        if not drained:
            print("  targets still attached after waiting; gateway delete may fail")

    # Contract rule 6: only delete a provider this state file records.
    if provider_arn:
        try:
            agentcore.delete_oauth2_credential_provider(name=provider_arn.rsplit("/", 1)[-1])
            print("Deleted credential provider.")
        except Exception as exc:  # noqa: BLE001
            if _already_gone(exc):
                print("Credential provider already gone.")
            else:
                print(f"Could not delete credential provider: {exc}")
                failures.append("credential provider")
    else:
        print("No credential provider recorded in state; skipping (it may belong to another deployment).")

    if gateway_id:
        try:
            agentcore.delete_gateway(gatewayIdentifier=gateway_id)
            print("Deleted gateway.")
        except Exception as exc:  # noqa: BLE001
            if _already_gone(exc):
                print("Gateway already gone.")
            else:
                print(f"Could not delete gateway: {exc}")
                failures.append("gateway")

    role_arn = config.get("role_arn") or ""
    if role_arn:
        role_name = role_arn.split("/")[-1]
        iam = boto3.client("iam")
        # Separate calls on purpose. A deploy that aborted before the grant step never
        # attached the inline policy, so delete_role_policy raises NoSuchEntity -- and
        # sharing one try block meant delete_role never ran, orphaning the role and making
        # every cleanup re-run fail identically forever.
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

    # Contract rule 2: never delete a pool this sample only adopted.
    if pool_id and not owns_pool:
        print(f"Leaving Cognito user pool {pool_id} in place — this sample did not create it.")
    elif pool_id:
        cognito = boto3.client("cognito-idp", region_name=config["region"])
        # The hosted-UI domain must go before the pool, otherwise DeleteUserPool fails
        # with "It has a domain configured that should be deleted first".
        domain = client_info.get("domain") or ""
        if domain:
            try:
                cognito.delete_user_pool_domain(Domain=domain, UserPoolId=pool_id)
                print(f"Deleted Cognito domain {domain}.")
                time.sleep(10)
            except Exception as exc:  # noqa: BLE001
                if _already_gone(exc):
                    print(f"Cognito domain {domain} already gone.")
                else:
                    print(f"Could not delete Cognito domain {domain}: {exc}")
        try:
            cognito.delete_user_pool(UserPoolId=pool_id)
            print(f"Deleted Cognito user pool {pool_id}.")
        except Exception as exc:  # noqa: BLE001
            if _already_gone(exc):
                print(f"Cognito user pool {pool_id} already gone.")
            else:
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
