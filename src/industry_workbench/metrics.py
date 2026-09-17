"""Pure day-level financial calculations. Never fetch, persist, or read a clock."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, localcontext
from typing import Any

from .models import DataError, decimal_text, decimal_value, metric, money_cent, parse_date


def _index(rows: list[dict], key: str, day: str | None = None) -> dict[str, dict]:
    result = {}
    for row in rows:
        code = row.get(key)
        if not isinstance(code, str) or not code or code in result:
            raise DataError("DUPLICATE_OR_MISSING_PRIMARY_KEY")
        if day and row.get("trade_date") != day:
            raise DataError("WRONG_TRADE_DATE")
        result[code] = row
    return result


def _median(values: list[Decimal]) -> Decimal | None:
    values = sorted(values)
    if not values:
        return None
    midpoint = len(values) // 2
    with localcontext() as ctx:
        ctx.prec = 50
        return values[midpoint] if len(values) % 2 else (values[midpoint-1] + values[midpoint]) / 2


def _eligible(stock: dict, day: str) -> bool:
    return (stock["ts_code"].endswith((".SH", ".SZ"))
            and (not stock.get("list_date") or stock["list_date"] <= day)
            and (not stock.get("delist_date") or day < stock["delist_date"]))


def compute_day(inputs: dict) -> dict:
    day = inputs["trade_date"]
    parse_date(day)
    if inputs.get("audit", {}).get("base_complete") is not True:
        raise DataError("BASE_INCOMPLETE")
    industries = _index(inputs["industries"], "uid")
    stocks = _index(inputs["stocks"], "ts_code")
    basics = _index(inputs["daily_basic"], "ts_code", day)
    daily = _index(inputs["daily"], "ts_code", day)
    flows = _index(inputs["moneyflow"], "ts_code", day)
    officials = _index(inputs.get("official", []), "uid", day)
    trade_codes = set()
    for code, row in daily.items():
        vol, amount = decimal_value(row.get("vol")), decimal_value(row.get("amount"))
        if vol is None or amount is None or vol < 0 or amount < 0 or (vol > 0) != (amount > 0):
            raise DataError("INVALID_TRADE_ACTIVITY")
        if vol > 0 and code in stocks and _eligible(stocks[code], day):
            trade_codes.add(code)
    relevant_flows = {code for code in flows if code.endswith((".SH", ".SZ"))}
    if trade_codes != relevant_flows:
        raise DataError("MONEYFLOW_COVERAGE_GAP")
    money = {code: money_cent(flows[code].get("net_mf_amount")) for code in relevant_flows}
    volumes = {}
    for code in relevant_flows:
        value = decimal_value(flows[code].get("net_mf_vol"))
        if value is None or value != value.to_integral_value():
            raise DataError("INVALID_FLOW_VOLUME")
        volumes[code] = int(value)
    grouped = defaultdict(dict)
    for assignment in inputs["memberships"]:
        uid, code = assignment.get("uid"), assignment.get("ts_code")
        if uid not in industries:
            raise DataError("UNKNOWN_INDUSTRY_IDENTITY")
        if code in grouped[uid] and grouped[uid][code] != assignment:
            raise DataError("DUPLICATE_MEMBERSHIP")
        grouped[uid][code] = assignment
    output, members = [], []
    for uid, industry in sorted(industries.items()):
        raw = list(grouped[uid].values())
        applicable = [row for row in raw if row["ts_code"] in stocks and _eligible(stocks[row["ts_code"]], day)]
        active = [row for row in applicable if row["state"] == "ACTIVE"]
        unknown = [row for row in applicable if row["state"] != "ACTIVE"]
        reasons = []
        if industry["taxonomy"] in inputs.get("unknown_taxonomies", []):
            ths_history=industry["taxonomy"]=="THS" and inputs.get("audit",{}).get("classifications",{}).get("THS",{}).get("membership_state")!="OBSERVED_SAME_DAY"
            reasons.append("MEMBERSHIP_HISTORY_UNAVAILABLE" if ths_history else "MEMBERSHIP_COVERAGE_UNKNOWN")
        if unknown:
            reasons.append(unknown[0]["state"])
        if any(row["ts_code"].endswith((".SH", ".SZ")) and row["ts_code"] not in stocks for row in raw):
            reasons.append("MEMBER_LIFECYCLE_UNKNOWN")
        count = len(active)
        received = sum(row["ts_code"] in basics for row in active)
        pe_values = [v for row in active if (v := decimal_value(basics.get(row["ts_code"], {}).get("pe_ttm"), positive=True)) is not None]
        pb_values = [v for row in active if (v := decimal_value(basics.get(row["ts_code"], {}).get("pb"), positive=True)) is not None]
        expected_flow = sum(row["ts_code"] in trade_codes for row in active)
        metrics = {}
        for field, values in (("pe_ttm_median", pe_values), ("pb_median", pb_values)):
            reason = reasons[0] if reasons else "NO_MEMBERS" if not count else "DAILY_BASIC_RECORD_MISSING" if received != count else None
            value = None if reason else _median(values)
            if value is None and reason is None:
                reason = "NO_VALID_ESTIMATE"
            metrics[field] = metric(value, day, reason=reason, status="SMALL_SAMPLE" if value is not None and len(values) < 5 else None,
                                    expected_count=count, received_count=received, valid_count=len(values))
        flow_reason = reasons[0] if reasons else "NO_MEMBERS" if not count else None
        metrics["flow_cent"] = metric(None if flow_reason else sum(money.get(row["ts_code"], 0) for row in active), day,
                                       reason=flow_reason, expected_count=expected_flow, received_count=expected_flow)
        metrics["net_mf_vol"] = metric(None if flow_reason else sum(volumes.get(row["ts_code"], 0) for row in active), day, reason=flow_reason)
        metrics["flow_cent"]["known_subtotal"] = str(sum(money.get(row["ts_code"], 0) for row in active))
        metrics["net_mf_vol"]["known_subtotal"] = str(sum(volumes.get(row["ts_code"], 0) for row in active))
        official = officials.get(uid, {})
        for name, source in (("official_pe", "pe"), ("official_pb", "pb"), ("close", "close")):
            value = decimal_value(official.get(source), positive=True)
            reason = None if value is not None else "OFFICIAL_NOT_PROVIDED" if name != "close" and industry["taxonomy"] in ("THS", "CI") else "OFFICIAL_VALUE_MISSING"
            metrics[name] = metric(value, day, reason=reason)
        for key in ("pe_percentile", "pb_percentile", "official_pe_percentile", "official_pb_percentile"):
            metrics[key] = metric(None, day, reason="HISTORY_INSUFFICIENT", valid_count=0)
        evidence = {row["evidence_kind"] for row in applicable}
        row = deepcopy(industry)
        row.update(metrics=metrics, counts={"member_count": count, "unknown_member_count":len(unknown),
                    "basic_received":received,"pe_valid":len(pe_values),"pb_valid":len(pb_values),
                    "flow_expected":expected_flow,"flow_received":expected_flow},
                   membership_evidence_kind=next(iter(evidence)) if len(evidence)==1 else "UNKNOWN",
                   diagnostics={"reason_codes":reasons,"unknown_members":[{"ts_code":a["ts_code"],"state":a["state"],"flow_cent":str(money[a["ts_code"]]) if a["ts_code"] in money else None} for a in unknown]},
                   status="WITH_GAPS" if reasons or any(metrics[k]["value"] is None for k in ("pe_ttm_median","pb_median","flow_cent")) else "OK")
        output.append(row)
        for assignment in applicable:
            code=assignment["ts_code"]; basic=basics.get(code,{}); valid=assignment["state"]=="ACTIVE" and not reasons
            member={"uid":uid,"ts_code":code,"name":stocks[code].get("name",code),"membership_state":assignment["state"],
                    "evidence_kind":assignment["evidence_kind"],"has_trade":code in trade_codes,"is_endpoint_member":assignment["state"]=="ACTIVE",
                    "flow_cent":str(money.get(code,0)) if valid else None,"net_mf_vol":str(volumes.get(code,0)) if valid else None,
                    "known_subtotal":str(money.get(code,0)) if assignment["state"]=="ACTIVE" else None,
                    "net_mf_vol_known_subtotal":str(volumes.get(code,0)) if assignment["state"]=="ACTIVE" else None,
                    "peer_pe_count":len(pe_values),"peer_pb_count":len(pb_values)}
            for field in ("pe_ttm","pb","pe","ps_ttm","dv_ttm","total_mv","circ_mv"):
                value=decimal_value(basic.get(field));member[field]=None if value is None else decimal_text(value)
            for key,field,values in (("pe_peer_percentile","pe_ttm",pe_values),("pb_peer_percentile","pb",pb_values)):
                value=decimal_value(basic.get(field),positive=True)
                with localcontext() as ctx:
                    ctx.prec=50
                    member[key]=decimal_text(Decimal(sum(x<=value for x in values))*100/len(values)) if valid and value is not None and len(values)>=5 else None
            members.append(member)
    result={"trade_date":day,"captured_at":inputs.get("captured_at"),"provider_kind":inputs["provider_kind"],
            "industries":output,"members":members,"audit":deepcopy(inputs["audit"]),"source_refs":deepcopy(inputs.get("source_refs",[]))}
    result["audit"]["market_flow_cent"]=str(sum(money.values()))
    result["audit"]["unknown_taxonomies"]=list(inputs.get("unknown_taxonomies",[]))
    coverage=[]
    for taxonomy,level in sorted({(r["taxonomy"],r["level"]) for r in output}):
        scope={r["uid"] for r in output if r["taxonomy"]==taxonomy and r["level"]==level}
        known=defaultdict(set);uncertain=set()
        for uid in scope:
            for code,assignment in grouped[uid].items():
                if code not in trade_codes:continue
                if assignment["state"]=="ACTIVE":known[code].add(uid)
                else:uncertain.add(code)
        unresolved=(trade_codes-set(known))|uncertain
        if taxonomy!="THS":unresolved|={code for code,uids in known.items() if len(uids)>1}
        coverage.append({"taxonomy":taxonomy,"level":level,"market_flow_cent":str(sum(money.values())),
                         "known_unique_flow_cent":str(sum(money[c] for c in known if c not in unresolved)),
                         "unknown_flow_cent":str(sum(money[c] for c in unresolved)),
                         "unknown_stocks":[{"ts_code":c,"flow_cent":str(money[c])} for c in sorted(unresolved)],
                         "complete":not unresolved and taxonomy not in inputs.get("unknown_taxonomies",[]),
                         "aggregation_note":"THS_MULTIMEMBERSHIP_NO_SUM_CONSERVATION" if taxonomy=="THS" else "WITHIN_LEVEL_OR_SERIES_ONLY"})
    result["audit"]["classification_coverage"]=coverage
    rank_flows(output)
    return result


def rank_flows(rows: list[dict]) -> None:
    groups=defaultdict(list)
    for row in rows:
        row["flow_rank"]=None
        value=row["metrics"]["flow_cent"]["value"]
        if value is not None:
            groups[(row["taxonomy"],row["level"])].append(row)
    for group in groups.values():
        previous=None; rank=0
        for pos,row in enumerate(sorted(group,key=lambda r:(-int(r["metrics"]["flow_cent"]["value"]),r["uid"])),1):
            value=row["metrics"]["flow_cent"]["value"]
            if value != previous:rank=pos
            row["flow_rank"]=rank;previous=value
