"""CLI side effects use temporary roots; no live account or old source data."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from industry_workbench import cli
from industry_workbench.models import DataError, json_bytes
from industry_workbench.storage import FileStore


RUN = "SWIVD2-RUN-20260901-001"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workbench-cli-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = FileStore(self.base / "new", development=True)

    def test_no_command_is_usage_not_serve(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.main([])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_default_directory_is_approved_independent_macos_root(self):
        with patch("industry_workbench.cli.sys.platform", "darwin"), patch("industry_workbench.cli.Path.home", return_value=self.base):
            self.assertEqual(cli.data_root(None), self.base / "Library/Application Support/ashare-industry")
        args = cli.parser().parse_args(["backfill", "--start-date", "20260901", "--end-date", "20260916", "--max-days", "1"])
        self.assertEqual(args.max_days, 1)

    def test_history_retry_is_explicit_boolean_and_only_for_backfill(self):
        base = ["backfill", "--start-date", "20260901", "--end-date", "20260916"]
        self.assertIs(cli.parser().parse_args(base).retry_failed, False)
        self.assertIs(cli.parser().parse_args(base + ["--retry-failed"]).retry_failed, True)
        for args in [base + ["--retry-failed", "false"], ["update", "--retry-failed"]]:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                cli.main(args)
            self.assertEqual(caught.exception.code, 2)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_history_retry_reaches_jobs_and_incomplete_results_exit_unsuccessfully(self):
        job = {"job_id": "H1", "kind": "backfill", "status": "FAILED", "error": {"code": "HISTORY_INCOMPLETE"},
               "result": {"attempted_days": ["20260901"], "captured_days": [], "pending_dates": [],
                          "blocked_dates": [{"trade_date": "20260901", "code": "INDEPENDENT_UNIVERSE_INCOMPLETE"}],
                          "remaining_days": 1, "remaining_attemptable_days": 0, "scan_complete": True, "history_complete": False}}
        args = ["backfill", "--start-date", "20260901", "--end-date", "20260916", "--data-dir", str(self.store.root), "--development"]
        for retry in [False, True]:
            with self.subTest(retry=retry), patch("industry_workbench.jobs.Pipeline"), patch("industry_workbench.jobs.JobManager") as manager, contextlib.redirect_stdout(io.StringIO()) as output:
                manager.return_value.submit.return_value = job
                status = cli.main(args + (["--retry-failed"] if retry else []))
                self.assertEqual(status, 1)
                self.assertEqual(json.loads(output.getvalue()), job)
                manager.return_value.submit.assert_called_once_with("backfill", {"start_date": "20260901", "end_date": "20260916", "retry_failed": retry}, asynchronous=False)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_doctor_does_not_write_or_expose_token(self):
        output = io.StringIO()
        def token():
            print("PRIVATE_TOKEN_CANARY")
            return "PRIVATE_TOKEN_CANARY"
        source = {"commit": "abc", "git_dirty": True}
        with patch("swivd.tushare_client.load_tushare_token", side_effect=token), patch("industry_workbench.storage.source_identity", return_value=source), contextlib.redirect_stdout(output):
            result = cli.doctor(self.store.root, development=True)
        self.assertTrue(result["token_configured"])
        self.assertFalse(result["network_checked"])
        self.assertFalse(result["formal_source_eligible"])
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(result) + output.getvalue())
        self.assertEqual(list(self.base.iterdir()), [])

    def test_verify_empty_is_explicit_and_read_only(self):
        self.assertEqual(cli.verify_batch(self.store)["status"], "EMPTY")
        self.assertFalse(self.store.root.exists())

    def publish_fixture(self):
        body = b"source fixture\n"
        source_files = {"src/example.py": hashlib.sha256(body).hexdigest()}
        archive_bytes = io.BytesIO()
        with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
            item = tarfile.TarInfo("src/example.py")
            item.size = len(body)
            archive.addfile(item, io.BytesIO(body))
        with self.store.writer():
            raw = self.store.put_json({"data": {"rows": []}}, "tushare_response")
            nested = self.store.put_json({"trade_date": "20260901", "source_refs": [raw]}, "day_input")
            source_ref = self.store.put_bytes(archive_bytes.getvalue(), "source_snapshot_tar_gz")
            source = {"files": source_files, "git_dirty": True, "commit": "fixture", "tree_sha256": hashlib.sha256(json_bytes(source_files)).hexdigest()}
            refs = {"input": nested, "result": self.store.put_json({"trade_date": "20260901", "industries": []}, "day_result"),
                    "input_contract_version": "industry-workbench-day-input-v1", "result_source_sha256": source["tree_sha256"],
                    "input_source": {"identity": {key: source[key] for key in ("commit", "git_dirty", "tree_sha256")},
                                     "inventory": self.store.put_json(source, "source_identity"), "snapshot": source_ref}}
            manifest = self.store.publish({"as_of": "20260901", "source": source,
                                           "source_snapshot": source_ref, "days": {"20260901": refs},
                                           "provider_kind": "TEST_INJECTED_CLIENT", "publication_state": "PUBLISHED_WITH_GAPS"})
        return manifest, raw

    def test_verify_walks_nested_raw_refs_and_freeze_without_writes(self):
        manifest, raw = self.publish_fixture()
        before = self.inventory(self.store.root)
        result = cli.verify_batch(self.store)
        self.assertEqual(result["verified_objects"], 5)
        self.assertEqual(result["batch_id"], manifest["batch_id"])
        self.assertEqual(before, self.inventory(self.store.root))
        self.store.path(raw["path"]).write_bytes(b"corrupt")
        with self.assertRaises((DataError, OSError)):
            cli.verify_batch(self.store)

    def test_legacy_source_cannot_become_new_data_root(self):
        self.store.root.mkdir()
        (self.store.root / "latest_run.json").write_text("{}")
        before = self.inventory(self.store.root)
        with contextlib.redirect_stderr(io.StringIO()) as output:
            status = cli.main(["verify", "--data-dir", str(self.store.root), "--development"])
        self.assertEqual(status, 1)
        self.assertIn("LEGACY_DATA_ROOT_REQUIRES_IMPORT", output.getvalue())
        self.assertEqual(before, self.inventory(self.store.root))

    def test_verify_rejects_source_inventory_even_with_new_receipt(self):
        manifest, _ = self.publish_fixture()
        manifest["source"]["tree_sha256"] = "0" * 64
        path = self.store.root / "batches" / manifest["batch_id"] / "manifest.json"
        raw = json_bytes(manifest)
        path.write_bytes(raw)
        path.with_suffix(".sha256").write_text(hashlib.sha256(raw).hexdigest())
        with self.assertRaisesRegex(DataError, "SOURCE_INVENTORY_HASH_MISMATCH"):
            cli.verify_batch(self.store, manifest["batch_id"])

    def test_verify_preserves_distinct_historical_capture_source(self):
        manifest, _ = self.publish_fixture()
        body = b"previous capture version\n"
        files = {"old_capture.py": hashlib.sha256(body).hexdigest()}
        source = {"commit": "old", "git_dirty": True, "files": files, "tree_sha256": hashlib.sha256(json_bytes(files)).hexdigest()}
        archive_bytes = io.BytesIO()
        with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
            item = tarfile.TarInfo("old_capture.py"); item.size = len(body)
            archive.addfile(item, io.BytesIO(body))
        with self.store.writer():
            snapshot = self.store.put_bytes(archive_bytes.getvalue(), "source_snapshot_tar_gz")
            inventory = self.store.put_json(source, "source_identity")
            manifest["days"]["20260901"]["input_source"] = {"identity": {key: source[key] for key in ("commit", "git_dirty", "tree_sha256")}, "inventory": inventory, "snapshot": snapshot}
            updated = self.store.publish(manifest)
        self.assertEqual(cli.verify_batch(self.store)["batch_id"], updated["batch_id"])
        self.assertEqual(cli.verify_batch(self.store)["verified_objects"], 6)

    def legacy_fixture(self):
        root = self.base / "old" / RUN
        root.mkdir(parents=True)
        (root / "ui").mkdir()
        body = b'{"industries": []}'
        (root / "ui/catalog.json").write_bytes(body)
        manifest = {"schema_version": "swivd-local-snapshot-manifest-v4", "run_id": RUN, "as_of": "20260901", "purpose": "UPDATE_LATEST",
                    "artifacts": [{"path": "ui/catalog.json", "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}]}
        (root / "manifest.json").write_bytes(json_bytes(manifest))
        sums = [f"{hashlib.sha256((root/name).read_bytes()).hexdigest()}  {name}" for name in ("manifest.json", "ui/catalog.json")]
        (root / "SHA256SUMS").write_text("\n".join(sums) + "\n")
        (root / "not-in-manifest.txt").write_text("must not copy")
        return root, manifest

    def test_import_validates_staged_copy_preserves_source_and_refuses_overwrite(self):
        source, manifest = self.legacy_fixture()
        before = self.inventory(source)
        with patch("industry_workbench.legacy.validate_run_v2", return_value=manifest) as validate:
            result = cli.import_legacy(self.store, source)
            self.assertEqual(result["status"], "IMPORTED_READ_ONLY")
            self.assertGreaterEqual(validate.call_count, 3)
            self.assertEqual(before, self.inventory(source))
            imported = self.store.root / "legacy" / "runs" / RUN
            self.assertEqual((imported / "manifest.json").read_bytes(), (source / "manifest.json").read_bytes())
            self.assertEqual((imported / "SHA256SUMS").read_bytes(), (source / "SHA256SUMS").read_bytes())
            self.assertFalse((imported / "not-in-manifest.txt").exists())
            with self.assertRaisesRegex(DataError, "LEGACY_ALREADY_IMPORTED"):
                cli.import_legacy(self.store, source)
        self.assertEqual(list(self.store.root.glob(".legacy-import-*")), [])

    def test_failed_staging_does_not_install_or_modify_source(self):
        source, manifest = self.legacy_fixture()
        before = self.inventory(source)
        def validate(root):
            if root != source:
                raise ValueError("STAGING_REJECTED")
            return manifest
        with patch("industry_workbench.legacy.validate_run_v2", side_effect=validate), self.assertRaisesRegex(ValueError, "STAGING_REJECTED"):
            cli.import_legacy(self.store, source)
        self.assertFalse((self.store.root / "legacy").exists())
        self.assertEqual(list(self.store.root.glob(".legacy-import-*")), [])
        self.assertEqual(before, self.inventory(source))

    def test_source_destination_overlap_denied_before_write(self):
        with self.assertRaisesRegex(DataError, "OVERLAP"):
            cli.import_legacy(self.store, self.base)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_empty_offline_serve_does_not_create_data_or_launch_old_app(self):
        # Server transport is stubbed; actual loopback/Chrome acceptance is separate.
        with patch("industry_workbench.server.WorkbenchServer") as server, patch("swivd.v2_server.LocalApp.__init__", side_effect=AssertionError("old app forbidden")), contextlib.redirect_stdout(io.StringIO()):
            server.return_value.origin = "http://127.0.0.1:8765"
            cli.serve(self.store, no_scheduler=True)
            server.return_value.serve_forever.assert_called_once()
            server.return_value.server_close.assert_called_once()
        self.assertFalse(self.store.root.exists())

    def test_cli_exception_is_redacted(self):
        with patch("industry_workbench.cli.doctor", side_effect=RuntimeError("PRIVATE_SECRET_CANARY")), contextlib.redirect_stderr(io.StringIO()) as output:
            status = cli.main(["doctor", "--data-dir", str(self.store.root), "--development"])
        self.assertEqual(status, 1)
        self.assertNotIn("PRIVATE_SECRET", output.getvalue())
        self.assertIn("RuntimeError", output.getvalue())

    @staticmethod
    def inventory(root):
        return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


if __name__ == "__main__":
    unittest.main()
