"""One read-only Tushare provider; the injected store owns all persistence."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import json
import re
import time
from zoneinfo import ZoneInfo

from .transport import SecureTushareClient, ENDPOINT_FIELDS, ROW_LIMITS, decode_rows
from .taxonomy import (
    DataError, active_daily, audit_ci_gap, audit_stock_day, ci_industries, date,
    dated_rows, decimal, fail, flat_industries, flat_memberships, is_a_share,
    normalize_stocks, sw_industries, text, tree_memberships, unique,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _rows_key(rows):
    return {json.dumps(row, sort_keys=True, ensure_ascii=False, default=str) for row in rows}


class TushareProvider:
    def __init__(self, client=None, *, put_raw=None, clock=None, progress=None):
        self.provider_kind = "LIVE_SECURE_TUSHARE" if client is None else "TEST_INJECTED_CLIENT"
        self.client = SecureTushareClient() if client is None else client
        self.put_raw = put_raw
        self.clock = clock or (lambda: datetime.now(SHANGHAI))
        self.progress = progress or (lambda *args: None)
        self._cache = {}
        self._refs = {}
        self._scope = ""
        self._request_count = 0
        self._last_request = None

    def _now(self):
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            fail("CLOCK_REQUIRES_AWARE_DATETIME")
        return now.astimezone(SHANGHAI)

    def _fetch(self, api, params, *, scope=None, allow_limit=False):
        # References without an endpoint date are scoped to the capture day,
        # including explicit independent member-read rounds.
        scope = scope or ("dated" if "trade_date" in params else self._scope)
        key = json.dumps({"api": api, "params": params, "fields": ENDPOINT_FIELDS[api], "schema": "workbench-endpoints-v1", "scope": scope}, sort_keys=True, ensure_ascii=False)
        if key in self._cache:
            rows, reference = self._cache[key]
            # A capped response is reusable only as a partitioning sample. A
            # later caller demanding completeness must not inherit allow_limit.
            if len(rows) >= ROW_LIMITS[api] and not allow_limit:
                fail("ENDPOINT_ROW_LIMIT")
            if reference is not None:
                self._refs[key] = reference
            return deepcopy(rows)
        if self.provider_kind == "LIVE_SECURE_TUSHARE" and self._last_request is not None:
            # THS permits 200/minute; share one conservative gate across APIs.
            delay = 0.35 - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
        self._last_request = time.monotonic()
        response = self.client.call(api, params, ENDPOINT_FIELDS[api])
        raw = getattr(response, "raw_bytes", None)
        if not isinstance(raw, bytes):
            fail("RAW_RESPONSE_BYTES_REQUIRED")
        metadata = {"api_name": api, "params": dict(params), "fields": list(ENDPOINT_FIELDS[api]),
                    "request_key": sha256(key.encode()).hexdigest(), "raw_sha256": sha256(raw).hexdigest(),
                    "captured_at": self._now().isoformat(), "provider_kind": self.provider_kind,
                    "attempt_count": getattr(response, "attempt_count", 1), "scope": scope,
                    "row_limit": ROW_LIMITS[api]}
        reference = self.put_raw(metadata, raw) if self.put_raw else metadata
        self._refs[key] = reference
        rows = decode_rows(response, api_name=api, expected_fields=ENDPOINT_FIELDS[api])
        if len(rows) > ROW_LIMITS[api] or (len(rows) == ROW_LIMITS[api] and not allow_limit):
            fail("ENDPOINT_ROW_LIMIT")
        self._cache[key] = (deepcopy(rows), reference)
        self._request_count += 1
        self.progress("FETCH", self._request_count, 0, api)
        return rows

    def calendar(self, start_date, end_date):
        date(start_date); date(end_date)
        if start_date > end_date:
            fail("CALENDAR_RANGE_INVALID")
        self._scope = self._now().strftime("%Y%m%d")
        rows = self._fetch("trade_cal", {"exchange": "SSE", "start_date": start_date, "end_date": end_date})
        unique(rows, ("cal_date",), code="CALENDAR_DUPLICATE")
        for row in rows:
            day = date(row.get("cal_date"))
            if not start_date <= day <= end_date or str(row.get("is_open")) not in {"0", "1"} or row.get("exchange") != "SSE":
                fail("CALENDAR_SCOPE_MISMATCH")
        if not rows:
            fail("CALENDAR_EMPTY")
        start = datetime.strptime(start_date, "%Y%m%d")
        end = datetime.strptime(end_date, "%Y%m%d")
        expected = {(start + timedelta(days=offset)).strftime("%Y%m%d") for offset in range((end - start).days + 1)}
        if {row["cal_date"] for row in rows} != expected:
            fail("CALENDAR_DATES_INCOMPLETE")
        return sorted(rows, key=lambda r: r["cal_date"])

    def _lifecycle(self):
        rows = []
        for state in ("L", "D", "P"):
            part = self._fetch("stock_basic", {"list_status": state})
            if any(row.get("list_status") != state for row in part):
                fail("LIFECYCLE_QUERY_SCOPE_MISMATCH")
            rows.extend(part)
        return normalize_stocks(rows)

    def _sw_member_round(self, classes, round_number):
        scope = f"{self._scope}:SW:R{round_number}"

        def query(level, code, state):
            selector = f"{level.lower()}_code"
            rows = self._fetch("index_member_all", {selector: code, "is_new": state}, scope=scope, allow_limit=True)
            if any(r.get(selector) != code or r.get("is_new") != state for r in rows):
                fail("SW_MEMBER_QUERY_SCOPE_MISMATCH")
            return rows

        def recurse(item, level, state):
            rows = query(level, item["index_code"], state)
            if len(rows) < ROW_LIMITS["index_member_all"]:
                return rows
            if level == "L3":
                fail("SW_LEAF_ROW_LIMIT")
            child_level = {"L1": "L2", "L2": "L3"}[level]
            children = [r for r in classes[child_level] if r["parent_code"] == item["industry_code"]]
            if not children:
                fail("SW_SPLIT_CHILDREN_MISSING")
            pieces = [r for child in children for r in recurse(child, child_level, state)]
            if not _rows_key(rows) <= _rows_key(pieces):
                fail("SW_SPLIT_INCOMPLETE")
            return pieces

        return [row for state in ("Y", "N") for item in classes["L1"] for row in recurse(item, "L1", state)]

    def _sw(self, trade_date):
        if trade_date < "20211213":
            return [], [], [], {"history_state": "BEFORE_SW2021_FLOOR"}, True
        classes = {level: self._fetch("index_classify", {"level": level, "src": "SW2021"}) for level in ("L1", "L2", "L3")}
        rounds = {n: self._sw_member_round(classes, n) for n in (1, 2)}
        if _rows_key(rounds[1]) != _rows_key(rounds[2]):
            fail("SW_MEMBERSHIP_SNAPSHOT_CHANGED")
        quotes = self._fetch("sw_daily", {"trade_date": trade_date})
        dated_rows(quotes, trade_date)
        identity = None
        if any(str(r.get("industry_code")) == "230501" for r in classes["L3"]):
            from swivd.v2_identity import SPECIAL_STEEL_CANDIDATE_CODES, resolve_quote_identity, resolve_membership_identity
            histories = {code: self._fetch("sw_daily", {"ts_code": code, "start_date": trade_date, "end_date": trade_date}) for code in SPECIAL_STEEL_CANDIDATE_CODES}
            quote_resolution = resolve_quote_identity([r for level in classes.values() for r in level], quotes, histories, [trade_date], target_trade_date=trade_date)
            candidate_rounds = {n: {code: {state: self._fetch("index_member_all", {"l3_code": code, "is_new": state}, scope=f"{self._scope}:SW:identity:R{n}") for state in ("Y", "N")} for code in SPECIAL_STEEL_CANDIDATE_CODES} for n in (1, 2)}
            l1_rounds = {n: {state: [r for r in rounds[n] if r["is_new"] == state] for state in ("Y", "N")} for n in (1, 2)}
            identity = resolve_membership_identity(quote_resolution, candidate_rounds, l1_rounds, membership_as_of=True)
        industries = sw_industries(classes, identity=identity)
        memberships = tree_memberships(rounds[1], industries, trade_date, taxonomy="SW", identity=identity)
        official = self._official(industries, quotes, trade_date)
        return industries, memberships, official, {"member_rounds": 2, "identity": identity.to_dict() if identity else {"state": "DIRECT"}}, not bool(memberships)

    def _ci(self, trade_date, stocks):
        # The fixed 30 L1 codes are the reviewed CI contract. Seed validation
        # detects an upstream taxonomy change before any amounts are assigned.
        expected = {f"CI005{n:03d}.CI" for n in range(1, 31)}
        seed = self._fetch("ci_index_member", {"is_new": "Y"}, allow_limit=True)
        if any(r.get("is_new") != "Y" for r in seed):
            fail("CI_SEED_SCOPE_MISMATCH")
        # A response at the API cap is only a sample: absence from that sample
        # cannot prove that an L1 category disappeared. Extra codes still prove
        # contract drift immediately; completeness is checked on all partitions.
        if not {r.get("l1_code") for r in seed} <= expected:
            fail("CI_L1_CONTRACT_CHANGED")
        rows = []
        for state in ("Y", "N"):
            for code in sorted(expected):
                part = self._fetch("ci_index_member", {"l1_code": code, "is_new": state}, allow_limit=True)
                if any(r.get("l1_code") != code or r.get("is_new") != state for r in part):
                    fail("CI_MEMBER_QUERY_SCOPE_MISMATCH")
                if len(part) == ROW_LIMITS["ci_index_member"]:
                    # A truncated sample cannot discover all historical child
                    # industries. Partition by the independent stock universe.
                    pieces = []
                    for stock in stocks:
                        single = self._fetch("ci_index_member", {"ts_code": stock["ts_code"], "is_new": state})
                        if any(r.get("ts_code") != stock["ts_code"] or r.get("is_new") != state for r in single):
                            fail("CI_STOCK_QUERY_SCOPE_MISMATCH")
                        pieces.extend(r for r in single if r.get("l1_code") == code)
                    sample_a = [r for r in part if is_a_share(r.get("ts_code"))]
                    if not _rows_key(sample_a) <= _rows_key(pieces):
                        fail("CI_SPLIT_INCOMPLETE")
                    part = pieces
                rows.extend(part)
        if {r.get("l1_code") for r in rows} != expected:
            fail("CI_L1_CONTRACT_CHANGED")
        if not _rows_key([r for r in seed if is_a_share(r.get("ts_code"))]) <= _rows_key(rows):
            fail("CI_SEED_NOT_COVERED")
        industries = ci_industries(rows)
        members = tree_memberships(rows, industries, trade_date, taxonomy="CI")
        return industries, members, not bool(members)

    def _ths(self, trade_date, captured_date):
        catalog = self._fetch("moneyflow_ind_ths", {"trade_date": trade_date})
        industries = flat_industries(catalog, taxonomy="THS", trade_date=trade_date)
        if trade_date != captured_date:
            return industries, [], {"membership_state": "HISTORY_UNAVAILABLE", "observed_date": None}, True
        if len(catalog) != 90:
            fail("THS_CATALOG_COUNT_CHANGED")
        index = self._fetch("ths_index", {"exchange": "A", "type": "I"})
        unique(index, ("ts_code",))
        by_code = {r["ts_code"]: r for r in index}
        if any(r["ts_code"] not in by_code or r["industry"] != by_code[r["ts_code"]]["name"] for r in catalog):
            fail("THS_CATALOG_IDENTITY_CONFLICT")
        rows, count_mismatches = [], []
        for item in catalog:
            part = self._fetch("ths_member", {"ts_code": item["ts_code"]}, scope=f"{captured_date}:THS")
            if not part or any(r.get("ts_code") != item["ts_code"] for r in part):
                fail("THS_MEMBER_SCOPE_MISMATCH")
            if item.get("company_num") is not None and int(item["company_num"]) != len(part):
                count_mismatches.append(item["ts_code"])
            rows.extend(part)
        members = flat_memberships(rows, industries, taxonomy="THS", trade_date=trade_date, captured_date=captured_date)
        return industries, members, {"membership_state": "OBSERVED_SAME_DAY", "observed_date": captured_date, "directory_count_mismatches": count_mismatches}, False

    def _tdx(self, trade_date, captured_date):
        catalog = self._fetch("tdx_index", {"trade_date": trade_date, "idx_type": "行业板块"})
        industries = flat_industries(catalog, taxonomy="TDX", trade_date=trade_date)
        if not catalog:
            return [], [], [], {"membership_state": "HISTORY_UNAVAILABLE"}, True
        rows = []
        for item in catalog:
            part = self._fetch("tdx_member", {"trade_date": trade_date, "ts_code": item["ts_code"]})
            if any(r.get("ts_code") != item["ts_code"] for r in part):
                fail("TDX_MEMBER_SCOPE_MISMATCH")
            if decimal(item.get("idx_count")) != len(part):
                fail("TDX_MEMBER_COUNT_MISMATCH")
            rows.extend(part)
        members = flat_memberships(rows, industries, taxonomy="TDX", trade_date=trade_date, captured_date=captured_date)
        quotes = self._fetch("tdx_daily", {"trade_date": trade_date}, allow_limit=True)
        if len(quotes) == ROW_LIMITS["tdx_daily"]:
            parts = [r for item in catalog for r in self._fetch("tdx_daily", {"trade_date": trade_date, "ts_code": item["ts_code"]})]
            wanted = {r["ts_code"] for r in catalog}
            if not _rows_key([r for r in quotes if r.get("ts_code") in wanted]) <= _rows_key(parts):
                fail("TDX_QUOTES_SPLIT_INCOMPLETE")
            quotes = parts
        dated_rows(quotes, trade_date)
        return industries, members, self._official(industries, quotes, trade_date), {"membership_state": "OFFICIAL_DATED"}, False

    @staticmethod
    def _official(industries, quotes, trade_date):
        unique(quotes, ("ts_code", "trade_date"), code="OFFICIAL_QUOTE_DUPLICATE")
        by_code = {r["ts_code"]: r for r in quotes}
        result = []
        for item in industries:
            row = by_code.get(item["market_code"])
            if row is not None:
                if row.get("trade_date") != trade_date:
                    fail("OFFICIAL_QUOTE_DATE_MISMATCH")
                # Legal empty or non-positive provider ratios stay separate;
                # the domain decides whether each metric is displayable.
                result.append({"uid": item["uid"], "trade_date": trade_date,
                               **{k: decimal(row.get(k), nullable=True) for k in ("close", "pe", "pb")}})
        return result

    def _close_only(self, taxonomy, industries, trade_date):
        """One daily cross-section, joined only to the dated taxonomy catalogue.

        THS includes concept/region/style quotes too; those cannot expand the
        authenticated industry universe. A capped response is never treated as
        complete and this path does not silently fan out to 90 stock-code calls.
        """
        api = {"CI": "ci_daily", "THS": "ths_daily"}[taxonomy]
        try:
            quotes = self._fetch(api, {"trade_date": trade_date})
        except DataError as error:
            if error.code == "ENDPOINT_ROW_LIMIT":
                fail(api.upper() + "_ROW_LIMIT")
            raise
        unique(quotes, ("ts_code", "trade_date"), code=api.upper() + "_QUOTE_DUPLICATE")
        unique(industries, ("code",), code=api.upper() + "_CATALOG_CODE_DUPLICATE")
        pattern = r"CI\d{6}\.CI" if taxonomy == "CI" else r"\d{6}\.TI"
        # THS's full cross-section contains non-industry codes such as
        # 700052R.TI and 700050B.TI. Its documented ts_code is a string, not the
        # six-digit industry identifier. Validate the bounded namespace here;
        # only exact matches to the stricter catalogue can become industry data.
        quote_pattern = pattern if taxonomy == "CI" else r"[0-9A-Z]{1,20}\.TI"
        parsed_closes = {}
        for quote in quotes:
            if quote.get("trade_date") != trade_date:
                fail(api.upper() + "_QUOTE_DATE_MISMATCH")
            if not isinstance(quote.get("ts_code"), str) or not re.fullmatch(quote_pattern, quote["ts_code"]):
                fail(api.upper() + "_QUOTE_CODE_INVALID")
            parsed_closes[quote["ts_code"]] = decimal(quote.get("close"), nullable=True)
        for item in industries:
            if item.get("taxonomy") != taxonomy or not re.fullmatch(pattern, item.get("code", "")):
                fail(api.upper() + "_CATALOG_IDENTITY_INVALID")
        by_code = {row["ts_code"]: row for row in quotes}
        result, missing, invalid = [], [], []
        for item in industries:
            row = by_code.get(item["code"])
            if row is None:
                missing.append(item["code"])
                continue
            close = parsed_closes[item["code"]]
            if close is None or close <= 0:
                invalid.append(item["code"])
            result.append({"uid": item["uid"], "trade_date": trade_date, "close": close, "pe": None, "pb": None})
        return result, {"api_name": api, "trade_date": trade_date, "requested_fields": ["ts_code", "trade_date", "close"],
                        "request_count": 1, "row_limit": ROW_LIMITS[api], "response_count": len(quotes),
                        "expected_count": len(industries), "received_count": len(result),
                        "valid_close_count": len(result) - len(invalid), "missing_codes": sorted(missing),
                        "nonpositive_or_null_close_codes": sorted(invalid), "unmapped_quote_count": len(quotes) - len(result)}

    def fetch_day(self, trade_date):
        date(trade_date)
        now = self._now()
        captured_date = now.strftime("%Y%m%d")
        if trade_date > captured_date or (trade_date == captured_date and (now.hour, now.minute) < (19, 30)):
            fail("TRADE_DAY_NOT_READY")
        self._scope = captured_date
        self._refs = {}
        begin = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=35)).strftime("%Y%m%d")
        calendar = self.calendar(begin, trade_date)
        opened = sorted(r["cal_date"] for r in calendar if str(r["is_open"]) == "1")
        if trade_date not in opened:
            fail("NOT_A_TRADING_DAY")
        prior_days = [day for day in opened if day < trade_date][-3:]
        if len(prior_days) != 3:
            fail("PRIOR_ACTIVITY_BASELINE_MISSING")
        stocks = self._lifecycle()
        daily = self._fetch("daily", {"trade_date": trade_date})
        basic = self._fetch("daily_basic", {"trade_date": trade_date})
        flow = self._fetch("moneyflow", {"trade_date": trade_date})
        suspended = self._fetch("suspend_d", {"trade_date": trade_date, "suspend_type": "S"})
        prior_counts = [len(active_daily(self._fetch("daily", {"trade_date": day}), day)) for day in prior_days]
        audit = audit_stock_day(trade_date=trade_date, stocks=stocks, daily=daily, daily_basic=basic,
                                moneyflow=flow, suspensions=suspended, prior_active_counts=prior_counts)
        industries, members, official, unknowns = [], [], [], []
        sw_i, sw_m, sw_o, sw_a, sw_unknown = self._sw(trade_date)
        ci_i, ci_m, ci_unknown = self._ci(trade_date, stocks)
        ths_i, ths_m, ths_a, ths_unknown = self._ths(trade_date, captured_date)
        ci_o, ci_quote_audit = self._close_only("CI", ci_i, trade_date)
        ths_o, ths_quote_audit = self._close_only("THS", ths_i, trade_date)
        ths_a["daily_close"] = ths_quote_audit
        tdx_i, tdx_m, tdx_o, tdx_a, tdx_unknown = self._tdx(trade_date, captured_date)
        for tax, i, m, is_unknown in (("SW", sw_i, sw_m, sw_unknown), ("CI", ci_i, ci_m, ci_unknown), ("THS", ths_i, ths_m, ths_unknown), ("TDX", tdx_i, tdx_m, tdx_unknown)):
            industries.extend(i); members.extend(m)
            if is_unknown:
                unknowns.append(tax)
        traded = {r["ts_code"] for r in active_daily(daily, trade_date)}
        for tax, m in (("SW", sw_m), ("THS", ths_m), ("CI", ci_m)):
            if tax not in unknowns and traded - {r["ts_code"] for r in m}:
                # The missing assignments have no bounded destination. Keep the
                # entire taxonomy visibly incomplete rather than fill with zero.
                unknowns.append(tax)
        for series in ("880", "881"):
            if "TDX" not in unknowns and traded - {r["ts_code"] for r in tdx_m if r["uid"].startswith(f"TDX:{series}:")}:
                unknowns.append("TDX")
        audit["classifications"] = {"SW": sw_a, "THS": ths_a, "TDX": tdx_a,
                                    "CI": {**audit_ci_gap(ci_m, flow), "membership_state": "HISTORY_UNAVAILABLE" if ci_unknown else "OFFICIAL_DATED", "daily_close": ci_quote_audit}}
        official.extend(sw_o); official.extend(tdx_o); official.extend(ci_o); official.extend(ths_o)
        # A capture that crossed local midnight is not a same-day THS snapshot.
        if self._now().strftime("%Y%m%d") != captured_date:
            fail("CAPTURE_DATE_CHANGED")
        return {"trade_date": trade_date, "captured_at": now.isoformat(), "provider_kind": self.provider_kind,
                "industries": industries, "memberships": members, "unknown_taxonomies": sorted(set(unknowns)),
                "stocks": stocks, "daily": [r for r in daily if is_a_share(r["ts_code"])],
                "daily_basic": [r for r in basic if is_a_share(r["ts_code"])],
                "moneyflow": [r for r in flow if is_a_share(r["ts_code"])], "official": official,
                "audit": audit, "source_refs": list(self._refs.values())}
