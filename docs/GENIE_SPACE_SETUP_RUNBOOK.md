# Databricks Genie Space — Creation Runbook

Creates the **Fresh Retail Sales Forecasting** Genie Space that Amazon Quick queries over MCP.
Do this AFTER notebooks `01 → 02 → 04 → 03` have run (the space's tables/views must exist).
Console steps are in the Databricks workspace UI.

## Prerequisites
- Notebooks run in this order: `01` (data prep) → `02` (forecast) → `04` (Genie views) → `03` (dims).
  The tables/views below must already exist in catalog `mmf`, schema `fresh_retail_net`.
- A **Serverless SQL Warehouse** the space will run on. This repo uses one named `supply-chain-genie`.
  Create one via **SQL → SQL Warehouses → Create** (Serverless), or reuse an existing serverless
  warehouse. Note its **warehouse id** (`<WAREHOUSE_ID>`) — you'll need it for the connector.
- **CAN USE** on that warehouse and **SELECT** on `mmf.fresh_retail_net`.

## STEP 1 — Create the space
Databricks workspace → **Genie** (left nav) → **New** → **New Genie space**.
| Field | Value |
|---|---|
| Space name | `Fresh Retail Sales Forecasting` |
| SQL warehouse | `supply-chain-genie` (your serverless warehouse) |
| Default catalog / schema | `mmf` / `fresh_retail_net` |

Save. The space's **space_id** appears in its URL (`.../genie/rooms/<GENIE_SPACE_ID>/...`) — record it
as `<GENIE_SPACE_ID>` for the Quick connector.

## STEP 2 — Add the six tables/views
In the space → **Data** (or **Add tables**), add exactly these **6** objects from `mmf.fresh_retail_net`:

| # | Object | Created by | Purpose |
|---|---|---|---|
| 1 | `daily_sales_raw` | notebook 01 | raw daily sales (actuals) |
| 2 | `demand_train` | notebook 01 | training history (per-SKU daily) |
| 3 | `scoring_output_mv` | notebook 04 | exploded Chronos-2 forecasts (one row per SKU per forecast date) |
| 4 | `evaluation_metrics_mv` | notebook 04 | backtest accuracy metrics |
| 5 | `product_dim` | notebook 03 | product_id → name / category / brand |
| 6 | `location_dim` | notebook 03 | city_id → city name / region / state |

(These are the 6 objects the connector runbook and end-to-end tests refer to as "the 6 tables.")

## STEP 3 — Paste the space instructions
Space → **Instructions** → paste the full contents of `genie/genie_instructions.md`. These define
what a SKU is, how to resolve product/region names, and the surge output contract.

## STEP 4 — Pin the trusted surge query (determinism)
Space → **Example / Trusted queries** → add a new one → paste `genie/genie_surge_trusted_query.sql`.
Mark it **trusted**. This is what makes unattended surge detection reproducible: Genie reuses this
exact SQL instead of generating (and drifting) its own each run.

## STEP 5 — Validate the space directly (before wiring Quick)
In the Genie space chat:
- *"How many SKUs do we have?"* → **1,000**
- *"Which products are surging in the Northeast?"* → returns the surging SKUs with
  `unique_id, retailer_product_id, city_id, region, city_name, forecast_7d_total, surge_ratio`
- *"Is Skim Milk demand spiking in the Northeast?"* → surge answer, ratio ~1.69, ~50 units/7d

Once these work, proceed to `QUICK_DATABRICKS_CONNECTOR_RUNBOOK.md` to connect the space to Amazon Quick.
