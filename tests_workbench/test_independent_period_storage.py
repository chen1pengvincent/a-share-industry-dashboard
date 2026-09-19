"""Independent calendar, temporal-statistics, and publication fault tests.

All filesystem changes are confined to TemporaryDirectory.  No provider,
network client, user runtime, wall clock, or production fixture is used.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from industry_workbench.models import DataError
from industry_workbench.periods import aggregate_period, period_dates
from industry_workbench.query import HistoryAccumulator, QueryService, compile_views
from industry_workbench.storage import FileStore


UID = "SW2021:L1:110000"


def independent_row(day: str, *, pe: str | None = "10", flow: str | None = "100", evidence: str = "OFFICIAL_DATED") -> dict:
    def item(value):
        return {"value": value, "status": "NA" if value is None else "OK",
                "reason_codes": ["TEST_MISSING"] if value is None else [], "metric_date": day}
    return {
        "uid": UID, "taxonomy": "SW", "version": "SW2021", "level": "L1",
        "name": "独立测试行业", "code": "110000", "parent_uid": None,
        "membership_evidence_kind": evidence, "status": "OK", "counts": {},
        "metrics": {"pe_ttm_median": item(pe), "pb_median": item("2"),
                    "official_pe": item("15"), "official_pb": item("3"),
                    "close": item("100"), "flow_cent": item(flow), "net_mf_vol": item("1")},
    }


def weekdays(start: str, count: int) -> list[str]:
    result = []
    current = date.fromisoformat(start)
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return result


class IndependentPeriodTests(unittest.TestCase):
    def test_natural_week_uses_monday_key_and_calendar_not_rolling_five_days(self):
        calendar = ["20260911", "20260914", "20260915", "20260916", "20260917", "20260918"]
        result = period_dates("week", "2026-09-16", "20260918", calendar)
        self.assertEqual(result["key"], "20260914")
        self.assertEqual(result["start"], "20260914")
        self.assertEqual(result["end"], "20260920")
        self.assertEqual(result["trade_dates"], calendar[1:])
        self.assertEqual(result["endpoint"], "20260918")

    def test_month_end_weekend_does_not_pull_next_month_quote(self):
        result = period_dates("month", "2026-01", "20260202", ["20260129", "20260130", "20260202"])
        self.assertEqual(result["endpoint"], "20260130")
        self.assertEqual(result["trade_dates"], ["20260129", "20260130"])
        self.assertEqual(result["status"], "COMPLETE")

    def test_unfinished_month_excludes_future_calendar_days(self):
        result = period_dates("month", "2026-09", "20260916", ["20260914", "20260915", "20260916", "20260917", "20260930"])
        self.assertEqual(result["trade_dates"], ["20260914", "20260915", "20260916"])
        self.assertEqual(result["status"], "IN_PROGRESS")
        self.assertEqual(result["endpoint"], "20260916")

    def test_calendar_closed_days_are_not_missing_trading_observations(self):
        calendar = [
            {"cal_date": "20260216", "is_open": 0}, {"cal_date": "20260217", "is_open": 0},
            {"cal_date": "20260218", "is_open": 0}, {"cal_date": "20260219", "is_open": 1},
            {"cal_date": "20260220", "is_open": 1},
        ]
        result = period_dates("week", "20260216", "20260222", calendar)
        self.assertEqual(result["expected_days"], 2)
        self.assertEqual(result["trade_dates"], ["20260219", "20260220"])

    def test_missing_endpoint_keeps_valuation_na_and_does_not_fill_partial_flow(self):
        calendar = ["20260914", "20260915", "20260916", "20260917", "20260918"]
        period = period_dates("week", "20260914", "20260920", calendar)
        days = {day: {"industries": [independent_row(day, pe="10", flow="100")]} for day in calendar[:-1]}
        result = aggregate_period(days, period)
        row = result["industries"][0]
        self.assertIsNone(row["metrics"]["pe_ttm_median"]["value"])
        self.assertIsNone(row["metrics"]["flow_cent"]["value"])
        self.assertEqual(row["metrics"]["flow_cent"]["known_subtotal"], "400")
        self.assertEqual(result["period"]["endpoint"], "20260918")
        self.assertEqual(result["period"]["missing_dates"], ["20260918"])
        self.assertEqual(result["period"]["status"], "DATA_GAP")

    def test_present_endpoint_with_missing_estimate_never_reuses_previous_estimate(self):
        calendar = ["20260917", "20260918"]
        period = period_dates("week", "20260918", "20260920", calendar)
        days = {"20260917": {"industries": [independent_row("20260917", pe="50", flow="100")]},
                "20260918": {"industries": [independent_row("20260918", pe=None, flow="-40")]}}
        row = aggregate_period(days, period)["industries"][0]
        self.assertIsNone(row["metrics"]["pe_ttm_median"]["value"])
        self.assertEqual(row["metrics"]["flow_cent"]["value"], "60")

    def test_period_estimate_is_endpoint_not_mean_of_daily_estimates(self):
        calendar = ["20260917", "20260918"]
        period = period_dates("week", "20260918", "20260920", calendar)
        days = {"20260917": {"industries": [independent_row("20260917", pe="100", flow="125")]},
                "20260918": {"industries": [independent_row("20260918", pe="10", flow="-26")]}}
        row = aggregate_period(days, period)["industries"][0]
        self.assertEqual(row["metrics"]["pe_ttm_median"]["value"], "10")
        self.assertEqual(row["metrics"]["flow_cent"]["value"], "99")

    def test_period_keeps_known_subtotals_from_incomplete_daily_membership(self):
        calendar = ["20260916", "20260917", "20260918"]
        complete = independent_row("20260916", flow="100")
        incomplete = independent_row("20260917", flow=None)
        incomplete["metrics"]["flow_cent"]["known_subtotal"] = "35"
        incomplete["metrics"]["net_mf_vol"].update(value=None, status="NA", known_subtotal="7")
        days = {"20260916": {"industries": [complete]}, "20260917": {"industries": [incomplete]}}
        for kind, key in (("week", "20260914"), ("month", "2026-09")):
            with self.subTest(kind=kind):
                row = aggregate_period(days, period_dates(kind, key, "20260920", calendar))["industries"][0]
                self.assertIsNone(row["metrics"]["flow_cent"]["value"])
                self.assertEqual(row["metrics"]["flow_cent"]["known_subtotal"], "135")
                self.assertEqual(row["metrics"]["net_mf_vol"]["known_subtotal"], "8")
                self.assertEqual(row["metrics"]["flow_cent"]["missing_dates"], ["20260917", "20260918"])


class IndependentHistoricalPercentileTests(unittest.TestCase):
    def test_251_values_are_insufficient_and_252nd_tied_observation_is_inclusive(self):
        calendar = weekdays("2025-01-01", 252)
        accumulator = HistoryAccumulator(calendar)
        result = None
        for day in calendar[:-1]:
            result = accumulator.enrich(day, [independent_row(day)])
        self.assertEqual(result[0]["metrics"]["pe_percentile"]["valid_count"], 251)
        self.assertIsNone(result[0]["metrics"]["pe_percentile"]["value"])
        result = accumulator.enrich(calendar[-1], [independent_row(calendar[-1])])[0]["metrics"]["pe_percentile"]
        self.assertEqual(Decimal(result["value"]), Decimal("100"))
        self.assertEqual(result["valid_count"], 252)
        self.assertEqual(result["equal_count"], 252)

    def test_future_observation_does_not_change_already_produced_historical_result(self):
        calendar = weekdays("2025-01-01", 253)
        accumulator = HistoryAccumulator(calendar)
        for day in calendar[:251]:
            accumulator.enrich(day, [independent_row(day, pe="10")])
        historical = accumulator.enrich(calendar[251], [independent_row(calendar[251], pe="20")])
        before = copy.deepcopy(historical)
        accumulator.enrich(calendar[252], [independent_row(calendar[252], pe="10000")])
        self.assertEqual(historical, before)
        self.assertEqual(Decimal(historical[0]["metrics"]["pe_percentile"]["value"]), Decimal("100"))

    def test_missing_observation_does_not_count_toward_252(self):
        calendar = weekdays("2025-01-01", 253)
        accumulator = HistoryAccumulator(calendar)
        for day in calendar[:251]:
            accumulator.enrich(day, [independent_row(day)])
        missing = accumulator.enrich(calendar[251], [independent_row(calendar[251], pe=None)])
        self.assertEqual(missing[0]["metrics"]["pe_percentile"]["valid_count"], 251)
        self.assertIsNone(missing[0]["metrics"]["pe_percentile"]["value"])
        last = accumulator.enrich(calendar[252], [independent_row(calendar[252], pe="20")])[0]["metrics"]["pe_percentile"]
        self.assertEqual(last["valid_count"], 252)
        self.assertEqual(last["missing_days"], 1)
        self.assertEqual(Decimal(last["value"]), Decimal("100"))

    def test_membership_evidence_change_does_not_borrow_other_series_samples(self):
        calendar = weekdays("2025-01-01", 252)
        accumulator = HistoryAccumulator(calendar)
        for day in calendar[:251]:
            accumulator.enrich(day, [independent_row(day, evidence="OFFICIAL_DATED")])
        result = accumulator.enrich(calendar[251], [independent_row(calendar[251], evidence="OBSERVED_SAME_DAY")])[0]
        self.assertEqual(result["metrics"]["pe_percentile"]["valid_count"], 1)
        self.assertIsNone(result["metrics"]["pe_percentile"]["value"])
        self.assertEqual(result["metrics"]["official_pe_percentile"]["valid_count"], 252)

    def test_out_of_order_date_is_rejected_instead_of_including_future_samples(self):
        calendar = weekdays("2025-01-01", 253)
        accumulator = HistoryAccumulator(calendar)
        for day in calendar[:251]:
            accumulator.enrich(day, [independent_row(day)])
        accumulator.enrich(calendar[252], [independent_row(calendar[252], pe="10000")])
        with self.assertRaises(DataError):
            accumulator.enrich(calendar[251], [independent_row(calendar[251], pe="20")])


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 9, 17, 20, 0, 0, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class IndependentPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="industry-independent-")
        self.addCleanup(self.temporary.cleanup)
        self.clock_patch = patch("industry_workbench.storage.datetime", FrozenDatetime)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.store = FileStore(Path(self.temporary.name), development=True)

    def payload(self, label: str = "sample") -> dict:
        ref = self.store.put_json({"value": label}, "independent_test")
        return {"as_of": "20260916", "provider_kind": "TEST_INJECTED_CLIENT",
                "source": {"commit": None, "git_dirty": True, "tree_sha256": "0" * 64, "files": {}},
                "data": ref, "publication_state": "PUBLISHED_WITH_GAPS"}

    def initial_batch(self) -> dict:
        with self.store.writer():
            return self.store.publish(self.payload("first"))

    def test_member_known_contributions_reconcile_when_industry_or_period_is_incomplete(self):
        from industry_workbench.metrics import compute_day
        from test_independent_domain import fixture, DAY, SW_A
        inputs = fixture([("10", "1", "1.11"), ("20", "2", "-0.11"), ("30", "3", "5.00")])
        inputs["memberships"][2]["state"] = "MEMBERSHIP_BOUNDARY_UNKNOWN"
        result = compute_day(inputs)
        self.assertIsNone(result["industries"][0]["metrics"]["flow_cent"]["value"])
        self.assertEqual(result["industries"][0]["metrics"]["flow_cent"]["known_subtotal"], "100")
        unresolved = next(row for row in result["members"] if row["ts_code"] == "000003.SZ")
        self.assertIsNone(unresolved.get("known_subtotal"))
        with self.store.writer():
            from support_day_refs import put_day_refs
            days = {DAY: put_day_refs(self.store, inputs, result)}
            payload = self.payload("member-reconciliation")
            payload.update(days=days, history_start="20260914", views=compile_views(self.store, days, ["20260914", "20260915", DAY]))
            manifest = self.store.publish(payload)
        query = QueryService(self.store)
        for kind, key in (("day", DAY), ("week", "20260914")):
            with self.subTest(kind=kind):
                rows = query.members(manifest["batch_id"], SW_A, period_kind=kind, period_key=key)["rows"]
                known = {row["ts_code"]: row["known_subtotal"] for row in rows}
                self.assertEqual(known["000001.SZ"], "111")
                self.assertEqual(known["000002.SZ"], "-11")
                self.assertEqual(sum(int(value or 0) for value in known.values()), 100)
                self.assertTrue(all(row["flow_cent"] is None for row in rows))

    def interrupted_publish(self, stage: str):
        def stop(actual):
            if actual == stage:
                raise RuntimeError("injected interruption")
        with self.assertRaisesRegex(RuntimeError, "injected interruption"):
            with self.store.writer():
                self.store.publish(self.payload("second"), fault=stop)

    def test_prepared_recovery_restores_old_pointer_before_commit(self):
        first = self.initial_batch()
        self.interrupted_publish("pointer_replaced")
        with self.assertRaises(DataError) as caught:
            self.store.current()
        self.assertEqual(caught.exception.code, "PUBLICATION_RECOVERY_REQUIRED")
        with self.store.writer():
            pass
        self.assertEqual(self.store.current()["batch_id"], first["batch_id"])
        ledger = [json.loads(line) for line in self.store.path("run_ledger.ndjson").read_text().splitlines()]
        self.assertEqual([line["batch_id"] for line in ledger], [first["batch_id"]])

    def test_prepared_first_publish_recovery_returns_to_no_current(self):
        self.interrupted_publish("prepared")
        with self.store.writer():
            pass
        self.assertIsNone(self.store.current())

    def test_committed_recovery_rolls_forward_and_appends_ledger_once(self):
        first = self.initial_batch()
        self.interrupted_publish("committed")
        transaction = json.loads(self.store.path("transactions/publication.json").read_text())
        second_id = transaction["after"]["batch_id"]
        with self.store.writer():
            pass
        with self.store.writer():
            pass
        self.assertNotEqual(second_id, first["batch_id"])
        self.assertEqual(self.store.current()["batch_id"], second_id)
        ledger = [json.loads(line) for line in self.store.path("run_ledger.ndjson").read_text().splitlines()]
        self.assertEqual(sum(item["batch_id"] == second_id for item in ledger), 1)

    def test_committed_recovery_rejects_corrupt_referenced_object(self):
        self.initial_batch()
        self.interrupted_publish("committed")
        transaction = json.loads(self.store.path("transactions/publication.json").read_text())
        target = self.store.manifest(transaction["after"]["batch_id"])
        self.store.path(target["data"]["path"]).write_bytes(gzip.compress(b"tampered", mtime=0))
        with self.assertRaises(DataError):
            with self.store.writer():
                pass
        self.assertTrue(self.store.path("transactions/publication.json").exists())

    def test_recovery_rejects_conflicting_existing_ledger_record(self):
        self.initial_batch()
        self.interrupted_publish("committed")
        transaction = json.loads(self.store.path("transactions/publication.json").read_text())
        ledger = self.store.path("run_ledger.ndjson")
        with ledger.open("a") as stream:
            stream.write(json.dumps({"batch_id": transaction["after"]["batch_id"], "status": "COMMITTED", "manifest_sha256": "f" * 64}) + "\n")
        with self.assertRaises(DataError):
            with self.store.writer():
                pass

    def test_direct_object_hash_corruption_is_detected(self):
        with self.store.writer():
            ref = self.store.put_json({"expected": "original"})
        self.store.path(ref["path"]).write_bytes(gzip.compress(b"changed", mtime=0))
        with self.assertRaises(DataError) as caught:
            self.store.read_json(ref)
        self.assertEqual(caught.exception.code, "OBJECT_HASH_MISMATCH")

    def test_publish_requires_raw_source_closure_not_only_top_level_refs(self):
        with self.store.writer():
            raw = self.store.put_bytes(b'{"source": "raw response"}', "tushare_response")
            normalized = self.store.put_json({"source_refs": [raw]}, "day_input")
            payload = self.payload()
            payload["days"] = {"20260916": {"input": normalized}}
            self.store.path(raw["path"]).write_bytes(gzip.compress(b"corrupted source", mtime=0))
            with self.assertRaises(DataError):
                self.store.publish(payload)

    def test_manifest_tampering_is_detected(self):
        batch = self.initial_batch()
        path = self.store.path(f'batches/{batch["batch_id"]}/manifest.json')
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        with self.assertRaises(DataError):
            self.store.current()

    def test_week_history_keeps_an_entire_missing_week_as_gap(self):
        calendar = weekdays("2026-02-02", 11)
        observed = ["20260202", "20260216"]
        with self.store.writer():
            days = {}
            for day in observed:
                from support_day_refs import put_day_refs
                result = {"trade_date": day, "industries": [independent_row(day)], "members": []}
                days[day] = put_day_refs(self.store, {"trade_date": day}, result)
            views = compile_views(self.store, days, calendar)
            payload = self.payload()
            payload.update(as_of="20260216", history_start="20260202", days=days, views=views)
            batch = self.store.publish(payload)
        rows = QueryService(self.store).history(batch["batch_id"], UID, "week")["rows"]
        by_period = {row["period"]["key"]: row for row in rows}
        self.assertIn("20260209", by_period)
        gap = by_period["20260209"]
        self.assertIsNone(gap["metrics"]["flow_cent"]["value"])
        self.assertEqual(gap["period"]["status"], "DATA_GAP")


if __name__ == "__main__":
    unittest.main()
