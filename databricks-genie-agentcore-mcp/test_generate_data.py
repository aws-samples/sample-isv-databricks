"""Pins the safety and correctness rules generate_data.py documents at its top.

generate_data.py is the only script here that writes to the reader's Unity Catalog, so
its guarantees are the ones whose violation costs data rather than a failed deploy:

    sql_str()          backslash escaping -- Spark reads '' as concatenation, so the
                       wrong escape silently drops a quote instead of erroring
    seed_credentials() the DDL identity, and the flag that gates the GRANTs. Falling
                       back when only half the seed pair is set matters: deploy.py
                       writes DATABRICKS_CLIENT_ID/SECRET into the gateway's credential
                       provider, so an admin reused there would become the gateway
    *_exists()         case-insensitive checks -- Unity Catalog lowercases identifiers,
                       so `Genie_Demo` must not defeat the guard and cause a CREATE
    drop_seeded()      --drop refuses a schema this script did not record creating
    main()             refuses to touch pre-existing products/sales in an adopted schema
    _request()         keeps the response body, where Databricks puts the actionable
                       message, instead of raise_for_status() discarding it
    Sql.run()          bounded polling, and a failed statement raises rather than
                       returning an empty result that reads as success

No test framework and no dependency beyond the sample's own requirements.txt -- no
AWS account, no Databricks workspace, no network:

    pip install -r requirements.txt
    python -m unittest test_generate_data -v
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

import requests

import generate_data


class _FakeResponse:
    """Minimal stand-in for requests.Response as _request() uses it."""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = text
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _FakeSql:
    """Stands in for generate_data.Sql, recording every statement main() issues."""

    def __init__(self, catalogs=(), schemas=(), tables=(), scalars=None):
        self.statements = []
        self._catalogs = {c.lower() for c in catalogs}
        self._schemas = {s.lower() for s in schemas}
        self._tables = {t.lower() for t in tables}
        self._scalars = scalars or {}

    # -- the surface main() and drop_seeded() call -------------------------
    def execute(self, statement):
        self.statements.append(statement)

    def scalar(self, statement):
        self.statements.append(statement)
        return self._scalars.get(statement, "0")

    def catalog_exists(self, catalog):
        return catalog.lower() in self._catalogs

    def schema_exists(self, catalog, schema):
        return schema.lower() in self._schemas

    def table_exists(self, catalog, schema, table):
        return table.lower() in self._tables

    # -- helpers for assertions -------------------------------------------
    def issued(self, fragment):
        return [s for s in self.statements if fragment.upper() in s.upper()]


_real_open = open


class _FileFailingOnClose:
    """Opens the real file and fails only at close, which is where a full disk lands for a
    document this small: json.dump fills an 8 KiB buffer without touching the disk. It
    delegates to the real open on purpose, so a write that truncates in place still does."""

    def __init__(self, *args, **kwargs):
        self._f = _real_open(*args, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.close()
        else:
            # An exception is already in flight; close for real and let that one win.
            with contextlib.suppress(Exception):
                self._f.close()
        return False

    def __getattr__(self, name):
        # Forward flush/writelines/name/... so adding a call in write_seed_state surfaces as
        # a test result rather than an AttributeError from this double.
        return getattr(self._f, name)

    def close(self):
        # Close for real first so the fd is not leaked to GC. Note what this does and does
        # not model: the bytes DO reach the scratch file, so this is "the document landed,
        # then close reported a failure", not "nothing was written". What the tests assert
        # either way is that os.replace is never reached. Not in a finally: a real error
        # from close() should surface rather than be swallowed by the synthetic one.
        self._f.close()
        raise OSError("No space left on device")


def _json_double():
    """A module-local stand-in for generate_data's json reference. wraps= keeps load/dumps
    real, but JSONDecodeError has to be reassigned: wraps leaves it a Mock, and
    read_seed_state catches it, so a corrupt-file read would raise TypeError instead."""
    fake = mock.Mock(wraps=json)
    fake.JSONDecodeError = json.JSONDecodeError
    return fake


def _quiet(fn, *args, **kwargs):
    """Call fn, swallowing its progress prints."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


class SqlStrTest(unittest.TestCase):
    """Spark escapes with backslashes; '' is concatenation and would drop the quote."""

    def test_single_quote_is_backslash_escaped(self):
        self.assertEqual(generate_data.sql_str("Rosie's"), "'Rosie\\'s'")

    def test_single_quote_is_not_doubled(self):
        # The wrong escape ('') is silently wrong rather than an error, which is
        # exactly why it needs a test.
        self.assertNotIn("''", generate_data.sql_str("Rosie's"))

    def test_backslash_is_escaped(self):
        self.assertEqual(generate_data.sql_str("a\\b"), "'a\\\\b'")

    def test_trailing_backslash_cannot_escape_the_closing_quote(self):
        rendered = generate_data.sql_str("ends with\\")
        self.assertTrue(rendered.endswith("\\\\'"))
        # An unescaped trailing backslash would leave an odd number before the quote.
        self.assertEqual((len(rendered) - len(rendered.rstrip("\\'")) - 1) % 2, 0)

    def test_date_uses_the_date_literal_form(self):
        self.assertEqual(generate_data.sql_str(date(2026, 3, 14)), "DATE'2026-03-14'")

    def test_numbers_are_not_quoted(self):
        self.assertEqual(generate_data.sql_str(42), "42")
        self.assertEqual(generate_data.sql_str(12.5), "12.5")

    def test_values_clause_groups_each_row(self):
        clause = generate_data.values_clause([(1, "a"), (2, "b's")])
        self.assertEqual(clause, "(1, 'a'), (2, 'b\\'s')")


class SeedCredentialsTest(unittest.TestCase):
    """The DDL identity, and the flag that decides whether GRANTs run."""

    def test_dedicated_seed_identity_is_used_when_both_are_set(self):
        with mock.patch.multiple(
            generate_data,
            DATABRICKS_SEED_CLIENT_ID="seed-id",
            DATABRICKS_SEED_CLIENT_SECRET="seed-secret",
            DATABRICKS_CLIENT_ID="query-id",
            DATABRICKS_CLIENT_SECRET="query-secret",
        ):
            self.assertEqual(
                generate_data.seed_credentials(), ("seed-id", "seed-secret", True)
            )

    def test_falls_back_to_the_query_principal_when_seed_is_unset(self):
        with mock.patch.multiple(
            generate_data,
            DATABRICKS_SEED_CLIENT_ID="",
            DATABRICKS_SEED_CLIENT_SECRET="",
            DATABRICKS_CLIENT_ID="query-id",
            DATABRICKS_CLIENT_SECRET="query-secret",
        ):
            self.assertEqual(
                generate_data.seed_credentials(), ("query-id", "query-secret", False)
            )

    def test_half_a_seed_pair_falls_back_rather_than_sending_a_blank_secret(self):
        for seed_id, seed_secret in (("seed-id", ""), ("", "seed-secret")):
            with self.subTest(seed_id=seed_id, seed_secret=seed_secret):
                with mock.patch.multiple(
                    generate_data,
                    DATABRICKS_SEED_CLIENT_ID=seed_id,
                    DATABRICKS_SEED_CLIENT_SECRET=seed_secret,
                    DATABRICKS_CLIENT_ID="query-id",
                    DATABRICKS_CLIENT_SECRET="query-secret",
                ):
                    self.assertEqual(
                        generate_data.seed_credentials(),
                        ("query-id", "query-secret", False),
                    )


class ExistenceCheckTest(unittest.TestCase):
    """Unity Catalog lowercases identifiers, so these checks must be case-insensitive."""

    def _sql(self, rows):
        db = generate_data.Sql({}, "wh-1")
        db.run = lambda statement: rows  # noqa: ARG005 - statement unused by the stub
        return db

    def test_catalog_match_ignores_case(self):
        db = self._sql([["genie_demo"]])
        self.assertTrue(db.catalog_exists("Genie_Demo"))

    def test_catalog_absent_is_false(self):
        db = self._sql([["something_else"]])
        self.assertFalse(db.catalog_exists("genie_demo"))

    def test_schema_match_ignores_case(self):
        db = self._sql([["sales"]])
        self.assertTrue(db.schema_exists("genie_demo", "Sales"))

    def test_table_match_reads_the_table_name_column(self):
        # SHOW TABLES returns (database, tableName, isTemporary): reading column 0
        # would compare the schema name and never match.
        db = self._sql([["sales", "products", False]])
        self.assertTrue(db.table_exists("genie_demo", "sales", "products"))

    def test_table_check_does_not_match_the_database_column(self):
        db = self._sql([["products", "sales", False]])
        self.assertFalse(db.table_exists("genie_demo", "products", "products"))

    def test_short_rows_do_not_raise(self):
        db = self._sql([[], ["only-one-column"]])
        self.assertFalse(db.table_exists("genie_demo", "sales", "products"))


class RequestRetryTest(unittest.TestCase):
    """_request keeps the response body and bounds its retries."""

    def _run(self, responses):
        calls = {"n": 0}

        def fake_request(method, url, **kwargs):
            i = calls["n"]
            calls["n"] += 1
            item = responses[min(i, len(responses) - 1)]
            if isinstance(item, Exception):
                raise item
            return item

        with (
            mock.patch.object(generate_data.requests, "request", fake_request),
            mock.patch.object(generate_data.time, "sleep"),
        ):
            return generate_data._request("GET", "https://example/x"), calls["n"]

    def test_transient_status_is_retried_then_succeeds(self):
        ok = _FakeResponse(200, {"ok": True})
        resp, attempts = self._run([_FakeResponse(503), ok])
        self.assertIs(resp, ok)
        self.assertEqual(attempts, 2)

    def test_non_retryable_status_aborts_immediately(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run([_FakeResponse(403, text="warehouse access denied")])
        self.assertIn("403", str(ctx.exception))

    def test_the_response_body_survives_into_the_error(self):
        # raise_for_status() would discard resp.text, which is where Databricks puts
        # the message that explains the failure.
        with self.assertRaises(SystemExit) as ctx:
            self._run([_FakeResponse(403, text="PERMISSION_DENIED: needs CAN_USE")])
        self.assertIn("PERMISSION_DENIED: needs CAN_USE", str(ctx.exception))

    def test_retries_are_bounded(self):
        with self.assertRaises(SystemExit):
            self._run([_FakeResponse(503)])

    def test_connection_errors_are_retried_then_give_up(self):
        boom = requests.exceptions.ConnectionError("reset by peer")
        with self.assertRaises(SystemExit) as ctx:
            self._run([boom])
        self.assertIn("retries", str(ctx.exception))


class SqlRunTest(unittest.TestCase):
    """Polling is bounded, and a failed statement raises instead of returning no rows."""

    def _db(self, responses):
        calls = {"n": 0}

        def fake_request(method, url, **kwargs):
            i = calls["n"]
            calls["n"] += 1
            return responses[min(i, len(responses) - 1)]

        db = generate_data.Sql({"Authorization": "Bearer x"}, "wh-1")
        return db, fake_request

    def test_pending_is_polled_until_succeeded(self):
        responses = [
            _FakeResponse(200, {"statement_id": "s1", "status": {"state": "PENDING"}}),
            _FakeResponse(200, {"status": {"state": "RUNNING"}}),
            _FakeResponse(
                200,
                {"status": {"state": "SUCCEEDED"}, "result": {"data_array": [["7"]]}},
            ),
        ]
        db, fake_request = self._db(responses)
        with (
            mock.patch.object(generate_data.requests, "request", fake_request),
            mock.patch.object(generate_data.time, "sleep"),
        ):
            self.assertEqual(_quiet(db.run, "SELECT 1"), [["7"]])

    def test_failed_statement_raises_with_the_error_message(self):
        responses = [
            _FakeResponse(
                200,
                {
                    "statement_id": "s1",
                    "status": {
                        "state": "FAILED",
                        "error": {"message": "TABLE_OR_VIEW_NOT_FOUND"},
                    },
                },
            )
        ]
        db, fake_request = self._db(responses)
        with mock.patch.object(generate_data.requests, "request", fake_request):
            with self.assertRaises(SystemExit) as ctx:
                _quiet(db.run, "SELECT 1")
        self.assertIn("TABLE_OR_VIEW_NOT_FOUND", str(ctx.exception))

    def test_polling_stops_at_the_deadline(self):
        responses = [
            _FakeResponse(200, {"statement_id": "s1", "status": {"state": "RUNNING"}})
        ]
        db, fake_request = self._db(responses)
        clock = iter([0, 10**6, 10**6, 10**6])
        with (
            mock.patch.object(generate_data.requests, "request", fake_request),
            mock.patch.object(generate_data.time, "sleep"),
            mock.patch.object(generate_data.time, "monotonic", lambda: next(clock)),
        ):
            with self.assertRaises(SystemExit) as ctx:
                _quiet(db.run, "SELECT 1")
        self.assertIn("RUNNING", str(ctx.exception))

    def test_scalar_returns_none_on_an_empty_result(self):
        responses = [
            _FakeResponse(
                200, {"statement_id": "s1", "status": {"state": "SUCCEEDED"}}
            )
        ]
        db, fake_request = self._db(responses)
        with mock.patch.object(generate_data.requests, "request", fake_request):
            self.assertIsNone(_quiet(db.scalar, "SELECT count(*) FROM t"))


class SeedStateTest(unittest.TestCase):
    """seed_state.json is how --drop knows what this script created."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        os.close(fd)
        os.remove(self.path)
        patcher = mock.patch.object(generate_data, "SEED_STATE_FILE", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))
        # The two write-failure tests below raise between the write and the rename, so the
        # scratch file really is left behind and really does need removing.
        self.addCleanup(
            lambda: os.path.isfile(generate_data.seed_state_tmp_file())
            and os.remove(generate_data.seed_state_tmp_file())
        )

    def test_round_trip(self):
        generate_data.write_seed_state({"catalog": "c", "created_schema": True})
        self.assertEqual(
            generate_data.read_seed_state(), {"catalog": "c", "created_schema": True}
        )
        # The rename consumes the scratch file, and the destination is a NEW inode each
        # time. Both halves matter: a copy leaves the scratch behind, and a copy or an
        # in-place write keeps the original inode, which is the defect under guard.
        self.assertFalse(os.path.exists(generate_data.seed_state_tmp_file()))
        first = os.stat(self.path).st_ino
        generate_data.write_seed_state({"catalog": "second"})
        self.assertNotEqual(first, os.stat(self.path).st_ino)

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(generate_data.read_seed_state(), {})

    def test_corrupt_file_reads_as_empty_rather_than_raising(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertEqual(generate_data.read_seed_state(), {})

    def test_clear_is_idempotent(self):
        generate_data.clear_seed_state()  # no file yet
        generate_data.write_seed_state({"a": 1})
        generate_data.clear_seed_state()
        self.assertFalse(os.path.exists(self.path))

    def _assert_state_file_is(self, expected):
        """Assert the bytes on disk, not just what read_seed_state tolerates."""
        try:
            with open(self.path) as f:
                raw = f.read()
        except FileNotFoundError:
            self.fail("the previous state file is gone entirely")
        try:
            actual = json.loads(raw)
        except json.JSONDecodeError:
            self.fail(f"the previous state was destroyed; file holds {raw!r}")
        self.assertEqual(actual, expected)

    def test_a_write_that_fails_midway_leaves_the_previous_state_readable(self):
        good = {"catalog": "c", "created_schema": True}
        generate_data.write_seed_state(good)
        # Patch the module's own reference to json, not json.dump on the shared stdlib
        # module, so nothing outside generate_data sees a broken serializer.
        fake_json = _json_double()
        fake_json.dump.side_effect = OSError("No space left on device")
        with mock.patch.object(generate_data, "json", fake_json):
            with self.assertRaises(OSError):
                generate_data.write_seed_state({"catalog": "later"})
        self._assert_state_file_is(good)

    def test_the_json_double_keeps_a_real_decode_error(self):
        """Without this the double is a trap: read_seed_state catches json.JSONDecodeError,
        so a Mock in that slot turns a corrupt-file read into an unrelated TypeError."""
        with open(self.path, "w") as f:
            f.write("{not json")
        with mock.patch.object(generate_data, "json", _json_double()):
            self.assertEqual(generate_data.read_seed_state(), {})

    def test_a_write_that_fails_at_close_does_not_replace_good_state(self):
        good = {"catalog": "c", "created_schema": True}
        generate_data.write_seed_state(good)
        with mock.patch.object(generate_data, "open", _FileFailingOnClose, create=True):
            with self.assertRaises(OSError):
                generate_data.write_seed_state({"catalog": "later"})
        self._assert_state_file_is(good)

    def test_a_failed_write_really_does_abandon_a_scratch_file(self):
        """The premise behind clear_seed_state's second path and the .gitignore glob."""
        with mock.patch.object(generate_data, "open", _FileFailingOnClose, create=True):
            with self.assertRaises(OSError):
                generate_data.write_seed_state({"catalog": "later"})
        self.assertTrue(os.path.exists(generate_data.seed_state_tmp_file()))

    def test_clear_removes_both_files_in_one_call(self):
        generate_data.write_seed_state({"catalog": "c"})
        with mock.patch.object(generate_data, "open", _FileFailingOnClose, create=True):
            with self.assertRaises(OSError):
                generate_data.write_seed_state({"catalog": "later"})
        tmp = generate_data.seed_state_tmp_file()
        self.assertTrue(os.path.exists(self.path) and os.path.exists(tmp))
        generate_data.clear_seed_state()
        self.assertFalse(os.path.exists(self.path), "authoritative state survived")
        self.assertFalse(os.path.exists(tmp), "scratch file survived")

    def test_clear_reaches_the_authoritative_file_even_if_the_scratch_cannot_go(self):
        """Ordering guard: drop_seeded clears right after a CASCADE drop, so a surviving
        seed_state.json would let the next --drop reclaim a schema it never created."""
        generate_data.write_seed_state({"catalog": "c", "created_schema": True})
        os.mkdir(generate_data.seed_state_tmp_file())  # os.remove cannot take a directory
        self.addCleanup(
            lambda: os.path.isdir(generate_data.seed_state_tmp_file())
            and os.rmdir(generate_data.seed_state_tmp_file())
        )
        generate_data.clear_seed_state()
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(generate_data.read_seed_state(), {})


class DropSeededTest(unittest.TestCase):
    """--drop is the destructive path: it must refuse anything it did not create."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        os.close(fd)
        os.remove(self.path)
        patcher = mock.patch.object(generate_data, "SEED_STATE_FILE", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def _drop(self, db, state=None):
        if state is not None:
            generate_data.write_seed_state(state)
        return _quiet(
            generate_data.drop_seeded, db, "genie_demo", "sales", "genie_demo.sales", True
        )

    def _present(self):
        return _FakeSql(catalogs=["genie_demo"], schemas=["sales"])

    def test_refuses_when_there_is_no_state_at_all(self):
        db = self._present()
        with self.assertRaises(SystemExit) as ctx:
            self._drop(db)
        self.assertIn("no record that this script created it", str(ctx.exception))
        self.assertEqual(db.issued("DROP SCHEMA"), [])

    def test_refuses_when_the_state_names_another_schema(self):
        db = self._present()
        with self.assertRaises(SystemExit):
            self._drop(
                db,
                {"catalog": "genie_demo", "schema": "other", "created_schema": True},
            )
        self.assertEqual(db.issued("DROP SCHEMA"), [])

    def test_refuses_when_the_state_names_another_catalog(self):
        db = self._present()
        with self.assertRaises(SystemExit):
            self._drop(
                db,
                {"catalog": "other", "schema": "sales", "created_schema": True},
            )
        self.assertEqual(db.issued("DROP SCHEMA"), [])

    def test_refuses_when_the_schema_was_adopted_not_created(self):
        db = self._present()
        with self.assertRaises(SystemExit):
            self._drop(
                db,
                {"catalog": "genie_demo", "schema": "sales", "created_schema": False},
            )
        self.assertEqual(db.issued("DROP SCHEMA"), [])

    def test_drops_and_clears_state_when_we_created_it(self):
        db = self._present()
        self._drop(
            db, {"catalog": "genie_demo", "schema": "sales", "created_schema": True}
        )
        self.assertEqual(len(db.issued("DROP SCHEMA")), 1)
        self.assertEqual(generate_data.read_seed_state(), {})

    def test_ownership_match_ignores_case(self):
        db = self._present()
        self._drop(
            db, {"catalog": "Genie_Demo", "schema": "Sales", "created_schema": True}
        )
        self.assertEqual(len(db.issued("DROP SCHEMA")), 1)

    def test_absent_catalog_is_a_no_op_not_an_error(self):
        db = _FakeSql()
        self._drop(
            db, {"catalog": "genie_demo", "schema": "sales", "created_schema": True}
        )
        self.assertEqual(db.issued("DROP SCHEMA"), [])

    def test_absent_schema_is_a_no_op_not_an_error(self):
        db = _FakeSql(catalogs=["genie_demo"])
        self._drop(
            db, {"catalog": "genie_demo", "schema": "sales", "created_schema": True}
        )
        self.assertEqual(db.issued("DROP SCHEMA"), [])

    def test_declining_the_prompt_aborts_before_dropping(self):
        db = self._present()
        generate_data.write_seed_state(
            {"catalog": "genie_demo", "schema": "sales", "created_schema": True}
        )
        with mock.patch.object(generate_data, "input", create=True, return_value="n"):
            with self.assertRaises(SystemExit):
                _quiet(
                    generate_data.drop_seeded,
                    db,
                    "genie_demo",
                    "sales",
                    "genie_demo.sales",
                    False,
                )
        self.assertEqual(db.issued("DROP SCHEMA"), [])


class MainSeedingTest(unittest.TestCase):
    """main()'s own guards: adopted tables are never touched, and GRANTs are gated."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        os.close(fd)
        os.remove(self.path)
        for patcher in (
            mock.patch.object(generate_data, "SEED_STATE_FILE", self.path),
            mock.patch.object(generate_data, "DATABRICKS_HOST", "https://dbc-x.example"),
            mock.patch.object(generate_data, "DATABRICKS_CLIENT_ID", "query-id"),
            mock.patch.object(generate_data, "DATABRICKS_CLIENT_SECRET", "query-secret"),
            mock.patch.object(generate_data, "DATABRICKS_WAREHOUSE_ID", "wh-1"),
            mock.patch.object(generate_data, "DATABRICKS_CATALOG", "genie_demo"),
            mock.patch.object(generate_data, "DATABRICKS_SCHEMA", "sales"),
            mock.patch.object(generate_data, "mint_token", lambda *a: "token"),
            mock.patch.object(generate_data, "resolve_warehouse", lambda h: "wh-1"),
            mock.patch.object(sys, "argv", ["generate_data.py"]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def _run_main(self, db, seed_dedicated=False):
        seed = (
            {"DATABRICKS_SEED_CLIENT_ID": "seed-id", "DATABRICKS_SEED_CLIENT_SECRET": "s"}
            if seed_dedicated
            else {"DATABRICKS_SEED_CLIENT_ID": "", "DATABRICKS_SEED_CLIENT_SECRET": ""}
        )
        with (
            mock.patch.multiple(generate_data, **seed),
            mock.patch.object(generate_data, "Sql", lambda headers, wid: db),
        ):
            return _quiet(generate_data.main)

    def test_adopted_tables_abort_before_any_write(self):
        db = _FakeSql(
            catalogs=["genie_demo"], schemas=["sales"], tables=["products"]
        )
        with self.assertRaises(SystemExit) as ctx:
            self._run_main(db)
        self.assertIn("refusing to modify data this script didn't create", str(ctx.exception))
        # The point of the guard: nothing was created, inserted or dropped.
        self.assertEqual(db.issued("CREATE TABLE"), [])
        self.assertEqual(db.issued("INSERT INTO"), [])
        self.assertEqual(db.issued("DROP"), [])

    def test_adopted_sales_table_also_aborts(self):
        db = _FakeSql(catalogs=["genie_demo"], schemas=["sales"], tables=["sales"])
        with self.assertRaises(SystemExit):
            self._run_main(db)
        self.assertEqual(db.issued("INSERT INTO"), [])

    def test_empty_adopted_schema_is_seeded_without_creating_the_catalog(self):
        db = _FakeSql(catalogs=["genie_demo"], schemas=["sales"])
        self._run_main(db)
        self.assertEqual(db.issued("CREATE CATALOG"), [])
        self.assertEqual(db.issued("CREATE SCHEMA"), [])
        self.assertEqual(len(db.issued("CREATE TABLE")), 2)
        self.assertTrue(db.issued("INSERT INTO"))
        state = generate_data.read_seed_state()
        self.assertFalse(state["created_catalog"])
        self.assertFalse(state["created_schema"])

    def test_fresh_catalog_and_schema_are_recorded_as_ours(self):
        db = _FakeSql()
        self._run_main(db)
        self.assertEqual(len(db.issued("CREATE CATALOG")), 1)
        self.assertEqual(len(db.issued("CREATE SCHEMA")), 1)
        state = generate_data.read_seed_state()
        self.assertTrue(state["created_catalog"])
        self.assertTrue(state["created_schema"])
        self.assertEqual(state["tables"], ["products", "sales"])

    def test_grants_run_only_under_a_dedicated_seed_identity(self):
        db = _FakeSql()
        self._run_main(db, seed_dedicated=True)
        grants = db.issued("GRANT")
        self.assertEqual(len(grants), 3)
        # The grants must name the QUERY principal, not the seeding one.
        for grant in grants:
            self.assertIn("query-id", grant)
            self.assertNotIn("seed-id", grant)

    def test_no_grants_when_seeding_as_the_query_principal(self):
        db = _FakeSql()
        self._run_main(db, seed_dedicated=False)
        self.assertEqual(db.issued("GRANT"), [])


class RequireSeedConfigTest(unittest.TestCase):
    """Fail fast, naming every gap, before any token is minted."""

    _PRESENT = {
        "DATABRICKS_HOST": "https://dbc-x.example",
        "DATABRICKS_CLIENT_ID": "id",
        "DATABRICKS_CLIENT_SECRET": "secret",
        "DATABRICKS_WAREHOUSE_ID": "wh-1",
        "GENIE_SPACE_ID": "",
    }

    def test_all_present_does_not_raise(self):
        with mock.patch.multiple(generate_data, **self._PRESENT):
            generate_data.require_seed_config()

    def test_each_missing_value_is_named(self):
        for missing in ("DATABRICKS_HOST", "DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET"):
            values = dict(self._PRESENT, **{missing: ""})
            with self.subTest(missing=missing), mock.patch.multiple(generate_data, **values):
                with self.assertRaises(SystemExit) as ctx:
                    generate_data.require_seed_config()
                self.assertIn(missing, str(ctx.exception))

    def test_all_missing_are_listed_together(self):
        values = dict(
            self._PRESENT,
            DATABRICKS_HOST="",
            DATABRICKS_CLIENT_ID="",
            DATABRICKS_CLIENT_SECRET="",
        )
        with mock.patch.multiple(generate_data, **values):
            with self.assertRaises(SystemExit) as ctx:
                generate_data.require_seed_config()
        message = str(ctx.exception)
        for name in ("DATABRICKS_HOST", "DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET"):
            self.assertIn(name, message)

    def test_a_warehouse_source_is_required(self):
        values = dict(self._PRESENT, DATABRICKS_WAREHOUSE_ID="", GENIE_SPACE_ID="")
        with mock.patch.multiple(generate_data, **values):
            with self.assertRaises(SystemExit) as ctx:
                generate_data.require_seed_config()
        self.assertIn("DATABRICKS_WAREHOUSE_ID", str(ctx.exception))

    def test_genie_space_alone_satisfies_the_warehouse_requirement(self):
        values = dict(self._PRESENT, DATABRICKS_WAREHOUSE_ID="", GENIE_SPACE_ID="space-1")
        with mock.patch.multiple(generate_data, **values):
            generate_data.require_seed_config()


class ResolveWarehouseTest(unittest.TestCase):
    """An explicit warehouse wins; otherwise the space must name one."""

    def test_explicit_warehouse_short_circuits_the_lookup(self):
        def explode(*a, **k):  # pragma: no cover - must not be called
            raise AssertionError("no HTTP call should happen when the id is set")

        with (
            mock.patch.object(generate_data, "DATABRICKS_WAREHOUSE_ID", "wh-explicit"),
            mock.patch.object(generate_data.requests, "request", explode),
        ):
            self.assertEqual(generate_data.resolve_warehouse({}), "wh-explicit")

    def test_space_without_a_warehouse_aborts_with_guidance(self):
        response = _FakeResponse(200, {})
        with (
            mock.patch.object(generate_data, "DATABRICKS_WAREHOUSE_ID", ""),
            mock.patch.object(generate_data, "GENIE_SPACE_ID", "space-1"),
            mock.patch.object(
                generate_data.requests, "request", lambda *a, **k: response
            ),
        ):
            with self.assertRaises(SystemExit) as ctx:
                generate_data.resolve_warehouse({})
        self.assertIn("DATABRICKS_WAREHOUSE_ID", str(ctx.exception))


class GeneratedDatasetTest(unittest.TestCase):
    """The dataset has to answer the questions the sample ships with."""

    @classmethod
    def setUpClass(cls):
        cls.sales = generate_data.gen_sales()

    def test_spans_enough_history_for_last_fiscal_year_questions(self):
        days = [row[3] for row in self.sales]
        self.assertGreaterEqual((max(days) - min(days)).days, 500)

    def test_covers_the_last_quarter(self):
        recent = [r for r in self.sales if r[3] >= date.today() - timedelta(days=90)]
        self.assertTrue(recent)

    def test_every_region_appears(self):
        self.assertEqual({row[2] for row in self.sales}, set(generate_data.REGIONS))

    def test_revenue_is_quantity_times_the_product_unit_price(self):
        prices = {p[0]: p[3] for p in generate_data.PRODUCTS}
        for sale_id, product_id, _region, _day, qty, revenue in self.sales:
            with self.subTest(sale_id=sale_id):
                self.assertEqual(revenue, round(qty * prices[product_id], 2))

    def test_sale_ids_are_unique(self):
        ids = [row[0] for row in self.sales]
        self.assertEqual(len(ids), len(set(ids)))

    def test_dataset_is_deterministic(self):
        # A fixed seed is what lets the README quote stable answers.
        generate_data.random.seed(42)
        first = generate_data.gen_sales()
        generate_data.random.seed(42)
        self.assertEqual(generate_data.gen_sales(), first)


if __name__ == "__main__":
    unittest.main()
