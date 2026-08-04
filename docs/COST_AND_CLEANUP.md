# Cost and Cleanup

What this solution costs to run, and how to tear it all down. Figures are order-of-magnitude for a
low-volume demo in `<REGION>` — confirm against the AWS Pricing Calculator for your own region/usage.

---

## Cost

This solution is built almost entirely on serverless, usage-based components, so the idle
cost is near zero — you pay mainly while forecasting and while the flow runs.

**AWS side (account, <REGION>):**
| Component | Pricing model | Demo-scale cost |
|---|---|---|
| API Gateway (HTTP API) | per request | < $0.01 — a handful of order/ticket calls per run |
| AWS Lambda | per request + GB-s | effectively $0 (well within free tier; 256 MB, <1s) |
| DynamoDB (2 tables, on-demand) | per request + storage | < $0.01 — tens of small items |
| Secrets Manager (1 secret) | $0.40 / secret / month + API calls | ~$0.40 / month |
| Amazon S3 Tables (Iceberg) | storage + requests + compaction | cents — 63,861 small rows |
| **AWS subtotal** | | **~$0.50 / month at rest**, plus negligible per-run |

**Databricks side:**
| Component | Pricing model | Notes |
|---|---|---|
| Serverless SQL Warehouse (Genie) | per-second DBU while querying; auto-stops | the main variable cost; scales to zero when idle |
| Serverless GPU (MMF / Chronos-2) | per-second while training/scoring | one-time/periodic forecast generation, not per-flow-run |
| Delta storage (catalog tables) | S3 storage | minimal |

**Amazon Quick:** per-user subscription (Author/Author Pro). Flows, connectors, and schedules
are included in the subscription — no separate per-run charge.

**Bottom line:** at rest the demo costs roughly the price of one Secrets Manager secret (~$0.40/mo)
plus Databricks/S3 storage; the meaningful spend is the SQL warehouse while Genie answers questions
and the GPU while you (re)generate forecasts. Both are serverless and scale to zero when idle.

> Tip: the Databricks Serverless SQL Warehouse auto-stop and the per-second GPU billing are what
> keep this cheap. Don't run forecasting on an always-on cluster for a demo.

---

## Cleanup

Remove everything in a few steps to avoid ongoing charges.

**1. Delete the AWS order/ticket stack (one command):**
```bash
aws cloudformation delete-stack --stack-name supplier-order-api --region <REGION>
aws cloudformation wait stack-delete-complete --stack-name supplier-order-api --region <REGION>
```
This removes the API Gateway, Lambda, both DynamoDB tables, the IAM role, and the Secrets Manager
secret. (If deletion protection or a retain policy is ever added, disable it first — none is set here.)

**2. Remove the S3 Tables supplier data:**
```bash
# drop the table (PyIceberg loader uses purge; or via your S3 Tables client)
aws s3tables delete-table --table-bucket-arn <BUCKET_ARN> --namespace supply_chain --name supplier_availability --region <REGION>
# optionally delete the namespace / table bucket if created only for this demo
```

**3. Amazon Quick:** pause or delete the schedule (Run mode → scheduling icon → delete), then
delete the Flow, the OpenAPI/order connector, the Genie MCP connector, and the S3 Tables dataset
if they were created only for this demo.

**4. Databricks:** delete the Genie space if demo-only; drop the demo catalog/schema
(`mmf.fresh_retail_net`, dims, views) if not reused; stop/delete the Serverless SQL Warehouse.
Serverless compute auto-stops, but deleting removes it from the workspace.

**5. Credentials:** rotate or delete the OAuth app used for the Quick→Genie connector and the
demo API key once the stack is gone.

> Verify nothing lingers: `aws cloudformation describe-stacks --stack-name supplier-order-api`
> should return a "does not exist" error after cleanup.
