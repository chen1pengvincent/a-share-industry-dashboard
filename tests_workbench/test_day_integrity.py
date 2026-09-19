"""Offline recovery faults must fail despite valid content-addressed objects."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from industry_workbench.cli import verify_batch
from industry_workbench.day_integrity import validate_day_refs
from industry_workbench.history_state import HistoryState
from industry_workbench.jobs import Pipeline, SHANGHAI
from industry_workbench.models import DataError
from industry_workbench.query import compile_views
from industry_workbench.storage import FileStore
from test_application import FixtureProvider


class DayIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="day-binding-test-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.source = base / "source"
        (self.source / "src/industry_workbench").mkdir(parents=True)
        (self.source / "src/industry_workbench/fixture.py").write_text("# frozen source\n")
        self.store = FileStore(base / "runtime", development=True)
        FixtureProvider.calls, FixtureProvider.fail_day = [], None
        self.pipeline = Pipeline(self.store, self.source, provider_factory=FixtureProvider,
                                 clock=lambda: datetime(2026, 9, 17, 20, tzinfo=SHANGHAI))

    def latest(self):
        self.pipeline.run("backfill", {"start_date": "20260916", "end_date": "20260917", "max_days": 1})
        return self.store.current()

    def retarget_checkpoint(self, change=None):
        manifest = self.latest()
        checkpoint = json.loads(self.store.path("checkpoints/20260917.json").read_bytes())
        checkpoint["trade_date"] = "20260916"
        if change:
            change(checkpoint)
        self.store.path("checkpoints/20260916.json").write_text(json.dumps(checkpoint))
        return manifest, checkpoint

    def test_other_date_valid_refs_are_rejected_before_publish_or_fetch(self):
        manifest, _ = self.retarget_checkpoint()
        pointer = self.store.path("current.json").read_bytes()
        with self.assertRaisesRegex(DataError, "DAY_DATE_MISMATCH"):
            self.pipeline.run("backfill", {"start_date": "20260916", "end_date": "20260916"})
        self.assertEqual(self.store.path("current.json").read_bytes(), pointer)
        self.assertEqual(self.store.current()["batch_id"], manifest["batch_id"])
        self.assertEqual(FixtureProvider.calls, ["20260917"])

    def test_normal_checkpoint_after_interrupted_publish_is_reused(self):
        with patch.object(self.pipeline, "_publish_days", side_effect=DataError("INJECTED_PREPUBLICATION_STOP")):
            with self.assertRaisesRegex(DataError, "INJECTED_PREPUBLICATION_STOP"):
                self.pipeline.run("update", {})
        self.assertIsNone(self.store.current())
        self.assertTrue(self.store.path("checkpoints/20260917.json").exists())
        self.pipeline.run("update", {})
        self.assertEqual(FixtureProvider.calls, ["20260917"])
        self.assertEqual(verify_batch(self.store)["status"], "VERIFIED")

    def test_checkpoint_source_and_contract_must_match(self):
        self.latest()
        original = json.loads(self.store.path("checkpoints/20260917.json").read_bytes())
        mutations = [
            (lambda value: value.pop("source_sha256"), "CHECKPOINT_SOURCE_INVALID"),
            (lambda value: value["day_refs"].update(result_source_sha256="f" * 64), "DAY_RESULT_SOURCE_MISMATCH"),
            (lambda value: value["day_refs"].update(input_contract_version="foreign-v9"), "DAY_INPUT_CONTRACT_MISMATCH"),
            (lambda value: value["day_refs"]["input_source"]["identity"].update(tree_sha256="f" * 64), "DAY_CAPTURE_SOURCE_INVALID"),
        ]
        for mutation, code in mutations:
            with self.subTest(code=code):
                value = deepcopy(original)
                mutation(value)
                self.store.path("checkpoints/20260917.json").write_text(json.dumps(value))
                with self.assertRaisesRegex(DataError, code):
                    self.pipeline._load_checkpoint("20260917", self.pipeline.source_check())

    def test_capture_snapshot_with_valid_hash_but_wrong_contents_is_rejected(self):
        manifest = self.latest()
        refs = deepcopy(manifest["days"]["20260917"])
        with self.store.writer():
            refs["input_source"]["snapshot"] = self.store.put_bytes(b"not the frozen archive", "source_snapshot_tar_gz")
        with self.assertRaisesRegex(DataError, "DAY_CAPTURE_SOURCE_INVALID"):
            validate_day_refs(self.store, "20260917", refs)

    def test_result_date_and_metric_date_are_checked_separately(self):
        manifest = self.latest()
        for change, code in (
                (lambda result: result.update(trade_date="20260916"), "DAY_DATE_MISMATCH"),
                (lambda result: result["industries"][0]["metrics"]["pe_ttm_median"].update(metric_date="20260916"), "DAY_METRIC_DATE_MISMATCH")):
            with self.subTest(code=code):
                refs = deepcopy(manifest["days"]["20260917"])
                result = self.store.read_json(refs["result"])
                change(result)
                with self.store.writer():
                    refs["result"] = self.store.put_json(result, "day_result")
                with self.assertRaisesRegex(DataError, code):
                    validate_day_refs(self.store, "20260917", refs)

    def test_projection_and_publish_check_dates_without_checkpoint(self):
        manifest = self.latest()
        bad_days = {"20260916": manifest["days"]["20260917"]}
        calendar = FixtureProvider().calendar("20260914", "20260917")
        pointer = self.store.path("current.json").read_bytes()
        with self.store.writer():
            with self.assertRaisesRegex(DataError, "DAY_DATE_MISMATCH"):
                compile_views(self.store, bad_days, calendar)
            with self.assertRaisesRegex(DataError, "DAY_DATE_MISMATCH"):
                self.pipeline._publish_days(days=bad_days, calendar=calendar, history_start="20260914",
                    source=manifest["source"], frozen_source=manifest["source_snapshot"], calendar_source_refs=[],
                    provider=FixtureProvider(), history=HistoryState(self.store, manifest["source"]), progress=lambda *args: None)
        self.assertEqual(self.store.path("current.json").read_bytes(), pointer)

    def test_verify_rejects_manifest_date_rebinding_despite_valid_hashes(self):
        manifest = self.latest()
        malformed = deepcopy(manifest)
        malformed["days"]["20260916"] = malformed["days"]["20260917"]
        with self.store.writer():
            published = self.store.publish(malformed)
        with self.assertRaisesRegex(DataError, "DAY_DATE_MISMATCH"):
            verify_batch(self.store, published["batch_id"])

    def test_verify_binds_result_to_manifest_source_not_running_checkout(self):
        manifest = self.latest()
        (self.source / "src/industry_workbench/fixture.py").write_text("# later checkout\n")
        self.assertEqual(verify_batch(self.store, manifest["batch_id"])["status"], "VERIFIED")
        malformed = deepcopy(manifest)
        malformed["days"]["20260917"]["result_source_sha256"] = "f" * 64
        with self.store.writer():
            published = self.store.publish(malformed)
        with self.assertRaisesRegex(DataError, "DAY_RESULT_SOURCE_MISMATCH"):
            verify_batch(self.store, published["batch_id"])

    def test_normal_checkpoint_reaudits_financial_values(self):
        manifest = self.latest()
        checkpoint = json.loads(self.store.path("checkpoints/20260917.json").read_bytes())
        result = self.store.read_json(manifest["days"]["20260917"]["result"])
        result["industries"][0]["metrics"]["pe_ttm_median"]["value"] = "999"
        with self.store.writer():
            checkpoint["day_refs"]["result"] = self.store.put_json(result, "day_result")
        self.store.path("checkpoints/20260917.json").write_text(json.dumps(checkpoint))
        with self.assertRaisesRegex(DataError, "INDEPENDENT_RECALCULATION_FAILED"):
            self.pipeline._load_checkpoint("20260917", self.pipeline.source_check())

    def test_formal_reader_rejects_development_input_and_capture_identity(self):
        manifest = self.latest()
        original = manifest["days"]["20260917"]
        formal = FileStore(self.store.root)

        def coherent_refs(*, provider="LIVE_SECURE_TUSHARE", dirty=False, commit="a" * 40):
            refs = deepcopy(original)
            inputs = self.store.read_json(refs["input"])
            inputs["provider_kind"] = provider
            inventory = self.store.read_json(refs["input_source"]["inventory"])
            inventory.update(commit=commit, git_dirty=dirty)
            refs["input_source"]["identity"].update(commit=commit, git_dirty=dirty)
            with self.store.writer():
                refs["input"] = self.store.put_json(inputs, "day_input")
                refs["input_source"]["inventory"] = self.store.put_json(inventory, "source_identity")
            return refs

        valid = coherent_refs()
        self.assertEqual(validate_day_refs(formal, "20260917", valid)[0]["provider_kind"], "LIVE_SECURE_TUSHARE")
        for overrides in ({"provider": "TEST_INJECTED_CLIENT"}, {"provider": None}, {"dirty": True},
                          {"dirty": "False"}, {"commit": None}, {"commit": ""}, {"commit": "not-a-commit"}):
            with self.subTest(overrides=overrides):
                refs = coherent_refs(**overrides)
                # The failure is semantic, not a corrupt hash or archive.
                self.store.verify_refs(refs)
                with self.assertRaisesRegex(DataError, "DEVELOPMENT_INPUT_IN_FORMAL_ROOT"):
                    validate_day_refs(formal, "20260917", refs)
                self.assertEqual(validate_day_refs(self.store, "20260917", refs)[0]["trade_date"], "20260917")


if __name__ == "__main__":
    unittest.main()
