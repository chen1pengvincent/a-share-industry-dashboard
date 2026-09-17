import json
import unittest
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

from industry_workbench.provider import TushareProvider
from industry_workbench.transport import (
    SecureTushareClient, ENDPOINT_FIELDS, ROW_LIMITS, RawApiResponse,
    TushareConfigurationError, TushareProtocolError, TushareRedirectError, decode_rows,
)
from industry_workbench.taxonomy import DataError

NOW = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)


class Response(BytesIO):
    status = 200
    headers = {}
    def geturl(self):
        return "https://api.tushare.pro"


class FakeClient:
    def __init__(self, data):
        self.data, self.calls = data, []
    def call(self, api, params, fields):
        self.calls.append((api, dict(params)))
        rows = self.data(api, params)
        payload = {"code": 0, "data": {"fields": list(fields), "items": [[row.get(f) for f in fields] for row in rows]}}
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode()
        return RawApiResponse(payload, raw_bytes=raw, http_status=200, headers={}, api_name=api, attempt_count=1)


def classes():
    return {"L1": [{"src": "SW2021", "level": "L1", "index_code": "801001.SI", "industry_code": "100000", "parent_code": "0", "industry_name": "一级", "is_pub": "1"}],
            "L2": [{"src": "SW2021", "level": "L2", "index_code": "801002.SI", "industry_code": "100100", "parent_code": "100000", "industry_name": "二级", "is_pub": "1"}],
            "L3": [{"src": "SW2021", "level": "L3", "index_code": "801003.SI", "industry_code": "100101", "parent_code": "100100", "industry_name": "三级", "is_pub": "1"}]}


def complete_market(api, params):
    day = params.get("trade_date", "20260917")
    if api == "trade_cal":
        start = datetime.strptime(params["start_date"], "%Y%m%d")
        end = datetime.strptime(params["end_date"], "%Y%m%d")
        return [{"exchange": "SSE", "cal_date": (start + timedelta(days=n)).strftime("%Y%m%d"),
                 "is_open": "1" if (start + timedelta(days=n)).weekday() < 5 else "0",
                 "pretrade_date": (start + timedelta(days=n-1)).strftime("%Y%m%d")}
                for n in range((end-start).days + 1)]
    if api == "stock_basic":
        return [{"ts_code": f"60{n:04d}.SH", "name": f"股票{n}", "market": "主板", "list_status": "L", "list_date": "20200101" if n == 1 else "20270101", "delist_date": ""} for n in range(1, 31)] if params["list_status"] == "L" else []
    if api == "daily":
        return [{"ts_code": "600001.SH", "trade_date": day, "vol": "100", "amount": "200"}]
    if api == "daily_basic":
        return [{"ts_code": "600001.SH", "trade_date": day, "pe_ttm": "13.001", "pb": "1.234"}]
    if api == "moneyflow":
        return [{"ts_code": "600001.SH", "trade_date": day, "net_mf_vol": "-3", "net_mf_amount": "-12.34"}]
    if api == "suspend_d":
        return []
    if api == "index_classify":
        return classes()[params["level"]]
    if api == "index_member_all":
        return [{"ts_code": "600001.SH", "name": "股票1", "l1_code": "801001.SI", "l1_name": "一级", "l2_code": "801002.SI", "l2_name": "二级", "l3_code": "801003.SI", "l3_name": "三级", "in_date": "20200101", "out_date": "", "is_new": "Y"}] if params["is_new"] == "Y" else []
    if api == "sw_daily":
        return [{"ts_code": f"80100{n}.SI", "name": f"{n}级", "trade_date": day, "close": "111.01", "pe": "7.77", "pb": "2.22"} for n in (1, 2, 3)]
    if api == "ci_index_member":
        if params["is_new"] == "N":
            return []
        rows = [{"ts_code": f"60{n:04d}.SH", "name": f"股票{n}", "l1_code": f"CI005{n:03d}.CI", "l1_name": f"一级{n}", "l2_code": f"CI010{n:03d}.CI", "l2_name": f"二级{n}", "l3_code": f"CI020{n:03d}.CI", "l3_name": f"三级{n}", "in_date": "20200101" if n == 1 else "20270101", "out_date": "", "is_new": "Y"} for n in range(1, 31)]
        return [r for r in rows if all(r[k] == v for k, v in params.items())]
    if api == "ci_daily":
        return [{"ts_code": f"CI{prefix}{n:03d}.CI", "trade_date": day, "close": "777.0123456789012345"} for prefix in ("005", "010", "020") for n in range(1, 31)]
    if api == "ths_daily":
        return [{"ts_code": f"881{n:03d}.TI", "trade_date": day, "close": "1234.56789"} for n in range(90)] + [{"ts_code": "885999.TI", "trade_date": day, "close": "999"}]
    if api == "moneyflow_ind_ths":
        return [{"ts_code": f"881{n:03d}.TI", "trade_date": day, "industry": f"行业{n}", "company_num": 1} for n in range(90)]
    if api == "ths_index":
        return [{"ts_code": f"881{n:03d}.TI", "name": f"行业{n}", "count": 1, "exchange": "A", "type": "I"} for n in range(90)]
    if api == "ths_member":
        return [{"ts_code": params["ts_code"], "con_code": "600001.SH", "con_name": "股票1", "in_date": None, "out_date": None, "is_new": "Y"}]
    if api == "tdx_index":
        return [{"ts_code": f"{series}001.TDX", "trade_date": day, "name": f"TDX{series}", "idx_type": "行业板块", "idx_count": 1} for series in (880, 881)]
    if api == "tdx_member":
        return [{"ts_code": params["ts_code"], "trade_date": day, "con_code": "600001.SH", "con_name": "股票1"}]
    if api == "tdx_daily":
        return [{"ts_code": f"{series}001.TDX", "trade_date": day, "close": "120.123", "pe": "10", "pb": "2"} for series in (880, 881)]
    raise AssertionError(api)


class TransportTests(unittest.TestCase):
    def test_moneyflow_uses_inherited_https_and_exact_decimal(self):
        raw = b'{"code":0,"data":{"fields":["net_mf_amount"],"items":[[1234567890.01]]}}'
        seen = []
        def opener(request, timeout):
            seen.append((request.full_url, timeout))
            return Response(raw)
        client = SecureTushareClient("CANARY_FAKE_CREDENTIAL_947", opener=opener)
        response = client.call("moneyflow", {"trade_date": "20260917"}, ["net_mf_amount"])
        self.assertEqual(decode_rows(response, api_name="moneyflow", expected_fields=["net_mf_amount"])[0]["net_mf_amount"], Decimal("1234567890.01"))
        self.assertEqual(seen[0][0], "https://api.tushare.pro")

    def test_no_custom_host_no_unbounded_calls(self):
        with self.assertRaises(TushareConfigurationError):
            SecureTushareClient("CANARY_FAKE_CREDENTIAL_947", base_url="https://evil.invalid")
        for api, params in (("moneyflow", {}), ("ci_index_member", {}), ("moneyflow", {"trade_date": "20260230"}), ("ths_index", {"exchange": "A", "type": "N"})):
            with self.assertRaises(TushareConfigurationError):
                SecureTushareClient._validate_request(api, params, None)

    def test_ci_ths_close_endpoints_are_bounded_and_close_only(self):
        for api, limit in (("ci_daily", 4000), ("ths_daily", 3000)):
            self.assertEqual(SecureTushareClient._validate_request(api, {"trade_date": "20260917"}, None), ("ts_code", "trade_date", "close"))
            self.assertEqual(ROW_LIMITS[api], limit)
            for params, fields in (({}, None), ({"ts_code": "881001.TI"}, None), ({"trade_date": "20260917"}, ["pe"])):
                with self.assertRaises(TushareConfigurationError):
                    SecureTushareClient._validate_request(api, params, fields)

    def test_response_schema_and_duplicate_fields_rejected(self):
        payload = {"code": 0, "data": {"fields": ["ts_code", "ts_code"], "items": []}}
        with self.assertRaises(TushareProtocolError):
            decode_rows(payload, api_name="moneyflow")

    def test_credential_echo_is_blocked_before_persistence(self):
        marker = "CANARY_FAKE_CREDENTIAL_947"
        client = SecureTushareClient(marker, opener=lambda request, timeout: Response(json.dumps({"msg": marker}).encode()))
        with self.assertRaisesRegex(TushareProtocolError, "credential material"):
            client.call("moneyflow", {"trade_date": "20260917"})


class ProviderTests(unittest.TestCase):
    def test_ths_nonindustry_code_namespace_never_expands_catalogue(self):
        industries = [{"uid": "THS:INDUSTRY:881001.TI", "taxonomy": "THS", "code": "881001.TI"}]
        quotes = [{"ts_code": code, "trade_date": "20210917", "close": "12.34"} for code in ("881001.TI", "700052R.TI", "700050B.TI")]
        provider = TushareProvider(FakeClient(lambda api, params: quotes), clock=lambda: NOW)
        result, audit = provider._close_only("THS", industries, "20210917")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["uid"], industries[0]["uid"])
        self.assertEqual(audit["unmapped_quote_count"], 2)
        with self.assertRaisesRegex(DataError, "CATALOG_IDENTITY_INVALID"):
            provider._close_only("THS", [{**industries[0], "code": "700052R.TI"}], "20210917")

    def test_close_only_history_does_not_fetch_members_or_derive_valuation(self):
        industries = [{"uid": "THS:INDUSTRY:881001.TI", "taxonomy": "THS", "code": "881001.TI"}]
        client = FakeClient(lambda api, params: [{"ts_code": "881001.TI", "trade_date": params["trade_date"], "close": "1234.000000000001"}])
        result, audit = TushareProvider(client, clock=lambda: NOW)._close_only("THS", industries, "20200102")
        self.assertEqual(client.calls, [("ths_daily", {"trade_date": "20200102"})])
        self.assertEqual(result, [{"uid": industries[0]["uid"], "trade_date": "20200102", "close": Decimal("1234.000000000001"), "pe": None, "pb": None}])
        self.assertEqual(audit["valid_close_count"], 1)

    def test_close_only_date_code_and_uniqueness_are_validated(self):
        industries = [{"uid": "CI:L1:CI005001.CI", "taxonomy": "CI", "code": "CI005001.CI"}]
        good = {"ts_code": "CI005001.CI", "trade_date": "20260917", "close": "123"}
        cases = [([good, good], "QUOTE_DUPLICATE"), ([{**good, "trade_date": "20260916"}], "DATE_MISMATCH"),
                 ([{**good, "ts_code": "881001.TI"}], "CODE_INVALID"), ([{**good, "close": "NaN"}], "INVALID_FINANCIAL_NUMBER")]
        for rows, expected in cases:
            with self.subTest(expected=expected), self.assertRaisesRegex(DataError, expected):
                TushareProvider(FakeClient(lambda api, params: rows), clock=lambda: NOW)._close_only("CI", industries, "20260917")

    def test_close_only_cap_stops_without_ninety_request_fanout(self):
        for tax, api in (("CI", "ci_daily"), ("THS", "ths_daily")):
            client = FakeClient(lambda name, params: [{}] * ROW_LIMITS[name])
            with self.subTest(tax=tax), self.assertRaisesRegex(DataError, api.upper() + "_ROW_LIMIT"):
                TushareProvider(client, clock=lambda: NOW)._close_only(tax, [], "20260917")
            self.assertEqual(len(client.calls), 1)

    def test_close_only_empty_response_is_explicit_metric_gap(self):
        industries = [{"uid": "CI:L1:CI005001.CI", "taxonomy": "CI", "code": "CI005001.CI"}]
        result, audit = TushareProvider(FakeClient(lambda api, params: []), clock=lambda: NOW)._close_only("CI", industries, "20260917")
        self.assertEqual(result, [])
        self.assertEqual(audit["missing_codes"], ["CI005001.CI"])
        self.assertEqual(audit["valid_close_count"], 0)

    def test_ths_unknown_members_do_not_hide_official_close_or_replace_moneyflow(self):
        def data(api, params):
            rows = complete_market(api, params)
            if api == "ths_member":
                for row in rows:
                    row["con_code"] = "600002.SH"
            return rows
        result = TushareProvider(FakeClient(data), clock=lambda: NOW).fetch_day("20260917")
        self.assertEqual(result["unknown_taxonomies"], ["THS"])
        ths_prices = [r for r in result["official"] if r["uid"].startswith("THS:")]
        self.assertEqual(len(ths_prices), 90)
        self.assertTrue(all(r["close"] == Decimal("1234.56789") and r["pe"] is None and r["pb"] is None for r in ths_prices))
        self.assertEqual(result["moneyflow"], [{"ts_code": "600001.SH", "trade_date": "20260917", "net_mf_vol": "-3", "net_mf_amount": "-12.34"}])

    def test_same_day_readiness_boundary_is_1930_shanghai(self):
        client = FakeClient(complete_market)
        with self.assertRaisesRegex(DataError, "TRADE_DAY_NOT_READY"):
            TushareProvider(client, clock=lambda: datetime(2026, 9, 17, 11, 29, 59, tzinfo=timezone.utc)).fetch_day("20260917")
        self.assertEqual(client.calls, [])
        result = TushareProvider(client, clock=lambda: datetime(2026, 9, 17, 11, 30, tzinfo=timezone.utc)).fetch_day("20260917")
        self.assertEqual(result["trade_date"], "20260917")

    def test_capped_ci_seed_does_not_need_to_contain_every_l1(self):
        def data(api, params):
            rows = complete_market(api, params)
            return rows[:29] if api == "ci_index_member" and params == {"is_new": "Y"} else rows
        with patch.dict(ROW_LIMITS, {"ci_index_member": 29}):
            industries, members, unknown = TushareProvider(FakeClient(data), clock=lambda: NOW)._ci("20260917", [])
        self.assertEqual(len([r for r in industries if r["level"] == "L1"]), 30)
        self.assertFalse(unknown)

    def test_ci_complete_partitions_still_require_all_reviewed_l1(self):
        def data(api, params):
            rows = complete_market(api, params)
            return [r for r in rows if r["l1_code"] != "CI005030.CI"] if api == "ci_index_member" else rows
        with self.assertRaisesRegex(DataError, "CI_L1_CONTRACT_CHANGED"):
            TushareProvider(FakeClient(data), clock=lambda: NOW)._ci("20260917", [])

    def test_ci_seed_unreviewed_l1_fails_before_partitions(self):
        row = {"is_new": "Y", "l1_code": "CI005031.CI"}
        client = FakeClient(lambda api, params: [row])
        with self.assertRaisesRegex(DataError, "CI_L1_CONTRACT_CHANGED"):
            TushareProvider(client, clock=lambda: NOW)._ci("20260917", [])
        self.assertEqual(len(client.calls), 1)

    def test_complete_day_inputs_share_requests_and_preserve_four_structures(self):
        client = FakeClient(complete_market)
        provider = TushareProvider(client, clock=lambda: NOW)
        result = provider.fetch_day("20260917")
        self.assertTrue(result["audit"]["base_complete"])
        self.assertEqual(result["unknown_taxonomies"], [])
        self.assertEqual(result["provider_kind"], "TEST_INJECTED_CLIENT")
        self.assertEqual({r["taxonomy"] for r in result["industries"]}, {"SW", "CI", "THS", "TDX"})
        self.assertEqual(len(result["memberships"]), 3 + 3 + 90 + 2)
        self.assertEqual(len(result["official"]), 3 + 2 + 90 + 90)
        extra = [r for r in result["official"] if r["uid"].startswith(("CI:", "THS:"))]
        self.assertTrue(all(r["close"] > 0 and r["pe"] is None and r["pb"] is None for r in extra))
        self.assertEqual(result["audit"]["classifications"]["THS"]["daily_close"]["unmapped_quote_count"], 1)
        self.assertEqual(result["moneyflow"][0]["net_mf_amount"], "-12.34")
        count = len(client.calls)
        again = provider.fetch_day("20260917")
        self.assertEqual(len(client.calls), count)
        self.assertEqual(result, again)
        self.assertTrue(all("request_key" in r for r in result["source_refs"]))

    def test_full_day_ci_gap_is_unknown_not_zero_or_blocked(self):
        def gap(api, params):
            rows = complete_market(api, params)
            if api == "ci_index_member":
                for row in rows:
                    if row["ts_code"] == "600001.SH":
                        row["in_date"] = "20270101"
            return rows
        result = TushareProvider(FakeClient(gap), clock=lambda: NOW).fetch_day("20260917")
        self.assertEqual(result["unknown_taxonomies"], ["CI"])
        self.assertFalse(result["audit"]["classifications"]["CI"]["membership_complete"])
        ci_prices = [r for r in result["official"] if r["uid"].startswith("CI:")]
        self.assertEqual(len(ci_prices), 90)
        self.assertTrue(all(r["close"] == Decimal("777.0123456789012345") and r["pe"] is None and r["pb"] is None for r in ci_prices))

    def test_dedup_and_independent_round_scope(self):
        client = FakeClient(lambda api, params: [])
        writes = []
        provider = TushareProvider(client, put_raw=lambda m, r: writes.append(m) or {"sha256": m["raw_sha256"]}, clock=lambda: NOW)
        params = {"l1_code": "801001.SI", "is_new": "Y"}
        provider._fetch("index_member_all", params, scope="R1")
        provider._fetch("index_member_all", params, scope="R1")
        provider._fetch("index_member_all", params, scope="R2")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(writes), 2)
        self.assertNotEqual(writes[0]["request_key"], writes[1]["request_key"])

    def test_endpoint_limit_is_not_success(self):
        client = FakeClient(lambda api, params: [{}] * ROW_LIMITS["moneyflow"])
        provider = TushareProvider(client, clock=lambda: NOW)
        with self.assertRaisesRegex(DataError, "ROW_LIMIT"):
            provider._fetch("moneyflow", {"trade_date": "20260917"})

    def test_cached_limit_sample_cannot_be_reused_as_complete(self):
        client = FakeClient(lambda api, params: [{}] * ROW_LIMITS[api])
        provider = TushareProvider(client, clock=lambda: NOW)
        provider._fetch("ci_index_member", {"is_new": "Y"}, allow_limit=True)
        with self.assertRaisesRegex(DataError, "ROW_LIMIT"):
            provider._fetch("ci_index_member", {"is_new": "Y"})
        self.assertEqual(len(client.calls), 1)

    def test_calendar_date_and_exchange_validation(self):
        client = FakeClient(lambda api, params: [{"exchange": "SZSE", "cal_date": "20260917", "is_open": 1, "pretrade_date": "20260916"}])
        with self.assertRaisesRegex(DataError, "SCOPE"):
            TushareProvider(client, clock=lambda: NOW).calendar("20260901", "20260917")

    def test_calendar_requires_closed_days_and_open_days_without_gaps(self):
        for missing in ("20260912", "20260914"):
            def data(api, params):
                return [r for r in complete_market(api, params) if r["cal_date"] != missing]
            with self.subTest(missing=missing), self.assertRaisesRegex(DataError, "CALENDAR_DATES_INCOMPLETE"):
                TushareProvider(FakeClient(data), clock=lambda: NOW).calendar("20260910", "20260917")
        result = TushareProvider(FakeClient(complete_market), clock=lambda: NOW).calendar("20260910", "20260917")
        self.assertEqual(len(result), 8)
        self.assertEqual(next(r for r in result if r["cal_date"] == "20260912")["is_open"], "0")

    def test_ths_history_never_requests_current_members(self):
        data = lambda api, params: [{"trade_date": "20260916", "ts_code": "881001.TI", "industry": "甲", "company_num": 10}]
        client = FakeClient(data)
        output = TushareProvider(client, clock=lambda: NOW)._ths("20260916", "20260917")
        self.assertTrue(output[-1])
        self.assertEqual(output[1], [])
        self.assertEqual([api for api, params in client.calls], ["moneyflow_ind_ths"])

    def test_tdx_structural_historical_absence_is_unknown(self):
        client = FakeClient(lambda api, params: [])
        output = TushareProvider(client, clock=lambda: NOW)._tdx("20220104", "20260917")
        self.assertTrue(output[-1])
        self.assertEqual([api for api, params in client.calls], ["tdx_index"])

    def test_sw_split_requires_parent_rows_in_union(self):
        row = {"l1_code": "801001.SI", "l2_code": "801002.SI", "l3_code": "801003.SI", "is_new": "Y"}
        def data(api, params):
            if "l1_code" in params:
                return [{**row, "ts_code": str(i)} for i in range(ROW_LIMITS[api])]
            return []
        with self.assertRaisesRegex(DataError, "SPLIT_INCOMPLETE"):
            TushareProvider(FakeClient(data), clock=lambda: NOW)._sw_member_round(classes(), 1)

    def test_official_values_remain_distinct(self):
        industries = [{"uid": "SW2021:L1:1", "market_code": "801001.SI"}]
        result = TushareProvider._official(industries, [{"ts_code": "801001.SI", "trade_date": "20260917", "close": "11.01", "pe": "0", "pb": None}], "20260917")
        self.assertEqual(result[0]["close"], Decimal("11.01"))
        self.assertEqual(result[0]["pe"], Decimal(0))
        self.assertIsNone(result[0]["pb"])

    def test_fetch_day_rejects_nontrading_day(self):
        def data(api, params):
            rows = complete_market(api, params)
            for row in rows:
                if row["cal_date"] == "20260917":
                    row["is_open"] = "0"
            return rows
        client = FakeClient(data)
        with self.assertRaisesRegex(DataError, "NOT_A_TRADING_DAY"):
            TushareProvider(client, clock=lambda: NOW).fetch_day("20260917")


if __name__ == "__main__":
    unittest.main()
