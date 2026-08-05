# Offline logic smoke test

Verifies the walkthrough's **DETECT → DECIDE** logic without provisioning any
infrastructure — no Databricks workspace, no AWS account, no Amazon Quick.

Useful as a fast pre-flight check before you build, and as a regression guard if you
change the surge query or the supplier economics.

## Run

```bash
cd autonomous-retail-replenishment-genie-quick-mmf
uv run --python 3.11 --with duckdb --with pandas --with pyarrow \
  smoketest/local_logic_smoketest.py
```

Exits non-zero if any check fails.

## What it proves

| | Check |
|---|---|
| **DETECT** | `genie/genie_surge_trusted_query.sql` selects exactly the intended surging SKUs when run against representative forecast/actual data |
| **DECIDE** | Supplier economics from `supplier-feed/load_supplier_availability.py` reconcile to the documented decision: a covered surge routes to the cheapest supplier that covers within lead time, and the deliberately under-supplied SKU routes to the exception (ticket) path |

Constants are read from the loader source with `ast` rather than imported, so the test uses
the loader's real values as a single source of truth without pulling in PyIceberg or
requiring AWS credentials.

## What it does not cover

These need live infrastructure and are verified during the walkthrough itself:

- Chronos-2 forecast accuracy (Serverless GPU notebook 02)
- Amazon Quick MCP/OAuth, S3 Tables Direct Query, Quick Flows orchestration (console-only)
- The deployed Supplier Order API round-trip (needs AWS credentials + CloudFormation)
