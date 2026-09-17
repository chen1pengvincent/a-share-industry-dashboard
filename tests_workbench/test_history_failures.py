"""Independent adversarial backfill cases: temp state, fixed time, no network."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from industry_workbench.history_state import HistoryState
from industry_workbench.jobs import JobManager, Pipeline, SHANGHAI
from industry_workbench.models import DataError
from industry_workbench.query import QueryService
from industry_workbench.scheduler import Scheduler
from industry_workbench.storage import FileStore
from industry_workbench.taxonomy import audit_stock_day, DataError as ProviderDataError
from test_application import FixtureProvider
from test_independent_domain import SW_A


class HistoricalFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="history-failure-test-")
        root = Path(self.temp.name)
        self.source = root / "source"
        for name in ("provider", "taxonomy", "transport", "models"):
            path = self.source / "src/industry_workbench" / (name + ".py")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# isolated version 1\n")
        self.store = FileStore(root / "runtime", development=True)
        self.now = datetime(2026, 9, 17, 20, tzinfo=SHANGHAI)
        self.calls, self.failures = [], {}
        self.before_day = None
        self.emit_evidence = True
        self.cache_lifecycle = False
        owner = self

        class Provider(FixtureProvider):
            def __init__(self, **kwargs):
                self.put_raw = kwargs["put_raw"]
                self._refs = {}
                self._lifecycle_refs = {}

            def fetch_day(self, day):
                owner.calls.append(day)
                self._refs = {}
                if owner.emit_evidence:
                    specs = [(api, {"trade_date": day}) for api in ("daily", "daily_basic", "moneyflow", "suspend_d")]
                    specs += [("stock_basic", {"list_status": state}) for state in ("L", "D", "P")]
                    for api, params in specs:
                        key = api + json.dumps(params)
                        ref = self._lifecycle_refs.get(key) if owner.cache_lifecycle and api == "stock_basic" else None
                        if ref is None:
                            ref = self.put_raw({"api_name": api, "params": params, "attempt_count": 1},
                                               json.dumps({"api": api, "params": params, "fixture": True}).encode())
                        if api == "stock_basic":
                            self._lifecycle_refs[key] = ref
                        self._refs[key] = ref
                if owner.before_day:
                    owner.before_day(day)
                if day in owner.failures:
                    raise DataError(owner.failures[day])
                result = super().fetch_day(day)
                result["source_refs"] = list(self._refs.values())
                return result

        FixtureProvider.calls = []
        FixtureProvider.fail_day = None
        self.pipeline = Pipeline(self.store, self.source, provider_factory=Provider, clock=lambda: self.now)
        self.jobs = JobManager(self.pipeline)
        self.releases = []
        self.params = {"start_date": "20260907", "end_date": "20260916"}

    def tearDown(self):
        for release in self.releases:
            release.set()
        self.jobs.stop(timeout=5)
        self.temp.cleanup()

    def latest(self):
        # Keep this fault fixture's declared history small; production's actual
        # five-year initialization is tested separately by ApplicationTests.
        job = self.jobs.submit("backfill", {**self.params, "max_days": 1}, asynchronous=False)
        self.assertEqual(job["status"], "SUCCEEDED", job)
        return self.store.current()

    def run_history(self, **overrides):
        return self.jobs.submit("backfill", {**self.params, **overrides}, asynchronous=False)

    def test_blocked_first_day_does_not_hide_later_days_or_fake_success(self):
        latest = self.latest()
        self.failures["20260907"] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        job = self.run_history()
        self.assertEqual(job["status"], "FAILED", job)
        self.assertEqual(job["error"]["code"], "HISTORY_INCOMPLETE")
        result = job["result"]
        self.assertTrue(result["scan_complete"])
        self.assertEqual(result["remaining_days"], 1)
        self.assertEqual(result["remaining_attemptable_days"], 0)
        self.assertEqual(len(result["attempted_days"]), 8)
        self.assertEqual(len(result["captured_days"]), 7)
        current = self.store.current()
        self.assertNotEqual(current["batch_id"], latest["batch_id"])
        self.assertEqual(current["as_of"], "20260917")
        self.assertNotIn("20260907", current["days"])
        self.assertIn("20260916", current["days"])
        self.assertFalse(self.store.path("checkpoints/20260907.json").exists())
        self.assertEqual(current["publication_state"], "PUBLISHED_WITH_GAPS")
        weekly = QueryService(self.store).industries(current["batch_id"], period_kind="week", period_key="20260907")
        self.assertIsNone(weekly["rows"][0]["metrics"]["flow_cent"]["value"])
        self.assertEqual(weekly["rows"][0]["metrics"]["flow_cent"]["known_subtotal"], "400")
        ledger = [json.loads(line) for line in self.store.path("run_ledger.ndjson").read_text().splitlines()]
        self.assertEqual(len(ledger), 8)  # latest + each of seven successful days
        self.assertEqual(result["published_batches"], [item["batch_id"] for item in ledger[1:]])
        self.assertEqual(len(result["published_batches"]), len(set(result["published_batches"])))
        for item in ledger:
            self.assertNotIn("20260907", self.store.manifest(item["batch_id"])["days"])
        self.assertGreater(self.store.verify_refs(result["blocked_dates"]), 7)

    def test_max_days_counts_failure_attempt_and_keeps_bounded_contract(self):
        self.latest()
        self.failures["20260907"] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        job = self.run_history(max_days=2)
        self.assertEqual(self.calls, ["20260917", "20260907", "20260908"])
        self.assertEqual(job["status"], "FAILED")
        self.assertFalse(job["result"]["scan_complete"])
        self.assertEqual(job["result"]["remaining_days"], 7)
        self.assertEqual(job["result"]["remaining_attemptable_days"], 6)
        self.assertEqual(job["result"]["captured_days"], ["20260908"])
        self.assertEqual(job["phase"], "CHUNK_COMPLETE_WITH_GAPS")
        self.assertIn("6 个日期待尝试", job["message"])

    def test_restart_and_ui_change_do_not_retry_same_blocked_evidence(self):
        self.latest()
        self.failures["20260907"] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        self.run_history()
        before = list(self.calls)
        self.jobs.stop()
        web = self.source / "web/app.js"
        web.parent.mkdir()
        web.write_text("// unrelated UI edit\n")
        self.jobs = JobManager(self.pipeline)
        job = self.run_history()
        self.assertEqual(self.calls, before)
        self.assertEqual(job["error"]["code"], "HISTORY_INCOMPLETE")
        self.assertEqual(len(job["result"]["blocked_dates"]), 1)

    def test_data_code_change_retries_only_under_new_evidence_version(self):
        self.latest()
        self.failures["20260907"] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        self.run_history()
        before = self.calls.count("20260907")
        (self.source / "src/industry_workbench/taxonomy.py").write_text("# reviewed data version 2\n")
        self.run_history()
        self.assertEqual(self.calls.count("20260907"), before + 1)
        self.run_history()
        self.assertEqual(self.calls.count("20260907"), before + 1)

    def test_explicit_retry_visits_each_failed_date_once_across_chunks(self):
        self.latest()
        for day in ("20260907", "20260908", "20260909", "20260910", "20260911"):
            self.failures[day] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        self.run_history()
        before = {day: self.calls.count(day) for day in self.failures}
        retried = self.run_history(retry_failed=True)
        self.assertEqual(retried["status"], "FAILED", retried)
        self.assertEqual(retried["error"]["code"], "HISTORY_INCOMPLETE")
        self.assertEqual(len(retried["result"]["attempted_days"]), 5)
        self.assertEqual(retried["result"]["chunks_completed"], 2)
        records = HistoryState(self.store, self.pipeline.source_check()).records
        for day in self.failures:
            self.assertEqual(self.calls.count(day), before[day] + 1)
            self.assertEqual(records[day]["retry_id"], retried["params"]["retry_id"])

    def test_recovery_resolves_only_actual_success_and_preserves_old_record(self):
        self.latest()
        for day in ("20260907", "20260908"):
            self.failures[day] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        self.run_history()
        old = HistoryState(self.store, self.pipeline.source_check())
        old_ref = deepcopy(old.index["dates"]["20260907"])
        old_bytes = self.store.read_bytes(old_ref)
        self.failures.pop("20260907")
        recovered = self.run_history(start_date="20260907", end_date="20260907", retry_failed=True)
        self.assertEqual(recovered["status"], "SUCCEEDED", recovered)
        state = HistoryState(self.store, self.pipeline.source_check())
        self.assertEqual(state.records["20260907"]["status"], "RESOLVED")
        self.assertEqual(state.records["20260907"]["supersedes"], old_ref)
        self.assertEqual(state.records["20260908"]["status"], "BLOCKED")
        self.assertEqual(self.store.read_bytes(old_ref), old_bytes)
        self.assertEqual([item["trade_date"] for item in self.pipeline.history_status()["blocked_dates"]], ["20260908"])

    def test_latest_failure_is_never_downgraded_to_a_historical_gap(self):
        self.failures["20260917"] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        job = self.run_history()
        self.assertEqual(job["error"]["code"], "INDEPENDENT_UNIVERSE_INCOMPLETE")
        self.assertEqual(self.calls, ["20260917"])
        self.assertIsNone(self.store.current())
        self.assertFalse(self.store.path("history/index.json").exists())

    def test_global_error_preserves_prior_atomic_day_and_stops_remaining_dates(self):
        for code in ("TushareTransportError", "RESPONSE_DATE_MISMATCH", "ENDPOINT_ROW_LIMIT", "MONEYFLOW_COVERAGE_GAP"):
            with self.subTest(code=code):
                # A new runtime ensures each fault is independently exercised.
                self.jobs.stop()
                self.store = FileStore(Path(self.temp.name) / code, development=True)
                self.pipeline.store = self.store
                self.jobs = JobManager(self.pipeline)
                self.calls.clear(); self.failures.clear()
                self.latest()
                # The transport fault occurs in chunk two, after four actual
                # publications, to check cumulative partial receipts as well.
                fault_day = "20260911" if code == "TushareTransportError" else "20260908"
                expected_saved = ["20260907", "20260908", "20260909", "20260910"] if code == "TushareTransportError" else ["20260907"]
                self.failures[fault_day] = code
                job = self.run_history()
                self.assertEqual(job["error"]["code"], code, job)
                self.assertEqual(self.calls, ["20260917"] + expected_saved + [fault_day])
                self.assertEqual(job["result"]["captured_days"], expected_saved)
                self.assertEqual(set(self.store.current()["days"]), {"20260917", *expected_saved})
                ledger = [json.loads(line) for line in self.store.path("run_ledger.ndjson").read_text().splitlines()]
                self.assertEqual(job["result"]["published_batches"], [item["batch_id"] for item in ledger[1:]])
                self.assertFalse(self.store.path("history/index.json").exists())

    def test_missing_raw_evidence_cannot_create_suppressing_failure_record(self):
        self.latest()
        self.emit_evidence = False
        self.failures["20260907"] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        job = self.run_history()
        self.assertEqual(job["error"]["code"], "HISTORY_FAILURE_EVIDENCE_MISSING")
        self.assertFalse(self.store.path("history/index.json").exists())

    def test_failure_index_write_error_releases_worker_and_does_not_continue(self):
        self.latest()
        self.failures["20260907"] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        with patch("industry_workbench.history_state.atomic_write", side_effect=OSError("fixture")):
            job = self.run_history()
        self.assertEqual(job["error"]["code"], "HISTORY_STATE_WRITE_FAILED")
        self.assertEqual(self.calls, ["20260917", "20260907"])
        self.assertIsNone(self.jobs.active())
        self.assertEqual(self.jobs.submit("update", asynchronous=False)["status"], "SUCCEEDED")

    def test_corrupted_failure_evidence_blocks_publication_of_later_days(self):
        self.latest()
        self.failures["20260907"] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        first = self.run_history(max_days=1)
        before = self.store.path("current.json").read_bytes()
        record = self.store.read_json(first["result"]["blocked_dates"][0]["evidence_ref"])
        raw_ref = record["source_refs"][0]
        self.store.path(raw_ref["path"]).write_bytes(b"corrupted fixture")
        job = self.run_history(max_days=1)
        self.assertEqual(job["status"], "FAILED")
        self.assertNotEqual(job["error"]["code"], "HISTORY_INCOMPLETE")
        self.assertEqual(self.store.path("current.json").read_bytes(), before)

    def test_failed_day_yields_to_new_update_before_next_historical_day(self):
        self.latest()
        self.failures["20260907"] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def gate(day):
            if day == "20260907":
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test timeout")
        self.before_day = gate
        backfill = self.jobs.submit("backfill", self.params)
        self.assertTrue(entered.wait(5))
        self.now = self.now.replace(day=18)
        update = self.jobs.submit("update")
        release.set()
        self.assertTrue(self.jobs._done[update["job_id"]].wait(10))
        self.assertTrue(self.jobs._done[backfill["job_id"]].wait(15))
        self.assertEqual(self.calls[:4], ["20260917", "20260907", "20260918", "20260908"])
        self.assertEqual(self.jobs.get(backfill["job_id"])["error"]["code"], "HISTORY_INCOMPLETE")

    def test_scheduler_keeps_gaps_visible_and_does_not_resubmit_blocked_only_range(self):
        self.latest()
        self.failures["20260907"] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        self.run_history()
        # Restrict this fixture's declared range to the eight exercised dates.
        current = self.store.current()
        current["views"]["coverage"]["missing_dates"] = ["20260907"]
        current["views"]["coverage"]["missing_days"] = 1
        scheduler = Scheduler(self.jobs, clock=lambda: self.now)
        scheduler.enabled = True
        scheduler.state.update(date="20260917", startup_checked=True)
        with patch.object(self.store, "current", return_value=current):
            scheduler.tick()
            scheduler.tick()
        self.assertIsNone(scheduler.status()["history_error"])
        self.assertEqual(scheduler.status()["history_gaps"]["remaining_attemptable_days"], 0)
        self.assertIsNone(scheduler.state["pending_history"])
        self.assertEqual(self.calls.count("20260907"), 1)

    def test_read_only_status_does_not_apply_execution_source_gate(self):
        self.latest()
        self.failures["20260907"] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        self.run_history(max_days=1)
        before = self.store.path("history/index.json").read_bytes()
        with patch.object(self.pipeline, "source_check", side_effect=DataError("CLEAN_COMMITTED_SOURCE_REQUIRED")):
            status = self.pipeline.history_status()
        self.assertEqual(status["blocked_dates"][0]["trade_date"], "20260907")
        self.assertEqual(self.store.path("history/index.json").read_bytes(), before)

    def test_valid_hash_cannot_legitimize_empty_or_misdated_failure_evidence(self):
        self.latest()
        self.failures["20260907"] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        job = self.run_history(max_days=1)
        index_bytes = self.store.path("history/index.json").read_bytes()
        original = self.store.read_json(job["result"]["blocked_dates"][0]["evidence_ref"])
        mutations = [lambda r: r.update(source_refs=[]),
                     lambda r: r.update(target_date="20260907"),
                     lambda r: r.update(evidence_version="0" * 64),
                     lambda r: r["capture_source"]["identity"].update(tree_sha256="0" * 64)]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                record = deepcopy(original)
                mutate(record)
                with self.store.writer():
                    ref = self.store.put_json(record)
                index = json.loads(index_bytes)
                index["head"] = ref
                index["dates"]["20260907"] = ref
                self.store.path("history/index.json").write_text(json.dumps(index))
                with self.assertRaises(DataError):
                    self.pipeline.history_status()
        self.store.path("history/index.json").write_bytes(index_bytes)

    def test_failure_source_snapshot_must_match_inventory_not_just_hash_itself(self):
        self.latest()
        self.failures["20260907"] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        job = self.run_history(max_days=1)
        original = self.store.read_json(job["result"]["blocked_dates"][0]["evidence_ref"])
        with self.store.writer():
            original["capture_source"]["snapshot"] = self.store.put_bytes(b"valid hash but no frozen source", "source_snapshot_tar_gz")
            ref = self.store.put_json(original)
        index = json.loads(self.store.path("history/index.json").read_bytes())
        index["head"] = ref
        index["dates"]["20260907"] = ref
        self.store.path("history/index.json").write_text(json.dumps(index))
        with self.assertRaises(DataError):
            self.pipeline.history_status()

    def test_failure_index_cannot_drop_head_or_relabel_a_different_date(self):
        self.latest()
        self.failures["20260907"] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        self.run_history(max_days=1)
        path = self.store.path("history/index.json")
        original = json.loads(path.read_bytes())
        no_head = {**original, "head": None}
        path.write_text(json.dumps(no_head))
        with self.assertRaises(DataError):
            self.pipeline.history_status()
        wrong_day = {**original, "dates": {"20260908": original["dates"]["20260907"]}}
        path.write_text(json.dumps(wrong_day))
        with self.assertRaises(DataError):
            self.pipeline.history_status()

    @staticmethod
    def actual_universe_failure(day):
        return audit_stock_day(trade_date=day,
            stocks=[{"ts_code": code, "list_date": "20100101", "delist_date": None} for code in ("000001.SZ", "000002.SZ")],
            daily=[{"ts_code": "000001.SZ", "trade_date": day, "vol": "100", "amount": "200"}],
            daily_basic=[], moneyflow=[], suspensions=[], prior_active_counts=[1, 1, 1])

    def test_real_taxonomy_auditor_error_is_date_scoped_but_latest_still_fails(self):
        with self.assertRaises(ProviderDataError) as raised:
            self.actual_universe_failure("20260907")
        self.assertNotIsInstance(raised.exception, DataError)
        self.latest()
        self.before_day = lambda day: self.actual_universe_failure(day) if day == "20260907" else None
        job = self.run_history(max_days=2)
        self.assertEqual(job["error"]["code"], "HISTORY_INCOMPLETE", job)
        self.assertEqual(job["result"]["captured_days"], ["20260908"])
        self.now = self.now.replace(day=18)
        self.before_day = self.actual_universe_failure
        before = self.store.path("current.json").read_bytes()
        update = self.jobs.submit("update", asynchronous=False)
        self.assertEqual(update["error"]["code"], "INDEPENDENT_UNIVERSE_INCOMPLETE")
        self.assertEqual(self.store.path("current.json").read_bytes(), before)
        self.assertNotIn("20260918", HistoryState(self.store, self.pipeline.source_check()).records)

    def test_arbitrary_exception_with_same_code_is_not_treated_as_controlled_audit(self):
        class Unrelated(ValueError):
            code = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        self.latest()
        def fail(day):
            raise Unrelated("fixture")
        self.before_day = fail
        job = self.run_history()
        self.assertEqual(job["error"]["code"], "INDEPENDENT_UNIVERSE_INCOMPLETE")
        self.assertEqual(self.calls, ["20260917", "20260907"])
        self.assertFalse(self.store.path("history/index.json").exists())

    def test_failed_day_preserves_cached_lifecycle_refs_not_only_new_requests(self):
        self.latest()
        self.cache_lifecycle = True
        self.failures["20260908"] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        journal = self.store.path("request_journal.ndjson")
        before = len(journal.read_text().splitlines())
        job = self.run_history(max_days=2)
        new_requests = [json.loads(line) for line in journal.read_text().splitlines()[before:]]
        self.assertEqual(sum(ref["request"]["api_name"] == "stock_basic" for ref in new_requests), 3)
        record = self.store.read_json(job["result"]["blocked_dates"][0]["evidence_ref"])
        states = {ref["request"]["params"]["list_status"] for ref in record["source_refs"] if ref["request"]["api_name"] == "stock_basic"}
        self.assertEqual(states, {"L", "D", "P"})

    def test_interleaved_manual_retry_does_not_reset_first_jobs_attempted_set(self):
        self.latest()
        self.failures["20260907"] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        args = {"start_date": "20260907", "end_date": "20260908", "retry_failed": True, "max_days": 1}
        first = self.pipeline.run("backfill", {**args, "retry_id": "RETRY-A"})
        self.pipeline.run("backfill", {**args, "retry_id": "RETRY-B"})
        continued = self.pipeline.run("backfill", {**args, "retry_id": "RETRY-A", "_attempted_dates": first["attempted_days"]})
        self.assertEqual(self.calls.count("20260907"), 2)
        self.assertEqual(continued["attempted_days"], ["20260908"])
        self.assertEqual(continued["remaining_attemptable_days"], 0)
        self.assertEqual(continued["remaining_days"], 1)

    def test_source_change_during_failed_fetch_cannot_be_recorded_as_safe_day_gap(self):
        self.latest()
        self.failures["20260907"] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        def change(day):
            (self.source / "src/industry_workbench/taxonomy.py").write_text("# changed during capture\n")
        self.before_day = change
        before = self.store.path("current.json").read_bytes()
        job = self.run_history()
        self.assertEqual(job["error"]["code"], "SOURCE_CHANGED_DURING_JOB")
        self.assertEqual(self.store.path("current.json").read_bytes(), before)
        self.assertFalse(self.store.path("history/index.json").exists())

    def test_record_only_polling_does_not_claim_or_replace_writer_evidence_validation(self):
        self.latest()
        self.failures["20260907"] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        failed = self.run_history(max_days=1)
        record = self.store.read_json(failed["result"]["blocked_dates"][0]["evidence_ref"])
        raw_ref = record["source_refs"][0]
        self.store.path(raw_ref["path"]).write_bytes(b"raw corrupted after recorded failure")
        before = self.store.path("current.json").read_bytes()
        status = self.pipeline.history_status()
        self.assertEqual(status["evidence_validation"], "RECORDS_ONLY")
        self.assertEqual(status["blocked_dates"][0]["trade_date"], "20260907")
        for kind, args in (("update", {}), ("backfill", self.params)):
            with self.subTest(kind=kind):
                job = self.jobs.submit(kind, args, asynchronous=False)
                self.assertEqual(job["status"], "FAILED")
                self.assertNotEqual(job["error"]["code"], "HISTORY_INCOMPLETE")
                self.assertEqual(self.store.path("current.json").read_bytes(), before)

    def test_index_cannot_omit_an_earlier_date_or_erase_all_recorded_failures(self):
        self.latest()
        self.failures.update({day: "INDEPENDENT_UNIVERSE_INCOMPLETE" for day in ("20260907", "20260908")})
        self.run_history(max_days=2)
        path = self.store.path("history/index.json")
        original = json.loads(path.read_bytes())
        omitted = deepcopy(original)
        del omitted["dates"]["20260907"]
        empty = {"schema_version": "history-attempt-index-v1", "head": None, "dates": {}}
        for corrupt in (omitted, empty):
            with self.subTest(corrupt=corrupt):
                path.write_text(json.dumps(corrupt))
                with self.assertRaisesRegex(DataError, "HISTORY_STATE_INVALID"):
                    self.pipeline.history_status()
                before = self.store.path("current.json").read_bytes()
                job = self.run_history()
                self.assertEqual(job["error"]["code"], "HISTORY_STATE_INVALID")
                self.assertEqual(self.store.path("current.json").read_bytes(), before)
        path.write_text(json.dumps(original))

    def test_supersedes_must_match_same_dates_previous_attempt(self):
        self.latest()
        self.failures["20260907"] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        self.run_history(max_days=1)
        self.run_history(max_days=1, retry_failed=True)
        path = self.store.path("history/index.json")
        index = json.loads(path.read_bytes())
        record = self.store.read_json(index["head"])
        self.assertIsNotNone(record["supersedes"])
        record["supersedes"] = None
        with self.store.writer():
            ref = self.store.put_json(record)
        index["head"] = ref
        index["dates"]["20260907"] = ref
        path.write_text(json.dumps(index))
        with self.assertRaisesRegex(DataError, "HISTORY_STATE_INVALID"):
            self.pipeline.history_status()

    def test_missing_index_or_rollback_cannot_forget_a_published_failure(self):
        self.latest()
        self.failures.update({day: "INDEPENDENT_UNIVERSE_INCOMPLETE" for day in ("20260907", "20260909")})
        self.run_history(start_date="20260907", end_date="20260908")
        path = self.store.path("history/index.json")
        old_index = path.read_bytes()
        self.run_history(start_date="20260909", end_date="20260910")
        current_index = path.read_bytes()
        self.assertEqual(len(self.store.current()["views"]["coverage"]["blocked_dates"]), 2)
        path.write_bytes(old_index)
        with self.assertRaisesRegex(DataError, "HISTORY_STATE_INVALID"):
            self.pipeline.history_status()
        path.unlink()
        with self.assertRaisesRegex(DataError, "HISTORY_STATE_INVALID"):
            self.pipeline.history_status()
        # Even deleting the now-empty directory does not remove published refs.
        path.parent.rmdir()
        with self.assertRaisesRegex(DataError, "HISTORY_STATE_INVALID"):
            self.pipeline.history_status()
        before = self.store.path("current.json").read_bytes()
        job = self.run_history()
        self.assertEqual(job["error"]["code"], "HISTORY_STATE_INVALID")
        self.assertEqual(self.store.path("current.json").read_bytes(), before)
        path.parent.mkdir()
        path.write_bytes(current_index)

    def test_history_directory_without_index_is_not_a_fresh_initialization(self):
        self.latest()
        self.store.path("history/index.json").parent.mkdir()
        with self.assertRaisesRegex(DataError, "HISTORY_STATE_INVALID"):
            self.pipeline.history_status()

    def test_published_old_failure_anchor_remains_valid_after_resolution(self):
        self.latest()
        self.failures["20260907"] = "INDEPENDENT_UNIVERSE_INCOMPLETE"
        self.run_history(start_date="20260907", end_date="20260908")
        old_anchors = deepcopy(self.store.current()["views"]["coverage"]["blocked_dates"])
        self.failures.clear()
        recovered = self.run_history(start_date="20260907", end_date="20260907", retry_failed=True)
        self.assertEqual(recovered["status"], "SUCCEEDED")
        state = HistoryState(self.store, self.pipeline.source_check(), published_failures=old_anchors)
        self.assertEqual(state.records["20260907"]["status"], "RESOLVED")

    def test_first_backfill_today_before_ready_publishes_readable_latest_day(self):
        self.now = self.now.replace(hour=19, minute=29)
        job = self.run_history(start_date="20260917", end_date="20260917")
        self.assertEqual(job["status"], "SUCCEEDED", job)
        current = self.store.current()
        self.assertEqual((current["history_start"], current["as_of"]), ("20260916", "20260916"))
        self.assertEqual(current["views"]["coverage"]["expected_days"], 1)
        view = QueryService(self.store).industries(current["batch_id"])
        self.assertEqual(view["period"]["endpoint"], "20260916")
        self.assertEqual(view["rows"][0]["metrics"]["flow_cent"]["value"], "100")
        self.assertEqual(self.calls, ["20260916"])

    def test_first_weekend_backfill_keeps_latest_trade_day_inside_history(self):
        self.now = self.now.replace(day=20, hour=10)  # Sunday; Friday is latest.
        job = self.run_history(start_date="20260920", end_date="20260920")
        self.assertEqual(job["status"], "SUCCEEDED", job)
        current = self.store.current()
        self.assertEqual((current["history_start"], current["as_of"]), ("20260918", "20260918"))
        view = QueryService(self.store).industries(current["batch_id"])
        self.assertEqual(view["period"]["endpoint"], "20260918")
        self.assertEqual(view["rows"][0]["metrics"]["pe_ttm_median"]["value"], "20")
        self.assertEqual(self.calls, ["20260918"])

    def test_publication_rejects_inverted_history_before_compiling_or_pointer_write(self):
        current = self.latest()
        before = self.store.path("current.json").read_bytes()
        with self.store.writer(), patch("industry_workbench.jobs.compile_views") as compile_views:
            with self.assertRaisesRegex(DataError, "HISTORY_RANGE_INVERTED"):
                self.pipeline._publish_days(days=current["days"], calendar=FixtureProvider().calendar("20260907", "20260917"),
                    history_start="20260918", source=current["source"], frozen_source=current["source_snapshot"],
                    calendar_source_refs=[], provider=FixtureProvider(), history=HistoryState(self.store, current["source"]),
                    progress=lambda *args: None)
            compile_views.assert_not_called()
        self.assertEqual(self.store.path("current.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
