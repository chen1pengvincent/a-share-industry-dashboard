"""Governed live acquisition and deterministic offline rebuild pipeline."""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from decimal import Decimal
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .core import (
    DataValidationError,
    classification_whitelist,
    normalize_history,
    ohlc_ordering_audit,
    parse_yyyymmdd,
    summarize_axis,
    validate_name_history,
    validate_index_classify,
    validate_per_code_history_continuity,
    validate_sw_daily,
    validate_trade_cal,
)
from .io_utils import (
    allocate_run_dir,
    append_ndjson,
    atomic_write_bytes,
    atomic_write_text,
    inventory_files,
    read_csv,
    read_json,
    sha256_bytes,
    sha256_file,
    write_csv,
    write_json,
    write_sha256sums,
)
from .render import render_dashboard
from .tushare_client import SecureTushareClient, decode_rows


DATE_RE = re.compile(r"^[0-9]{8}$")
CANONICAL_PROJECT_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_SPEC_PATH = (CANONICAL_PROJECT_ROOT / "PROJECT_SPEC.json").resolve()
CLASSIFICATION_FIELDS = [
    "index_code",
    "industry_name",
    "parent_code",
    "level",
    "industry_code",
    "is_pub",
    "src",
]
NORMALIZED_CLASSIFICATION_FIELDS = [
    *CLASSIFICATION_FIELDS,
    "publication_state",
    "selection_basis",
]
TRADE_CAL_FIELDS = ["exchange", "cal_date", "is_open", "pretrade_date"]
SW_DAILY_FIELDS = [
    "ts_code",
    "trade_date",
    "name",
    "open",
    "low",
    "high",
    "close",
    "change",
    "pct_change",
    "vol",
    "amount",
    "pe",
    "pb",
    "total_mv",
    "float_mv",
]
NORMALIZED_DAILY_FIELDS = ["taxonomy", *SW_DAILY_FIELDS]
SUMMARY_FIELDS = [
    "taxonomy",
    "index_code",
    "industry_name",
    "as_of",
    "pe",
    "pb",
    "pe_percentile",
    "pb_percentile",
    "pe_valid_count",
    "pb_valid_count",
    "pe_tie_count",
    "pb_tie_count",
    "pe_tie_ratio",
    "pb_tie_ratio",
    "pe_first_valid_date",
    "pe_last_valid_date",
    "pb_first_valid_date",
    "pb_last_valid_date",
    "pe_label",
    "pb_label",
    "return_5d",
    "return_mtd",
    "return_ytd",
    "valuation_state",
    "return_state",
]
SW2021_HISTORY_FIELDS = [
    "taxonomy",
    "index_code",
    "industry_name",
    "trade_date",
    "close",
    "pe",
    "pb",
    "is_pub",
]
SW2014_HISTORY_FIELDS = [
    *SW2021_HISTORY_FIELDS[:-1],
    "source_name",
    "is_pub",
]

SW2021_PUBLICATION_STATE = "BINARY_FLAG_PROVIDED"
SW2021_SELECTION_BASIS = "PUBLISHED_ONLY"
SW2021_CONTINUITY_POLICY = "COMMON_START_RECTANGLE"
SW2014_PUBLICATION_STATE = "NOT_PROVIDED_FOR_RETIRED_TAXONOMY"
SW2014_SELECTION_BASIS = "ALL_CLASSIFIED_L1_ROWS_FOR_RETIRED_TAXONOMY"
SW2014_CONTINUITY_POLICY = "PER_CODE_OBSERVED_INCEPTION_TO_COMMON_END"
SW2014_NAME_HISTORY_POLICY = "STABLE_TS_CODE_WITH_SOURCE_NAME_HISTORY"


class PipelineError(RuntimeError):
    """A run could not satisfy the frozen contract."""


class SourceClosureError(PipelineError):
    """The governed source closure changed while a run was executing."""

    code = "SOURCE_CLOSURE_CHANGED_DURING_RUN"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _validate_as_of_text(as_of: str) -> None:
    if not isinstance(as_of, str) or not DATE_RE.fullmatch(as_of):
        raise PipelineError("as_of must be YYYYMMDD")
    parse_yyyymmdd(as_of, field="as_of")


def _safe_error(exc: BaseException) -> dict[str, str]:
    message = str(exc)
    if len(message) > 1000:
        message = message[:1000] + "..."
    raw_reason = getattr(exc, "code", type(exc).__name__)
    if isinstance(raw_reason, str) and raw_reason.strip():
        reason_code = raw_reason.strip()
    elif isinstance(raw_reason, int) and not isinstance(raw_reason, bool):
        reason_code = f"UPSTREAM_API_CODE_{raw_reason}"
    else:
        reason_code = type(exc).__name__
    return {
        "type": type(exc).__name__,
        "reason_code": reason_code,
        "message": message,
    }


def _installed_version(distribution: str) -> str | None:
    try:
        return importlib_metadata.version(distribution)
    except importlib_metadata.PackageNotFoundError:
        return None


def _raw_response_bytes(response: Mapping[str, Any]) -> bytes:
    raw = getattr(response, "raw_bytes", None)
    if isinstance(raw, bytes):
        return raw
    return json.dumps(
        response,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fetch(
    *,
    client: Any,
    api_name: str,
    params: Mapping[str, Any],
    fields: Sequence[str],
    row_limit: int,
    raw_path: Path,
    request_log: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    response = client.call(api_name, params, fields)
    raw_bytes = _raw_response_bytes(response)
    atomic_write_bytes(raw_path, raw_bytes)
    request_record = {
        "api_name": api_name,
        "params": dict(params),
        "fields": list(fields),
        "row_count": None,
        "decode_state": "PENDING",
        "http_status": getattr(response, "http_status", None),
        "attempt_count": getattr(response, "attempt_count", 1),
        "raw_path": raw_path.as_posix(),
        "raw_sha256": sha256_bytes(raw_bytes),
    }
    request_log.append(request_record)
    try:
        rows = decode_rows(
            response,
            api_name=api_name,
            expected_fields=fields,
            row_limit=row_limit,
        )
    except Exception as exc:
        request_record["decode_state"] = "BLOCKED"
        request_record["reason_code"] = _safe_error(exc)["reason_code"]
        raise
    request_record["row_count"] = len(rows)
    request_record["decode_state"] = "PASS"
    return rows


def _open_dates(calendar_rows: list[dict[str, Any]], start: str, end: str) -> list[str]:
    return sorted(
        row["cal_date"]
        for row in calendar_rows
        if str(row["is_open"]) == "1" and start <= row["cal_date"] <= end
    )


def _require_complete_close_rectangle(
    rows: list[dict[str, Any]],
    codes: Sequence[str],
    open_dates: Sequence[str],
    *,
    taxonomy: str,
) -> dict[str, Any]:
    expected = {(code, date) for code in codes for date in open_dates}
    observed = {(row["ts_code"], row["trade_date"]) for row in rows}
    missing = sorted(expected - observed)
    extra = sorted(observed - expected)
    if missing or extra:
        raise DataValidationError(
            "CLOSE_RECTANGLE_MISMATCH",
            f"{taxonomy} close history is not a complete published-code/open-date rectangle",
            details={
                "expected_rows": len(expected),
                "actual_rows": len(observed),
                "missing_sample": [list(item) for item in missing[:20]],
                "extra_sample": [list(item) for item in extra[:20]],
            },
        )
    return {
        "policy": SW2021_CONTINUITY_POLICY,
        "expected_rows": len(expected),
        "actual_rows": len(observed),
        "code_count": len(codes),
        "open_date_count": len(open_dates),
        "first_date": open_dates[0] if open_dates else None,
        "last_date": open_dates[-1] if open_dates else None,
    }


def _normalized_classifications(
    rows: Sequence[Mapping[str, Any]],
    taxonomy: str,
) -> list[dict[str, Any]]:
    """Attach explicit derived semantics without rewriting Tushare fields."""

    normalized: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if taxonomy == "SW2021":
            flag = str(item.get("is_pub"))
            if flag not in {"0", "1"}:
                raise DataValidationError(
                    "INVALID_FLAG",
                    "SW2021 normalized classification requires binary is_pub",
                )
            item["publication_state"] = "PUBLISHED" if flag == "1" else "NOT_PUBLISHED"
            item["selection_basis"] = SW2021_SELECTION_BASIS
        elif taxonomy == "SW2014":
            if item.get("is_pub") is not None:
                raise DataValidationError(
                    "RETIRED_PUBLICATION_STATE_INVALID",
                    "SW2014 is_pub must remain null in normalized classification",
                )
            item["publication_state"] = SW2014_PUBLICATION_STATE
            item["selection_basis"] = SW2014_SELECTION_BASIS
        else:  # pragma: no cover - callers are contract-bound
            raise DataValidationError("INVALID_SOURCE", "unknown classification taxonomy")
        normalized.append(item)
    return normalized


def _fetch_axis(
    *,
    client: Any,
    taxonomy: str,
    start_date: str,
    end_date: str,
    expected_count: int,
    minimum_observations: int,
    calendar_rows: list[dict[str, Any]],
    run_dir: Path,
    request_log: list[dict[str, Any]],
) -> dict[str, Any]:
    raw_root = run_dir / "inputs" / "raw"
    classification = _fetch(
        client=client,
        api_name="index_classify",
        params={"level": "L1", "src": taxonomy},
        fields=CLASSIFICATION_FIELDS,
        row_limit=5000,
        raw_path=raw_root / "index_classify" / f"{taxonomy}_L1.json",
        request_log=request_log,
    )
    classification = validate_index_classify(
        classification,
        src=taxonomy,
        level="L1",
        expected_count=expected_count,
    )
    is_retired_axis = taxonomy == "SW2014"
    whitelist = classification_whitelist(
        classification,
        published_only=not is_retired_axis,
        src=taxonomy,
    )
    selected_codes = sorted(whitelist)
    open_dates = _open_dates(calendar_rows, start_date, end_date)
    if not open_dates or open_dates[0] != start_date and taxonomy == "SW2021":
        # SW2021's frozen start must itself be an observed open day.
        raise DataValidationError(
            "AXIS_START_NOT_OPEN",
            f"{taxonomy} query start is not the first observed open day",
        )
    combined: list[dict[str, Any]] = []
    for code in selected_codes:
        safe_code = code.replace(".", "_")
        rows = _fetch(
            client=client,
            api_name="sw_daily",
            params={"ts_code": code, "start_date": start_date, "end_date": end_date},
            fields=SW_DAILY_FIELDS,
            row_limit=4000,
            raw_path=raw_root / "sw_daily" / taxonomy / f"{safe_code}.json",
            request_log=request_log,
        )
        rows = validate_sw_daily(
            rows,
            whitelist=whitelist,
            start_date=start_date,
            end_date=end_date,
            open_dates=open_dates,
            row_limit=4000,
            strict_name_match=False,
            allow_name_history=is_retired_axis,
        )
        returned_codes = {row["ts_code"] for row in rows}
        if returned_codes != {code}:
            raise DataValidationError(
                "PER_CODE_RESPONSE_MISMATCH",
                f"sw_daily({code}) returned another or no code",
                details={"returned_codes": sorted(returned_codes)},
            )
        combined.extend(rows)
    combined = validate_sw_daily(
        combined,
        whitelist=whitelist,
        start_date=start_date,
        end_date=end_date,
        open_dates=open_dates,
        row_limit=None,
        strict_name_match=False,
        allow_name_history=is_retired_axis,
    )
    if is_retired_axis:
        continuity = validate_per_code_history_continuity(
            combined,
            whitelist=whitelist,
            open_dates=open_dates,
            end_date=end_date,
            minimum_valid_observations=minimum_observations,
            src=taxonomy,
        )
        publication_state = SW2014_PUBLICATION_STATE
        selection_basis = SW2014_SELECTION_BASIS
        continuity_policy = SW2014_CONTINUITY_POLICY
        name_history_policy = SW2014_NAME_HISTORY_POLICY
        name_history = validate_name_history(
            combined,
            whitelist=whitelist,
            end_date=end_date,
            src=taxonomy,
        )
        published_count: int | None = None
        unpublished_count: int | None = None
    else:
        continuity = _require_complete_close_rectangle(
            combined,
            selected_codes,
            open_dates,
            taxonomy=taxonomy,
        )
        publication_state = SW2021_PUBLICATION_STATE
        selection_basis = SW2021_SELECTION_BASIS
        continuity_policy = SW2021_CONTINUITY_POLICY
        name_history_policy = "CONSTANT_NAME_REQUIRED"
        name_history = None
        published_count = len(selected_codes)
        unpublished_count = len(classification) - len(selected_codes)
    summary = summarize_axis(
        combined,
        classification,
        calendar_rows,
        src=taxonomy,
        as_of=end_date,
        minimum_valid_observations=minimum_observations,
    )
    history = normalize_history(combined, classification, src=taxonomy)
    axis_audit: dict[str, Any] = {
        "state": "PASS",
        "classification_count": len(classification),
        "selected_count": len(selected_codes),
        "published_count": published_count,
        "unpublished_count": unpublished_count,
        "publication_flag_null_count": sum(
            row.get("is_pub") is None for row in classification
        ),
        "publication_state": publication_state,
        "selection_basis": selection_basis,
        "continuity_policy": continuity_policy,
        "continuity": continuity,
        "name_history_policy": name_history_policy,
        "valuation_coverage": _valuation_coverage(combined),
        "ohlc_ordering": ohlc_ordering_audit(combined),
    }
    if name_history is not None:
        axis_audit["name_history"] = name_history
    return {
        "classification": classification,
        "daily": combined,
        "summary": _canonical_summary(summary, classification, taxonomy),
        "history": _canonical_history(history, taxonomy),
        "audit": axis_audit,
    }


def _canonical_summary(
    summary: list[dict[str, Any]],
    classifications: list[dict[str, Any]],
    taxonomy: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in summary:
        pe_status = item.get("pe_status", "UNKNOWN")
        pb_status = item.get("pb_status", "UNKNOWN")
        return_statuses = [
            item.get("return_5d_status", "UNKNOWN"),
            item.get("return_mtd_status", "UNKNOWN"),
            item.get("return_ytd_status", "UNKNOWN"),
        ]
        rows.append(
            {
                "taxonomy": taxonomy,
                "index_code": item["index_code"],
                "industry_name": item["industry_name"],
                "as_of": item["as_of"],
                "pe": item.get("pe"),
                "pb": item.get("pb"),
                "pe_percentile": item.get("pe_percentile_le"),
                "pb_percentile": item.get("pb_percentile_le"),
                "pe_valid_count": item.get("pe_valid_count"),
                "pb_valid_count": item.get("pb_valid_count"),
                "pe_tie_count": item.get("pe_tie_count"),
                "pb_tie_count": item.get("pb_tie_count"),
                "pe_tie_ratio": item.get("pe_tie_ratio"),
                "pb_tie_ratio": item.get("pb_tie_ratio"),
                "pe_first_valid_date": item.get("pe_first_valid_date"),
                "pe_last_valid_date": item.get("pe_last_valid_date"),
                "pb_first_valid_date": item.get("pb_first_valid_date"),
                "pb_last_valid_date": item.get("pb_last_valid_date"),
                "pe_label": item.get("pe_history_label"),
                "pb_label": item.get("pb_history_label"),
                "return_5d": item.get("return_5d"),
                "return_mtd": item.get("return_mtd"),
                "return_ytd": item.get("return_ytd"),
                "valuation_state": "OK"
                if pe_status == "OK" and pb_status == "OK"
                else f"PE={pe_status};PB={pb_status}",
                "return_state": "OK"
                if all(value == "OK" for value in return_statuses)
                else ";".join(return_statuses),
            }
        )
    for classification in classifications:
        if taxonomy == "SW2021" and str(classification["is_pub"]) != "1":
            rows.append(
                {
                    "taxonomy": taxonomy,
                    "index_code": classification["index_code"],
                    "industry_name": classification["industry_name"],
                    "as_of": summary[0]["as_of"] if summary else "",
                    "valuation_state": "NA_NOT_PUBLISHED",
                    "return_state": "NA_NOT_PUBLISHED",
                }
            )
    return sorted(rows, key=lambda row: row["index_code"])


def _canonical_history(history: list[dict[str, Any]], taxonomy: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in history:
        canonical = {
            "taxonomy": taxonomy,
            "index_code": row["index_code"],
            "industry_name": row["industry_name"],
            "trade_date": row["trade_date"],
            "close": row["close"],
            "pe": row["pe"],
            "pb": row["pb"],
            "is_pub": 1 if taxonomy == "SW2021" else None,
        }
        if taxonomy == "SW2014":
            canonical["source_name"] = row["source_name"]
        rows.append(canonical)
    return rows


def _valuation_coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"row_count": len(rows)}
    for field in ("pe", "pb"):
        present = 0
        positive = 0
        for row in rows:
            value = row.get(field)
            if value not in (None, ""):
                present += 1
                try:
                    parsed = Decimal(str(value))
                    if parsed.is_finite() and parsed > 0:
                        positive += 1
                except Exception:
                    pass
        result[field] = {
            "present_count": present,
            "positive_finite_count": positive,
            "positive_finite_ratio": str(Decimal(positive) / Decimal(len(rows)))
            if rows
            else None,
        }
    return result


def _write_axis_files(run_dir: Path, taxonomy: str, axis: Mapping[str, Any]) -> None:
    suffix = taxonomy.lower()
    raw_classification = [dict(row) for row in axis.get("classification", [])]
    classification = (
        _normalized_classifications(raw_classification, taxonomy)
        if raw_classification
        else []
    )
    daily = [
        {"taxonomy": taxonomy, **dict(row)} for row in axis.get("daily", [])
    ]
    write_csv(
        run_dir / "inputs" / "normalized" / f"classification_{suffix}.csv",
        classification,
        NORMALIZED_CLASSIFICATION_FIELDS,
    )
    write_csv(
        run_dir / "inputs" / "normalized" / f"sw_daily_{suffix}.csv",
        daily,
        NORMALIZED_DAILY_FIELDS,
    )
    if taxonomy == "SW2021":
        write_csv(run_dir / "tables" / "sw2021_current.csv", axis.get("summary", []), SUMMARY_FIELDS)
        write_csv(
            run_dir / "tables" / "sw2021_history.csv",
            axis.get("history", []),
            SW2021_HISTORY_FIELDS,
        )
    else:
        write_csv(run_dir / "tables" / "sw2014_archive.csv", axis.get("summary", []), SUMMARY_FIELDS)
        write_csv(
            run_dir / "tables" / "sw2014_history.csv",
            axis.get("history", []),
            SW2014_HISTORY_FIELDS,
        )


def _write_empty_axis_files(run_dir: Path, taxonomy: str) -> None:
    _write_axis_files(
        run_dir,
        taxonomy,
        {"classification": [], "daily": [], "summary": [], "history": []},
    )


def _source_paths(project_root: Path) -> list[Path]:
    return [
        project_root / "PROJECT_CONTRACT.md",
        project_root / "PROJECT_SPEC.json",
        project_root / "README.md",
        project_root / "run_dashboard.py",
        *sorted((project_root / "src" / "swivd").glob("*.py")),
        *sorted((project_root / "tests").glob("test_*.py")),
    ]


def _source_files(project_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in _source_paths(project_root):
        if not path.is_file() or path.is_symlink():
            raise SourceClosureError(
                f"source closure requires a regular non-symlink file: {path}"
            )
        records.append({
            "path": path.relative_to(project_root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    return records


def _assert_source_closure_equal(
    expected: Sequence[Mapping[str, Any]],
    actual: Sequence[Mapping[str, Any]],
    *,
    stage: str,
) -> None:
    if list(expected) == list(actual):
        return
    expected_by_path = {str(item.get("path")): item for item in expected}
    actual_by_path = {str(item.get("path")): item for item in actual}
    changed = sorted(
        path
        for path in set(expected_by_path) | set(actual_by_path)
        if expected_by_path.get(path) != actual_by_path.get(path)
    )
    raise SourceClosureError(
        f"source closure changed at {stage}; changed_paths={changed}"
    )


def _copy_source_snapshot(
    *,
    project_root: Path,
    snapshot_root: Path,
    expected: Sequence[Mapping[str, Any]],
) -> None:
    for item in expected:
        relative = Path(str(item["path"]))
        source = project_root / relative
        target = snapshot_root / relative
        if not source.is_file() or source.is_symlink():
            raise SourceClosureError(
                f"source closure changed while copying snapshot: {relative.as_posix()}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if target.stat().st_size != item["bytes"] or sha256_file(target) != item["sha256"]:
            raise SourceClosureError(
                f"source snapshot differs from pre-run closure: {relative.as_posix()}"
            )


def _run_frozen_cli(
    *,
    snapshot_root: Path,
    arguments: Sequence[str],
    process_temp_root: Path,
    label: str,
) -> None:
    process_temp_root.mkdir(parents=True, exist_ok=False)
    environment = {"TMPDIR": str(process_temp_root)}
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-X",
                "utf8",
                str(snapshot_root / "run_dashboard.py"),
                *arguments,
            ],
            cwd=snapshot_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PipelineError(f"FROZEN_SOURCE_{label}_PROCESS_FAILED") from exc
    if completed.returncode != 0:
        raise PipelineError(f"FROZEN_SOURCE_{label}_FAILED")


def _adversarial_report(
    *,
    run_id: str,
    as_of: str,
    axis_states: Mapping[str, str],
    audit: Mapping[str, Any],
) -> str:
    legacy_audit = audit.get("axes", {}).get("SW2014", {})
    legacy_issue = legacy_audit.get("error")
    renamed_codes = legacy_audit.get("name_history", {}).get("renamed_codes", [])
    current_ohlc_count = (
        audit.get("axes", {})
        .get("SW2021", {})
        .get("ohlc_ordering", {})
        .get("anomaly_count", "UNKNOWN")
    )
    legacy_ohlc_count = (
        legacy_audit.get("ohlc_ordering", {}).get("anomaly_count", "AXIS_BLOCKED")
    )
    legacy_line = (
        "SW2014 轴已闭合：原样保留退役分类 is_pub=null，按每个行业自身首观测日"
        "至共同末日校验无内部交易日缺口。"
        if axis_states.get("SW2014") == "PASS"
        else f"SW2014 轴失败关闭：{json.dumps(legacy_issue, ensure_ascii=False, sort_keys=True)}"
    )
    return f"""# {run_id} 对抗式审查

- 数据截止日：`{as_of}`
- SW2021 轴：`{axis_states.get('SW2021', 'UNKNOWN')}`
- SW2014 轴：`{axis_states.get('SW2014', 'UNKNOWN')}`
- 研究等级：`RESEARCH_ONLY`
- 决策资格：`false`
- 生产批准：`false`

## 已主动拒绝的错误路径

1. 没有用证监会行业、扁平供应商行业或当前成分股冒充申万一级行业。
2. 没有用个股 PE/PB 聚合替代 `sw_daily.pe/pb`。
3. 没有把 SW2014 与 SW2021 拼成一个历史百分位或跨版本排行。
4. 没有用舍入后的 `pct_change` 连乘替代收盘价锚点收益。
5. 没有 Pickle、CDN、远程字体、自动打开浏览器或正式缓存与测试缓存共用。
6. 没有把 SW2014 的上游 `is_pub=null` 伪造成 `1`，也没有把指数首观测日前的
   结构性空白误报为中间丢数。
7. 没有按名称拼接或覆盖 SW2014 历史。`ts_code` 是身份，`source_name` 保留
   上游日标签；实际改名代码为 `{json.dumps(renamed_codes, ensure_ascii=False)}`。
8. `open/low/high` 不参与本项目公式；其与 `close` 的排序异常原样保留并披露、
   不改写源值，也不冒充 `close/pe/pb` 有效性。SW2021 异常行 `{current_ohlc_count}`，
   SW2014 异常行 `{legacy_ohlc_count}`。

## 分轴结论

{legacy_line}

历史位置采用用户确认的经验分布函数 `count(x <= current) / N`。页面同时披露 N、并列数
和并列比例，因此不能把上游舍入造成的并列上偏隐藏成真实变化。历史位置只是描述性统计，
不构成内在价值或未来收益判断。
"""


def _write_manifest_and_sums(run_dir: Path, manifest: dict[str, Any]) -> None:
    manifest["artifacts"] = inventory_files(
        run_dir,
        excluded={"manifest.json", "SHA256SUMS"},
    )
    write_json(run_dir / "manifest.json", manifest)
    write_sha256sums(run_dir)


def _status_fields(axis_states: Mapping[str, str]) -> tuple[str, str]:
    if axis_states.get("SW2021") != "PASS":
        return "FAILED", "FAIL"
    if axis_states.get("SW2014") == "PASS":
        return "LOCAL_RESEARCH_CANDIDATE_COMPLETE", "PASS"
    return "LOCAL_RESEARCH_CANDIDATE_PARTIAL", "PARTIAL"


def _verify_offline_rebuild(
    run_dir: Path,
    *,
    project_root: Path,
    expected_source_files: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate and rebuild with a fresh interpreter from a frozen source copy."""

    relative_pairs = {
        "tables/sw2021_current.csv": "sw2021_current.csv",
        "tables/sw2021_history.csv": "sw2021_history.csv",
        "tables/sw2014_archive.csv": "sw2014_archive.csv",
        "tables/sw2014_history.csv": "sw2014_history.csv",
        "dashboard.html": "dashboard.html",
        "reports/adversarial_review.md": "reports/adversarial_review.md",
    }
    with tempfile.TemporaryDirectory(prefix="swivd-pointer-gate-") as temporary:
        temporary_root = Path(temporary)
        snapshot_root = temporary_root / "source"
        rebuilt = temporary_root / "derived"
        _copy_source_snapshot(
            project_root=project_root,
            snapshot_root=snapshot_root,
            expected=expected_source_files,
        )
        _assert_source_closure_equal(
            expected_source_files,
            _source_files(snapshot_root),
            stage="FROZEN_SNAPSHOT",
        )
        _assert_source_closure_equal(
            expected_source_files,
            _source_files(project_root),
            stage="AFTER_SNAPSHOT_COPY",
        )
        _run_frozen_cli(
            snapshot_root=snapshot_root,
            arguments=["validate-run", "--run-dir", str(run_dir.resolve())],
            process_temp_root=temporary_root / "validate-process-tmp",
            label="VALIDATION",
        )
        _run_frozen_cli(
            snapshot_root=snapshot_root,
            arguments=[
                "rebuild",
                "--run-dir",
                str(run_dir.resolve()),
                "--output-dir",
                str(rebuilt),
            ],
            process_temp_root=temporary_root / "rebuild-process-tmp",
            label="REBUILD",
        )
        evidence: list[dict[str, str]] = []
        for source_relative, rebuilt_relative in relative_pairs.items():
            source_hash = sha256_file(run_dir / source_relative)
            rebuilt_hash = sha256_file(rebuilt / rebuilt_relative)
            if source_hash != rebuilt_hash:
                raise PipelineError(
                    "DERIVED_REBUILD_MISMATCH: "
                    f"{source_relative} differs from frozen-input rebuild"
                )
            evidence.append(
                {
                    "path": source_relative,
                    "sha256": source_hash,
                }
            )
    return {
        "status": "PASS",
        "fresh_process_validation": "PASS",
        "fresh_process_rebuild": "PASS",
        "files": evidence,
    }


def run_live(
    *,
    spec_path: str | Path,
    as_of: str,
    output_root: str | Path,
    client: Any | None = None,
    now: Callable[[], str] = _now,
) -> dict[str, Any]:
    """Create one immutable live run; never reuse a failed or previous directory."""

    _validate_as_of_text(as_of)
    spec_path = Path(spec_path).resolve()
    if spec_path != CANONICAL_SPEC_PATH:
        raise PipelineError("run requires the canonical PROJECT_SPEC.json path")
    project_root = CANONICAL_PROJECT_ROOT
    output_root = Path(output_root).resolve()
    if client is None and output_root != (CANONICAL_PROJECT_ROOT / "output").resolve():
        raise PipelineError("live network runs require the canonical output root")
    # Normative authorization must close before allocating a run directory or
    # constructing a network client.  A semantically altered --spec must have
    # zero project/network side effects.
    from .validator import validate_spec

    spec = validate_spec(spec_path)
    source_files_before = _source_files(project_root)
    created_at = now()
    run_id, run_dir = allocate_run_dir(output_root, as_of)
    request_log: list[dict[str, Any]] = []
    audit: dict[str, Any] = {
        "schema_version": "swivd-audit-v3",
        "run_id": run_id,
        "as_of": as_of,
        "created_at": created_at,
        "contract_version": spec.get("contract_version"),
        "spec_version": spec.get("schema_version"),
        "semantic_successor_id": spec.get("semantic_successor_id"),
        "name_history_successor_id": spec.get("name_history_successor_id"),
        "axes": {},
        "requests": request_log,
        "issues": [],
    }
    axis_states = {"SW2021": "BLOCKED", "SW2014": "BLOCKED"}
    sw2021: dict[str, Any] | None = None
    sw2014: dict[str, Any] | None = None
    calendar_rows: list[dict[str, Any]] = []
    validated_candidate = False
    latest_pointer_written = False
    try:
        if spec.get("decision_id") != "GOV-20260830-001":
            raise PipelineError("spec decision_id is not authorized")
        if (
            spec.get("semantic_successor_id")
            != "GOV-20260830-001-SW2014-SEMANTICS-AUTHORIZED"
        ):
            raise PipelineError("spec semantic_successor_id is not authorized")
        if (
            spec.get("name_history_successor_id")
            != "GOV-20260830-001-NAME-HISTORY-AUTHORIZED"
        ):
            raise PipelineError("spec name_history_successor_id is not authorized")
        if client is None:
            network = spec["network"]
            client = SecureTushareClient(
                base_url=network["base_url"],
                timeout_seconds=network["timeout_seconds"],
                max_transient_retries=network["max_transient_retries"],
                backoff_seconds=network["backoff_seconds"],
            )
        calendar_rows = _fetch(
            client=client,
            api_name="trade_cal",
            params={
                "exchange": "SSE",
                "start_date": "20140101",
                "end_date": as_of,
                "is_open": "1",
            },
            fields=TRADE_CAL_FIELDS,
            row_limit=6000,
            raw_path=run_dir / "inputs" / "raw" / "trade_cal" / f"SSE_20140101_{as_of}.json",
            request_log=request_log,
        )
        calendar_rows = validate_trade_cal(
            calendar_rows,
            start_date="20140101",
            end_date=as_of,
            exchange="SSE",
        )
        open_dates = _open_dates(calendar_rows, "20140101", as_of)
        if as_of not in open_dates:
            raise DataValidationError("AS_OF_NOT_OPEN", "explicit as_of is not an open day")
        legacy_candidates = [value for value in open_dates if value < "20211213"]
        if not legacy_candidates:
            raise DataValidationError("NO_LEGACY_END", "no open day exists before SW2021 start")
        legacy_end = legacy_candidates[-1]
        write_csv(
            run_dir / "inputs" / "normalized" / "trade_calendar.csv",
            calendar_rows,
            TRADE_CAL_FIELDS,
        )
        audit["trade_calendar"] = {
            "state": "PASS",
            "row_count": len(calendar_rows),
            "first_open_date": open_dates[0],
            "last_open_date": open_dates[-1],
            "legacy_end": legacy_end,
        }

        current_spec = spec["axes"]["SW2021"]
        sw2021 = _fetch_axis(
            client=client,
            taxonomy="SW2021",
            start_date=current_spec["query_start"],
            end_date=as_of,
            expected_count=int(current_spec["expected_classification_count"]),
            minimum_observations=int(current_spec["minimum_valid_observations"]),
            calendar_rows=calendar_rows,
            run_dir=run_dir,
            request_log=request_log,
        )
        axis_states["SW2021"] = "PASS"
        audit["axes"]["SW2021"] = sw2021["audit"]
        _write_axis_files(run_dir, "SW2021", sw2021)

        legacy_spec = spec["axes"]["SW2014"]
        try:
            sw2014 = _fetch_axis(
                client=client,
                taxonomy="SW2014",
                start_date=legacy_spec["query_start"],
                end_date=legacy_end,
                expected_count=int(legacy_spec["expected_classification_count"]),
                minimum_observations=int(legacy_spec["minimum_valid_observations"]),
                calendar_rows=calendar_rows,
                run_dir=run_dir,
                request_log=request_log,
            )
            axis_states["SW2014"] = "PASS"
            audit["axes"]["SW2014"] = sw2014["audit"]
            _write_axis_files(run_dir, "SW2014", sw2014)
        except Exception as exc:
            reason_code = _safe_error(exc)["reason_code"]
            audit["axes"]["SW2014"] = {
                "state": "BLOCKED",
                "reason_codes": [reason_code],
                "error": _safe_error(exc),
            }
            audit["issues"].append(
                {"axis": "SW2014", "severity": "BLOCKED", **_safe_error(exc)}
            )
            _write_empty_axis_files(run_dir, "SW2014")

        artifact_state, live_state = _status_fields(axis_states)
        statuses = {
            "execution_status": "COMPLETED",
            "artifact_publish_state": artifact_state,
            "live_validation_state": live_state,
            "sw2021_axis_state": axis_states["SW2021"],
            "sw2014_axis_state": axis_states["SW2014"],
            "research_grade": "RESEARCH_ONLY",
            "decision_eligible": False,
            "production_approved": False,
        }
        metadata = {
            "run_id": run_id,
            "as_of": as_of,
            "archive_end": legacy_end,
            "source": "Tushare Pro sw_daily 原始行业指数字段",
            "taxonomy": "SW2021 L1 / SW2014 L1 独立轴",
            "contract_version": spec["contract_version"],
            "spec_version": spec["schema_version"],
            "formula": spec["valuation"]["formula"],
            "generated_at": "",
            "statuses": statuses,
        }
        completed_at = now()
        metadata["generated_at"] = completed_at
        audit["status"] = statuses
        audit["completed_at"] = completed_at
        html = render_dashboard(
            current_rows=sw2021["summary"],
            sw2021_history=sw2021["history"],
            sw2014_summary=sw2014["summary"] if sw2014 else [],
            sw2014_history=sw2014["history"] if sw2014 else [],
            metadata=metadata,
            audit=audit,
        )
        atomic_write_text(run_dir / "dashboard.html", html)
        write_json(run_dir / "audit.json", audit)
        atomic_write_text(
            run_dir / "reports" / "adversarial_review.md",
            _adversarial_report(
                run_id=run_id,
                as_of=as_of,
                axis_states=axis_states,
                audit=audit,
            ),
        )
        _assert_source_closure_equal(
            source_files_before,
            _source_files(project_root),
            stage="BEFORE_MANIFEST",
        )
        manifest: dict[str, Any] = {
            "schema_version": "swivd-run-manifest-v3",
            "run_id": run_id,
            "as_of": as_of,
            "created_at": created_at,
            "completed_at": completed_at,
            "execution_status": "COMPLETED",
            "artifact_publish_state": artifact_state,
            "live_validation_state": live_state,
            "axes": dict(axis_states),
            "research_grade": "RESEARCH_ONLY",
            "decision_eligible": False,
            "production_approved": False,
            "contract_version": spec["contract_version"],
            "spec_version": spec["schema_version"],
            "semantic_successor_id": spec["semantic_successor_id"],
            "name_history_successor_id": spec["name_history_successor_id"],
            "source": {
                "provider": "Tushare Pro",
                "transport": "HTTPS_POST_NO_REDIRECT",
                "apis": ["trade_cal", "index_classify", "sw_daily"],
            },
            "runtime": {
                "python_implementation": platform.python_implementation(),
                "python_version": platform.python_version(),
                "executable": str(Path(sys.executable).resolve()),
                "platform": platform.platform(),
                "dependency_policy": (
                    "STDLIB_HTTP_WITH_OPTIONAL_CERTIFI_CA_AND_TUSHARE_TOKEN_LOOKUP"
                ),
                "package_versions": {
                    "certifi": _installed_version("certifi"),
                    "tushare": _installed_version("tushare"),
                },
            },
            "spec_sha256": sha256_file(spec_path),
            "contract_sha256": sha256_file(project_root / "PROJECT_CONTRACT.md"),
            "source_files": source_files_before,
            "request_count": len(request_log),
            "validation": {
                "status": "PASS",
                "entrypoint": "swivd.validator.validate_run",
            },
            "artifacts": [],
        }
        _write_manifest_and_sums(run_dir, manifest)

        from .validator import validate_run

        first_validation = validate_run(run_dir)
        write_json(
            run_dir / "offline_validation.json",
            {
                "schema_version": "swivd-offline-validation-v1",
                "validated_at": now(),
                "result": first_validation,
            },
        )
        _write_manifest_and_sums(run_dir, manifest)
        final_validation = validate_run(run_dir)
        offline_rebuild_validation = _verify_offline_rebuild(
            run_dir,
            project_root=project_root,
            expected_source_files=source_files_before,
        )
        _assert_source_closure_equal(
            source_files_before,
            _source_files(project_root),
            stage="BEFORE_PUBLICATION",
        )
        validated_candidate = True

        manifest_sha256 = sha256_file(run_dir / "manifest.json")
        pointer: dict[str, Any] | None = None
        if artifact_state == "LOCAL_RESEARCH_CANDIDATE_COMPLETE":
            pointer = {
                "pointer_kind": "manifest",
                "scope": "SW2021_L1_CURRENT_WITH_SEPARATE_SW2014_ARCHIVE",
                "run_id": run_id,
                "as_of": as_of,
                "target_path": f"runs/{run_id}/manifest.json",
                "target_sha256": manifest_sha256,
                "artifact_publish_state": artifact_state,
                "updated_at": now(),
            }
        append_ndjson(
            output_root / "run_ledger.ndjson",
            {
                "record_type": "RUN_SUMMARY",
                "run_id": run_id,
                "as_of": as_of,
                "recorded_at": now(),
                "execution_status": "COMPLETED",
                "artifact_publish_state": artifact_state,
                "live_validation_state": live_state,
                "axes": axis_states,
                "manifest_sha256": manifest_sha256,
                "fresh_process_validation": offline_rebuild_validation[
                    "fresh_process_validation"
                ],
                "fresh_process_rebuild": offline_rebuild_validation[
                    "fresh_process_rebuild"
                ],
                "derived_rebuild_files": offline_rebuild_validation["files"],
                "latest_pointer_update_eligible": pointer is not None,
                "latest_pointer_updated": False,
            },
        )
        # The summary records eligibility without claiming a future mutation.
        # The pointer is atomically committed, read back, then acknowledged by
        # a second append-only event.  A crash between phases leaves no false
        # claim; the pointer itself remains the fact source.
        if pointer is not None:
            write_json(output_root / "latest_run.json", pointer)
            if read_json(output_root / "latest_run.json") != pointer:
                raise PipelineError("latest pointer read-back differs from the committed object")
            latest_pointer_written = True
            append_ndjson(
                output_root / "run_ledger.ndjson",
                {
                    "record_type": "LATEST_POINTER_UPDATED",
                    "run_id": run_id,
                    "as_of": as_of,
                    "recorded_at": now(),
                    "manifest_sha256": manifest_sha256,
                    "target_path": pointer["target_path"],
                    "latest_pointer_updated": True,
                },
            )
        return {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "manifest": str(run_dir / "manifest.json"),
            "dashboard": str(run_dir / "dashboard.html"),
            "artifact_publish_state": artifact_state,
            "live_validation_state": live_state,
            "axes": axis_states,
            "validation": final_validation,
            "offline_rebuild_validation": offline_rebuild_validation,
        }
    except Exception as exc:
        error = _safe_error(exc)
        if validated_candidate:
            try:
                append_ndjson(
                    output_root / "run_ledger.ndjson",
                    {
                        "record_type": "PUBLICATION_FAILURE",
                        "run_id": run_id,
                        "as_of": as_of,
                        "recorded_at": now(),
                        "execution_status": "COMPLETED",
                        "artifact_publish_state": artifact_state,
                        "live_validation_state": live_state,
                        "axes": axis_states,
                        "manifest_sha256": sha256_file(run_dir / "manifest.json"),
                        "fresh_process_validation": offline_rebuild_validation[
                            "fresh_process_validation"
                        ],
                        "fresh_process_rebuild": offline_rebuild_validation[
                            "fresh_process_rebuild"
                        ],
                        "derived_rebuild_files": offline_rebuild_validation["files"],
                        "latest_pointer_updated": latest_pointer_written,
                        "reason_code": error["reason_code"],
                    },
                )
            except Exception:
                pass
            raise PipelineError(
                f"run {run_id} passed validation but publication failed; "
                f"candidate retained unchanged at {run_dir}: {error['reason_code']}"
            ) from exc
        audit["execution_error"] = error
        audit["status"] = {
            "execution_status": "FAILED",
            "artifact_publish_state": "FAILED",
            "live_validation_state": "FAIL",
            "sw2021_axis_state": axis_states["SW2021"],
            "sw2014_axis_state": axis_states["SW2014"],
            "research_grade": "RESEARCH_ONLY",
            "decision_eligible": False,
            "production_approved": False,
        }
        audit["completed_at"] = now()
        write_json(run_dir / "audit.json", audit)
        manifest = {
            "schema_version": "swivd-run-manifest-v3",
            "run_id": run_id,
            "as_of": as_of,
            "created_at": created_at,
            "completed_at": now(),
            "execution_status": "FAILED",
            "artifact_publish_state": "FAILED",
            "live_validation_state": "FAIL",
            "axes": dict(axis_states),
            "research_grade": "RESEARCH_ONLY",
            "decision_eligible": False,
            "production_approved": False,
            "contract_version": spec.get("contract_version"),
            "spec_version": spec.get("schema_version"),
            "semantic_successor_id": spec.get("semantic_successor_id"),
            "name_history_successor_id": spec.get("name_history_successor_id"),
            "error": error,
            "source": {
                "provider": "Tushare Pro",
                "transport": "HTTPS_POST_NO_REDIRECT",
                "apis": ["trade_cal", "index_classify", "sw_daily"],
            },
            "spec_sha256": sha256_file(spec_path),
            "contract_sha256": sha256_file(project_root / "PROJECT_CONTRACT.md"),
            "source_files": source_files_before,
            "request_count": len(request_log),
            "validation": {"status": "NOT_RUN_DUE_TO_EXECUTION_FAILURE"},
            "artifacts": [],
        }
        _write_manifest_and_sums(run_dir, manifest)
        append_ndjson(
            output_root / "run_ledger.ndjson",
            {
                "run_id": run_id,
                "as_of": as_of,
                "recorded_at": now(),
                "execution_status": "FAILED",
                "artifact_publish_state": "FAILED",
                "live_validation_state": "FAIL",
                "axes": axis_states,
                "manifest_sha256": sha256_file(run_dir / "manifest.json"),
                "reason_code": error["reason_code"],
            },
        )
        raise PipelineError(f"run {run_id} failed; evidence retained at {run_dir}: {error['reason_code']}") from exc


def _restore_daily(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    return [{key: (None if value == "" else value) for key, value in row.items() if key != "taxonomy"} for row in rows]


def _restore_classifications(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    source_fields = set(CLASSIFICATION_FIELDS)
    return [
        {
            key: (None if value == "" else value)
            for key, value in row.items()
            if key in source_fields
        }
        for row in rows
    ]


def rebuild_derived(*, run_dir: str | Path, output_dir: str | Path) -> dict[str, Any]:
    """Rebuild tables and HTML only from frozen normalized inputs, without network."""

    run_dir = Path(run_dir).resolve()
    output_dir = Path(output_dir).resolve()
    canonical_runs_root = (CANONICAL_PROJECT_ROOT / "output" / "runs").resolve()
    if output_dir == run_dir or run_dir in output_dir.parents:
        raise PipelineError("rebuild output_dir must not modify the immutable source run")
    if output_dir == canonical_runs_root or canonical_runs_root in output_dir.parents:
        raise PipelineError("rebuild output_dir must be outside canonical output/runs")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise PipelineError("rebuild output_dir must be absent or empty")
    from .validator import validate_run

    validate_run(run_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = read_json(run_dir / "manifest.json")
    audit = read_json(run_dir / "audit.json")
    calendar = read_csv(run_dir / "inputs" / "normalized" / "trade_calendar.csv")
    classifications_2021 = _restore_classifications(
        read_csv(run_dir / "inputs" / "normalized" / "classification_sw2021.csv")
    )
    daily_2021 = _restore_daily(
        read_csv(run_dir / "inputs" / "normalized" / "sw_daily_sw2021.csv")
    )
    summary_2021 = _canonical_summary(
        summarize_axis(
            daily_2021,
            classifications_2021,
            calendar,
            src="SW2021",
            as_of=manifest["as_of"],
            minimum_valid_observations=252,
        ),
        classifications_2021,
        "SW2021",
    )
    history_2021 = _canonical_history(
        normalize_history(daily_2021, classifications_2021, src="SW2021"),
        "SW2021",
    )
    summary_2014: list[dict[str, Any]] = []
    history_2014: list[dict[str, Any]] = []
    archive_end = audit["trade_calendar"]["legacy_end"]
    if manifest["axes"]["SW2014"] == "PASS":
        classifications_2014 = _restore_classifications(
            read_csv(run_dir / "inputs" / "normalized" / "classification_sw2014.csv")
        )
        daily_2014 = _restore_daily(
            read_csv(run_dir / "inputs" / "normalized" / "sw_daily_sw2014.csv")
        )
        summary_2014 = _canonical_summary(
            summarize_axis(
                daily_2014,
                classifications_2014,
                calendar,
                src="SW2014",
                as_of=archive_end,
                minimum_valid_observations=252,
            ),
            classifications_2014,
            "SW2014",
        )
        history_2014 = _canonical_history(
            normalize_history(daily_2014, classifications_2014, src="SW2014"),
            "SW2014",
        )
    write_csv(output_dir / "sw2021_current.csv", summary_2021, SUMMARY_FIELDS)
    write_csv(output_dir / "sw2021_history.csv", history_2021, SW2021_HISTORY_FIELDS)
    write_csv(output_dir / "sw2014_archive.csv", summary_2014, SUMMARY_FIELDS)
    write_csv(output_dir / "sw2014_history.csv", history_2014, SW2014_HISTORY_FIELDS)
    metadata = {
        "run_id": manifest["run_id"],
        "as_of": manifest["as_of"],
        "archive_end": archive_end,
        "source": "Tushare Pro sw_daily 原始行业指数字段",
        "taxonomy": "SW2021 L1 / SW2014 L1 独立轴",
        "contract_version": manifest["contract_version"],
        "spec_version": manifest["spec_version"],
        "formula": "count(x <= current) / valid_count * 100",
        "generated_at": audit["completed_at"],
        "statuses": audit["status"],
    }
    html = render_dashboard(
        current_rows=summary_2021,
        sw2021_history=history_2021,
        sw2014_summary=summary_2014,
        sw2014_history=history_2014,
        metadata=metadata,
        audit=audit,
    )
    atomic_write_text(output_dir / "dashboard.html", html)
    atomic_write_text(
        output_dir / "reports" / "adversarial_review.md",
        _adversarial_report(
            run_id=manifest["run_id"],
            as_of=manifest["as_of"],
            axis_states=manifest["axes"],
            audit=audit,
        ),
    )
    return {
        "run_id": manifest["run_id"],
        "output_dir": str(output_dir),
        "files": inventory_files(output_dir),
    }


__all__ = ["PipelineError", "rebuild_derived", "run_live"]
