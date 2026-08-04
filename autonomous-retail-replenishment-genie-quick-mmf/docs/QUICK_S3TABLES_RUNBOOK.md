# Amazon Quick → S3 Tables (Supplier Feed) — Setup Runbook

Connect Amazon Quick to the supplier-availability data in S3 Tables as a **native Direct Query data
source** (no MCP, no Lambda, no proxy — Quick has a native S3 Tables connector).

> Prerequisite: an Amazon Quick account provisioned in `<REGION>` — see `QUICK_ACCOUNT_SETUP_RUNBOOK.md`.

## Environment facts
- S3 Tables bucket: `<S3T_BUCKET>` (choose your own globally-unique name; set it as `S3T_BUCKET`)
- Bucket ARN: `arn:aws:s3tables:<REGION>:<ACCOUNT_ID>:bucket/<S3T_BUCKET>`
- Namespace.table: `supply_chain.supplier_availability` (~63,861 rows across 54 supplier_ids)
- Region: `<REGION>` (same as your Quick account)

---

## STEP 0 — Create the S3 Tables bucket, then load the feed
The loader (`supplier-feed/load_supplier_availability.py`) writes into an **existing** S3 Tables
*table bucket* — it does not create the bucket. Create it first:

```bash
aws s3tables create-table-bucket --name <S3T_BUCKET> --region <REGION>
```

Then run the loader (creates the `supply_chain` namespace + `supplier_availability` table and loads
rows). The loader reads the distinct `(product_id, city_id)` keys from the retailer's own Databricks
table (`mmf.fresh_retail_net.daily_sales_raw`) via the SQL warehouse — it downloads no third-party
dataset — so it needs `DBX_PROFILE` (an authenticated `databricks auth login` profile) and
`WAREHOUSE_ID` in addition to `AWS_ACCOUNT_ID`; override `S3T_BUCKET` if you used a different bucket name:

```bash
eval "$(AWS_PROFILE=<your-profile> aws configure export-credentials --format env)"
AWS_ACCOUNT_ID=<ACCOUNT_ID> AWS_REGION=<REGION> S3T_BUCKET=<S3T_BUCKET> \
DBX_PROFILE=<DBX_PROFILE> WAREHOUSE_ID=<WAREHOUSE_ID> \
  uv run --python 3.11 \
  --with 'pyiceberg[pyarrow]>=0.9,<0.10' --with 'pyarrow>=17,<22' \
  --with boto3 --with requests \
  supplier-feed/load_supplier_availability.py
```

Expect ~63,861 rows across 54 distinct `supplier_id`s (3 suppliers × 18 cities). Then continue below.

---

## STEP 1 — Grant Quick access to the S3 Tables bucket
Amazon Quick console → **Manage Account** (admin) → **AWS Resources** → "Quick access to AWS services"
page → keep the default IAM Role **Use Quick-managed role** → under **Allow access and autodiscovery
for these resources** select **Amazon S3 Tables**
- Choose **Select S3 table buckets from current Quick account and Region** → pick **`<S3T_BUCKET>`**
- **Save.** This adds the required S3 Tables permissions to your Amazon Quick service role.

> If you don't see "Amazon S3 Tables" in the resource list, your Quick account/region may need the
> S3 Tables connector enabled — confirm the account is in `<REGION>` (where the bucket lives).

---

## STEP 2 — Create the data source
Quick → **Datasets** → **New dataset** → choose **Amazon S3 Tables (Apache Iceberg tables)**
| Field | Value |
|---|---|
| Data source name | `Supplier Availability (S3 Tables)` |
| S3 table bucket ARN | `arn:aws:s3tables:<REGION>:<ACCOUNT_ID>:bucket/<S3T_BUCKET>` |

→ Connect. Quick auto-discovers namespaces → pick namespace **`supply_chain`**.

---

## STEP 3 — Build the dataset (Direct Query)
- Select table **`supplier_availability`**.
- **Set query mode = Direct Query** (top-right) — near-real-time, reflects new ingests without manual refresh.
  (SPICE = cached/scheduled; use Direct Query for the live-supplier-feed story.)
- Save the dataset (e.g. name it `supplier_availability`).

Columns you should see:
`supplier_id, supplier_name, supplier_product_id, supplier_product_desc, retailer_product_id,
city_id, dt, available_qty, lead_time_days, unit_cost, min_order_qty`

---

## STEP 4 — Test it in Quick Chat / My Assistant
Ask questions that hit ONLY the supplier data first (isolate this source):

1. `How many suppliers are in the supplier availability data?`  → expect 54 (3 per city × 18 cities)
2. `Which suppliers can supply retailer product 122 in city 0, and what are their available quantities, lead times, and unit costs?`
   → expect 3 suppliers (S1/S2/S3) for city 0, with 7-day capacity 83/55/28, costs 5.62/4.50/3.82, lead 1/3/5.

---

## STEP 5 — The multi-source moment (forecast + supplier together)
Now both sources are connected (Genie MCP + S3 Tables). Ask a question that needs BOTH:

`Skim Milk in the Northeast is forecast to surge. Which suppliers can cover the 7-day forecast of
~50 units, and which is the cheapest that can fulfill it within its lead time?`

Expected reasoning: Genie gives the forecast/surge (product 122, ~50 units), the S3 Tables source gives
the suppliers for retailer_product_id 122 in that city; Quick picks the cheapest supplier whose
available_qty covers ~50 within lead time. (S3 at 28/7d can't cover 50 → must pick S2 or S1 — a real decision.)

> NOTE: "Northeast" / "Skim Milk" resolve via Databricks product_dim/location_dim (Genie side).
> The supplier table keys on retailer_product_id + city_id, so the reconciliation is on product 122 / city 0.

---

## Notes & gotchas
- **Reconciliation key:** supplier rows use `retailer_product_id` (= retailer product_id; a GTIN in prod).
  Suppliers have their OWN `supplier_product_id` and `supplier_product_desc` (differ per supplier) — match on retailer_product_id.
- **Direct Query** so the "daily supplier ingest" story is live (no manual refresh).
- Region must be `<REGION>` end to end.
- This is a SEPARATE data source from the Genie MCP connector — Quick orchestrates across both.
