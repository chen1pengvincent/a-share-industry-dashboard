"""Independent numerical counterexamples for the integrated domain.

Expected values are hand-calculated constants.  Fixtures never call a provider,
read a token, write a snapshot, or reuse a production calculation as an oracle.
"""

from __future__ import annotations

import copy
from decimal import Decimal
import unittest

from industry_workbench.metrics import compute_day
from industry_workbench.models import DataError


DAY = "20260916"
SW_A = "SW2021:L1:110000"
SW_B = "SW2021:L1:220000"
CI_A = "CI:L1:CI005001.CI"
THS_A = "THS:881001.TI"
THS_B = "THS:881002.TI"
TDX_A = "TDX:880:880001.TDX"


def industry(uid: str, taxonomy: str = "SW", *, name: str = "测试行业") -> dict:
    return {
        "uid": uid,
        "taxonomy": taxonomy,
        "version": "SW2021" if taxonomy == "SW" else taxonomy,
        "level": "L1" if taxonomy in {"SW", "CI"} else ("880" if taxonomy == "TDX" else "INDUSTRY"),
        "code": uid.rsplit(":", 1)[-1],
        "name": name,
        "parent_uid": None,
        "is_pub": True,
        "market_code": None,
        "membership_code": None,
    }


def fixture(values: list[tuple[object, object, str]]) -> dict:
    """Make independently specified stock PE, PB and moneyflow observations."""
    payload = {
        "trade_date": DAY,
        "captured_at": "2026-09-16T19:35:00+08:00",
        "provider_kind": "TEST_INJECTED_CLIENT",
        "industries": [industry(SW_A)],
        "memberships": [],
        "unknown_taxonomies": [],
        "stocks": [],
        "daily": [],
        "daily_basic": [],
        "moneyflow": [],
        "official": [],
        "audit": {"base_complete": True, "eligible_count": len(values), "traded_count": len(values)},
        "source_refs": [],
    }
    for index, (pe, pb, amount) in enumerate(values, start=1):
        code = f"{index:06d}.SZ"
        payload["stocks"].append({
            "ts_code": code, "name": f"股票{index}", "list_date": "20200101",
            "delist_date": None, "market": "主板",
        })
        payload["memberships"].append({
            "uid": SW_A, "ts_code": code, "state": "ACTIVE",
            "evidence_kind": "OFFICIAL_DATED", "in_date": "20200101", "out_date": None,
        })
        payload["daily"].append({"ts_code": code, "trade_date": DAY, "close": "10", "vol": "100", "amount": "100"})
        payload["daily_basic"].append({"ts_code": code, "trade_date": DAY, "pe_ttm": pe, "pb": pb})
        payload["moneyflow"].append({"ts_code": code, "trade_date": DAY, "net_mf_amount": amount, "net_mf_vol": "1"})
    return payload


def rows_by_uid(result: dict) -> dict:
    return {row["uid"]: row for row in result["industries"]}


class IndependentDomainTests(unittest.TestCase):
    def assert_decimal(self, row: dict, key: str, expected: str) -> None:
        metric = row["metrics"][key]
        self.assertIsNotNone(metric["value"], (key, metric))
        self.assertEqual(Decimal(metric["value"]), Decimal(expected), (key, metric))

    def assert_unavailable(self, row: dict, key: str) -> None:
        metric = row["metrics"][key]
        self.assertIsNone(metric["value"], (key, metric))
        self.assertEqual(metric["status"], "NA", (key, metric))
        self.assertTrue(metric["reason_codes"], (key, metric))

    def test_odd_median_is_middle_observation_not_mean(self):
        result = compute_day(fixture([("100", "5", "1"), ("10", "1", "2"), ("20", "2", "3")]))
        row = rows_by_uid(result)[SW_A]
        self.assert_decimal(row, "pe_ttm_median", "20")
        self.assert_decimal(row, "pb_median", "2")
        self.assertEqual(row["metrics"]["flow_cent"]["value"], "600")

    def test_even_median_averages_only_two_middle_values(self):
        result = compute_day(fixture([("20", "8", "1"), ("1", "1", "1"), ("10", "4", "1"), ("2", "2", "1")]))
        row = rows_by_uid(result)[SW_A]
        self.assert_decimal(row, "pe_ttm_median", "6")
        self.assert_decimal(row, "pb_median", "3")

    def test_valuation_samples_are_separate_and_loss_makers_keep_their_flow(self):
        payload = fixture([
            ("10", "-1", "1.01"), ("20", "0", "2.02"),
            ("-5", "2", "3.03"), (None, "4", "4.04"),
            ("0", None, "5.05"), ("30", "6", "6.06"),
        ])
        row = rows_by_uid(compute_day(payload))[SW_A]
        self.assert_decimal(row, "pe_ttm_median", "20")
        self.assert_decimal(row, "pb_median", "4")
        self.assertEqual(row["counts"]["member_count"], 6)
        self.assertEqual(row["counts"]["basic_received"], 6)
        self.assertEqual(row["counts"]["pe_valid"], 3)
        self.assertEqual(row["counts"]["pb_valid"], 3)
        self.assertEqual(row["counts"]["flow_expected"], 6)
        self.assertEqual(row["counts"]["flow_received"], 6)
        self.assertEqual(row["metrics"]["flow_cent"]["value"], "2121")

    def test_pe_and_pb_do_not_share_a_common_complete_case_filter(self):
        row = rows_by_uid(compute_day(fixture([
            ("10", None, "1"), ("30", None, "2"),
            (None, "2", "3"), (None, "8", "4"),
        ])))[SW_A]
        self.assert_decimal(row, "pe_ttm_median", "20")
        self.assert_decimal(row, "pb_median", "5")
        self.assertEqual(row["metrics"]["flow_cent"]["value"], "1000")

    def test_no_positive_pe_is_na_while_valid_pb_and_flow_survive(self):
        row = rows_by_uid(compute_day(fixture([
            (None, "1", "10"), ("0", "2", "-3"), ("-10", "9", "0"),
        ])))[SW_A]
        self.assert_unavailable(row, "pe_ttm_median")
        self.assert_decimal(row, "pb_median", "2")
        self.assertEqual(row["metrics"]["flow_cent"]["value"], "700")

    def test_true_zero_moneyflow_survives_exact_decimal_cancellation(self):
        row = rows_by_uid(compute_day(fixture([
            ("10", "1", "0.10"), ("20", "2", "0.20"), ("30", "3", "-0.30"),
        ])))[SW_A]
        self.assertEqual(row["metrics"]["flow_cent"]["value"], "0")
        self.assertEqual(row["metrics"]["flow_cent"]["status"], "OK")
        self.assertEqual(row["counts"]["flow_received"], 3)

    def test_flow_keeps_integer_precision_beyond_javascript_safe_range(self):
        row = rows_by_uid(compute_day(fixture([
            ("10", "1", "90071992547409.91"), ("20", "2", "0.02"),
        ])))[SW_A]
        self.assertEqual(row["metrics"]["flow_cent"]["value"], "9007199254740993")

    def test_one_missing_flow_record_blocks_the_incomplete_base(self):
        payload = fixture([("10", "1", "0"), ("20", "2", "7")])
        payload["moneyflow"].pop()
        # A missing market-base observation is a hard input failure; it cannot
        # become either a true zero or a locally publishable partial subtotal.
        with self.assertRaises(DataError) as caught:
            compute_day(payload)
        self.assertEqual(caught.exception.code, "MONEYFLOW_COVERAGE_GAP")

    def test_missing_basic_row_is_distinct_from_a_present_null_pe(self):
        payload = fixture([("10", "1", "2"), (None, None, "3")])
        payload["daily_basic"].pop()
        row = rows_by_uid(compute_day(payload))[SW_A]
        self.assert_unavailable(row, "pe_ttm_median")
        self.assert_unavailable(row, "pb_median")
        self.assertEqual(row["counts"]["basic_received"], 1)
        self.assertEqual(row["metrics"]["flow_cent"]["value"], "500")

    def test_local_membership_unknown_does_not_poison_unrelated_sector(self):
        payload = fixture([("10", "1", "1"), ("20", "2", "2")])
        payload["industries"].append(industry(SW_B))
        payload["memberships"][1]["uid"] = SW_B
        payload["memberships"][1]["state"] = "MEMBERSHIP_BOUNDARY_UNKNOWN"
        payload["memberships"][1]["in_date"] = DAY
        payload["official"] = [{"uid": SW_B, "trade_date": DAY, "pe": "15", "pb": "1.5", "close": "1234.5"}]
        rows = rows_by_uid(compute_day(payload))
        self.assert_decimal(rows[SW_A], "pe_ttm_median", "10")
        self.assertEqual(rows[SW_A]["metrics"]["flow_cent"]["value"], "100")
        self.assert_unavailable(rows[SW_B], "pe_ttm_median")
        self.assert_unavailable(rows[SW_B], "flow_cent")
        self.assert_decimal(rows[SW_B], "official_pe", "15")
        self.assert_decimal(rows[SW_B], "official_pb", "1.5")

    def test_unknown_taxonomy_never_erases_another_taxonomys_valid_row(self):
        payload = fixture([("10", "2", "3")])
        payload["industries"].append(industry(CI_A, "CI"))
        ci_member = dict(payload["memberships"][0], uid=CI_A)
        payload["memberships"].append(ci_member)
        payload["unknown_taxonomies"] = ["CI"]
        rows = rows_by_uid(compute_day(payload))
        self.assertEqual(rows[SW_A]["metrics"]["flow_cent"]["value"], "300")
        self.assert_unavailable(rows[CI_A], "flow_cent")
        self.assert_unavailable(rows[CI_A], "pe_ttm_median")

    def test_ths_multiple_membership_and_cross_taxonomy_values_are_separate(self):
        payload = fixture([("10", "2", "1.23")])
        for uid, taxonomy in [(THS_A, "THS"), (THS_B, "THS"), (TDX_A, "TDX")]:
            payload["industries"].append(industry(uid, taxonomy))
            membership = dict(payload["memberships"][0], uid=uid)
            if taxonomy == "THS":
                membership["evidence_kind"] = "OBSERVED_SAME_DAY"
            payload["memberships"].append(membership)
        rows = rows_by_uid(compute_day(payload))
        self.assertEqual(set(rows), {SW_A, THS_A, THS_B, TDX_A})
        for uid in [SW_A, THS_A, THS_B, TDX_A]:
            self.assertEqual(rows[uid]["counts"]["member_count"], 1)
            self.assertEqual(rows[uid]["metrics"]["flow_cent"]["value"], "123")
        self.assertEqual(rows[THS_A]["membership_evidence_kind"], "OBSERVED_SAME_DAY")

    def test_official_estimates_stay_separate_from_constituent_medians(self):
        payload = fixture([("10", "2", "1"), ("30", "4", "2")])
        payload["official"] = [{"uid": SW_A, "trade_date": DAY, "pe": "12.25", "pb": "1.75", "close": "1000"}]
        row = rows_by_uid(compute_day(payload))[SW_A]
        self.assert_decimal(row, "pe_ttm_median", "20")
        self.assert_decimal(row, "pb_median", "3")
        self.assert_decimal(row, "official_pe", "12.25")
        self.assert_decimal(row, "official_pb", "1.75")

    def test_unpublished_industry_keeps_constituent_statistics(self):
        payload = fixture([("10", "2", "1.25")])
        payload["industries"][0]["is_pub"] = False
        row = rows_by_uid(compute_day(payload))[SW_A]
        self.assert_decimal(row, "pe_ttm_median", "10")
        self.assertEqual(row["metrics"]["flow_cent"]["value"], "125")
        self.assert_unavailable(row, "official_pe")

    def test_input_rows_and_provenance_are_not_mutated(self):
        payload = fixture([("30", "4", "-1.02"), ("10", "2", "4.05")])
        payload["source_refs"] = [{"path": "raw/evidence.json", "sha256": "0" * 64}]
        before = copy.deepcopy(payload)
        result = compute_day(payload)
        self.assertEqual(payload, before)
        self.assertEqual(result["trade_date"], DAY)
        self.assertEqual(result["captured_at"], payload["captured_at"])
        self.assertEqual(result["provider_kind"], "TEST_INJECTED_CLIENT")
        self.assertEqual(result["source_refs"], payload["source_refs"])


if __name__ == "__main__":
    unittest.main()
