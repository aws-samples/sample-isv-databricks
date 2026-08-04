# End-to-End Testing & Usage Runbook — Autonomous Supply Chain Demo

Run these in **Amazon Quick on the web (AWS portal)** — where all components live and were validated.
Sequence goes from "is each piece connected" → "do they fuse" → "does the full detect-decide-act loop run."
Each step lists the exact prompt and the expected result, so a failure pinpoints the broken layer.

## Prerequisites checklist (all already done — confirm before testing)
- [ ] Genie space "Fresh Retail Sales Forecasting" on Serverless warehouse `supply-chain-genie`, 6 tables.
- [ ] Quick → Genie MCP connector created (User auth OAuth).
- [ ] Quick → S3 Tables dataset `supplier_availability` published (Direct Query), region columns present.
- [ ] Quick custom actions added: Order API (`submitOrder`) + Ticket API (`createTicket`). (The Flows
      connector exposes only single-object operations; `/orders/bulk` is intentionally not wired — Quick rejects array types.)
- [ ] Order/Ticket API stack `supplier-order-api` deployed; x-api-key in Secrets Manager.
- [ ] All sources attached to the chat/assistant you test in.

Get the API key when configuring actions:
```
aws secretsmanager get-secret-value --secret-id supplier-order-api/api-key --region <REGION> --query SecretString --output text
```

---

## TIER 1 — Each source answers on its own (isolate connectivity)

**1.1 Genie (forecast intelligence)**
Prompt: `How many SKUs do we have?`
Expect: **1,000** (store-product combinations). → proves MCP + OAuth + warehouse + permissions.

**1.2 Genie (forecast retrieval)**
Prompt: `What is the 7-day forecast for SKU 18_122?`
Expect: 7 daily values (~6.4–7.6 units, Jun 26–Jul 2 2024).

**1.3 S3 Tables (supplier data)**
Prompt: `How many suppliers are in the supplier availability data?`
Expect: **54** (3 per city × 18 cities). → proves the S3 Tables dataset is queryable.

**1.4 S3 Tables (region resolves natively)**
Prompt: `Which suppliers operate in the Northeast region?`
Expect: 9 suppliers across Boston, New York, Philadelphia.

---

## TIER 2 — Detection (Genie multi-table reasoning)

**2.1 Surge detection**
Prompt: `Which SKUs have a forecasted demand surge — where the 7-day average forecast is at least 1.5x the recent 14-day average sales?`
Expect: 6 SKUs (18_122, 450_300, 3_191, 299_300, 561_261, 375_4) with surge ratios ~1.5–1.7x.

**2.2 Named hero SKU**
Prompt: `Is Skim Milk demand spiking in the Northeast?`
Expect: yes — SKU 18_122, recent ~4.26/day, forecast ~7.18/day, surge ratio ~1.69, 7-day total ~50 units.

---

## TIER 3 — Cross-source fusion (the core thesis: forecast + supplier)

**3.1 The multi-source decision** (attach BOTH Genie + supplier dataset)
Prompt:
```
Skim Milk demand is surging in the Northeast for June 26 to July 2, 2024. Use the Databricks forecast
for the total demand, then from the supplier availability data (matching on retailer_product_id) list
the suppliers for that product with their 7-day total available quantity, lead time, and unit cost.
Recommend the cheapest supplier that can cover the full demand within its lead time.
```
Expect: forecast ~50 units → 3 suppliers (S1=83u/$5.62/1d, S2=55u/$4.50/3d, S3=28u/$3.82/5d) →
**recommends S2** (cheapest that covers 50; S3 too small at 28; S1 fastest but priciest), with cost ~$225.

> If it answers from only one source: confirm both are attached, and add
> "match on retailer_product_id, not the description" + pin the June 26–July 2 2024 dates.

---

## TIER 4 — Action (detect → decide → ACT)

**4.1 Routine auto-order** (single supplier covers → submit)
Prompt:
```
For the Skim Milk surge in the Northeast, submit a routine purchase order to the recommended supplier
(S2) for the forecasted quantity. Use retailer_product_id 122, quantity 50, unit cost 4.50.
```
Expect: order confirmation with `ORD-...`, status **SUBMITTED**.
Verify: `aws dynamodb scan --table-name supplier-order-api-orders --region <REGION>` shows the order.

**4.2 Exception → ticket** (no single supplier can cover → raise ticket)

> This is a **forced/hypothetical** test of the ticket path — the demand figure below is invented so
> the exception branch fires on demand. In the real scheduled run the *natural* exception is SKU
> **561_261** (product 261, West), which no single supplier can cover; the two product-300 SKUs
> (299_300, 450_300) are routine orders. Use this prompt only to exercise `createTicket` directly.

Prompt:
```
For product 300 in the West, assume a forecast of 120 units that no single supplier can cover. Raise a
ticket for manual review with subject "Demand surge - no single supplier covers product 300" and
priority High.
```
Expect: ticket confirmation with `TKT-...`, status **OPEN**.
Verify: `aws dynamodb scan --table-name supplier-order-api-tickets --region <REGION>` shows the ticket.

---

## TIER 5 — The full autonomous loop (the demo finale, one prompt)

Prompt:
```
Skim Milk demand is surging in the Northeast for June 26 to July 2, 2024. Use the Databricks forecast
for the total demand, find the suppliers for that product (match on retailer_product_id), pick the
cheapest supplier that can cover it within lead time, and submit a routine purchase order to that
supplier for the forecasted quantity. If no single supplier can cover it, raise a ticket for manual
review instead.
```
Expect: Genie forecast (~50) → S3 Tables suppliers → S2 chosen → **routine order SUBMITTED via /orders**.
This is forecast → detect → decide → act in one conversation, no SQL, no Databricks login.

> If Quick won't chain query+action in one turn, run it as two turns: (1) get the recommendation
> (Tier 3.1), then (2) "submit the routine order to S2" (Tier 4.1). Both paths are valid for the demo.

---

## TIER 6 — Package as a Quick Flow (the "automation" story)

Tiers 1–5 prove the loop works conversationally. A **Quick Flow** packages it into a deployed,
repeatable, scheduled automation — no manual prompting each run. The full build (steps, reasoning
group, per-step "only run if" fork, and the schedule) is documented in `flow/flow_build_guide.md`;
this tier is the end-to-end validation of that Flow.

The Flow structure (see `flow/flow_build_guide.md` for the step-by-step prompts):
- **Step 1 — Detect Demand Surges** (Genie MCP, pinned trusted query): returns the surging SKUs.
- **SKU Processing reasoning group** (iterates per surging SKU):
  - **Lookup Suppliers** (S3 Tables Direct Query) → supplier options with 7-day qty, lead time, cost.
  - **Select Best Supplier** (reasoning): cheapest supplier whose 7-day quantity covers demand →
    routine; if none can cover it → exception.
  - **Submit Order** → `submitOrder`, only run if routine.
  - **Raise Ticket** → `createTicket`, only run if exception.
  - **Generate SKU Summary**.
- **Step 7 — Generate Consolidated Report**.

### Run it unattended — schedule the Flow (this is what makes it autonomous)
In **Run mode → scheduling icon → Create schedule**: set recurrence/timezone, and turn ON
**"Run with no confirmation"** so routine write actions auto-fire during scheduled runs (manual runs
always confirm each write). See `flow/flow_build_guide.md` for the full schedule steps.

With the schedule, the full loop — detect surge → choose supplier → auto-order routine / raise a
human-review ticket for exceptions — runs on its own, no login, no prompt. That is the autonomous loop
the title promises; the conversational path (Tiers 1–5) is the same loop run interactively/ad hoc.

**Validated result:** a single scheduled run, no human interaction, produced **5 routine purchase
orders** for the covered SKUs and **1 human-review ticket** for SKU 261 (Denver/West), which no single
supplier could cover — the loop tiering itself correctly.

---

## Cleanup / housekeeping
- Reset demo data between runs (optional): delete items from the two DynamoDB tables, or leave them
  (each run creates new ORD-/TKT- ids).
- Tear down all order/ticket infra when finished:
  ```
  aws cloudformation delete-stack --stack-name supplier-order-api --region <REGION>
  ```
- API key lives in Secrets Manager (`supplier-order-api/api-key`); rotate the demo value and keep it out
  of the published blog.

## Reading the results (what a failure tells you)
| Fails at | Likely cause |
|---|---|
| 1.1 | MCP connector / OAuth / warehouse / permissions |
| 1.3 | S3 Tables dataset not published or not attached |
| 1.4 | region columns missing — republish the dataset |
| 3.1 (one source only) | both sources not attached, or prompt didn't force the join/key/date |
| 4.x | custom action not added, wrong x-api-key, or API stack down |
