# Amazon Quick — Account Setup Runbook (Step 0 for all Quick work)

Before any of the other Quick runbooks (Genie connector, S3 Tables data source, Order/Ticket
actions, the Flow), you need an **Amazon Quick account provisioned in your chosen `<REGION>`**. This
runbook covers that one-time setup. It is **console-only** — adapt the exact labels to what your
console shows; the values below are what matters.

## Why this matters
- The whole solution lives in **one region** (`<REGION>`) — S3 Tables, the Order API stack, and
  Amazon Quick must all be in it. Amazon Quick's **home region is chosen once at sign-up and cannot
  be changed afterward**, so pick the region that supports Amazon S3 Tables *and* Amazon Quick before
  you subscribe. (`us-west-2` is a known-good choice.)
- **Flows and third-party connectors (MCP / OpenAPI custom actions) require the Author or Author Pro
  role.** A Reader-only account cannot build the automation in this repo.

## STEP 1 — Subscribe to Amazon Quick
1. Sign in to the AWS console with an account/role that can create an Amazon Quick subscription, and
   switch the console to your chosen **`<REGION>`**.
2. Open **Amazon Quick** and choose **Sign up for Amazon Quick** (first-time) — or open your existing
   account's admin if you already have one.
3. Choose the **edition/role that includes Author (or Author Pro)** — required for connectors and Flows.
4. Set the **home/notification region to `<REGION>`** during sign-up. Confirm it matches the region
   where you will create the S3 Tables bucket and deploy the Order API stack.
5. Complete sign-up (account name, notification email, IAM role Quick will use for AWS resource access).

## STEP 2 — Confirm region and edition
- In **Manage account** (admin), verify the account **region = `<REGION>`** and the **edition
  includes Author/Author Pro**. If the region is wrong, you must create a new subscription in the
  correct region (region is fixed post-signup).

## STEP 3 — Set up the querying user
- Ensure the user who will build/run the assistant and Flow has an **Author (or Author Pro)** role in
  Quick (Manage users / Manage account → Users).
- This same person will complete the Databricks OAuth login when the Genie connector is first used
  (see `QUICK_DATABRICKS_CONNECTOR_RUNBOOK.md` — that user needs the Databricks grants listed there).

## Next
With Quick provisioned in `<REGION>`, proceed to the component runbooks in this order:
1. `QUICK_DATABRICKS_CONNECTOR_RUNBOOK.md` — connect Genie (forecasts) over MCP.
2. `QUICK_S3TABLES_RUNBOOK.md` — connect the supplier feed (S3 Tables Direct Query).
3. `QUICK_ACTION_TICKET_RUNBOOK.md` — deploy the Order API + add the order/ticket custom actions.
4. `../flow/flow_build_guide.md` — build and schedule the autonomous Flow.
