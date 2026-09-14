"""Runtime regressions using temporary state, synthetic data and no network."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from swivd.io_utils import write_json
from swivd.v2_jobs import JobConflict, JobManager
from swivd.v2_pipeline import UpdateTarget
from swivd.v2_server import LocalApp, _handler
from swivd.v2_storage import POINTER_SCOPE, SingleWriterLock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "SWIVD2-RUN-20260901-002"
JOB_ID = "12345678-1234-1234-1234-123456789abc"


def job_record(**changes):
    record = {
        "schema_version": "swivd-job-v2",
        "job_id": JOB_ID,
        "kind": "UPDATE_LATEST",
        "requested_as_of": None,
        "as_of": "20260901",
        "target_selection": UpdateTarget(
            as_of="20260901",
            reason_code="CURRENT_OPEN_DAY_AT_OR_AFTER_SW_DAILY_CUTOFF",
            evaluated_at="2026-09-01T21:18:27+08:00",
        ).as_dict(),
        "state": "MEMBERSHIP",
        "completed_units": 2,
        "total_units": 62,
        "percent": 50,
        "current_item": "R1:Y:801010.SI",
        "safe_message_code": "MEMBERSHIP",
        "run_id": RUN_ID,
        "created_at": "2026-09-01T21:18:27+08:00",
        "updated_at": "2026-09-01T21:19:27+08:00",
    }
    record.update(changes)
    return record


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="swivd-v23-runtime-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = LocalApp(
            project_root=PROJECT_ROOT, data_dir=self.root,
            host="127.0.0.1", port=8765,
        )

    def make_snapshot(self, run_id=RUN_ID):
        root = self.root / "runs" / run_id
        body = b'{"schema_version":"synthetic-only"}'
        (root / "ui").mkdir(parents=True)
        (root / "ui" / "catalog.json").write_bytes(body)
        manifest = {
            "schema_version": "swivd-local-snapshot-manifest-v3",
            "run_id": run_id,
            "as_of": "20260901",
            "purpose": "UPDATE_LATEST",
            "artifacts": [{
                "path": "ui/catalog.json", "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }],
        }
        write_json(root / "manifest.json", manifest)
        return root, manifest

    def make_current(self):
        root, manifest = self.make_snapshot()
        write_json(self.root / "latest_run.json", {
            "pointer_kind": "manifest", "scope": POINTER_SCOPE,
            "run_id": RUN_ID, "as_of": "20260901",
            "target_path": f"runs/{RUN_ID}/manifest.json",
            "target_sha256": hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest(),
        })
        return root, manifest

    def test_same_run_concurrent_first_reads_share_one_complete_validation(self):
        root, manifest = self.make_snapshot()
        entered, release, all_waiting = threading.Event(), threading.Event(), threading.Event()
        count_lock = threading.Lock()
        waiter_count = 0

        class ObservedFuture(Future):
            def result(self, timeout=None):
                nonlocal waiter_count
                with count_lock:
                    waiter_count += 1
                    if waiter_count == 7:
                        all_waiting.set()
                return super().result(timeout)

        def validate(_root):
            entered.set()
            if not release.wait(5):
                raise AssertionError("validation was never released")
            return manifest

        with patch("swivd.v2_server.Future", ObservedFuture), patch(
            "swivd.v2_server.validate_run_v2", side_effect=validate
        ) as validator, ThreadPoolExecutor(max_workers=8) as pool:
            calls = [pool.submit(self.app.snapshot, RUN_ID)]
            self.assertTrue(entered.wait(3))
            calls.extend(pool.submit(self.app.snapshot, RUN_ID) for _ in range(7))
            try:
                self.assertTrue(all_waiting.wait(3))
                self.assertEqual(validator.call_count, 1)
            finally:
                release.set()
            self.assertEqual([call.result(3) for call in calls], [root] * 8)
            self.assertEqual(self.app.verified_json(RUN_ID, "ui/catalog.json"),
                             {"schema_version": "synthetic-only"})
            self.assertEqual(validator.call_count, 1)

    def test_failed_validation_is_shared_then_a_later_request_can_retry(self):
        root, manifest = self.make_snapshot()
        entered, waiting, release = threading.Event(), threading.Event(), threading.Event()

        class ObservedFuture(Future):
            def result(self, timeout=None):
                waiting.set()
                return super().result(timeout)

        def fail(_root):
            entered.set()
            if not release.wait(5):
                raise AssertionError("validation was never released")
            raise ValueError("SYNTHETIC_VALIDATION_FAILURE")

        with patch("swivd.v2_server.Future", ObservedFuture), patch(
            "swivd.v2_server.validate_run_v2", side_effect=fail
        ) as validator, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.app.snapshot, RUN_ID)
            self.assertTrue(entered.wait(3))
            second = pool.submit(self.app.snapshot, RUN_ID)
            try:
                self.assertTrue(waiting.wait(3))
                self.assertEqual(validator.call_count, 1)
            finally:
                release.set()
            for result in (first, second):
                with self.assertRaisesRegex(ValueError, "SYNTHETIC_VALIDATION_FAILURE"):
                    result.result(3)
            validator.side_effect = None
            validator.return_value = manifest
            self.assertEqual(self.app.snapshot(RUN_ID), root)
            self.assertEqual(validator.call_count, 2)

    def test_different_snapshots_do_not_block_each_others_validation(self):
        root_one, one = self.make_snapshot()
        other_id = "SWIVD2-RUN-20260901-003"
        root_two, two = self.make_snapshot(other_id)
        barrier = threading.Barrier(2)

        def validate(root):
            barrier.wait(timeout=3)
            return one if root == root_one else two

        with patch("swivd.v2_server.validate_run_v2", side_effect=validate), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.app.snapshot, RUN_ID)
            second = pool.submit(self.app.snapshot, other_id)
            self.assertEqual(first.result(5), root_one)
            self.assertEqual(second.result(5), root_two)

    def test_cached_artifact_mutation_is_rejected_and_manifest_change_revalidates(self):
        root, manifest = self.make_snapshot()
        with patch("swivd.v2_server.validate_run_v2", return_value=manifest) as validator:
            self.app.verified_bytes(RUN_ID, "ui/catalog.json")
            manifest["test_revision"] = 2
            write_json(root / "manifest.json", manifest)
            self.app.snapshot(RUN_ID)
            self.assertEqual(validator.call_count, 2)
            artifact = root / "ui" / "catalog.json"
            original = artifact.read_bytes()
            artifact.write_bytes(original.replace(b"synthetic", b"tampering"))
            with self.assertRaisesRegex(ValueError, "SNAPSHOT_ARTIFACT_DRIFT"):
                self.app.verified_bytes(RUN_ID, "ui/catalog.json")

    def test_manifest_modified_during_validation_is_not_cached(self):
        root, manifest = self.make_snapshot()

        def validate(_root):
            write_json(root / "manifest.json", {**manifest, "changed": True})
            return manifest

        with patch("swivd.v2_server.validate_run_v2", side_effect=validate):
            with self.assertRaisesRegex(ValueError, "SNAPSHOT_MANIFEST_DRIFT"):
                self.app.snapshot(RUN_ID)
        self.assertNotIn(RUN_ID, self.app.validated)
        self.assertEqual(self.app._validation_flights, {})

    def test_startup_during_writer_keeps_old_snapshot_and_does_not_interrupt_owner(self):
        root, manifest = self.make_current()
        path = self.root / "jobs" / f"{JOB_ID}.json"
        write_json(path, job_record())
        before = path.read_bytes()
        with SingleWriterLock(self.root), patch("swivd.v2_jobs.recover_publication") as recover:
            reader = LocalApp(project_root=PROJECT_ROOT, data_dir=self.root,
                              host="127.0.0.1", port=8766)
            recover.assert_not_called()
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(reader.bootstrap()["current_run_id"], RUN_ID)
            self.assertEqual(reader.bootstrap()["active_jobs"][0]["job_id"], JOB_ID)
            with patch("swivd.v2_server.validate_run_v2", return_value=manifest):
                self.assertEqual(reader.snapshot(RUN_ID), root)
            with self.assertRaises(JobConflict):
                reader.jobs.create(kind="UPDATE_LATEST")
        self.assertEqual(path.read_bytes(), before)

    def test_pending_publication_hides_pointer_but_bootstrap_can_recover_progress(self):
        self.make_current()
        write_json(self.root / "jobs" / f"{JOB_ID}.json", job_record(state="PUBLISH"))
        transaction = self.root / "transactions" / "publication.json"
        write_json(transaction, {"unread_while_owned": True})
        before = transaction.read_bytes()
        with SingleWriterLock(self.root):
            reader = LocalApp(project_root=PROJECT_ROOT, data_dir=self.root,
                              host="127.0.0.1", port=8766)
            with self.assertRaisesRegex(ValueError, "PUBLICATION_RECOVERY_REQUIRED"):
                reader.current_snapshot()
            bootstrap = reader.bootstrap()
            self.assertIsNone(bootstrap["current_run_id"])
            self.assertEqual(bootstrap["current_snapshot_state"], "PUBLICATION_RECOVERY_REQUIRED")
            self.assertEqual(bootstrap["active_jobs"][0]["state"], "PUBLISH")
            self.assertTrue(bootstrap["nonce"])
        self.assertEqual(transaction.read_bytes(), before)

    def test_active_and_single_job_endpoints_expose_only_safe_projection(self):
        canary = "SYNTHETICCANARYVALUE123456789"
        record = job_record(current_item=canary, safe_message_code=canary,
                            debug={"credential": canary})
        record["target_selection"]["debug"] = canary
        write_json(self.root / "jobs" / f"{JOB_ID}.json", record)
        active = self.app.jobs.active()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["current_item"], "")
        self.assertEqual(active[0]["safe_message_code"], "MEMBERSHIP")
        self.assertEqual(active[0]["target_selection"], job_record()["target_selection"])
        self.assertNotIn(canary, json.dumps(active))
        for route in ("/api/v1/jobs/active", f"/api/v1/jobs/{JOB_ID}"):
            status, body = self.request(route)
            self.assertEqual(status, 200)
            self.assertNotIn(canary, json.dumps(body))
            self.assertEqual(self.request(route, host="evil.test")[0], 421)

    def test_active_skips_terminal_invalid_symlink_and_mismatched_job_files(self):
        path = self.root / "jobs" / f"{JOB_ID}.json"
        for record in (job_record(state="SUCCEEDED"), job_record(job_id="other"),
                       job_record(percent=True), job_record(percent=101),
                       job_record(state="credential_value")):
            write_json(path, record)
            self.assertEqual(self.app.jobs.active(), [])
        path.unlink()
        outside = self.root / "not-a-job.json"
        write_json(outside, job_record())
        path.symlink_to(outside)
        self.assertEqual(self.app.jobs.active(), [])
        self.assertEqual(self.request(f"/api/v1/jobs/{JOB_ID}")[0], 404)
        self.assertEqual(self.request("/api/v1/jobs/../not-a-job")[0], 404)

    def test_current_endpoint_is_read_only_and_respects_pending_and_hash_drift(self):
        self.assertEqual(self.request("/api/v1/snapshots/current"),
                         (200, {"run_id": None, "as_of": None}))
        root, manifest = self.make_current()
        pointer = self.root / "latest_run.json"
        before = pointer.read_bytes()
        self.assertEqual(self.request("/api/v1/snapshots/current"),
                         (200, {"run_id": RUN_ID, "as_of": "20260901"}))
        write_json(root / "manifest.json", {**manifest, "changed": True})
        self.assertEqual(self.request("/api/v1/snapshots/current"),
                         (503, {"error": "CURRENT_SNAPSHOT_UNAVAILABLE"}))
        write_json(self.root / "transactions" / "publication.json", {})
        self.assertEqual(self.request("/api/v1/snapshots/current"),
                         (503, {"error": "PUBLICATION_RECOVERY_REQUIRED"}))
        self.assertEqual(pointer.read_bytes(), before)
        self.assertFalse((self.root / "run_ledger.ndjson").exists())

    def test_post_security_gates_remain_required_for_creation(self):
        with patch.object(self.app.jobs, "create", return_value={"job_id": JOB_ID}) as create:
            self.assertEqual(self.request("/api/v1/jobs", method="POST")[0], 403)
            self.assertEqual(self.request("/api/v1/jobs", method="POST", nonce="bad")[0], 403)
            self.assertEqual(self.request("/api/v1/jobs", method="POST", nonce=self.app.nonce,
                                          content_type="text/plain")[0], 415)
            create.assert_not_called()
            self.assertEqual(self.request("/api/v1/jobs", method="POST", nonce=self.app.nonce)[0], 202)
            create.assert_called_once_with(kind="UPDATE_LATEST", as_of=None)

    def request(self, route, *, host="127.0.0.1:8765", method="GET", nonce=None,
                content_type="application/json"):
        # Exercise actual handler dispatch without opening a network socket.
        handler = object.__new__(_handler(self.app))
        handler.path = route
        handler.command = method
        handler.headers = Message()
        handler.headers["Host"] = host
        handler.headers["Origin"] = "http://127.0.0.1:8765"
        handler.headers["Content-Type"] = content_type
        if nonce is not None:
            handler.headers["X-SWIVD-Nonce"] = nonce
        body = b'{"kind":"UPDATE_LATEST"}'
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = io.BytesIO(body)
        sent = []
        handler._send = lambda status, payload: sent.append((status, payload))
        (handler.do_GET if method == "GET" else handler.do_POST)()
        self.assertEqual(len(sent), 1)
        return sent[0]


if __name__ == "__main__":
    unittest.main()
