"""Natural period boundaries, preserving missing trading dates."""
from __future__ import annotations
import calendar as calendar_module
from datetime import timedelta
from .models import DataError, parse_date
from .models import metric
from copy import deepcopy
from dataclasses import dataclass


@dataclass(frozen=True)
class CalendarIndex:
    """A validated immutable calendar for repeated period queries."""
    dates: tuple[str,...]

    @classmethod
    def from_rows(cls,rows):
        return cls(tuple(open_dates(rows)))


def open_dates(calendar: list[dict] | list[str]) -> list[str]:
    if isinstance(calendar,CalendarIndex):return list(calendar.dates)
    result=[];seen=set()
    for row in calendar:
        day=row if isinstance(row,str) else row["cal_date"]
        parse_date(day)
        if day in seen:raise DataError("DUPLICATE_CALENDAR_DAY")
        seen.add(day)
        if not isinstance(row,str) and str(row.get("is_open")) not in ("0","1"):raise DataError("INVALID_CALENDAR_STATE")
        if isinstance(row,str) or str(row.get("is_open"))=="1":result.append(day)
    return sorted(result)


def period_dates(kind: str, key: str, as_of: str, calendar: list) -> dict:
    cutoff=parse_date(as_of)
    if kind=="month":
        try:
            base=parse_date(key.replace("-","")+"01" if len(key.replace("-",""))==6 else key.replace("-",""))
        except (ValueError,TypeError):raise DataError("INVALID_PERIOD") from None
        start=base.replace(day=1);end=base.replace(day=calendar_module.monthrange(base.year,base.month)[1]);canonical=start.strftime("%Y-%m")
    else:
        base=parse_date(key.replace("-",""))
        if kind=="week":
            start=base-timedelta(days=base.weekday());end=start+timedelta(days=6);canonical=start.strftime("%Y%m%d")
        elif kind=="day":start=end=base;canonical=key.replace("-","")
        else:raise DataError("INVALID_PERIOD_KIND")
    if start>cutoff:raise DataError("FUTURE_PERIOD")
    effective=min(end,cutoff)
    dates=[d for d in open_dates(calendar) if start.strftime("%Y%m%d")<=d<=effective.strftime("%Y%m%d")]
    return {"kind":kind,"key":canonical,"start":start.strftime("%Y%m%d"),"end":end.strftime("%Y%m%d"),
            "as_of":effective.strftime("%Y%m%d"),"endpoint":dates[-1] if dates else None,
            "expected_days":len(dates),"trade_dates":dates,
            "status":"NO_TRADING_DAYS" if not dates else "IN_PROGRESS" if end>cutoff else "COMPLETE"}


def aggregate_period(days: dict[str,dict], period: dict, fallback: list[dict] | None = None) -> dict:
    """Aggregate daily industry assignments, never end-of-period stock baskets."""
    from .metrics import rank_flows
    dates=period["trade_dates"]; endpoint=period["endpoint"]
    by_day={day:{row["uid"]:row for row in days.get(day,{}).get("industries",[])} for day in dates}
    identities={row["uid"]:row for row in fallback or []}
    for day in dates:identities.update(by_day[day])
    result=[]
    for uid,identity in sorted(identities.items()):
        endrow=by_day.get(endpoint,{}).get(uid)
        row=deepcopy(endrow or identity)
        if endrow is None:
            row["metrics"]={key:metric(None,endpoint or period["as_of"],reason="PERIOD_ENDPOINT_MISSING") for key in row["metrics"]}
            row["counts"]={key:None for key in row.get("counts",{})}
            row["membership_evidence_kind"]="UNKNOWN"
        for key in ("flow_cent","net_mf_vol"):
            daily_metrics=[by_day[d].get(uid,{}).get("metrics",{}).get(key,{}) for d in dates]
            values=[m.get("value") for m in daily_metrics]
            missing=[d for d,v in zip(dates,values) if v is None]
            value=sum(int(v) for v in values) if values and not missing else None
            m=metric(value,endpoint or period["as_of"],reason="NO_TRADING_DAYS" if not dates else "PERIOD_DATA_GAP" if missing else None,
                     expected_count=len(dates),received_count=len(dates)-len(missing))
            # Incomplete daily totals can still carry a verified member subtotal.
            # Keep that diagnostic amount without promoting it to a complete flow.
            subtotals=[item.get("known_subtotal",item.get("value")) for item in daily_metrics]
            m.update(known_subtotal=str(sum(int(v) for v in subtotals if v is not None)),missing_dates=missing)
            row["metrics"][key]=m
        row["status"]="WITH_GAPS" if any(row["metrics"][k]["value"] is None for k in ("flow_cent","pe_ttm_median","pb_median")) else "OK"
        result.append(row)
    rank_flows(result)
    p=deepcopy(period);p["available_days"]=sum(d in days for d in dates)
    p["missing_dates"]=[d for d in dates if d not in days]
    if p["missing_dates"]:p["status"]="DATA_GAP"
    return {"period":p,"industries":result}
