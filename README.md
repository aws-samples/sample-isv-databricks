# Autonomous Supply Chain — Databricks Genie Agents + Amazon Quick + Amazon S3 Tables

Companion code for the autonomous supply chain solution described in two posts:
- **AWS Machine Learning Blog** (technical how-to): *"Autonomous Supply Chain with Chronos-2 via MMF,
  Databricks Genie Agents, and Amazon Quick"*
- **AWS Partner Network (APN) Blog** (business framing): *"Closing the Last Mile: Turning Demand
  Forecasts into Autonomous Action on AWS and Databricks"*

It demonstrates a closed detect → decide → act loop:

1. **Forecast** — Databricks Many Model Forecasting (MMF) serves Amazon Chronos-2 to generate demand
   forecasts over the FreshRetailNet-50K dataset.
2. **Detect** — a Databricks Genie Agent surfaces demand surges in plain English (a SKU whose next
   7-day average forecast is ≥ 1.5× its prior 14-day average sales).
3. **Decide** — Amazon Quick reconciles each surging SKU against live supplier availability in
   Amazon S3 Tables and picks the best-fit supplier that can cover the forecast.
4. **Act** — Amazon Quick Flows submits a routine purchase order, or raises a human-review ticket
   for exceptions, through a Supplier Order API. On a schedule, the loop runs unattended.

> **Forecasting notebooks live in the official Databricks accelerator.** Notebooks 01–04 are
> maintained in the [Databricks Many Model Forecasting repo](https://github.com/databricks-industry-solutions/many-model-forecasting/tree/main/examples/fresh_retail_net)
> (the canonical source). Copies are included in this repo for convenience.

> **Note on data and names.** FreshRetailNet-50K is anonymized; product, location, and supplier
> names in this demo (e.g. "Skim Milk", "Boston/Northeast", "Supplier S2") are **illustrative
> synthetic labels** mapped to real dataset IDs. No third-party dataset is redistributed here.

## Architecture

```
Databricks (Chronos-2 / MMF)            Amazon S3 Tables (Iceberg)
   forecasts in Delta tables               supplier_availability feed
          │                                          │
   Databricks Genie Agent                            │
   (NL → SQL, surge detection)                       │
          │  MCP (OAuth/3LO)            native Direct Query
          └───────────────┬──────────────────────────┘
                          ▼
                    Amazon Quick  ── orchestrates, reconciles on retailer_product_id
                          │
                 Amazon Quick Flows (scheduled, reasoning group per SKU)
                          │  OpenAPI action
                          ▼
        Supplier Order API  (API Gateway → Lambda → DynamoDB)
        POST /orders (routine)   |   POST /tickets (exception)
```

The Genie Agent knows only forecasts; S3 Tables knows only suppliers. Amazon Quick is the only
component that touches both and reconciles them on a shared `retailer_product_id`, the answer to "why
not just do it all in Databricks?": the supplier feed is an external operational system.

## Repo layout
```
notebooks/        Databricks notebooks: data prep, MMF/Chronos-2 forecast, dimension tables, Genie views
  run_notebook.sh parameterized Databricks job runner (submit → poll → report; handles nb04 MODEL_FILTER)
  optional/       chronos2_covariate_showcase.ipynb — model comparison (honest framing)
supplier-feed/    load_supplier_availability.py — independent S3 Tables loader (PyIceberg)
order-api/        CloudFormation + OpenAPI spec for the mock external Supplier Order API
genie/            Genie space instructions + the pinned surge trusted query + deterministic surge SQL
flow/             flow_definition.json (create-flow input) + flow_build_guide.md (step-by-step build)
docs/             setup runbooks (Quick account, Genie space, Quick connector, S3 Tables, order/ticket action, end-to-end test) + cost/cleanup
cleanup/          cleanup.sh — tears down all AWS + Databricks resources created by the walkthrough
```

## Prerequisites
You run this in **your own** environment. Before starting you need:

> **Pick ONE AWS region that supports Amazon Quick's agentic features, and use it for everything.**
> Set it once as `<REGION>` and substitute it consistently. The region **must support Amazon Quick's
> agentic capabilities** — action connectors, spaces, and flows — which are available only in **US East
> (N. Virginia) `us-east-1`, US West (Oregon) `us-west-2`, Europe (Dublin) `eu-west-1`, and Asia
> Pacific (Sydney) `ap-southeast-2`**. `us-west-2` is a known-good choice. Note: some regions (for
> example `us-east-2`) offer Amazon S3 Tables and classic Quick BI but **not** the agentic Quick
> features this solution needs — the S3 Tables data source, connectors, and flows will fail there.
> The Amazon S3 Tables bucket that backs the supplier feed must be in the **same region as Quick**
> (Quick's S3 Tables data source is region-bound). The Order API and Databricks can be in any region —
> Quick reaches them over HTTPS.

- **AWS account** in your chosen `<REGION>` (S3 Tables + Amazon Quick), with permission to
  create S3 Tables buckets, and to deploy API Gateway, Lambda, DynamoDB, Secrets Manager, and IAM
  roles via CloudFormation.
- **Databricks workspace** with Unity Catalog, a **Serverless SQL Warehouse**, and **Serverless GPU**
  (A10) for the Chronos-2/MMF run in notebook 02. `CREATE CATALOG` on the metastore (or an admin to
  pre-create the `mmf` catalog).
- **Amazon Quick** (Author/Author Pro) in `<REGION>` — for the Genie MCP connector, S3 Tables data
  source, the Order API action, and Quick Flows. If you don't already have a Quick account, provision
  it first per **`docs/QUICK_ACCOUNT_SETUP_RUNBOOK.md`** (its home region is fixed at sign-up).
- **Local tools:** AWS CLI **v2.36.2+** (the Amazon Quick `create-agent`/`create-action-connector`/
  `create-flow` verbs require a recent release), the **Databricks CLI v0.299.0+**, `jq`, `uv` (or
  Python 3.11) for the supplier-feed loader, and network access to Hugging Face (the notebooks download
  the FreshRetailNet-50K dataset).

## Quickstart (build order)
Follow these steps in order. Notebooks run **01 → 02 → 04 → 03** (notebook 03's validation cell joins a
view that notebook 04 creates). Import the `.ipynb` files into your Databricks workspace and run each on
the compute noted in its first cell. Steps 8–13 use the AWS CLI (`quicksight` verbs) unless noted as
console-only; substitute your `<PLACEHOLDER>` values throughout.

1. **Provision Amazon Quick** — if you don't already have a Quick account, create it first (its home
   region is fixed at sign-up). See **`docs/QUICK_ACCOUNT_SETUP_RUNBOOK.md`**.
2. **Forecast** — run `notebooks/01` (data prep → catalog `mmf`, schema `fresh_retail_net`) then
   `notebooks/02` on **Serverless GPU** (Chronos-2 via MMF). Expected: 1,000 SKUs, forecast + eval tables.
   You can drive the runs with `notebooks/run_notebook.sh <nb>` (parameterized Databricks job runner).
3. **Views + Dimensions** — run `notebooks/04` (Genie views) then `notebooks/03` (product/location dims).
   Notebook 04 scopes the Genie views to Chronos-2 (`MODEL_FILTER="Chronos2"`) for this walkthrough; set
   `MODEL_FILTER=None` to expose all models MMF ran and enable model-comparison questions.
4. **Genie Agent** — create the agent, add its six tables, paste `genie/genie_instructions.md`, and pin
   `genie/genie_surge_trusted_query.sql` as a trusted query. See **`docs/GENIE_SPACE_SETUP_RUNBOOK.md`**.
5. **Order API** — deploy `order-api/supplier-order-api.yaml` (CloudFormation; the deploy command with
   `CAPABILITY_NAMED_IAM` is in `docs/QUICK_ACTION_TICKET_RUNBOOK.md` STEP 0). Capture the `ApiBaseUrl`
   stack output for the OpenAPI connector.
6. **Supplier feed** — create an S3 Tables bucket (same region as Quick) and run
   `supplier-feed/load_supplier_availability.py` to load it. Full steps in `docs/QUICK_S3TABLES_RUNBOOK.md`.
7. **Databricks OAuth app** — register a custom OAuth app for Quick (capture the client id; copy the
   secret at creation — it is shown once and cannot be regenerated).
8. **Genie MCP connector** (Quick console) — connect the Genie Agent to Amazon Quick.
   See **`docs/QUICK_DATABRICKS_CONNECTOR_RUNBOOK.md`**.
9. **OpenAPI connector** (Quick console) — register `order-api/supplier-order-api-openapi-flows.json` as a
   Quick OpenAPI action (SubmitOrder + CreateTicket). Same runbook as the Order API.
10. **S3 Tables data source** — grant Quick access to the bucket (console), then `create-data-source`
    (`--type S3_TABLES`). The `datasource` phase then auto-shares it to both your CLI and console
    identities: it prompts once for the username you sign into the console with, matches it against
    `aws quicksight list-users`, and grants both owner access (press Enter if the two identities are the
    same; the answer is saved for the `space` and `flow` phases). See **`docs/QUICK_S3TABLES_RUNBOOK.md`**.
11. **Dataset** (Quick console) — build the `supplier_availability` dataset on the data source
    (Direct Query); note its `<DATASET_ID>`. (S3 Tables datasets are console-only — the CLI `create-data-set`
    rejects native S3 Tables sources.)
12. **Supplier space** — `create-space`, then `update-space-resources` to add the dataset as a `DATA_SET`;
    the phase auto-shares the space to your CLI + console identities (same prompt as step 10, reused).
    The flow's Lookup Suppliers step reads this space (`<QUICK_SUPPLIER_SPACE_ID>`).
13. **Flow** — fill the placeholders in `flow/flow_definition.json`, `create-flow`; the phase auto-shares
    the flow to your CLI + console identities (owner role incl. `quicksight:CreatePresignedUrl`). Full
    walkthrough in `flow/flow_build_guide.md`. Schedule it to run unattended.
14. **Validate** — follow `docs/END_TO_END_TEST_RUNBOOK.md`. Expected per run: 1,000 SKUs → 6 surging
    SKUs → **5 routine orders + 1 exception ticket (product 122, Northeast)**. (DynamoDB stores product
    ids/quantities as strings — query with `.S`, not `.N`.)

## Configuration — placeholders to replace
Every environment-specific value in the runbooks/specs is a `<PLACEHOLDER>`. Substitute your own.
Find them all with: `grep -rn "<[A-Z_]*>" .`

| Placeholder | What it is | Where to get it |
|---|---|---|
| `<ACCOUNT_ID>` | your 12-digit AWS account id | `aws sts get-caller-identity` |
| `<REGION>` | the single AWS region for the whole solution (must support S3 Tables + Quick; e.g. `us-west-2`) | you choose it once |
| `<WORKSPACE_HOST>` | Databricks workspace host (`dbc-xxxx.cloud.databricks.com`) | workspace URL |
| `<DATABRICKS_ACCOUNT_ID>` | Databricks account id | account console |
| `<GENIE_SPACE_ID>` | Genie space id | space URL after you create it |
| `<WAREHOUSE_ID>` | Serverless SQL warehouse id | SQL Warehouses list |
| `<GENIE_MCP_CONNECTOR_ID>` / `<OPENAPI_ACTION_CONNECTOR_ID>` | Quick action-connector ids | `aws quicksight list-action-connectors` |
| `<OPENAPI_SUBMIT_ORDER_ACTION_ID>` / `<OPENAPI_CREATE_TICKET_ACTION_ID>` | OpenAPI action ids | connector Test action, or `describe-action-connector` |
| `<DATASET_ID>` | supplier_availability dataset id | `aws quicksight list-data-sets` (step 11) |
| `<QUICK_SUPPLIER_SPACE_ID>` | Quick space holding the dataset | you set it in `create-space` (step 12) |
| `<EXPERIMENT_ID>` | MLflow experiment id (notebook 02 metadata) | your workspace after running nb02 |
| `<API_ID>` | API Gateway id in the Order API base URL | CloudFormation stack output (step 5) |
| `<S3T_BUCKET>` | your S3 Tables bucket name | you choose it (step 4) |
| `<BUCKET_ARN>` | full S3 Tables bucket ARN | `arn:aws:s3tables:<REGION>:<ACCOUNT_ID>:bucket/<S3T_BUCKET>` |
| `<YOUR_PROFILE>` / `<your-profile>` | your local AWS CLI profile name | `~/.aws/config` |

Do not commit real secrets. The Order API key lives in AWS Secrets Manager and is read by the Lambda
at runtime; the Quick→Genie OAuth client id/secret you generate stay in the Quick connector config.

## Cost and Cleanup
See `docs/COST_AND_CLEANUP.md`. In short: the solution is serverless/usage-based
(~USD 0.50/month at rest); tear down the order API with
`aws cloudformation delete-stack --stack-name supplier-order-api --region <REGION>`, delete the
S3 Tables data, and remove the Quick flow/connectors and Databricks space/warehouse.

## License
MIT-0. See `LICENSE`.
