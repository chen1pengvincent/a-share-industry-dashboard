from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from swivd.core import (  # noqa: E402
    DataValidationError,
    NAME_HISTORY_POLICY,
    OHLC_ORDERING_POLICY,
    PER_CODE_CONTINUITY_POLICY,
    classification_whitelist,
    normalize_history,
    ohlc_ordering_audit,
    validate_index_classify,
    validate_name_history,
    validate_per_code_history_continuity,
    validate_sw_daily,
)


def _weekdays(count: int) -> list[str]:
    cursor = date(2020, 1, 2)
    result: list[str] = []
    while len(result) < count:
        if cursor.weekday() < 5:
            result.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return result


def _classification(src: str, count: int, is_pub: Any) -> list[dict[str, Any]]:
    prefix = 810000 if src == "SW2014" else 801000
    return [
        {
            "index_code": f"{prefix + position:06d}.SI",
            "industry_name": f"{src}行业{position}",
            "parent_code": "",
            "level": "L1",
            "industry_code": f"{position:06d}",
            "is_pub": is_pub,
            "src": src,
        }
        for position in range(1, count + 1)
    ]


def _series(code: str, dates: list[str], *, taxonomy: str = "SW2014") -> list[dict[str, Any]]:
    return [
        {
            "ts_code": code,
            "trade_date": trade_date,
            "pe": "12.5",
            "pb": "1.5",
            "taxonomy": taxonomy,
        }
        for trade_date in dates
    ]


def _daily_row(code: str, trade_date: str, name: str) -> dict[str, Any]:
    return {
        "ts_code": code,
        "trade_date": trade_date,
        "name": name,
        "open": "10",
        "low": "9",
        "high": "11",
        "close": "10",
        "change": "0",
        "pct_change": "0",
        "vol": "100",
        "amount": "1000",
        "pe": "12.5",
        "pb": "1.5",
        "total_mv": "10000",
        "float_mv": "8000",
    }


def _renamed_history_fixture() -> tuple[
    list[str], dict[str, str], list[dict[str, Any]], list[dict[str, Any]]
]:
    dates = ["20140102", "20150121", "20150122", "20211210"]
    whitelist = {"801040.SI": "钢铁", "801210.SI": "休闲服务"}
    source_names = {
        "801040.SI": ["黑色金属", "黑色金属", "钢铁", "钢铁"],
        "801210.SI": ["餐饮旅游", "餐饮旅游", "休闲服务", "休闲服务"],
    }
    rows = [
        _daily_row(code, trade_date, source_name)
        for code in sorted(whitelist)
        for trade_date, source_name in zip(dates, source_names[code])
    ]
    classifications = [
        {
            "index_code": code,
            "industry_name": industry_name,
            "parent_code": "",
            "level": "L1",
            "industry_code": code.split(".")[0],
            "is_pub": None,
            "src": "SW2014",
        }
        for code, industry_name in whitelist.items()
    ]
    return dates, whitelist, rows, classifications


class OhlcOrderingDisclosureTests(unittest.TestCase):
    def test_non_formula_ohlc_ordering_anomaly_is_preserved_and_disclosed(self) -> None:
        row = _daily_row("801080.SI", "20151230", "电子")
        row["high"] = "9.8"
        validated = validate_sw_daily(
            [row],
            whitelist={"801080.SI": "电子"},
            start_date="20151230",
            end_date="20151230",
            open_dates=["20151230"],
            row_limit=None,
        )
        self.assertEqual(validated, [row])
        disclosure = ohlc_ordering_audit(validated)
        self.assertEqual(disclosure["policy"], OHLC_ORDERING_POLICY)
        self.assertFalse(disclosure["blocking"])
        self.assertEqual(disclosure["formula_fields"], ["close", "pe", "pb"])
        self.assertEqual(disclosure["anomaly_count"], 1)
        self.assertEqual(
            disclosure["anomalies"][0]["anomaly_codes"],
            ["HIGH_BELOW_CLOSE", "HIGH_BELOW_OPEN"],
        )

    def test_nonpositive_present_ohl_field_still_fails_closed(self) -> None:
        row = _daily_row("801080.SI", "20151230", "电子")
        row["high"] = "-1"
        with self.assertRaises(DataValidationError) as caught:
            validate_sw_daily(
                [row],
                whitelist={"801080.SI": "电子"},
                start_date="20151230",
                end_date="20151230",
                open_dates=["20151230"],
                row_limit=None,
            )
        self.assertEqual(caught.exception.code, "INVALID_PRICE")


class RetiredClassificationSemanticsTests(unittest.TestCase):
    def test_sw2014_requires_null_and_all_rows_are_selected_without_mutation(self) -> None:
        rows = _classification("SW2014", 2, None)
        validated = validate_index_classify(rows, src="SW2014", expected_count=2)
        whitelist = classification_whitelist(
            validated, published_only=False, src="SW2014"
        )
        self.assertEqual(len(whitelist), 2)
        self.assertTrue(all(row["is_pub"] is None for row in rows))
        self.assertTrue(all(row["is_pub"] is None for row in validated))
        serialized = deepcopy(validated)
        for row in serialized:
            row["is_pub"] = ""
        self.assertEqual(
            classification_whitelist(
                serialized, published_only=False, src="SW2014"
            ),
            whitelist,
        )
        self.assertTrue(all(row["is_pub"] == "" for row in serialized))
        with self.assertRaises(DataValidationError) as caught:
            classification_whitelist(validated, src="SW2014")
        self.assertEqual(caught.exception.code, "RETIRED_PUBLICATION_FILTER_FORBIDDEN")

    def test_sw2014_forged_or_mixed_binary_state_is_rejected(self) -> None:
        forged = _classification("SW2014", 2, None)
        forged[0]["is_pub"] = "1"
        with self.assertRaises(DataValidationError) as caught:
            validate_index_classify(forged, src="SW2014", expected_count=2)
        self.assertEqual(caught.exception.code, "RETIRED_PUBLICATION_STATE_INVALID")
        with self.assertRaises(DataValidationError) as caught:
            classification_whitelist(forged, published_only=False, src="SW2014")
        self.assertEqual(caught.exception.code, "RETIRED_PUBLICATION_STATE_INVALID")

    def test_sw2021_still_requires_strict_binary_non_boolean_state(self) -> None:
        for invalid in (None, True, ""):
            rows = _classification("SW2021", 1, invalid)
            with self.assertRaises(DataValidationError) as caught:
                validate_index_classify(rows, src="SW2021", expected_count=1)
            self.assertEqual(caught.exception.code, "INVALID_FLAG")


class PerCodeContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.open_dates = _weekdays(270)
        self.codes = ["810001.SI", "810002.SI"]
        self.whitelist = {self.codes[0]: "行业1", self.codes[1]: "行业2"}
        self.rows = _series(self.codes[0], self.open_dates) + _series(
            self.codes[1], self.open_dates[10:]
        )

    def _validate(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        return validate_per_code_history_continuity(
            rows,
            whitelist=self.whitelist,
            open_dates=self.open_dates,
            end_date=self.open_dates[-1],
            minimum_valid_observations=252,
            src="SW2014",
        )

    def _assert_code(self, rows: list[dict[str, Any]], expected: str) -> None:
        with self.assertRaises(DataValidationError) as caught:
            self._validate(rows)
        self.assertEqual(caught.exception.code, expected)

    def test_each_code_may_start_late_but_must_be_continuous_to_common_end(self) -> None:
        result = self._validate(self.rows)
        self.assertEqual(result["policy"], PER_CODE_CONTINUITY_POLICY)
        self.assertEqual(result["code_count"], 2)
        self.assertEqual(result["total_actual_rows"], 530)
        self.assertEqual(result["common_end_date"], self.open_dates[-1])
        by_code = {row["code"]: row for row in result["per_code"]}
        self.assertEqual(by_code[self.codes[0]]["first_observed_date"], self.open_dates[0])
        self.assertEqual(by_code[self.codes[0]]["expected_rows"], 270)
        self.assertEqual(by_code[self.codes[1]]["first_observed_date"], self.open_dates[10])
        self.assertEqual(by_code[self.codes[1]]["expected_rows"], 260)
        self.assertEqual(by_code[self.codes[1]]["pe_positive_count"], 260)
        self.assertEqual(by_code[self.codes[1]]["pb_positive_count"], 260)

    def test_internal_gap_and_common_end_missing_are_rejected(self) -> None:
        gap = [
            row
            for row in self.rows
            if not (
                row["ts_code"] == self.codes[1]
                and row["trade_date"] == self.open_dates[100]
            )
        ]
        self._assert_code(gap, "INTERNAL_TRADING_DAY_GAP")
        no_end = [
            row
            for row in self.rows
            if not (
                row["ts_code"] == self.codes[1]
                and row["trade_date"] == self.open_dates[-1]
            )
        ]
        self._assert_code(no_end, "COMMON_END_MISSING")

    def test_duplicate_cross_axis_and_outside_code_are_rejected(self) -> None:
        duplicate = self.rows + [dict(self.rows[0])]
        self._assert_code(duplicate, "DUPLICATE_PRIMARY_KEY")
        cross_axis = deepcopy(self.rows)
        cross_axis[0]["taxonomy"] = "SW2021"
        self._assert_code(cross_axis, "CROSS_AXIS_ROW")
        outside = self.rows + _series("899999.SI", [self.open_dates[-1]])
        self._assert_code(outside, "CODE_OUTSIDE_WHITELIST")

    def test_pe_and_pb_each_have_an_independent_minimum_gate(self) -> None:
        for field in ("pe", "pb"):
            rows = deepcopy(self.rows)
            affected = [row for row in rows if row["ts_code"] == self.codes[1]][:9]
            for row in affected:
                row[field] = None
            self._assert_code(rows, "VALUATION_OBSERVATIONS_INSUFFICIENT")


class NameHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        (
            self.dates,
            self.whitelist,
            self.rows,
            self.classifications,
        ) = _renamed_history_fixture()

    def test_default_rejects_name_drift_but_explicit_archive_policy_accepts_it(self) -> None:
        with self.assertRaises(DataValidationError) as caught:
            validate_sw_daily(
                self.rows,
                whitelist=self.whitelist,
                start_date=self.dates[0],
                end_date=self.dates[-1],
                open_dates=self.dates,
                row_limit=None,
            )
        self.assertEqual(caught.exception.code, "NAME_DRIFT")

        validated = validate_sw_daily(
            self.rows,
            whitelist=self.whitelist,
            start_date=self.dates[0],
            end_date=self.dates[-1],
            open_dates=self.dates,
            row_limit=None,
            allow_name_history=True,
        )
        self.assertEqual(validated, self.rows)

    def test_real_sw2014_renames_are_summarized_as_stable_code_segments(self) -> None:
        result = validate_name_history(
            self.rows,
            whitelist=self.whitelist,
            end_date=self.dates[-1],
            src="SW2014",
        )
        self.assertEqual(NAME_HISTORY_POLICY, "STABLE_TS_CODE_WITH_SOURCE_NAME_HISTORY")
        self.assertEqual(
            set(result),
            {
                "code_count",
                "renamed_code_count",
                "renamed_codes",
                "segment_count",
                "segments",
            },
        )
        self.assertEqual(result["code_count"], 2)
        self.assertEqual(result["renamed_code_count"], 2)
        self.assertEqual(result["renamed_codes"], ["801040.SI", "801210.SI"])
        self.assertEqual(result["segment_count"], 4)
        self.assertEqual(
            result["segments"],
            [
                {
                    "index_code": "801040.SI",
                    "source_name": "黑色金属",
                    "first_date": "20140102",
                    "last_date": "20150121",
                    "row_count": 2,
                },
                {
                    "index_code": "801040.SI",
                    "source_name": "钢铁",
                    "first_date": "20150122",
                    "last_date": "20211210",
                    "row_count": 2,
                },
                {
                    "index_code": "801210.SI",
                    "source_name": "餐饮旅游",
                    "first_date": "20140102",
                    "last_date": "20150121",
                    "row_count": 2,
                },
                {
                    "index_code": "801210.SI",
                    "source_name": "休闲服务",
                    "first_date": "20150122",
                    "last_date": "20211210",
                    "row_count": 2,
                },
            ],
        )

    def test_source_name_is_preserved_without_replacing_frozen_industry_name(self) -> None:
        normalized = normalize_history(
            self.rows,
            self.classifications,
            src="SW2014",
        )
        early_steel = next(
            row
            for row in normalized
            if row["index_code"] == "801040.SI" and row["trade_date"] == "20140102"
        )
        self.assertEqual(early_steel["source_name"], "黑色金属")
        self.assertEqual(early_steel["industry_name"], "钢铁")

    def test_empty_terminal_mismatch_and_outside_code_fail_closed(self) -> None:
        empty_name = deepcopy(self.rows)
        empty_name[0]["name"] = ""
        with self.assertRaises(DataValidationError) as caught:
            validate_name_history(
                empty_name,
                whitelist=self.whitelist,
                end_date=self.dates[-1],
                src="SW2014",
            )
        self.assertEqual(caught.exception.code, "INVALID_INDUSTRY_NAME")

        terminal_mismatch = deepcopy(self.rows)
        terminal_mismatch[3]["name"] = "黑色金属"
        with self.assertRaises(DataValidationError) as caught:
            validate_name_history(
                terminal_mismatch,
                whitelist=self.whitelist,
                end_date=self.dates[-1],
                src="SW2014",
            )
        self.assertEqual(caught.exception.code, "END_NAME_MISMATCH")

        outside_code = deepcopy(self.rows)
        outside_code[0]["ts_code"] = "899999.SI"
        with self.assertRaises(DataValidationError) as caught:
            validate_name_history(
                outside_code,
                whitelist=self.whitelist,
                end_date=self.dates[-1],
                src="SW2014",
            )
        self.assertEqual(caught.exception.code, "CODE_OUTSIDE_WHITELIST")


if __name__ == "__main__":
    unittest.main()
