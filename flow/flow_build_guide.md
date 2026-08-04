# Quick Flows Build Guide — Autonomous Supply Chain Replenishment

The orchestration + automation layer. This Flow ties together Genie (forecast/surge detection),
S3 Tables (supplier availability), a decision rule, and the Supplier Order API (order/ticket actions),
and runs it **unattended on a schedule**.

Built and validated live: a scheduled run with "run with no confirmation" placed correct routine
orders for 5 of the 6 surging SKUs, and raised a human-review ticket for the 1 SKU that no single
supplier could cover — all with no human interaction.

---

## Prerequisites (must exist before building the Flow)
1. **Genie MCP connector** in Quick → the "Fresh Retail Sales Forecasting" space
   (see QUICK_DATABRICKS_CONNECTOR_RUNBOOK.md). The space MUST have the surge **trusted query**
   pinned (see genie/genie_surge_trusted_query.sql + genie_instructions.md) so it returns clean,
   deterministic keys: unique_id, retailer_product_id, city_id, region, city_name,
   forecast_7d_total, surge_ratio.
2. **S3 Tables supplier dataset** connected to Quick (Direct Query) — see QUICK_S3TABLES_RUNBOOK.md.
3. **Supplier Order API** deployed (CloudFormation) and registered in Quick as an **OpenAPI connector**
   from `supplier-order-api-openapi-flows.json` (exposes `submitOrder` and `createTicket`).
   - Import format MUST be JSON (Quick rejects YAML). No array types (that's why /orders/bulk is dropped).
   - Auth: None works for the demo (the Lambda's x-api-key check is optional); use the key header for prod.

---

## Flow structure

```
Step 1  Detect Demand Surges            (Quick data → Genie MCP)        ← runs once
└─ SKU Processing                       (Reasoning Group, iterates per surging SKU)
     Step 2  Lookup Suppliers           (Quick data → S3 Tables dataset)
     Step 3  Select Best Supplier       (reasoning — pinned decision rule)
     Step 4  Submit Order  (routine)    (Application action → submitOrder)   only-run-if routine
     Step 5  Raise Ticket  (exception)  (Application action → createTicket)  only-run-if exception
     Step 6  Generate SKU Summary       (reasoning)
Step 7  Generate Consolidated Report    (reasoning, after the group)
```

### Hard-won lessons baked into this design
- **Execution follows visual top-to-bottom order**, not @-reference dependency. Keep steps in order.
- **A reasoning group cannot be nested inside a reasoning group.** The fork is done with per-step
  **"only run if"** conditions on Step 4 and Step 5 — not a nested branch.
- **The reasoning group's Instructions field must be filled** — an empty one breaks loop processing.
- **Action steps must bind an EXPLICIT operation** (submitOrder / createTicket), NOT "Choose for me."
  "Choose for me" makes the step narrate the call instead of invoking it.
- **Write actions require confirmation on manual runs**; the **schedule** has a "Run with no
  confirmation" toggle that makes routine orders auto-fire unattended.
- **Determinism:** Genie writes SQL per run and will drift. Pinning the surge query as a Genie
  trusted/example query makes unattended runs reproducible.

---

## Step-by-step

### Step 1 — Detect Demand Surges  (Quick data step → Genie connector)
Prompt:
```
List the SKUs with a demand surge. Return one row per surging SKU with these columns:
unique_id, retailer_product_id, city_id, region, city_name, forecast_7d_total, surge_ratio.
A demand surge is where the average forecast over 2024-06-26 to 2024-07-02 is at least 1.5x the
average actual sales over the prior 14 days (2024-06-12 to 2024-06-25).
```
(Genie reuses the pinned trusted query → deterministic, correct keys. Returns 6 SKUs.)

### Create the SKU Processing reasoning group
- Type: **Reasoning Group**
- **Run these steps for each item in:** Detect Demand Surges
- **Instructions** (REQUIRED — empty breaks the loop):
```
For each surging SKU from Detect Demand Surges, run these steps in order, processing exactly one SKU
per iteration:
1. Look up the suppliers for this SKU's retailer_product_id and city_id.
2. Select the best supplier: the cheapest whose total 7-day available quantity >= the SKU's 7-day
   forecast demand. Compare raw 7-day quantities directly; never scale availability by lead time.
   If a supplier covers demand it is a routine order; if none can, it is an exception for human review.
3. If routine, call submitOrder to place the order (only when routine).
4. If an exception, call createTicket to raise a human-review ticket (only when an exception).
5. Summarize the SKU outcome.
Take real actions by invoking the operations — do not just describe what would happen.
```

### Step 2 — Lookup Suppliers  (Quick data step → S3 Tables dataset)
Prompt:
```
You are given a surging SKU with two integer fields: retailer_product_id and city_id from
@Detect Demand Surges. From the supplier availability data, return every supplier whose
retailer_product_id AND city_id exactly equal those two values. For each supplier return:
supplier_id, supplier_name, total available_qty over the 7-day window, lead_time_days, and unit_cost.
Sort by unit_cost ascending. Do not reinterpret, split, or reformat any id.
```

### Step 3 — Select Best Supplier  (reasoning step)
Prompt:
```
Based on the demand forecast from @Detect Demand Surges and the supplier data from @Lookup Suppliers:

Apply this EXACT decision rule. Do not invent any other criteria.
1. A supplier "covers demand" if its total_available_qty >= forecast_7d_total. Compare the raw 7-day
   quantity directly. Do NOT prorate or scale the quantity by lead time. lead_time_days is informational.
2. Among the suppliers that cover demand, choose the one with the LOWEST unit_cost. This is a routine order.
3. Only if NO supplier covers demand, mark the SKU as needing human review (exception).
Do NOT consider surge volatility, safety buffers, multi-supplier splitting, or lead-time urgency.
Output: selected supplier_id, unit_cost, quantity to order (round forecast_7d_total up to a whole number),
region, city_id, retailer_product_id, and Order Type = "routine" or "exception".
```

### Step 4 — Submit Order  (Application action → submitOrder)  — only run if Order Type = routine
- **Action connector:** Supplier Order API ; **Action:** submitOrder (EXPLICIT)
- **Condition / only run if:** Order Type from @Select Best Supplier = routine
- Prompt:
```
Submit a routine purchase order by calling the submitOrder operation now. Do not describe the call —
invoke it. Use the selected supplier from @Select Best Supplier and SKU details from @Detect Demand
Surges. Map: supplier_id, retailer_product_id, quantity (rounded-up demand), unit_cost, region, city_id,
order_type = "routine". Return order_id and status.
```

### Step 5 — Raise Ticket  (Application action → createTicket)  — only run if Order Type = exception
- **Action connector:** Supplier Order API ; **Action:** createTicket (EXPLICIT)
- **Condition / only run if:** Order Type = exception
- Prompt:
```
Raise a human-review ticket by calling the createTicket operation now. Do not describe the call —
invoke it. Use the SKU from @Detect Demand Surges and the decision from @Select Best Supplier.
Map: subject = "Human review needed: no single supplier can cover the demand surge for product " +
retailer_product_id + " in " + region; description = short note that no single supplier covers the
7-day demand, with the demand qty and surge ratio; retailer_product_id; region; priority = "High";
category = "supply-exception". Return ticket_id and status.
```

### Step 6 — Generate SKU Summary  (reasoning step)
Prompt:
```
Create a summary for the current SKU from @Detect Demand Surges: product/retailer_product_id, region,
chosen supplier (if any), quantity, unit cost, and the action taken (order submitted with order_id, or
ticket raised with ticket_id). Base supplier/action on @Select Best Supplier, @Submit Order, @Raise Ticket.
```

### Step 7 — Generate Consolidated Report  (reasoning step, after the group)
Prompt:
```
Compile all @Generate SKU Summary results into a supply chain replenishment report: total SKUs processed,
orders placed, tickets raised, and a SKU-by-SKU breakdown. Format as an executive summary.
```

---

## Schedule it (this is what makes it autonomous)
1. Open the Flow in **Run mode** → **scheduling icon** → **Create schedule**.
2. Recurrence: e.g. daily at a fixed time, with timezone (and an end date if you want it to auto-stop).
3. Provide default inputs (none required — Step 1 is self-contained).
4. **Action permissions → turn ON "Run with no confirmation"** so routine write actions auto-submit
   during scheduled runs. (Manual runs always confirm each write — good for testing; the docs note user
   confirmation helps avoid AI prediction errors, which is a fair responsible-AI framing for the blog.)
5. Save. You'll get an email when each scheduled run completes.

---

## Verify end to end
- After a scheduled run, the orders land in DynamoDB `supplier-order-api-orders` and exceptions in
  `supplier-order-api-tickets`. Correct output for the 6 demo SKUs is 5 routine orders + 1 exception ticket
  (values below verified from a live run, 2026-07-21):
  - Routine orders (5): 261→SUP_S3_16/13/West, 191→SUP_S3_10/21/Southwest,
    299_300→SUP_S2_12/37/West, 450_300→SUP_S3_12/15/West, 4→SUP_S3_12/11/West.
  - Exception ticket (1): 122 (Northeast) — no single supplier can cover the 7-day demand surge,
    so Step 5 fires createTicket instead of an order.
  ```bash
  aws dynamodb scan --table-name supplier-order-api-orders  --region <REGION>
  aws dynamodb scan --table-name supplier-order-api-tickets --region <REGION>
  ```
- The exception path fires naturally on SKU 122 — no need to force it. (If you want to demo it on a
  different SKU, temporarily make that SKU's demand exceed all suppliers so no supplier covers it.)
- Product ids and quantities are stored as strings in DynamoDB; if you query with
  `--query "Items[].retailer_product_id.N"` they read as empty. Use `.S`.
- A recurring schedule appends a fresh batch (5 orders + 1 ticket) every interval, so the tables grow
  each run. Scan by `created_at` to isolate a single run, and disable the schedule after validating.
