from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from swivd.validator import (  # noqa: E402
    HISTORY_FIELDS,
    SUMMARY_FIELDS,
    SW2014_HISTORY_FIELDS,
    ValidationError,
    validate_run,
    validate_spec,
)
from swivd.render import render_dashboard  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _write_csv(path: Path, header: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(header))
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def _summary_row(
    taxonomy: str,
    code: str,
    name: str,
    as_of: str,
    *,
    valid_count: int,
    first_valid_date: str,
) -> dict[str, str]:
    return {
        "taxonomy": taxonomy,
        "index_code": code,
        "industry_name": name,
        "as_of": as_of,
        "pe": "20",
        "pb": "2",
        "pe_percentile": "100",
        "pb_percentile": "100",
        "pe_valid_count": str(valid_count),
        "pb_valid_count": str(valid_count),
        "pe_tie_count": str(valid_count),
        "pb_tie_count": str(valid_count),
        "pe_tie_ratio": "1",
        "pb_tie_ratio": "1",
        "pe_first_valid_date": first_valid_date,
        "pe_last_valid_date": as_of,
        "pb_first_valid_date": first_valid_date,
        "pb_last_valid_date": as_of,
        "pe_label": "历史极高位",
        "pb_label": "历史极高位",
        "return_5d": "0",
        "return_mtd": "0",
        "return_ytd": "0",
        "valuation_state": "OK",
        "return_state": "OK",
    }


def _calendar_days(start: str, count: int) -> list[str]:
    cursor = date(int(start[:4]), int(start[4:6]), int(start[6:]))
    return [(cursor + timedelta(days=offset)).strftime("%Y%m%d") for offset in range(count)]


def _source_file_records() -> list[dict[str, Any]]:
    paths = [
        PROJECT_ROOT / "PROJECT_CONTRACT.md",
        PROJECT_ROOT / "PROJECT_SPEC.json",
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "run_dashboard.py",
        *sorted((PROJECT_ROOT / "src" / "swivd").glob("*.py")),
        *sorted((PROJECT_ROOT / "tests").glob("test_*.py")),
    ]
    return [
        {
            "path": path.relative_to(PROJECT_ROOT).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in paths
    ]


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _raw_response(fields: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "code": 0,
        "msg": "",
        "data": {
            "fields": list(fields),
            "items": [[row[field] for field in fields] for row in rows],
        },
    }


def _audit_requests(run_root: Path, *, as_of: str, legacy_end: str) -> list[dict[str, Any]]:
    endpoint_fields = {
        "trade_cal": ["exchange", "cal_date", "is_open", "pretrade_date"],
        "index_classify": [
            "index_code",
            "industry_name",
            "parent_code",
            "level",
            "industry_code",
            "is_pub",
            "src",
        ],
        "sw_daily": [
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
        ],
    }
    raw_root = run_root / "inputs" / "raw"
    requests: list[dict[str, Any]] = []
    for path in sorted(raw_root.rglob("*.json")):
        parts = path.relative_to(raw_root).parts
        if parts[0] == "trade_cal":
            api_name = "trade_cal"
            params = {
                "exchange": "SSE",
                "start_date": "20140101",
                "end_date": as_of,
                "is_open": "1",
            }
        elif parts[0] == "index_classify":
            api_name = "index_classify"
            taxonomy = parts[1].split("_", 1)[0]
            params = {"level": "L1", "src": taxonomy}
        else:
            api_name = "sw_daily"
            taxonomy = parts[1]
            code = parts[2][:-5].replace("_", ".")
            params = {
                "ts_code": code,
                "start_date": "20211213" if taxonomy == "SW2021" else "20140101",
                "end_date": as_of if taxonomy == "SW2021" else legacy_end,
            }
        response = json.loads(path.read_text(encoding="utf-8"))
        data = response.get("data")
        success = (
            response.get("code") == 0
            and isinstance(data, dict)
            and data.get("fields") == endpoint_fields[api_name]
            and isinstance(data.get("items"), list)
            and all(
                isinstance(item, list) and len(item) == len(endpoint_fields[api_name])
                for item in data.get("items", [])
            )
        )
        request = {
            "api_name": api_name,
            "params": params,
            "fields": endpoint_fields[api_name],
            "row_count": len(data["items"]) if success else None,
            "decode_state": "PASS" if success else "BLOCKED",
            "http_status": 200,
            "attempt_count": 1,
            "raw_path": str(path.resolve()),
            "raw_sha256": _sha256(path),
        }
        if not success:
            request["reason_code"] = "SYNTHETIC_BLOCK"
        requests.append(request)
    return requests


def _history_row(
    taxonomy: str,
    code: str,
    name: str,
    trade_date: str,
    *,
    source_name: str | None = None,
) -> dict[str, str]:
    row = {
        "taxonomy": taxonomy,
        "index_code": code,
        "industry_name": name,
        "trade_date": trade_date,
        "close": "100",
        "pe": "20",
        "pb": "2",
        "is_pub": "" if taxonomy == "SW2014" else "1",
    }
    if taxonomy == "SW2014":
        row["source_name"] = name if source_name is None else source_name
    return row


def _mutate_daily_value(
    fixture: Any,
    *,
    taxonomy: str,
    code: str,
    trade_date: str,
    field: str,
    value: str,
) -> None:
    suffix = taxonomy.lower()
    daily_rows = fixture.daily_2021 if taxonomy == "SW2021" else fixture.daily_2014
    matched = [
        row
        for row in daily_rows
        if row["ts_code"] == code and row["trade_date"] == trade_date
    ]
    if len(matched) != 1:
        raise AssertionError("fixture daily row is not unique")
    matched[0][field] = value

    normalized_path = (
        fixture.root / "inputs" / "normalized" / f"sw_daily_{suffix}.csv"
    )
    header, normalized_rows = _read_csv(normalized_path)
    normalized_match = [
        row
        for row in normalized_rows
        if row["ts_code"] == code and row["trade_date"] == trade_date
    ]
    if len(normalized_match) != 1:
        raise AssertionError("normalized fixture daily row is not unique")
    normalized_match[0][field] = value
    _write_csv(normalized_path, header, normalized_rows)

    raw_path = (
        fixture.root
        / "inputs"
        / "raw"
        / "sw_daily"
        / taxonomy
        / f"{code.replace('.', '_')}.json"
    )
    response = json.loads(raw_path.read_text(encoding="utf-8"))
    fields = response["data"]["fields"]
    code_position = fields.index("ts_code")
    date_position = fields.index("trade_date")
    field_position = fields.index(field)
    raw_match = [
        item
        for item in response["data"]["items"]
        if item[code_position] == code and item[date_position] == trade_date
    ]
    if len(raw_match) != 1:
        raise AssertionError("raw fixture daily row is not unique")
    raw_match[0][field_position] = value
    _write_json(raw_path, response)


def _payload_summary(row: Mapping[str, str]) -> dict[str, Any]:
    return {
        "taxonomy": row["taxonomy"],
        "ts_code": row["index_code"],
        "index_code": row["index_code"],
        "industry_name": row["industry_name"],
        "trade_date": row["as_of"],
        "as_of": row["as_of"],
        "pe": row["pe"],
        "pb": row["pb"],
        "pe_percentile_le": row["pe_percentile"],
        "pb_percentile_le": row["pb_percentile"],
        "pe_valid_count": row["pe_valid_count"],
        "pb_valid_count": row["pb_valid_count"],
        "pe_tie_count": row["pe_tie_count"],
        "pb_tie_count": row["pb_tie_count"],
        "pe_tie_ratio": row["pe_tie_ratio"],
        "pb_tie_ratio": row["pb_tie_ratio"],
        "pe_first_valid_date": row["pe_first_valid_date"],
        "pe_last_valid_date": row["pe_last_valid_date"],
        "pb_first_valid_date": row["pb_first_valid_date"],
        "pb_last_valid_date": row["pb_last_valid_date"],
        "return_5d": row["return_5d"],
        "return_mtd": row["return_mtd"],
        "return_ytd": row["return_ytd"],
        "pe_label": row["pe_label"],
        "pb_label": row["pb_label"],
        "valuation_state": row["valuation_state"],
        "return_state": row["return_state"],
        "status": row["valuation_state"],
        "is_pub": None if row["taxonomy"] == "SW2014" else "1",
    }


def _payload_history(row: Mapping[str, str]) -> dict[str, Any]:
    payload = {
        "taxonomy": row["taxonomy"],
        "ts_code": row["index_code"],
        "index_code": row["index_code"],
        "industry_name": row["industry_name"],
        "trade_date": row["trade_date"],
        "close": row["close"],
        "pe": row["pe"],
        "pb": row["pb"],
        "is_pub": None if row["taxonomy"] == "SW2014" else row["is_pub"],
    }
    if "source_name" in row:
        payload["source_name"] = row["source_name"]
    return payload


def _name_history(rows: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row["index_code"], []).append(row)
    segments: list[dict[str, Any]] = []
    renamed_codes: list[str] = []
    for code in sorted(grouped):
        ordered = sorted(grouped[code], key=lambda row: row["trade_date"])
        code_segments: list[dict[str, Any]] = []
        current_name = ordered[0]["source_name"]
        first_date = ordered[0]["trade_date"]
        last_date = first_date
        row_count = 1
        for row in ordered[1:]:
            if row["source_name"] == current_name:
                last_date = row["trade_date"]
                row_count += 1
                continue
            code_segments.append(
                {
                    "index_code": code,
                    "source_name": current_name,
                    "first_date": first_date,
                    "last_date": last_date,
                    "row_count": row_count,
                }
            )
            current_name = row["source_name"]
            first_date = row["trade_date"]
            last_date = first_date
            row_count = 1
        code_segments.append(
            {
                "index_code": code,
                "source_name": current_name,
                "first_date": first_date,
                "last_date": last_date,
                "row_count": row_count,
            }
        )
        if len(code_segments) > 1:
            renamed_codes.append(code)
        segments.extend(code_segments)
    return {
        "code_count": len(grouped),
        "renamed_code_count": len(renamed_codes),
        "renamed_codes": renamed_codes,
        "segment_count": len(segments),
        "segments": segments,
    }


def _ohlc_audit(rows: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    anomalies: list[dict[str, Any]] = []
    fully_observed = 0
    unchecked_missing = 0
    for row in rows:
        values = {
            field: Decimal(row[field]) if row[field].strip() else None
            for field in ("open", "low", "high", "close")
        }
        if any(values[field] is None for field in ("open", "low", "high")):
            unchecked_missing += 1
            continue
        fully_observed += 1
        open_value = values["open"]
        low = values["low"]
        high = values["high"]
        close = values["close"]
        assert None not in (open_value, low, high, close)
        codes = []
        if high < low:
            codes.append("HIGH_BELOW_LOW")
        if high < open_value:
            codes.append("HIGH_BELOW_OPEN")
        if high < close:
            codes.append("HIGH_BELOW_CLOSE")
        if low > open_value:
            codes.append("LOW_ABOVE_OPEN")
        if low > close:
            codes.append("LOW_ABOVE_CLOSE")
        if codes:
            anomalies.append(
                {
                    "ts_code": row["ts_code"],
                    "trade_date": row["trade_date"],
                    "anomaly_codes": sorted(codes),
                    "open": format(open_value, "f"),
                    "low": format(low, "f"),
                    "high": format(high, "f"),
                    "close": format(close, "f"),
                }
            )
    anomalies.sort(key=lambda item: (item["ts_code"], item["trade_date"]))
    return {
        "policy": "DISCLOSE_NON_BLOCKING_OHLC_ORDERING",
        "blocking": False,
        "formula_fields": ["close", "pe", "pb"],
        "row_count": len(rows),
        "fully_observed_row_count": fully_observed,
        "unchecked_missing_ohl_row_count": unchecked_missing,
        "anomaly_count": len(anomalies),
        "anomalies": anomalies,
    }


def _report_text(
    *,
    run_id: str,
    as_of: str,
    sw2014_state: str,
    renamed_codes: Sequence[str],
    current_ohlc_count: int,
    legacy_ohlc_count: int | str,
) -> str:
    legacy_line = (
        "SW2014 轴已闭合：原样保留退役分类 is_pub=null，按每个行业自身首观测日"
        "至共同末日校验无内部交易日缺口。"
        if sw2014_state == "PASS"
        else "SW2014 轴失败关闭：{\"reason_code\": \"TRANSPORT_BEFORE_RESPONSE\"}"
    )
    return f"""# {run_id} 对抗式审查

- 数据截止日：`{as_of}`
- SW2021 轴：`PASS`
- SW2014 轴：`{sw2014_state}`
- 研究等级：`RESEARCH_ONLY`
- 决策资格：`false`
- 生产批准：`false`

## 已主动拒绝的错误路径

1. 没有用个股 PE/PB 聚合替代 `sw_daily.pe/pb`。
2. 没有把 SW2014 与 SW2021 拼成一个历史百分位。
3. 没有按名称拼接或覆盖 SW2014 历史。`ts_code` 是身份，`source_name` 保留
   上游日标签；实际改名代码为 `{json.dumps(list(renamed_codes), ensure_ascii=False)}`。
4. `open/low/high` 不参与本项目公式；其与 `close` 的排序异常原样保留并披露、
   不改写源值，也不冒充 `close/pe/pb` 有效性。SW2021 异常行 `{current_ohlc_count}`，
   SW2014 异常行 `{legacy_ohlc_count}`。

## 分轴结论

{legacy_line}

历史位置采用用户确认的经验分布函数 `count(x <= current) / N`。历史位置只是描述性统计，
不构成内在价值或未来收益判断。
"""


class RunFixture:
    def __init__(self, parent: Path, *, sw2014_state: str = "PASS") -> None:
        self.as_of = "20221230"
        self.run_id = f"SWIVD-RUN-{self.as_of}-001"
        self.created_at = "2026-08-30T22:40:00+08:00"
        self.completed_at = "2026-08-30T22:41:00+08:00"
        self.root = parent / self.run_id
        self.root.mkdir()
        (self.root / "inputs" / "raw").mkdir(parents=True)

        self.codes_2021 = [f"801{index:03d}.SI" for index in range(1, 32)]
        self.codes_2014 = [f"851{index:03d}.SI" for index in range(1, 29)]
        classify_header = (
            "index_code",
            "industry_name",
            "parent_code",
            "level",
            "industry_code",
            "is_pub",
            "src",
            "publication_state",
            "selection_basis",
        )
        classify_2021 = [
            {
                "index_code": code,
                "industry_name": f"当前行业{index}",
                "parent_code": "",
                "level": "L1",
                "industry_code": f"21{index:04d}",
                "is_pub": "1",
                "src": "SW2021",
                "publication_state": "PUBLISHED",
                "selection_basis": "PUBLISHED_ONLY",
            }
            for index, code in enumerate(self.codes_2021, start=1)
        ]
        classify_2014 = [
            {
                "index_code": code,
                "industry_name": f"历史行业{index}",
                "parent_code": "",
                "level": "L1",
                "industry_code": f"14{index:04d}",
                "is_pub": "",
                "src": "SW2014",
                "publication_state": "NOT_PROVIDED_FOR_RETIRED_TAXONOMY",
                "selection_basis": "ALL_CLASSIFIED_L1_ROWS_FOR_RETIRED_TAXONOMY",
            }
            for index, code in enumerate(self.codes_2014, start=1)
        ]
        self.classify_2021 = classify_2021
        self.classify_2014 = classify_2014
        normalized = self.root / "inputs" / "normalized"
        _write_csv(normalized / "classification_sw2021.csv", classify_header, classify_2021)
        _write_csv(
            normalized / "classification_sw2014.csv",
            classify_header,
            classify_2014 if sw2014_state == "PASS" else [],
        )
        self.legacy_dates = sorted(
            {
                *_calendar_days("20140102", 245),
                "20201231",
                "20211130",
                "20211203",
                "20211206",
                "20211207",
                "20211208",
                "20211209",
                "20211210",
            }
        )
        self.current_dates = sorted(
            {
                *_calendar_days("20211213", 245),
                "20221130",
                "20221223",
                "20221226",
                "20221227",
                "20221228",
                "20221229",
                self.as_of,
            }
        )

        def legacy_source_name(index: int, trade_date: str) -> str:
            if index == 1 and trade_date in self.legacy_dates[:2]:
                return "旧历史行业1"
            return f"历史行业{index}"

        all_dates = sorted({*self.legacy_dates, *self.current_dates})
        self.calendar_rows = []
        previous = "20131231"
        for value in all_dates:
            self.calendar_rows.append(
                {
                    "exchange": "SSE",
                    "cal_date": value,
                    "is_open": "1",
                    "pretrade_date": previous,
                }
            )
            previous = value
        _write_csv(
            normalized / "trade_calendar.csv",
            ("exchange", "cal_date", "is_open", "pretrade_date"),
            self.calendar_rows,
        )
        raw_daily_fields = (
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
        )
        daily_header = ("taxonomy", *raw_daily_fields)
        self.daily_2021 = [
            {
                "taxonomy": "SW2021",
                "ts_code": code,
                "trade_date": trade_date,
                "name": f"当前行业{i}",
                "open": "100",
                "low": "100",
                "high": "100",
                "close": "100",
                "change": "0",
                "pct_change": "0",
                "vol": "1000",
                "amount": "100000",
                "pe": "20",
                "pb": "2",
                "total_mv": "1000000",
                "float_mv": "800000",
            }
            for i, code in enumerate(self.codes_2021, start=1)
            for trade_date in self.current_dates
        ]
        self.daily_2014 = (
            []
            if sw2014_state == "BLOCKED"
            else [
                {
                    "taxonomy": "SW2014",
                    "ts_code": code,
                    "trade_date": trade_date,
                    "name": legacy_source_name(i, trade_date),
                    "open": "100",
                    "low": "100",
                    "high": "100",
                    "close": "100",
                    "change": "0",
                    "pct_change": "0",
                    "vol": "1000",
                    "amount": "100000",
                    "pe": "20",
                    "pb": "2",
                    "total_mv": "1000000",
                    "float_mv": "800000",
                }
                for i, code in enumerate(self.codes_2014, start=1)
                for trade_date in (self.legacy_dates[1:] if i == 2 else self.legacy_dates)
            ]
        )
        _write_csv(
            normalized / "sw_daily_sw2021.csv",
            daily_header,
            self.daily_2021,
        )
        _write_csv(
            normalized / "sw_daily_sw2014.csv",
            daily_header,
            self.daily_2014,
        )

        raw_root = self.root / "inputs" / "raw"
        _write_json(
            raw_root / "trade_cal" / f"SSE_20140101_{self.as_of}.json",
            _raw_response(("exchange", "cal_date", "is_open", "pretrade_date"), self.calendar_rows),
        )
        raw_classification_fields = classify_header[:7]
        raw_classify_2021 = [
            {field: row[field] for field in raw_classification_fields}
            for row in classify_2021
        ]
        raw_classify_2014 = [
            {
                **{field: row[field] for field in raw_classification_fields},
                "is_pub": None,
            }
            for row in classify_2014
        ]
        _write_json(
            raw_root / "index_classify" / "SW2021_L1.json",
            _raw_response(raw_classification_fields, raw_classify_2021),
        )
        if sw2014_state == "PASS":
            _write_json(
                raw_root / "index_classify" / "SW2014_L1.json",
                _raw_response(raw_classification_fields, raw_classify_2014),
            )
        for taxonomy, codes, rows in (
            ("SW2021", self.codes_2021, self.daily_2021),
            ("SW2014", self.codes_2014, self.daily_2014),
        ):
            for code in codes:
                code_rows = [
                    {field: row[field] for field in raw_daily_fields}
                    for row in rows
                    if row["ts_code"] == code
                ]
                if not code_rows:
                    continue
                _write_json(
                    raw_root / "sw_daily" / taxonomy / f"{code.replace('.', '_')}.json",
                    _raw_response(raw_daily_fields, code_rows),
                )

        self.current = [
            _summary_row(
                "SW2021",
                code,
                f"当前行业{i}",
                self.as_of,
                valid_count=len(self.current_dates),
                first_valid_date=self.current_dates[0],
            )
            for i, code in enumerate(self.codes_2021, start=1)
        ]
        self.history_2021 = [
            _history_row("SW2021", code, f"当前行业{i}", trade_date)
            for i, code in enumerate(self.codes_2021, start=1)
            for trade_date in self.current_dates
        ]
        self.archive = (
            []
            if sw2014_state == "BLOCKED"
            else [
                _summary_row(
                    "SW2014",
                    code,
                    f"历史行业{i}",
                    "20211210",
                    valid_count=len(self.legacy_dates[1:] if i == 2 else self.legacy_dates),
                    first_valid_date=(
                        self.legacy_dates[1] if i == 2 else self.legacy_dates[0]
                    ),
                )
                for i, code in enumerate(self.codes_2014, start=1)
            ]
        )
        self.history_2014 = (
            []
            if sw2014_state == "BLOCKED"
            else [
                _history_row(
                    "SW2014",
                    code,
                    f"历史行业{i}",
                    trade_date,
                    source_name=legacy_source_name(i, trade_date),
                )
                for i, code in enumerate(self.codes_2014, start=1)
                for trade_date in (self.legacy_dates[1:] if i == 2 else self.legacy_dates)
            ]
        )
        tables = self.root / "tables"
        _write_csv(tables / "sw2021_current.csv", SUMMARY_FIELDS, self.current)
        _write_csv(tables / "sw2021_history.csv", HISTORY_FIELDS, self.history_2021)
        _write_csv(tables / "sw2014_archive.csv", SUMMARY_FIELDS, self.archive)
        _write_csv(
            tables / "sw2014_history.csv", SW2014_HISTORY_FIELDS, self.history_2014
        )

        sw2014_audit: dict[str, Any] = {
            "state": sw2014_state,
            "reason_codes": []
            if sw2014_state == "PASS"
            else ["TRANSPORT_BEFORE_RESPONSE"],
        }
        if sw2014_state == "BLOCKED":
            sw2014_audit["error"] = {
                "type": "RuntimeError",
                "reason_code": "TRANSPORT_BEFORE_RESPONSE",
                "message": "synthetic transport failure before response",
            }
        if sw2014_state == "PASS":
            sw2014_audit.update(
                {
                    "classification_count": 28,
                    "publication_state": "NOT_PROVIDED_FOR_RETIRED_TAXONOMY",
                    "selection_basis": "ALL_CLASSIFIED_L1_ROWS_FOR_RETIRED_TAXONOMY",
                    "selected_count": 28,
                    "publication_flag_null_count": 28,
                    "continuity_policy": "PER_CODE_OBSERVED_INCEPTION_TO_COMMON_END",
                    "name_history_policy": "STABLE_TS_CODE_WITH_SOURCE_NAME_HISTORY",
                    "name_history": _name_history(self.history_2014),
                    "ohlc_ordering": _ohlc_audit(self.daily_2014),
                }
            )
        self.statuses = {
            "execution_status": "COMPLETED",
            "artifact_publish_state": (
                "LOCAL_RESEARCH_CANDIDATE_COMPLETE"
                if sw2014_state == "PASS"
                else "LOCAL_RESEARCH_CANDIDATE_PARTIAL"
            ),
            "live_validation_state": "PASS" if sw2014_state == "PASS" else "PARTIAL",
            "sw2021_axis_state": "PASS",
            "sw2014_axis_state": sw2014_state,
            "research_grade": "RESEARCH_ONLY",
            "decision_eligible": False,
            "production_approved": False,
        }
        self.audit = {
            "schema_version": "swivd-audit-v3",
            "run_id": self.run_id,
            "as_of": self.as_of,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "contract_version": "swivd-contract-v1.2.0",
            "spec_version": "swivd-project-spec-v3",
            "semantic_successor_id": "GOV-20260830-001-SW2014-SEMANTICS-AUTHORIZED",
            "name_history_successor_id": "GOV-20260830-001-NAME-HISTORY-AUTHORIZED",
            "status": self.statuses,
            "axes": {
                "SW2021": {
                    "state": "PASS",
                    "reason_codes": [],
                    "classification_count": 31,
                    "selected_count": 31,
                    "published_count": 31,
                    "unpublished_count": 0,
                    "publication_flag_null_count": 0,
                    "publication_state": "BINARY_FLAG_PROVIDED",
                    "selection_basis": "PUBLISHED_ONLY",
                    "continuity_policy": "COMMON_START_RECTANGLE",
                    "name_history_policy": "CONSTANT_NAME_REQUIRED",
                    "ohlc_ordering": _ohlc_audit(self.daily_2021),
                },
                "SW2014": sw2014_audit,
            },
            "trade_calendar": {
                "state": "PASS",
                "row_count": len(self.calendar_rows),
                "first_open_date": self.calendar_rows[0]["cal_date"],
                "last_open_date": self.calendar_rows[-1]["cal_date"],
                "legacy_end": self.legacy_dates[-1],
            },
            "requests": _audit_requests(
                self.root, as_of=self.as_of, legacy_end=self.legacy_dates[-1]
            ),
        }
        self.metadata = {
            "run_id": self.run_id,
            "as_of": self.as_of,
            "source": "Tushare Pro sw_daily 原始行业指数字段",
            "taxonomy": "SW2021 L1 / SW2014 L1 独立轴",
            "spec_version": "swivd-project-spec-v3",
            "contract_version": "swivd-contract-v1.2.0",
            "formula": "count(x <= current) / valid_count * 100",
            "archive_end": self.legacy_dates[-1],
            "generated_at": self.completed_at,
            "statuses": self.statuses,
        }
        self.payload = {
            "schema_version": "swivd-dashboard-payload-v1",
            "metadata": self.metadata,
            "audit": self.audit,
            "current_rows": [_payload_summary(row) for row in self.current],
            "sw2021_history": [_payload_history(row) for row in self.history_2021],
            "sw2014_summary": [_payload_summary(row) for row in self.archive],
            "sw2014_history": [_payload_history(row) for row in self.history_2014],
        }
        self.payload["sw2014_policy"] = {
            "publication_state": "NOT_PROVIDED_FOR_RETIRED_TAXONOMY",
            "selection_basis": "ALL_CLASSIFIED_L1_ROWS_FOR_RETIRED_TAXONOMY",
            "continuity_policy": "PER_CODE_OBSERVED_INCEPTION_TO_COMMON_END",
            "selected_count": 28 if sw2014_state == "PASS" else None,
            "publication_flag_null_count": 28 if sw2014_state == "PASS" else None,
        }
        if sw2014_state == "PASS":
            self.payload["sw2014_policy"].update(
                {
                "name_history_policy": sw2014_audit["name_history_policy"],
                "name_history": sw2014_audit["name_history"],
                }
            )
        self.write_canonical_html()
        (self.root / "audit.json").write_text(
            json.dumps(
                self.audit,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        report = self.root / "reports" / "adversarial_review.md"
        report.parent.mkdir(parents=True)
        report.write_text(
            _report_text(
                run_id=self.run_id,
                as_of=self.as_of,
                sw2014_state=sw2014_state,
                renamed_codes=(
                    sw2014_audit["name_history"]["renamed_codes"]
                    if sw2014_state == "PASS"
                    else []
                ),
                current_ohlc_count=self.audit["axes"]["SW2021"]["ohlc_ordering"][
                    "anomaly_count"
                ],
                legacy_ohlc_count=(
                    sw2014_audit["ohlc_ordering"]["anomaly_count"]
                    if sw2014_state == "PASS"
                    else "AXIS_BLOCKED"
                ),
            ),
            encoding="utf-8",
        )

        self.manifest = {
            "schema_version": "swivd-run-manifest-v3",
            "contract_version": "swivd-contract-v1.2.0",
            "spec_version": "swivd-project-spec-v3",
            "semantic_successor_id": "GOV-20260830-001-SW2014-SEMANTICS-AUTHORIZED",
            "name_history_successor_id": "GOV-20260830-001-NAME-HISTORY-AUTHORIZED",
            "run_id": self.run_id,
            "as_of": self.as_of,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "execution_status": "COMPLETED",
            "artifact_publish_state": (
                "LOCAL_RESEARCH_CANDIDATE_COMPLETE"
                if sw2014_state == "PASS"
                else "LOCAL_RESEARCH_CANDIDATE_PARTIAL"
            ),
            "live_validation_state": "PASS" if sw2014_state == "PASS" else "PARTIAL",
            "axes": {"SW2021": "PASS", "SW2014": sw2014_state},
            "research_grade": "RESEARCH_ONLY",
            "decision_eligible": False,
            "production_approved": False,
            "source": {
                "provider": "Tushare Pro",
                "transport": "HTTPS_POST_NO_REDIRECT",
                "apis": ["trade_cal", "index_classify", "sw_daily"],
            },
            "runtime": {
                "python_implementation": "CPython",
                "python_version": "3.test",
                "executable": "/usr/bin/python3",
                "platform": "fixture-platform",
                "dependency_policy": "STDLIB_HTTP_WITH_OPTIONAL_CERTIFI_CA_AND_TUSHARE_TOKEN_LOOKUP",
                "package_versions": {"certifi": None, "tushare": None},
            },
            "spec_sha256": _sha256(PROJECT_ROOT / "PROJECT_SPEC.json"),
            "contract_sha256": _sha256(PROJECT_ROOT / "PROJECT_CONTRACT.md"),
            "source_files": _source_file_records(),
            "request_count": len(list((self.root / "inputs" / "raw").rglob("*.json"))),
            "artifacts": [],
            "validation": {
                "status": "PASS",
                "entrypoint": "swivd.validator.validate_run",
            },
        }
        self.reclose()

    def sync_requests(self) -> None:
        self.audit["requests"] = _audit_requests(
            self.root, as_of=self.as_of, legacy_end=self.legacy_dates[-1]
        )
        self.payload["audit"] = self.audit
        (self.root / "audit.json").write_text(
            json.dumps(self.audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def write_report(self) -> None:
        legacy_axis = self.audit["axes"]["SW2014"]
        (self.root / "reports" / "adversarial_review.md").write_text(
            _report_text(
                run_id=self.run_id,
                as_of=self.as_of,
                sw2014_state=legacy_axis["state"],
                renamed_codes=(
                    legacy_axis["name_history"]["renamed_codes"]
                    if legacy_axis["state"] == "PASS"
                    else []
                ),
                current_ohlc_count=self.audit["axes"]["SW2021"][
                    "ohlc_ordering"
                ]["anomaly_count"],
                legacy_ohlc_count=(
                    legacy_axis["ohlc_ordering"]["anomaly_count"]
                    if legacy_axis["state"] == "PASS"
                    else "AXIS_BLOCKED"
                ),
            ),
            encoding="utf-8",
        )

    def write_payload(self) -> None:
        (self.root / "dashboard.html").write_text(
            "<!doctype html><meta charset=\"utf-8\"><title>申万行业历史位置</title>"
            '<script id="swivd-data" type="application/json">'
            + json.dumps(self.payload, ensure_ascii=False, separators=(",", ":"))
            + "</script><main>本地研究页面</main>\n",
            encoding="utf-8",
        )

    def write_canonical_html(self) -> None:
        html = render_dashboard(
            self.current,
            self.history_2021,
            metadata=self.metadata,
            audit=self.audit,
            sw2014_summary=self.archive,
            sw2014_history=self.history_2014,
        )
        (self.root / "dashboard.html").write_text(html, encoding="utf-8")

    def reclose(self) -> None:
        manifest_path = self.root / "manifest.json"
        sums_path = self.root / "SHA256SUMS"
        if manifest_path.exists():
            manifest_path.unlink()
        if sums_path.exists():
            sums_path.unlink()
        self.manifest["request_count"] = len(
            list((self.root / "inputs" / "raw").rglob("*.json"))
        )
        artifacts = []
        for path in sorted(candidate for candidate in self.root.rglob("*") if candidate.is_file()):
            artifacts.append(
                {
                    "path": path.relative_to(self.root).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
        self.manifest["artifacts"] = artifacts
        manifest_path.write_text(
            json.dumps(self.manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        sum_lines = []
        for path in sorted(candidate for candidate in self.root.rglob("*") if candidate.is_file()):
            sum_lines.append(f"{_sha256(path)}  {path.relative_to(self.root).as_posix()}")
        sums_path.write_text("\n".join(sum_lines) + "\n", encoding="utf-8")


class AdversarialValidatorTests(unittest.TestCase):
    def test_actual_renderer_output_passes_independent_validator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            audit = json.loads((fixture.root / "audit.json").read_text(encoding="utf-8"))
            html = render_dashboard(
                fixture.current,
                fixture.history_2021,
                metadata=fixture.metadata,
                audit=audit,
                sw2014_summary=fixture.archive,
                sw2014_history=fixture.history_2014,
            )
            (fixture.root / "dashboard.html").write_text(html, encoding="utf-8")
            fixture.reclose()
            self.assertEqual(validate_run(fixture.root)["live_validation_state"], "PASS")

    def test_reclosed_visible_title_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "dashboard.html"
            original = path.read_text(encoding="utf-8")
            tampered = original.replace(
                "<title>申万行业估值全景仪表盘</title>",
                "<title>伪造的生产仪表盘</title>",
                1,
            )
            self.assertNotEqual(original, tampered)
            path.write_text(tampered, encoding="utf-8")
            fixture.reclose()
            with self.assertRaisesRegex(
                ValidationError, "differs from deterministic renderer output"
            ):
                validate_run(fixture.root)

    def test_tmpdir_pointing_at_run_cannot_create_or_modify_run_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))

            def snapshot() -> dict[str, tuple[str, int, int, str | None]]:
                observed: dict[str, tuple[str, int, int, str | None]] = {}
                for path in (fixture.root, *sorted(fixture.root.rglob("*"))):
                    relative = "." if path == fixture.root else path.relative_to(
                        fixture.root
                    ).as_posix()
                    stat = path.stat()
                    observed[relative] = (
                        "dir" if path.is_dir() else "file",
                        stat.st_mtime_ns,
                        stat.st_size,
                        _sha256(path) if path.is_file() else None,
                    )
                return observed

            before = snapshot()
            previous_tmpdir = os.environ.get("TMPDIR")
            os.environ["TMPDIR"] = str(fixture.root)
            try:
                self.assertEqual(validate_run(fixture.root)["live_validation_state"], "PASS")
            finally:
                if previous_tmpdir is None:
                    os.environ.pop("TMPDIR", None)
                else:
                    os.environ["TMPDIR"] = previous_tmpdir
            self.assertEqual(snapshot(), before)

    def test_valid_complete_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            self.assertEqual(validate_run(fixture.root)["artifact_publish_state"], "LOCAL_RESEARCH_CANDIDATE_COMPLETE")

    def test_ohlc_ordering_anomaly_is_disclosed_but_nonblocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            code = fixture.codes_2021[0]
            trade_date = fixture.current_dates[0]
            _mutate_daily_value(
                fixture,
                taxonomy="SW2021",
                code=code,
                trade_date=trade_date,
                field="high",
                value="99",
            )
            fixture.audit["axes"]["SW2021"]["ohlc_ordering"] = _ohlc_audit(
                fixture.daily_2021
            )
            fixture.sync_requests()
            fixture.write_report()
            fixture.write_canonical_html()
            fixture.reclose()
            result = validate_run(fixture.root)
            self.assertEqual(result["artifact_publish_state"], "LOCAL_RESEARCH_CANDIDATE_COMPLETE")
            self.assertEqual(
                fixture.audit["axes"]["SW2021"]["ohlc_ordering"]["anomaly_count"], 1
            )

    def test_resealed_nonpositive_ohl_value_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            _mutate_daily_value(
                fixture,
                taxonomy="SW2021",
                code=fixture.codes_2021[0],
                trade_date=fixture.current_dates[0],
                field="high",
                value="-1",
            )
            fixture.audit["axes"]["SW2021"]["ohlc_ordering"] = _ohlc_audit(
                fixture.daily_2021
            )
            fixture.sync_requests()
            fixture.write_report()
            fixture.write_canonical_html()
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "strictly positive"):
                validate_run(fixture.root)

    def test_ohlc_ordering_anomaly_omission_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            _mutate_daily_value(
                fixture,
                taxonomy="SW2021",
                code=fixture.codes_2021[0],
                trade_date=fixture.current_dates[0],
                field="high",
                value="99",
            )
            fixture.sync_requests()
            fixture.write_canonical_html()
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "SW2021.ohlc_ordering"):
                validate_run(fixture.root)

    def test_ohlc_ordering_forged_anomaly_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            forged = fixture.audit["axes"]["SW2021"]["ohlc_ordering"]
            forged["anomaly_count"] = 1
            forged["anomalies"] = [
                {
                    "ts_code": fixture.codes_2021[0],
                    "trade_date": fixture.current_dates[0],
                    "anomaly_codes": ["HIGH_BELOW_CLOSE"],
                    "open": "100",
                    "low": "100",
                    "high": "99",
                    "close": "100",
                }
            ]
            fixture.sync_requests()
            fixture.write_report()
            fixture.write_canonical_html()
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "SW2021.ohlc_ordering"):
                validate_run(fixture.root)

    def test_complete_report_requires_nonblocking_ohlc_disclosure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            report_path = fixture.root / "reports" / "adversarial_review.md"
            report = report_path.read_text(encoding="utf-8")
            report_path.write_text(
                report.replace("`open/low/high` 不参与本项目公式", "OHLC 未披露", 1),
                encoding="utf-8",
            )
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "non-blocking OHLC disclosure"):
                validate_run(fixture.root)

    def test_exact_canonical_run003_remains_partial_and_blocked(self) -> None:
        run = PROJECT_ROOT / "output" / "runs" / "SWIVD-RUN-20260828-003"
        manifest = validate_run(run)
        self.assertEqual(manifest["schema_version"], "swivd-run-manifest-v3")
        self.assertEqual(manifest["artifact_publish_state"], "LOCAL_RESEARCH_CANDIDATE_PARTIAL")
        self.assertEqual(manifest["axes"], {"SW2021": "PASS", "SW2014": "BLOCKED"})

    def test_exact_pre_ohlc_canonical_runs_remain_partial_and_blocked(self) -> None:
        for suffix, schema in (("001", "swivd-run-manifest-v1"), ("002", "swivd-run-manifest-v2")):
            run = PROJECT_ROOT / "output" / "runs" / f"SWIVD-RUN-20260828-{suffix}"
            manifest = validate_run(run)
            self.assertEqual(manifest["schema_version"], schema)
            self.assertEqual(
                manifest["artifact_publish_state"],
                "LOCAL_RESEARCH_CANDIDATE_PARTIAL",
            )
            self.assertEqual(manifest["axes"], {"SW2021": "PASS", "SW2014": "BLOCKED"})

    def test_pre_ohlc_run_copies_do_not_inherit_grandfather(self) -> None:
        for suffix in ("001", "002"):
            source = PROJECT_ROOT / "output" / "runs" / f"SWIVD-RUN-20260828-{suffix}"
            with tempfile.TemporaryDirectory() as temporary:
                copied = Path(temporary) / source.name
                shutil.copytree(source, copied)
                with self.assertRaises(ValidationError):
                    validate_run(copied)

    def test_run003_copy_and_resealed_tamper_do_not_inherit_grandfather(self) -> None:
        source = PROJECT_ROOT / "output" / "runs" / "SWIVD-RUN-20260828-003"
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / source.name
            shutil.copytree(source, copied)
            with self.assertRaises(ValidationError):
                validate_run(copied)

            report_path = copied / "reports" / "adversarial_review.md"
            report_path.write_text(
                report_path.read_text(encoding="utf-8") + "\n篡改后重封。\n",
                encoding="utf-8",
            )
            manifest_path = copied / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source_files"] = _source_file_records()
            for artifact in manifest["artifacts"]:
                if artifact["path"] == "reports/adversarial_review.md":
                    artifact["bytes"] = report_path.stat().st_size
                    artifact["sha256"] = _sha256(report_path)
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            sums_path = copied / "SHA256SUMS"
            sums_path.write_text(
                "\n".join(
                    f"{_sha256(path)}  {path.relative_to(copied).as_posix()}"
                    for path in sorted(copied.rglob("*"))
                    if path.is_file() and path != sums_path
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValidationError):
                validate_run(copied)

    def test_html_summary_label_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            fixture.payload["current_rows"][0]["pe_label"] = "伪造标签"
            fixture.write_payload()
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "payload/CSV value mismatch"):
                validate_run(fixture.root)

    def test_html_production_status_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            fixture.payload["metadata"]["statuses"]["production_approved"] = True
            fixture.write_payload()
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "dashboard payload.metadata"):
                validate_run(fixture.root)

    def test_html_formula_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            fixture.payload["metadata"]["formula"] = "rank / N"
            fixture.write_payload()
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "dashboard payload.metadata"):
                validate_run(fixture.root)

    def test_html_embedded_audit_state_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            fixture.payload["audit"] = json.loads(json.dumps(fixture.audit))
            fixture.payload["audit"]["axes"]["SW2014"]["state"] = "BLOCKED"
            fixture.write_payload()
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "dashboard payload.audit"):
                validate_run(fixture.root)

    def test_manifest_source_substitution_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            fixture.manifest["source"]["provider"] = "Wind"
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "manifest.source"):
                validate_run(fixture.root)

    def test_sw2021_audit_constant_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            fixture.audit["axes"]["SW2021"]["continuity_policy"] = "PER_CODE"
            (fixture.root / "audit.json").write_text(
                json.dumps(fixture.audit, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            fixture.write_payload()
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "SW2021.continuity_policy"):
                validate_run(fixture.root)

    def test_audit_trade_calendar_legacy_end_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            fixture.audit["trade_calendar"]["legacy_end"] = "20211209"
            (fixture.root / "audit.json").write_text(
                json.dumps(fixture.audit, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            fixture.write_payload()
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "audit.trade_calendar"):
                validate_run(fixture.root)

    def test_report_axis_state_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "reports" / "adversarial_review.md"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "- SW2014 轴：`PASS`", "- SW2014 轴：`BLOCKED`"
                ),
                encoding="utf-8",
            )
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "SW2014 state contradicts"):
                validate_run(fixture.root)

    def test_offline_validation_cannot_claim_different_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            result = dict(fixture.manifest)
            result["production_approved"] = True
            _write_json(
                fixture.root / "offline_validation.json",
                {
                    "schema_version": "swivd-offline-validation-v1",
                    "validated_at": "2026-08-30T22:42:00+08:00",
                    "result": result,
                },
            )
            fixture.reclose()
            with self.assertRaisesRegex(
                ValidationError, "offline_validation.result.production_approved"
            ):
                validate_run(fixture.root)

    def test_history_numeric_tamper_is_rejected_against_normalized_daily(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "tables" / "sw2021_history.csv"
            header, rows = _read_csv(path)
            rows[0]["close"] = "101"
            _write_csv(path, header, rows)
            fixture.reclose()
            with self.assertRaisesRegex(
                ValidationError, "history close differs from normalized sw_daily"
            ):
                validate_run(fixture.root)

    def test_wrong_empirical_percentile_is_rejected_by_independent_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "tables" / "sw2021_current.csv"
            header, rows = _read_csv(path)
            rows[0]["pe_percentile"] = "99"
            _write_csv(path, header, rows)
            fixture.reclose()
            with self.assertRaisesRegex(
                ValidationError, "independently derived numeric mismatch.*pe_percentile"
            ):
                validate_run(fixture.root)

    def test_wrong_anchor_return_is_rejected_by_independent_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "tables" / "sw2021_current.csv"
            header, rows = _read_csv(path)
            rows[0]["return_5d"] = "0.1"
            _write_csv(path, header, rows)
            fixture.reclose()
            with self.assertRaisesRegex(
                ValidationError, "independently derived numeric mismatch.*return_5d"
            ):
                validate_run(fixture.root)

    def test_raw_normalized_numeric_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = (
                fixture.root
                / "inputs"
                / "raw"
                / "sw_daily"
                / "SW2021"
                / f"{fixture.codes_2021[0].replace('.', '_')}.json"
            )
            response = json.loads(path.read_text(encoding="utf-8"))
            close_position = response["data"]["fields"].index("close")
            response["data"]["items"][0][close_position] = "101"
            _write_json(path, response)
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "raw/normalized field mismatch"):
                validate_run(fixture.root)

    def test_audit_request_hash_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            fixture.audit["requests"][0]["raw_sha256"] = "0" * 64
            fixture.payload["audit"] = fixture.audit
            (fixture.root / "audit.json").write_text(
                json.dumps(fixture.audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            fixture.write_payload()
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "audit request raw_sha256 mismatch"):
                validate_run(fixture.root)

    def test_source_file_closure_omission_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            fixture.manifest["source_files"] = fixture.manifest["source_files"][:-1]
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "canonical source closure"):
                validate_run(fixture.root)

    def test_runtime_schema_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            fixture.manifest["runtime"].pop("platform")
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "manifest.runtime schema drift"):
                validate_run(fixture.root)

    def test_sw2021_normalized_extra_source_name_column_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "inputs" / "normalized" / "sw_daily_sw2021.csv"
            header, rows = _read_csv(path)
            for row in rows:
                row["source_name"] = row["name"]
            _write_csv(path, [*header, "source_name"], rows)
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "CSV schema drift"):
                validate_run(fixture.root)

    def test_sw2014_blocked_schema_drift_raw_is_preserved_as_failure_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary), sw2014_state="BLOCKED")
            raw_path = (
                fixture.root / "inputs" / "raw" / "index_classify" / "SW2014_L1.json"
            )
            _write_json(
                raw_path,
                {"code": 0, "msg": "", "data": {"fields": ["unexpected"], "items": []}},
            )
            axis = fixture.audit["axes"]["SW2014"]
            axis["reason_codes"] = ["SCHEMA_MISMATCH"]
            axis["error"] = {
                "type": "DataValidationError",
                "reason_code": "SCHEMA_MISMATCH",
                "message": "synthetic schema drift",
            }
            fixture.sync_requests()
            fixture.write_canonical_html()
            fixture.reclose()
            self.assertEqual(validate_run(fixture.root)["live_validation_state"], "PARTIAL")

    def test_sw2014_blocked_classification_cannot_have_later_daily_raw(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary), sw2014_state="BLOCKED")
            classification_path = (
                fixture.root / "inputs" / "raw" / "index_classify" / "SW2014_L1.json"
            )
            _write_json(
                classification_path,
                {"code": 0, "msg": "", "data": {"fields": ["unexpected"], "items": []}},
            )
            _write_json(
                fixture.root
                / "inputs"
                / "raw"
                / "sw_daily"
                / "SW2014"
                / "999999_SI.json",
                {"code": 1, "msg": "synthetic", "data": None},
            )
            axis = fixture.audit["axes"]["SW2014"]
            axis["reason_codes"] = ["SCHEMA_MISMATCH"]
            axis["error"] = {
                "type": "DataValidationError",
                "reason_code": "SCHEMA_MISMATCH",
                "message": "synthetic schema drift",
            }
            fixture.sync_requests()
            fixture.write_payload()
            fixture.reclose()
            with self.assertRaisesRegex(
                ValidationError, "daily responses exist after blocked classification"
            ):
                validate_run(fixture.root)

    def test_sw2014_pass_cannot_hide_raw_schema_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            raw_path = (
                fixture.root / "inputs" / "raw" / "index_classify" / "SW2014_L1.json"
            )
            _write_json(
                raw_path,
                {"code": 0, "msg": "", "data": {"fields": ["unexpected"], "items": []}},
            )
            fixture.sync_requests()
            fixture.write_payload()
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "fields differ from the frozen"):
                validate_run(fixture.root)

    def test_sw2014_structural_blanks_before_each_code_inception_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            first_dates: dict[str, str] = {}
            for row in fixture.history_2014:
                first_dates[row["index_code"]] = min(
                    first_dates.get(row["index_code"], row["trade_date"]), row["trade_date"]
                )
            self.assertNotEqual(first_dates[fixture.codes_2014[0]], first_dates[fixture.codes_2014[1]])
            self.assertEqual(validate_run(fixture.root)["live_validation_state"], "PASS")

    def test_sw2014_internal_open_date_gap_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "inputs" / "normalized" / "sw_daily_sw2014.csv"
            header, rows = _read_csv(path)
            rows = [
                row
                for row in rows
                if not (
                    row["ts_code"] == fixture.codes_2014[0]
                    and row["trade_date"] == fixture.legacy_dates[1]
                )
            ]
            _write_csv(path, header, rows)
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "internal open-date gap"):
                validate_run(fixture.root)

    def test_sw2014_common_end_missing_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            daily_path = fixture.root / "inputs" / "normalized" / "sw_daily_sw2014.csv"
            daily_header, daily_rows = _read_csv(daily_path)
            daily_rows = [
                row
                for row in daily_rows
                if not (
                    row["ts_code"] == fixture.codes_2014[0]
                    and row["trade_date"] == fixture.legacy_dates[-1]
                )
            ]
            _write_csv(daily_path, daily_header, daily_rows)
            path = fixture.root / "tables" / "sw2014_history.csv"
            header, rows = _read_csv(path)
            rows = [
                row
                for row in rows
                if not (
                    row["index_code"] == fixture.codes_2014[0]
                    and row["trade_date"] == fixture.legacy_dates[-1]
                )
            ]
            _write_csv(path, header, rows)
            fixture.payload["sw2014_history"] = [
                row
                for row in fixture.payload["sw2014_history"]
                if not (
                    row["ts_code"] == fixture.codes_2014[0]
                    and row["trade_date"] == fixture.legacy_dates[-1]
                )
            ]
            fixture.write_payload()
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "lacks the common end date"):
                validate_run(fixture.root)

    def test_sw2014_empty_source_name_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "inputs" / "normalized" / "sw_daily_sw2014.csv"
            header, rows = _read_csv(path)
            rows[0]["name"] = ""
            _write_csv(path, header, rows)
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "empty source name"):
                validate_run(fixture.root)

    def test_sw2014_terminal_source_name_conflict_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "inputs" / "normalized" / "sw_daily_sw2014.csv"
            header, rows = _read_csv(path)
            terminal = next(
                row
                for row in rows
                if row["ts_code"] == fixture.codes_2014[0]
                and row["trade_date"] == fixture.legacy_dates[-1]
            )
            terminal["name"] = "错误末日名称"
            _write_csv(path, header, rows)
            fixture.reclose()
            with self.assertRaisesRegex(
                ValidationError, "common-end source name conflicts with classification"
            ):
                validate_run(fixture.root)

    def test_sw2014_silent_historical_name_overwrite_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "tables" / "sw2014_history.csv"
            header, rows = _read_csv(path)
            target = next(
                row
                for row in rows
                if row["index_code"] == fixture.codes_2014[0]
                and row["trade_date"] == fixture.legacy_dates[0]
            )
            self.assertEqual(target["source_name"], "旧历史行业1")
            target["source_name"] = "历史行业1"
            _write_csv(path, header, rows)
            for row in fixture.payload["sw2014_history"]:
                if (
                    row["ts_code"] == fixture.codes_2014[0]
                    and row["trade_date"] == fixture.legacy_dates[0]
                ):
                    row["source_name"] = "历史行业1"
            fixture.write_payload()
            fixture.reclose()
            with self.assertRaisesRegex(
                ValidationError, "source_name differs from normalized sw_daily.name"
            ):
                validate_run(fixture.root)

    def test_sw2014_cross_code_source_name_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "tables" / "sw2014_history.csv"
            header, rows = _read_csv(path)
            target = next(
                row
                for row in rows
                if row["index_code"] == fixture.codes_2014[0]
                and row["trade_date"] == fixture.legacy_dates[0]
            )
            target["source_name"] = "历史行业2"
            _write_csv(path, header, rows)
            for row in fixture.payload["sw2014_history"]:
                if (
                    row["ts_code"] == fixture.codes_2014[0]
                    and row["trade_date"] == fixture.legacy_dates[0]
                ):
                    row["source_name"] = "历史行业2"
            fixture.write_payload()
            fixture.reclose()
            with self.assertRaisesRegex(
                ValidationError, "source_name differs from normalized sw_daily.name"
            ):
                validate_run(fixture.root)

    def test_sw2014_omitted_name_history_disclosure_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            fixture.audit["axes"]["SW2014"].pop("name_history")
            fixture.payload["audit"] = fixture.audit
            fixture.payload["sw2014_policy"].pop("name_history")
            (fixture.root / "audit.json").write_text(
                json.dumps(fixture.audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            fixture.write_payload()
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "audit.axes.SW2014.name_history"):
                validate_run(fixture.root)

    def test_sw2014_html_source_name_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            fixture.payload["sw2014_history"][0]["source_name"] = "页面静默改名"
            fixture.write_payload()
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "payload/CSV value mismatch"):
                validate_run(fixture.root)

    def test_sw2014_html_name_history_mirror_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            fixture.payload["sw2014_policy"]["name_history"] = json.loads(
                json.dumps(fixture.payload["sw2014_policy"]["name_history"], ensure_ascii=False)
            )
            fixture.payload["sw2014_policy"]["name_history"]["segment_count"] += 1
            fixture.write_payload()
            fixture.reclose()
            with self.assertRaisesRegex(
                ValidationError, r"dashboard payload\.sw2014_policy"
            ):
                validate_run(fixture.root)

    def test_sw2014_cross_axis_daily_row_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "inputs" / "normalized" / "sw_daily_sw2014.csv"
            header, rows = _read_csv(path)
            rows[0]["ts_code"] = fixture.codes_2021[0]
            _write_csv(path, header, rows)
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "cross-version"):
                validate_run(fixture.root)

    def test_sw2014_classification_cannot_forge_is_pub_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "inputs" / "normalized" / "classification_sw2014.csv"
            header, rows = _read_csv(path)
            rows[0]["is_pub"] = "1"
            _write_csv(path, header, rows)
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "source null"):
                validate_run(fixture.root)

    def test_sw2014_history_cannot_forge_is_pub_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "tables" / "sw2014_history.csv"
            header, rows = _read_csv(path)
            rows[0]["is_pub"] = "1"
            _write_csv(path, header, rows)
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "publication semantics"):
                validate_run(fixture.root)

    def test_sw2014_html_payload_cannot_forge_is_pub_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            fixture.payload["sw2014_history"][0]["is_pub"] = "1"
            fixture.write_payload()
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "payload/CSV value mismatch"):
                validate_run(fixture.root)

    def test_sw2014_summary_payload_cannot_forge_is_pub_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            fixture.payload["sw2014_summary"][0]["is_pub"] = "1"
            fixture.write_payload()
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "explicit JSON null"):
                validate_run(fixture.root)

    def test_sw2014_audit_requires_authorized_semantic_carriers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "audit.json"
            audit = json.loads(path.read_text(encoding="utf-8"))
            audit["axes"]["SW2014"]["selection_basis"] = "PUBLISHED_ONLY"
            path.write_text(json.dumps(audit, ensure_ascii=False) + "\n", encoding="utf-8")
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "selection_basis"):
                validate_run(fixture.root)

    def test_unpublished_classification_is_summary_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            unpublished_code = fixture.codes_2021[-1]

            classification_path = (
                fixture.root / "inputs" / "normalized" / "classification_sw2021.csv"
            )
            header, classifications = _read_csv(classification_path)
            classifications[-1]["is_pub"] = "0"
            classifications[-1]["publication_state"] = "NOT_PUBLISHED"
            _write_csv(classification_path, header, classifications)

            raw_classification_path = (
                fixture.root / "inputs" / "raw" / "index_classify" / "SW2021_L1.json"
            )
            raw_classification = json.loads(
                raw_classification_path.read_text(encoding="utf-8")
            )
            raw_fields = raw_classification["data"]["fields"]
            code_position = raw_fields.index("index_code")
            flag_position = raw_fields.index("is_pub")
            for item in raw_classification["data"]["items"]:
                if item[code_position] == unpublished_code:
                    item[flag_position] = "0"
            _write_json(raw_classification_path, raw_classification)
            (
                fixture.root
                / "inputs"
                / "raw"
                / "sw_daily"
                / "SW2021"
                / f"{unpublished_code.replace('.', '_')}.json"
            ).unlink()

            daily_path = fixture.root / "inputs" / "normalized" / "sw_daily_sw2021.csv"
            header, daily_rows = _read_csv(daily_path)
            filtered_daily_rows = [
                row for row in daily_rows if row["ts_code"] != unpublished_code
            ]
            _write_csv(
                daily_path,
                header,
                filtered_daily_rows,
            )

            history_path = fixture.root / "tables" / "sw2021_history.csv"
            header, fixture.history_2021 = _read_csv(history_path)
            fixture.history_2021 = [
                row for row in fixture.history_2021 if row["index_code"] != unpublished_code
            ]
            _write_csv(history_path, header, fixture.history_2021)

            current_path = fixture.root / "tables" / "sw2021_current.csv"
            header, fixture.current = _read_csv(current_path)
            unpublished_row = next(
                row for row in fixture.current if row["index_code"] == unpublished_code
            )
            for field in SUMMARY_FIELDS:
                if field not in {
                    "taxonomy",
                    "index_code",
                    "industry_name",
                    "as_of",
                    "valuation_state",
                    "return_state",
                }:
                    unpublished_row[field] = ""
            unpublished_row["valuation_state"] = "NA_NOT_PUBLISHED"
            unpublished_row["return_state"] = "NA_NOT_PUBLISHED"
            _write_csv(current_path, header, fixture.current)

            current_axis = fixture.audit["axes"]["SW2021"]
            current_axis["selected_count"] = 30
            current_axis["published_count"] = 30
            current_axis["unpublished_count"] = 1
            current_axis["ohlc_ordering"] = _ohlc_audit(filtered_daily_rows)
            fixture.sync_requests()
            audit = json.loads((fixture.root / "audit.json").read_text(encoding="utf-8"))
            html = render_dashboard(
                fixture.current,
                fixture.history_2021,
                metadata=fixture.metadata,
                audit=audit,
                sw2014_summary=fixture.archive,
                sw2014_history=fixture.history_2014,
            )
            (fixture.root / "dashboard.html").write_text(html, encoding="utf-8")
            fixture.reclose()
            self.assertEqual(validate_run(fixture.root)["live_validation_state"], "PASS")

    def test_unpublished_classification_cannot_have_history_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            classification_path = (
                fixture.root / "inputs" / "normalized" / "classification_sw2021.csv"
            )
            header, classifications = _read_csv(classification_path)
            classifications[-1]["is_pub"] = "0"
            classifications[-1]["publication_state"] = "NOT_PUBLISHED"
            _write_csv(classification_path, header, classifications)
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "unpublished"):
                validate_run(fixture.root)

    def test_sw2021_blank_publication_flag_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "inputs" / "normalized" / "classification_sw2021.csv"
            header, rows = _read_csv(path)
            rows[0]["is_pub"] = ""
            _write_csv(path, header, rows)
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "must be 0 or 1"):
                validate_run(fixture.root)

    def test_valid_partial_run_requires_block_reason_and_header_only_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary), sw2014_state="BLOCKED")
            self.assertEqual(validate_run(fixture.root)["live_validation_state"], "PARTIAL")

    def test_block_reason_must_be_scoped_to_sw2014_axis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary), sw2014_state="BLOCKED")
            path = fixture.root / "audit.json"
            audit = json.loads(path.read_text(encoding="utf-8"))
            audit["axes"]["SW2014"]["reason_codes"] = []
            audit["unrelated_reason_codes"] = ["SW2014_PROVIDER_DATA_BLOCKED"]
            path.write_text(
                json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            fixture.reclose()
            with self.assertRaisesRegex(
                ValidationError,
                "(?:scoped audit reason_codes|scoped transport failure reason)",
            ):
                validate_run(fixture.root)

    def test_cross_version_splice_is_rejected_even_with_fresh_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "tables" / "sw2021_current.csv"
            header, rows = _read_csv(path)
            rows[0]["index_code"] = fixture.codes_2014[0]
            _write_csv(path, header, rows)
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "unclassified summary code"):
                validate_run(fixture.root)

    def test_duplicate_primary_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "tables" / "sw2021_history.csv"
            header, rows = _read_csv(path)
            rows.append(dict(rows[0]))
            _write_csv(path, header, rows)
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "duplicate primary key"):
                validate_run(fixture.root)

    def test_external_link_is_rejected_even_with_fresh_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "dashboard.html"
            path.write_text(path.read_text(encoding="utf-8") + '<script src="https://cdn.example/x.js"></script>', encoding="utf-8")
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "forbidden remote URL"):
                validate_run(fixture.root)

    def test_hash_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "reports" / "adversarial_review.md"
            path.write_text(path.read_text(encoding="utf-8") + "drift\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "artifact (?:byte count|hash) drift"):
                validate_run(fixture.root)

    def test_token_leak_is_rejected_even_with_fresh_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "audit.json"
            audit = json.loads(path.read_text(encoding="utf-8"))
            audit["debug"] = "TUSHARE_" + "TOKEN=" + "abcdefghijklmnopqrstuvwxyz123456"
            path.write_text(json.dumps(audit, ensure_ascii=False) + "\n", encoding="utf-8")
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "suspected persisted token"):
                validate_run(fixture.root)

    def test_required_research_states_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            fixture.manifest["production_approved"] = True
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "production_approved"):
                validate_run(fixture.root)

    def test_blocked_archive_cannot_claim_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary), sw2014_state="BLOCKED")
            fixture.manifest["artifact_publish_state"] = "LOCAL_RESEARCH_CANDIDATE_COMPLETE"
            fixture.manifest["live_validation_state"] = "PASS"
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "artifact_publish_state"):
                validate_run(fixture.root)

    def test_embedded_payload_csv_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "tables" / "sw2021_current.csv"
            header, rows = _read_csv(path)
            rows[0]["pe"] = "21"
            _write_csv(path, header, rows)
            fixture.reclose()
            with self.assertRaisesRegex(
                ValidationError,
                "(?:payload/CSV value mismatch|independently derived numeric mismatch)",
            ):
                validate_run(fixture.root)

    def test_pickle_artifact_is_rejected_even_if_manifested(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            (fixture.root / "inputs" / "raw" / "cache.pkl").write_bytes(b"\x80\x04evil")
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "Pickle"):
                validate_run(fixture.root)

    def test_forbidden_valuation_wording_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RunFixture(Path(temporary))
            path = fixture.root / "dashboard.html"
            path.write_text(path.read_text(encoding="utf-8") + "<p>低估</p>", encoding="utf-8")
            fixture.reclose()
            with self.assertRaisesRegex(ValidationError, "forbidden valuation wording"):
                validate_run(fixture.root)

    def test_spec_byte_or_semantic_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "PROJECT_SPEC.json"
            spec = json.loads((PROJECT_ROOT / "PROJECT_SPEC.json").read_text(encoding="utf-8"))
            spec["network"]["allow_redirects"] = True
            path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "network.allow_redirects"):
                validate_spec(path)


if __name__ == "__main__":
    unittest.main()
