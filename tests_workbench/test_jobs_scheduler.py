"""Queue/scheduler adversarial tests: fake time, temp files, no sockets/data API."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from industry_workbench.jobs import JobManager, SHANGHAI
from industry_workbench.models import DataError
from industry_workbench.scheduler import Scheduler


class MemoryStore:
    """Only job/scheduler records hit the injected temporary directory."""
    development = False

    def __init__(self, root):
        self.root = Path(root)
        self.snapshot = None

    def path(self, name):
        return self.root / name

    def current(self):
        return deepcopy(self.snapshot)


class ChunkPipeline:
    def __init__(self, store, clock):
        self.store, self.clock = store, clock
        self.dates = [f"202609{day:02d}" for day in range(1, 8)]
        self.saved = set()
        self.calls, self.executed = [], []
        self.before_day = None
        self.fail_day = None
        self.failure_code = "TushareTransportError"
        self.source_error = None
        self.inflight, self.max_inflight = 0, 0

    def source_check(self):
        if self.source_error:
            raise DataError(self.source_error)
        return {"tree_sha256": "fixture"}

    def run(self, kind, params, *, progress=None, should_yield=None):
        self.source_check()
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            self.calls.append((kind, deepcopy(params)))
            todo = ([self.clock().strftime("%Y%m%d")] if kind == "update" else
                    [day for day in self.dates if params["start_date"] <= day <= params["end_date"] and day not in self.saved])
            limit = params.get("max_days", 3 if kind == "backfill" else 1)
            captured = []
            for index, day in enumerate(todo[:limit]):
                if captured and should_yield and should_yield():
                    break
                if progress:
                    progress("DAY", index, min(limit, len(todo)), day)
                if self.before_day:
                    self.before_day(kind, day)
                if self.fail_day == day:
                    raise DataError(self.failure_code)
                self.executed.append((kind, day))
                captured.append(day)
            self.saved.update(captured)
            self.store.snapshot = {
                "as_of": max(self.saved),
                "views": {"coverage": {"start": self.dates[0],
                                       "missing_days": sum(day not in self.saved for day in self.dates)}},
            }
            return {"batch_id": f"fixture-{len(self.saved)}", "as_of": max(self.saved),
                    "captured_days": captured, "remaining_days": len(todo) - len(captured)}
        finally:
            self.inflight -= 1


class QueueSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="workbench-queue-test-")
        self.store = MemoryStore(self.tmp.name)
        self.now = datetime(2026, 9, 17, 19, 30, tzinfo=SHANGHAI)
        self.pipeline = ChunkPipeline(self.store, lambda: self.now)
        self.jobs = JobManager(self.pipeline)
        self.params = {"start_date": "20260901", "end_date": "20260907"}
        self.releases = []
        self.clock_patch = patch("industry_workbench.jobs.utc_now", return_value="2026-09-17T11:30:00+00:00")
        self.clock_patch.start()

    def tearDown(self):
        for event in self.releases:
            event.set()
        self.jobs.stop(timeout=5)
        self.clock_patch.stop()
        self.tmp.cleanup()

    def wait_job(self, job):
        job_id = job["job_id"] if isinstance(job, dict) else job
        self.assertTrue(self.jobs._done[job_id].wait(5), "job did not complete")
        return self.jobs.get(job_id)

    def block_day(self, kind, day):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        previous = self.pipeline.before_day
        def block(actual_kind, actual_day):
            if previous:
                previous(actual_kind, actual_day)
            if (actual_kind, actual_day) == (kind, day):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test gate timed out")
        self.pipeline.before_day = block
        return entered, release

    def scheduler(self):
        scheduler = Scheduler(self.jobs, clock=lambda: self.now)
        self.addCleanup(scheduler.stop)
        return scheduler

    def make_current(self, missing=7):
        self.store.snapshot = {"as_of": "20260917", "views": {"coverage": {"start": "20260901", "missing_days": missing}}}

    def test_whole_range_keeps_same_id_until_all_seven_days_finish(self):
        entered, release = self.block_day("backfill", "20260904")
        job = self.jobs.submit("backfill", self.params)
        self.assertTrue(entered.wait(5))
        middle = self.jobs.get(job["job_id"])
        self.assertEqual(middle["status"], "RUNNING")
        self.assertEqual(middle["result"]["captured_days"], ["20260901", "20260902", "20260903"])
        self.assertEqual(middle["result"]["remaining_days"], 4)
        self.assertFalse(self.jobs._done[job["job_id"]].is_set())
        release.set()
        final = self.wait_job(job)
        self.assertEqual(final["status"], "SUCCEEDED", final)
        self.assertEqual(final["result"]["captured_days"], self.pipeline.dates)
        self.assertEqual(final["result"]["remaining_days"], 0)
        self.assertEqual(final["result"]["chunks_completed"], 3)
        self.assertEqual((final["completed_units"], final["total_units"]), (7, 7))
        self.assertEqual(len(self.pipeline.calls), 3)
        saved = json.loads(self.store.path(f"jobs/{job['job_id']}.json").read_bytes())
        self.assertEqual(saved, final)

    def test_explicit_max_days_is_one_bounded_chunk(self):
        job = self.jobs.submit("backfill", dict(self.params, max_days=2), asynchronous=False)
        self.assertEqual(job["status"], "SUCCEEDED", job)
        self.assertEqual(job["result"]["captured_days"], ["20260901", "20260902"])
        self.assertEqual(job["result"]["remaining_days"], 5)
        self.assertEqual(len(self.pipeline.calls), 1)

    def test_manual_update_preempts_backfill_at_next_complete_day_and_deduplicates(self):
        entered, release = self.block_day("backfill", "20260901")
        update_entered, update_release = self.block_day("update", "20260917")
        backfill = self.jobs.submit("backfill", self.params)
        self.assertTrue(entered.wait(5))
        update = self.jobs.submit("update")
        duplicate = self.jobs.submit("update")
        self.assertEqual(update["job_id"], duplicate["job_id"])
        self.assertEqual(update["status"], "QUEUED")
        self.assertEqual(self.jobs.active()["job_id"], backfill["job_id"])
        release.set()
        self.assertTrue(update_entered.wait(5))
        paused = self.jobs.get(backfill["job_id"])
        self.assertEqual(paused["status"], "QUEUED")
        self.assertEqual(paused["result"]["remaining_days"], 6)
        self.assertEqual(self.jobs.active()["job_id"], update["job_id"])
        update_release.set()
        self.assertEqual(self.wait_job(update)["status"], "SUCCEEDED")
        self.assertEqual(self.wait_job(backfill)["status"], "SUCCEEDED")
        self.assertEqual(self.pipeline.executed[:3], [("backfill", "20260901"), ("update", "20260917"), ("backfill", "20260902")])
        self.assertEqual(sum(kind == "update" for kind, _ in self.pipeline.calls), 1)
        self.assertEqual(self.pipeline.max_inflight, 1)

    def test_running_update_is_deduplicated(self):
        entered, release = self.block_day("update", "20260917")
        first = self.jobs.submit("update")
        self.assertTrue(entered.wait(5))
        second = self.jobs.submit("update")
        self.assertEqual(second["job_id"], first["job_id"])
        release.set()
        self.assertEqual(self.wait_job(first)["status"], "SUCCEEDED")
        self.assertEqual(len(self.pipeline.calls), 1)

    def test_failure_in_later_chunk_keeps_partial_result_and_does_not_resume(self):
        self.pipeline.fail_day = "20260904"
        job = self.jobs.submit("backfill", self.params, asynchronous=False)
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(job["error"]["code"], "TushareTransportError")
        self.assertEqual(job["result"]["captured_days"], ["20260901", "20260902", "20260903"])
        self.assertEqual(job["result"]["remaining_days"], 4)
        self.assertEqual(len(self.pipeline.calls), 2)
        self.pipeline.fail_day = None
        resumed = self.jobs.submit("backfill", self.params, asynchronous=False)
        self.assertEqual(resumed["status"], "SUCCEEDED")
        self.assertEqual(resumed["result"]["captured_days"], ["20260904", "20260905", "20260906", "20260907"])

    def test_source_failure_between_chunks_does_not_report_success(self):
        original = self.pipeline.run
        def change_source(*args, **kwargs):
            result = original(*args, **kwargs)
            self.pipeline.source_error = "SOURCE_CHANGED_DURING_JOB"
            return result
        with patch.object(self.pipeline, "run", side_effect=change_source):
            job = self.jobs.submit("backfill", self.params, asynchronous=False)
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(job["error"]["code"], "SOURCE_CHANGED_DURING_JOB")
        self.assertEqual(job["result"]["remaining_days"], 4)

    def test_no_progress_contract_failure_does_not_spin(self):
        with patch.object(self.pipeline, "run", return_value={"remaining_days": 5, "captured_days": []}) as run:
            job = self.jobs.submit("backfill", self.params, asynchronous=False)
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(job["error"]["code"], "BACKFILL_NO_PROGRESS")
        self.assertEqual(run.call_count, 1)

    def test_stop_marks_incomplete_backfill_and_queued_update_interrupted(self):
        entered, release = self.block_day("backfill", "20260901")
        backfill = self.jobs.submit("backfill", self.params)
        self.assertTrue(entered.wait(5))
        update = self.jobs.submit("update")
        self.jobs.stop(timeout=0)
        self.assertEqual(self.wait_job(update)["error"]["code"], "PROCESS_INTERRUPTED")
        release.set()
        stopped = self.wait_job(backfill)
        self.assertEqual(stopped["status"], "FAILED")
        self.assertEqual(stopped["phase"], "INTERRUPTED")
        self.assertEqual(stopped["result"]["remaining_days"], 6)
        self.assertEqual(self.pipeline.executed, [("backfill", "20260901")])
        with self.assertRaisesRegex(DataError, "SERVICE_STOPPING"):
            self.jobs.submit("update")

    def test_stopping_after_last_required_day_is_not_fake_cancellation(self):
        entered, release = self.block_day("backfill", "20260901")
        job = self.jobs.submit("backfill", {"start_date": "20260901", "end_date": "20260901"})
        self.assertTrue(entered.wait(5))
        self.jobs.stop(timeout=0)
        release.set()
        self.assertEqual(self.wait_job(job)["status"], "SUCCEEDED")

    def test_queued_save_failure_never_starts_pipeline_or_wedges_next_submission(self):
        with patch.object(self.jobs, "_save", side_effect=OSError("secret token fixture")):
            failed = self.jobs.submit("update", asynchronous=False)
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["error"]["code"], "JOB_PERSISTENCE_FAILED")
        self.assertNotIn("secret", json.dumps(failed))
        self.assertEqual(self.pipeline.calls, [])
        self.assertIsNone(self.jobs.active())
        self.assertEqual(self.jobs.submit("update", asynchronous=False)["status"], "SUCCEEDED")

    def test_final_save_failure_releases_writer_slot_and_preserves_actual_result(self):
        original = self.jobs._save
        def fail_final(job):
            if job["status"] == "SUCCEEDED":
                raise OSError("private filesystem detail")
            original(job)
        with patch.object(self.jobs, "_save", side_effect=fail_final):
            failed = self.jobs.submit("update", asynchronous=False)
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["error"]["code"], "JOB_PERSISTENCE_FAILED")
        self.assertIsNotNone(failed["result"]["batch_id"])
        self.assertNotIn("private filesystem", json.dumps(failed))
        self.assertIsNone(self.jobs.active())
        self.assertEqual(self.jobs.submit("update", asynchronous=False)["status"], "SUCCEEDED")

    def test_progress_save_failure_fails_safely_without_deadlock(self):
        original = self.jobs._save
        def fail_progress(job):
            if job["phase"] == "DAY":
                raise OSError("full disk")
            original(job)
        with patch.object(self.jobs, "_save", side_effect=fail_progress):
            failed = self.jobs.submit("update", asynchronous=False)
        self.assertEqual(failed["error"]["code"], "JOB_PERSISTENCE_FAILED")
        self.assertEqual(self.pipeline.executed, [])
        self.assertIsNone(self.jobs.active())
        self.assertEqual(self.jobs.submit("update", asynchronous=False)["status"], "SUCCEEDED")

    def test_final_persistence_failure_does_not_block_already_queued_update(self):
        entered, release = self.block_day("backfill", "20260901")
        original = self.jobs._save
        def fail_backfill_final(job):
            if job["kind"] == "backfill" and job["status"] == "SUCCEEDED":
                raise OSError("full disk fixture")
            original(job)
        with patch.object(self.jobs, "_save", side_effect=fail_backfill_final):
            backfill = self.jobs.submit("backfill", dict(self.params, max_days=3))
            self.assertTrue(entered.wait(5))
            update = self.jobs.submit("update")
            release.set()
            self.assertEqual(self.wait_job(backfill)["error"]["code"], "JOB_PERSISTENCE_FAILED")
            self.assertEqual(self.wait_job(update)["status"], "SUCCEEDED")
        self.assertEqual(self.pipeline.max_inflight, 1)

    def test_worker_start_failure_does_not_leave_a_stuck_queue(self):
        with patch("industry_workbench.jobs.threading.Thread.start", side_effect=RuntimeError("thread unavailable")):
            failed = self.jobs.submit("update", asynchronous=False)
        self.assertEqual(failed["status"], "FAILED")
        self.assertIsNone(self.jobs.active())
        self.assertEqual(self.jobs.pending(), [])
        self.assertEqual(self.jobs.submit("update", asynchronous=False)["status"], "SUCCEEDED")

    def test_returned_job_snapshots_cannot_mutate_internal_state(self):
        entered, release = self.block_day("update", "20260917")
        job = self.jobs.submit("update")
        self.assertTrue(entered.wait(5))
        pending = self.jobs.pending("update")
        pending[0]["params"]["max_days"] = 40
        self.assertEqual(self.jobs.get(job["job_id"])["params"], {})
        release.set()
        self.wait_job(job)
        finished = self.jobs.latest_finished("update")
        finished["status"] = "RUNNING"
        self.assertEqual(self.jobs.latest_finished("update")["status"], "SUCCEEDED")

    def test_restart_reports_abandoned_queued_record_as_interrupted(self):
        job_id = "JOB-" + "a" * 20
        path = self.store.path(f"jobs/{job_id}.json")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"job_id": job_id, "kind": "backfill", "status": "QUEUED"}))
        job = self.jobs.get(job_id)
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(job["error"]["code"], "PROCESS_INTERRUPTED")

    def test_scheduler_due_update_can_queue_behind_manual_backfill(self):
        entered, release = self.block_day("backfill", "20260901")
        backfill = self.jobs.submit("backfill", self.params)
        self.assertTrue(entered.wait(5))
        scheduler = self.scheduler()
        with patch.object(self.store, "current", side_effect=AssertionError("must not read in-flight publication")):
            scheduler.tick()
        update_id = scheduler.state["pending_update"]
        self.assertIsNotNone(update_id, scheduler.status())
        self.assertEqual(self.jobs.get(update_id)["status"], "QUEUED")
        scheduler.tick()
        self.assertEqual(len(self.jobs.pending("update")), 1)
        release.set()
        self.wait_job(backfill)
        self.wait_job(update_id)
        self.assertEqual(self.pipeline.executed[1], ("update", "20260917"))

    def test_scheduler_manual_backfill_success_clears_permanent_history_error(self):
        self.make_current()
        scheduler = self.scheduler()
        scheduler.state.update(date="20260917", startup_checked=True,
                               history_error={"code": "TushareApiError", "message": "permission fixture"})
        self.pipeline.fail_day = "20260901"
        self.pipeline.failure_code = "TushareApiError"
        failed = self.jobs.submit("backfill", self.params, asynchronous=False)
        scheduler.state["pending_history"] = failed["job_id"]
        scheduler.tick()
        self.assertEqual(scheduler.status()["history_error"]["code"], "TushareApiError")
        self.pipeline.fail_day = None
        self.jobs.submit("backfill", dict(self.params, max_days=1), asynchronous=False)
        # Emulate the independently current daily batch retained by real Pipeline.
        self.make_current(missing=6)
        scheduler.tick()
        self.assertIsNone(scheduler.status()["history_error"])
        auto = scheduler.state["pending_history"]
        self.assertIsNotNone(auto)
        job = self.wait_job(auto)
        self.assertEqual(job["params"]["max_days"], 3)

    def test_scheduler_transient_history_error_pauses_same_day_retries_next_day(self):
        self.make_current()
        scheduler = self.scheduler()
        scheduler.state.update(date="20260917", startup_checked=True,
                               history_error={"code": "TushareTransportError", "message": "fixture"})
        scheduler.tick()
        self.assertIsNone(scheduler.state["pending_history"])
        self.now = self.now.replace(day=18, hour=10)
        self.store.snapshot["as_of"] = "20260918"
        scheduler.tick()
        self.assertIsNone(scheduler.status()["history_error"])
        self.assertIsNotNone(scheduler.state["pending_history"])
        self.wait_job(scheduler.state["pending_history"])

    def test_scheduler_does_not_clear_new_manual_failure_on_initial_tick(self):
        self.make_current()
        self.pipeline.fail_day = "20260901"
        job = self.jobs.submit("backfill", self.params, asynchronous=False)
        scheduler = self.scheduler()
        scheduler.tick()
        self.assertEqual(scheduler.status()["history_error"]["code"], "TushareTransportError")
        self.assertIsNone(scheduler.state["pending_history"])
        self.assertEqual(self.jobs.latest_finished("backfill")["job_id"], job["job_id"])

    def test_scheduler_daily_retry_slots_are_persisted_and_not_replayed_on_restart(self):
        self.pipeline.fail_day = "20260917"
        scheduler = self.scheduler()
        scheduler.tick()
        self.wait_job(scheduler.state["pending_update"])
        scheduler.tick()
        self.assertEqual(scheduler.status()["last_error"]["code"], "TushareTransportError")
        restart = self.scheduler()
        restart.tick()
        self.assertEqual(len(self.pipeline.calls), 1)
        self.now = self.now.replace(minute=45)
        restart.tick()
        self.wait_job(restart.state["pending_update"])
        self.assertEqual(len(self.pipeline.calls), 2)
        self.assertEqual(restart.state["attempts"], ["19:30", "19:45"])

    def test_scheduler_nontransient_daily_failure_does_not_retry_later_slot(self):
        self.pipeline.fail_day = "20260917"
        self.pipeline.failure_code = "TushareApiError"
        scheduler = self.scheduler()
        scheduler.tick()
        self.wait_job(scheduler.state["pending_update"])
        self.now = self.now.replace(minute=45)
        scheduler.tick()
        self.assertEqual(len(self.pipeline.calls), 1)
        self.assertEqual(scheduler.status()["last_error"]["code"], "TushareApiError")

    def test_scheduler_state_write_failure_is_visible_and_dispatches_nothing(self):
        scheduler = self.scheduler()
        with patch("industry_workbench.scheduler.atomic_write", side_effect=OSError("secret filesystem detail")):
            scheduler.tick()
        self.assertFalse(scheduler.status()["enabled"])
        self.assertEqual(scheduler.status()["last_error"]["code"], "SCHEDULER_STATE_WRITE_FAILED")
        self.assertEqual(self.pipeline.calls, [])
        self.assertNotIn("secret", json.dumps(scheduler.status()))
        self.assertEqual(self.jobs.submit("update", asynchronous=False)["status"], "SUCCEEDED")

    def test_scheduler_source_gate_error_is_visible_without_api_call(self):
        self.pipeline.source_error = "CLEAN_COMMITTED_SOURCE_REQUIRED"
        scheduler = self.scheduler()
        scheduler.tick()
        self.assertEqual(scheduler.status()["last_error"]["code"], "CLEAN_COMMITTED_SOURCE_REQUIRED")
        self.assertEqual(self.pipeline.calls, [])

    def test_scheduler_does_not_duplicate_running_manual_whole_range_backfill(self):
        entered, release = self.block_day("backfill", "20260901")
        backfill = self.jobs.submit("backfill", self.params)
        self.assertTrue(entered.wait(5))
        scheduler = self.scheduler()
        scheduler.state.update(date="20260917", startup_checked=True, attempts=["19:30"])
        scheduler.tick()
        self.assertIsNone(scheduler.state["pending_history"])
        self.assertEqual(len(self.jobs.pending("backfill")), 1)
        release.set()
        self.wait_job(backfill)

    def test_scheduler_missing_pending_record_is_visible_and_manual_success_recovers(self):
        self.make_current()
        scheduler = self.scheduler()
        scheduler.state.update(date="20260917", startup_checked=True,
                               pending_history="JOB-" + "b" * 20)
        scheduler.tick()
        self.assertEqual(scheduler.status()["history_error"]["code"], "JOB_NOT_FOUND")
        self.assertIsNone(scheduler.state["pending_history"])
        self.jobs.submit("backfill", dict(self.params, max_days=1), asynchronous=False)
        self.make_current(missing=6)
        scheduler.tick()
        self.assertIsNone(scheduler.status()["history_error"])
        self.wait_job(scheduler.state["pending_history"])

    def test_development_store_never_enables_scheduler(self):
        self.store.development = True
        scheduler = self.scheduler()
        scheduler.tick()
        self.assertFalse(scheduler.status()["enabled"])
        self.assertEqual(self.pipeline.calls, [])


if __name__ == "__main__":
    unittest.main()
