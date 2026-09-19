"""Old value-only history indexes must retain the verified day's Metric contract."""
from copy import deepcopy
import gzip
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from industry_workbench.models import DataError, json_bytes, metric
from industry_workbench.query import QueryService, compile_views
from industry_workbench.storage import FileStore


class HistoryMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = FileStore(Path(self.tmp.name) / "runtime", development=True)
        self.uid = "SW2021:L2:110200"
        self.days = ["20260916", "20260918"]
        self.metrics = {}
        for day in self.days:
            self.metrics[day] = {
                "pe_ttm_median": metric("105.2508", day, status="SMALL_SAMPLE", valid_count=4,
                                        expected_count=5, received_count=5),
                "pb_median": metric(None, day, reason="MEMBER_LIFECYCLE_UNKNOWN", valid_count=4),
                "pe_percentile": metric(None, day, reason="HISTORY_INSUFFICIENT", valid_count=5,
                                        first_valid_date="20230112", last_valid_date=day,
                                        minimum_valid_days=252, expected_days=897, missing_days=892,
                                        equal_count=1),
                "flow_cent": metric("0", day, expected_count=5, received_count=5),
                "net_mf_vol": metric(None, day, reason="CLASSIFICATION_INCOMPLETE", known_subtotal="20"),
                "return_5d": metric(None, day, reason="RETURN_ANCHOR_MISSING", anchor_date="20260909"),
            }
        self.series = {"uid": self.uid, "dates": self.days,
                       "values": {key: [self.metrics[day][key]["value"] for day in self.days]
                                  for key in self.metrics[self.days[0]]}}
        with self.store.writer():
            self.manifest = {
                "batch_id": "old-published-batch", "as_of": self.days[-1], "history_start": self.days[0],
                "views": {
                    "calendar": self.store.put_json([
                        {"cal_date": day, "is_open": 1, "exchange": "SSE"}
                        for day in [self.days[0], "20260917", self.days[1]]], "calendar"),
                    "history": {self.uid: self.store.put_json(self.series, "industry_history")},
                    "day": {day: self.store.put_json({"trade_date": day,
                             "industries": [{"uid": self.uid, "metrics": self.metrics[day]}]}, "daily_view")
                            for day in self.days},
                    "week": {}, "month": {},
                },
            }
        self.addCleanup(patch.stopall)
        patch.object(self.store, "published_manifest", side_effect=lambda batch: deepcopy(self.manifest)).start()
        self.query = QueryService(self.store)

    def history(self, kind="day"):
        return self.query.history(self.manifest["batch_id"], self.uid, kind)

    def replace_day(self, day, value):
        with self.store.writer():
            self.manifest["views"]["day"][day] = self.store.put_json(value, "daily_view")

    def test_existing_compact_history_retains_every_original_metric_field(self):
        rows = self.history()["rows"]
        for index, day in ((0, self.days[0]), (2, self.days[1])):
            self.assertEqual(rows[index]["metrics"], self.metrics[day])
            for name, values in self.series["values"].items():
                self.assertEqual(rows[index]["metrics"][name]["value"], values[self.days.index(day)])
        self.assertEqual(rows[0]["metrics"]["pe_ttm_median"]["status"], "SMALL_SAMPLE")
        self.assertEqual(rows[0]["metrics"]["pe_percentile"]["reason_codes"], ["HISTORY_INSUFFICIENT"])
        self.assertEqual(rows[0]["metrics"]["flow_cent"]["value"], "0")

    def test_missing_date_stays_na_without_borrowing_coverage_or_filling_zero(self):
        gap = self.history()["rows"][1]
        self.assertEqual(gap["trade_date"], "20260917")
        self.assertEqual(gap["metrics"], {
            name: metric(None, "20260917", reason="HISTORICAL_VALUE_UNAVAILABLE")
            for name in self.series["values"]})

    def test_complete_history_matches_old_batch_without_reading_daily_views(self):
        expected = self.history()
        complete = {**self.series, "metrics": [self.metrics[day] for day in self.days]}
        with self.store.writer():
            self.manifest["views"]["history"][self.uid] = self.store.put_json(complete, "industry_history")
        read_json = self.store.read_json
        reads = []
        def read(ref):
            self.assertNotEqual(ref["kind"], "daily_view")
            reads.append(ref["kind"])
            return read_json(ref)
        with patch.object(self.store, "read_json", side_effect=read):
            self.assertEqual(self.history(), expected)
        self.assertEqual(reads, ["industry_history", "calendar"])

    def test_misaligned_complete_metrics_are_not_silently_downgraded(self):
        for metrics in (None, [], [self.metrics[self.days[0]]], [None, None]):
            with self.subTest(metrics=metrics):
                with self.store.writer():
                    self.manifest["views"]["history"][self.uid] = self.store.put_json(
                        {**self.series, "metrics": metrics}, "industry_history")
                with self.assertRaisesRegex(DataError, "HISTORY_METRIC_COLUMNS_INVALID"):
                    self.history()

    def test_compilation_preserves_complete_metrics_and_existing_value_columns(self):
        from industry_workbench.metrics import compute_day
        from support_day_refs import put_day_refs
        from test_independent_domain import DAY, SW_A, fixture
        inputs = fixture([("10", "1", "0"), ("20", "2", "0")])
        result = compute_day(inputs)
        with self.store.writer():
            refs = put_day_refs(self.store, inputs, result)
            views = compile_views(self.store, {DAY: refs}, [DAY], expected_result_source_sha256=refs["result_source_sha256"])
        day_row = next(row for row in self.store.read_json(views["day"][DAY])["industries"] if row["uid"] == SW_A)
        history = self.store.read_json(views["history"][SW_A])
        self.assertEqual(history["dates"], [DAY])
        self.assertEqual(history["metrics"], [day_row["metrics"]])
        self.assertEqual(history["values"], {name: [record["value"]] for name, record in day_row["metrics"].items()})

    def test_compilation_rejects_wrong_result_source_before_any_projection_write(self):
        from industry_workbench.metrics import compute_day
        from support_day_refs import put_day_refs
        from test_independent_domain import DAY, fixture
        inputs = fixture([("10", "1", "0")])
        with self.store.writer():
            refs = put_day_refs(self.store, inputs, compute_day(inputs))
            with patch.object(self.store, "put_json", side_effect=AssertionError("projection before validation")):
                with self.assertRaisesRegex(DataError, "DAY_RESULT_SOURCE_MISMATCH"):
                    compile_views(self.store, {DAY: refs}, [DAY], expected_result_source_sha256="f" * 64)

    def test_history_only_reads_existing_objects_and_cannot_mutate_them(self):
        def snapshot():
            return {str(p.relative_to(self.store.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.store.root.rglob("*") if p.is_file()}
        before = snapshot()
        with patch.object(self.store, "writer", side_effect=AssertionError("query attempted writer")), \
                patch.object(self.store, "put_json", side_effect=AssertionError("query attempted object write")):
            rows = self.history()["rows"]
            rows[0]["metrics"]["pe_ttm_median"]["valid_count"] = 999
            self.assertEqual(self.history()["rows"][0]["metrics"]["pe_ttm_median"]["valid_count"], 4)
        self.assertEqual(snapshot(), before)

    def test_corrupt_day_view_hash_is_rejected(self):
        ref = self.manifest["views"]["day"][self.days[0]]
        self.store.path(ref["path"]).write_bytes(gzip.compress(json_bytes({"forged": True}), mtime=0))
        with self.assertRaisesRegex(DataError, "OBJECT_HASH_MISMATCH"):
            self.history()

    def test_expected_day_view_cannot_silently_fall_back_to_value_only_history(self):
        del self.manifest["views"]["day"][self.days[0]]
        with self.assertRaisesRegex(DataError, "HISTORY_DAY_VIEW_MISSING"):
            self.history()

    def test_day_view_must_match_history_date(self):
        self.replace_day(self.days[0], {"trade_date": self.days[1], "industries": []})
        with self.assertRaisesRegex(DataError, "HISTORY_DAY_DATE_MISMATCH"):
            self.history()

    def test_day_view_must_have_exactly_one_matching_industry(self):
        row = {"uid": self.uid, "metrics": self.metrics[self.days[0]]}
        for industries in ([], [row, row]):
            with self.subTest(count=len(industries)):
                self.replace_day(self.days[0], {"trade_date": self.days[0], "industries": industries})
                with self.assertRaisesRegex(DataError, "HISTORY_DAY_INDUSTRY_MISMATCH"):
                    self.history()

    def test_week_and_month_keep_the_same_complete_metric_semantics(self):
        for kind, key in (("week", "20260914"), ("month", "2026-09")):
            with self.subTest(kind=kind):
                with self.store.writer():
                    self.manifest["views"][kind][key] = self.store.put_json({
                        "period": {"endpoint": self.days[-1]},
                        "industries": [{"uid": self.uid, "metrics": self.metrics[self.days[-1]]}],
                    }, "period_view")
                self.assertEqual(self.history(kind)["rows"][0]["metrics"], self.metrics[self.days[-1]])


if __name__ == "__main__":
    unittest.main()
