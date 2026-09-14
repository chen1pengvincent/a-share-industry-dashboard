from __future__ import annotations

import http.client
import json
import re
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from swivd.io_utils import read_csv, read_json, sha256_file, write_csv, write_json
from swivd.tushare_client import (
    ENDPOINT_FIELDS,
    RawApiResponse,
    SecureTushareClient,
    TushareConfigurationError,
    TushareProtocolError,
)
from swivd.v2_domain import (
    V2DataError,
    compute_peer_rows,
    normalize_classifications,
    normalize_members,
    select_members_as_of,
)
from swivd.v2_identity import IdentityResolutionError
from swivd.v2_server import LocalApp, LoopbackHTTPServer, _handler
from swivd.v2_storage import (
    POINTER_SCOPE,
    SingleWriterLock,
    inventory_artifacts,
    publish_current,
    publish_historical,
    resolve_pointer,
    write_sums,
)
from swivd.v2_pipeline import (
    UpdateTarget,
    V2PipelineError,
    execute_update_locked,
    rebuild_v2,
    resolve_update_target,
    run_snapshot,
)
from swivd.v2_validator import _decode_raw_rows, validate_run_v2, validate_spec_v4
from swivd.v2_jobs import JobManager


@contextmanager
def running_local_app(testcase: unittest.TestCase, data_dir: Path):
    """Run the real loopback handler without starting any data job."""

    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
    except PermissionError:
        probe.close()
        testcase.skipTest("sandbox forbids loopback bind; run in host E2E")
    port = probe.getsockname()[1]
    probe.close()
    app = LocalApp(project_root=PROJECT_ROOT, data_dir=data_dir, host="127.0.0.1", port=port)
    server = LoopbackHTTPServer(("127.0.0.1", port), _handler(app))
    server.daemon_threads = True
    server.block_on_close = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield app, port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def local_request(
    port: int,
    method: str,
    path: str,
    *,
    body: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, payload
    finally:
        connection.close()


def classifications():
    # The only endpoint-code anomaly in the governed fixture is the real
    # SW2021 special-steel path.  Every synthetic row remains a DIRECT identity.
    l1 = [
        {
            "src": "SW2021",
            "level": "L1",
            "index_code": "801040.SI",
            "industry_name": "钢铁",
            "industry_code": "230000",
            "parent_code": "0",
            "is_pub": 1,
        },
        *[
            {
                "src": "SW2021",
                "level": "L1",
                "index_code": f"81{i:04d}.SI",
                "industry_name": f"一级{i}",
                "industry_code": f"A{i:03d}",
                "parent_code": "",
                "is_pub": 1 if i == 1 else 0,
            }
            for i in range(1, 31)
        ],
    ]
    l2 = [
        {
            "src": "SW2021",
            "level": "L2",
            "index_code": "801045.SI",
            "industry_name": "特钢Ⅱ",
            "industry_code": "230500",
            "parent_code": "230000",
            "is_pub": 1,
        },
        *[
            {
                "src": "SW2021",
                "level": "L2",
                "index_code": f"82{i:04d}.SI",
                "industry_name": f"二级{i}",
                "industry_code": f"B{i:03d}",
                "parent_code": f"A{((i - 1) % 30) + 1:03d}",
                "is_pub": 0,
            }
            for i in range(1, 134)
        ],
    ]
    l3 = [
        {
            "src": "SW2021",
            "level": "L3",
            "index_code": "850401.SI",
            "industry_name": "特钢Ⅲ",
            "industry_code": "230501",
            "parent_code": "230500",
            "is_pub": 1,
        },
        *[
            {
                "src": "SW2021",
                "level": "L3",
                "index_code": f"83{i:04d}.SI",
                "industry_name": f"三级{i}",
                "industry_code": f"C{i:03d}",
                "parent_code": f"B{((i - 1) % 133) + 1:03d}",
                "is_pub": 1 if i == 1 else 0,
            }
            for i in range(1, 346)
        ],
    ]
    return {"L1": l1, "L2": l2, "L3": l3}


def member(
    code: str,
    *,
    in_date: str = "20211201",
    out_date: str = "",
    is_new: str = "Y",
    special_steel_endpoint: bool = False,
):
    if special_steel_endpoint:
        path = {
            "l1_code": "801040.SI",
            "l1_name": "钢铁",
            "l2_code": "801045.SI",
            "l2_name": "特钢Ⅱ",
            "l3_code": "850412.SI",
            "l3_name": "特钢Ⅲ",
        }
    else:
        path = {
            "l1_code": "810001.SI",
            "l1_name": "一级1",
            "l2_code": "820001.SI",
            "l2_name": "二级1",
            "l3_code": "830001.SI",
            "l3_name": "三级1",
        }
    return {
        **path,
        "ts_code": code, "name": code, "in_date": in_date,
        "out_date": out_date, "is_new": is_new,
    }


class CalendarClient:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def call(self, api_name, params, fields):
        if api_name != "trade_cal":
            raise AssertionError(api_name)
        self.calls.append(dict(params))
        ordered = list(ENDPOINT_FIELDS[api_name])
        rows = [
            row for row in self.rows
            if params["start_date"] <= row["cal_date"] <= params["end_date"]
        ]
        payload = {
            "code": 0,
            "msg": None,
            "data": {
                "fields": ordered,
                "items": [[row.get(field) for field in ordered] for row in rows],
            },
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        return RawApiResponse(
            payload,
            raw_bytes=raw,
            http_status=200,
            headers={},
            api_name=api_name,
            attempt_count=1,
        )


def calendar_row(date, *, is_open, previous):
    return {
        "exchange": "SSE",
        "cal_date": date,
        "is_open": is_open,
        "pretrade_date": previous,
    }


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.normalized, self.paths = normalize_classifications(classifications())

    def test_hierarchy_counts_and_parent_drift(self):
        self.assertEqual({k: len(v) for k, v in self.normalized.items()}, {"L1": 31, "L2": 134, "L3": 346})
        broken = classifications()
        broken["L3"][0]["parent_code"] = "MISSING"
        with self.assertRaisesRegex(V2DataError, "CLASSIFICATION_PARENT_MISSING"):
            normalize_classifications(broken)

    def test_y_n_reconciliation_boundary_and_overlap(self):
        rows = [member("000001.SZ"), member("000001.SZ", out_date="20220110", is_new="N")]
        episodes = normalize_members(rows, l3_paths=self.paths)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["out_date"], "20220110")
        self.assertEqual(select_members_as_of(episodes, "20220110")[0]["membership_state"], "MEMBERSHIP_BOUNDARY_UNKNOWN")
        conflict = rows + [member("000001.SZ", out_date="20220111", is_new="N")]
        with self.assertRaisesRegex(V2DataError, "MEMBERSHIP_EPISODE_CONFLICT"):
            normalize_members(conflict, l3_paths=self.paths)

    def test_membership_primary_key_duplicate_never_silently_deduplicates(self):
        original = member("000001.SZ")
        for duplicate in (
            dict(original),
            {**original, "name": "同一主键但不同名称"},
        ):
            with self.subTest(duplicate_name=duplicate["name"]):
                with self.assertRaisesRegex(
                    V2DataError, "MEMBERSHIP_PRIMARY_KEY_DUPLICATE"
                ):
                    normalize_members([original, duplicate], l3_paths=self.paths)

    def test_percentile_positive_ties_missing_and_minimum(self):
        members = []
        values = [10, 20, 20, -1, None]
        valuations = {}
        for index, value in enumerate(values, start=1):
            row = member(f"00000{index}.SZ")
            row["membership_state"] = "ACTIVE"
            members.append(row)
            if value is not None:
                valuations[row["ts_code"]] = {"ts_code": row["ts_code"], "trade_date": "20211213", "close": 1, "pe": 1, "pe_ttm": value, "pb": index, "ps_ttm": 1, "dv_ttm": 1, "total_mv": 1, "circ_mv": 1}
        result = compute_peer_rows(members, valuations, as_of="20211213", minimum_peers=3)
        target = next(row for row in result if row["level"] == "L3" and row["ts_code"] == "000002.SZ")
        self.assertEqual(target["pe_ttm_percentile_le"], Decimal("100"))
        self.assertEqual(target["pe_ttm_valid_n"], 3)
        self.assertEqual(target["pe_ttm_tie_count"], 2)
        missing = next(row for row in result if row["level"] == "L3" and row["ts_code"] == "000005.SZ")
        self.assertEqual(missing["valuation_state"], "VALUATION_UNAVAILABLE")
        self.assertEqual(missing["pe_ttm_percentile_state"], "CURRENT_INVALID")

    def test_unknown_member_blocks_entire_peer_group(self):
        rows = [member(f"00000{i}.SZ") for i in range(1, 6)]
        for row in rows:
            row["membership_state"] = "ACTIVE"
        rows[0]["membership_state"] = "MEMBERSHIP_BOUNDARY_UNKNOWN"
        valuations = {row["ts_code"]: {"ts_code": row["ts_code"], "trade_date": "20211213", "close": 1, "pe": 1, "pe_ttm": 10, "pb": 1, "ps_ttm": 1, "dv_ttm": 1, "total_mv": 1, "circ_mv": 1} for row in rows}
        result = compute_peer_rows(rows, valuations, as_of="20211213")
        self.assertTrue(all(row["pe_ttm_percentile_state"] == "MEMBERSHIP_UNKNOWN" for row in result))


class UpdateTargetTests(unittest.TestCase):
    def test_open_day_before_cutoff_selects_pretrade_across_month(self):
        client = CalendarClient([calendar_row("20260901", is_open=1, previous="20260831")])
        target = resolve_update_target(
            client=client,
            now=datetime(2026, 9, 1, 14, 55, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        self.assertEqual(target.as_of, "20260831")
        self.assertEqual(target.reason_code, "PREVIOUS_OPEN_DAY_BEFORE_SW_DAILY_CUTOFF")
        self.assertEqual(target.timezone, "Asia/Shanghai")
        self.assertEqual(target.cutoff, "18:30:00")
        self.assertEqual(
            client.calls,
            [{"exchange": "SSE", "start_date": "20260901", "end_date": "20260901"}],
        )

    def test_non_trading_day_uses_pretrade_across_year(self):
        target = resolve_update_target(
            client=CalendarClient(
                [calendar_row("20270101", is_open=0, previous="20261231")]
            ),
            now=datetime(2027, 1, 1, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        self.assertEqual(target.as_of, "20261231")
        self.assertEqual(target.reason_code, "LATEST_OPEN_DAY_NON_TRADING_DATE")

    def test_cutoff_boundary_and_utc_conversion(self):
        row = calendar_row("20260901", is_open=1, previous="20260831")
        before = resolve_update_target(
            client=CalendarClient([row]),
            now=datetime(2026, 9, 1, 10, 29, 59, tzinfo=timezone.utc),
        )
        at_cutoff = resolve_update_target(
            client=CalendarClient([row]),
            now=datetime(2026, 9, 1, 10, 30, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(before.as_of, "20260831")
        self.assertEqual(at_cutoff.as_of, "20260901")
        self.assertEqual(
            at_cutoff.reason_code,
            "CURRENT_OPEN_DAY_AT_OR_AFTER_SW_DAILY_CUTOFF",
        )
        self.assertTrue(at_cutoff.evaluated_at.endswith("+08:00"))

    def test_non_trading_day_uses_exact_pretrade_at_any_time(self):
        row = calendar_row("20260905", is_open=0, previous="20260904")
        for hour in (8, 21):
            with self.subTest(hour=hour):
                target = resolve_update_target(
                    client=CalendarClient([row]),
                    now=datetime(2026, 9, 5, hour, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                )
                self.assertEqual(target.as_of, "20260904")
                self.assertEqual(target.reason_code, "LATEST_OPEN_DAY_NON_TRADING_DATE")

    def test_invalid_calendar_clock_and_pretrade_fail_closed(self):
        valid_now = datetime(2026, 9, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with self.assertRaisesRegex(V2PipelineError, "CURRENT_TRADE_CALENDAR_ROW_INVALID"):
            resolve_update_target(client=CalendarClient([]), now=valid_now)
        with self.assertRaisesRegex(V2PipelineError, "PREVIOUS_OPEN_DAY_INVALID"):
            resolve_update_target(
                client=CalendarClient([calendar_row("20260901", is_open=1, previous="")]),
                now=valid_now,
            )
        with self.assertRaisesRegex(V2PipelineError, "NAIVE_UPDATE_CLOCK"):
            resolve_update_target(
                client=CalendarClient([calendar_row("20260901", is_open=1, previous="20260831")]),
                now=datetime(2026, 9, 1, 12, 0),
            )

    def test_cli_update_selects_target_inside_single_writer_lock(self):
        events = []
        target = UpdateTarget(
            as_of="20260831",
            reason_code="PREVIOUS_OPEN_DAY_BEFORE_SW_DAILY_CUTOFF",
            evaluated_at="2026-09-01T14:55:00+08:00",
        )

        class RecordingLock:
            def __init__(self, _data_dir):
                pass

            def __enter__(self):
                events.append("lock-enter")
                return self

            def __exit__(self, *_args):
                events.append("lock-exit")

        def select_target(**_kwargs):
            self.assertEqual(events, ["lock-enter"])
            events.append("target-selected")
            return target

        def build_snapshot(**kwargs):
            self.assertEqual(events, ["lock-enter", "target-selected", "callback"])
            self.assertEqual(kwargs["as_of"], target.as_of)
            events.append("snapshot")
            return {"run_id": "SWIVD2-RUN-20260831-001", "as_of": target.as_of}

        with mock.patch("swivd.v2_pipeline.SingleWriterLock", RecordingLock), mock.patch(
            "swivd.v2_pipeline.resolve_update_target", side_effect=select_target
        ), mock.patch("swivd.v2_pipeline.run_snapshot", side_effect=build_snapshot):
            result = execute_update_locked(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json",
                data_dir=Path("unused"),
                target_selected=lambda _target: events.append("callback"),
            )
        self.assertEqual(events, ["lock-enter", "target-selected", "callback", "snapshot", "lock-exit"])
        self.assertEqual(result["target_selection"], target.as_dict())

    def test_current_spec_freezes_identity_and_cutoff_and_legacy_specs_validate(self):
        current = read_json(PROJECT_ROOT / "PROJECT_SPEC_V4.json")
        self.assertEqual(
            validate_spec_v4(PROJECT_ROOT / "PROJECT_SPEC_V4.json")["schema_version"],
            "swivd-project-spec-v4.3",
        )
        self.assertEqual(current["manifest_version"], "swivd-local-snapshot-manifest-v4")
        self.assertEqual(current["audit_version"], "swivd-v2-audit-v2")
        self.assertEqual(current["ui_catalog_version"], "swivd-ui-catalog-v3")
        self.assertEqual(current["ui_industry_shard_version"], "swivd-industry-shard-v2")
        self.assertEqual(
            current["industry_identity"]["only_scoped_exception"]["industry_uid"],
            "SW2021:L3:230501",
        )
        mixed_current = json.loads(json.dumps(current))
        mixed_current["jobs"]["latest_fallback"] = "ALLOWED"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mixed-current.json"
            write_json(path, mixed_current)
            with self.assertRaisesRegex(ValueError, "must not contain the legacy"):
                validate_spec_v4(path)

        v4_1 = json.loads(json.dumps(current))
        v4_1.update(
            schema_version="swivd-project-spec-v4.1",
            contract_version="swivd-contract-v2.1.0",
            decision_id="GOV-20260901-003",
            manifest_version="swivd-local-snapshot-manifest-v2",
        )
        for key in (
            "audit_version",
            "ui_catalog_version",
            "ui_industry_shard_version",
            "industry_identity",
            "provider_execution",
        ):
            v4_1.pop(key)
        v4_1["jobs"].pop("progress_monotonicity")
        for key in (
            "pointer_scope",
            "pointer_record_fields",
            "historical_index_version",
            "historical_index_v1_policy",
            "publication_transaction",
        ):
            v4_1["output"].pop(key)
        legacy = json.loads(json.dumps(v4_1))
        legacy.update(
            schema_version="swivd-project-spec-v4",
            contract_version="swivd-contract-v2.0.0",
            decision_id="GOV-20260901-001",
        )
        legacy["jobs"].pop("update_target_policy")
        legacy["jobs"].pop("request_failure_fallback")
        legacy["jobs"]["latest_fallback"] = "FORBIDDEN"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy-spec.json"
            write_json(path, v4_1)
            self.assertEqual(validate_spec_v4(path)["schema_version"], "swivd-project-spec-v4.1")
            with self.assertRaisesRegex(ValueError, "new runs require the current spec successor"):
                validate_spec_v4(path, require_current=True)

            write_json(path, legacy)
            self.assertEqual(validate_spec_v4(path)["schema_version"], "swivd-project-spec-v4")
            with self.assertRaisesRegex(ValueError, "new runs require the current spec successor"):
                validate_spec_v4(path, require_current=True)
            mixed_legacy = json.loads(json.dumps(legacy))
            mixed_legacy["jobs"]["update_target_policy"] = current["jobs"]["update_target_policy"]
            mixed_legacy["jobs"]["request_failure_fallback"] = "FORBIDDEN"
            write_json(path, mixed_legacy)
            with self.assertRaisesRegex(ValueError, "must not contain current update-target"):
                validate_spec_v4(path)


class SecurityTests(unittest.TestCase):
    def test_http_log_never_interpolates_request_target(self):
        handler_type = _handler(mock.Mock())
        handler = object.__new__(handler_type)
        handler.client_address = ("127.0.0.1", 12345)
        handler.command = "GET"
        secret_target = (
            "GET /?tushare_" + "token=" + "SHOULD_NOT_BE_LOGGED HTTP/1.1"
        )
        with mock.patch("builtins.print") as output:
            handler.log_message(
                '"%s" %s',
                secret_target,
                "200",
            )
        rendered = " ".join(str(value) for value in output.call_args.args)
        self.assertEqual(rendered, "127.0.0.1 GET request completed")
        self.assertNotIn("token", rendered.lower())

    def test_progress_is_monotonic_and_unfinished_job_recovers_interrupted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = JobManager(data_dir=root, spec_path=Path(__file__))
            job_id = "abc-def"
            write_json(root / "jobs" / f"{job_id}.json", {"job_id": job_id, "state": "QUEUED", "percent": 0})
            manager._write_progress(job_id, "MEMBERSHIP", 1, 2, "x")
            first = manager.get(job_id)
            manager._write_progress(job_id, "PREFLIGHT", 0, 1, "older-phase")
            self.assertEqual(manager.get(job_id), first)
            manager._write_progress(job_id, "MEMBERSHIP", 0, 2, "older-completed")
            self.assertEqual(manager.get(job_id), first)
            manager._write_progress(job_id, "MEMBERSHIP", 1, 1, "older-total")
            self.assertEqual(manager.get(job_id), first)
            manager._write_progress(job_id, "MEMBERSHIP", 2, 3, "newer-units")
            advanced = manager.get(job_id)
            self.assertEqual(advanced["completed_units"], 2)
            self.assertEqual(advanced["total_units"], 3)
            self.assertGreaterEqual(advanced["percent"], first["percent"])
            manager._write_progress(job_id, "MEMBERSHIP", 2, 4, "lower-fraction")
            lower_fraction = manager.get(job_id)
            self.assertEqual(lower_fraction["percent"], advanced["percent"])
            manager._write_progress(job_id, "STOCK_VALUATION", 0, 1, "next-phase")
            next_phase = manager.get(job_id)
            self.assertEqual(next_phase["state"], "STOCK_VALUATION")
            self.assertEqual(next_phase["completed_units"], 0)
            self.assertEqual(next_phase["total_units"], 1)
            manager._write_progress(job_id, "MEMBERSHIP", 3, 4, "late-callback")
            self.assertEqual(manager.get(job_id), next_phase)
            recovered = JobManager(data_dir=root, spec_path=Path(__file__))
            self.assertEqual(recovered.get(job_id)["state"], "INTERRUPTED")
            interrupted = recovered.get(job_id)
            recovered._write_progress(job_id, "PUBLISH", 1, 1, "too-late")
            self.assertEqual(recovered.get(job_id), interrupted)

    def test_job_persists_target_reason_through_progress_and_reports_noop(self):
        class DummyLock:
            exited = False

            def __exit__(self, *_args):
                self.exited = True

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = JobManager(data_dir=root, spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json")
            job_id = "abc-123"
            write_json(
                root / "jobs" / f"{job_id}.json",
                {
                    "schema_version": "swivd-job-v2",
                    "job_id": job_id,
                    "kind": "UPDATE_LATEST",
                    "requested_as_of": None,
                    "as_of": None,
                    "target_selection": None,
                    "state": "QUEUED",
                    "completed_units": 0,
                    "total_units": 1,
                    "percent": 0,
                    "current_item": "",
                    "safe_message_code": "QUEUED",
                    "run_id": None,
                },
            )
            target = UpdateTarget(
                as_of="20260831",
                reason_code="PREVIOUS_OPEN_DAY_BEFORE_SW_DAILY_CUTOFF",
                evaluated_at="2026-09-01T14:55:00+08:00",
            )

            def fake_snapshot(**kwargs):
                kwargs["progress"]("INDUSTRY_DATA", 1, 2, "L1:801010.SI")
                return {
                    "run_id": "SWIVD2-RUN-20260831-001",
                    "as_of": "20260831",
                    "job_outcome": "ALREADY_UP_TO_DATE",
                }

            lock = DummyLock()
            with mock.patch("swivd.v2_jobs.resolve_update_target", return_value=target), mock.patch(
                "swivd.v2_jobs.run_snapshot", side_effect=fake_snapshot
            ):
                manager._run(job_id, lock)  # type: ignore[arg-type]
            record = manager.get(job_id)
            self.assertTrue(lock.exited)
            self.assertEqual(record["state"], "SUCCEEDED")
            self.assertEqual(record["safe_message_code"], "ALREADY_UP_TO_DATE")
            self.assertEqual(record["as_of"], "20260831")
            self.assertEqual(record["target_selection"], target.as_dict())
            self.assertEqual(record["percent"], 100)

    def test_job_rejects_inconsistent_pipeline_success_metadata(self):
        target = UpdateTarget(
            as_of="20260831",
            reason_code="PREVIOUS_OPEN_DAY_BEFORE_SW_DAILY_CUTOFF",
            evaluated_at="2026-09-01T14:55:00+08:00",
        )
        cases = (
            (
                "JOB_RESULT_AS_OF_MISMATCH",
                {"run_id": "SWIVD2-RUN-20260901-001", "as_of": "20260901"},
            ),
            (
                "JOB_RESULT_RUN_ID_MISMATCH",
                {"run_id": "SWIVD2-RUN-20260901-001", "as_of": "20260831"},
            ),
            (
                "JOB_RESULT_OUTCOME_INVALID",
                {
                    "run_id": "SWIVD2-RUN-20260831-001",
                    "as_of": "20260831",
                    "job_outcome": "UNRECOGNIZED_SUCCESS",
                },
            ),
        )

        class DummyLock:
            def __exit__(self, *_args):
                return None

        for expected_code, manifest in cases:
            with self.subTest(expected_code=expected_code), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                manager = JobManager(data_dir=root, spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json")
                job_id = "abc-123"
                write_json(
                    root / "jobs" / f"{job_id}.json",
                    {
                        "schema_version": "swivd-job-v2",
                        "job_id": job_id,
                        "kind": "UPDATE_LATEST",
                        "requested_as_of": None,
                        "as_of": None,
                        "target_selection": None,
                        "state": "QUEUED",
                        "completed_units": 0,
                        "total_units": 1,
                        "percent": 0,
                        "current_item": "",
                        "safe_message_code": "QUEUED",
                        "run_id": None,
                    },
                )
                with mock.patch("swivd.v2_jobs.resolve_update_target", return_value=target), mock.patch(
                    "swivd.v2_jobs.run_snapshot", return_value=manifest
                ):
                    manager._run(job_id, DummyLock())  # type: ignore[arg-type]
                record = manager.get(job_id)
                self.assertEqual(record["state"], "FAILED")
                self.assertEqual(record["safe_message_code"], expected_code)
                self.assertEqual(record["target_selection"], target.as_dict())

    def test_request_shape_and_empty_values_fail_closed(self):
        with self.assertRaises(TushareConfigurationError):
            SecureTushareClient._validate_request("sw_daily", {"trade_date": ""}, None)
        with self.assertRaises(TushareConfigurationError):
            SecureTushareClient._validate_request("index_member_all", {"l1_code": "x", "l2_code": "y", "is_new": "Y"}, None)
        with self.assertRaises(TushareConfigurationError):
            SecureTushareClient._validate_request("daily_basic", {"trade_date": "20211213", "ts_code": "x"}, None)

    def test_raw_replay_preserves_decimal_lexemes_without_binary_float(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sw_daily.json"
            fields = json.dumps(list(ENDPOINT_FIELDS["sw_daily"]), ensure_ascii=False)
            path.write_text(
                '{"code":0,"msg":null,"data":{"fields":'
                + fields
                + ',"items":[["850412.SI","20211213","特钢Ⅲ",100,99,101,100,0,0,1,1,10.00,1e-7,12345678901234567890.123,1]]}}',
                encoding="utf-8",
            )
            row = _decode_raw_rows(path, "sw_daily")[0]
            self.assertEqual(str(row["pe"]), "10.00")
            self.assertEqual(str(row["pb"]), "1E-7")
            self.assertEqual(str(row["total_mv"]), "12345678901234567890.123")

    def test_secure_client_rejects_credential_reflection_before_raw_can_be_saved(self):
        credential = "SYNTHETIC" + "CANARYVALUE123456789"

        class Response:
            status = 200
            headers = {}

            def __init__(self, raw: bytes):
                self.raw = raw

            def getcode(self):
                return self.status

            def geturl(self):
                return "https://api.tushare.pro"

            def read(self):
                return self.raw

            def close(self):
                return None

        payloads = (
            {
                "code": 0,
                "msg": credential,
                "data": {"fields": list(ENDPOINT_FIELDS["trade_cal"]), "items": []},
            },
            {
                "code": 0,
                "msg": "ok",
                "data": {
                    "fields": list(ENDPOINT_FIELDS["trade_cal"]),
                    "items": [[credential, "20211213", 1, "20211210"]],
                },
            },
        )
        for payload in payloads:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            client = SecureTushareClient(
                token=credential,
                opener=lambda _request, timeout, raw=raw: Response(raw),
                max_transient_retries=0,
            )
            with self.subTest(location=payload["msg"]), self.assertRaisesRegex(
                TushareProtocolError, "credential material"
            ):
                client.call("trade_cal", {"exchange": "SSE", "start_date": "20211213", "end_date": "20211213"})

    def test_lock_is_nonblocking_and_pointer_hash_is_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = SingleWriterLock(root)
            first.__enter__()
            try:
                with self.assertRaisesRegex(RuntimeError, "CONCURRENT_UPDATE"):
                    with SingleWriterLock(root):
                        pass
            finally:
                first.__exit__(None, None, None)
            run = root / "runs" / "SWIVD2-RUN-20211213-001"
            run.mkdir(parents=True)
            write_json(
                run / "manifest.json",
                {
                    "schema_version": "swivd-local-snapshot-manifest-v3",
                    "run_id": run.name,
                    "as_of": "20211213",
                    "purpose": "UPDATE_LATEST",
                },
            )
            write_json(
                root / "latest_run.json",
                {
                    "pointer_kind": "manifest",
                    "scope": "SW2021_L1_L2_L3_WITH_POINT_IN_TIME_MEMBERS",
                    "run_id": run.name,
                    "as_of": "20211213",
                    "target_path": f"runs/{run.name}/manifest.json",
                    "target_sha256": "0" * 64,
                },
            )
            with self.assertRaisesRegex(ValueError, "POINTER_HASH_MISMATCH"):
                resolve_pointer(root)

    def test_pointer_metadata_manifest_purpose_and_legacy_history_are_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def make_run(run_id: str, purpose: str) -> Path:
                run = root / "runs" / run_id
                run.mkdir(parents=True)
                write_json(
                    run / "manifest.json",
                    {
                        "schema_version": "swivd-local-snapshot-manifest-v3",
                        "run_id": run_id,
                        "as_of": run_id.split("-")[2],
                        "purpose": purpose,
                    },
                )
                return run

            current = make_run("SWIVD2-RUN-20211213-001", "UPDATE_LATEST")
            publish_current(
                root,
                run_id=current.name,
                as_of="20211213",
                manifest_sha256=sha256_file(current / "manifest.json"),
            )
            self.assertEqual(resolve_pointer(root)[0], current.resolve())
            pointer = read_json(root / "latest_run.json")
            for field, value, code in (
                ("run_id", "SWIVD2-RUN-20211214-999", "POINTER_AS_OF_MISMATCH"),
                ("as_of", "20211214", "POINTER_AS_OF_MISMATCH"),
                ("target_path", "runs/other/manifest.json", "POINTER_TARGET_PATH_MISMATCH"),
            ):
                forged = dict(pointer)
                forged[field] = value
                write_json(root / "latest_run.json", forged)
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, code):
                    resolve_pointer(root)
            write_json(root / "latest_run.json", pointer)

            historical = make_run("SWIVD2-RUN-20211214-001", "MATERIALIZE_DATE")
            legacy_record = {
                "run_id": historical.name,
                "target_path": f"runs/{historical.name}/manifest.json",
                "target_sha256": sha256_file(historical / "manifest.json"),
            }
            write_json(
                root / "historical_index.json",
                {
                    "schema_version": "swivd-historical-index-v1",
                    "dates": {"20211214": legacy_record},
                },
            )
            self.assertEqual(
                resolve_pointer(root, historical_date="20211214")[0], historical.resolve()
            )
            historical_two = make_run("SWIVD2-RUN-20211215-001", "MATERIALIZE_DATE")
            publish_historical(
                root,
                run_id=historical_two.name,
                as_of="20211215",
                manifest_sha256=sha256_file(historical_two / "manifest.json"),
            )
            upgraded = read_json(root / "historical_index.json")
            self.assertEqual(upgraded["schema_version"], "swivd-historical-index-v2")
            self.assertEqual(upgraded["dates"]["20211214"]["as_of"], "20211214")

    def test_http_origin_nonce_content_type_host_and_cors(self):
        with tempfile.TemporaryDirectory() as temporary:
            probe = socket.socket()
            try:
                probe.bind(("127.0.0.1", 0))
            except PermissionError:
                probe.close()
                self.skipTest("sandbox forbids loopback bind; run in host E2E")
            port = probe.getsockname()[1]
            probe.close()
            project = Path(__file__).resolve().parents[1]
            app = LocalApp(project_root=project, data_dir=Path(temporary), host="127.0.0.1", port=port)
            app.jobs.create = lambda **kwargs: {"job_id": "abc"}  # type: ignore[method-assign]
            server = LoopbackHTTPServer(("127.0.0.1", port), _handler(app))
            server.daemon_threads = True
            server.block_on_close = False
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = None
            try:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                def status() -> int:
                    response = connection.getresponse()
                    value = response.status
                    response.read()
                    return value
                connection.request("POST", "/api/v1/jobs", body="{}", headers={"Host": "evil.test", "Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json", "X-SWIVD-Nonce": app.nonce})
                self.assertEqual(status(), 421)
                connection.request("POST", "/api/v1/jobs", body="{}", headers={"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"})
                self.assertEqual(status(), 403)
                connection.request("POST", "/api/v1/jobs", body="{}", headers={"Origin": f"http://127.0.0.1:{port}", "Content-Type": "text/plain", "X-SWIVD-Nonce": app.nonce})
                self.assertEqual(status(), 415)
                connection.request("POST", "/api/v1/jobs", body=json.dumps({"kind": "UPDATE_LATEST"}), headers={"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json", "X-SWIVD-Nonce": app.nonce})
                self.assertEqual(status(), 202)
                connection.request("OPTIONS", "/api/v1/jobs")
                response = connection.getresponse()
                self.assertEqual(response.status, 405)
                self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
            finally:
                if connection is not None:
                    connection.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_http_shell_and_all_assets_have_safe_headers_and_mime_types(self):
        expected_types = {
            "/": {"text/html"},
            "/app.css": {"text/css"},
            "/history.css": {"text/css"},
            "/app.js": {"text/javascript", "application/javascript"},
            "/bootstrap.js": {"text/javascript", "application/javascript"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            with running_local_app(self, Path(temporary)) as (_, port):
                for route, allowed_types in expected_types.items():
                    with self.subTest(route=route):
                        status, headers, body = local_request(port, "GET", route)
                        self.assertEqual(status, 200)
                        media_type = headers.get("content-type", "").split(";", 1)[0]
                        self.assertIn(media_type, allowed_types)
                        self.assertGreater(len(body), 20)
                        self.assertEqual(headers.get("cache-control"), "no-store")
                        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
                        self.assertNotIn("access-control-allow-origin", headers)
                        csp = headers.get("content-security-policy", "")
                        for directive in (
                            "default-src 'self'",
                            "script-src 'self'",
                            "style-src 'self'",
                            "connect-src 'self'",
                            "frame-ancestors 'none'",
                        ):
                            self.assertIn(directive, csp)
                        self.assertNotIn("unsafe-inline", csp)
                _, _, bootstrap = local_request(port, "GET", "/bootstrap.js")
                self.assertIn(b"window.__SWIVD__=", bootstrap)
                self.assertNotIn(b"TUSHARE_TOKEN", bootstrap)

    def test_non_loopback_bind_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "NON_LOOPBACK_BIND_FORBIDDEN"):
                LocalApp(project_root=Path(__file__).resolve().parents[1], data_dir=Path(temporary), host="0.0.0.0", port=8765)


class WebShellContractTests(unittest.TestCase):
    def setUp(self):
        self.html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.styles = (PROJECT_ROOT / "web" / "app.css").read_text(encoding="utf-8")
        self.script = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")

    def test_assets_are_relative_and_file_open_fails_closed_with_service_guard(self):
        for asset in ("app.css", "history.css"):
            self.assertRegex(self.html, rf'<link[^>]+href=["\']\./{re.escape(asset)}["\']')
            self.assertNotIn(f'href="/{asset}"', self.html)
        for asset in ("bootstrap.js", "app.js"):
            self.assertRegex(self.html, rf'<script[^>]+src=["\']\./{re.escape(asset)}["\']')
            self.assertNotIn(f'src="/{asset}"', self.html)

        guard = re.search(r'<[^>]+id=["\']serviceGuard["\'][^>]*>', self.html)
        shell = re.search(r'<[^>]+id=["\']appShell["\'][^>]*>', self.html)
        self.assertIsNotNone(guard)
        self.assertIsNotNone(shell)
        self.assertNotRegex(guard.group(0), r"\bhidden\b")
        self.assertRegex(shell.group(0), r"\bhidden\b")
        self.assertIn("请通过本地服务打开仪表盘", self.html)
        self.assertIn("window.__SWIVD__", self.script)
        self.assertIn("serviceGuard", self.script)
        self.assertIn("appShell", self.script)

        for element_id in ("job", "ready", "constituentDetail"):
            tag = re.search(rf'<[^>]+id=["\']{element_id}["\'][^>]*>', self.html)
            self.assertIsNotNone(tag, element_id)
            self.assertRegex(tag.group(0), r"\bhidden\b", element_id)

    def test_original_six_views_and_v2_controls_are_present(self):
        required_tabs = {
            "tab-overview": "总览",
            "tab-heatmap": "估值热力图",
            "tab-trend": "历史走势",
            "tab-ranking": "涨跌排行",
            "tab-scatter": "PE-PB 象限",
            "tab-detail": "数据明细",
        }
        for tab_id, label in required_tabs.items():
            self.assertIn(f'id="{tab_id}"', self.html)
            self.assertIn(label, self.html)
        self.assertGreaterEqual(len(re.findall(r'role=["\']tab["\']', self.html)), 6)
        for level in ("L1", "L2", "L3"):
            self.assertRegex(self.html, rf'data-level=["\']{level}["\']')
        for element_id in ("update", "materialize", "stocks"):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn("目标日成分股", self.html)

    def test_web_assets_have_no_remote_dependencies(self):
        remote = re.compile(
            r'https?://(?!(?:(?:127\.0\.0\.1|localhost)(?::|/)|www\.w3\.org/2000/svg(?:["\'/]|$)))'
            r'|(?<!:)//(?:cdn\.|unpkg\.|cdnjs\.)',
            re.I,
        )
        for path in sorted((PROJECT_ROOT / "web").rglob("*")):
            if path.is_file():
                with self.subTest(path=path.name):
                    self.assertNotRegex(path.read_text(encoding="utf-8"), remote)

    def test_original_zip_dark_palette_is_preserved(self):
        for color in ("#0d1117", "#161b22", "#30363d", "#58a6ff", "#f85149", "#3fb950"):
            self.assertIn(color, self.styles.lower())
        self.assertIn("color-scheme: dark", self.styles.lower())
        self.assertRegex(self.styles, r"\.app-header\s*\{[^}]*position:\s*sticky")

    def test_dynamic_names_are_not_bare_html_interpolations(self):
        self.assertRegex(self.script, r"(?:const|function)\s+(?:esc|escapeHtml)\b")
        self.assertIn(".textContent", self.script)
        unsafe_html = re.compile(
            r"(?:innerHTML\s*=|insertAdjacentHTML\s*\()[^;]{0,8000}"
            r"\$\{\s*(?:row|x|item|industry)\."
            r"(?:industry_name|stock_name|name)\s*\}",
            re.S,
        )
        self.assertNotRegex(self.script, unsafe_html)

    def test_ui_contract_uses_frozen_fields_and_closes_failure_states(self):
        self.assertIn("industry.pe_percentile_le", self.script)
        self.assertIn("industry.pb_percentile_le", self.script)
        for browser_recomputed_label in (
            "历史极低位", "历史较低位", "历史中位", "历史较高位", "历史极高位",
        ):
            self.assertNotIn(browser_recomputed_label, self.script)
        self.assertIn("const shown = rows;", self.script)
        self.assertIn("overview-bars", self.styles)
        self.assertIn('if (!$("panel-trend").hidden)', self.script)
        self.assertIn("未显示上一行业统计", self.script)
        self.assertIn("仍停留在旧快照", self.script)
        self.assertIn("fillIndustrySelects();", self.script)
        self.assertIn("current_index_code", self.script)
        self.assertIn("UNKNOWN_UPSTREAM_INTERNAL_CAUSE", self.script)
        self.assertIn("上游内部原因未知；本地仅做证据门连接", self.script)

    def test_update_target_reason_is_allowlisted_and_visible(self):
        for marker in (
            "PREVIOUS_OPEN_DAY_BEFORE_SW_DAILY_CUTOFF",
            "CURRENT_OPEN_DAY_AT_OR_AFTER_SW_DAILY_CUTOFF",
            "LATEST_OPEN_DAY_NON_TRADING_DATE",
            "ALREADY_UP_TO_DATE",
            "Asia/Shanghai",
            "18:30:00",
            "target_selection",
            "SNAPSHOT_AS_OF_MISMATCH",
        ):
            self.assertIn(marker, self.script)
        self.assertIn("北京时间尚未到 18:30，按规则选择前一交易日", self.script)
        self.assertIn("loadCatalog(readyRun, readyAsOf)", self.script)
        self.assertIn("Object.prototype.hasOwnProperty.call", self.script)
        self.assertIn('String(selection.as_of || "") !== String(job.as_of || "")', self.script)
        self.assertIn("runId === job.run_id", self.script)
        self.assertIn("最新快照 ${job.run_id} 已存在", self.script)
        self.assertNotRegex(self.script, r"target_selection\.(?:message|error|token)")


class FakeClient:
    def __init__(self):
        self.classes = classifications()
        self.quote_names = {
            row["index_code"]: row["industry_name"]
            for rows in self.classes.values()
            for row in rows
            if row["is_pub"] == 1
            and not (
                row["level"] == "L3" and row["industry_code"] == "230501"
            )
        }
        self.quote_names["850412.SI"] = "特钢Ⅲ"
        self.members = [
            member(f"00000{i}.SZ", special_steel_endpoint=True)
            for i in range(1, 6)
        ]
        self.dates = ["20211213", "20211214"]
        self.bad_daily_basic = False
        self.empty_industry_day = False

    def call(self, api_name, params, fields):
        if api_name == "trade_cal":
            rows = [
                {"exchange": "SSE", "cal_date": date, "is_open": 1, "pretrade_date": "20211210" if date == "20211213" else "20211213"}
                for date in self.dates if params["start_date"] <= date <= params["end_date"]
            ]
        elif api_name == "index_classify":
            rows = self.classes[params["level"]]
        elif api_name == "sw_daily":
            if "ts_code" in params:
                requested = params["ts_code"]
                codes = [requested] if requested in self.quote_names else []
            else:
                codes = sorted(self.quote_names)
            dates = [params["trade_date"]] if "trade_date" in params else [date for date in self.dates if params["start_date"] <= date <= params["end_date"]]
            rows = [
                {"ts_code": code, "trade_date": date, "name": self.quote_names[code], "open": 100, "low": 99, "high": 101, "close": 100, "change": 0, "pct_change": 0, "vol": 1, "amount": 1, "pe": 10, "pb": 1, "total_mv": 1, "float_mv": 1}
                for code in codes for date in dates
            ]
            if self.empty_industry_day and "trade_date" in params and "ts_code" not in params:
                rows = []
        elif api_name == "index_member_all":
            selector = next(key for key in ("l1_code", "l2_code", "l3_code", "ts_code") if key in params)
            rows = [row for row in self.members if row[selector] == params[selector]] if params["is_new"] == "Y" else []
        elif api_name == "daily_basic":
            source = self.members if not self.bad_daily_basic else [
                member(f"{i:06d}.SZ", special_steel_endpoint=True)
                for i in range(6000)
            ]
            rows = [{"ts_code": row["ts_code"], "trade_date": params["trade_date"], "close": 10, "pe": 12, "pe_ttm": 10 + index, "pb": 1 + index / 10, "ps_ttm": 2, "dv_ttm": 1, "total_mv": 100, "circ_mv": 80} for index, row in enumerate(source)]
        else:
            raise AssertionError(api_name)
        ordered = list(ENDPOINT_FIELDS[api_name])
        payload = {"code": 0, "msg": None, "data": {"fields": ordered, "items": [[row.get(field) for field in ordered] for row in rows]}}
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        return RawApiResponse(payload, raw_bytes=raw, http_status=200, headers={}, api_name=api_name, attempt_count=1)


def reclose_test_run(run: Path) -> None:
    """Recompute every mutable hash after an adversarial test rewrite."""

    manifest = read_json(run / "manifest.json")
    manifest["artifacts"] = inventory_artifacts(run)
    write_json(run / "manifest.json", manifest)
    write_sums(run)


class PipelineV2Tests(unittest.TestCase):
    def test_injected_client_is_rejected_outside_system_temp_before_any_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowed_temp = root / "allowed-system-temp"
            allowed_temp.mkdir()
            forbidden = root / "outside-system-temp" / "data"
            with mock.patch(
                "swivd.v2_pipeline.tempfile.gettempdir",
                return_value=str(allowed_temp),
            ):
                with self.assertRaisesRegex(
                    V2PipelineError, "INJECTED_CLIENT_DATA_DIR_FORBIDDEN"
                ):
                    run_snapshot(
                        spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json",
                        data_dir=forbidden,
                        as_of="20211213",
                        purpose="UPDATE_LATEST",
                        client=FakeClient(),
                    )
            self.assertFalse(forbidden.exists())

    def test_validator_replays_row_limit_split_tree(self):
        class SplitClient(FakeClient):
            def __init__(self):
                super().__init__()
                next(
                    row
                    for row in self.classes["L2"]
                    if row["index_code"] == "820001.SI"
                )["parent_code"] = "230000"
                self.split_members = []
                for index in range(1995):
                    row = member(f"{index + 1000:06d}.SZ")
                    row["l1_code"] = "801040.SI"
                    row["l1_name"] = "钢铁"
                    self.split_members.append(row)

            def call(self, api_name, params, fields):
                if (
                    api_name == "index_member_all"
                    and params.get("l1_code") == "801040.SI"
                    and params.get("is_new") == "Y"
                ):
                    rows = [*self.members, *self.split_members]
                elif (
                    api_name == "index_member_all"
                    and params.get("l2_code") == "820001.SI"
                    and params.get("is_new") == "Y"
                ):
                    rows = self.split_members
                else:
                    return super().call(api_name, params, fields)
                ordered = list(ENDPOINT_FIELDS[api_name])
                payload = {
                    "code": 0,
                    "msg": None,
                    "data": {
                        "fields": ordered,
                        "items": [
                            [item.get(field) for field in ordered]
                            for item in rows
                        ],
                    },
                }
                raw = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
                return RawApiResponse(
                    payload,
                    raw_bytes=raw,
                    http_status=200,
                    headers={},
                    api_name=api_name,
                    attempt_count=1,
                )

        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            manifest = run_snapshot(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json",
                data_dir=data,
                as_of="20211213",
                purpose="UPDATE_LATEST",
                client=SplitClient(),
            )
            run = data / "runs" / manifest["run_id"]
            validated = validate_run_v2(run)
            self.assertEqual(validated["run_id"], manifest["run_id"])
            requests = read_json(run / "audit.json")["requests"]
            self.assertTrue(
                any(
                    row["params"] == {"l1_code": "801040.SI", "is_new": "Y"}
                    and row["row_count"] == 2000
                    for row in requests
                )
            )
            self.assertTrue(
                any(
                    row["params"] == {"l2_code": "801045.SI", "is_new": "Y"}
                    and row["row_count"] == 5
                    for row in requests
                )
            )
            self.assertTrue(
                any(
                    row["params"] == {"l2_code": "820001.SI", "is_new": "Y"}
                    and row["row_count"] == 1995
                    for row in requests
                )
            )

    def test_membership_response_must_match_its_query_selector_and_state(self):
        class MisroutedClient(FakeClient):
            def call(self, api_name, params, fields):
                if (
                    api_name == "index_member_all"
                    and params.get("l1_code") == "810002.SI"
                    and params.get("is_new") == "Y"
                ):
                    row = member("009999.SZ")
                    ordered = list(ENDPOINT_FIELDS[api_name])
                    payload = {
                        "code": 0,
                        "msg": None,
                        "data": {
                            "fields": ordered,
                            "items": [[row.get(field) for field in ordered]],
                        },
                    }
                    raw = json.dumps(payload, ensure_ascii=False).encode()
                    return RawApiResponse(
                        payload,
                        raw_bytes=raw,
                        http_status=200,
                        headers={},
                        api_name=api_name,
                        attempt_count=1,
                    )
                return super().call(api_name, params, fields)

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(V2DataError, "MEMBER_QUERY_SCOPE_MISMATCH"):
                run_snapshot(
                    spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json",
                    data_dir=Path(temporary) / "data",
                    as_of="20211213",
                    purpose="UPDATE_LATEST",
                    client=MisroutedClient(),
                )

    def test_membership_raw_response_rejects_exact_and_conflicting_primary_keys(self):
        class DuplicateMembershipClient(FakeClient):
            def __init__(self, *, conflicting_name):
                super().__init__()
                self.conflicting_name = conflicting_name

            def call(self, api_name, params, fields):
                response = super().call(api_name, params, fields)
                if not (
                    api_name == "index_member_all"
                    and params.get("l1_code") == "801040.SI"
                    and params.get("is_new") == "Y"
                ):
                    return response
                payload = json.loads(response.raw_bytes.decode("utf-8"))
                duplicate = list(payload["data"]["items"][0])
                if self.conflicting_name:
                    duplicate[list(ENDPOINT_FIELDS[api_name]).index("name")] = "冲突名称"
                payload["data"]["items"].append(duplicate)
                raw = json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":")
                ).encode()
                return RawApiResponse(
                    payload,
                    raw_bytes=raw,
                    http_status=200,
                    headers={},
                    api_name=api_name,
                    attempt_count=1,
                )

        for conflicting_name in (False, True):
            with self.subTest(conflicting_name=conflicting_name):
                with tempfile.TemporaryDirectory() as temporary:
                    with self.assertRaisesRegex(
                        V2DataError, "MEMBERSHIP_PRIMARY_KEY_DUPLICATE"
                    ):
                        run_snapshot(
                            spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json",
                            data_dir=Path(temporary) / "data",
                            as_of="20211213",
                            purpose="UPDATE_LATEST",
                            client=DuplicateMembershipClient(
                                conflicting_name=conflicting_name
                            ),
                        )

    def test_materialize_older_date_filters_parent_before_identity_projection(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            client = FakeClient()
            current = run_snapshot(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json",
                data_dir=data,
                as_of="20211214",
                purpose="UPDATE_LATEST",
                client=client,
            )
            current_pointer = (data / "latest_run.json").read_bytes()
            historical = run_snapshot(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json",
                data_dir=data,
                as_of="20211213",
                purpose="MATERIALIZE_DATE",
                client=client,
            )
            self.assertEqual((data / "latest_run.json").read_bytes(), current_pointer)
            self.assertEqual(historical["as_of"], "20211213")
            run = data / "runs" / historical["run_id"]
            validate_run_v2(run)
            special_history = [
                row
                for row in read_csv(
                    run
                    / "inputs"
                    / "normalized"
                    / "sw_daily_sw2021_l3.csv"
                )
                if row["industry_uid"] == "SW2021:L3:230501"
            ]
            self.assertEqual(
                [row["trade_date"] for row in special_history], ["20211213"]
            )

    def test_selected_target_empty_data_fails_without_further_date_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            client = FakeClient()
            client.empty_industry_day = True
            with self.assertRaisesRegex(
                IdentityResolutionError, "IDENTITY_TARGET_DAY_SINGLETON_REQUIRED"
            ):
                run_snapshot(
                    spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json",
                    data_dir=data,
                    as_of="20211214",
                    purpose="UPDATE_LATEST",
                    client=client,
                )
            self.assertFalse((data / "latest_run.json").exists())
            failures = sorted((data / "runs").glob("*/failure.json"))
            self.assertEqual(len(failures), 1)
            self.assertEqual(read_json(failures[0])["as_of"], "20211214")

    def test_update_equal_current_is_noop_and_older_target_is_rejected_before_network(self):
        class NoNetworkClient:
            def call(self, *_args, **_kwargs):
                raise AssertionError("network must not be reached")

        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            run = data / "runs" / "SWIVD2-RUN-20211214-001"
            run.mkdir(parents=True)
            current_manifest = {
                "run_id": run.name,
                "as_of": "20211214",
                "schema_version": "swivd-local-snapshot-manifest-v2",
                "purpose": "UPDATE_LATEST",
            }
            write_json(run / "manifest.json", current_manifest)
            write_json(
                data / "latest_run.json",
                {
                    "pointer_kind": "manifest",
                    "scope": POINTER_SCOPE,
                    "run_id": run.name,
                    "as_of": "20211214",
                    "target_path": f"runs/{run.name}/manifest.json",
                    "target_sha256": sha256_file(run / "manifest.json"),
                },
            )
            with mock.patch(
                "swivd.v2_validator.validate_run_v2",
                return_value=current_manifest,
            ):
                result = run_snapshot(
                    spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json",
                    data_dir=data,
                    as_of="20211214",
                    purpose="UPDATE_LATEST",
                    client=NoNetworkClient(),
                )
                self.assertEqual(result["job_outcome"], "ALREADY_UP_TO_DATE")
                self.assertEqual(sorted((data / "runs").iterdir()), [run])
                with self.assertRaisesRegex(V2PipelineError, "TARGET_BEFORE_CURRENT"):
                    run_snapshot(
                        spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json",
                        data_dir=data,
                        as_of="20211213",
                        purpose="UPDATE_LATEST",
                        client=NoNetworkClient(),
                    )
                self.assertEqual(sorted((data / "runs").iterdir()), [run])

    def test_validator_rejects_reclosed_normalized_identity_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            manifest = run_snapshot(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json",
                data_dir=data,
                as_of="20211213",
                purpose="UPDATE_LATEST",
                client=FakeClient(),
            )
            run = data / "runs" / manifest["run_id"]
            evidence_path = (
                run / "inputs" / "normalized" / "industry_identity_resolution.json"
            )
            forged = read_json(evidence_path)
            forged["catalog_index_code"] = "850412.SI"
            forged["identity_state"] = "DIRECT"
            forged["identity_rule"] = "CATALOG_EQUALS_SINGLE_CONTINUOUS_QUOTE_CODE"
            write_json(evidence_path, forged)

            evidence_sha = sha256_file(evidence_path)
            audit = read_json(run / "audit.json")
            audit["identity_resolution"] = forged
            audit["identity_evidence"]["sha256"] = evidence_sha
            write_json(run / "audit.json", audit)
            forged_manifest = read_json(run / "manifest.json")
            forged_manifest["identity_resolution"].update(
                state="DIRECT",
                catalog_index_code="850412.SI",
                evidence_sha256=evidence_sha,
            )
            write_json(run / "manifest.json", forged_manifest)
            reclose_test_run(run)

            with self.assertRaisesRegex(
                ValueError, "recomputed identity evidence differs from the frozen contract"
            ):
                validate_run_v2(run)
            rebuilt = Path(temporary) / "forged-rebuild"
            with self.assertRaisesRegex(
                ValueError, "recomputed identity evidence differs from the frozen contract"
            ):
                rebuild_v2(run_dir=run, output_dir=rebuilt)
            self.assertFalse(rebuilt.exists())

    def test_success_ledger_post_fsync_error_keeps_committed_latest_without_failed_terminal(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            previous = run_snapshot(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json",
                data_dir=data,
                as_of="20211213",
                purpose="UPDATE_LATEST",
                client=FakeClient(),
            )
            before = (data / "latest_run.json").read_bytes()
            real_append = __import__(
                "swivd.v2_storage", fromlist=["append_ledger"]
            ).append_ledger

            def append_success_then_raise(data_dir, record):
                if record.get("event") == "RUN_SUCCEEDED":
                    real_append(data_dir, record)
                    raise OSError("simulated post-fsync caller failure")
                return real_append(data_dir, record)

            with mock.patch(
                "swivd.v2_storage.append_ledger",
                side_effect=append_success_then_raise,
            ):
                current = run_snapshot(
                    spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json",
                    data_dir=data,
                    as_of="20211214",
                    purpose="UPDATE_LATEST",
                    client=FakeClient(),
                )
            self.assertNotEqual((data / "latest_run.json").read_bytes(), before)
            self.assertNotEqual(current["run_id"], previous["run_id"])
            self.assertEqual(
                read_json(data / "latest_run.json")["run_id"], current["run_id"]
            )
            ledger = [
                json.loads(line)
                for line in (data / "run_ledger.ndjson").read_text(encoding="utf-8").splitlines()
            ]
            current_terminals = [
                row
                for row in ledger
                if row.get("run_id") == current["run_id"]
                and row.get("event") in {
                    "RUN_SUCCEEDED", "RUN_FAILED", "RUN_INTERRUPTED"
                }
            ]
            self.assertEqual(
                [row["event"] for row in current_terminals], ["RUN_SUCCEEDED"]
            )

    def test_validator_rejects_reclosed_raw_identity_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            manifest = run_snapshot(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json",
                data_dir=data,
                as_of="20211213",
                purpose="UPDATE_LATEST",
                client=FakeClient(),
            )
            run = data / "runs" / manifest["run_id"]
            relative = (
                "inputs/raw/identity_resolution/sw_daily/"
                "850412_SI_20211213_20211213.json"
            )
            raw_path = run / relative
            forged_raw = read_json(raw_path)
            self.assertEqual(forged_raw["data"]["items"][0][0], "850412.SI")
            forged_raw["data"]["items"][0][0] = "850401.SI"
            write_json(raw_path, forged_raw)

            audit = read_json(run / "audit.json")
            request = next(
                row for row in audit["requests"] if row["raw_path"] == relative
            )
            request["raw_sha256"] = sha256_file(raw_path)
            write_json(run / "audit.json", audit)
            reclose_test_run(run)

            with self.assertRaisesRegex(
                ValueError, "response rows differ from their request selector"
            ):
                validate_run_v2(run)

    def test_validator_rejects_resealed_raw_audit_source_and_ui_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            manifest = run_snapshot(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json",
                data_dir=data,
                as_of="20211213",
                purpose="UPDATE_LATEST",
                client=FakeClient(),
            )
            run = data / "runs" / manifest["run_id"]

            catalog = read_json(run / "ui" / "catalog.json")
            shard_path = run / "ui" / catalog["industries"][0]["shard"]
            shard_bytes = shard_path.read_bytes()
            shard = read_json(shard_path)
            self.assertTrue(shard["history"])
            shard["history"][0]["pe"] = "777"
            write_json(shard_path, shard)
            reclose_test_run(run)
            with self.assertRaisesRegex(ValueError, "derived ui differs from replay"):
                validate_run_v2(run)
            shard_path.write_bytes(shard_bytes)
            reclose_test_run(run)

            raw_path = next(
                iter(sorted((run / "inputs" / "raw" / "sw_daily" / "L1").glob("*.json")))
            )
            raw_bytes = raw_path.read_bytes()
            audit_path = run / "audit.json"
            audit_bytes = audit_path.read_bytes()
            raw = read_json(raw_path)
            open_position = raw["data"]["fields"].index("open")
            raw["data"]["items"][0][open_position] = "NOT_A_NUMBER"
            write_json(raw_path, raw)
            audit = read_json(audit_path)
            relative = raw_path.relative_to(run).as_posix()
            next(row for row in audit["requests"] if row["raw_path"] == relative)[
                "raw_sha256"
            ] = sha256_file(raw_path)
            write_json(audit_path, audit)
            reclose_test_run(run)
            with self.assertRaisesRegex(
                ValueError, "raw ordinary sw_daily replay failed"
            ):
                validate_run_v2(run)
            raw_path.write_bytes(raw_bytes)
            audit_path.write_bytes(audit_bytes)
            reclose_test_run(run)

            ordinary_paths = sorted(
                (run / "inputs" / "raw" / "sw_daily" / "L1").glob("*.json")
            )[:2]
            first_bytes, second_bytes = (
                ordinary_paths[0].read_bytes(),
                ordinary_paths[1].read_bytes(),
            )
            ordinary_paths[0].write_bytes(second_bytes)
            ordinary_paths[1].write_bytes(first_bytes)
            audit = read_json(audit_path)
            for path in ordinary_paths:
                relative = path.relative_to(run).as_posix()
                next(
                    row for row in audit["requests"] if row["raw_path"] == relative
                )["raw_sha256"] = sha256_file(path)
            write_json(audit_path, audit)
            reclose_test_run(run)
            with self.assertRaisesRegex(
                ValueError, "response rows differ from their request selector"
            ):
                validate_run_v2(run)
            ordinary_paths[0].write_bytes(first_bytes)
            ordinary_paths[1].write_bytes(second_bytes)
            audit_path.write_bytes(audit_bytes)
            reclose_test_run(run)

            audit = read_json(audit_path)
            audit["requests"][0]["api_name"] = "daily_basic"
            audit["requests"][0]["attempt_count"] = 999
            write_json(audit_path, audit)
            reclose_test_run(run)
            with self.assertRaisesRegex(ValueError, "request api name"):
                validate_run_v2(run)
            audit_path.write_bytes(audit_bytes)
            reclose_test_run(run)

            source_path = run / "source" / "src" / "swivd" / "v2_identity.py"
            source_bytes = source_path.read_bytes()
            source_path.unlink()
            reclose_test_run(run)
            with self.assertRaisesRegex(
                ValueError, "source file inventory mismatch|source_files does not exactly cover"
            ):
                validate_run_v2(run)
            source_path.write_bytes(source_bytes)
            reclose_test_run(run)

            frozen_contract = run / "source" / "PROJECT_CONTRACT_V2.md"
            contract_bytes = frozen_contract.read_bytes()
            frozen_contract.write_text(
                "FORGED CONTRACT\nproduction_approved=true\n", encoding="utf-8"
            )
            forged_manifest = read_json(run / "manifest.json")
            forged_manifest["contract_sha256"] = sha256_file(frozen_contract)
            contract_record = next(
                row
                for row in forged_manifest["source_files"]
                if row["path"] == "PROJECT_CONTRACT_V2.md"
            )
            contract_record.update(
                bytes=frozen_contract.stat().st_size,
                sha256=sha256_file(frozen_contract),
            )
            write_json(run / "manifest.json", forged_manifest)
            reclose_test_run(run)
            with self.assertRaisesRegex(ValueError, "differs from the trusted project source"):
                validate_run_v2(run)
            frozen_contract.write_bytes(contract_bytes)
            restored_manifest = read_json(run / "manifest.json")
            restored_manifest["contract_sha256"] = sha256_file(frozen_contract)
            contract_record = next(
                row
                for row in restored_manifest["source_files"]
                if row["path"] == "PROJECT_CONTRACT_V2.md"
            )
            contract_record.update(
                bytes=frozen_contract.stat().st_size,
                sha256=sha256_file(frozen_contract),
            )
            write_json(run / "manifest.json", restored_manifest)
            reclose_test_run(run)

            offline_path = run / "offline_validation.json"
            offline_bytes = offline_path.read_bytes()
            offline = read_json(offline_path)
            offline["status"] = "FAIL"
            offline["entrypoint"] = "forged.entrypoint"
            write_json(offline_path, offline)
            reclose_test_run(run)
            with self.assertRaisesRegex(ValueError, "offline validation receipt"):
                validate_run_v2(run)
            offline_path.write_bytes(offline_bytes)
            reclose_test_run(run)

            report_path = run / "reports" / "adversarial_review.md"
            report_bytes = report_path.read_bytes()
            report_path.write_text("production_approved=true\n", encoding="utf-8")
            reclose_test_run(run)
            with self.assertRaisesRegex(ValueError, "adversarial report differs"):
                validate_run_v2(run)
            report_path.write_bytes(report_bytes)
            reclose_test_run(run)

            legacy_dashboard = (
                run
                / "legacy"
                / "SWIVD-RUN-20260828-004"
                / "dashboard.html"
            )
            legacy_bytes = legacy_dashboard.read_bytes()
            legacy_dashboard.write_bytes(legacy_bytes + b"\n<!-- tamper -->\n")
            reclose_test_run(run)
            with self.assertRaisesRegex(ValueError, "legacy SW2014 artifact identity mismatch"):
                validate_run_v2(run)
            legacy_dashboard.write_bytes(legacy_bytes)
            reclose_test_run(run)
            validate_run_v2(run)

    def test_validator_binds_increment_lineage_raw_to_parent_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            client = FakeClient()
            parent = run_snapshot(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json",
                data_dir=data,
                as_of="20211213",
                purpose="UPDATE_LATEST",
                client=client,
            )
            child = run_snapshot(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json",
                data_dir=data,
                as_of="20211214",
                purpose="UPDATE_LATEST",
                client=client,
            )
            child_run = data / "runs" / child["run_id"]
            lineage_root = child_run / "lineage" / parent["run_id"]
            raw_path = next(
                iter(
                    sorted(
                        (lineage_root / "inputs" / "raw" / "sw_daily" / "L1").glob("*.json")
                    )
                )
            )
            raw = read_json(raw_path)
            pe_position = raw["data"]["fields"].index("pe")
            raw["data"]["items"][0][pe_position] = 777
            write_json(raw_path, raw)
            reclose_test_run(child_run)
            with self.assertRaisesRegex(ValueError, "lineage artifact identity mismatch"):
                validate_run_v2(child_run)

    def test_fake_snapshot_validates_rebuilds_and_keeps_legacy_immutable(self):
        project = Path(__file__).resolve().parents[1]
        legacy_manifest = project / "output" / "runs" / "SWIVD-RUN-20260828-004" / "manifest.json"
        before = legacy_manifest.read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "数据 目录"
            client = FakeClient()
            manifest = run_snapshot(
                spec_path=project / "PROJECT_SPEC_V4.json",
                data_dir=data,
                as_of="20211213",
                purpose="UPDATE_LATEST",
                client=client,
            )
            run = data / "runs" / manifest["run_id"]
            validated = validate_run_v2(run)
            self.assertEqual(validated["research_grade"], "RESEARCH_ONLY")
            self.assertEqual(validated["provider_kind"], "TEST_INJECTED_CLIENT")
            self.assertEqual(
                validated["artifact_publish_state"],
                "LOCAL_TEST_PROVIDER_COMPLETE",
            )
            self.assertEqual(
                validated["live_validation_state"], "NOT_LIVE_TEST_PROVIDER"
            )
            self.assertEqual(
                validated["live_validation_reason"], "EXPLICIT_CLIENT_INJECTION"
            )
            self.assertEqual(
                validated["schema_version"], "swivd-local-snapshot-manifest-v4"
            )
            self.assertEqual(
                validated["identity_resolution"],
                {
                    "rule_version": "swivd-special-steel-identity-v1",
                    "industry_uid": "SW2021:L3:230501",
                    "state": "EVIDENCE_GATED_ALIAS",
                    "current_index_code": "850412.SI",
                    "catalog_index_code": "850401.SI",
                    "quote_index_code": "850412.SI",
                    "member_index_code": "850412.SI",
                    "evidence_path": "inputs/normalized/industry_identity_resolution.json",
                    "evidence_sha256": sha256_file(
                        run / "inputs" / "normalized" / "industry_identity_resolution.json"
                    ),
                },
            )
            self.assertEqual(read_json(data / "latest_run.json")["run_id"], manifest["run_id"])
            current_pointer = (data / "latest_run.json").read_bytes()
            catalog = read_json(run / "ui" / "catalog.json")
            self.assertEqual(catalog["schema_version"], "swivd-ui-catalog-v3")
            self.assertEqual(len(catalog["industries"]), 511)
            summary_fields = {
                "pe", "pb", "pe_percentile", "pb_percentile",
                "return_5d", "return_mtd", "return_ytd",
                "valuation_state", "return_state",
            }
            identity_fields = {
                "industry_uid",
                "catalog_index_code",
                "quote_index_code",
                "member_index_code",
                "current_index_code",
                "identity_state",
                "identity_rule",
                "identity_rule_version",
                "identity_reason_disclosure",
            }
            for industry in catalog["industries"]:
                self.assertTrue(summary_fields.issubset(industry), industry["index_code"])
                self.assertTrue(identity_fields.issubset(industry), industry["index_code"])
            special = next(
                row
                for row in catalog["industries"]
                if row["industry_uid"] == "SW2021:L3:230501"
            )
            self.assertEqual(
                {
                    "index_code": special["index_code"],
                    "catalog_index_code": special["catalog_index_code"],
                    "quote_index_code": special["quote_index_code"],
                    "member_index_code": special["member_index_code"],
                    "current_index_code": special["current_index_code"],
                    "identity_state": special["identity_state"],
                    "identity_reason_disclosure": special["identity_reason_disclosure"],
                },
                {
                    "index_code": "850412.SI",
                    "catalog_index_code": "850401.SI",
                    "quote_index_code": "850412.SI",
                    "member_index_code": "850412.SI",
                    "current_index_code": "850412.SI",
                    "identity_state": "EVIDENCE_GATED_ALIAS",
                    "identity_reason_disclosure": "UNKNOWN_UPSTREAM_INTERNAL_CAUSE",
                },
            )
            self.assertTrue(
                all(
                    row["identity_state"] == "DIRECT"
                    for row in catalog["industries"]
                    if row["industry_uid"] != "SW2021:L3:230501"
                )
            )
            special_shard = read_json(run / "ui" / special["shard"])
            self.assertEqual(
                special_shard["schema_version"], "swivd-industry-shard-v2"
            )
            self.assertEqual(special_shard["identity"]["industry_uid"], special["industry_uid"])
            self.assertEqual(
                read_json(run / "audit.json")["schema_version"],
                "swivd-v2-audit-v2",
            )
            audit = read_json(run / "audit.json")
            self.assertEqual(audit["provider_kind"], "TEST_INJECTED_CLIENT")
            self.assertEqual(
                audit["live_validation_state"], "NOT_LIVE_TEST_PROVIDER"
            )
            self.assertEqual(
                audit["live_validation_reason"], "EXPLICIT_CLIENT_INJECTION"
            )
            self.assertTrue(audit["requests"])
            self.assertTrue(
                all(
                    request["provider_kind"] == "TEST_INJECTED_CLIENT"
                    and request["transport"] == "INJECTED_TEST_CLIENT"
                    for request in audit["requests"]
                )
            )
            self.assertEqual(
                read_json(run / "offline_validation.json")["provider_kind"],
                "TEST_INJECTED_CLIENT",
            )
            evidence = read_json(
                run / "inputs" / "normalized" / "industry_identity_resolution.json"
            )
            self.assertEqual(evidence["identity_state"], "EVIDENCE_GATED_ALIAS")
            self.assertEqual(evidence["intervals"][0]["source_ts_code"], "850412.SI")
            self.assertEqual(catalog["legacy_archive"]["taxonomy"], "SW2014")
            self.assertEqual(catalog["legacy_archive"]["level"], "L1")
            rebuilt = Path(temporary) / "离线 重建"
            self.assertEqual(rebuild_v2(run_dir=run, output_dir=rebuilt)["status"], "PASS")

            historical = run_snapshot(spec_path=project / "PROJECT_SPEC_V4.json", data_dir=data, as_of="20211213", purpose="MATERIALIZE_DATE", client=client)
            self.assertEqual((data / "latest_run.json").read_bytes(), current_pointer)
            self.assertEqual(read_json(data / "historical_index.json")["dates"]["20211213"]["run_id"], historical["run_id"])

            client.bad_daily_basic = True
            with self.assertRaisesRegex(V2DataError, "ENDPOINT_ROW_LIMIT"):
                run_snapshot(spec_path=project / "PROJECT_SPEC_V4.json", data_dir=data, as_of="20211214", purpose="UPDATE_LATEST", client=client)
            self.assertEqual((data / "latest_run.json").read_bytes(), current_pointer)

            peer = run / "tables" / "stock_peer_valuation.csv"
            original_peer = peer.read_bytes()
            rows = read_csv(peer)
            header = list(rows[0])
            rows[0]["pe_ttm_percentile_le"] = "0"
            write_csv(peer, rows, header)
            manifest_record = read_json(run / "manifest.json")
            for artifact in manifest_record["artifacts"]:
                if artifact["path"] == "tables/stock_peer_valuation.csv":
                    artifact.update(bytes=peer.stat().st_size, sha256=sha256_file(peer))
            write_json(run / "manifest.json", manifest_record)
            write_sums(run)
            with self.assertRaisesRegex(ValueError, "percentile recomputation mismatch"):
                validate_run_v2(run)

            peer.write_bytes(original_peer)
            manifest_record["artifacts"] = inventory_artifacts(run)
            write_json(run / "manifest.json", manifest_record)
            write_sums(run)
            validate_run_v2(run)
            canary = "tushare_" + "token=" + "CANARY_TOKEN_VALUE_123456"
            (run / "leak.txt").write_text(canary, encoding="utf-8")
            manifest_record["artifacts"] = inventory_artifacts(run)
            write_json(run / "manifest.json", manifest_record)
            write_sums(run)
            with self.assertRaisesRegex(ValueError, "possible secret value found"):
                validate_run_v2(run)
        self.assertEqual(legacy_manifest.read_bytes(), before)

    def test_sw2014_archive_catalog_and_industry_are_get_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            manifest = run_snapshot(
                spec_path=PROJECT_ROOT / "PROJECT_SPEC_V4.json",
                data_dir=data,
                as_of="20211213",
                purpose="UPDATE_LATEST",
                client=FakeClient(),
            )
            run_id = manifest["run_id"]
            run = data / "runs" / run_id
            catalog_path = run / "ui" / "catalog.json"
            catalog_v3 = read_json(catalog_path)

            bad_catalog = json.loads(json.dumps(catalog_v3))
            bad_catalog["legacy_archive"]["run_id"] = "SWIVD-RUN-UNKNOWN"
            write_json(catalog_path, bad_catalog)
            manifest_record = read_json(run / "manifest.json")
            manifest_record["artifacts"] = inventory_artifacts(run)
            write_json(run / "manifest.json", manifest_record)
            write_sums(run)
            with self.assertRaisesRegex(ValueError, "legacy archive identity"):
                validate_run_v2(run)

            write_json(catalog_path, catalog_v3)
            manifest_record["artifacts"] = inventory_artifacts(run)
            write_json(run / "manifest.json", manifest_record)
            write_sums(run)
            validate_run_v2(run)

            with running_local_app(self, data) as (app, port):
                ui_catalog_route = f"/api/v1/snapshots/{run_id}/catalog"
                status, _, payload = local_request(port, "GET", ui_catalog_route)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(payload)["schema_version"], "swivd-ui-catalog-v3")

                verified_catalog_bytes = catalog_path.read_bytes()
                catalog_path.write_bytes(verified_catalog_bytes + b"\n")
                status, _, _ = local_request(port, "GET", ui_catalog_route)
                self.assertEqual(status, 404)
                catalog_path.write_bytes(verified_catalog_bytes)

                catalog_route = f"/api/v1/snapshots/{run_id}/archive/sw2014/catalog"
                status, headers, payload = local_request(port, "GET", catalog_route)
                self.assertEqual(status, 200)
                self.assertNotIn("access-control-allow-origin", headers)
                archive = json.loads(payload)
                self.assertEqual(archive["schema_version"], "swivd-sw2014-catalog-v1")
                self.assertEqual(archive["taxonomy"], "SW2014")
                self.assertEqual(archive["level"], "L1")
                self.assertEqual(archive["source_run_id"], "SWIVD-RUN-20260828-004")
                self.assertEqual(len(archive["industries"]), 28)

                code = archive["industries"][0]["index_code"]
                detail_route = f"/api/v1/snapshots/{run_id}/archive/sw2014/industries/{code}"
                status, _, payload = local_request(port, "GET", detail_route)
                self.assertEqual(status, 200)
                detail = json.loads(payload)
                self.assertEqual(detail["schema_version"], "swivd-sw2014-industry-v1")
                self.assertEqual(detail["taxonomy"], "SW2014")
                self.assertEqual(detail["level"], "L1")
                self.assertEqual(detail["source_run_id"], "SWIVD-RUN-20260828-004")
                self.assertEqual(detail["index_code"], code)
                self.assertEqual(detail["summary"]["index_code"], code)
                self.assertTrue(detail["history"])
                self.assertTrue(all(row["index_code"] == code for row in detail["history"]))
                self.assertNotIn("constituents", detail)

                status, _, _ = local_request(
                    port,
                    "POST",
                    detail_route,
                    body="{}",
                    headers={
                        "Origin": f"http://127.0.0.1:{port}",
                        "Content-Type": "application/json",
                        "X-SWIVD-Nonce": app.nonce,
                    },
                )
                self.assertIn(status, {404, 405})

    def test_legacy_catalog_v1_and_v2_read_adapters_do_not_write(self):
        run_id = "SWIVD2-RUN-20211213-001"
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            app = LocalApp(
                project_root=PROJECT_ROOT,
                data_dir=data,
                host="127.0.0.1",
                port=8765,
            )
            before = {
                path.relative_to(data).as_posix(): path.read_bytes()
                for path in data.rglob("*")
                if path.is_file()
            }
            catalog_v2 = {
                "schema_version": "swivd-ui-catalog-v2",
                "industries": [{"level": "L1", "index_code": "801040.SI"}],
            }
            with mock.patch.object(app, "snapshot"), mock.patch.object(
                app, "verified_json", return_value=catalog_v2
            ):
                self.assertEqual(app.ui_catalog(run_id), catalog_v2)

            catalog_v1 = {
                "schema_version": "swivd-ui-catalog-v1",
                "as_of": "20211213",
                "research_grade": "RESEARCH_ONLY",
                "decision_eligible": False,
                "production_approved": False,
                "levels": ["L1", "L2", "L3"],
                "industries": [
                    {
                        "level": "L1",
                        "index_code": "801040.SI",
                        "industry_name": "钢铁",
                        "is_pub": 1,
                        "member_row_count": 0,
                        "valuation_state": "OK",
                        "shard": "industries/L1/801040_SI.json",
                    }
                ],
                "legacy_archive": {
                    "run_id": "SWIVD-RUN-20260828-004",
                    "level": "L1",
                    "taxonomy": "SW2014",
                },
            }
            shard = {
                "industry": {
                    "index_code": "801040.SI",
                    "industry_name": "钢铁",
                    "as_of": "20211213",
                    "pe_status": "INSUFFICIENT_HISTORY",
                    "pb_status": "INSUFFICIENT_HISTORY",
                    "return_5d_status": "INSUFFICIENT_HISTORY",
                    "return_mtd_status": "INSUFFICIENT_HISTORY",
                    "return_ytd_status": "INSUFFICIENT_HISTORY",
                }
            }

            def legacy_json(_run_id, relative):
                return catalog_v1 if relative == "ui/catalog.json" else shard

            with mock.patch.object(app, "snapshot"), mock.patch.object(
                app, "verified_json", side_effect=legacy_json
            ), mock.patch.object(
                app,
                "verified_csv",
                return_value=[{"index_code": "801040.SI", "parent_code": ""}],
            ):
                adapted = app.ui_catalog(run_id)
            self.assertEqual(adapted["schema_version"], "swivd-ui-catalog-v2")
            self.assertEqual(adapted["industries"][0]["parent_code"], "")
            self.assertIn("pe_percentile", adapted["industries"][0])
            self.assertEqual(catalog_v1["schema_version"], "swivd-ui-catalog-v1")
            after = {
                path.relative_to(data).as_posix(): path.read_bytes()
                for path in data.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
