"""Stage the Databricks OAuth M2M secret in AWS Secrets Manager (optional, production path).

By default deploy.py passes the plaintext DATABRICKS_CLIENT_SECRET straight into the
credential provider, and AgentCore stores it in a secret it creates and owns. That is fine
for a walkthrough, but it means the plaintext lives in your .env / shell.

The production-shaped pattern is to keep the secret in *your* Secrets Manager and have the
credential provider reference it by ARN. This script provisions that secret. After running
it you point deploy.py at the ARN and never put the plaintext in .env again:

    export DATABRICKS_CLIENT_SECRET="<OAuth M2M secret>"   # needed once, to seed the secret
    python secrets_setup.py                                # creates the SM secret, prints the ARN
    export DATABRICKS_SECRET_ARN="arn:aws:secretsmanager:...:secret:...-abcde"
    unset DATABRICKS_CLIENT_SECRET                         # plaintext no longer needed by deploy.py
    python deploy.py                                       # registers the provider as EXTERNAL

deploy.py then registers the credential provider with clientSecretSource="EXTERNAL" and
scopes the gateway role's secretsmanager:GetSecretValue to exactly this ARN.

The secret is a JSON document so it can hold more than one field later; DATABRICKS_SECRET_JSON_KEY
(default "client_secret") names the key that holds the value.

Two principals read this secret, and AgentCore makes both reads on a principal's behalf rather
than as itself: the principal running deploy.py reads it at deploy time, and the gateway
execution role reads it when the cached Databricks token expires (~1 hour). See "Who reads the
secret" in README.md. The secret is expected to live in the same account as the gateway, so no
Secrets Manager resource policy is needed. With the account's default Secrets Manager
encryption there is nothing further to grant; if you encrypt it with a customer-managed KMS key
instead, BOTH principals above need kms:Decrypt on that key.

Usage:
    python secrets_setup.py               # create or update the secret, print its ARN
    python secrets_setup.py --show-arn    # just print the ARN of the existing secret
    python secrets_setup.py --delete      # schedule deletion of a secret THIS script created
    python secrets_setup.py --delete --force --yes   # delete immediately, no prompt, no recovery

Requires:
    DATABRICKS_CLIENT_SECRET   (the value to store; not needed for --show-arn / --delete)
Optional:
    DATABRICKS_SECRET_NAME       (default: databricks-genie-agentcore/oauth-client-secret)
    DATABRICKS_SECRET_JSON_KEY   (default: client_secret)
    AWS_REGION                   (default: us-east-1)
"""

import argparse
import json
import os

import boto3
from config import (
    AWS_REGION,
    DATABRICKS_CLIENT_SECRET,
    DATABRICKS_SECRET_JSON_KEY,
    DATABRICKS_SECRET_NAME,
    STATE_FILE,
)

# secrets_setup.py owns this file; it records whether THIS script created the secret so
# --delete never removes a secret it merely referenced. Independent of gateway_config.json.
SECRET_STATE_FILE = os.path.join(os.path.dirname(STATE_FILE), "secret_state.json")


def build_secret_string(client_secret: str, json_key: str) -> str:
    """The JSON document stored in Secrets Manager: one key holding the OAuth secret."""
    return json.dumps({json_key: client_secret})


def read_secret_state() -> dict:
    try:
        with open(SECRET_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_secret_state(state: dict) -> None:
    with open(SECRET_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def clear_secret_state() -> None:
    try:
        os.remove(SECRET_STATE_FILE)
    except FileNotFoundError:
        pass


def confirm(action: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    if input(f"{action} [y/N] ").strip().lower() not in ("y", "yes"):
        raise SystemExit("Aborted.")


def describe_secret(client, name: str) -> dict | None:
    """Return the describe_secret response for this name, or None if it does not exist."""
    try:
        return client.describe_secret(SecretId=name)
    except client.exceptions.ResourceNotFoundException:
        return None


def find_secret_arn(client, name: str) -> str | None:
    """Return the ARN of the secret with this name, or None if it does not exist."""
    desc = describe_secret(client, name)
    return desc["ARN"] if desc else None


def existing_document(client, name: str) -> dict:
    """The current secret value as a dict, so provision() can MERGE rather than clobber.

    A non-JSON or non-object value is ambiguous to merge into, so refuse rather than
    overwrite a secret we may not own.
    """
    current = client.get_secret_value(SecretId=name).get("SecretString")
    if not current:
        return {}
    try:
        document = json.loads(current)
    except json.JSONDecodeError:
        raise SystemExit(
            f"Secret {name} exists but its value is not JSON. secrets_setup.py stores a JSON "
            "document and will not overwrite a plain-string secret you may rely on elsewhere. "
            "Point DATABRICKS_SECRET_NAME at a different name, or convert the secret yourself."
        )
    if not isinstance(document, dict):
        raise SystemExit(
            f"Secret {name} exists but its JSON value is not an object; refusing to overwrite it. "
            "Point DATABRICKS_SECRET_NAME at a different name."
        )
    return document


def provision(client, name: str, json_key: str) -> str:
    """Create the secret, or merge our key into it if it already exists. Return the ARN."""
    if not DATABRICKS_CLIENT_SECRET:
        raise SystemExit(
            "DATABRICKS_CLIENT_SECRET is not set. Set it to the Databricks OAuth M2M secret "
            "so this script can store it in Secrets Manager, then re-run."
        )

    desc = describe_secret(client, name)
    if desc:
        if desc.get("DeletedDate"):
            # describe_secret still resolves a secret inside its deletion recovery window, so
            # the adopt branch below would take it and put_secret_value would raise. Say what
            # to do instead of crashing -- this is the README's own --delete then re-run path.
            raise SystemExit(
                f"Secret {name} is scheduled for deletion (in its recovery window). Restore it "
                f"before re-running:\n  aws secretsmanager restore-secret --secret-id {name}\n"
                "or wait for deletion to complete, then create a fresh one."
            )
        existing_arn = desc["ARN"]
        # Adopt in place by MERGING our key into the existing document, so any sibling keys on
        # a secret we did not create survive (the module docstring promises this). Preserve a
        # prior created=True so a create-then-update sequence still lets us --delete it.
        prior = read_secret_state()
        created = bool(prior.get("created")) and prior.get("secret_arn") == existing_arn
        if not created:
            print(f"  Adopting existing secret {name} (this script did not create it and will not --delete it)")
        document = existing_document(client, name)
        others = len(document) - (1 if json_key in document else 0)
        document[json_key] = DATABRICKS_CLIENT_SECRET
        client.put_secret_value(SecretId=name, SecretString=json.dumps(document))
        arn = existing_arn
        print(f"  Updated secret {name}: set key '{json_key}', preserved {others} other field(s)")
    else:
        resp = client.create_secret(
            Name=name,
            Description="Databricks Genie OAuth M2M client secret for the AgentCore gateway",
            SecretString=build_secret_string(DATABRICKS_CLIENT_SECRET, json_key),
        )
        arn = resp["ARN"]
        created = True
        print(f"  Created secret {name}")

    write_secret_state({"secret_arn": arn, "name": name, "created": created, "json_key": json_key})
    return arn


def delete(client, name: str, force: bool, assume_yes: bool) -> None:
    """Delete the secret ONLY if this script recorded creating it."""
    state = read_secret_state()
    arn = find_secret_arn(client, name)
    if not arn:
        print(f"Nothing to delete: secret {name} does not exist.")
        # Only clear the ownership record if it actually refers to THIS name. Running
        # --delete with a different DATABRICKS_SECRET_NAME must not wipe the record for
        # the secret the script really created, leaving it un-deletable by the script.
        if state.get("name") == name:
            clear_secret_state()
        return
    if not (state.get("created") and state.get("secret_arn") == arn):
        raise SystemExit(
            f"Refusing to delete {name}: no record that this script created it, so it may be a "
            "secret you rely on elsewhere. If you are sure, delete it manually:\n"
            f"  aws secretsmanager delete-secret --secret-id {name}"
        )
    how = "immediately (no recovery window)" if force else "with the default 30-day recovery window"
    print(f"--delete will delete secret {name} {how}.")
    confirm("Proceed?", assume_yes)
    kwargs = {"SecretId": name}
    if force:
        kwargs["ForceDeleteWithoutRecovery"] = True
    else:
        kwargs["RecoveryWindowInDays"] = 30
    client.delete_secret(**kwargs)
    clear_secret_state()
    print(f"  Deleted {name}.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--show-arn", action="store_true", help="print the existing secret's ARN and exit")
    parser.add_argument("--delete", action="store_true", help="delete a secret this script created")
    parser.add_argument(
        "--force", action="store_true", help="with --delete, delete immediately without a recovery window"
    )
    parser.add_argument("--yes", action="store_true", help="skip confirmation prompts")
    args = parser.parse_args()

    name = DATABRICKS_SECRET_NAME
    client = boto3.client("secretsmanager", region_name=AWS_REGION)

    if args.show_arn:
        arn = find_secret_arn(client, name)
        if not arn:
            raise SystemExit(f"Secret {name} does not exist. Run `python secrets_setup.py` to create it.")
        print(arn)
        return

    if args.delete:
        delete(client, name, args.force, args.yes)
        return

    arn = provision(client, name, DATABRICKS_SECRET_JSON_KEY)
    print("\nDone. Point deploy.py at the secret instead of the plaintext env var:\n")
    print(f'  export DATABRICKS_SECRET_ARN="{arn}"')
    print("  unset DATABRICKS_CLIENT_SECRET   # deploy.py no longer needs the plaintext")
    print("  python deploy.py")


if __name__ == "__main__":
    main()
