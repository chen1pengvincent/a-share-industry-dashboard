"""Shared value contracts; no I/O, provider, or application dependencies."""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


class DataError(ValueError):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


def parse_date(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
        raise DataError("INVALID_DATE")
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        raise DataError("INVALID_DATE") from None


def decimal_value(value: Any, *, positive: bool = False) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (ValueError, InvalidOperation):
        return None
    if not result.is_finite() or (positive and result <= 0):
        return None
    return result


def decimal_text(value: Decimal | int) -> str:
    result = format(value, "f") if isinstance(value, Decimal) else str(value)
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return "0" if result in ("-0", "") else result


def money_cent(value: Any) -> int:
    parsed = decimal_value(value)
    if parsed is None or parsed * 100 != (parsed * 100).to_integral_value():
        raise DataError("INVALID_MONEY_PRECISION")
    return int(parsed * 100)


def metric(value: Any, day: str, *, reason: str | None = None,
           status: str | None = None, **counts: Any) -> dict:
    return {"value": None if value is None else decimal_text(value),
            "status": status or ("OK" if value is not None else "NA"),
            "reason_codes": [reason] if reason else [], "metric_date": day, **counts}


def json_bytes(value: Any) -> bytes:
    def convert(item: Any):
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise DataError("NONFINITE_JSON")
            return decimal_text(item)
        raise TypeError(type(item).__name__)
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                       allow_nan=False, default=convert) + "\n").encode("utf-8")


TAXONOMIES = [
    {"id": "SW", "name": "申万", "levels": [{"id": x, "name": n} for x, n in
     [("L1", "一级行业"), ("L2", "二级行业"), ("L3", "三级行业")]]},
    {"id": "THS", "name": "同花顺", "levels": [{"id": "INDUSTRY", "name": "行业板块"}]},
    {"id": "TDX", "name": "通达信", "levels": [{"id": "880", "name": "880 系列"},
                                                     {"id": "881", "name": "881 系列"}]},
    {"id": "CI", "name": "中信", "levels": [{"id": x, "name": n} for x, n in
     [("L1", "一级行业"), ("L2", "二级行业"), ("L3", "三级行业")]]},
]
