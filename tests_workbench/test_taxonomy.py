import unittest
from copy import deepcopy
from decimal import Decimal

from industry_workbench.taxonomy import (
    DataError, active_daily, audit_ci_gap, audit_stock_day, ci_industries,
    flat_industries, flat_memberships, flow_cent, normalize_stocks,
    sw_industries, tree_memberships,
)

DAY = "20260917"


def member(code="600001.SH", *, leaf="CI003.CI", start="20200101", end="", state="Y"):
    return {"l1_code": "CI001.CI", "l1_name": "一级", "l2_code": "CI002.CI", "l2_name": "二级",
            "l3_code": leaf, "l3_name": "三级" if leaf == "CI003.CI" else "其他三级", "ts_code": code,
            "name": "测试股票", "in_date": start, "out_date": end, "is_new": state}


def stock_inputs():
    stocks = [{"ts_code": "600001.SH", "name": "甲", "market": "主板", "list_status": "L", "list_date": "20200101", "delist_date": ""},
              {"ts_code": "000001.SZ", "name": "乙", "market": "主板", "list_status": "L", "list_date": "20200101", "delist_date": ""}]
    daily = [{"ts_code": r["ts_code"], "trade_date": DAY, "vol": "100", "amount": "200"} for r in stocks]
    basic = [{"ts_code": r["ts_code"], "trade_date": DAY, "pe_ttm": "12.34", "pb": "1.2"} for r in stocks]
    flow = [{"ts_code": r["ts_code"], "trade_date": DAY, "net_mf_amount": value, "net_mf_vol": "10"} for r, value in zip(stocks, ("100.01", "-100.00"))]
    return dict(trade_date=DAY, stocks=stocks, daily=daily, daily_basic=basic,
                moneyflow=flow, suspensions=[], prior_active_counts=[2, 2, 2])


class StockUniverseTests(unittest.TestCase):
    def test_integer_precision_and_cancellation(self):
        self.assertEqual(flow_cent("-0.01"), -1)
        self.assertEqual(flow_cent(Decimal("100000000000.01")), 10000000000001)
        for bad in ("0.001", "NaN", "Infinity", None, True):
            with self.assertRaises(DataError):
                flow_cent(bad)

    def test_base_universe_passes(self):
        audit = audit_stock_day(**stock_inputs())
        self.assertTrue(audit["base_complete"])
        self.assertEqual(audit["traded_count"], 2)

    def test_synchronized_two_tables_missing_same_stock_rejected(self):
        args = stock_inputs(); args["daily"].pop(); args["moneyflow"].pop()
        with self.assertRaisesRegex(DataError, "INDEPENDENT_UNIVERSE"):
            audit_stock_day(**args)

    def test_intraday_suspension_is_not_exemption(self):
        args = stock_inputs(); args["daily"].pop(); args["moneyflow"].pop()
        args["suspensions"] = [{"ts_code": "000001.SZ", "trade_date": DAY, "suspend_type": "S", "suspend_timing": "09:30-10:00"}]
        with self.assertRaisesRegex(DataError, "INDEPENDENT_UNIVERSE"):
            audit_stock_day(**args)
        args["suspensions"][0]["suspend_timing"] = "全天"
        args["prior_active_counts"] = [1, 1, 1]
        self.assertTrue(audit_stock_day(**args)["base_complete"])

    def test_moneyflow_and_basic_holes_fail(self):
        for field, reason in (("moneyflow", "MONEYFLOW_COVERAGE_GAP"), ("daily_basic", "DAILY_BASIC_COVERAGE_GAP")):
            args = stock_inputs(); args[field].pop()
            with self.assertRaisesRegex(DataError, reason):
                audit_stock_day(**args)

    def test_legal_blank_negative_valuation_not_bottom_table_hole(self):
        args = stock_inputs(); args["daily_basic"][0].update(pe_ttm=None, pb="-1")
        self.assertTrue(audit_stock_day(**args)["base_complete"])

    def test_duplicate_date_and_nonfinite_detected(self):
        args = stock_inputs(); args["moneyflow"].append(args["moneyflow"][0])
        with self.assertRaisesRegex(DataError, "DUPLICATE"):
            audit_stock_day(**args)
        args = stock_inputs(); args["daily"][0]["amount"] = "Infinity"
        with self.assertRaises(DataError):
            audit_stock_day(**args)

    def test_lifecycle_filters_bj_and_b_shares(self):
        rows = stock_inputs()["stocks"]
        for code in ("920001.BJ", "900001.SH", "200001.SZ"):
            rows.append({**rows[0], "ts_code": code})
        self.assertEqual(len(normalize_stocks(rows)), 2)
        rows.append(dict(rows[0]))
        with self.assertRaisesRegex(DataError, "IDENTITY"):
            normalize_stocks(rows)

    def test_baseline_partial_snapshot_rejected(self):
        args = stock_inputs(); args["prior_active_counts"] = [3, 3, 3]
        with self.assertRaisesRegex(DataError, "SYNCHRONIZED"):
            audit_stock_day(**args)


class TaxonomyTests(unittest.TestCase):
    def test_tree_expands_once_to_each_level(self):
        raw = [member()]
        memberships = tree_memberships(raw, ci_industries(raw), DAY, taxonomy="CI")
        self.assertEqual(len(memberships), 3)
        self.assertEqual({r["state"] for r in memberships}, {"ACTIVE"})

    def test_membership_boundary_remains_unknown(self):
        raw = [member(start=DAY)]
        result = tree_memberships(raw, ci_industries(raw), DAY, taxonomy="CI")
        self.assertEqual({r["state"] for r in result}, {"MEMBERSHIP_BOUNDARY_UNKNOWN"})

    def test_closed_episode_supersedes_stale_open_exact_episode(self):
        raw = [member(), member(end="20240101", state="N")]
        self.assertEqual(tree_memberships(raw, ci_industries(raw), DAY, taxonomy="CI"), [])
        raw.append(member(end="20250101", state="N"))
        with self.assertRaisesRegex(DataError, "EPISODE_CONFLICT"):
            tree_memberships(raw, ci_industries(raw), DAY, taxonomy="CI")

    def test_overlap_is_unknown_not_arbitrarily_deduplicated(self):
        raw = [member(), member(leaf="CI004.CI")]
        result = tree_memberships(raw, ci_industries(raw), DAY, taxonomy="CI")
        self.assertEqual({r["state"] for r in result}, {"MEMBERSHIP_OVERLAP_UNKNOWN"})

    def test_missing_ci_never_claims_complete_even_legacy_tolerates(self):
        flows = [{"ts_code": f"60{i:04d}.SH", "net_mf_amount": "0"} for i in range(1000)]
        members = [{"ts_code": r["ts_code"]} for r in flows[1:]]
        audit = audit_ci_gap(members, flows)
        self.assertTrue(audit["legacy_threshold_pass"])
        self.assertFalse(audit["membership_complete"])
        self.assertEqual(audit["official_abs_amount_coverage"], "1")

    def test_ci_cancelled_missing_amount_is_visible(self):
        flows = [{"ts_code": "600001.SH", "net_mf_amount": "100"}, {"ts_code": "600002.SH", "net_mf_amount": "-100"}]
        audit = audit_ci_gap([], flows)
        self.assertEqual(audit["unclassified_flow_cent"], "0")
        self.assertEqual(audit["unclassified_abs_flow_cent"], "20000")
        self.assertFalse(audit["membership_complete"])

    def test_ths_retains_overlap_but_rejects_historical_capture(self):
        catalog = [{"ts_code": code, "trade_date": DAY, "industry": code} for code in ("881001.TI", "881002.TI")]
        industries = flat_industries(catalog, taxonomy="THS", trade_date=DAY)
        rows = [{"ts_code": r["ts_code"], "con_code": "600001.SH", "is_new": "Y"} for r in catalog]
        result = flat_memberships(rows, industries, taxonomy="THS", trade_date=DAY, captured_date=DAY)
        self.assertEqual(len(result), 2)
        self.assertEqual({r["state"] for r in result}, {"ACTIVE"})
        with self.assertRaisesRegex(DataError, "HISTORICAL"):
            flat_memberships(rows, industries, taxonomy="THS", trade_date=DAY, captured_date="20260918")

    def test_tdx_two_series_are_not_parent_child(self):
        catalog = [{"ts_code": code, "trade_date": DAY, "idx_type": "行业板块", "name": code} for code in ("880001.TDX", "881001.TDX", "881002.TDX")]
        industries = flat_industries(catalog, taxonomy="TDX", trade_date=DAY)
        self.assertEqual({r["parent_uid"] for r in industries}, {None})
        rows = [{"ts_code": r["ts_code"], "trade_date": DAY, "con_code": "600001.SH"} for r in catalog]
        result = flat_memberships(rows, industries, taxonomy="TDX", trade_date=DAY, captured_date=DAY)
        self.assertEqual(result[0]["state"], "ACTIVE")
        self.assertEqual({r["state"] for r in result[1:]}, {"MEMBERSHIP_OVERLAP_UNKNOWN"})

    def test_identity_cannot_be_joined_by_name(self):
        raw = [member()]; industries = ci_industries(raw)
        raw[0]["l2_name"] = "同名伪造"
        with self.assertRaisesRegex(DataError, "IDENTITY"):
            tree_memberships(raw, industries, DAY, taxonomy="CI")


if __name__ == "__main__":
    unittest.main()
