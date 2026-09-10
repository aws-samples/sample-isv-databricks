"""Populate a tiny sample lakehouse so a Genie space can answer the demo questions.

Creates one catalog / one schema / two small tables (`products` and `sales`) in
Unity Catalog and seeds them with a few hundred deterministic rows — just enough
to answer the questions this sample ships with, e.g.:

    python invoke.py "What were our top 5 products by revenue last quarter?"
    python invoke.py "Break down sales by region for the last fiscal year."

It talks to Databricks over the SQL Statement Execution API using the same
OAuth2 M2M service principal as the rest of the sample, so it needs no extra
dependencies (just `requests`) and no PAT.

The service principal running this must be able to CREATE the catalog/schema/tables
(catalog owner, or `CREATE CATALOG` on the metastore + `CREATE SCHEMA`/`CREATE TABLE`).
If your catalog already exists and is owned elsewhere, set DATABRICKS_CATALOG to it
and grant the SP `CREATE SCHEMA` there, or pre-create the schema.

After it runs, add `<catalog>.<schema>.products` and `<catalog>.<schema>.sales`
to your Genie space as data assets (Genie UI), and make sure the SP has the
Genie-space / warehouse / Unity Catalog grants from the README.

Usage:
    python generate_data.py
    python generate_data.py --drop      # drop the schema first, then recreate

Requires (same env as the rest of the sample):
    DATABRICKS_HOST, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET
    GENIE_SPACE_ID           (used only to auto-resolve the warehouse)
Optional:
    DATABRICKS_WAREHOUSE_ID  (skip the space lookup)
    DATABRICKS_CATALOG       (default: genie_demo)
    DATABRICKS_SCHEMA        (default: sales)
"""

import argparse
import random
import time
from datetime import date, timedelta

import requests
from config import (
    DATABRICKS_CATALOG,
    DATABRICKS_CLIENT_ID,
    DATABRICKS_CLIENT_SECRET,
    DATABRICKS_HOST,
    DATABRICKS_SCHEMA,
    DATABRICKS_WAREHOUSE_ID,
    GENIE_SPACE_ID,
    require_databricks_config,
)

random.seed(42)  # deterministic dataset across runs

# --- The tiny catalog -------------------------------------------------------
# 8 products, 4 regions, ~18 months of sales — small on purpose. Each product
# has a fixed unit price so revenue = quantity * unit_price is explainable.
PRODUCTS = [
    # (product_id, product_name, category, unit_price)
    (1, "Espresso Beans", "Beverage", 12.50),
    (2, "Butter Croissant", "Bakery", 3.25),
    (3, "Sourdough Loaf", "Bakery", 6.00),
    (4, "Blueberry Muffin", "Bakery", 2.75),
    (5, "Green Tea", "Beverage", 4.00),
    (6, "Chocolate Cake", "Dessert", 28.00),
    (7, "Sesame Bagel", "Bakery", 1.95),
    (8, "Latte Mix", "Beverage", 9.75),
]
REGIONS = ["North", "South", "East", "West"]


def mint_token() -> str:
    """OAuth2 client-credentials token for the Databricks service principal."""
    resp = requests.post(
        f"{DATABRICKS_HOST}/oidc/v1/token",
        auth=(DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET),
        data={"grant_type": "client_credentials", "scope": "all-apis"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def resolve_warehouse(headers: dict) -> str:
    """Use DATABRICKS_WAREHOUSE_ID if set, else the warehouse behind the space."""
    if DATABRICKS_WAREHOUSE_ID:
        return DATABRICKS_WAREHOUSE_ID
    if not GENIE_SPACE_ID:
        raise SystemExit(
            "No DATABRICKS_WAREHOUSE_ID and no GENIE_SPACE_ID set — cannot pick a "
            "SQL warehouse to run against."
        )
    resp = requests.get(
        f"{DATABRICKS_HOST}/api/2.0/genie/spaces/{GENIE_SPACE_ID}",
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    wid = resp.json().get("warehouse_id")
    if not wid:
        raise SystemExit(
            f"Genie space {GENIE_SPACE_ID} returned no warehouse_id — set "
            "DATABRICKS_WAREHOUSE_ID explicitly."
        )
    return wid


class Sql:
    """Run statements via the SQL Statement Execution API and wait for results."""

    def __init__(self, headers: dict, warehouse_id: str):
        self.headers = headers
        self.warehouse_id = warehouse_id

    def run(self, statement: str) -> list:
        """Execute a statement, wait for it, and return the result rows."""
        resp = requests.post(
            f"{DATABRICKS_HOST}/api/2.0/sql/statements",
            headers=self.headers,
            json={"warehouse_id": self.warehouse_id, "statement": statement, "wait_timeout": "30s"},
            timeout=60,
        )
        resp.raise_for_status()
        body = resp.json()
        statement_id = body.get("statement_id")
        state = body["status"]["state"]

        # A long statement can still be running when wait_timeout elapses; poll.
        while state in ("PENDING", "RUNNING"):
            time.sleep(2)
            poll = requests.get(
                f"{DATABRICKS_HOST}/api/2.0/sql/statements/{statement_id}",
                headers=self.headers,
                timeout=30,
            )
            poll.raise_for_status()
            body = poll.json()
            state = body["status"]["state"]

        if state != "SUCCEEDED":
            err = body["status"].get("error", {}).get("message", "unknown error")
            raise SystemExit(f"Statement failed ({state}): {err}\n  SQL: {statement[:200]}")
        return body.get("result", {}).get("data_array", []) or []

    def execute(self, statement: str) -> None:
        self.run(statement)

    def catalog_exists(self, catalog: str) -> bool:
        # CREATE CATALOG IF NOT EXISTS still requires the metastore CREATE CATALOG
        # privilege even when the catalog is already there, so check first and
        # only create when genuinely absent — lets the SP target an existing
        # catalog it merely has CREATE SCHEMA on.
        rows = self.run("SHOW CATALOGS")
        return any(r and r[0] == catalog for r in rows)


def sql_str(value) -> str:
    """Quote a Python value as a SQL literal (str/int/float/date only)."""
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, date):
        return f"DATE'{value.isoformat()}'"
    return str(value)


def gen_sales() -> list[tuple]:
    """~18 months of sales so 'last quarter' and 'last fiscal year' both return rows."""
    today = date.today()
    start = today - timedelta(days=550)
    rows = []
    sale_id = 1
    day = start
    while day <= today:
        # A few sales per week, spread across regions and products.
        if day.weekday() in (0, 2, 4):  # Mon/Wed/Fri
            for region in REGIONS:
                for _ in range(random.randint(1, 2)):
                    pid, _name, _cat, price = random.choice(PRODUCTS)
                    qty = random.randint(5, 60)
                    revenue = round(qty * price, 2)
                    rows.append((sale_id, pid, region, day, qty, revenue))
                    sale_id += 1
        day += timedelta(days=1)
    return rows


def values_clause(rows: list[tuple]) -> str:
    return ", ".join("(" + ", ".join(sql_str(v) for v in row) + ")" for row in rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drop", action="store_true", help="drop the schema before recreating")
    args = parser.parse_args()

    require_databricks_config()
    catalog, schema = DATABRICKS_CATALOG, DATABRICKS_SCHEMA
    fq = f"{catalog}.{schema}"

    token = mint_token()
    headers = {"Authorization": f"Bearer {token}"}
    warehouse_id = resolve_warehouse(headers)
    print(f"Using warehouse {warehouse_id}; target {fq}")
    db = Sql(headers, warehouse_id)

    if args.drop:
        print(f"Dropping schema {fq} ...")
        db.execute(f"DROP SCHEMA IF EXISTS {fq} CASCADE")

    print("Creating catalog / schema / tables ...")
    if db.catalog_exists(catalog):
        print(f"  Catalog {catalog} already exists — using it.")
    else:
        db.execute(f"CREATE CATALOG IF NOT EXISTS {catalog}")
    db.execute(f"CREATE SCHEMA IF NOT EXISTS {fq}")
    db.execute(
        f"CREATE TABLE IF NOT EXISTS {fq}.products ("
        "product_id INT, product_name STRING, category STRING, unit_price DECIMAL(10,2)) "
        "COMMENT 'Product catalog for the Genie sample'"
    )
    db.execute(
        f"CREATE TABLE IF NOT EXISTS {fq}.sales ("
        "sale_id INT, product_id INT, region STRING, sale_date DATE, "
        "quantity INT, revenue DECIMAL(12,2)) "
        "COMMENT 'Line-item sales used by the Genie demo questions'"
    )

    print("Loading products ...")
    db.execute(f"DELETE FROM {fq}.products")
    product_rows = [(p[0], p[1], p[2], p[3]) for p in PRODUCTS]
    db.execute(f"INSERT INTO {fq}.products VALUES {values_clause(product_rows)}")

    print("Loading sales ...")
    db.execute(f"DELETE FROM {fq}.sales")
    sales = gen_sales()
    for i in range(0, len(sales), 200):  # batch inserts
        db.execute(f"INSERT INTO {fq}.sales VALUES {values_clause(sales[i:i + 200])}")
    print(f"  {len(product_rows)} products, {len(sales)} sales rows")

    # Let the query service principal read the data through Genie. Requires the
    # SP running this to own the objects (it does if it just created them).
    print(f"Granting SELECT to the service principal ({DATABRICKS_CLIENT_ID}) ...")
    grants = [
        f"GRANT USE CATALOG ON CATALOG {catalog} TO `{DATABRICKS_CLIENT_ID}`",
        f"GRANT USE SCHEMA ON SCHEMA {fq} TO `{DATABRICKS_CLIENT_ID}`",
        f"GRANT SELECT ON SCHEMA {fq} TO `{DATABRICKS_CLIENT_ID}`",
    ]
    for g in grants:
        try:
            db.execute(g)
        except SystemExit as exc:
            print(f"  Skipped a grant (grant it manually if needed): {exc}")

    print(
        f"\nDone. Now add {fq}.products and {fq}.sales to your Genie space "
        "(Genie UI → the space → data assets), then run:\n"
        '  python invoke.py "What were our top 5 products by revenue last quarter?"'
    )


if __name__ == "__main__":
    main()
