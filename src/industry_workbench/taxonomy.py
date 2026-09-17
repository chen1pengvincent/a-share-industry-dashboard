"""Pure classification and stock-universe validation for the workbench."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from statistics import median
import re


class DataError(ValueError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def fail(code):
    raise DataError(code)


def text(value, *, blank=False):
    value = "" if value is None else str(value).strip()
    if (not value and not blank) or any(c in value for c in ("\r", "\n", "\x00")):
        fail("INVALID_TEXT_FIELD")
    return value


def date(value, *, blank=False):
    value = text(value, blank=blank)
    if blank and not value:
        return value
    try:
        if len(value) != 8 or not value.isdigit():
            raise ValueError()
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        fail("INVALID_DATE")
    return value


def decimal(value, *, nullable=False):
    if nullable and (value is None or value == ""):
        return None
    try:
        if isinstance(value, bool):
            raise InvalidOperation()
        result = Decimal(str(value))
        if not result.is_finite():
            raise InvalidOperation()
        return result
    except (InvalidOperation, ValueError):
        fail("INVALID_FINANCIAL_NUMBER")


def flow_cent(value):
    numerator, denominator = decimal(value).as_integer_ratio()
    if (numerator * 100) % denominator:
        fail("MONEYFLOW_PRECISION")
    return numerator * 100 // denominator


def is_a_share(code):
    return bool(re.fullmatch(r"(?:6\d{5}\.SH|[03]\d{5}\.SZ)", str(code)))


def unique(rows, fields, *, code="DUPLICATE_PRIMARY_KEY"):
    seen = set()
    for row in rows:
        key = tuple(text(row.get(field), blank=field in {"out_date", "delist_date"}) for field in fields)
        if key in seen:
            fail(code)
        seen.add(key)


def dated_rows(rows, trade_date):
    for row in rows:
        if date(row.get("trade_date")) != trade_date:
            fail("RESPONSE_DATE_MISMATCH")
    unique(rows, ("ts_code", "trade_date"))


def normalize_stocks(rows):
    unique(rows, ("ts_code",), code="LIFECYCLE_IDENTITY_CONFLICT")
    result = []
    for source in rows:
        row = dict(source)
        row["ts_code"] = text(row.get("ts_code"))
        row["name"] = text(row.get("name"))
        row["list_date"] = date(row.get("list_date"))
        row["delist_date"] = date(row.get("delist_date"), blank=True)
        if row["list_status"] not in {"L", "D", "P"}:
            fail("LIFECYCLE_STATE_INVALID")
        if row["delist_date"] and row["list_date"] >= row["delist_date"]:
            fail("LIFECYCLE_INTERVAL_INVALID")
        if is_a_share(row["ts_code"]):
            result.append(row)
    if not result:
        fail("LIFECYCLE_EMPTY")
    return result


def active_daily(rows, trade_date):
    dated_rows(rows, trade_date)
    active = []
    for row in rows:
        vol, amount = decimal(row.get("vol")), decimal(row.get("amount"))
        if vol < 0 or amount < 0 or (vol > 0) != (amount > 0):
            fail("DAILY_ACTIVITY_INVALID")
        if is_a_share(row["ts_code"]) and vol > 0:
            active.append(dict(row))
    if not active:
        fail("ACTIVE_DAILY_EMPTY")
    return active


def audit_stock_day(*, trade_date, stocks, daily, daily_basic, moneyflow, suspensions, prior_active_counts):
    """Validate independent universes; legal missing valuation *values* are not gaps."""
    date(trade_date)
    eligible = {r["ts_code"] for r in stocks if r["list_date"] <= trade_date and (not r["delist_date"] or trade_date < r["delist_date"])}
    active = active_daily(daily, trade_date)
    traded = {r["ts_code"] for r in active}
    suspended = set()
    suspension_keys = set()
    for row in suspensions:
        if date(row.get("trade_date")) != trade_date or row.get("suspend_type") != "S":
            fail("SUSPENSION_SCOPE_MISMATCH")
        timing = text(row.get("suspend_timing"), blank=True).lower()
        key = (text(row.get("ts_code")), timing)
        if key in suspension_keys:
            fail("SUSPENSION_DUPLICATE")
        suspension_keys.add(key)
        if timing in {"", "全天", "全天停牌", "full day", "full-day"}:
            suspended.add(text(row.get("ts_code")))
    if not eligible or traded - eligible or eligible - traded - suspended:
        fail("INDEPENDENT_UNIVERSE_INCOMPLETE")
    dated_rows(moneyflow, trade_date)
    flow = [dict(row) for row in moneyflow if is_a_share(row["ts_code"])]
    if {r["ts_code"] for r in flow} != traded:
        fail("MONEYFLOW_COVERAGE_GAP")
    for row in flow:
        flow_cent(row.get("net_mf_amount"))
        vol = decimal(row.get("net_mf_vol"))
        if vol != vol.to_integral_value():
            fail("MONEYFLOW_VOLUME_NOT_INTEGER")
    dated_rows(daily_basic, trade_date)
    basics = [dict(row) for row in daily_basic if is_a_share(row["ts_code"])]
    basic_codes = {r["ts_code"] for r in basics}
    if traded - basic_codes or basic_codes - eligible:
        fail("DAILY_BASIC_COVERAGE_GAP")
    for row in basics:
        for field in ("pe_ttm", "pb"):
            decimal(row.get(field), nullable=True)
    if len(prior_active_counts) != 3 or any(not isinstance(n, int) or n <= 0 for n in prior_active_counts):
        fail("PRIOR_ACTIVITY_BASELINE_INVALID")
    prior = Decimal(str(median(prior_active_counts)))
    if Decimal(len(traded)) / prior < Decimal("0.95"):
        fail("SYNCHRONIZED_PARTIAL_SNAPSHOT")
    return {
        "base_complete": True, "eligible_count": len(eligible), "traded_count": len(traded),
        "full_day_suspended_count": len((suspended & eligible) - traded),
        "basic_received_count": len(basics), "basic_suspended_missing_count": len(eligible - traded - basic_codes),
        "moneyflow_received_count": len(flow), "prior_active_counts": list(prior_active_counts),
        "daily_row_coverage": "1", "daily_amount_coverage": "1",
        "excluded_non_a_daily_count": sum(not is_a_share(r["ts_code"]) for r in daily),
        "excluded_non_a_moneyflow_count": len(moneyflow) - len(flow),
    }


def industry(*, taxonomy, level, code, name, parent_uid=None, is_pub=True, market_code=None, membership_code=None):
    version = "SW2021" if taxonomy == "SW" else taxonomy
    return {"uid": f"{version}:{level}:{code}", "taxonomy": taxonomy, "version": version,
            "level": level, "code": code, "name": name, "parent_uid": parent_uid,
            "is_pub": is_pub, "market_code": market_code, "membership_code": membership_code}


def sw_industries(classes, *, identity=None):
    result, by_code = [], {}
    for level in ("L1", "L2", "L3"):
        rows = classes.get(level, [])
        if not rows:
            fail("SW_CATALOG_EMPTY")
        unique(rows, ("industry_code",), code="SW_CATALOG_IDENTITY_CONFLICT")
        for row in rows:
            if row.get("level") != level or row.get("src") != "SW2021":
                fail("SW_CATALOG_SCOPE_MISMATCH")
            code, source = text(row.get("industry_code")), text(row.get("index_code"))
            parent_code = text(row.get("parent_code"))
            parent = None if level == "L1" else by_code.get(parent_code)
            if level == "L1" and parent_code != "0":
                fail("SW_PARENT_INVALID")
            if level != "L1" and (not parent or parent["level"] != {"L2": "L1", "L3": "L2"}[level]):
                fail("SW_PARENT_INVALID")
            quote_code = member_code = source
            if code == "230501":
                if identity is None:
                    fail("SW_SPECIAL_IDENTITY_EVIDENCE_REQUIRED")
                quote_code, member_code = identity.quote_index_code, identity.member_index_code
            if str(row.get("is_pub")) not in {"0", "1"}:
                fail("SW_PUBLICATION_STATE_INVALID")
            item = industry(taxonomy="SW", level=level, code=code, name=text(row.get("industry_name")),
                            parent_uid=parent["uid"] if parent else None, is_pub=str(row["is_pub"]) == "1",
                            market_code=quote_code, membership_code=member_code)
            result.append(item)
            if code in by_code:
                fail("SW_CATALOG_IDENTITY_CONFLICT")
            by_code[code] = item
    unique(result, ("membership_code",), code="SW_CODE_COLLISION")
    return result


def ci_industries(rows):
    result = {}
    for row in rows:
        parent_uid = None
        for n in (1, 2, 3):
            code, name = text(row.get(f"l{n}_code")), text(row.get(f"l{n}_name"))
            item = industry(taxonomy="CI", level=f"L{n}", code=code, name=name,
                            parent_uid=parent_uid, membership_code=code)
            if item["uid"] in result and result[item["uid"]] != item:
                fail("CI_CATALOG_IDENTITY_CONFLICT")
            result[item["uid"]] = item
            parent_uid = item["uid"]
    return sorted(result.values(), key=lambda r: (r["level"], r["code"]))


def tree_memberships(rows, industries, trade_date, *, taxonomy, identity=None):
    """Normalize episodes and preserve boundary/overlap uncertainty at each level."""
    date(trade_date)
    by_member_code = {r["membership_code"]: r for r in industries}
    by_uid = {r["uid"]: r for r in industries}
    episodes = defaultdict(list)
    raw_keys = set()
    for source in rows:
        row = dict(source)
        row["ts_code"] = text(row.get("ts_code"))
        row["in_date"] = date(row.get("in_date"))
        row["out_date"] = date(row.get("out_date"), blank=True)
        state = text(row.get("is_new"))
        if state not in {"Y", "N"} or (state == "Y" and row["out_date"]) or (state == "N" and not row["out_date"]):
            fail("MEMBER_STATE_DATE_CONFLICT")
        if row["out_date"] and row["out_date"] < row["in_date"]:
            fail("MEMBER_INTERVAL_INVALID")
        raw_key = tuple(text(row.get(k), blank=k == "out_date") for k in ("ts_code", "l1_code", "l2_code", "l3_code", "in_date", "out_date", "is_new"))
        if raw_key in raw_keys:
            fail("MEMBERSHIP_DUPLICATE")
        raw_keys.add(raw_key)
        leaf_code = text(row.get("l3_code"))
        if taxonomy == "SW" and identity is not None and leaf_code in {"850401.SI", "850412.SI"}:
            from swivd.v2_identity import project_member
            row = project_member(row, identity)
            leaf_code = row["l3_code"]
        leaf = by_member_code.get(leaf_code)
        if not leaf or leaf["level"] != "L3":
            fail("MEMBER_PATH_UNKNOWN")
        path = [leaf]
        while path[-1]["parent_uid"]:
            path.append(by_uid[path[-1]["parent_uid"]])
        for item in path:
            n = item["level"][-1]
            # Names reject mismatched identities; they never establish a join.
            if text(row.get(f"l{n}_code")) != item["membership_code"] or text(row.get(f"l{n}_name")) != item["name"]:
                fail("MEMBER_PATH_IDENTITY_CONFLICT")
        row["path"] = path
        key = (row["ts_code"], leaf["uid"], row["in_date"])
        episodes[key].append(row)
    reconciled = []
    for candidates in episodes.values():
        closed = {r["out_date"] for r in candidates if r["out_date"]}
        if len(closed) > 1:
            fail("MEMBER_EPISODE_CONFLICT")
        reconciled.append(next((r for r in candidates if r["out_date"]), candidates[0]))
    stock_rows = defaultdict(list)
    for row in reconciled:
        if row["in_date"] <= trade_date and (not row["out_date"] or trade_date <= row["out_date"]) and is_a_share(row["ts_code"]):
            stock_rows[row["ts_code"]].append(row)
    result = []
    for code, selected in stock_rows.items():
        for level in ("L1", "L2", "L3"):
            at_level = defaultdict(list)
            for row in selected:
                item = next(i for i in row["path"] if i["level"] == level)
                at_level[item["uid"]].append(row)
            for uid, matches in at_level.items():
                boundary = any(trade_date in {r["in_date"], r["out_date"]} for r in matches)
                state = "MEMBERSHIP_BOUNDARY_UNKNOWN" if boundary else "MEMBERSHIP_OVERLAP_UNKNOWN" if len(at_level) > 1 or len(matches) > 1 else "ACTIVE"
                result.append({"uid": uid, "ts_code": code, "state": state, "evidence_kind": "OFFICIAL_DATED",
                               "in_date": min(r["in_date"] for r in matches),
                               "out_date": "" if any(not r["out_date"] for r in matches) else max(r["out_date"] for r in matches)})
    return sorted(result, key=lambda r: (r["uid"], r["ts_code"]))


def flat_industries(catalog, *, taxonomy, trade_date):
    unique(catalog, ("ts_code",), code="FLAT_CATALOG_IDENTITY_CONFLICT")
    result = []
    for row in catalog:
        code = text(row.get("ts_code"))
        if taxonomy == "TDX":
            if date(row.get("trade_date")) != trade_date or row.get("idx_type") != "行业板块" or not re.fullmatch(r"88[01]\d{3}\.TDX", code):
                fail("TDX_CATALOG_SCOPE_MISMATCH")
            level, name = code[:3], text(row.get("name"))
        else:
            if date(row.get("trade_date")) != trade_date or not re.fullmatch(r"881\d{3}\.TI", code):
                fail("THS_CATALOG_SCOPE_MISMATCH")
            level, name = "INDUSTRY", text(row.get("industry"))
        result.append(industry(taxonomy=taxonomy, level=level, code=code, name=name,
                               market_code=code if taxonomy == "TDX" else None, membership_code=code))
    if taxonomy == "TDX" and catalog and {r["level"] for r in result} != {"880", "881"}:
        fail("TDX_SERIES_CHANGED")
    return result


def flat_memberships(rows, industries, *, taxonomy, trade_date, captured_date):
    if taxonomy == "THS" and trade_date != captured_date:
        fail("THS_HISTORICAL_MEMBERS_FORBIDDEN")
    by_code = {r["code"]: r for r in industries}
    unique(rows, ("ts_code", "con_code"), code="FLAT_MEMBERSHIP_DUPLICATE")
    result = []
    for row in rows:
        board, code = text(row.get("ts_code")), text(row.get("con_code"))
        if board not in by_code:
            fail("FLAT_MEMBER_CATALOG_MISMATCH")
        if taxonomy == "TDX" and date(row.get("trade_date")) != trade_date:
            fail("TDX_MEMBER_DATE_MISMATCH")
        if taxonomy == "THS" and row.get("is_new") != "Y":
            fail("THS_CURRENT_STATE_MISMATCH")
        if is_a_share(code):
            result.append({"uid": by_code[board]["uid"], "ts_code": code, "state": "ACTIVE",
                           "evidence_kind": "OBSERVED_SAME_DAY" if taxonomy == "THS" else "OFFICIAL_DATED",
                           "in_date": trade_date if taxonomy == "THS" else "", "out_date": ""})
    if taxonomy == "TDX":
        owners = defaultdict(list)
        for row in result:
            owners[(row["ts_code"], by_code[row["uid"].split(":", 2)[2]]["level"])].append(row)
        for matches in owners.values():
            if len(matches) > 1:
                for row in matches:
                    row["state"] = "MEMBERSHIP_OVERLAP_UNKNOWN"
    return result


def audit_ci_gap(memberships, moneyflow):
    covered = {r["ts_code"] for r in memberships}
    flow = {r["ts_code"]: flow_cent(r["net_mf_amount"]) for r in moneyflow if is_a_share(r["ts_code"])}
    if not flow:
        fail("CI_EMPTY_FLOW_UNIVERSE")
    missing = sorted(set(flow) - covered)
    unresolved = sorted({r["ts_code"] for r in memberships if r.get("state", "ACTIVE") != "ACTIVE" and r["ts_code"] in flow})
    total_abs = sum(abs(v) for v in flow.values())
    row_coverage = Decimal(len(flow) - len(missing)) / Decimal(len(flow))
    abs_coverage = Decimal(sum(abs(v) for k, v in flow.items() if k in covered)) / Decimal(total_abs) if total_abs else Decimal(1)
    return {"official_row_coverage": str(row_coverage), "official_abs_amount_coverage": str(abs_coverage),
            "unclassified_codes": missing, "unclassified_flow_cent": str(sum(flow[c] for c in missing)),
            "unclassified_abs_flow_cent": str(sum(abs(flow[c]) for c in missing)),
            "unresolved_codes": unresolved, "membership_complete": not missing and not unresolved,
            "legacy_threshold_pass": row_coverage >= Decimal("0.999") and abs_coverage >= Decimal("0.999") and len(missing) <= 10}
