"""Independent publication gate: exact rational arithmetic, no I/O or provider.

This module intentionally does not share financial helpers with metrics.py.
It audits the declared classification scope and uncertainty supplied by the
provider; proving provider coverage remains a separate upstream responsibility.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction

from .models import DataError

VERSION = "independent-rational-day-audit-v1"


def _rational(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("boolean numeric value")
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("nonfinite numeric value")
    return Fraction(number)


def _positive(value):
    value = _rational(value)
    return value if value is not None and value > 0 else None


def _middle(values):
    ordered = sorted(values)
    if not ordered:
        return None
    # The same two-position formula handles odd and even sample counts.
    return (ordered[(len(ordered) - 1) // 2] + ordered[len(ordered) // 2]) / 2


def _scope(stock, day):
    code = str(stock["ts_code"])
    return bool(re.fullmatch(r"(?:6\d{5}\.SH|[03]\d{5}\.SZ)", code)) and (
        not stock.get("list_date") or stock["list_date"] <= day) and (
        not stock.get("delist_date") or day < stock["delist_date"])


def _index(rows, key):
    result = {row[key]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError("duplicate identity")
    return result


def _percent(value, values):
    ratio = Fraction(sum(other <= value for other in values) * 100, len(values))
    with localcontext() as context:
        context.prec = 50
        return Fraction(Decimal(ratio.numerator) / Decimal(ratio.denominator))


def audit_day(inputs: dict, result: dict) -> dict:
    """Fail closed on a disagreement; return a compact manifest-ready summary."""
    try:
        return _audit_day(inputs, result)
    except (ArithmeticError, InvalidOperation, KeyError, TypeError, ValueError, AttributeError, IndexError):
        # Do not expose raw fields/financial inputs in an application exception.
        raise DataError("INDEPENDENT_RECALCULATION_FAILED") from None


def _audit_day(inputs, result):
    checks = Counter()

    def same(category, actual, expected):
        checks[category] += 1
        if isinstance(actual, bool) and isinstance(expected, int) and not isinstance(expected, bool):
            raise ValueError(category)
        if actual != expected:
            raise ValueError(category)

    day = inputs["trade_date"]
    same("identity", result["trade_date"], day)
    stocks = _index(inputs["stocks"], "ts_code")
    basics = _index(inputs["daily_basic"], "ts_code")
    daily = _index(inputs["daily"], "ts_code")
    flows = _index(inputs["moneyflow"], "ts_code")
    industries = _index(inputs["industries"], "uid")
    actual = _index(result["industries"], "uid")
    official = _index(inputs.get("official", []), "uid")
    same("identity", set(actual), set(industries))
    pool = {code for code, stock in stocks.items() if _scope(stock, day)}
    traded = set()
    for code, row in daily.items():
        same("base_dates", row["trade_date"], day)
        vol, amount = _rational(row["vol"]), _rational(row["amount"])
        if vol is None or amount is None or vol < 0 or amount < 0 or (vol > 0) != (amount > 0):
            raise ValueError("invalid trade activity")
        if vol > 0 and code in pool:
            traded.add(code)
    same("base", set(flows), traded)
    same("base", traded - set(basics), set())
    for rows in (basics, flows):
        for row in rows.values():
            same("base_dates", row["trade_date"], day)
    money, volume = {}, {}
    for code, row in flows.items():
        cent = _rational(row["net_mf_amount"]) * 100
        vol = _rational(row["net_mf_vol"])
        if cent.denominator != 1 or vol is None or vol.denominator != 1:
            raise ValueError("invalid integer amount or volume")
        money[code], volume[code] = int(cent), int(vol)
    if "market_flow_cent" in result.get("audit", {}):
        same("market_conservation", _rational(result["audit"]["market_flow_cent"]), sum(money.values()))
    for field, expected in (("eligible_count", len(pool)), ("traded_count", len(traded))):
        if field in inputs.get("audit", {}):
            same("base", inputs["audit"][field], expected)
    groups = defaultdict(list)
    seen = set()
    for assignment in inputs["memberships"]:
        key = (assignment["uid"], assignment["ts_code"])
        if key in seen or assignment["uid"] not in industries:
            raise ValueError("invalid membership identity")
        seen.add(key)
        groups[assignment["uid"]].append(assignment)
    declared_unknown = set(inputs.get("unknown_taxonomies", []))
    expected_flows = {}
    rank_groups = defaultdict(list)
    expected_members = {}
    known_values = Counter()
    for uid, identity in industries.items():
        row = actual[uid]
        for field in ("taxonomy", "level", "code", "name", "parent_uid"):
            same("industry_identity", row.get(field), identity.get(field))
        applicable = [member for member in groups[uid] if member["ts_code"] in pool]
        active = [member for member in applicable if member["state"] == "ACTIVE"]
        unknown = [member for member in applicable if member["state"] != "ACTIVE"]
        codes = {member["ts_code"] for member in active}
        unresolved_lifecycle = any(member["ts_code"].endswith((".SH", ".SZ")) and member["ts_code"] not in stocks for member in groups[uid])
        unavailable = identity["taxonomy"] in declared_unknown or bool(unknown) or unresolved_lifecycle
        received = len(codes & set(basics))
        pe = [value for code in codes if (value := _positive(basics.get(code, {}).get("pe_ttm"))) is not None]
        pb = [value for code in codes if (value := _positive(basics.get(code, {}).get("pb"))) is not None]
        enough_records = bool(active) and not unavailable and received == len(active)
        expected = {"pe_ttm_median": _middle(pe) if enough_records else None, "pb_median": _middle(pb) if enough_records else None,
                    "flow_cent": sum(money.get(code, 0) for code in codes) if active and not unavailable else None,
                    "net_mf_vol": sum(volume.get(code, 0) for code in codes) if active and not unavailable else None,
                    "official_pe": _positive(official.get(uid, {}).get("pe")), "official_pb": _positive(official.get(uid, {}).get("pb")),
                    "close": _positive(official.get(uid, {}).get("close"))}
        expected_flows[uid] = expected["flow_cent"]
        counts = {"member_count": len(active), "unknown_member_count": len(unknown), "basic_received": received,
                  "pe_valid": len(pe), "pb_valid": len(pb), "flow_expected": len(codes & traded), "flow_received": len(codes & traded)}
        for field, value in counts.items():
            same("sample_counts", row["counts"].get(field), value)
        for field, value in expected.items():
            saved = row["metrics"][field]
            same("financial_values", _rational(saved["value"]), value)
            if value is None:
                same("missing_value", saved["value"], None)
                same("missing_reason", bool(saved.get("reason_codes")), True)
            else:
                known_values[field] += 1
            count = len(pe) if field == "pe_ttm_median" else len(pb) if field == "pb_median" else None
            status = "NA" if value is None else "SMALL_SAMPLE" if count is not None and 1 <= count < 5 else "OK"
            same("metric_status", saved["status"], status)
            same("metric_dates", saved["metric_date"], day)
            if count is not None:
                same("metric_sample_counts", saved.get("valid_count"), count)
                same("metric_sample_counts", saved.get("expected_count"), len(active))
                same("metric_sample_counts", saved.get("received_count"), received)
        if expected["flow_cent"] is not None:
            rank_groups[(identity["taxonomy"], identity["level"])].append(uid)
        same("industry_known_subtotal",_rational(row["metrics"]["flow_cent"].get("known_subtotal")),sum(money.get(code,0) for code in codes))
        same("industry_known_subtotal",_rational(row["metrics"]["net_mf_vol"].get("known_subtotal")),sum(volume.get(code,0) for code in codes))
        same("industry_status", row["status"], "WITH_GAPS" if unavailable or any(expected[k] is None for k in ("pe_ttm_median", "pb_median", "flow_cent")) else "OK")
        for assignment in applicable:
            code = assignment["ts_code"]
            valid = assignment["state"] == "ACTIVE" and not unavailable
            expected_member = {"membership_state": assignment["state"], "evidence_kind": assignment["evidence_kind"],
                               "has_trade": code in traded, "is_endpoint_member": assignment["state"] == "ACTIVE",
                               "flow_cent": Fraction(money.get(code, 0)) if valid else None,
                               "net_mf_vol": Fraction(volume.get(code, 0)) if valid else None,
                               "known_subtotal": Fraction(money.get(code,0)) if assignment["state"]=="ACTIVE" else None,
                               "net_mf_vol_known_subtotal": Fraction(volume.get(code,0)) if assignment["state"]=="ACTIVE" else None,
                               "peer_pe_count": len(pe), "peer_pb_count": len(pb)}
            for field in ("pe_ttm", "pb", "pe", "ps_ttm", "dv_ttm", "total_mv", "circ_mv"):
                expected_member[field] = _rational(basics.get(code, {}).get(field))
            for field, values, source in (("pe_peer_percentile", pe, "pe_ttm"), ("pb_peer_percentile", pb, "pb")):
                value = _positive(basics.get(code, {}).get(source))
                expected_member[field] = _percent(value, values) if valid and value is not None and len(values) >= 5 else None
            expected_members[(uid, code)] = expected_member
    for uid, row in actual.items():
        value = expected_flows[uid]
        rank = None if value is None else 1 + sum(expected_flows[other] > value for other in rank_groups[(row["taxonomy"], row["level"])])
        same("ranking", row["flow_rank"], rank)
    actual_members = {(m["uid"], m["ts_code"]): m for m in result["members"]}
    same("identity", len(actual_members), len(result["members"]))
    same("identity", set(actual_members), set(expected_members))
    numeric_member_fields = {"flow_cent", "net_mf_vol", "known_subtotal", "net_mf_vol_known_subtotal", "pe_ttm", "pb", "pe", "ps_ttm", "dv_ttm", "total_mv", "circ_mv", "pe_peer_percentile", "pb_peer_percentile"}
    for key, expected in expected_members.items():
        for field, value in expected.items():
            actual_value = actual_members[key].get(field)
            if field in numeric_member_fields and value is None:
                same("member_missing_value", actual_value, None)
            same("member_values", _rational(actual_value) if field in numeric_member_fields else actual_value, value)
    return {"version": VERSION, "status": "PASS", "trade_date": day, "checks": sum(checks.values()),
            "check_counts": dict(checks), "industry_count": len(industries), "member_count": len(actual_members),
            "known_metric_counts": dict(known_values), "declared_unknown_taxonomies": sorted(declared_unknown),
            "algorithm": "Fraction median; exact integer money; independent competition rank; Decimal(50) peer percentiles"}
