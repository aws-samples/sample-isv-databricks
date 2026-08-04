# Amazon Quick → Databricks Genie (MCP Connector) — Setup Runbook

Connects Amazon Quick to your Databricks Genie Space over MCP (OAuth/3LO), so Quick can ask the
space natural-language questions and get surge results back. **Console-only** — there is no API/CLI
for Quick connectors. Prerequisites: an Amazon Quick account provisioned in `<REGION>`
(`QUICK_ACCOUNT_SETUP_RUNBOOK.md`) and the Genie Space already created (`GENIE_SPACE_SETUP_RUNBOOK.md`).

## Environment facts (fill in with your own values — see the placeholder table in README.md)
- Databricks workspace host: `<WORKSPACE_HOST>` (e.g. `dbc-xxxx.cloud.databricks.com`)
- Databricks account console: `accounts.cloud.databricks.com` (your account `<DATABRICKS_ACCOUNT_ID>`)
- Genie space: **Fresh Retail Sales Forecasting**, space_id `<GENIE_SPACE_ID>`
- SQL warehouse: `supply-chain-genie` (`<WAREHOUSE_ID>`, Serverless) — attached to the space
- AWS account / region: `<ACCOUNT_ID>` / `<REGION>` (Amazon Quick must be in `<REGION>`)

---

## STEP A — Register a Databricks OAuth app  (Databricks Account Console)
You must create this so Quick can authenticate to your workspace. In the **Account Console**
(`accounts.cloud.databricks.com`) → **Settings → App Connections → Add Connection**:

| Field | Value |
|---|---|
| Application name | `Amazon Quick Connector` |
| Redirect URLs (one per line) | `https://us-east-1.quicksight.aws.amazon.com/sn/oauthcallback` AND `https://<REGION>.quicksight.aws.amazon.com/sn/oauthcallback` |
| Access scopes | All APIs |
| Generate client secret | ✅ |
| Access token TTL | 1440 (24h) |
| Refresh token TTL | 129600 (90 days) |

→ **Save the Client ID and Client Secret now** (the secret is shown only once). You will paste both
into Quick in Step C2.

---

## STEP B — Databricks grants for the querying user
Genie runs with a U2M (user-to-machine) OAuth flow, so **queries run as the Databricks user who
logs in** from Quick. That user needs:
- **CAN VIEW** on the Genie space
- **CAN USE** on the SQL warehouse `supply-chain-genie`
- **SELECT** on the catalog/schema `mmf.fresh_retail_net` (the six space tables/views)

If you created everything yourself you already hold these as owner. If a different person will log
in from Quick, grant them the three above in Databricks (space sharing, warehouse permissions, and a
`GRANT SELECT ON SCHEMA mmf.fresh_retail_net TO <user>`).

---

## STEP C — Create the MCP connector in Amazon Quick  (Quick console)

Connectors → **Create for your team** → **Model Context Protocol (MCP)**

### C1. Integration details
| Field | Value |
|---|---|
| Name | `Databricks Supply Chain Genie` |
| Description | `Supply chain demand forecast intelligence via Genie Agent` |
| MCP server endpoint | `https://<WORKSPACE_HOST>/api/2.0/mcp/genie/<GENIE_SPACE_ID>` |

→ Next

### C2. Authentication — select **User authentication (OAuth)**
| Field | Value |
|---|---|
| Client ID | *(from Step A)* |
| Client Secret | *(from Step A)* |
| Token URL | `https://<WORKSPACE_HOST>/oidc/v1/token` |
| Authorization URL | `https://<WORKSPACE_HOST>/oidc/v1/authorize` |
| Redirect URL | `https://<REGION>.quicksight.aws.amazon.com/sn/oauthcallback` (matches your Quick region) |

→ Create and continue

### C3. Review capabilities
Quick connects to the MCP server, discovers Genie's tools, and lists them as actions. Confirm the
Genie tools appear → Next → share with team if needed.

---

## STEP D — First use & validation
1. Your first Genie question in Quick triggers a **one-time Databricks login prompt** (browser).
   After that, the session persists ~90 days (token refresh).
2. **Connectivity test:** Quick Chat → *"How many SKUs do we have?"* → expect **1,000**.
3. **Hero test:** *"Is Skim Milk demand spiking in the Northeast?"* → expect the surge answer
   (ratio ~1.69, ~50 units/7d) — same as the Genie UI.

---

## Critical rules
- **Workspace-level OIDC endpoints**, NOT account-level (`accounts.cloud.databricks.com/oidc/...`
  returns auth errors — Genie/UC are workspace resources).
- **MCP = remote HTTP streaming only.** No stdio, no VPC, no custom HTTP headers.
- **Static tool registration:** Quick caches Genie's tools at connect time. If you change the space
  later (add/remove tables), you must **delete and recreate this MCP integration**.
- **Redirect URL must match** one of the URLs registered in Step A and your Quick region (<REGION>).

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| Auth error on connect | Used account-level OIDC URL → switch to workspace-level (`<WORKSPACE_HOST>/oidc/...`) |
| Redirect/callback mismatch | Step C2 Redirect URL not in Step A's registered list, or wrong region |
| Query permission error | Logging-in user missing CAN VIEW / CAN USE / SELECT grant (Step B) |
| PENDING_WAREHOUSE / timeout | warehouse cold start — `supply-chain-genie` is serverless so this should not occur; keep it from being deleted |
| New table not visible in Quick | space changed after connect → delete + recreate the MCP integration |
