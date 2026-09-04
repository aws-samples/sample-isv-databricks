# Amazon Quick — Order + Ticket Action Connector — Setup Runbook

The final console steps: let the Quick assistant (1) submit purchase orders to the supplier via the
Order API for routine surges, and (2) raise a human-review ticket via the Ticket API for
exceptions/approvals. Both endpoints live on the same Supplier Order API stack.

> Prerequisite for the Quick action steps (A/B onward): an Amazon Quick account provisioned in
> `<REGION>` — see `QUICK_ACCOUNT_SETUP_RUNBOOK.md`. (STEP 0 below is AWS-side and needs only the CLI.)

## STEP 0 — Deploy the Supplier Order API stack (CloudFormation)
The template `order-api/supplier-order-api.yaml` provisions API Gateway → Lambda → two DynamoDB
tables → a Secrets Manager secret. It creates a **named IAM role**, so you MUST pass
`CAPABILITY_NAMED_IAM`:

```bash
aws cloudformation deploy \
  --template-file order-api/supplier-order-api.yaml \
  --stack-name supplier-order-api \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides ApiKeyValue="$(openssl rand -hex 24)" \
  --region <REGION>
```

Then read the deployed base URL (the `<API_ID>` you substitute in the OpenAPI spec):

```bash
aws cloudformation describe-stacks --stack-name supplier-order-api --region <REGION> \
  --query "Stacks[0].Outputs" --output table
```

Notes:
- The stack uses fixed resource names (role, functions, tables, secret `supplier-order-api/api-key`).
  You can deploy only **one** copy per account+region. If you delete and redeploy within 7 days, the
  Secrets Manager secret name is still in its recovery window — either wait, or
  `aws secretsmanager delete-secret --secret-id supplier-order-api/api-key --force-delete-without-recovery`.
- `ApiKeyValue` is **required** (no default) — the deploy command above generates a strong random
  value with `openssl rand -hex 24`. The Lambda's `x-api-key` check is optional by default
  (`RequireApiKey=false`), so auth "None" works in Quick; deploy with `RequireApiKey=true` to reject
  requests that omit the key. Either way the key is stored (KMS-encrypted) in Secrets Manager.
- The stack also creates a customer-managed **KMS key** (`alias/supplier-order-api`) that encrypts the
  Secrets Manager secret and both DynamoDB tables at rest; the tables have point-in-time recovery
  enabled. Deleting the stack schedules the key for deletion (7–30 day pending window).

## Deployed resources (<REGION>)
- Supplier Order API base URL: `https://<API_ID>.execute-api.<REGION>.amazonaws.com/prod`
  - `POST /orders`       — single order (the operation the Quick Flows connector uses)
  - `POST /orders/bulk`  — multiple orders (exists on the stack, but NOT wired into Quick — the Flows
    connector uses single-object operations only, since Quick rejects array request bodies)
  - Auth header: `x-api-key: <value from Secrets Manager>`
- **API key is stored in AWS Secrets Manager** — secret `supplier-order-api/api-key` (JSON key `api_key`).
  The Lambda reads it from the secret at runtime; it is NOT a plaintext Lambda env var.
  Retrieve the current value to paste into Quick:
  ```
  aws secretsmanager get-secret-value --secret-id supplier-order-api/api-key \
    --region <REGION> --query SecretString --output text
  ```
- OpenAPI spec file: `supplier-order-api-openapi-flows.json` (JSON; the Flows-compatible spec — Quick
  requires JSON and rejects array types, so this spec exposes `submitOrder` and `createTicket` only).
- Order behavior: `order_type=routine` → status SUBMITTED (auto); `order_type=exception` → PENDING_APPROVAL.

---

## STEP A — Add the Order API as a custom action in Amazon Quick

Quick console → **Connectors / Actions** → **Create** → choose the **custom API / OpenAPI** action type
(in Quick this is the "OpenAPI" or "API action" connector — the non-MCP custom action).

| Field | Value |
|---|---|
| Name | `Supplier Order API` |
| Description | `Submit purchase orders to the supplier (routine auto-submit; exception to approval)` |
| OpenAPI definition | upload/paste `supplier-order-api-openapi-flows.json` (server URL already baked in) |
| Authentication | **API key** (or **None** for the demo — the key check is optional unless the stack is deployed with `RequireApiKey=true`) |
| Header name | `x-api-key` |
| Header value | (retrieve from Secrets Manager — see command above) |

→ Save. Quick reads the spec and exposes two operations: `submitOrder` (POST /orders) and
`createTicket` (POST /tickets).

> If Quick asks for the endpoint instead of an OpenAPI file, use the base URL above and define the
> two POST operations (`/orders`, `/tickets`) with the `x-api-key` header.

### Test the action in Quick Chat
```
Submit a routine purchase order to supplier SUP_S2_00 for retailer product 122 (Skim Milk),
quantity 50, unit cost 4.50, city 0, region Northeast.
```
Expect: an order confirmation with an ORD-... id and status SUBMITTED. Verify in DynamoDB table
`supplier-order-api-orders` if needed.

---

## STEP B — Add the Ticket API as a custom action (exception / human-review path)

The exception path uses a REST ticket endpoint on the SAME Order API stack (`POST /tickets`) — no
external ticketing system is needed. Add it as a custom action the same way as Step A — it's already in
the OpenAPI spec (operation `createTicket`). If you imported the whole `supplier-order-api-openapi-flows.json`,
`createTicket` is already available; otherwise add a second action pointing at:
- Endpoint: `https://<API_ID>.execute-api.<REGION>.amazonaws.com/prod/tickets`
- Auth: `x-api-key` (same key from Secrets Manager)

### Test
```
Raise a ticket: subject "Manual review: no supplier can cover Skim Milk surge",
description "Forecast ~50 units in the Northeast; no single supplier covers within lead time",
product Skim Milk, region Northeast, priority High.
```
Expect a TKT-... id with status OPEN. Verify in DynamoDB table `supplier-order-api-tickets` if needed.

---

## STEP C — The full autonomous-loop prompt (the demo finale)

With all sources + actions connected (Genie MCP, S3 Tables dataset, Order API action, Ticket API action), run:

```
Skim Milk demand is surging in the Northeast for June 26 to July 2, 2024. Use the Databricks forecast
for the total demand, find the suppliers for that product (match on retailer_product_id), pick the
cheapest supplier that can cover it within lead time, and submit a routine purchase order for that
quantity to the chosen supplier. If no single supplier can cover it, instead raise a ticket for
manual review.
```

Expected: forecast ~50 units (Genie) → S2 chosen (S3 Tables) → routine order SUBMITTED via the Order
API. For an SKU where no supplier covers the demand, it raises a ticket (status OPEN) via the Ticket API instead.

### Tiering rule (routine vs exception) — phrase it in the prompt or an assistant instruction
- **Routine (auto-submit, POST /orders):** a single supplier can cover the forecast within its lead time.
- **Exception (raise ticket, POST /tickets):** no single supplier can cover it (needs splitting / manual
  decision), OR quantity/value exceeds an agreed threshold.

---

## Notes / honesty
- **Auth:** API key is realistic for an external supplier API. The key lives in **AWS Secrets Manager**
  (`supplier-order-api/api-key`), not in the template/env/runbook as plaintext. Do NOT put the value in the
  published blog. **Hardening (recommended):** deploy the stack with a placeholder `ApiKeyValue`, then set the
  real key directly in Secrets Manager so it never appears in CloudFormation parameters or CLI history:
  ```
  aws secretsmanager put-secret-value --secret-id supplier-order-api/api-key \
    --secret-string '{"api_key":"<new-strong-key>"}' --region <REGION>
  ```
  (The Lambda caches the key per container; force a fresh read by updating the function or waiting for cold start.)
- **Teardown:** `aws cloudformation delete-stack --stack-name supplier-order-api --region <REGION>`.
- **What I can't pre-verify:** the exact label/flow of Quick's custom-API-action connector UI (MCP vs
  OpenAPI vs generic HTTP) — adapt field names to what your console shows; the values above are what matters.
- **Cross-tool orchestration** (query Genie + S3 Tables, then call the Order API or Ticket API in one
  turn) is the agent's reasoning; if it doesn't chain automatically, drive it with the explicit prompt in STEP C.
