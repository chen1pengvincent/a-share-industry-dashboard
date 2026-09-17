"""Build verified read projections; HTTP has no financial calculations."""
from __future__ import annotations
from bisect import bisect_left,bisect_right,insort
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal,localcontext
from pathlib import Path
from datetime import timedelta
from .models import DataError,TAXONOMIES,decimal_value,decimal_text,metric,parse_date
from .periods import CalendarIndex,aggregate_period,open_dates,period_dates
from .sorting import add_name_sort_keys

PERCENTILES={"pe_ttm_median":"pe_percentile","pb_median":"pb_percentile",
             "official_pe":"official_pe_percentile","official_pb":"official_pb_percentile"}


class HistoryAccumulator:
    def __init__(self,calendar: list):
        self.calendar=open_dates(calendar);self.values=defaultdict(list);self.first={};self.last={};self.closes=defaultdict(dict)
        self.last_enriched_day=None

    def enrich(self,day: str,rows: list[dict]) -> list[dict]:
        parse_date(day)
        if self.last_enriched_day is not None and day<=self.last_enriched_day:raise DataError("NON_MONOTONIC_HISTORY_DATE")
        self.last_enriched_day=day
        rows=deepcopy(rows)
        for row in rows:
            for source,target in PERCENTILES.items():
                evidence="OFFICIAL" if source.startswith("official_") else row["membership_evidence_kind"]
                key=(row["uid"],source,evidence)
                current=decimal_value(row["metrics"][source]["value"],positive=True)
                values=self.values[key]
                if current is not None:
                    insort(values,current);self.first.setdefault(key,day);self.last[key]=day
                value=None
                if current is not None and len(values)>=252:
                    with localcontext() as ctx:
                        ctx.prec=50;value=Decimal(bisect_right(values,current))*100/len(values)
                row["metrics"][target]=metric(value,day,reason=None if value is not None else "CURRENT_VALUE_MISSING" if current is None else "HISTORY_INSUFFICIENT",valid_count=len(values))
                first = self.first.get(key)
                expected = bisect_right(self.calendar,day)-bisect_left(self.calendar,first) if first else 0
                row["metrics"][target].update(first_valid_date=first,last_valid_date=self.last.get(key),minimum_valid_days=252,
                                               expected_days=expected,missing_days=expected-len(values),
                                               equal_count=bisect_right(values,current)-bisect_left(values,current) if current is not None else 0)
            close=decimal_value(row["metrics"]["close"]["value"],positive=True)
            series=self.closes[row["uid"]]
            if close is not None:series[day]=close
            idx=bisect_right(self.calendar,day)-1
            anchors={"return_5d":self.calendar[idx-5] if idx>=5 else None}
            for key,start in (("return_mtd",day[:6]+"01"),("return_ytd",day[:4]+"0101")):
                i=bisect_right(self.calendar,start)-1
                if i>=0 and self.calendar[i]>=start:i-=1
                anchors[key]=self.calendar[i] if i>=0 else None
            for key,anchor in anchors.items():
                base=series.get(anchor)
                with localcontext() as ctx:
                    ctx.prec=50;value=(close/base-1)*100 if close is not None and base is not None else None
                row["metrics"][key]=metric(value,day,reason=None if value is not None else "RETURN_ANCHOR_MISSING")
                row["metrics"][key]["anchor_date"]=anchor
        return rows


def compile_views(store,day_refs: dict,calendar: list,progress=None) -> dict:
    """Reproducible projections. Stream raw daily shards; no all-stock history load."""
    dates=sorted(day_refs)
    if not dates:raise DataError("NO_DAILY_DATA")
    as_of=dates[-1];calendar_index=CalendarIndex.from_rows(calendar);history=HistoryAccumulator(calendar_index)
    index={"day":{},"week":{},"month":{},"history":{}}
    pending={"week":{},"month":{}};pending_key={"week":None,"month":None}
    columns={};catalogue=[];availability={};days_with_gaps=[]
    for number,day in enumerate(dates,1):
        result=store.read_json(day_refs[day]["result"])
        rows=history.enrich(day,result["industries"])
        if result.get("audit",{}).get("unknown_taxonomies") or any(r["status"]!="OK" for r in rows):days_with_gaps.append(day)
        for row in rows:
            scope=row["taxonomy"]+":"+row["level"]
            summary=availability.setdefault(scope,{"taxonomy":row["taxonomy"],"level":row["level"],"metrics":{}})
            for key in ("pe_ttm_median","pb_median","flow_cent","official_pe","official_pb","close"):
                record=summary["metrics"].setdefault(key,{"first_valid_date":None,"last_valid_date":None,"valid_industry_days":0,"missing_industry_days":0})
                if row["metrics"][key]["value"] is None:record["missing_industry_days"]+=1
                else:
                    record["first_valid_date"]=record["first_valid_date"] or day
                    record["last_valid_date"]=day;record["valid_industry_days"]+=1
        view={"trade_date":day,"industries":rows}
        index["day"][day]=store.put_json(view,"daily_view")
        catalogue=rows
        for row in rows:
            uid=row["uid"]
            entry=columns.setdefault(uid,{"uid":uid,"dates":[],"values":{},"identity":{k:row.get(k) for k in ("uid","name","code","taxonomy","version","level","parent_uid")}})
            prior=len(entry["dates"]);entry["dates"].append(day)
            for key in set(entry["values"])|set(row["metrics"]):
                entry["values"].setdefault(key,[None]*prior).append(row["metrics"].get(key,{}).get("value"))
        for kind in ("week","month"):
            p=period_dates(kind,day,as_of,calendar_index);key=p["key"]
            if pending_key[kind] is not None and key!=pending_key[kind]:
                old=period_dates(kind,pending_key[kind],as_of,calendar_index)
                index[kind][old["key"]]=store.put_json(aggregate_period(pending[kind],old),"period_view")
                pending[kind]={}
            pending_key[kind]=key;pending[kind][day]=view
        if progress:progress("COMPILE",number,len(dates),day)
    for kind in ("week","month"):
        if pending[kind]:
            p=period_dates(kind,pending_key[kind],as_of,calendar_index)
            index[kind][p["key"]]=store.put_json(aggregate_period(pending[kind],p),"period_view")
    for uid,value in columns.items():index["history"][uid]=store.put_json(value,"industry_history")
    expected=[d for d in open_dates(calendar) if dates[0]<=d<=as_of]
    gaps=[d for d in expected if d not in day_refs]
    index["calendar"]=store.put_json(calendar,"calendar")
    index["catalogue"]=store.put_json({"industries":catalogue},"industry_catalogue")
    index["coverage"]={"start":dates[0],"end":as_of,"completed_days":len(dates),"missing_days":len(gaps),"missing_dates":gaps,
                       "days_with_metric_gaps":days_with_gaps,"availability_by_scope":list(availability.values())}
    return index


class QueryService:
    def __init__(self,store):self.store=store

    @staticmethod
    def _period(manifest,kind,key,calendar):
        period=period_dates(kind,key or manifest["as_of"],manifest["as_of"],calendar)
        if period["as_of"]<manifest.get("history_start",manifest["views"]["coverage"]["start"]):
            raise DataError("PERIOD_OUTSIDE_HISTORY_RANGE")
        return period

    def catalog(self,batch: str) -> dict:
        m=self.store.published_manifest(batch)
        catalogue=self.store.read_json(m["views"]["catalogue"])["industries"]
        return {"batch_id":batch,"as_of":m["as_of"],"taxonomies":TAXONOMIES,
                "trade_dates":sorted(m["days"]),"history":m["views"]["coverage"],"publication_state":m["publication_state"],
                "industries":add_name_sort_keys([{k:r.get(k) for k in ("uid","taxonomy","version","level","code","name","parent_uid")} for r in catalogue])}

    def industries(self,batch: str,*,taxonomy="SW",level_or_series="L1",period_kind="day",period_key=None) -> dict:
        allowed=next((x for x in TAXONOMIES if x["id"]==taxonomy),None)
        if not allowed or level_or_series not in [x["id"] for x in allowed["levels"]]:raise DataError("INVALID_TAXONOMY_LEVEL")
        m=self.store.published_manifest(batch);calendar=self.store.read_json(m["views"]["calendar"])
        p=self._period(m,period_kind,period_key,calendar)
        ref=m["views"].get(period_kind,{}).get(p["key"])
        if period_kind=="day" and ref:
            view=self.store.read_json(ref);p.update(available_days=1,missing_dates=[])
            rows=view["industries"]
        elif ref:
            view=self.store.read_json(ref);p=view["period"];rows=view["industries"]
        else:
            fallback=self.store.read_json(m["views"]["catalogue"])["industries"]
            view=aggregate_period({},p,fallback);p=view["period"];rows=view["industries"]
        rows=[r for r in rows if r["taxonomy"]==taxonomy and r["level"]==level_or_series]
        return {"batch_id":batch,"as_of":m["as_of"],"period":p,"rows":add_name_sort_keys(rows),
                "source_refs":{day:m["days"][day]["input"] for day in p["trade_dates"] if day in m["days"]}}

    def history(self,batch: str,uid: str,period_kind="day") -> dict:
        m=self.store.published_manifest(batch)
        if uid not in m["views"]["history"]:raise DataError("INDUSTRY_NOT_FOUND")
        if period_kind=="day":
            data=self.store.read_json(m["views"]["history"][uid])
            positions={day:i for i,day in enumerate(data["dates"])}
            calendar=self.store.read_json(m["views"]["calendar"])
            dates=[d for d in open_dates(calendar) if m.get("history_start",data["dates"][0]) <= d <= m["as_of"]]
            rows=[]
            for day in dates:
                i=positions.get(day)
                rows.append({"trade_date":day,"metrics":{k:metric(v[i] if i is not None else None,day,reason="HISTORICAL_VALUE_UNAVAILABLE" if i is None or v[i] is None else None) for k,v in data["values"].items()}})
        elif period_kind in ("week","month"):
            rows=[]
            calendar=CalendarIndex.from_rows(self.store.read_json(m["views"]["calendar"]))
            series=self.store.read_json(m["views"]["history"][uid])
            def period_key(day):
                if period_kind=="month":return day[:4]+"-"+day[4:6]
                value=parse_date(day)
                return (value-timedelta(days=value.weekday())).strftime("%Y%m%d")
            keys={period_key(d) for d in open_dates(calendar) if m.get("history_start",series["dates"][0])<=d<=m["as_of"]}
            for key in sorted(keys):
                p=period_dates(period_kind,key,m["as_of"],calendar)
                ref=m["views"][period_kind].get(key)
                view=self.store.read_json(ref) if ref else {"period":p,"industries":[]}
                row=next((r for r in view["industries"] if r["uid"]==uid),None)
                metrics=row["metrics"] if row else {k:metric(None,p["endpoint"] or p["as_of"],reason="PERIOD_DATA_GAP") for k in series["values"]}
                if row is None:view["period"].update(status="DATA_GAP",available_days=0,missing_dates=p["trade_dates"])
                rows.append({"trade_date":view["period"]["endpoint"] or key,"period":view["period"],"metrics":metrics})
        else:raise DataError("INVALID_PERIOD_KIND")
        return {"batch_id":batch,"uid":uid,"rows":rows}

    def members(self,batch: str,uid: str,*,period_kind="day",period_key=None) -> dict:
        m=self.store.published_manifest(batch);calendar=self.store.read_json(m["views"]["calendar"])
        p=self._period(m,period_kind,period_key,calendar)
        if uid not in m["views"]["history"]:raise DataError("INDUSTRY_NOT_FOUND")
        combined={};gap=False
        for day in p["trade_dates"]:
            if day not in m["days"]:gap=True;continue
            result=self.store.read_json(m["days"][day]["result"])
            industry=next((x for x in result["industries"] if x["uid"]==uid),None)
            if not industry or industry["metrics"]["flow_cent"]["value"] is None:gap=True
            for member in result["members"]:
                if member["uid"]!=uid:continue
                code=member["ts_code"]
                entry=combined.setdefault(code,{"last":None,"endpoint":None,"sum":0,"volume":0,"valid":True,"contribution_days":[]})
                entry["last"]=member
                if day==p["endpoint"]:entry["endpoint"]=member
                if member["flow_cent"] is None:entry["valid"]=False
                subtotal=member.get("known_subtotal",member["flow_cent"])
                volume=member.get("net_mf_vol_known_subtotal",member["net_mf_vol"])
                if subtotal is not None:
                    entry["sum"]+=int(subtotal);entry["volume"]+=int(volume or 0);entry["contribution_days"].append(day)
        rows=[]
        for entry in combined.values():
            member=deepcopy(entry["endpoint"] or entry["last"])
            member["is_endpoint_member"]=bool(entry["endpoint"] and entry["endpoint"]["is_endpoint_member"])
            if not entry["endpoint"]:
                for key in ("pe_ttm","pb","pe","ps_ttm","dv_ttm","total_mv","circ_mv","pe_peer_percentile","pb_peer_percentile"):member[key]=None
            member.update(flow_cent=str(entry["sum"]) if entry["valid"] and not gap else None,
                          net_mf_vol=str(entry["volume"]) if entry["valid"] and not gap else None,
                          known_subtotal=str(entry["sum"]) if entry["contribution_days"] else None,
                          net_mf_vol_known_subtotal=str(entry["volume"]) if entry["contribution_days"] else None,
                          contribution_days=entry["contribution_days"])
            rows.append(member)
        p.update(available_days=sum(d in m["days"] for d in p["trade_dates"]),missing_dates=[d for d in p["trade_dates"] if d not in m["days"]])
        return {"batch_id":batch,"uid":uid,"period":p,"rows":add_name_sort_keys(sorted(rows,key=lambda r:r["ts_code"]))}

    def quality(self,batch: str,*,period_kind="day",period_key=None) -> dict:
        m=self.store.published_manifest(batch)
        calendar=self.store.read_json(m["views"]["calendar"])
        p=self._period(m,period_kind,period_key,calendar)
        days=[];missing=[]
        for day in p["trade_dates"]:
            if day not in m["days"]:
                missing.append(day);continue
            result=self.store.read_json(m["days"][day]["result"])
            days.append({"trade_date":day,"captured_at":result.get("captured_at"),"audit":result["audit"],
                         "source_ref":m["days"][day]["input"]})
        p.update(available_days=len(days),missing_dates=missing)
        if missing:p["status"]="DATA_GAP"
        return {"batch_id":batch,"period":p,"days":days,"missing_dates":missing}
