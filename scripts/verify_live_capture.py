"""Offline independent arithmetic audit of one explicit successful capture.

Reads immutable FileStore objects only. Never imports the implementation's
compute_day, median, ranking, provider, or taxonomy functions; never calls APIs.
Writes a new audit directory and refuses to overwrite an earlier report.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path

from industry_workbench.storage import FileStore
from industry_workbench.validation import audit_day


def exact(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    number = Decimal(str(value))
    return Fraction(number) if number.is_finite() else None


def positive(value):
    number = exact(value)
    return number if number is not None and number > 0 else None


def decimal_string(value):
    if value is None:
        return None
    if isinstance(value, int) or value.denominator == 1:
        return str(int(value))
    with localcontext() as context:
        context.prec = max(80, len(str(abs(value.numerator))) + len(str(value.denominator)) + 10)
        text = format(Decimal(value.numerator) / Decimal(value.denominator), "f")
    return text.rstrip("0").rstrip(".")


def independent_median(values):
    ordered = sorted(values)
    if not ordered:
        return None
    return (ordered[(len(ordered) - 1) // 2] + ordered[len(ordered) // 2]) / 2


def a_share(code):
    return bool(re.fullmatch(r"(?:6\d{5}\.SH|[03]\d{5}\.SZ)", str(code)))


def eligible(stock, day):
    return a_share(stock["ts_code"]) and stock["list_date"] <= day and (not stock.get("delist_date") or day < stock["delist_date"])


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def audit_capture(capture: Path):
    receipt_path = capture / "capture_receipt.json"
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    if receipt.get("status") != "SUCCEEDED" or receipt.get("provider_kind") != "LIVE_SECURE_TUSHARE":
        raise ValueError("SUCCESSFUL_LIVE_CAPTURE_REQUIRED")
    store = FileStore(capture, development=True)
    inputs = store.read_json(receipt["input"])
    result = store.read_json(receipt["result"])
    day = inputs["trade_date"]
    if day != receipt["trade_date"] or day != result["trade_date"]:
        raise ValueError("CAPTURE_DATE_MISMATCH")
    publication_gate = audit_day(inputs, result)
    frozen_verified = 0
    for name, expected in receipt["source"]["files"].items():
        path = capture / "source_snapshot" / name
        if not path.resolve().is_relative_to((capture / "source_snapshot").resolve()):
            raise ValueError("FROZEN_SOURCE_PATH_ESCAPE")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("FROZEN_SOURCE_HASH_MISMATCH")
        frozen_verified += 1

    failures = []
    checks = Counter()

    def check(category, identity, field, actual, expected):
        checks[category] += 1
        if actual != expected:
            failures.append({"category": category, "identity": identity, "field": field,
                             "actual": actual, "expected": expected})

    stocks = {row["ts_code"]: row for row in inputs["stocks"]}
    industries = {row["uid"]: row for row in inputs["industries"]}
    actual_rows = {row["uid"]: row for row in result["industries"]}
    basics = {row["ts_code"]: row for row in inputs["daily_basic"]}
    raw_flow = {row["ts_code"]: row for row in inputs["moneyflow"]}
    official = {row["uid"]: row for row in inputs["official"]}
    memberships = defaultdict(list)
    for row in inputs["memberships"]:
        memberships[row["uid"]].append(row)
    check("identity", "industries", "unique_uids", len(actual_rows), len(result["industries"]))
    check("identity", "industries", "exact_input_set", sorted(actual_rows), sorted(industries))
    check("identity", "stocks", "unique_codes", len(stocks), len(inputs["stocks"]))
    pool = {code for code, stock in stocks.items() if eligible(stock, day)}
    traded = set()
    for row in inputs["daily"]:
        vol, amount = exact(row["vol"]), exact(row["amount"])
        if vol is None or amount is None or vol < 0 or amount < 0 or (vol > 0) != (amount > 0):
            raise ValueError("INVALID_ACTIVITY_IN_FROZEN_INPUT")
        if vol > 0 and row["ts_code"] in pool:
            traded.add(row["ts_code"])
    check("base", day, "flow_exact_traded_set", sorted(raw_flow), sorted(traded))
    check("base", day, "basic_covers_all_traded", sorted(traded - set(basics)), [])
    money, volume = {}, {}
    for code, raw in raw_flow.items():
        cent = exact(raw["net_mf_amount"]) * 100
        vol = exact(raw["net_mf_vol"])
        if cent.denominator != 1 or vol is None or vol.denominator != 1:
            raise ValueError("INVALID_MONEY_OR_VOLUME_PRECISION")
        money[code], volume[code] = int(cent), int(vol)
    check("base", day, "eligible_count", inputs["audit"]["eligible_count"], len(pool))
    check("base", day, "traded_count", inputs["audit"]["traded_count"], len(traded))
    check("base", day, "market_flow_cent", result["audit"].get("market_flow_cent"), str(sum(money.values())))

    tax_covered = defaultdict(set)
    group_covered = defaultdict(set)
    for uid, records in memberships.items():
        for member in records:
            tax_covered[industries[uid]["taxonomy"]].add(member["ts_code"])
            group_covered[(industries[uid]["taxonomy"], industries[uid]["level"])].add(member["ts_code"])
    missing = {tax: sorted(traded - tax_covered[tax]) for tax in ("SW", "THS", "TDX", "CI")}
    expected_global_unknown = {tax for tax in ("SW", "THS", "CI") if missing[tax]}
    if any(traded - group_covered[("TDX", series)] for series in ("880", "881")):
        expected_global_unknown.add("TDX")
    check("classification", day, "unknown_taxonomies", sorted(inputs["unknown_taxonomies"]), sorted(expected_global_unknown))

    detail = []
    expected_industries = {}
    expected_members = {}
    eligible_groups = defaultdict(list)
    for uid, identity in sorted(industries.items()):
        actual = actual_rows[uid]
        applicable = [m for m in memberships[uid] if m["ts_code"] in pool]
        active = [m for m in applicable if m["state"] == "ACTIVE"]
        unresolved = [m for m in applicable if m["state"] != "ACTIVE"]
        active_codes = {m["ts_code"] for m in active}
        check("identity", uid, "unique_active_members", len(active), len(active_codes))
        unrecognized = any(a_share(m["ts_code"]) and m["ts_code"] not in stocks for m in memberships[uid])
        unavailable = identity["taxonomy"] in expected_global_unknown or bool(unresolved) or unrecognized
        received = len(active_codes & set(basics))
        estimates = {field: [value for code in active_codes if (value := positive(basics.get(code, {}).get(field))) is not None]
                     for field in ("pe_ttm", "pb")}
        expected = {
            "pe_ttm_median": independent_median(estimates["pe_ttm"]) if active and not unavailable and received == len(active) else None,
            "pb_median": independent_median(estimates["pb"]) if active and not unavailable and received == len(active) else None,
            "flow_cent": sum(money.get(code, 0) for code in active_codes) if active and not unavailable else None,
            "net_mf_vol": sum(volume.get(code, 0) for code in active_codes) if active and not unavailable else None,
        }
        for name, source in (("official_pe", "pe"), ("official_pb", "pb"), ("close", "close")):
            expected[name] = positive(official.get(uid, {}).get(source))
        expected_industries[uid] = expected
        for name, value in expected.items():
            recorded = actual["metrics"][name]
            check("industry_values", uid, name, decimal_string(exact(recorded["value"])), decimal_string(value))
            expected_status = "NA" if value is None else "SMALL_SAMPLE" if name in {"pe_ttm_median", "pb_median"} and len(estimates["pe_ttm" if name == "pe_ttm_median" else "pb"]) < 5 else "OK"
            check("industry_status", uid, name, recorded["status"], expected_status)
            check("industry_dates", uid, name, recorded["metric_date"], day)
        counts = {"member_count": len(active), "unknown_member_count": len(unresolved), "basic_received": received,
                  "pe_valid": len(estimates["pe_ttm"]), "pb_valid": len(estimates["pb"]),
                  "flow_expected": len(active_codes & traded), "flow_received": len(active_codes & traded)}
        for name, value in counts.items():
            check("industry_counts", uid, name, actual["counts"].get(name), value)
        if expected["flow_cent"] is not None:
            eligible_groups[(identity["taxonomy"], identity["level"])].append(uid)
        detail.append({"uid": uid, "taxonomy": identity["taxonomy"], "level": identity["level"], "name": identity["name"],
                       "expected": {name: decimal_string(value) for name, value in expected.items()}, "counts": counts,
                       "observed_flow_status": actual["metrics"]["flow_cent"]["status"],
                       "observed_flow_reasons": actual["metrics"]["flow_cent"]["reason_codes"]})
        for member in applicable:
            code = member["ts_code"]
            valid = member["state"] == "ACTIVE" and not unavailable
            expected_members[(uid, code)] = {
                "flow_cent": str(money.get(code, 0)) if valid else None,
                "net_mf_vol": str(volume.get(code, 0)) if valid else None,
                "membership_state": member["state"], "has_trade": code in traded,
                "peer_pe_count": len(estimates["pe_ttm"]), "peer_pb_count": len(estimates["pb"]),
            }
    for uid, actual in actual_rows.items():
        value = expected_industries[uid]["flow_cent"]
        # Independent competition rank: 1 + number of strictly greater amounts.
        expected_rank = None if value is None else 1 + sum(expected_industries[other]["flow_cent"] > value for other in eligible_groups[(actual["taxonomy"], actual["level"])])
        check("ranking", uid, "flow_rank", actual["flow_rank"], expected_rank)
    actual_members = {(row["uid"], row["ts_code"]): row for row in result["members"]}
    check("identity", day, "unique_result_members", len(actual_members), len(result["members"]))
    check("identity", day, "member_identity_set", sorted(actual_members), sorted(expected_members))
    for key, expected in expected_members.items():
        for name, value in expected.items():
            check("member_values", "/".join(key), name, actual_members[key].get(name), value)

    log = [json.loads(line) for line in (capture / "requests.ndjson").read_text().splitlines() if line.strip()]
    logged = {r["request"]["request_key"]: r for r in log}
    check("provenance", day, "unique_request_keys", len(logged), len(log))
    check("provenance", day, "request_reference_keys", sorted(logged), sorted(r["request"]["request_key"] for r in inputs["source_refs"]))
    candidate_rows = {tax: defaultdict(list) for tax in ("CI", "THS")}
    request_counts = Counter()
    raw_verified = 0
    for ref in inputs["source_refs"]:
        metadata = ref["request"]
        raw_bytes = store.read_bytes(ref)
        raw_verified += 1
        check("provenance", metadata["request_key"], "logged_reference", logged.get(metadata["request_key"]), ref)
        check("provenance", metadata["request_key"], "metadata_raw_hash", metadata["raw_sha256"], ref["sha256"])
        api = metadata["api_name"]
        request_counts[api] += 1
        if api not in ("ci_index_member", "ths_member"):
            continue
        tax = "CI" if api == "ci_index_member" else "THS"
        payload = json.loads(raw_bytes, parse_float=Decimal)
        fields = payload["data"]["fields"]
        for values in payload["data"]["items"]:
            raw = dict(zip(fields, values))
            code = raw.get("ts_code") if tax == "CI" else raw.get("con_code")
            if code not in missing[tax]:
                continue
            record = {name: raw.get(name) for name in (("l1_code", "l2_code", "l3_code") if tax == "CI" else ("ts_code",)) + ("in_date", "out_date", "is_new")}
            record["response_sha256"] = ref["sha256"]
            record["request_params"] = metadata["params"]
            candidate_rows[tax][code].append(record)

    missing_records = []
    diagnoses = {}
    for tax in ("CI", "THS"):
        unresolved = sorted({m["ts_code"] for uid, rows in memberships.items() if industries[uid]["taxonomy"] == tax for m in rows if m["state"] != "ACTIVE" and m["ts_code"] in traded})
        for code in missing[tax]:
            raw_candidates = candidate_rows[tax][code]
            valid_candidates = [c for c in raw_candidates if c.get("in_date") and c["in_date"] <= day and (not c.get("out_date") or day <= c["out_date"])]
            missing_records.append({"taxonomy": tax, "ts_code": code, "name": stocks[code]["name"],
                                    "list_date": stocks[code]["list_date"], "flow_cent": str(money[code]),
                                    "raw_candidate_count": len(raw_candidates), "target_date_candidate_count": len(valid_candidates),
                                    "raw_candidates": raw_candidates, "target_date_candidates": valid_candidates,
                                    "localization": "BOUNDED_BY_DATED_EVIDENCE" if valid_candidates else "NO_PROVABLE_TARGET_DATE_INDUSTRY"})
        localized = [r["ts_code"] for r in missing_records if r["taxonomy"] == tax and r["target_date_candidate_count"]]
        diagnoses[tax] = {"missing_count": len(missing[tax]), "missing_codes": missing[tax],
                          "missing_flow_cent": str(sum(money[c] for c in missing[tax])),
                          "missing_abs_flow_cent": str(sum(abs(money[c]) for c in missing[tax])),
                          "unresolved_count": len(unresolved), "unresolved_codes": unresolved,
                          "bounded_missing_codes": localized,
                          "all_taxonomy_na_supported": bool(missing[tax]) and len(localized) < len(missing[tax]),
                          "reason": "缺失股票没有可证明的目标日行业归属，影响不能限定到候选行业；其他分类归属和公司名称不能替代本分类证据。"}

    representatives = []
    for tax in ("SW", "THS", "TDX", "CI"):
        candidates = [r for r in detail if r["taxonomy"] == tax]
        chosen = next((r for r in candidates if r["expected"]["pe_ttm_median"] is not None and r["expected"]["flow_cent"] is not None), candidates[0])
        representatives.append(chosen)
    return {
        "status": "PASS" if not failures else "FAIL", "execution_status": "SUCCEEDED", "artifact_publish_state": "DEVELOPMENT_AUDIT_ONLY",
        "publication_gate": publication_gate,
        "live_validation_state": "OFFLINE_RECOMPUTATION_OF_REAL_CAPTURE", "research_grade": "research_only",
        "decision_eligible": False, "production_approved": False,
        "capture": str(capture.resolve()), "capture_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "trade_date": day, "input_sha256": receipt["input"]["sha256"], "result_sha256": receipt["result"]["sha256"],
        "frozen_source_files_verified": frozen_verified, "raw_response_objects_verified": raw_verified,
        "request_counts": dict(request_counts), "check_counts": dict(checks), "failure_count": len(failures), "failures": failures,
        "industry_count": len(industries), "member_count": len(actual_members), "traded_count": len(traded), "eligible_count": len(pool),
        "known_flow_industry_count": sum(r["expected"]["flow_cent"] is not None for r in detail),
        "known_pe_industry_count": sum(r["expected"]["pe_ttm_median"] is not None for r in detail),
        "market_flow_cent": str(sum(money.values())), "unknown_taxonomies": sorted(expected_global_unknown),
        "diagnoses": diagnoses, "missing_records": missing_records, "representatives": representatives, "industries": detail,
        "limitations": [f"审计冻结的{day}单日真实快照，不替代新源码的全链路验收。", "不验证未采集的五年历史，也不把测试或离线复算称为生产发布。",
                        "独立算法直接使用被冻结的DayInputs；原始响应逐个校验哈希，并对缺失归属读取原始成员记录；不是另一数据供应商交叉核验。"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    capture, output = args.capture.resolve(), args.output.resolve()
    if output == capture or output.is_relative_to(capture) or capture.is_relative_to(output):
        raise SystemExit("AUDIT_OUTPUT_MUST_BE_SEPARATE")
    if not (capture / "capture_receipt.json").is_file() or output.exists():
        raise SystemExit("EXPLICIT_CAPTURE_AND_NEW_OUTPUT_REQUIRED")
    output.mkdir(parents=True, exist_ok=False)
    (output / "auditor_source.py").write_bytes(Path(__file__).read_bytes())
    validator_source = Path(audit_day.__code__.co_filename).read_bytes()
    (output / "validator_source.py").write_bytes(validator_source)
    try:
        report = audit_capture(capture)
        report["auditor_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        report["validator_sha256"] = hashlib.sha256(validator_source).hexdigest()
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        write_json(output / "audit.json", report)
        with (output / "missing_members.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["分类", "股票代码", "股票名称", "上市日期", "当日净流入（0.01万元）", "原始历史候选记录数", "目标日有效候选记录数", "影响可局部定位"])
            for row in report["missing_records"]:
                writer.writerow([row[k] for k in ("taxonomy", "ts_code", "name", "list_date", "flow_cent", "raw_candidate_count", "target_date_candidate_count")] + ["是" if row["target_date_candidate_count"] else "否"])
        lines = ["# 真实采集独立数值复核", "", f"结论：{report['status']}；{report['failure_count']}项差异。", "",
                 f"明确输入：`{capture}` 的SUCCEEDED回执，交易日{report['trade_date']}。", "",
                 f"核对{report['industry_count']}个行业、{report['member_count']}条成员记录、{report['traded_count']}只沪深成交股；"
                 f"验证{report['raw_response_objects_verified']}个原始响应及{report['frozen_source_files_verified']}个冻结源码文件。", "",
                 "使用独立Fraction算法计算中位数、整分资金净额和严格大于计数排名；没有导入被测compute_day、_median或rank_flows。", "",
                 "## 归属缺口", ""]
        for tax, d in report["diagnoses"].items():
            lines += [f"- {tax}：{d['missing_count']}只成交股无归属，净额{d['missing_flow_cent']}（0.01万元），绝对额{d['missing_abs_flow_cent']}（0.01万元）；另有{d['unresolved_count']}只重叠归属。"
                      f"目标日可定位缺失股：{len(d['bounded_missing_codes'])}只。{d['reason']}"]
        expired = [f"{row['ts_code']}：历史退出日{candidate['out_date']}" for row in report["missing_records"] for candidate in row["raw_candidates"] if candidate.get("out_date") and candidate["out_date"] < report["trade_date"]]
        lines += ["", ("已失效原始成员记录：" + "；".join(expired) + "。历史路径不证明目标日行业。") if expired else "缺失股的目标日候选与定位能力已逐股记录。", "缺股的原始证据详见audit.json和missing_members.csv。", "",
                  "## 使用边界", "", *["- " + text for text in report["limitations"]], "", "全部逐行业预期数值、覆盖数、原始候选与哈希证据见audit.json。", ""]
        (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
        print(json.dumps({key: report[key] for key in ("status", "failure_count", "industry_count", "member_count", "unknown_taxonomies")}, ensure_ascii=False))
        return 0 if report["status"] == "PASS" else 1
    except Exception as error:
        write_json(output / "failed_audit.json", {"execution_status": "FAILED", "error_type": type(error).__name__,
                   "error_code": str(error) if re.fullmatch(r"[A-Z0-9_]+", str(error)) else "AUDIT_EXCEPTION", "capture": str(capture)})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
