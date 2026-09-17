import ast
import unittest
from copy import deepcopy
from pathlib import Path

from industry_workbench.metrics import compute_day
from industry_workbench.models import DataError
from industry_workbench.validation import audit_day


def inputs():
    day = "20260917"
    codes = ("600001.SH", "000001.SZ", "300001.SZ")
    identity = {"uid": "SW2021:L1:100000", "taxonomy": "SW", "version": "SW2021", "level": "L1", "code": "100000",
                "name": "测试行业", "parent_uid": None, "is_pub": True, "market_code": "801001.SI", "membership_code": "801001.SI"}
    return {"trade_date": day, "captured_at": "2026-09-17T20:00:00+08:00", "provider_kind": "TEST_INJECTED_CLIENT",
            "industries": [identity], "unknown_taxonomies": [], "source_refs": [],
            "stocks": [{"ts_code": c, "name": c, "list_date": "20200101", "delist_date": ""} for c in codes],
            "daily": [{"ts_code": c, "trade_date": day, "vol": "100", "amount": "500"} for c in codes],
            "daily_basic": [{"ts_code": c, "trade_date": day, "pe_ttm": pe, "pb": pb} for c, pe, pb in zip(codes, ("2", "3", "9"), ("1", "2", "7"))],
            "moneyflow": [{"ts_code": c, "trade_date": day, "net_mf_amount": money, "net_mf_vol": vol} for c, money, vol in zip(codes, ("1.23", "-0.34", "0"), ("2", "-1", "0"))],
            "memberships": [{"uid": identity["uid"], "ts_code": c, "state": "ACTIVE", "evidence_kind": "OFFICIAL_DATED"} for c in codes],
            "official": [{"uid": identity["uid"], "trade_date": day, "pe": "55", "pb": "4.5", "close": "123.45"}],
            "audit": {"base_complete": True, "eligible_count": 3, "traded_count": 3}}


class IndependentValidationTests(unittest.TestCase):
    def rejected(self, source, result):
        with self.assertRaises(DataError) as caught:
            audit_day(source, result)
        self.assertEqual(caught.exception.code, "INDEPENDENT_RECALCULATION_FAILED")

    def test_minimal_declared_scope_exact_numbers_no_mutation(self):
        source = inputs()
        result = compute_day(source)
        before_source, before_result = deepcopy(source), deepcopy(result)
        audit = audit_day(source, result)
        self.assertEqual(audit["status"], "PASS")
        self.assertEqual(audit["industry_count"], 1)
        self.assertEqual(result["industries"][0]["metrics"]["pe_ttm_median"]["value"], "3")
        self.assertEqual(result["industries"][0]["metrics"]["flow_cent"]["value"], "89")
        self.assertEqual(source, before_source)
        self.assertEqual(result, before_result)

    def test_mean_substitution_and_official_median_swap_fail(self):
        source = inputs()
        for wrong in ("4.6666666666666666666666666667", "55"):
            result = compute_day(source)
            result["industries"][0]["metrics"]["pe_ttm_median"]["value"] = wrong
            self.rejected(source, result)

    def test_one_cent_beyond_js_safe_integer_is_detected(self):
        source = inputs()
        source["moneyflow"][0]["net_mf_amount"] = "900719925474099.93"
        result = compute_day(source)
        self.assertEqual(audit_day(source, result)["status"], "PASS")
        flow = result["industries"][0]["metrics"]["flow_cent"]
        flow["value"] = str(int(flow["value"]) + 1)
        self.rejected(source, result)

    def test_counts_small_sample_status_and_metric_dates_are_checked(self):
        source = inputs()
        for mutation in ("count", "status", "date"):
            result = compute_day(source)
            row = result["industries"][0]
            if mutation == "count":
                row["counts"]["pe_valid"] = 99
            elif mutation == "status":
                row["metrics"]["pe_ttm_median"]["status"] = "OK"
            else:
                row["metrics"]["pe_ttm_median"]["metric_date"] = "20260916"
            self.rejected(source, result)

    def test_declared_structural_unknown_can_have_no_catalogue(self):
        source = inputs()
        source["unknown_taxonomies"] = ["THS", "TDX"]
        self.assertEqual(audit_day(source, compute_day(source))["status"], "PASS")
        source["unknown_taxonomies"] = ["SW", "THS", "TDX"]
        result = compute_day(source)
        self.assertEqual(audit_day(source, result)["status"], "PASS")
        result["industries"][0]["metrics"]["flow_cent"].update(value="0", status="OK", reason_codes=[])
        self.rejected(source, result)

    def test_localized_boundary_remains_na_without_new_global_gate(self):
        source = inputs()
        source["memberships"][0]["state"] = "MEMBERSHIP_BOUNDARY_UNKNOWN"
        result = compute_day(source)
        self.assertEqual(audit_day(source, result)["status"], "PASS")
        self.assertIsNone(result["industries"][0]["metrics"]["flow_cent"]["value"])

    def test_suspended_missing_basic_is_na_while_money_remains_calculable(self):
        source = inputs()
        for field in ("daily", "daily_basic", "moneyflow"):
            source[field] = source[field][1:]
        source["audit"]["traded_count"] = 2
        result = compute_day(source)
        self.assertEqual(audit_day(source, result)["status"], "PASS")
        self.assertIsNone(result["industries"][0]["metrics"]["pe_ttm_median"]["value"])
        self.assertEqual(result["industries"][0]["metrics"]["flow_cent"]["value"], "-34")

    def test_member_amount_ranking_and_market_conservation_are_independent(self):
        source = inputs()
        for change in ("member", "rank", "market"):
            result = compute_day(source)
            if change == "member":
                result["members"][0]["flow_cent"] = "888"
            elif change == "rank":
                result["industries"][0]["flow_rank"] = 2
            else:
                result["audit"]["market_flow_cent"] = "90"
            self.rejected(source, result)

    def test_duplicate_identity_and_nonfinite_value_fail_closed(self):
        source = inputs()
        result = compute_day(source)
        result["members"].append(deepcopy(result["members"][0]))
        self.rejected(source, result)
        result = compute_day(source)
        result["industries"][0]["metrics"]["pb_median"]["value"] = "NaN"
        self.rejected(source, result)

    def test_peer_percentile_ties_and_na_must_remain_null(self):
        source = inputs()
        for code, pe, pb in (("600002.SH", "3", "2"), ("000002.SZ", "20", "15")):
            source["stocks"].append({"ts_code": code, "name": code, "list_date": "20200101", "delist_date": ""})
            source["daily"].append({"ts_code": code, "trade_date": "20260917", "vol": "1", "amount": "1"})
            source["daily_basic"].append({"ts_code": code, "trade_date": "20260917", "pe_ttm": pe, "pb": pb})
            source["moneyflow"].append({"ts_code": code, "trade_date": "20260917", "net_mf_amount": "0", "net_mf_vol": "0"})
            source["memberships"].append({"uid": "SW2021:L1:100000", "ts_code": code, "state": "ACTIVE", "evidence_kind": "OFFICIAL_DATED"})
        source["audit"].update(eligible_count=5, traded_count=5)
        result = compute_day(source)
        self.assertEqual(audit_day(source, result)["status"], "PASS")
        member = next(m for m in result["members"] if m["ts_code"] == "600002.SH")
        self.assertEqual(member["pe_peer_percentile"], "60")
        member["pe_peer_percentile"] = "40"
        self.rejected(source, result)
        result = compute_day(source)
        result["members"][0]["total_mv"] = ""
        self.rejected(source, result)

    def test_module_does_not_import_tested_financial_functions_or_io(self):
        path = Path(__file__).parents[1] / "src" / "industry_workbench" / "validation.py"
        tree = ast.parse(path.read_text())
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.extend((node.module or "") + "." + alias.name for alias in node.names)
        self.assertFalse(any(name.startswith(("pathlib", "os", "urllib", "industry_workbench.metrics", "metrics", "provider", "storage", "taxonomy")) for name in imported))
        self.assertFalse(any(name.endswith(("compute_day", "_median", "rank_flows", "money_cent", "decimal_value")) for name in imported))


if __name__ == "__main__":
    unittest.main()
