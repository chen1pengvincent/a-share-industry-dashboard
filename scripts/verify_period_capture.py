"""Independent, offline audit of a named batch against frozen real inputs.

The oracle uses Fraction and Python's calendar only. QueryService is the system
under test; compute_day, aggregate_period and HistoryAccumulator are never
called to produce expected values. No provider/client or writer is constructed.
The output directory must be new and separate from all input directories.

Example:
  PYTHONPATH=src:.local/evidence/export-deps python3 -B scripts/verify_period_capture.py \
    --root .local/evidence/app-runtime --batch BATCH-... \
    --capture .local/evidence/history-001 --capture .local/evidence/live-current-214753 \
    --date 20260803 --date 20260804 --date 20260805 --date 20260806 --date 20260807 \
    --date 20260917 --output-dir .local/evidence/period-audit-001

Omit --date to verify every batch day. --period week:20260803 and
--period month:2026-08 can additionally select missing/unpublished periods.
"""
from __future__ import annotations

import argparse
import calendar
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from fractions import Fraction
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys
import platform

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from industry_workbench.query import QueryService
from industry_workbench.storage import FileStore


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    # The captured API JSON may contain numeric literals; preserve their decimal
    # digits rather than converting them to binary floats before the audit.
    return json.loads(raw, parse_float=str, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def number(value):
    if value is None or isinstance(value, bool) or str(value).strip() in ("", "--", "-", "None"):
        return None
    try:
        return Fraction(str(value).strip())
    except (ValueError, ZeroDivisionError):
        return None


def positive(value):
    result = number(value)
    return result if result is not None and result > 0 else None


def printable(value):
    if isinstance(value, Fraction):
        with localcontext() as context:
            context.prec = 90
            return format(Decimal(value.numerator) / Decimal(value.denominator), "f")
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return value


def median(values):
    ordered = sorted(values)
    return (ordered[(len(ordered)-1)//2] + ordered[len(ordered)//2]) / 2 if ordered else None


def parse_day(value):
    return datetime.strptime(value.replace("-", ""), "%Y%m%d").date()


def natural_period(kind, key, as_of, opens):
    cutoff = parse_day(as_of)
    if kind == "month":
        token = key.replace("-", "")
        base = parse_day(token + "01" if len(token) == 6 else token)
        start = base.replace(day=1)
        end = base.replace(day=calendar.monthrange(base.year, base.month)[1])
        canonical = start.strftime("%Y-%m")
    else:
        base = parse_day(key)
        start = base - timedelta(days=base.weekday()) if kind == "week" else base
        end = start + timedelta(days=6) if kind == "week" else start
        canonical = start.strftime("%Y%m%d")
    if kind not in {"day", "week", "month"} or start > cutoff:
        raise ValueError("INVALID_OR_FUTURE_AUDIT_PERIOD")
    effective = min(end, cutoff)
    trading = [day for day in opens if start.strftime("%Y%m%d") <= day <= effective.strftime("%Y%m%d")]
    return {"kind": kind, "key": canonical, "start": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"),
            "as_of": effective.strftime("%Y%m%d"), "endpoint": trading[-1] if trading else None,
            "trade_dates": trading, "expected_days": len(trading),
            "status": "NO_TRADING_DAYS" if not trading else "IN_PROGRESS" if end > cutoff else "COMPLETE"}


class FrozenReader:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.verified = set()

    def read(self, ref):
        sha = ref.get("sha256", "")
        if not re.fullmatch("[a-f0-9]{64}", sha) or ref.get("path") != f"objects/{sha[:2]}/{sha}.gz":
            raise ValueError("INVALID_FROZEN_OBJECT_REFERENCE")
        path = (self.root / ref["path"]).resolve()
        if not path.is_relative_to(self.root) or ref.get("compression") != "gzip":
            raise ValueError("FROZEN_OBJECT_PATH_ESCAPE")
        raw = gzip.decompress(path.read_bytes())
        if len(raw) != ref["bytes"] or digest(raw) != sha:
            raise ValueError("FROZEN_OBJECT_HASH_MISMATCH")
        self.verified.add(sha)
        return decode(raw)


class ReadOnlyStore(FileStore):
    def writer(self, *args, **kwargs):
        raise AssertionError("AUDITOR_MUST_NOT_WRITE_INPUT_STORE")

    def put_bytes(self, *args, **kwargs):
        raise AssertionError("AUDITOR_MUST_NOT_WRITE_INPUT_STORE")


class Audit:
    def __init__(self):
        self.checks = Counter()
        self.failures = []
        self.failure_count = 0
        self.failure_categories = Counter()
        self.failure_examples = defaultdict(list)

    def equal(self, category, identity, field, actual, expected):
        self.checks[category] += 1
        if actual != expected:
            self.failure_count += 1
            self.failure_categories[category] += 1
            failure = {"category": category, "identity": identity, "field": field,
                       "actual": printable(actual), "expected": printable(expected)}
            if len(self.failures) < 100:
                self.failures.append(failure)
            if len(self.failure_examples[category]) < 5:
                self.failure_examples[category].append(failure)

    def numeric(self, category, identity, field, actual, expected):
        self.equal(category, identity, field, number(actual), expected)


def eligible(stock, day):
    return bool(re.fullmatch(r"(?:6\d{5}\.SH|[03]\d{5}\.SZ)", stock["ts_code"])) and (
        not stock.get("list_date") or stock["list_date"] <= day) and (
        not stock.get("delist_date") or day < stock["delist_date"])


def verify_successor(day, inputs, derivation, reader, captured_supplements, audit):
    """Check the close-only transformation without importing its composer."""
    audit.equal("derivation", day, "transform", derivation.get("transform"), "append-official-ci-ths-close-v1")
    audit.equal("derivation", day, "trade_date", derivation.get("trade_date"), day)
    parent = reader.read(derivation["parent_input"])
    for field in set(parent) | set(inputs):
        if field not in {"official", "audit", "source_refs"}:
            audit.equal("derivation_preserved", day, field, inputs.get(field), parent.get(field))
    # Preserve all old audit diagnostics except the separately sourced close
    # availability record; unknown membership must never be cured by a quote.
    def without_close(value):
        result = json.loads(json.dumps(value))
        for tax in ("CI", "THS"):
            result.get("classifications", {}).get(tax, {}).pop("daily_close", None)
        return result
    audit.equal("derivation_preserved", day, "audit_without_close", without_close(inputs["audit"]), without_close(parent["audit"]))
    identities = {row["uid"]: row for row in parent["industries"]}
    expected_official = {row["uid"]: row for row in parent["official"] if identities[row["uid"]]["taxonomy"] not in {"CI", "THS"}}
    expected_sources = list(parent["source_refs"])
    audit.equal("derivation", day, "supplement_taxonomies", set(derivation["supplement_refs"]), {"CI", "THS"})
    for tax, ref in derivation["supplement_refs"].items():
        extra = reader.read(ref)
        if (day, tax) in captured_supplements:
            origin, original_ref = captured_supplements[day, tax]
            audit.equal("derivation_capture", day, tax + "_sha256", ref["sha256"], original_ref["sha256"])
            origin.read(original_ref)
        audit.equal("derivation", day, tax + "_status", extra.get("status"), "SUCCEEDED")
        audit.equal("derivation", day, tax + "_date", extra["trade_date"], day)
        audit.equal("derivation", day, tax + "_fields", extra["fields"], ["ts_code", "trade_date", "close"])
        quotes = {row["ts_code"]: row for row in extra["rows"]}
        for uid, industry in identities.items():
            if industry["taxonomy"] == tax and industry["code"] in quotes:
                quote = quotes[industry["code"]]
                audit.equal("derivation", day, uid + "_quote_date", quote["trade_date"], day)
                expected_official[uid] = {"uid": uid, "trade_date": day, "close": quote["close"], "pe": None, "pb": None}
        expected_sources.extend(extra["source_refs"])
    actual_official = {row["uid"]: row for row in inputs["official"]}
    audit.equal("derivation", day, "official_uid_set", set(actual_official), set(expected_official))
    for uid, row in expected_official.items():
        for field in ("pe", "pb", "close"):
            audit.numeric("derivation_close", f"{day}/{uid}", field, actual_official.get(uid, {}).get(field), number(row.get(field)))
    source_identity = lambda ref: (ref["sha256"], ref.get("request", {}).get("request_key"))
    audit.equal("derivation", day, "exact_raw_source_union", {source_identity(ref) for ref in inputs["source_refs"]},
                {source_identity(ref) for ref in expected_sources})


def independent_day(inputs, reader, audit):
    day = inputs["trade_date"]
    stocks = {row["ts_code"]: row for row in inputs["stocks"]}
    pool = {code for code, stock in stocks.items() if eligible(stock, day)}
    basics = {row["ts_code"]: row for row in inputs["daily_basic"]}
    flows = {row["ts_code"]: row for row in inputs["moneyflow"]}
    traded = {row["ts_code"] for row in inputs["daily"] if row["ts_code"] in pool and positive(row["vol"]) is not None}
    audit.equal("base", day, "flow_matches_traded", set(flows), traded)
    audit.equal("base", day, "base_complete", inputs["audit"]["base_complete"], True)
    audit.equal("base", day, "provider_kind", inputs["provider_kind"], "LIVE_SECURE_TUSHARE")
    money, volumes = {}, {}
    for code, row in flows.items():
        cents, volume = number(row["net_mf_amount"]) * 100, number(row["net_mf_vol"])
        if cents.denominator != 1 or volume is None or volume.denominator != 1:
            raise ValueError("INVALID_FROZEN_MONEY_PRECISION")
        money[code], volumes[code] = int(cents), int(volume)
    # Check the decimal values used by the oracle against the actual recorded
    # API response. This avoids auditing only a mistaken normalization layer.
    raw_official = {}
    for api, normalized, fields in (("daily_basic", basics, ("pe_ttm", "pb")),
                                    ("moneyflow", flows, ("net_mf_amount", "net_mf_vol"))):
        refs = [ref for ref in inputs["source_refs"] if ref.get("request", {}).get("api_name") == api
                and ref["request"].get("params", {}).get("trade_date") == day]
        audit.equal("raw", day, api + "_response_present", bool(refs), True)
        raw_rows = {}
        for ref in refs:
            payload = reader.read(ref)
            audit.equal("raw", day, api + "_return_code", payload.get("code"), 0)
            table = payload["data"]
            for cells in table["items"]:
                row = dict(zip(table["fields"], cells))
                if row["ts_code"] in pool:
                    audit.equal("raw_dates", f"{day}/{row['ts_code']}", api + "_raw_date", row.get("trade_date"), day)
                    raw_rows[row["ts_code"]] = row
        audit.equal("raw", day, api + "_normalized_set", set(normalized), set(raw_rows))
        for code, row in normalized.items():
            audit.equal("raw_dates", f"{day}/{code}", api, row["trade_date"], day)
            for field in fields:
                audit.numeric("raw_values", f"{day}/{code}", api + "." + field, row.get(field), number(raw_rows.get(code, {}).get(field)))
    for ref in inputs["source_refs"]:
        request = ref.get("request", {})
        if request.get("api_name") in {"sw_daily", "tdx_daily", "ci_daily", "ths_daily"} and request.get("params", {}).get("trade_date") == day:
            payload = reader.read(ref)
            for cells in payload["data"]["items"]:
                row = dict(zip(payload["data"]["fields"], cells))
                raw_official[row["ts_code"]] = row
    identities = {row["uid"]: row for row in inputs["industries"]}
    official = {row["uid"]: row for row in inputs.get("official", [])}
    assignments = defaultdict(dict)
    for row in inputs["memberships"]:
        uid, code = row["uid"], row["ts_code"]
        if code in pool:
            assignments[uid][code] = (row["state"], row["evidence_kind"])
            if row["state"] == "ACTIVE" and row["evidence_kind"] == "OFFICIAL_DATED":
                audit.equal("membership_time", f"{day}/{uid}/{code}", "not_before_entry", not row.get("in_date") or day >= row["in_date"], True)
                audit.equal("membership_time", f"{day}/{uid}/{code}", "not_after_exit", not row.get("out_date") or day < row["out_date"], True)
            if row["evidence_kind"] == "OBSERVED_SAME_DAY":
                observed = inputs["audit"].get("classifications", {}).get("THS", {}).get("observed_date")
                audit.equal("membership_time", f"{day}/{uid}/{code}", "observation_date", observed, day)
    unresolved_lifecycle = {row["uid"] for row in inputs["memberships"]
                            if row["ts_code"].endswith((".SH", ".SZ")) and row["ts_code"] not in stocks}
    truth = {}
    for uid, identity in identities.items():
        members = assignments[uid]
        active = frozenset(code for code, state in members.items() if state[0] == "ACTIVE")
        unknown = any(state[0] != "ACTIVE" for state in members.values())
        blocked = identity["taxonomy"] in inputs["unknown_taxonomies"] or unknown or uid in unresolved_lifecycle
        pe = [v for code in active if (v := positive(basics.get(code, {}).get("pe_ttm"))) is not None]
        pb = [v for code in active if (v := positive(basics.get(code, {}).get("pb"))) is not None]
        received = len(active & basics.keys())
        allowed = bool(active) and not blocked and received == len(active)
        evidence = {state[1] for state in members.values()}
        values = {"pe_ttm_median": median(pe) if allowed else None, "pb_median": median(pb) if allowed else None,
                  "flow_cent": sum(money.get(code, 0) for code in active) if active and not blocked else None,
                  "net_mf_vol": sum(volumes.get(code, 0) for code in active) if active and not blocked else None}
        for key, field in (("official_pe", "pe"), ("official_pb", "pb"), ("close", "close")):
            values[key] = positive(official.get(uid, {}).get(field))
            if uid in official:
                audit.equal("official_dates", f"{day}/{uid}", field, official[uid].get("trade_date"), day)
                code = identity.get("market_code") or identity.get("membership_code") or identity["code"]
                audit.numeric("official_raw", f"{day}/{uid}", field, official[uid].get(field), number(raw_official.get(code, {}).get(field)))
            if identity["taxonomy"] in {"THS", "CI"} and key.startswith("official_"):
                audit.equal("official_separation", f"{day}/{uid}", key, values[key], None)
        truth[uid] = {"identity": identity, "values": values, "active": active, "members": members, "blocked": blocked,
                      "evidence": next(iter(evidence)) if len(evidence) == 1 else "UNKNOWN",
                      "subtotal": {"flow_cent": sum(money.get(code, 0) for code in active),
                                   "net_mf_vol": sum(volumes.get(code, 0) for code in active)},
                      "counts": {"member_count": len(active), "unknown_member_count": sum(state[0] != "ACTIVE" for state in members.values()),
                                 "basic_received": received, "pe_valid": len(pe), "pb_valid": len(pb),
                                 "flow_expected": len(active & traded), "flow_received": len(active & traded)}}
    ths = inputs["audit"].get("classifications", {}).get("THS", {})
    if ths.get("membership_state") == "HISTORY_UNAVAILABLE":
        for uid, row in truth.items():
            if row["identity"]["taxonomy"] == "THS":
                audit.equal("ths_no_backdating", f"{day}/{uid}", "no_historical_members", len(row["members"]), 0)
                for field in ("pe_ttm_median", "pb_median", "flow_cent"):
                    audit.equal("ths_no_backdating", f"{day}/{uid}", field, row["values"][field], None)
    return {"industries": truth, "money": money, "volumes": volumes,
            "basics": {code: {key: number(row.get(key)) for key in ("pe_ttm", "pb")} for code, row in basics.items()},
            "trade_date": day, "unknown_taxonomies": inputs["unknown_taxonomies"]}


def period_truth(period, days, fallback):
    available = [day for day in period["trade_dates"] if day in days]
    identities = {}
    for day in available:
        identities.update(days[day]["industries"])
    if not identities:
        identities = fallback
    result = {}
    for uid, identity in identities.items():
        endpoint = days.get(period["endpoint"], {}).get("industries", {}).get(uid)
        values = {field: endpoint["values"][field] if endpoint else None for field in identity["values"]}
        subtotals, missing = {}, {}
        for field in ("flow_cent", "net_mf_vol"):
            daily = [days.get(day, {}).get("industries", {}).get(uid) for day in period["trade_dates"]]
            missing[field] = [day for day, row in zip(period["trade_dates"], daily) if row is None or row["values"][field] is None]
            values[field] = sum(row["values"][field] for row in daily) if daily and not missing[field] else None
            subtotals[field] = sum(row["subtotal"][field] for row in daily if row)
        result[uid] = {"values": values, "subtotal": subtotals, "missing": missing, "endpoint": endpoint,
                       "identity": identity["identity"]}
    return result


def verify_members(query, batch, period, uid, truth, days, audit):
    response = query.members(batch, uid, period_kind=period["kind"], period_key=period["key"])
    audit.equal("member_batch", uid, "batch", response["batch_id"], batch)
    codes = set()
    for day in period["trade_dates"]:
        codes.update(days.get(day, {}).get("industries", {}).get(uid, {}).get("members", {}))
    rows = {row["ts_code"]: row for row in response["rows"]}
    identity = f"{period['kind']}:{period['key']}/{uid}"
    audit.equal("member_identity", identity, "union_of_daily_members", set(rows), codes)
    subtotal_sum = 0
    for code in codes:
        known, volume, valid, has_known = 0, 0, True, False
        for day in period["trade_dates"]:
            snapshot = days.get(day)
            industry = (snapshot or {}).get("industries", {}).get(uid)
            if not industry:
                continue
            assignment = industry["members"].get(code)
            if assignment:
                if assignment[0] == "ACTIVE":
                    has_known = True
                    known += snapshot["money"].get(code, 0)
                    volume += snapshot["volumes"].get(code, 0)
                if assignment[0] != "ACTIVE" or industry["blocked"]:
                    valid = False
        expected_flow = known if valid and not truth["missing"]["flow_cent"] else None
        endpoint = days.get(period["endpoint"], {})
        assignment = endpoint.get("industries", {}).get(uid, {}).get("members", {}).get(code)
        row = rows.get(code, {})
        audit.numeric("member_subtotal", identity + "/" + code, "known_subtotal", row.get("known_subtotal"), known if has_known else None)
        audit.numeric("member_subtotal", identity + "/" + code, "net_mf_vol_known_subtotal", row.get("net_mf_vol_known_subtotal"), volume if has_known else None)
        audit.numeric("member_flow", identity + "/" + code, "flow_cent", row.get("flow_cent"), expected_flow)
        audit.equal("member_endpoint", identity + "/" + code, "is_endpoint_member", row.get("is_endpoint_member"), bool(assignment and assignment[0] == "ACTIVE"))
        for field in ("pe_ttm", "pb"):
            estimate = endpoint.get("basics", {}).get(code, {}).get(field) if assignment else None
            audit.numeric("member_endpoint", identity + "/" + code, field, row.get(field), estimate)
        subtotal_sum += int(number(row.get("known_subtotal")) or 0)
    audit.equal("member_reconciliation", identity, "sum_equals_industry_known_subtotal", subtotal_sum, truth["subtotal"]["flow_cent"])
    return {"uid": uid, "member_count": len(codes), "known_subtotal": str(truth["subtotal"]["flow_cent"])}


def run(args, output):
    audit = Audit()
    runtime = Path(args.root).resolve()
    store = ReadOnlyStore(runtime, development=".local" in runtime.parts and "evidence" in runtime.parts)
    before_pointer = (runtime / "current.json").read_bytes() if (runtime / "current.json").exists() else b""
    manifest = store.published_manifest(args.batch) if args.batch else store.current()
    if not manifest:
        raise ValueError("PUBLISHED_BATCH_REQUIRED")
    batch = manifest["batch_id"]
    audit.equal("batch_identity", batch, "as_of_equals_latest_day", manifest["as_of"], max(manifest["days"]))
    manifest_path = runtime / "batches" / batch / "manifest.json"
    manifest_hash = digest(manifest_path.read_bytes())
    module_paths = [PROJECT / "src/industry_workbench" / name for name in ("query.py", "periods.py", "metrics.py", "models.py", "storage.py", "sorting.py")]
    source_before = {str(path.relative_to(PROJECT)): digest(path.read_bytes()) for path in module_paths}
    reader = FrozenReader(runtime)
    calendar_rows = reader.read(manifest["views"]["calendar"])
    opens = sorted(row if isinstance(row, str) else row["cal_date"] for row in calendar_rows if isinstance(row, str) or str(row["is_open"]) == "1")
    captured, captured_supplements, receipts = {}, {}, []
    for directory in args.capture:
        directory = Path(directory).resolve()
        raw = (directory / "capture_receipt.json").read_bytes()
        receipt = decode(raw)
        records = receipt.get("days") or {receipt["trade_date"]: {"input": receipt["input"]}}
        origin = FrozenReader(directory)
        frozen = directory / "source_snapshot"
        for name, sha in receipt.get("source", {}).get("files", {}).items():
            path = (frozen / name).resolve()
            if not path.is_relative_to(frozen.resolve()):
                raise ValueError("CAPTURE_SOURCE_PATH_ESCAPE")
            audit.equal("capture_source", str(directory), name, digest(path.read_bytes()), sha)
        receipts.append({"directory": str(directory), "receipt_sha256": digest(raw), "days_at_audit_start": sorted(records),
                         "verified_source_files": len(receipt.get("source", {}).get("files", {}))})
        for day, refs in records.items():
            if "supplements" in refs:
                for tax, ref in refs["supplements"].items():
                    captured_supplements[day, tax] = (origin, ref)
                continue
            if day in captured and captured[day][1]["sha256"] != refs["input"]["sha256"]:
                raise ValueError("CONFLICTING_CAPTURE_DAY")
            captured[day] = (origin, refs["input"])
    requested = sorted(set(args.date or manifest["days"]))
    dataset, percentile_counts = {}, {}
    history = defaultdict(list)
    for index, (day, refs) in enumerate(sorted(manifest["days"].items()), 1):
        inputs = reader.read(refs["input"])
        audit.equal("capture_identity", day, "trade_date", inputs["trade_date"], day)
        if day in captured:
            origin, captured_ref = captured[day]
            parent_ref = refs.get("input_derivation", {}).get("parent_input") or refs["input"]
            audit.equal("capture_identity", day, "parent_input_hash", parent_ref["sha256"], captured_ref["sha256"])
            origin.read(captured_ref)
        if refs.get("input_derivation"):
            verify_successor(day, inputs, refs["input_derivation"], reader, captured_supplements, audit)
        dataset[day] = independent_day(inputs, reader, audit)
        for uid, row in dataset[day]["industries"].items():
            counts = {}
            for field, target in (("pe_ttm_median", "pe_percentile"), ("pb_median", "pb_percentile"),
                                  ("official_pe", "official_pe_percentile"), ("official_pb", "official_pb_percentile")):
                key = (uid, field, "OFFICIAL" if field.startswith("official_") else row["evidence"])
                value = row["values"][field]
                if value is not None:
                    history[key].append(value)
                counts[target] = len(history[key])
            percentile_counts[day, uid] = counts
        print(f"independent day {index}/{len(manifest['days'])}: {day}", flush=True)
    query = QueryService(store)
    periods = {(kind, natural_period(kind, day, manifest["as_of"], opens)["key"])
               for day in requested for kind in ("day", "week", "month")}
    for option in args.period:
        kind, key = option.split(":", 1)
        periods.add((kind, natural_period(kind, key, manifest["as_of"], opens)["key"]))
    reports, changes, anti_end_basket = [], [], []
    fallback = dataset[max(dataset)]["industries"]
    for kind, key in sorted(periods):
        period = natural_period(kind, key, manifest["as_of"], opens)
        expected = period_truth(period, dataset, fallback)
        actual = {}
        missing = [day for day in period["trade_dates"] if day not in dataset]
        expected_status = "DATA_GAP" if missing else period["status"]
        scopes = sorted({(row["identity"]["taxonomy"], row["identity"]["level"]) for row in expected.values()})
        for taxonomy, level in scopes:
            response = query.industries(batch, taxonomy=taxonomy, level_or_series=level, period_kind=kind, period_key=key)
            audit.equal("query_batch", f"{kind}:{key}/{taxonomy}/{level}", "batch_id", response["batch_id"], batch)
            audit.equal("query_batch", f"{kind}:{key}/{taxonomy}/{level}", "as_of", response["as_of"], manifest["as_of"])
            for field in ("key", "start", "end", "as_of", "endpoint", "trade_dates", "expected_days"):
                audit.equal("calendar", f"{kind}:{key}", field, response["period"].get(field), period[field])
            audit.equal("calendar", f"{kind}:{key}", "status", response["period"]["status"], expected_status)
            audit.equal("calendar", f"{kind}:{key}", "missing_dates", response["period"].get("missing_dates"), missing)
            audit.equal("query_sources", f"{kind}:{key}", "source_days", set(response["source_refs"]), set(period["trade_dates"]) & dataset.keys())
            for day, source in response["source_refs"].items():
                audit.equal("query_sources", f"{kind}:{key}/{day}", "input_sha256", source["sha256"], manifest["days"][day]["input"]["sha256"])
            actual.update({row["uid"]: row for row in response["rows"]})
        audit.equal("industry_identity", f"{kind}:{key}", "all_scope_union", set(actual), set(expected))
        row_coverage = Counter()
        changed_uids = []
        for uid, oracle in expected.items():
            row = actual.get(uid, {})
            endpoint = oracle["endpoint"]
            for field, value in oracle["values"].items():
                metric = row.get("metrics", {}).get(field, {})
                audit.numeric("period_values", f"{kind}:{key}/{uid}", field, metric.get("value"), value)
                audit.equal("metric_dates", f"{kind}:{key}/{uid}", field, metric.get("metric_date"), period["endpoint"] or period["as_of"])
                status = "NA" if value is None else "SMALL_SAMPLE" if field in {"pe_ttm_median", "pb_median"} and endpoint["counts"]["pe_valid" if field == "pe_ttm_median" else "pb_valid"] < 5 else "OK"
                audit.equal("metric_status", f"{kind}:{key}/{uid}", field, metric.get("status"), status)
            for field in ("flow_cent", "net_mf_vol"):
                metric = row.get("metrics", {}).get(field, {})
                audit.numeric("period_subtotals", f"{kind}:{key}/{uid}", field, metric.get("known_subtotal"), oracle["subtotal"][field])
                if kind != "day":
                    audit.equal("period_missing", f"{kind}:{key}/{uid}", field, metric.get("missing_dates"), oracle["missing"][field])
            if endpoint:
                for field, count in endpoint["counts"].items():
                    audit.equal("sample_denominators", f"{kind}:{key}/{uid}", field, row.get("counts", {}).get(field), count)
                for field, count in percentile_counts[period["endpoint"], uid].items():
                    metric = row["metrics"].get(field, {})
                    audit.equal("history_sample_count", f"{kind}:{key}/{uid}", field, metric.get("valid_count"), count)
                    if count < 252:
                        audit.equal("history_under_252", f"{kind}:{key}/{uid}", field, metric.get("value"), None)
            row_coverage[(oracle["identity"]["taxonomy"], "numeric_flow" if oracle["values"]["flow_cent"] is not None else "NA_flow")] += 1
            baskets = [dataset[day]["industries"].get(uid, {}).get("active", frozenset()) for day in period["trade_dates"] if day in dataset]
            if baskets and any(basket != baskets[0] for basket in baskets[1:]):
                changed_uids.append(uid)
                if len(changes) < 30:
                    changes.append({"period": f"{kind}:{key}", "uid": uid,
                                    "members_per_available_day": [len(basket) for basket in baskets]})
                if endpoint and not missing and oracle["values"]["flow_cent"] is not None:
                    wrong = sum(dataset[day]["money"].get(code, 0) for day in period["trade_dates"] for code in endpoint["active"])
                    if wrong != oracle["values"]["flow_cent"] and len(anti_end_basket) < 20:
                        anti_end_basket.append({"period": f"{kind}:{key}", "uid": uid,
                                               "daily_membership_flow_cent": str(oracle["values"]["flow_cent"]),
                                               "incorrect_endpoint_basket_flow_cent": str(wrong)})
        # Every industry's aggregate is checked. Member-detail API traversal is
        # sampled explicitly, prioritizing changes and nonzero incomplete data.
        candidates = []
        for taxonomy in ("SW", "TDX", "CI", "THS"):
            scoped = [uid for uid, row in expected.items() if row["identity"]["taxonomy"] == taxonomy]
            preferred = [uid for uid in changed_uids if uid in scoped]
            preferred += [uid for uid in scoped if expected[uid]["values"]["flow_cent"] is None and expected[uid]["subtotal"]["flow_cent"]]
            candidates += (preferred or scoped)[:1]
        candidates += changed_uids
        selected = list(dict.fromkeys(candidates))[:args.member_samples] if kind != "day" or key == manifest["as_of"] else []
        member_reports = [verify_members(query, batch, period, uid, expected[uid], dataset, audit)
                          for uid in selected if uid in manifest["views"]["history"]]
        reports.append({"period": period, "available_days": len(period["trade_dates"]) - len(missing), "missing_dates": missing,
                        "industry_count": len(expected), "coverage": {f"{tax}/{state}": count for (tax, state), count in sorted(row_coverage.items())},
                        "changed_industries": len(changed_uids), "member_detail_samples": member_reports})
        print(f"queried {kind}:{key}: {len(expected)} industries, failures so far {audit.failure_count}", flush=True)
    source_after = {str(path.relative_to(PROJECT)): digest(path.read_bytes()) for path in module_paths}
    audit.equal("immutability", batch, "query_implementation_unchanged", source_after, source_before)
    audit.equal("immutability", batch, "manifest_unchanged", digest(manifest_path.read_bytes()), manifest_hash)
    report = {"status": "PASS" if not audit.failure_count else "FAIL", "scope": "FROZEN_REAL_BATCH_PERIOD_NUMERICAL_ACCEPTANCE",
              "batch_id": batch, "as_of": manifest["as_of"], "runtime": str(runtime), "manifest_sha256": manifest_hash,
              "batch_dates": sorted(dataset), "requested_dates": requested, "capture_receipts": receipts,
              "captures_not_materialized": sorted(set(captured) - dataset.keys()), "verified_object_count": len(reader.verified),
              "checks": dict(audit.checks), "failure_count": audit.failure_count, "first_failures": audit.failures,
              "failure_categories": dict(audit.failure_categories), "failure_examples_by_category": dict(audit.failure_examples),
              "periods": reports, "membership_change_examples": changes, "endpoint_basket_negative_controls": anti_end_basket,
              "complete_closed_weeks_months": [f"{row['period']['kind']}:{row['period']['key']}" for row in reports
                                               if row["period"]["kind"] != "day" and row["period"]["status"] == "COMPLETE" and not row["missing_dates"]],
              "periods_with_missing_trade_dates": [f"{row['period']['kind']}:{row['period']['key']}" for row in reports if row["missing_dates"]],
              "history_percentile_live_boundary_verified": False,
              "maximum_actual_valid_history_samples": max((len(values) for values in history.values()), default=0),
              "implementation_sha256": source_before, "script_sha256": digest(Path(__file__).read_bytes()),
              "artifact_source_identity": {key: manifest["source"].get(key) for key in ("commit", "git_dirty", "tree_sha256")},
              "query_modules_match_artifact_source": {name: sha == manifest["source"].get("files", {}).get(name) for name, sha in source_before.items()},
              "current_pointer_changed_concurrently": digest((runtime/"current.json").read_bytes()) != digest(before_pointer) if (runtime/"current.json").exists() else bool(before_pointer),
              "environment": {"python": sys.version, "platform": platform.platform(), "executable": sys.executable},
              "limitations": ["所有期望数值独立计算，但成员状态以冻结输入的归属证据为前提；不宣称独立证明供应商历史成员完整性。",
                              "成员明细 API 为报告列明的抽样；行业日/周/月估值与资金逐行业核对。",
                              "真实捕获不足252个有效日，不能宣称已真实验收252样本的历史百分位数值边界；本次仅验证不足252留空及样本数无前视。",
                              "未物化的捕获日不作为应用批次可用日；NA和已知小计不等于完整行业资金。",
                              "无网络、无新行情采集、无正式数据写入；这是开发验收证据，不是生产发布或投资结论。"],
              "created_at": datetime.now(timezone.utc).isoformat()}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, help="已物化批次的只读运行目录")
    parser.add_argument("--batch", help="显式批次；省略时只在启动时解引用一次 current")
    parser.add_argument("--capture", action="append", default=[], help="可重复的原始捕获目录")
    parser.add_argument("--date", action="append", default=[], help="可重复的日期 YYYYMMDD；默认全部批次日")
    parser.add_argument("--period", action="append", default=[], help="额外周期，例如 month:2026-08")
    parser.add_argument("--member-samples", type=int, default=4, help="每个周/月的成员明细样本行业数")
    parser.add_argument("--output-dir", required=True, help="新的独立验收证据目录，不得覆盖既有报告")
    args = parser.parse_args()
    output = Path(args.output_dir).resolve()
    if not 0 <= args.member_samples <= 30:
        parser.error("member-samples must be between 0 and 30")
    for path in [Path(args.root).resolve()] + [Path(value).resolve() for value in args.capture]:
        if output.is_relative_to(path) or path.is_relative_to(output):
            parser.error("output directory must be separate from every input directory")
    if not output.is_relative_to(PROJECT / ".local/evidence"):
        parser.error("output must be an independent directory under this project's .local/evidence")
    output.mkdir(parents=True, exist_ok=False)
    (output / "auditor_source.py").write_bytes(Path(__file__).read_bytes())
    try:
        report = run(args, output)
    except Exception as exc:
        report = {"status": "ERROR", "error_type": type(exc).__name__, "error": str(exc), "command": sys.argv}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=printable, allow_nan=False) + "\n")
    lines = ["# 冻结真实批次周期独立验收", "", f"结果：{report['status']}", "", f"批次：{report.get('batch_id', args.batch)}", "",
             f"检查数：{sum(report.get('checks', {}).values())}；差异数：{report.get('failure_count', '见错误详情')}。", "",
             "本报告对应 report.json 中明确固定的批次、捕获日期及源码哈希。", ""]
    lines += ["- " + item for item in report.get("limitations", [])]
    (output / "README.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"status": report["status"], "batch_id": report.get("batch_id"), "failure_count": report.get("failure_count"),
                      "checks": sum(report.get("checks", {}).values()), "report": str(output / "report.json"),
                      "error": report.get("error")}, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
