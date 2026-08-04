# Amazon Quick console guide (the manual steps)

Four steps in this solution cannot be done with the AWS CLI — the `quicksight` API rejects them — so
you do them in the Amazon Quick console. The two `scripts/` orchestrators automate everything else and
**stop** where a console step is required, pointing here. Do these in the order the scripts prompt you.

Substitute your own values for every `<PLACEHOLDER>`. Screenshots are marked `[SCREENSHOT: ...]` —
capture them from your own console (the UI labels evolve; treat the click-paths as the source of truth).

> **Two Quick identities.** Resources created by the CLI are owned by the Quick user your CLI
> credentials map to; the console signs you in as a (possibly different) identity. After any CLI step,
> share the resource to both identities. The console steps below are owned by your *console* identity.

---

## Console step 1 — Grant Quick access to the S3 Tables bucket
*(do this before `setup_aws.sh datasource`)*

The CLI cannot register Quick's access to an AWS resource — there is no API for it.

1. Profile icon (top right) → **Manage account** (labeled **Manage QuickSuite** in some versions).
2. **Permissions** → **AWS resources**.
3. Choose **Use QuickSuite-managed role (default)** — let Quick create and manage the IAM role.
4. Choose **Amazon S3 Tables** → **S3 table buckets from current Quick account & region**.
5. Select your supplier-feed bucket `<S3T_BUCKET>` → **Finish** → **Save**.

> **Do not hand-create `aws-quicksight-s3-tables-role-v0`.** If a role with that name already exists,
> the console attaches its policy but does not repair the trust relationship, and the data source fails
> with "Failed to assume the service role." Let the console create it.

`[SCREENSHOT: Manage account → Permissions → AWS resources, S3 Tables selected with the bucket checked]`

---

## Console step 2 — Genie MCP connector (Quick → Databricks Genie)
*(do this before `setup_aws.sh flow`; needs the OAuth client id/secret from `setup_databricks.sh oauth`)*

The MCP connector type is not API-creatable (`create-action-connector` rejects `MODEL_CONTEXT_PROTOCOL`).

1. **Connectors** → **Create for your team** → **Model Context Protocol (MCP)**.
2. Fill in:

   | Field | Value |
   |---|---|
   | Name | `Databricks Supply Chain Genie` |
   | MCP server endpoint | `https://<WORKSPACE_HOST>/api/2.0/mcp/genie/<GENIE_SPACE_ID>` |
   | Authentication | **User authentication (OAuth)** |
   | Client ID | `<OAUTH_CLIENT_ID>` |
   | Client Secret | *(paste from your secrets manager — never into chat/commits)* |
   | Token URL | `https://<WORKSPACE_HOST>/oidc/v1/token` |
   | Authorization URL | `https://<WORKSPACE_HOST>/oidc/v1/authorize` |
   | Redirect URL | `https://<REGION>.quicksight.aws.amazon.com/sn/oauthcallback` |

3. Complete the OAuth authorization (sign in to Databricks as the user with read on `mmf.fresh_retail_net`).
4. **Share** the connector, then finish. Status should be **CREATION_SUCCESSFUL**.
5. Capture its id: `aws quicksight list-action-connectors --aws-account-id <ACCOUNT_ID> --region <REGION>
   --profile <AWS_PROFILE> --query "ActionConnectorSummaries[?Type=='MODEL_CONTEXT_PROTOCOL'].ActionConnectorId"`.

`[SCREENSHOT: MCP connector create form with the endpoint + OAuth fields]`
`[SCREENSHOT: connector status CREATION_SUCCESSFUL with discovered Genie tools]`

---

## Console step 3 — OpenAPI connector (Quick → Supplier Order API)
*(do this before `setup_aws.sh flow`)*

`create-action-connector` also rejects `OPEN_API`, so this is a console step.

1. **Connectors** → **Create for your team** → **OpenAPI specification**.
2. **Add schema** *(the first screen — a required upload gate; accepts `.json`, max 1 MB)*: upload
   `order-api/supplier-order-api-openapi-flows.json` from the repo. Review it in the code editor.
3. **Name**: `Supplier Order and Ticket OpenAPI`.
4. **Description** (required): `External supplier order intake API. Submits a routine purchase order or
   raises a human review ticket when no single supplier can cover demand.`
5. **Base URL**: your Order API `ApiBaseUrl` (`https://<API_ID>.execute-api.<REGION>.amazonaws.com/prod`).
6. **Authentication**: **None** (the demo Lambda treats the API key as optional).
7. Finish. Registers two write actions: **SubmitOrder** (`POST /orders`), **CreateTicket** (`POST /tickets`).
8. Collect the ids the flow needs:
   - Connector id: `list-action-connectors` (the `OPEN_API` row).
   - Action ids: open the connector → **Test action** on SubmitOrder and CreateTicket → note each `id`.

`[SCREENSHOT: Add schema upload screen]`
`[SCREENSHOT: connector with SubmitOrder + CreateTicket actions listed]`

---

## Console step 4 — Supplier dataset (on the S3 Tables data source)
*(do this before `setup_aws.sh space`; needs the data source from `setup_aws.sh datasource`)*

`create-data-set` does not support S3 Tables ("only supported for new data preparation experience
datasets"), so build the dataset in the console.

1. **Datasets** → **New dataset**.
2. Choose the data source **Supplier Availability S3 Tables**.
3. Confirm the auto-discovered **Namespace** `supply_chain` and **Table** `supplier_availability`.
4. Choose **Directly query your data** (Direct Query — not SPICE). Leave "Email owners when a refresh
   fails" unchecked (SPICE-only).
5. Choose **Visualize** — *this commits and saves the dataset* — then **close** the analysis chooser
   that opens (you need neither an analysis nor a report).
6. Capture the id: `aws quicksight list-data-sets --aws-account-id <ACCOUNT_ID> --region <REGION>
   --profile <AWS_PROFILE> --query "DataSetSummaries[?Name=='supplier_availability'].DataSetId"`.
   Set it as `DATASET_ID` before running `setup_aws.sh space`.

`[SCREENSHOT: New dataset — data source + table selection]`
`[SCREENSHOT: Direct Query option selected before Visualize]`

---

## Where this fits

```
setup_databricks.sh  (notebooks → Genie → OAuth)
setup_aws.sh order-api → feed → quick-account
   → CONSOLE step 1 (S3 Tables grant)
setup_aws.sh datasource   [+ share to both identities]
   → CONSOLE step 4 (dataset)
setup_aws.sh space
   → CONSOLE steps 2 & 3 (MCP + OpenAPI connectors)
setup_aws.sh flow          [+ share flow, then schedule in console]
```
