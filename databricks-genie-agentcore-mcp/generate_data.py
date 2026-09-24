"""Populate a tiny sample lakehouse so a Genie space can answer the demo questions.

Creates one catalog / one schema / two small tables (`products` and `sales`) in
Unity Catalog and seeds them with ~1,400 rows — just enough to answer the
questions this sample ships with, e.g.:

    python invoke.py "What were our top 5 products by revenue last quarter?"
    python invoke.py "Break down sales by region for the last fiscal year."

It talks to Databricks over the SQL Statement Execution API using `requests`, so
it needs no extra dependencies and no PAT.

Which identity runs the DDL
---------------------------
Creating a catalog/schema/tables needs privileges the *query* service principal
(DATABRICKS_CLIENT_ID) usually does not have. Set a SEPARATE seeding identity:

    DATABRICKS_SEED_CLIENT_ID / DATABRICKS_SEED_CLIENT_SECRET

The script runs its DDL as that identity and then GRANTs the query service
principal read access. Keeping it separate matters: deploy.py writes
DATABRICKS_CLIENT_ID/SECRET into the gateway's outbound credential provider, so
reusing those as an admin would silently make the gateway run as that admin. If
the seed credentials are unset the script falls back to the query service
principal (fine when it happens to own the target catalog).

Safety
------
- It refuses to modify `products`/`sales` tables it did not create.
- `--drop` only removes a schema this script recorded creating, and prompts first
  (use `--yes` to skip the prompt). It records what it created in `seed_state.json`.

Usage:
    python generate_data.py                 # create + load
    python generate_data.py --drop          # drop what we created, then recreate
    python generate_data.py --drop --yes    # ... without the confirmation prompt

Requires:
    DATABRICKS_HOST, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET
    and at least one of DATABRICKS_WAREHOUSE_ID or GENIE_SPACE_ID (to pick a warehouse;
    DATABRICKS_WAREHOUSE_ID wins if both are set)
Optional:
    DATABRICKS_SEED_CLIENT_ID / DATABRICKS_SEED_CLIENT_SECRET  (separate DDL identity)
    DATABRICKS_CATALOG   (default: genie_demo)
    DATABRICKS_SCHEMA    (default: sales)
"""

import argparse
import json
import os
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
    DATABRICKS_SEED_CLIENT_ID,
    DATABRICKS_SEED_CLIENT_SECRET,
    DATABRICKS_WAREHOUSE_ID,
    GENIE_SPACE_ID,
    STATE_FILE,
)

random.seed(42)  # fixed-seed dataset

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

# generate_data.py owns this file; it is independent of the AWS ownership state in
# STATE_FILE (gateway_config.json), whose contract cleanup.py depends on.
SEED_STATE_FILE = os.path.join(os.path.dirname(STATE_FILE), "seed_state.json")

# Transient HTTP statuses worth retrying on a shared warehouse.
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 5
_POLL_DEADLINE_S = 300  # a stopped warehouse cold-starts in ~1-2 min; cap the wait.


def _request(method: str, url: str, **kwargs) -> requests.Response:
    """HTTP with bounded backoff on transient errors, preserving the response body.

    raise_for_status() throws away resp.text, which is where Databricks puts the
    actionable message (a 403 for missing warehouse access explains itself there).
    """
    delay = 1.0
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.exceptions.RequestException as exc:
            # Connection reset / timeout / DNS blip — retry a few times, then give up.
            if attempt < _MAX_RETRIES:
                time.sleep(delay)
                delay = min(delay * 2, 16)
                continue
            raise SystemExit(f"{method} {url} failed after {_MAX_RETRIES} retries: {exc}")
        if resp.status_code in _RETRY_STATUS and attempt < _MAX_RETRIES:
            time.sleep(delay)
            delay = min(delay * 2, 16)
            continue
        if not resp.ok:
            raise SystemExit(f"{method} {url} -> HTTP {resp.status_code}: {resp.text[:600]}")
        return resp
    raise SystemExit(f"{method} {url}: exhausted retries")  # unreachable


def seed_credentials() -> tuple[str, str, bool]:
    """(client_id, client_secret, is_dedicated) for the identity that runs the DDL."""
    if DATABRICKS_SEED_CLIENT_ID and DATABRICKS_SEED_CLIENT_SECRET:
        return DATABRICKS_SEED_CLIENT_ID, DATABRICKS_SEED_CLIENT_SECRET, True
    return DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET, False


def mint_token(client_id: str, client_secret: str) -> str:
    """OAuth2 client-credentials token for the given Databricks service principal."""
    resp = _request(
        "POST",
        f"{DATABRICKS_HOST}/oidc/v1/token",
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials", "scope": "all-apis"},
        timeout=30,
    )
    return resp.json()["access_token"]


def resolve_warehouse(headers: dict) -> str:
    """Use DATABRICKS_WAREHOUSE_ID if set, else the warehouse behind the space."""
    if DATABRICKS_WAREHOUSE_ID:
        return DATABRICKS_WAREHOUSE_ID
    resp = _request(
        "GET",
        f"{DATABRICKS_HOST}/api/2.0/genie/spaces/{GENIE_SPACE_ID}",
        headers=headers,
        timeout=30,
    )
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
        """Execute a statement, wait (bounded) for it, and return the result rows."""
        resp = _request(
            "POST",
            f"{DATABRICKS_HOST}/api/2.0/sql/statements",
            headers=self.headers,
            json={
                "warehouse_id": self.warehouse_id,
                "statement": statement,
                "wait_timeout": "30s",
            },
            timeout=60,
        )
        body = resp.json()
        statement_id = body.get("statement_id")
        state = body["status"]["state"]

        # A long statement (or a cold warehouse) can still be running when
        # wait_timeout elapses; poll, but never hang forever.
        deadline = time.monotonic() + _POLL_DEADLINE_S
        waited = 0
        while state in ("PENDING", "RUNNING"):
            if time.monotonic() > deadline:
                raise SystemExit(
                    f"Statement {statement_id} still {state} after {_POLL_DEADLINE_S}s "
                    "(the SQL warehouse may be slow to start or overloaded).\n"
                    f"  SQL: {statement[:200]}"
                )
            time.sleep(2)
            waited += 2
            if waited % 20 == 0:
                print(f"  ... still running ({waited}s)")
            poll = _request(
                "GET",
                f"{DATABRICKS_HOST}/api/2.0/sql/statements/{statement_id}",
                headers=self.headers,
                timeout=30,
            )
            body = poll.json()
            state = body["status"]["state"]

        if state != "SUCCEEDED":
            err = body["status"].get("error", {}).get("message", "unknown error")
            raise SystemExit(f"Statement failed ({state}): {err}\n  SQL: {statement[:200]}")
        return body.get("result", {}).get("data_array", []) or []

    def execute(self, statement: str) -> None:
        self.run(statement)

    def scalar(self, statement: str) -> str:
        rows = self.run(statement)
        return rows[0][0] if rows and rows[0] else None

    def catalog_exists(self, catalog: str) -> bool:
        # CREATE CATALOG IF NOT EXISTS still requires the metastore CREATE CATALOG
        # privilege even when the catalog is already there, so check first and only
        # create when genuinely absent. Unity Catalog lowercases identifiers, so the
        # comparison must be case-insensitive or `Genie_Demo` would defeat the check.
        rows = self.run("SHOW CATALOGS")
        return any(r and r[0].lower() == catalog.lower() for r in rows)

    def schema_exists(self, catalog: str, schema: str) -> bool:
        rows = self.run(f"SHOW SCHEMAS IN {catalog}")
        return any(r and r[0].lower() == schema.lower() for r in rows)

    def table_exists(self, catalog: str, schema: str, table: str) -> bool:
        # SHOW TABLES returns (database, tableName, isTemporary).
        rows = self.run(f"SHOW TABLES IN {catalog}.{schema}")
        return any(len(r) >= 2 and r[1].lower() == table.lower() for r in rows)


def sql_str(value) -> str:
    """Quote a Python value as a SQL literal (str/int/float/date only).

    Databricks/Spark SQL escapes with backslashes, NOT by doubling the quote: `''`
    is read as string concatenation, so it silently drops the quote. Backslashes
    themselves must be escaped or a trailing `\\` would escape the closing quote.
    """
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return "'" + escaped + "'"
    if isinstance(value, date):
        return f"DATE'{value.isoformat()}'"
    return str(value)


def gen_sales() -> list:
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


def values_clause(rows: list) -> str:
    return ", ".join("(" + ", ".join(sql_str(v) for v in row) + ")" for row in rows)


# --- Seed-state bookkeeping (created-vs-adopted) ----------------------------
def read_seed_state() -> dict:
    try:
        with open(SEED_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def seed_state_tmp_file() -> str:
    """The scratch path write_seed_state renames from. Derived at call time, so it follows
    SEED_STATE_FILE and there is one spelling of the suffix rather than one per caller."""
    return f"{SEED_STATE_FILE}.tmp"


def write_seed_state(state: dict) -> None:
    # Write-then-rename, the same reason deploy.py's write_state does it: a plain write
    # truncates first, so a failed or interrupted write leaves invalid JSON,
    # read_seed_state reads that as {}, and --drop then refuses to reclaim the schema this
    # script created.
    #
    # This covers the write itself, not every way the state can go missing. main() still
    # calls this once, after the catalog, schema and both tables are created, so a run that
    # dies during that stretch exits with real objects and no state at all, and --drop
    # refuses for the same reason. (--drop also only ever drops the schema; the catalog is
    # left standing either way, which is why created_catalog has no reader today.)
    tmp = seed_state_tmp_file()
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, SEED_STATE_FILE)


def clear_seed_state() -> None:
    # The authoritative file goes first and its failures are not swallowed. drop_seeded
    # calls this straight after a CASCADE drop, so a seed_state.json that survives still
    # claims ownership of a schema that no longer exists: recreate that schema by hand and
    # the next --drop would CASCADE something this script never made. The scratch file is
    # only litter, so removing it is best effort and cannot mask a failure on the real one.
    try:
        os.remove(SEED_STATE_FILE)
    except FileNotFoundError:
        pass
    try:
        os.remove(seed_state_tmp_file())
    except OSError:
        pass


def require_seed_config() -> None:
    """Validate what THIS script needs — not GENIE_SPACE_ID, which has an alternative."""
    missing = [
        name
        for name, value in (
            ("DATABRICKS_HOST", DATABRICKS_HOST),
            ("DATABRICKS_CLIENT_ID", DATABRICKS_CLIENT_ID),
            ("DATABRICKS_CLIENT_SECRET", DATABRICKS_CLIENT_SECRET),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + "\nSee the Configuration section of README.md."
        )
    if not (DATABRICKS_WAREHOUSE_ID or GENIE_SPACE_ID):
        raise SystemExit(
            "Set DATABRICKS_WAREHOUSE_ID or GENIE_SPACE_ID so a SQL warehouse can be chosen."
        )


def confirm(action: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    if input(f"{action} [y/N] ").strip().lower() not in ("y", "yes"):
        raise SystemExit("Aborted.")


def drop_seeded(db: Sql, catalog: str, schema: str, fq: str, assume_yes: bool) -> None:
    """Drop the schema ONLY if this script recorded creating it."""
    if not db.catalog_exists(catalog):
        print(f"Nothing to drop: catalog {catalog} does not exist.")
        return
    if not db.schema_exists(catalog, schema):
        print(f"Nothing to drop: schema {fq} does not exist.")
        return
    state = read_seed_state()
    ours = (
        state.get("catalog", "").lower() == catalog.lower()
        and state.get("schema", "").lower() == schema.lower()
        and state.get("created_schema")
    )
    if not ours:
        raise SystemExit(
            f"Refusing to --drop {fq}: no record that this script created it, so it "
            "may hold data you care about. If you are sure, drop it manually:\n"
            f"  DROP SCHEMA {fq} CASCADE"
        )
    print(f"--drop will CASCADE-drop schema {fq} and every table/row in it.")
    confirm("Proceed?", assume_yes)
    db.execute(f"DROP SCHEMA IF EXISTS {fq} CASCADE")
    clear_seed_state()
    print(f"  Dropped {fq}.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--drop", action="store_true", help="drop what we created, then recreate")
    parser.add_argument("--yes", action="store_true", help="skip confirmation prompts")
    args = parser.parse_args()

    require_seed_config()
    catalog, schema = DATABRICKS_CATALOG, DATABRICKS_SCHEMA
    fq = f"{catalog}.{schema}"

    client_id, client_secret, dedicated = seed_credentials()
    token = mint_token(client_id, client_secret)
    headers = {"Authorization": f"Bearer {token}"}
    warehouse_id = resolve_warehouse(headers)
    print(f"Using warehouse {warehouse_id}; target {fq}")
    if not dedicated:
        print(
            "  Seeding as the query service principal (no DATABRICKS_SEED_CLIENT_ID set). "
            "Set a separate seed identity if it lacks CREATE privileges."
        )
    db = Sql(headers, warehouse_id)

    if args.drop:
        drop_seeded(db, catalog, schema, fq, args.yes)

    # Ensure the catalog exists first — a later CREATE SCHEMA in a missing catalog fails.
    print("Creating catalog / schema / tables ...")
    created_catalog = False
    if db.catalog_exists(catalog):
        print(f"  Catalog {catalog} already exists — using it.")
    else:
        db.execute(f"CREATE CATALOG {catalog}")
        created_catalog = True

    created_schema = False
    if db.schema_exists(catalog, schema):
        # Adopted schema: refuse to touch tables we did not create (a positional
        # INSERT into a reader's pre-existing table would fail, or worse, corrupt it).
        for table in ("products", "sales"):
            if db.table_exists(catalog, schema, table):
                raise SystemExit(
                    f"{fq}.{table} already exists and was not created by this run — "
                    "refusing to modify data this script didn't create.\n"
                    f"Re-run with --drop to replace the '{schema}' schema, or set "
                    "DATABRICKS_SCHEMA to an unused schema name."
                )
        print(f"  Schema {fq} already exists — using it.")
    else:
        db.execute(f"CREATE SCHEMA {fq}")
        created_schema = True

    # Tables are freshly created here (we aborted above if they already existed), so
    # there is no DELETE-then-INSERT that could empty a reader's real table.
    db.execute(
        f"CREATE TABLE {fq}.products ("
        "product_id INT, product_name STRING, category STRING, unit_price DECIMAL(10,2)) "
        "COMMENT 'Product catalog for the Genie sample'"
    )
    db.execute(
        f"CREATE TABLE {fq}.sales ("
        "sale_id INT, product_id INT, region STRING, sale_date DATE, "
        "quantity INT, revenue DECIMAL(12,2)) "
        "COMMENT 'Line-item sales used by the Genie demo questions'"
    )
    write_seed_state(
        {
            "catalog": catalog,
            "schema": schema,
            "tables": ["products", "sales"],
            "created_catalog": created_catalog,
            "created_schema": created_schema,
        }
    )

    print("Loading products ...")
    product_rows = [(p[0], p[1], p[2], p[3]) for p in PRODUCTS]
    db.execute(f"INSERT INTO {fq}.products VALUES {values_clause(product_rows)}")

    print("Loading sales ...")
    sales = gen_sales()
    for i in range(0, len(sales), 200):  # batched inserts (each retried by _request)
        db.execute(f"INSERT INTO {fq}.sales VALUES {values_clause(sales[i:i + 200])}")

    # Report measured counts, not the intended row count.
    n_products = db.scalar(f"SELECT count(*) FROM {fq}.products")
    n_sales = db.scalar(f"SELECT count(*) FROM {fq}.sales")
    print(f"  loaded {n_products} products, {n_sales} sales rows")

    # Grant the QUERY service principal read access. Meaningful only when a separate
    # seed identity ran the DDL; a no-op self-grant otherwise.
    if dedicated:
        print("Granting read access to the query service principal ...")
        grants = [
            f"GRANT USE CATALOG ON CATALOG {catalog} TO `{DATABRICKS_CLIENT_ID}`",
            f"GRANT USE SCHEMA ON SCHEMA {fq} TO `{DATABRICKS_CLIENT_ID}`",
            f"GRANT SELECT ON SCHEMA {fq} TO `{DATABRICKS_CLIENT_ID}`",
        ]
        failed = False
        for grant in grants:
            try:
                db.execute(grant)
            except SystemExit as exc:
                failed = True
                print(f"  WARNING: grant failed, apply it manually: {exc}")
        if failed:
            print("  Some grants failed — the query service principal may not be able to read the data.")
    else:
        print(
            "Skipping grants: seeded as the query service principal, which already owns "
            "the objects. (Set DATABRICKS_SEED_CLIENT_ID to seed under a separate identity.)"
        )

    print(
        f"\nDone. Now add {fq}.products and {fq}.sales to your Genie space "
        "(Genie UI -> the space -> data assets), then run:\n"
        '  python invoke.py "What were our top 5 products by revenue last quarter?"'
    )


if __name__ == "__main__":
    main()
