from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(PROJECT_ROOT / "src"))

from swivd.io_utils import read_json, sha256_file
import swivd.pipeline as pipeline_module
from swivd.pipeline import PipelineError, _safe_error, rebuild_derived, run_live
from swivd.validator import validate_run


FIELDS = {
    "trade_cal": ["exchange", "cal_date", "is_open", "pretrade_date"],
    "index_classify": [
        "index_code",
        "industry_name",
        "parent_code",
        "level",
        "industry_code",
        "is_pub",
        "src",
    ],
    "sw_daily": [
        "ts_code",
        "trade_date",
        "name",
        "open",
        "low",
        "high",
        "close",
        "change",
        "pct_change",
        "vol",
        "amount",
        "pe",
        "pb",
        "total_mv",
        "float_mv",
    ],
}


def weekdays(start: str, end: str) -> list[str]:
    cursor = date.fromisoformat(f"{start[:4]}-{start[4:6]}-{start[6:]}")
    upper = date.fromisoformat(f"{end[:4]}-{end[4:6]}-{end[6:]}")
    values: list[str] = []
    while cursor <= upper:
        if cursor.weekday() < 5:
            values.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return values


class FakeTushareClient:
    def __init__(
        self,
        *,
        fail_current: bool = False,
        fail_legacy_transport: bool = False,
        legacy_api_code: int | None = None,
        legacy_internal_gap: bool = False,
        legacy_end_name_mismatch: bool = False,
    ) -> None:
        self.all_dates = weekdays("20140101", "20221216")
        self.fail_current = fail_current
        self.fail_legacy_transport = fail_legacy_transport
        self.legacy_api_code = legacy_api_code
        self.legacy_internal_gap = legacy_internal_gap
        self.legacy_end_name_mismatch = legacy_end_name_mismatch
        self.classifications: dict[str, list[dict[str, Any]]] = {}
        for taxonomy, count, prefix in (("SW2021", 31, 801000), ("SW2014", 28, 810000)):
            self.classifications[taxonomy] = [
                {
                    "index_code": f"{prefix + position:06d}.SI",
                    "industry_name": f"{taxonomy}行业{position:02d}",
                    "parent_code": "",
                    "level": "L1",
                    "industry_code": f"{position:02d}",
                    "is_pub": "1" if taxonomy == "SW2021" else None,
                    "src": taxonomy,
                }
                for position in range(1, count + 1)
            ]
        self.legacy_late_codes = {
            row["index_code"] for row in self.classifications["SW2014"][:11]
        }
        self.names = {
            row["index_code"]: row["industry_name"]
            for rows in self.classifications.values()
            for row in rows
        }
        self.legacy_old_names = {
            "810001.SI": "SW2014旧名01",
            "810002.SI": "SW2014旧名02",
        }

    @staticmethod
    def response(api_name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        fields = FIELDS[api_name]
        return {
            "code": 0,
            "msg": "",
            "data": {
                "fields": fields,
                "items": [[row[field] for field in fields] for row in rows],
            },
        }

    def call(self, api_name: str, params: dict[str, Any], fields: Any = None) -> dict[str, Any]:
        self.assert_fields(api_name, fields)
        if api_name == "trade_cal":
            selected = [
                value
                for value in self.all_dates
                if params["start_date"] <= value <= params["end_date"]
            ]
            rows = []
            previous = "20131231"
            for value in selected:
                rows.append(
                    {
                        "exchange": "SSE",
                        "cal_date": value,
                        "is_open": "1",
                        "pretrade_date": previous,
                    }
                )
                previous = value
            return self.response(api_name, rows)
        if api_name == "index_classify":
            if self.fail_legacy_transport and params["src"] == "SW2014":
                raise OSError("synthetic transport failure before any response bytes")
            if self.legacy_api_code is not None and params["src"] == "SW2014":
                return {
                    "code": self.legacy_api_code,
                    "msg": "synthetic upstream business error",
                    "data": None,
                }
            return self.response(api_name, self.classifications[params["src"]])
        if api_name == "sw_daily":
            code = params["ts_code"]
            if self.fail_current and code == "801001.SI":
                raise RuntimeError("synthetic_current_axis_failure")
            selected = [
                value
                for value in self.all_dates
                if params["start_date"] <= value <= params["end_date"]
            ]
            if code in self.legacy_late_codes:
                selected = [value for value in selected if value >= "20140221"]
            if self.legacy_internal_gap and code == "810001.SI" and len(selected) > 300:
                selected.pop(150)
            rows = []
            code_number = int(code[:6]) % 1000
            for position, value in enumerate(selected):
                close_cents = 10000 + code_number * 10 + position
                close = f"{close_cents / 100:.2f}"
                high = (
                    f"{(close_cents - 1) / 100:.2f}"
                    if code == "810001.SI" and position == 100
                    else f"{(close_cents + 5) / 100:.2f}"
                )
                source_name = (
                    self.legacy_old_names[code]
                    if code in self.legacy_old_names and value < "20150122"
                    else self.names[code]
                )
                if (
                    self.legacy_end_name_mismatch
                    and code == "810001.SI"
                    and value == params["end_date"]
                ):
                    source_name = "错误末日名称"
                rows.append(
                    {
                        "ts_code": code,
                        "trade_date": value,
                        "name": source_name,
                        "open": close,
                        "low": f"{(close_cents - 5) / 100:.2f}",
                        "high": high,
                        "close": close,
                        "change": "0.01",
                        "pct_change": "0.01",
                        "vol": "1000",
                        "amount": "100000",
                        "pe": f"{10 + (position % 60) / 10:.2f}",
                        "pb": f"{1 + (position % 40) / 100:.2f}",
                        "total_mv": "1000000",
                        "float_mv": "800000",
                    }
                )
            return self.response(api_name, rows)
        raise AssertionError(api_name)

    def assert_fields(self, api_name: str, fields: Any) -> None:
        self.assert_equal(list(fields), FIELDS[api_name])

    @staticmethod
    def assert_equal(actual: Any, expected: Any) -> None:
        if actual != expected:
            raise AssertionError((actual, expected))


class PipelineIntegrationTests(unittest.TestCase):
    def test_frozen_cli_uses_isolated_interpreter_and_minimal_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "source"
            snapshot.mkdir()
            process_temp = root / "gate-tmp"
            completed = pipeline_module.subprocess.CompletedProcess([], 0, "{}", "")
            with patch.object(
                pipeline_module.subprocess,
                "run",
                return_value=completed,
            ) as mocked:
                pipeline_module._run_frozen_cli(
                    snapshot_root=snapshot,
                    arguments=["validate-run", "--run-dir", "/frozen/run"],
                    process_temp_root=process_temp,
                    label="VALIDATION",
                )

            command = mocked.call_args.args[0]
            options = mocked.call_args.kwargs
            self.assertEqual(command[1:6], ["-I", "-S", "-B", "-X", "utf8"])
            self.assertEqual(command[6], str(snapshot / "run_dashboard.py"))
            self.assertEqual(options["env"], {"TMPDIR": str(process_temp)})
            self.assertNotIn("HOME", options["env"])
            self.assertNotIn("PYTHONPATH", options["env"])
            self.assertNotIn("TUSHARE_TOKEN", options["env"])
            self.assertTrue(process_temp.is_dir())

    def test_rebuild_cannot_write_inside_immutable_source_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "SWIVD-RUN-20221216-001"
            run_dir.mkdir()
            nested = run_dir / "rebuilt"
            with self.assertRaisesRegex(PipelineError, "immutable source run"):
                rebuild_derived(run_dir=run_dir, output_dir=nested)
            self.assertFalse(nested.exists())

    def test_integer_upstream_reason_code_is_stable_string(self) -> None:
        class UpstreamFailure(RuntimeError):
            code = 40203

        self.assertEqual(
            _safe_error(UpstreamFailure("withheld"))["reason_code"],
            "UPSTREAM_API_CODE_40203",
        )

    def test_spec_drift_has_no_output_or_network_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            drifted_spec = root / "PROJECT_SPEC.json"
            payload = json.loads((PROJECT_ROOT / "PROJECT_SPEC.json").read_text())
            payload["axes"]["SW2021"]["expected_classification_count"] = 30
            drifted_spec.write_text(json.dumps(payload), encoding="utf-8")
            output = root / "output"

            with self.assertRaises(PipelineError):
                run_live(
                    spec_path=drifted_spec,
                    as_of="20221216",
                    output_root=output,
                    client=object(),
                )

            self.assertFalse(output.exists())

    def test_byte_identical_noncanonical_spec_has_no_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            copied_spec = root / "PROJECT_SPEC.json"
            copied_spec.write_bytes((PROJECT_ROOT / "PROJECT_SPEC.json").read_bytes())
            output = root / "output"

            with self.assertRaises(PipelineError):
                run_live(
                    spec_path=copied_spec,
                    as_of="20221216",
                    output_root=output,
                    client=object(),
                )

            self.assertFalse(output.exists())

    def test_live_network_mode_cannot_write_outside_canonical_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            with self.assertRaisesRegex(PipelineError, "canonical output root"):
                run_live(
                    spec_path=PROJECT_ROOT / "PROJECT_SPEC.json",
                    as_of="20221216",
                    output_root=output,
                    client=None,
                )
            self.assertFalse(output.exists())

    def test_full_fake_run_validates_and_rebuilds_identically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            result = run_live(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC.json",
                as_of="20221216",
                output_root=output,
                client=FakeTushareClient(),
                now=lambda: "2026-08-30T23:00:00+08:00",
            )
            run_dir = Path(result["run_dir"])
            manifest = validate_run(run_dir)
            self.assertEqual(manifest["axes"], {"SW2021": "PASS", "SW2014": "PASS"})
            self.assertEqual(result["artifact_publish_state"], "LOCAL_RESEARCH_CANDIDATE_COMPLETE")
            self.assertEqual(result["offline_rebuild_validation"]["status"], "PASS")
            self.assertEqual(
                result["offline_rebuild_validation"]["fresh_process_validation"],
                "PASS",
            )
            self.assertEqual(
                result["offline_rebuild_validation"]["fresh_process_rebuild"],
                "PASS",
            )
            source_paths = {item["path"] for item in manifest["source_files"]}
            self.assertIn("README.md", source_paths)
            self.assertIn("tests/test_pipeline.py", source_paths)
            audit = read_json(run_dir / "audit.json")
            self.assertEqual(
                audit["axes"]["SW2014"]["publication_state"],
                "NOT_PROVIDED_FOR_RETIRED_TAXONOMY",
            )
            self.assertEqual(
                audit["axes"]["SW2014"]["continuity"]["policy"],
                "PER_CODE_OBSERVED_INCEPTION_TO_COMMON_END",
            )
            self.assertEqual(
                audit["axes"]["SW2014"]["name_history_policy"],
                "STABLE_TS_CODE_WITH_SOURCE_NAME_HISTORY",
            )
            self.assertEqual(
                audit["axes"]["SW2014"]["name_history"]["renamed_codes"],
                ["810001.SI", "810002.SI"],
            )
            self.assertEqual(
                audit["axes"]["SW2014"]["ohlc_ordering"]["anomaly_count"],
                1,
            )
            self.assertEqual(
                audit["axes"]["SW2014"]["ohlc_ordering"]["anomalies"][0][
                    "anomaly_codes"
                ],
                ["HIGH_BELOW_CLOSE", "HIGH_BELOW_OPEN"],
            )
            classification_text = (
                run_dir / "inputs" / "normalized" / "classification_sw2014.csv"
            ).read_text(encoding="utf-8")
            self.assertIn("publication_state,selection_basis", classification_text.splitlines()[0])
            self.assertNotIn(",1,SW2014,", classification_text)
            history_text = (run_dir / "tables" / "sw2014_history.csv").read_text(
                encoding="utf-8"
            )
            sw2021_history_header = (
                run_dir / "tables" / "sw2021_history.csv"
            ).read_text(encoding="utf-8").splitlines()[0]
            self.assertTrue(all(line.endswith(",") for line in history_text.splitlines()[1:]))
            self.assertIn("source_name", history_text.splitlines()[0])
            self.assertNotIn("source_name", sw2021_history_header)
            self.assertIn("SW2014旧名01", history_text)
            pointer = read_json(output / "latest_run.json")
            self.assertEqual(pointer["target_sha256"], sha256_file(run_dir / "manifest.json"))
            ledger = [
                json.loads(line)
                for line in (output / "run_ledger.ndjson").read_text().splitlines()
            ]
            self.assertEqual(len(ledger), 2)
            self.assertEqual(ledger[0]["record_type"], "RUN_SUMMARY")
            self.assertEqual(ledger[0]["fresh_process_validation"], "PASS")
            self.assertEqual(ledger[0]["fresh_process_rebuild"], "PASS")
            self.assertEqual(len(ledger[0]["derived_rebuild_files"]), 6)
            self.assertFalse(ledger[0]["latest_pointer_updated"])
            self.assertEqual(ledger[1]["record_type"], "LATEST_POINTER_UPDATED")
            self.assertTrue(ledger[1]["latest_pointer_updated"])

            rebuilt = root / "rebuilt"
            rebuild_derived(run_dir=run_dir, output_dir=rebuilt)
            pairs = {
                run_dir / "tables" / "sw2021_current.csv": rebuilt / "sw2021_current.csv",
                run_dir / "tables" / "sw2021_history.csv": rebuilt / "sw2021_history.csv",
                run_dir / "tables" / "sw2014_archive.csv": rebuilt / "sw2014_archive.csv",
                run_dir / "tables" / "sw2014_history.csv": rebuilt / "sw2014_history.csv",
                run_dir / "dashboard.html": rebuilt / "dashboard.html",
                run_dir / "reports" / "adversarial_review.md": (
                    rebuilt / "reports" / "adversarial_review.md"
                ),
            }
            for original, replica in pairs.items():
                self.assertEqual(sha256_file(original), sha256_file(replica), original.name)

    def test_source_closure_drift_fails_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            before = pipeline_module._source_files(PROJECT_ROOT)
            after = [dict(item) for item in before]
            after[-1]["sha256"] = "0" * 64

            with patch.object(
                pipeline_module,
                "_source_files",
                side_effect=[before, after],
            ):
                with self.assertRaisesRegex(
                    PipelineError, "SOURCE_CLOSURE_CHANGED_DURING_RUN"
                ):
                    run_live(
                        spec_path=PROJECT_ROOT / "PROJECT_SPEC.json",
                        as_of="20221216",
                        output_root=output,
                        client=FakeTushareClient(),
                        now=lambda: "2026-08-30T23:00:00+08:00",
                    )

            self.assertFalse((output / "latest_run.json").exists())
            run_dir = next((output / "runs").iterdir())
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["execution_status"], "FAILED")
            self.assertEqual(
                manifest["error"]["reason_code"],
                "SOURCE_CLOSURE_CHANGED_DURING_RUN",
            )

    def test_failed_current_axis_never_updates_latest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            with self.assertRaises(PipelineError):
                run_live(
                    spec_path=PROJECT_ROOT / "PROJECT_SPEC.json",
                    as_of="20221216",
                    output_root=output,
                    client=FakeTushareClient(fail_current=True),
                    now=lambda: "2026-08-30T23:00:00+08:00",
                )
            self.assertFalse((output / "latest_run.json").exists())
            ledger = [json.loads(line) for line in (output / "run_ledger.ndjson").read_text().splitlines()]
            self.assertEqual(ledger[0]["execution_status"], "FAILED")

    def test_legacy_internal_gap_is_partial_and_never_updates_latest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            result = run_live(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC.json",
                as_of="20221216",
                output_root=output,
                client=FakeTushareClient(legacy_internal_gap=True),
                now=lambda: "2026-08-30T23:00:00+08:00",
            )
            self.assertEqual(result["axes"], {"SW2021": "PASS", "SW2014": "BLOCKED"})
            self.assertEqual(
                result["artifact_publish_state"],
                "LOCAL_RESEARCH_CANDIDATE_PARTIAL",
            )
            self.assertFalse((output / "latest_run.json").exists())
            audit = read_json(Path(result["run_dir"]) / "audit.json")
            self.assertEqual(
                audit["axes"]["SW2014"]["error"]["reason_code"],
                "INTERNAL_TRADING_DAY_GAP",
            )

    def test_legacy_transport_before_response_is_partial_without_raw_fabrication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            result = run_live(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC.json",
                as_of="20221216",
                output_root=output,
                client=FakeTushareClient(fail_legacy_transport=True),
                now=lambda: "2026-08-30T23:00:00+08:00",
            )
            self.assertEqual(result["axes"], {"SW2021": "PASS", "SW2014": "BLOCKED"})
            self.assertEqual(result["live_validation_state"], "PARTIAL")
            self.assertFalse((output / "latest_run.json").exists())
            run_dir = Path(result["run_dir"])
            self.assertFalse(
                (run_dir / "inputs" / "raw" / "index_classify" / "SW2014_L1.json").exists()
            )
            audit = read_json(run_dir / "audit.json")
            self.assertEqual(audit["axes"]["SW2014"]["error"]["reason_code"], "OSError")

    def test_legacy_integer_api_code_is_scoped_partial_string_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            result = run_live(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC.json",
                as_of="20221216",
                output_root=output,
                client=FakeTushareClient(legacy_api_code=40203),
                now=lambda: "2026-08-30T23:00:00+08:00",
            )
            self.assertEqual(result["axes"], {"SW2021": "PASS", "SW2014": "BLOCKED"})
            self.assertEqual(result["live_validation_state"], "PARTIAL")
            self.assertFalse((output / "latest_run.json").exists())
            audit = read_json(Path(result["run_dir"]) / "audit.json")
            self.assertEqual(
                audit["axes"]["SW2014"]["error"]["reason_code"],
                "UPSTREAM_API_CODE_40203",
            )
            blocked_request = audit["requests"][-1]
            self.assertEqual(blocked_request["decode_state"], "BLOCKED")
            self.assertEqual(blocked_request["reason_code"], "UPSTREAM_API_CODE_40203")

    def test_legacy_end_name_mismatch_is_partial_and_never_updates_latest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            result = run_live(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC.json",
                as_of="20221216",
                output_root=output,
                client=FakeTushareClient(legacy_end_name_mismatch=True),
                now=lambda: "2026-08-30T23:00:00+08:00",
            )
            self.assertEqual(result["axes"], {"SW2021": "PASS", "SW2014": "BLOCKED"})
            self.assertFalse((output / "latest_run.json").exists())
            audit = read_json(Path(result["run_dir"]) / "audit.json")
            self.assertEqual(
                audit["axes"]["SW2014"]["error"]["reason_code"],
                "END_NAME_MISMATCH",
            )

    def test_latest_pointer_failure_does_not_relabel_validated_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            original_write_json = pipeline_module.write_json

            def fail_only_latest(path: str | Path, value: Any, **kwargs: Any) -> None:
                if Path(path).name == "latest_run.json":
                    raise OSError("simulated atomic pointer publication failure")
                original_write_json(path, value, **kwargs)

            with patch.object(pipeline_module, "write_json", side_effect=fail_only_latest):
                with self.assertRaises(PipelineError):
                    run_live(
                        spec_path=PROJECT_ROOT / "PROJECT_SPEC.json",
                        as_of="20221216",
                        output_root=output,
                        client=FakeTushareClient(),
                        now=lambda: "2026-08-30T23:00:00+08:00",
                    )

            self.assertFalse((output / "latest_run.json").exists())
            run_dir = next((output / "runs").iterdir())
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["execution_status"], "COMPLETED")
            self.assertEqual(
                manifest["artifact_publish_state"],
                "LOCAL_RESEARCH_CANDIDATE_COMPLETE",
            )
            ledger = [
                json.loads(line)
                for line in (output / "run_ledger.ndjson").read_text().splitlines()
            ]
            self.assertEqual(ledger[-1]["record_type"], "PUBLICATION_FAILURE")
            self.assertFalse(ledger[-1]["latest_pointer_updated"])


if __name__ == "__main__":
    unittest.main()
