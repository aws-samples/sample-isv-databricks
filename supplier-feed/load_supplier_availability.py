"""Load synthetic supplier-availability data into Amazon S3 Tables (Iceberg).

Storyline: suppliers report daily stock availability per product and location; the customer ingests
that feed into Amazon S3 Tables. Amazon Quick later reads this (native S3 Tables Direct Query) alongside
the Databricks Chronos-2 forecast (Genie MCP) to choose a supplier and act (order via the Supplier Order API).

INDEPENDENCE: this loader has NO dependency on Databricks. It reads only the product/location KEYS from
the same HuggingFace dataset (FreshRetailNet-50K) used by the original prep, and generates the supplier
catalog itself. Suppliers use THEIR OWN product codes (`supplier_product_id`) and THEIR OWN descriptions
(`supplier_product_desc`) which vary slightly per supplier -- exactly like real supplier catalogs. The
shared reconciliation key is `retailer_product_id` (the retailer's product_id; a GTIN/UPC in production),
which is how Quick matches a retailer SKU to supplier offers.

WRITE path: PyIceberg -> S3 Tables NATIVE Iceberg REST endpoint (signing-name=s3tables, SigV4). No
analytics-services integration needed. Idempotent: re-runs drop+recreate the table.

Run (ephemeral deps via uv; uses your AWS creds):
    eval "$(AWS_PROFILE=<YOUR_PROFILE> aws configure export-credentials --format env)"
    AWS_ACCOUNT_ID=<ACCOUNT_ID> AWS_REGION=<REGION> uv run --python 3.11 \
        --with 'pyiceberg[pyarrow]>=0.9,<0.10' --with 'pyarrow>=17,<22' \
        --with boto3 --with requests --with huggingface_hub \
        load_supplier_availability.py
"""
from __future__ import annotations
import os
import datetime as dt
import pyarrow as pa
import pyarrow.parquet as pq
from pyiceberg.catalog import load_catalog

# ---------------------------------------------------------------- config
REGION = os.environ.get("AWS_REGION")  # required; use the SAME region for the whole solution
if not REGION:
    raise SystemExit("Set AWS_REGION to the region you are using for the whole solution "
                     "(must support S3 Tables + Amazon Quick, e.g. us-west-2).")
ACCOUNT = os.environ.get("AWS_ACCOUNT_ID")  # required; set to your AWS account id
if not ACCOUNT:
    raise SystemExit("Set AWS_ACCOUNT_ID to your AWS account id before running this loader.")
BUCKET = os.environ.get("S3T_BUCKET")  # required; the S3 Tables bucket you created for this solution
if not BUCKET:
    raise SystemExit("Set S3T_BUCKET to the S3 Tables bucket name you created for this solution.")
NAMESPACE = os.environ.get("S3T_NAMESPACE", "supply_chain")
TABLE = os.environ.get("S3T_TABLE", "supplier_availability")

S3T_REST_URI = f"https://s3tables.{REGION}.amazonaws.com/iceberg"
WAREHOUSE_ARN = f"arn:aws:s3tables:{REGION}:{ACCOUNT}:bucket/{BUCKET}"

FORECAST_DATES = [dt.date(2024, 6, 26) + dt.timedelta(days=i) for i in range(7)]
HF_REPO = "Dingdong-Inc/FreshRetailNet-50K"

# Geographic labels carried on the supplier feed (a real supplier feed names the city/region it serves).
# Keyed by city_id; MUST match the retailer-side location_dim mapping so both sides share the same
# geographic vocabulary (like they share retailer_product_id). City 0 = Boston/Northeast (hero).
CITY_GEO = {
    0:("Boston","Northeast","MA"), 1:("New York","Northeast","NY"), 2:("Philadelphia","Northeast","PA"),
    3:("Atlanta","Southeast","GA"), 4:("Miami","Southeast","FL"), 5:("Charlotte","Southeast","NC"),
    6:("Chicago","Midwest","IL"), 7:("Detroit","Midwest","MI"), 8:("Minneapolis","Midwest","MN"),
    9:("Dallas","Southwest","TX"), 10:("Houston","Southwest","TX"), 11:("Phoenix","Southwest","AZ"),
    12:("Los Angeles","West","CA"), 13:("San Francisco","West","CA"), 14:("Seattle","Northwest","WA"),
    15:("Portland","Northwest","OR"), 16:("Denver","West","CO"), 17:("Las Vegas","West","NV"),
}

# Supplier profiles: (tag, capacity multiplier vs weekly baseline, lead days, unit-cost factor).
PROFILES = [("S1", 2.0, 1, 1.25), ("S2", 1.1, 3, 1.00), ("S3", 0.7, 5, 0.85)]
BASE_COST = 4.50

# --- Supplier-side product vocabulary (INDEPENDENT of the retailer's product_dim) ---
# Each supplier describes the same physical product slightly differently. We derive a supplier-specific
# code and description from the reconciliation key (retailer product_id) + the supplier tag, so the same
# retailer product_id reconciles to 3 different supplier SKUs/descriptions.
SUPPLIER_CATS = ["DRY", "PRD", "BKY", "MET", "BEV", "FRZ", "PAN"]  # supplier's own category codes
# supplier-specific phrasing variants, indexed by profile so each supplier phrases it differently
DESC_STYLE = {
    "S1": "{base} 2L",
    "S2": "Fat-Free {base} (2L)",
    "S3": "{base}, Skimmed 2L",
}
# generic base words by supplier category code (supplier's own naming, not the retailer's)
SUP_BASE = {
    "DRY": ["Milk", "Yogurt", "Cheese", "Butter"],
    "PRD": ["Greens", "Fruit", "Roots", "Herbs"],
    "BKY": ["Bread", "Pastry", "Wrap"],
    "MET": ["Poultry", "Beef", "Pork", "Fish"],
    "BEV": ["Juice", "Water", "Soda", "Brew"],
    "FRZ": ["Frozen Meal", "Ice Cream", "Frozen Veg"],
    "PAN": ["Cereal", "Pasta", "Canned", "Snack"],
}


def get_retailer_product_city_keys() -> list[tuple[int, int]]:
    """Distinct (retailer product_id, city_id) pairs from the HF FreshRetailNet parquet."""
    from huggingface_hub import HfApi, hf_hub_download
    api = HfApi()
    files = [f for f in api.list_repo_files(HF_REPO, repo_type="dataset") if f.endswith(".parquet")]
    if not files:
        raise SystemExit(f"No parquet files in HF dataset {HF_REPO}")
    print(f"HF parquet files: {len(files)} (reading product/city keys)", flush=True)
    pairs: set[tuple[int, int]] = set()
    for fp in files:
        local = hf_hub_download(HF_REPO, fp, repo_type="dataset")
        t = pq.read_table(local, columns=["product_id", "city_id"])
        for pid, city in zip(t.column("product_id").to_pylist(), t.column("city_id").to_pylist()):
            pairs.add((int(pid), int(city)))
    print(f"distinct (retailer product_id, city_id): {len(pairs):,}", flush=True)
    return sorted(pairs)


def supplier_sku_and_desc(retailer_pid: int, tag: str) -> tuple[str, str]:
    """Deterministic supplier-OWN product code + description for a retailer product, per supplier.
    Same retailer_pid -> 3 different supplier codes/descriptions (one per profile tag)."""
    cat = SUPPLIER_CATS[retailer_pid % len(SUPPLIER_CATS)]
    bases = SUP_BASE[cat]
    base = bases[(retailer_pid // len(SUPPLIER_CATS)) % len(bases)]
    # supplier's own SKU code: <TAG>-<CAT>-<zero-padded retailer pid>  (supplier-specific, differs per tag)
    code = f"{tag}-{cat}-{retailer_pid:04d}"
    desc = DESC_STYLE[tag].format(base=base)
    return code, desc


def build_supplier_rows(pairs: list[tuple[int, int]]) -> pa.Table:
    # Capacity is scaled to the surge narrative: a top surge SKU forecasts ~50 units / 7 days.
    # available_qty is PER DAY. Daily base per profile -> 7-day capacity:
    #   S1 12/day (83/7d, $5.62): covers 50 comfortably, fastest, priciest
    #   S2 8/day  (55/7d, $4.50): covers 50, mid cost/lead
    #   S3 4/day  (28/7d, $3.82): CANNOT cover a 50 surge alone, cheapest
    # => "cheapest supplier that can actually cover the surge within lead time" is a real decision.
    DAILY_BASE = {"S1": 12, "S2": 8, "S3": 4}
    # Exception scenario (intentional): one surging SKU is deliberately under-supplied so that NO
    # single supplier can cover its 7-day forecast. This exercises the flow's human-review / ticket
    # (exception) path. Target = the headline surge SKU (retailer_product_id 122 in Boston / city 0),
    # which forecasts ~50 units/7d. Here every supplier's 7-day capacity is set below that
    # (S1~35, S2~28, S3~21), so the flow escalates it to a ticket instead of placing an order.
    UNDER_SUPPLIED = {(122, 0): {"S1": 5, "S2": 4, "S3": 3}}  # per-day base for this (product, city)
    rows = []
    for pid, city in pairs:
        city_name, region, state = CITY_GEO.get(city, (f"City {city}", "Unknown", "NA"))
        for tag, cap_mult, lead, cost_factor in PROFILES:
            sup_id = f"SUP_{tag}_{city:02d}"
            sup_name = f"Supplier {tag} ({city_name})"
            sku, desc = supplier_sku_and_desc(pid, tag)
            base_daily = UNDER_SUPPLIED.get((pid, city), DAILY_BASE)[tag]
            for di, d in enumerate(FORECAST_DATES):
                wobble = 1.0 + 0.08 * ((di % 3) - 1)  # -8%,0,+8% cycling; deterministic
                avail = int(round(base_daily * wobble))
                rows.append({
                    "supplier_id": sup_id,
                    "supplier_name": sup_name,
                    "supplier_product_id": sku,          # supplier's OWN product code
                    "supplier_product_desc": desc,       # supplier's OWN description (varies per supplier)
                    "retailer_product_id": pid,          # reconciliation key (GTIN/UPC in production)
                    "city_id": city,
                    "city_name": city_name,              # geographic labels the supplier feed serves
                    "region": region,
                    "state": state,
                    "dt": d.isoformat(),
                    "available_qty": avail,
                    "lead_time_days": lead,
                    "unit_cost": round(BASE_COST * cost_factor, 2),
                    "min_order_qty": 5,
                })
    schema = pa.schema([
        ("supplier_id", pa.string()), ("supplier_name", pa.string()),
        ("supplier_product_id", pa.string()), ("supplier_product_desc", pa.string()),
        ("retailer_product_id", pa.int64()), ("city_id", pa.int64()),
        ("city_name", pa.string()), ("region", pa.string()), ("state", pa.string()),
        ("dt", pa.string()),
        ("available_qty", pa.int64()), ("lead_time_days", pa.int64()),
        ("unit_cost", pa.float64()), ("min_order_qty", pa.int64()),
    ])
    tbl = pa.Table.from_pylist(rows, schema=schema)
    print(f"generated rows: {tbl.num_rows:,} | suppliers/city: {len(PROFILES)} | dates: {len(FORECAST_DATES)}",
          flush=True)
    return tbl


def write_to_s3tables(arrow: pa.Table) -> None:
    catalog = load_catalog("s3tables", **{
        "type": "rest", "uri": S3T_REST_URI, "warehouse": WAREHOUSE_ARN,
        "rest.sigv4-enabled": "true", "rest.signing-name": "s3tables", "rest.signing-region": REGION,
    })
    existing = {n[0] if isinstance(n, tuple) else n for n in catalog.list_namespaces()}
    if NAMESPACE not in existing:
        catalog.create_namespace(NAMESPACE)
        print(f"created namespace {NAMESPACE}", flush=True)
    ident = f"{NAMESPACE}.{TABLE}"
    if catalog.table_exists(ident):
        # S3 Tables requires purge on drop.
        try:
            catalog.purge_table(ident)
        except Exception:
            catalog.drop_table(ident, purge_requested=True)
        print(f"dropped existing {ident} (idempotent reload)", flush=True)
    t = catalog.create_table(ident, schema=arrow.schema)
    t.append(arrow)
    print(f"WROTE {arrow.num_rows:,} rows to {BUCKET}.{ident}", flush=True)
    back = catalog.load_table(ident).scan().to_arrow()
    print(f"VERIFY: {back.num_rows:,} rows readable from S3 Tables", flush=True)


def main() -> None:
    print(f"Target: {WAREHOUSE_ARN} -> {NAMESPACE}.{TABLE} (region {REGION})", flush=True)
    pairs = get_retailer_product_city_keys()
    arrow = build_supplier_rows(pairs)
    write_to_s3tables(arrow)
    # show one reconciliation example (same retailer product -> 3 supplier SKUs/descs)
    ex_pid = pairs[0][0]
    print(f"\nReconciliation example for retailer_product_id={ex_pid}:", flush=True)
    for tag, *_ in PROFILES:
        code, desc = supplier_sku_and_desc(ex_pid, tag)
        print(f"  {tag}: supplier_product_id={code} | desc='{desc}'", flush=True)
    print("DONE.", flush=True)


if __name__ == "__main__":
    main()
