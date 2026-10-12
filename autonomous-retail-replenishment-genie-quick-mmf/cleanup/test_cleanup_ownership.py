"""Hermetic tests for what cleanup.sh is allowed to delete in Databricks.

Runs the real cleanup/cleanup.sh and scripts/setup_databricks.sh against fake `aws` and
`databricks` executables placed first on PATH. The fakes record every call, so each test asserts
on the deletes the script actually issued, not on what it printed; tests about what cleanup keeps
also check what it tells the reader. No AWS account, no Databricks workspace and no network are
needed; bash and jq are.

    cd autonomous-retail-replenishment-genie-quick-mmf
    python3 -m unittest discover -s cleanup -p "test_*.py" -v
"""

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

SAMPLE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLEANUP = os.path.join(SAMPLE_DIR, "cleanup", "cleanup.sh")
SETUP = os.path.join(SAMPLE_DIR, "scripts", "setup_databricks.sh")
SCRIPTED_TITLE = "Supply Chain Demand Forecasting (Chronos-2)"

FAKE_AWS = textwrap.dedent("""\
    #!/usr/bin/env bash
    echo "aws $*" >> "$FAKE_LOG"
    case "$1 $2" in
      "quicksight describe-account-subscription") echo UNSUBSCRIBED ;;
      "quicksight list-"*) echo None ;;
      "iam get-role") exit 254 ;;
    esac
    exit 0
    """)

# list-spaces serves spaces_first.json, or spaces_<token>.json for --page-token <token>; a missing
# page file exits 1, which is how a test simulates a page the CLI could not read. warehouses create
# returns wh-created, then wh-created-2, wh-created-3, so a second create is visible. create-space
# fails once if the create_space_fails_once file exists.
FAKE_DATABRICKS = textwrap.dedent("""\
    #!/usr/bin/env bash
    echo "databricks $*" >> "$FAKE_LOG"
    case "$1 $2" in
      "genie list-spaces")
        page=first; prev=""
        for a in "$@"; do [[ "$prev" == "--page-token" ]] && page="$a"; prev="$a"; done
        [[ -f "$FAKE_DIR/spaces_${page}.json" ]] || exit 1
        cat "$FAKE_DIR/spaces_${page}.json" ;;
      "warehouses list") if [[ -f "$FAKE_DIR/warehouses.json" ]]; then cat "$FAKE_DIR/warehouses.json"; else echo "[]"; fi ;;
      "warehouses create")
        n=$(( $(cat "$FAKE_DIR/created" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$FAKE_DIR/created"
        if (( n == 1 )); then echo '{"id":"wh-created"}'; else echo '{"id":"wh-created-'"$n"'"}'; fi ;;
      "genie create-space")
        if [[ -f "$FAKE_DIR/create_space_fails_once" ]]; then rm "$FAKE_DIR/create_space_fails_once"; exit 1; fi
        echo '{"space_id":"sp-created"}' ;;
      "genie get-space") echo '{"space_id":"sp-created","title":"t","warehouse_id":"w"}' ;;
    esac
    exit 0
    """)


def _page(spaces, next_token=None):
    body = {"spaces": [{"space_id": sid, "title": title} for sid, title in spaces]}
    if next_token:
        body["next_page_token"] = next_token
    return body


class _Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cleanup_test_")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = os.path.join(self.tmp, "bin")
        self.fixtures = os.path.join(self.tmp, "fixtures")
        os.makedirs(self.bin)
        os.makedirs(self.fixtures)
        for name, body in (("aws", FAKE_AWS), ("databricks", FAKE_DATABRICKS)):
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, 0o755)
        self.log = os.path.join(self.tmp, "calls.log")
        self.generated = os.path.join(self.tmp, ".env.generated")
        open(self.log, "w").close()

    def fixture(self, name, payload):
        with open(os.path.join(self.fixtures, name), "w") as f:
            json.dump(payload, f)

    def write_generated(self, **values):
        with open(self.generated, "w") as f:
            for k, v in values.items():
                f.write(f"export {k}={v}\n")

    def run_script(self, argv, **env_extra):
        # Built from scratch so nothing from the developer's own shell (a real WAREHOUSE_ID,
        # GENIE_SPACE_ID or profile) can leak into the run and change what gets deleted.
        env = {
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.tmp,
            "FAKE_LOG": self.log,
            "FAKE_DIR": self.fixtures,
            "GENERATED_ENV": self.generated,
            "ASSUME_YES": "1",
        }
        env.update(env_extra)
        proc = subprocess.run(["bash", *argv], cwd=SAMPLE_DIR, env=env, capture_output=True,
                              text=True, timeout=60)
        with open(self.log) as f:
            calls = [line.rstrip("\n") for line in f]
        return proc, calls

    def run_cleanup(self, **env_extra):
        env = {"ACCOUNT_ID": "111111111111", "REGION": "us-east-1",
               "AWS_PROFILE_SC": "demo", "DBX_PROFILE": "dbx"}
        env.update(env_extra)
        return self.run_script([CLEANUP], **env)

    @staticmethod
    def deletes(calls, prefix):
        return [c for c in calls if c.startswith(prefix)]


class WarehouseOwnership(_Harness):
    def test_reused_warehouse_is_kept(self):
        # setup_databricks.sh saves WAREHOUSE_ID even when it only reused the reader's warehouse.
        self.write_generated(WAREHOUSE_ID="wh-reader")
        proc, calls = self.run_cleanup()
        self.assertEqual(self.deletes(calls, "databricks warehouses delete"), [], proc.stdout)
        self.assertIn("wh-reader: kept, cleanup cannot confirm setup_databricks.sh created it", proc.stdout)
        # Neither the preview nor the delete step may call the warehouse it just kept missing.
        self.assertNotRegex(proc.stdout, r"SQL warehouse \(Databricks\)\s+<not found")
        self.assertNotIn("SQL warehouse (Databricks): not present", proc.stdout)

    def test_warehouse_setup_created_is_deleted(self):
        self.write_generated(WAREHOUSE_ID="wh-created", WAREHOUSE_CREATED_BY_SETUP="wh-created")
        proc, calls = self.run_cleanup()
        self.assertEqual(self.deletes(calls, "databricks warehouses delete"),
                         ["databricks warehouses delete wh-created --profile dbx"], proc.stdout)

    def test_only_the_recorded_warehouse_is_deleted_when_ids_differ(self):
        self.write_generated(WAREHOUSE_ID="wh-reader", WAREHOUSE_CREATED_BY_SETUP="wh-created")
        proc, calls = self.run_cleanup()
        self.assertEqual(self.deletes(calls, "databricks warehouses delete"),
                         ["databricks warehouses delete wh-created --profile dbx"], proc.stdout)
        self.assertIn("wh-reader: kept", proc.stdout)

    def test_a_name_match_alone_never_deletes(self):
        # No state file at all: the lookup by the scripted name still finds a warehouse, but
        # nothing says this run's setup created it.
        self.fixture("warehouses.json", [{"id": "wh-named", "name": "Supply Chain Serverless Warehouse"}])
        proc, calls = self.run_cleanup()
        self.assertEqual(self.deletes(calls, "databricks warehouses delete"), [], proc.stdout)
        self.assertIn("wh-named: kept", proc.stdout)

    def test_every_warehouse_with_the_scripted_name_is_listed(self):
        # A shared workspace can hold several; reporting only the first would hand the reader a
        # delete command for whichever the API happened to return first.
        self.fixture("warehouses.json", [{"id": "wh-colleague", "name": "Supply Chain Serverless Warehouse"},
                                         {"id": "wh-mine", "name": "Supply Chain Serverless Warehouse"}])
        proc, calls = self.run_cleanup()
        self.assertEqual(self.deletes(calls, "databricks warehouses delete"), [], proc.stdout)
        self.assertIn("wh-colleague: kept", proc.stdout)
        self.assertIn("wh-mine: kept", proc.stdout)
        self.assertRegex(proc.stdout, r"SQL warehouse KEPT\s+wh-colleague wh-mine \(cannot confirm setup created it\)")


class GenieTitleLookup(_Harness):
    def test_a_title_match_on_a_later_page_is_listed_not_trashed(self):
        # Even the only Agent with the scripted title can be a colleague's in a shared workspace.
        self.fixture("spaces_first.json", _page([("sp-other", "Someone else's Agent")], next_token="p2"))
        self.fixture("spaces_p2.json", _page([("sp-mine", SCRIPTED_TITLE)]))
        proc, calls = self.run_cleanup()
        self.assertEqual(self.deletes(calls, "databricks genie trash-space"), [], proc.stdout)
        self.assertIn("Genie Agent sp-mine: kept, found by title only", proc.stdout)
        self.assertIn("databricks genie trash-space sp-mine --profile dbx", proc.stdout)
        self.assertRegex(proc.stdout, r"Genie Agent KEPT\s+sp-mine \(found by title only\)")
        self.assertNotRegex(proc.stdout, r"Genie Agent \(Databricks\)\s+<not found")
        self.assertNotIn("Genie Agent (Databricks): not present", proc.stdout)

    def test_duplicate_titles_across_pages_are_all_listed(self):
        self.fixture("spaces_first.json", _page([("sp-a", SCRIPTED_TITLE)], next_token="p2"))
        self.fixture("spaces_p2.json", _page([("sp-b", SCRIPTED_TITLE)]))
        proc, calls = self.run_cleanup()
        self.assertEqual(self.deletes(calls, "databricks genie trash-space"), [], proc.stdout)
        self.assertIn("Genie Agent sp-a: kept", proc.stdout)
        self.assertIn("Genie Agent sp-b: kept", proc.stdout)

    def test_an_agent_returned_on_two_pages_is_listed_once(self):
        self.fixture("spaces_first.json", _page([("sp-mine", SCRIPTED_TITLE)], next_token="p2"))
        self.fixture("spaces_p2.json", _page([("sp-mine", SCRIPTED_TITLE)]))
        proc, _ = self.run_cleanup()
        self.assertEqual(proc.stdout.count("Genie Agent sp-mine: kept"), 1, proc.stdout)

    def test_an_unreadable_page_trashes_nothing(self):
        # Page 1 has a match, but page 2 cannot be read: cleanup must say the listing failed rather
        # than present page 1 as everything it found.
        self.fixture("spaces_first.json", _page([("sp-a", SCRIPTED_TITLE)], next_token="p2"))
        proc, calls = self.run_cleanup()
        self.assertEqual(self.deletes(calls, "databricks genie trash-space"), [], proc.stdout)
        self.assertIn("Genie Agent (Databricks): skipped <could not list Genie Agents", proc.stdout)

    def test_no_matching_title_trashes_nothing_and_warns_of_nothing(self):
        # The everyday case: the listing reads fine, the Agent is already gone or never existed.
        self.fixture("spaces_first.json", _page([("sp-other", "Someone else's Agent")]))
        proc, calls = self.run_cleanup()
        self.assertEqual(self.deletes(calls, "databricks genie trash-space"), [], proc.stdout)
        self.assertIn("Genie Agent (Databricks): not present", proc.stdout)
        self.assertNotIn("Agents titled", proc.stdout)
        self.assertNotIn("could not list Genie Agents", proc.stdout)

    def test_endless_pagination_stops_and_trashes_nothing(self):
        # A page whose next_page_token points back at itself: the lookup must give up, not hang.
        self.fixture("spaces_first.json", _page([("sp-a", SCRIPTED_TITLE)], next_token="first"))
        proc, calls = self.run_cleanup()
        self.assertEqual(self.deletes(calls, "databricks genie trash-space"), [], proc.stdout)
        self.assertIn("Genie Agent (Databricks): skipped <could not list Genie Agents", proc.stdout)
        self.assertEqual(len(self.deletes(calls, "databricks genie list-spaces")), 100)

    def test_every_page_is_requested_at_the_maximum_page_size(self):
        self.fixture("spaces_first.json", _page([], next_token="p2"))
        self.fixture("spaces_p2.json", _page([("sp-mine", SCRIPTED_TITLE)]))
        _, calls = self.run_cleanup()
        listed = self.deletes(calls, "databricks genie list-spaces")
        self.assertEqual(len(listed), 2, listed)
        self.assertTrue(all("--page-size 100" in c for c in listed), listed)
        self.assertIn("--page-token p2", listed[1])

    def test_a_recorded_id_is_used_without_a_lookup(self):
        self.write_generated(GENIE_SPACE_ID="sp-recorded")
        proc, calls = self.run_cleanup()
        self.assertEqual(self.deletes(calls, "databricks genie list-spaces"), [], proc.stdout)
        self.assertEqual(self.deletes(calls, "databricks genie trash-space"),
                         ["databricks genie trash-space sp-recorded --profile dbx"], proc.stdout)


class SetupRecordsWhatItCreated(_Harness):
    def run_genie_phase(self, expect_success=True, **env_extra):
        # A stand-in Genie artifact, so these tests do not depend on the real genie/genie_space.json.
        artifact = os.path.join(self.tmp, "genie_space.json")
        with open(artifact, "w") as f:
            json.dump({"version": 1}, f)
        env = {"DBX_PROFILE": "dbx", "WORKSPACE_USER": "reader@example.com",
               "WORKSPACE_HOST": "dbc-example.cloud.databricks.com", "GENIE_ARTIFACT": artifact}
        env.update(env_extra)
        proc, calls = self.run_script([SETUP, "genie"], **env)
        if expect_success:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        else:
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        with open(self.generated) as f:
            return f.read(), calls

    def test_a_created_warehouse_is_recorded(self):
        saved, _ = self.run_genie_phase()
        self.assertIn("export WAREHOUSE_CREATED_BY_SETUP=wh-created", saved)

    def test_a_reused_warehouse_is_not_recorded(self):
        saved, calls = self.run_genie_phase(WAREHOUSE_ID="wh-reader")
        self.assertEqual(self.deletes(calls, "databricks warehouses create"), [])
        self.assertIn("export WAREHOUSE_ID=wh-reader", saved)
        self.assertNotIn("WAREHOUSE_CREATED_BY_SETUP", saved)

    def test_setup_then_cleanup_deletes_the_created_warehouse(self):
        self.run_genie_phase()
        proc, calls = self.run_cleanup()
        self.assertEqual(self.deletes(calls, "databricks warehouses delete"),
                         ["databricks warehouses delete wh-created --profile dbx"], proc.stdout)

    def test_setup_then_cleanup_keeps_a_reused_warehouse(self):
        self.run_genie_phase(WAREHOUSE_ID="wh-reader")
        proc, calls = self.run_cleanup()
        self.assertEqual(self.deletes(calls, "databricks warehouses delete"), [], proc.stdout)

    def test_a_rerun_after_a_failed_genie_step_reuses_the_created_warehouse(self):
        # The warehouse is created, then create-space fails. A re-run must reuse that warehouse,
        # not create a second one and leave the first billable and unrecorded.
        open(os.path.join(self.fixtures, "create_space_fails_once"), "w").close()
        self.run_genie_phase(expect_success=False)
        saved, calls = self.run_genie_phase()
        self.assertEqual(len(self.deletes(calls, "databricks warehouses create")), 1, calls)
        self.assertIn("export WAREHOUSE_CREATED_BY_SETUP=wh-created\n", saved)
        proc, calls = self.run_cleanup()
        self.assertEqual(self.deletes(calls, "databricks warehouses delete"),
                         ["databricks warehouses delete wh-created --profile dbx"], proc.stdout)


if __name__ == "__main__":
    unittest.main()
