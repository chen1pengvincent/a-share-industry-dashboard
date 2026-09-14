"""Validation and Decimal-safe calculations for the SW valuation dashboard.

All public functions operate on ``list[dict]`` values.  They do not perform I/O,
silently sort away duplicates, fill missing observations, or combine SW2014 and
SW2021.  Validation errors are fail-closed and carry a stable machine code.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from .tushare_client import ENDPOINT_FIELDS


PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "trade_cal": ("exchange", "cal_date"),
    "index_classify": ("src", "index_code"),
    "sw_daily": ("ts_code", "trade_date"),
}
ROW_LIMITS: dict[str, int] = {
    "trade_cal": 6000,
    "index_classify": 5000,
    "sw_daily": 4000,
}
AXIS_CLASSIFICATION_COUNTS = {"SW2021": 31, "SW2014": 28}
VALID_SOURCES = frozenset(AXIS_CLASSIFICATION_COUNTS)
DATE_PATTERN = re.compile(r"^[0-9]{8}$")
INDEX_CODE_PATTERN = re.compile(r"^[0-9]{6}\.[A-Z]{2}$")
STATUS_OK = "OK"
STATUS_CURRENT_MISSING = "CURRENT_MISSING"
STATUS_CURRENT_INVALID = "CURRENT_INVALID"
STATUS_HISTORY_INSUFFICIENT = "HISTORY_INSUFFICIENT"
RETIRED_PUBLICATION_STATE = "NOT_PROVIDED_FOR_RETIRED_TAXONOMY"
RETIRED_SELECTION_BASIS = "ALL_CLASSIFIED_L1_ROWS_FOR_RETIRED_TAXONOMY"
PER_CODE_CONTINUITY_POLICY = "PER_CODE_OBSERVED_INCEPTION_TO_COMMON_END"
NAME_HISTORY_POLICY = "STABLE_TS_CODE_WITH_SOURCE_NAME_HISTORY"
OHLC_ORDERING_POLICY = "DISCLOSE_NON_BLOCKING_OHLC_ORDERING"


class DataValidationError(ValueError):
    """Input data contradicts the frozen project contract."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.details = dict(details or {})


def parse_yyyymmdd(value: Any, *, field: str = "date") -> date:
    """Parse an exact ``YYYYMMDD`` string without permissive coercion."""

    if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value):
        raise DataValidationError("INVALID_DATE", f"{field} must be YYYYMMDD")
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise DataValidationError("INVALID_DATE", f"{field} is not a calendar date") from exc


def parse_decimal(
    value: Any,
    *,
    field: str = "value",
    allow_missing: bool = False,
    require_finite: bool = True,
) -> Decimal | None:
    """Convert a JSON scalar to Decimal without using a binary-float operation."""

    if value is None or (isinstance(value, str) and not value.strip()):
        if allow_missing:
            return None
        raise DataValidationError("MISSING_NUMERIC", f"{field} is missing")
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise DataValidationError("INVALID_NUMERIC", f"{field} is not a numeric scalar")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise DataValidationError("INVALID_NUMERIC", f"{field} is not decimal data") from exc
    if require_finite and not parsed.is_finite():
        raise DataValidationError("NON_FINITE_NUMERIC", f"{field} is not finite")
    return parsed


def _finite_positive_or_none(value: Any, *, field: str) -> Decimal | None:
    parsed = parse_decimal(
        value,
        field=field,
        allow_missing=True,
        require_finite=False,
    )
    if parsed is None or not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _finite_or_none(value: Any, *, field: str) -> Decimal | None:
    parsed = parse_decimal(
        value,
        field=field,
        allow_missing=True,
        require_finite=False,
    )
    return parsed if parsed is not None and parsed.is_finite() else None


def _validate_rows(
    api_name: str,
    rows: list[dict[str, Any]],
    *,
    row_limit: int | None,
) -> list[dict[str, Any]]:
    if api_name not in ENDPOINT_FIELDS:
        raise DataValidationError("UNKNOWN_ENDPOINT", f"unsupported endpoint {api_name!r}")
    if not isinstance(rows, list):
        raise DataValidationError("INVALID_CONTAINER", f"{api_name} rows must be a list")
    if row_limit is not None:
        if row_limit <= 0:
            raise DataValidationError("INVALID_ROW_LIMIT", "row_limit must be positive")
        if len(rows) >= row_limit:
            raise DataValidationError(
                "ROW_LIMIT_TOUCHED",
                f"{api_name} response touched its row limit",
                details={"row_count": len(rows), "row_limit": row_limit},
            )
    expected_fields = set(ENDPOINT_FIELDS[api_name])
    primary_key = PRIMARY_KEYS[api_name]
    seen: set[tuple[Any, ...]] = set()
    copied: list[dict[str, Any]] = []
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            raise DataValidationError(
                "INVALID_ROW", f"{api_name} row {position} must be a dict"
            )
        actual_fields = set(row)
        if actual_fields != expected_fields:
            raise DataValidationError(
                "SCHEMA_MISMATCH",
                f"{api_name} row {position} differs from frozen schema",
                details={
                    "missing": sorted(expected_fields - actual_fields),
                    "unexpected": sorted(actual_fields - expected_fields),
                },
            )
        key = tuple(row[field] for field in primary_key)
        if any(value is None or str(value).strip() == "" for value in key):
            raise DataValidationError(
                "EMPTY_PRIMARY_KEY", f"{api_name} row {position} has an empty primary key"
            )
        if key in seen:
            raise DataValidationError(
                "DUPLICATE_PRIMARY_KEY",
                f"{api_name} contains a duplicate primary key",
                details={"primary_key": list(primary_key), "value": list(key)},
            )
        seen.add(key)
        copied.append(dict(row))
    return copied


def _normalize_binary_flag(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise DataValidationError("INVALID_FLAG", f"{field} must be 0 or 1, not bool")
    if value in (0, "0"):
        return 0
    if value in (1, "1"):
        return 1
    raise DataValidationError("INVALID_FLAG", f"{field} must be 0 or 1")


def validate_trade_cal(
    rows: list[dict[str, Any]],
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    exchange: str | None = None,
    row_limit: int | None = ROW_LIMITS["trade_cal"],
) -> list[dict[str, Any]]:
    """Validate one ``trade_cal`` response and return independent row copies."""

    validated = _validate_rows("trade_cal", rows, row_limit=row_limit)
    lower = parse_yyyymmdd(start_date, field="start_date") if start_date else None
    upper = parse_yyyymmdd(end_date, field="end_date") if end_date else None
    if lower and upper and lower > upper:
        raise DataValidationError("INVALID_DATE_RANGE", "start_date is after end_date")

    by_exchange: dict[str, list[tuple[date, int, str]]] = defaultdict(list)
    for position, row in enumerate(validated):
        row_exchange = row["exchange"]
        if not isinstance(row_exchange, str) or not row_exchange.strip():
            raise DataValidationError("INVALID_EXCHANGE", f"trade_cal row {position} has no exchange")
        if exchange is not None and row_exchange != exchange:
            raise DataValidationError(
                "EXCHANGE_MISMATCH",
                f"trade_cal row {position} is outside exchange {exchange!r}",
            )
        calendar_date = parse_yyyymmdd(row["cal_date"], field="cal_date")
        if lower and calendar_date < lower or upper and calendar_date > upper:
            raise DataValidationError("DATE_OUT_OF_RANGE", "trade_cal date is outside query bounds")
        is_open = _normalize_binary_flag(row["is_open"], field="is_open")
        previous = row["pretrade_date"]
        if previous is not None and str(previous).strip():
            previous_date = parse_yyyymmdd(str(previous), field="pretrade_date")
            if previous_date >= calendar_date:
                raise DataValidationError(
                    "INVALID_PRETRADE_DATE", "pretrade_date must be before cal_date"
                )
        by_exchange[row_exchange].append((calendar_date, is_open, str(previous or "")))

    for row_group in by_exchange.values():
        last_open: date | None = None
        for calendar_date, is_open, previous_raw in sorted(row_group):
            if last_open is not None and previous_raw:
                previous_date = parse_yyyymmdd(previous_raw, field="pretrade_date")
                if previous_date != last_open:
                    raise DataValidationError(
                        "PRETRADE_INCONSISTENT",
                        "pretrade_date disagrees with the observed open-day sequence",
                    )
            if is_open:
                last_open = calendar_date
    return validated


def validate_index_classify(
    rows: list[dict[str, Any]],
    *,
    src: str,
    level: str = "L1",
    expected_count: int | None = None,
    row_limit: int | None = ROW_LIMITS["index_classify"],
) -> list[dict[str, Any]]:
    """Validate one version-specific SW classification response."""

    if src not in VALID_SOURCES:
        raise DataValidationError("INVALID_SOURCE", "src must be SW2021 or SW2014")
    if level != "L1":
        raise DataValidationError("INVALID_LEVEL", "this project permits L1 only")
    validated = _validate_rows("index_classify", rows, row_limit=row_limit)
    expected = AXIS_CLASSIFICATION_COUNTS[src] if expected_count is None else expected_count
    if len(validated) != expected:
        raise DataValidationError(
            "CLASSIFICATION_COUNT_MISMATCH",
            f"{src} {level} count differs from contract",
            details={"actual": len(validated), "expected": expected},
        )

    names: set[str] = set()
    industry_codes: set[str] = set()
    for position, row in enumerate(validated):
        if row["src"] != src or row["level"] != level:
            raise DataValidationError(
                "CLASSIFICATION_AXIS_MISMATCH",
                f"classification row {position} belongs to another axis",
            )
        index_code = row["index_code"]
        if not isinstance(index_code, str) or not INDEX_CODE_PATTERN.fullmatch(index_code):
            raise DataValidationError(
                "INVALID_INDEX_CODE", f"classification row {position} has invalid index_code"
            )
        name = row["industry_name"]
        if not isinstance(name, str) or not name.strip():
            raise DataValidationError(
                "INVALID_INDUSTRY_NAME", f"classification row {position} has no name"
            )
        if name in names:
            raise DataValidationError("DUPLICATE_INDUSTRY_NAME", f"duplicate name {name!r}")
        names.add(name)
        industry_code = row["industry_code"]
        if not isinstance(industry_code, str) or not industry_code.strip():
            raise DataValidationError("INVALID_INDUSTRY_CODE", "industry_code is empty")
        if industry_code in industry_codes:
            raise DataValidationError(
                "DUPLICATE_INDUSTRY_CODE", f"duplicate industry_code {industry_code!r}"
            )
        industry_codes.add(industry_code)
        if src == "SW2021":
            _normalize_binary_flag(row["is_pub"], field="is_pub")
        elif row["is_pub"] is not None:
            raise DataValidationError(
                "RETIRED_PUBLICATION_STATE_INVALID",
                "SW2014 retired classification must preserve JSON null is_pub",
                details={"position": position},
            )
        parent = row["parent_code"]
        if parent is not None and not isinstance(parent, str):
            raise DataValidationError("INVALID_PARENT_CODE", "parent_code must be a string or null")
    return validated


def classification_whitelist(
    rows: list[dict[str, Any]],
    *,
    published_only: bool = True,
    src: str | None = None,
) -> dict[str, str]:
    """Build a code-to-name whitelist without rewriting publication state.

    SW2014 callers must explicitly pass ``published_only=False``.  A retired
    directory does not have a binary publication flag, so silently applying a
    current-directory filter would either erase the axis or tempt a caller to
    forge ``null`` into ``1``.
    """

    if src is not None and src not in VALID_SOURCES:
        raise DataValidationError("INVALID_SOURCE", "src must be SW2021 or SW2014")
    observed_sources = {row.get("src") for row in rows if isinstance(row, Mapping)}
    if None in observed_sources:
        raise DataValidationError("CLASSIFICATION_AXIS_MISMATCH", "classification src is missing")
    if len(observed_sources) != 1:
        raise DataValidationError(
            "CLASSIFICATION_AXIS_MISMATCH", "classification rows mix multiple axes"
        )
    observed_src = next(iter(observed_sources), None)
    if observed_src is not None and observed_src not in VALID_SOURCES:
        raise DataValidationError(
            "INVALID_SOURCE", "classification src must be SW2021 or SW2014"
        )
    if src is not None and observed_src != src:
        raise DataValidationError(
            "CLASSIFICATION_AXIS_MISMATCH", "classification rows belong to another axis"
        )
    for position, row in enumerate(rows):
        value = row.get("is_pub")
        if observed_src == "SW2014":
            # Blank is the reversible CSV representation of the raw JSON null.
            # It is accepted here for offline rebuild but never rewritten.
            if value is not None and not (isinstance(value, str) and not value.strip()):
                raise DataValidationError(
                    "RETIRED_PUBLICATION_STATE_INVALID",
                    "SW2014 retired classification cannot carry a binary publication flag",
                    details={"position": position},
                )
        elif observed_src == "SW2021":
            _normalize_binary_flag(value, field="is_pub")
    if observed_src == "SW2014" and published_only:
        raise DataValidationError(
            "RETIRED_PUBLICATION_FILTER_FORBIDDEN",
            "SW2014 whitelist must explicitly select all classified L1 rows",
        )

    whitelist: dict[str, str] = {}
    for row in rows:
        if published_only and _normalize_binary_flag(row["is_pub"], field="is_pub") != 1:
            continue
        code = str(row["index_code"])
        if code in whitelist:
            raise DataValidationError("DUPLICATE_INDEX_CODE", f"duplicate index_code {code!r}")
        whitelist[code] = str(row["industry_name"])
    if not whitelist:
        raise DataValidationError("EMPTY_WHITELIST", "classification whitelist is empty")
    return whitelist


def validate_per_code_history_continuity(
    rows: list[dict[str, Any]],
    *,
    whitelist: Mapping[str, str] | Iterable[str],
    open_dates: Iterable[str],
    end_date: str,
    minimum_valid_observations: int = 252,
    src: str | None = None,
) -> dict[str, Any]:
    """Validate a retired/history axis from each code's own first observation.

    Structural absence before a code's first official observation is allowed.
    From that first observation through the common ``end_date``, every official
    open day must be present exactly once.  PE and PB independently need the
    frozen minimum number of finite, strictly positive observations.
    """

    if not isinstance(rows, list):
        raise DataValidationError("INVALID_CONTAINER", "history rows must be a list")
    if src is not None and src not in VALID_SOURCES:
        raise DataValidationError("INVALID_SOURCE", "src must be SW2021 or SW2014")
    if minimum_valid_observations <= 0:
        raise DataValidationError(
            "INVALID_MINIMUM_OBSERVATIONS", "minimum_valid_observations must be positive"
        )
    end = parse_yyyymmdd(end_date, field="end_date")
    allowed_codes = set(whitelist) if isinstance(whitelist, Mapping) else set(whitelist)
    if not allowed_codes:
        raise DataValidationError("EMPTY_WHITELIST", "history whitelist is empty")
    invalid_codes = sorted(
        repr(code)
        for code in allowed_codes
        if not isinstance(code, str) or not code.strip()
    )
    if invalid_codes:
        raise DataValidationError(
            "INVALID_INDEX_CODE",
            "history whitelist contains invalid index codes",
            details={"invalid_sample": invalid_codes[:20]},
        )

    calendar_values = list(open_dates)
    if any(not isinstance(value, str) for value in calendar_values):
        raise DataValidationError("INVALID_DATE", "open_dates must contain YYYYMMDD strings")
    parsed_calendar: dict[str, date] = {}
    for value in calendar_values:
        parsed = parse_yyyymmdd(value, field="open_date")
        if parsed <= end:
            parsed_calendar[value] = parsed
    if end_date not in parsed_calendar:
        raise DataValidationError(
            "COMMON_END_NOT_OPEN", "common end_date is not present in the official calendar"
        )
    ordered_open_dates = sorted(parsed_calendar)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_keys: set[tuple[str, str]] = set()
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            raise DataValidationError("INVALID_ROW", f"history row {position} must be a dict")
        if "ts_code" not in row or "trade_date" not in row:
            raise DataValidationError(
                "SCHEMA_MISMATCH", f"history row {position} lacks ts_code/trade_date"
            )
        if "pe" not in row or "pb" not in row:
            raise DataValidationError(
                "SCHEMA_MISMATCH", f"history row {position} lacks pe/pb"
            )
        code = row["ts_code"]
        if not isinstance(code, str) or code not in allowed_codes:
            raise DataValidationError(
                "CODE_OUTSIDE_WHITELIST",
                "history contains a code outside this classification axis",
                details={"position": position, "ts_code": code},
            )
        if src is not None:
            declared_sources = [
                row[field]
                for field in ("taxonomy", "src")
                if field in row and row[field] is not None
            ]
            if any(declared_src != src for declared_src in declared_sources):
                raise DataValidationError(
                    "CROSS_AXIS_ROW",
                    "history row declares another classification axis",
                    details={"position": position, "ts_code": code},
                )
        trade_date = row["trade_date"]
        parsed_trade_date = parse_yyyymmdd(trade_date, field="trade_date")
        if parsed_trade_date > end:
            raise DataValidationError(
                "DATE_OUT_OF_RANGE", "history contains a date after common end_date"
            )
        if trade_date not in parsed_calendar:
            raise DataValidationError(
                "NON_TRADING_DATE",
                "history contains a date outside the official open-day axis",
                details={"position": position, "trade_date": trade_date},
            )
        key = (code, trade_date)
        if key in seen_keys:
            raise DataValidationError(
                "DUPLICATE_PRIMARY_KEY",
                "history contains a duplicate (ts_code, trade_date)",
                details={"ts_code": code, "trade_date": trade_date},
            )
        seen_keys.add(key)
        grouped[code].append(row)

    missing_codes = sorted(allowed_codes - set(grouped))
    if missing_codes:
        raise DataValidationError(
            "MISSING_HISTORY_SERIES",
            "one or more classified codes have no history",
            details={"missing_sample": missing_codes[:20], "missing_count": len(missing_codes)},
        )

    per_code: list[dict[str, Any]] = []
    for code in sorted(allowed_codes):
        series = grouped[code]
        observed_dates = sorted(row["trade_date"] for row in series)
        first_observed = observed_dates[0]
        last_observed = observed_dates[-1]
        if last_observed != end_date:
            raise DataValidationError(
                "COMMON_END_MISSING",
                "industry history does not reach the common end_date",
                details={
                    "ts_code": code,
                    "last_observed_date": last_observed,
                    "common_end_date": end_date,
                },
            )
        expected_dates = [
            value for value in ordered_open_dates if first_observed <= value <= end_date
        ]
        observed_set = set(observed_dates)
        expected_set = set(expected_dates)
        missing_dates = sorted(expected_set - observed_set)
        unexpected_dates = sorted(observed_set - expected_set)
        if missing_dates or unexpected_dates:
            raise DataValidationError(
                "INTERNAL_TRADING_DAY_GAP",
                "industry history is not continuous from its first observation",
                details={
                    "ts_code": code,
                    "first_observed_date": first_observed,
                    "common_end_date": end_date,
                    "missing_count": len(missing_dates),
                    "missing_sample": missing_dates[:20],
                    "unexpected_count": len(unexpected_dates),
                    "unexpected_sample": unexpected_dates[:20],
                },
            )
        positive_counts: dict[str, int] = {}
        for field in ("pe", "pb"):
            positive_counts[field] = sum(
                _finite_positive_or_none(row[field], field=field) is not None
                for row in series
            )
            if positive_counts[field] < minimum_valid_observations:
                raise DataValidationError(
                    "VALUATION_OBSERVATIONS_INSUFFICIENT",
                    f"{field} has fewer than the required positive finite observations",
                    details={
                        "ts_code": code,
                        "field": field,
                        "actual": positive_counts[field],
                        "required": minimum_valid_observations,
                    },
                )
        per_code.append(
            {
                "code": code,
                "first_observed_date": first_observed,
                "last_observed_date": last_observed,
                "expected_rows": len(expected_dates),
                "actual_rows": len(series),
                "pe_positive_count": positive_counts["pe"],
                "pb_positive_count": positive_counts["pb"],
            }
        )

    return {
        "policy": PER_CODE_CONTINUITY_POLICY,
        "code_count": len(allowed_codes),
        "total_actual_rows": len(rows),
        "common_end_date": end_date,
        "minimum_valid_observations": minimum_valid_observations,
        "per_code": per_code,
    }


def validate_sw_daily(
    rows: list[dict[str, Any]],
    *,
    whitelist: Mapping[str, str] | Iterable[str],
    start_date: str | None = None,
    end_date: str | None = None,
    open_dates: Iterable[str] | None = None,
    row_limit: int | None = ROW_LIMITS["sw_daily"],
    strict_name_match: bool = False,
    allow_name_history: bool = False,
) -> list[dict[str, Any]]:
    """Validate one ``sw_daily`` response.

    ``row_limit`` applies to a single API response.  After every response has
    passed this check, callers may validate a concatenated collection by
    explicitly passing ``row_limit=None``.
    """

    if not isinstance(allow_name_history, bool):
        raise DataValidationError(
            "INVALID_NAME_HISTORY_POLICY", "allow_name_history must be a boolean"
        )
    validated = _validate_rows("sw_daily", rows, row_limit=row_limit)
    names_by_code = dict(whitelist) if isinstance(whitelist, Mapping) else None
    allowed_codes = set(names_by_code) if names_by_code is not None else set(whitelist)
    if not allowed_codes:
        raise DataValidationError("EMPTY_WHITELIST", "sw_daily whitelist is empty")
    lower = parse_yyyymmdd(start_date, field="start_date") if start_date else None
    upper = parse_yyyymmdd(end_date, field="end_date") if end_date else None
    if lower and upper and lower > upper:
        raise DataValidationError("INVALID_DATE_RANGE", "start_date is after end_date")
    allowed_open_dates = set(open_dates) if open_dates is not None else None
    if allowed_open_dates is not None:
        for value in allowed_open_dates:
            parse_yyyymmdd(value, field="open_date")

    observed_names: dict[str, str] = {}
    for position, row in enumerate(validated):
        code = row["ts_code"]
        if code not in allowed_codes:
            raise DataValidationError(
                "CODE_OUTSIDE_WHITELIST",
                f"sw_daily row {position} contains an unapproved index code",
                details={"ts_code": code},
            )
        date_text = row["trade_date"]
        trading_date = parse_yyyymmdd(date_text, field="trade_date")
        if lower and trading_date < lower or upper and trading_date > upper:
            raise DataValidationError("DATE_OUT_OF_RANGE", "sw_daily date is outside query bounds")
        if allowed_open_dates is not None and date_text not in allowed_open_dates:
            raise DataValidationError(
                "NON_TRADING_DATE", f"sw_daily contains non-open date {date_text}"
            )
        name = row["name"]
        if not isinstance(name, str) or not name.strip():
            raise DataValidationError("INVALID_INDUSTRY_NAME", "sw_daily name is empty")
        if (
            not allow_name_history
            and code in observed_names
            and observed_names[code] != name
        ):
            raise DataValidationError(
                "NAME_DRIFT", f"sw_daily name changed within index code {code!r}"
            )
        observed_names[code] = name
        if strict_name_match and names_by_code is not None and name != names_by_code[code]:
            raise DataValidationError(
                "NAME_MISMATCH", f"sw_daily name differs from classification for {code!r}"
            )

        # ``close`` is the only price used by the frozen formula and therefore
        # mandatory.  Old archive rows may legitimately omit other display-only
        # fields.  Provider OHLC ordering differences are preserved and disclosed
        # by ``ohlc_ordering_audit``; they do not invalidate close/PE/PB analytics.
        close = parse_decimal(row["close"], field="close")
        if close is None or close <= 0:
            raise DataValidationError("INVALID_PRICE", "close must be strictly positive")
        prices = {
            field: parse_decimal(row[field], field=field, allow_missing=True)
            for field in ("open", "low", "high")
        }
        if any(value is not None and value <= 0 for value in prices.values()):
            raise DataValidationError("INVALID_PRICE", "present OHLC values must be positive")
        for field in ("change", "pct_change", "vol", "amount"):
            parsed = parse_decimal(row[field], field=field, allow_missing=True)
            if field in ("vol", "amount") and parsed is not None and parsed < 0:
                raise DataValidationError("INVALID_NUMERIC", f"{field} must be non-negative")
        for field in ("pe", "pb", "total_mv", "float_mv"):
            parsed = parse_decimal(
                row[field], field=field, allow_missing=True, require_finite=False
            )
            if (
                field in ("total_mv", "float_mv")
                and parsed is not None
                and parsed.is_finite()
                and parsed < 0
            ):
                raise DataValidationError("INVALID_NUMERIC", f"{field} must be non-negative")
    return validated


def ohlc_ordering_audit(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Disclose provider OHLC ordering anomalies without changing source values.

    Only ``close``, ``pe`` and ``pb`` are formula-bearing in this project.  A
    positive, finite ``close`` remains mandatory in ``validate_sw_daily``;
    ordering differences in the unused open/low/high fields are retained as
    traceable provider-quality evidence instead of blocking the axis.
    """

    anomalies: list[dict[str, Any]] = []
    fully_observed = 0
    unchecked_missing = 0
    for row in rows:
        close = parse_decimal(row.get("close"), field="close")
        open_value = parse_decimal(row.get("open"), field="open", allow_missing=True)
        low = parse_decimal(row.get("low"), field="low", allow_missing=True)
        high = parse_decimal(row.get("high"), field="high", allow_missing=True)
        if open_value is None or low is None or high is None:
            unchecked_missing += 1
            continue
        assert close is not None
        fully_observed += 1
        codes: list[str] = []
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
                    "ts_code": str(row.get("ts_code", "")),
                    "trade_date": str(row.get("trade_date", "")),
                    "anomaly_codes": sorted(codes),
                    "open": format(open_value, "f"),
                    "low": format(low, "f"),
                    "high": format(high, "f"),
                    "close": format(close, "f"),
                }
            )
    anomalies.sort(key=lambda item: (item["ts_code"], item["trade_date"]))
    return {
        "policy": OHLC_ORDERING_POLICY,
        "blocking": False,
        "formula_fields": ["close", "pe", "pb"],
        "row_count": len(rows),
        "fully_observed_row_count": fully_observed,
        "unchecked_missing_ohl_row_count": unchecked_missing,
        "anomaly_count": len(anomalies),
        "anomalies": anomalies,
    }


def validate_name_history(
    rows: list[dict[str, Any]],
    *,
    whitelist: Mapping[str, str],
    end_date: str,
    src: str,
) -> dict[str, Any]:
    """Validate and summarize mutable source labels on stable index codes.

    This function never joins by ``name``.  The classification whitelist owns
    identity, while each exact upstream ``sw_daily.name`` is retained as a
    time-varying label.  Trading-day continuity is a separate gate handled by
    :func:`validate_per_code_history_continuity`.  The returned object is the
    flattened evidence payload; callers record :data:`NAME_HISTORY_POLICY` as
    the surrounding axis policy instead of duplicating it inside that payload.
    """

    if src not in VALID_SOURCES:
        raise DataValidationError("INVALID_SOURCE", "src must be SW2021 or SW2014")
    if not isinstance(rows, list):
        raise DataValidationError("INVALID_CONTAINER", "name-history rows must be a list")
    if not isinstance(whitelist, Mapping) or not whitelist:
        raise DataValidationError("EMPTY_WHITELIST", "name-history whitelist is empty")
    common_end = parse_yyyymmdd(end_date, field="end_date")
    names_by_code: dict[str, str] = {}
    for code, classification_name in whitelist.items():
        if not isinstance(code, str) or not code.strip():
            raise DataValidationError("INVALID_INDEX_CODE", "whitelist code is invalid")
        if not isinstance(classification_name, str) or not classification_name.strip():
            raise DataValidationError(
                "INVALID_INDUSTRY_NAME", "whitelist classification name is empty"
            )
        names_by_code[code] = classification_name

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_keys: set[tuple[str, str]] = set()
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            raise DataValidationError(
                "INVALID_ROW", f"name-history row {position} must be a dict"
            )
        if "ts_code" not in row or "trade_date" not in row or "name" not in row:
            raise DataValidationError(
                "SCHEMA_MISMATCH", "name-history row lacks ts_code/trade_date/name"
            )
        code = row["ts_code"]
        if not isinstance(code, str) or code not in names_by_code:
            raise DataValidationError(
                "CODE_OUTSIDE_WHITELIST",
                "name history contains a code outside this classification axis",
                details={"position": position, "ts_code": code},
            )
        declared_sources = [
            row[field]
            for field in ("taxonomy", "src")
            if field in row and row[field] is not None
        ]
        if any(declared_src != src for declared_src in declared_sources):
            raise DataValidationError(
                "CROSS_AXIS_ROW",
                "name-history row declares another classification axis",
                details={"position": position, "ts_code": code},
            )
        trade_date = row["trade_date"]
        parsed_trade_date = parse_yyyymmdd(trade_date, field="trade_date")
        if parsed_trade_date > common_end:
            raise DataValidationError(
                "DATE_OUT_OF_RANGE", "name history contains a date after common end_date"
            )
        source_name = row["name"]
        if not isinstance(source_name, str) or not source_name.strip():
            raise DataValidationError(
                "INVALID_INDUSTRY_NAME",
                "sw_daily.name must be a non-empty string",
                details={"position": position, "ts_code": code},
            )
        key = (code, trade_date)
        if key in seen_keys:
            raise DataValidationError(
                "DUPLICATE_PRIMARY_KEY",
                "name history contains a duplicate (ts_code, trade_date)",
                details={"ts_code": code, "trade_date": trade_date},
            )
        seen_keys.add(key)
        grouped[code].append(row)

    missing_codes = sorted(set(names_by_code) - set(grouped))
    if missing_codes:
        raise DataValidationError(
            "MISSING_NAME_HISTORY",
            "one or more classified codes have no name history",
            details={"missing_count": len(missing_codes), "missing_sample": missing_codes[:20]},
        )

    segments: list[dict[str, Any]] = []
    renamed_codes: list[str] = []
    for code in sorted(names_by_code):
        series = sorted(grouped[code], key=lambda row: row["trade_date"])
        end_rows = [row for row in series if row["trade_date"] == end_date]
        if len(end_rows) != 1:
            raise DataValidationError(
                "COMMON_END_NAME_ROW_MISMATCH",
                "each code must have exactly one name row on common end_date",
                details={"ts_code": code, "actual": len(end_rows), "end_date": end_date},
            )
        if end_rows[0]["name"] != names_by_code[code]:
            raise DataValidationError(
                "END_NAME_MISMATCH",
                "common-end source name differs from frozen classification name",
                details={"ts_code": code, "end_date": end_date},
            )

        code_segments: list[dict[str, Any]] = []
        for row in series:
            source_name = row["name"]
            trade_date = row["trade_date"]
            if code_segments and code_segments[-1]["source_name"] == source_name:
                code_segments[-1]["last_date"] = trade_date
                code_segments[-1]["row_count"] += 1
            else:
                code_segments.append(
                    {
                        "index_code": code,
                        "source_name": source_name,
                        "first_date": trade_date,
                        "last_date": trade_date,
                        "row_count": 1,
                    }
                )
        if len(code_segments) > 1:
            renamed_codes.append(code)
        segments.extend(code_segments)

    segments.sort(key=lambda segment: (segment["index_code"], segment["first_date"]))
    renamed_codes.sort()
    return {
        "code_count": len(names_by_code),
        "renamed_code_count": len(renamed_codes),
        "renamed_codes": renamed_codes,
        "segment_count": len(segments),
        "segments": segments,
    }


def validate_as_of_coverage(
    rows: list[dict[str, Any]],
    *,
    whitelist: Mapping[str, str] | Iterable[str],
    as_of: str,
) -> None:
    """Require exactly one current-date row for every approved industry code."""

    parse_yyyymmdd(as_of, field="as_of")
    expected = set(whitelist) if isinstance(whitelist, Mapping) else set(whitelist)
    observed = [row["ts_code"] for row in rows if row.get("trade_date") == as_of]
    observed_set = set(observed)
    duplicates = sorted({code for code in observed if observed.count(code) > 1})
    if duplicates:
        raise DataValidationError(
            "DUPLICATE_AS_OF_ROW", "multiple current rows exist for an industry"
        )
    if observed_set != expected:
        raise DataValidationError(
            "AS_OF_COVERAGE_MISMATCH",
            "current-day published industry coverage is incomplete or contains extras",
            details={
                "missing": sorted(expected - observed_set),
                "unexpected": sorted(observed_set - expected),
            },
        )


def history_position_label(percentile: Decimal | int | str | None) -> str | None:
    """Map a 0..100 empirical percentile to the contract's neutral labels."""

    if percentile is None:
        return None
    parsed = parse_decimal(percentile, field="percentile")
    assert parsed is not None
    if parsed < 0 or parsed > 100:
        raise DataValidationError("INVALID_PERCENTILE", "percentile must be within [0, 100]")
    if parsed < 20:
        return "历史极低位"
    if parsed < 40:
        return "历史较低位"
    if parsed < 60:
        return "历史中位"
    if parsed < 80:
        return "历史较高位"
    return "历史极高位"


def empirical_cdf_le(
    rows: list[dict[str, Any]],
    field: str,
    as_of: str,
    *,
    minimum_valid_observations: int = 252,
    date_field: str = "trade_date",
) -> dict[str, Any]:
    """Compute ``count(x <= current) / N`` over positive finite observations.

    ``tie_ratio`` is a fraction in [0, 1]; ``tie_ratio_pct`` is included to make
    display scaling unambiguous.  The percentile itself is on a 0..100 scale.
    """

    as_of_date = parse_yyyymmdd(as_of, field="as_of")
    if minimum_valid_observations <= 0:
        raise DataValidationError(
            "INVALID_MINIMUM_OBSERVATIONS", "minimum_valid_observations must be positive"
        )
    if not isinstance(rows, list):
        raise DataValidationError("INVALID_CONTAINER", "history rows must be a list")
    dated: list[tuple[str, Decimal]] = []
    current_raw: Any = None
    current_found = False
    seen_dates: set[str] = set()
    seen_codes = {
        str(row["ts_code"])
        for row in rows
        if isinstance(row, Mapping) and row.get("ts_code") not in (None, "")
    }
    if len(seen_codes) > 1:
        raise DataValidationError(
            "MULTIPLE_SERIES", "percentile input must contain exactly one industry series"
        )
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            raise DataValidationError("INVALID_ROW", f"history row {position} must be a dict")
        if date_field not in row or field not in row:
            raise DataValidationError("SCHEMA_MISMATCH", "history row lacks date/value field")
        date_text = row[date_field]
        row_date = parse_yyyymmdd(date_text, field=date_field)
        if row_date > as_of_date:
            continue
        if date_text in seen_dates:
            raise DataValidationError("DUPLICATE_DATE", "history contains a duplicate date")
        seen_dates.add(date_text)
        if date_text == as_of:
            current_found = True
            current_raw = row[field]
        value = _finite_positive_or_none(row[field], field=field)
        if value is not None:
            dated.append((date_text, value))

    dated.sort(key=lambda item: item[0])
    valid_count = len(dated)
    first_valid = dated[0][0] if dated else None
    last_valid = dated[-1][0] if dated else None
    current_value = (
        _finite_positive_or_none(current_raw, field=field) if current_found else None
    )
    if not current_found:
        status = STATUS_CURRENT_MISSING
    elif current_value is None:
        status = STATUS_CURRENT_INVALID
    elif valid_count < minimum_valid_observations:
        status = STATUS_HISTORY_INSUFFICIENT
    else:
        status = STATUS_OK

    tie_count = 0
    tie_ratio: Decimal | None = None
    tie_ratio_pct: Decimal | None = None
    percentile: Decimal | None = None
    if current_value is not None and valid_count:
        tie_count = sum(value == current_value for _, value in dated)
        less_or_equal = sum(value <= current_value for _, value in dated)
        with localcontext() as context:
            context.prec = 50
            tie_ratio = Decimal(tie_count) / Decimal(valid_count)
            tie_ratio_pct = tie_ratio * Decimal(100)
            if status == STATUS_OK:
                percentile = Decimal(less_or_equal) * Decimal(100) / Decimal(valid_count)

    return {
        "field": field,
        "as_of": as_of,
        "current_value": current_value,
        "percentile_le": percentile,
        "valid_count": valid_count,
        "tie_count": tie_count,
        "tie_ratio": tie_ratio,
        "tie_ratio_pct": tie_ratio_pct,
        "first_valid_date": first_valid,
        "last_valid_date": last_valid,
        "minimum_valid_observations": minimum_valid_observations,
        "status": status,
        "history_label": history_position_label(percentile),
    }


def _open_calendar_axis(
    calendar_rows: list[dict[str, Any]],
) -> tuple[list[str], dict[str, str | None]]:
    open_dates: set[str] = set()
    predecessors: dict[str, str | None] = {}
    for position, row in enumerate(calendar_rows):
        if not isinstance(row, dict) or "cal_date" not in row or "is_open" not in row:
            raise DataValidationError(
                "SCHEMA_MISMATCH", f"calendar row {position} lacks cal_date/is_open"
            )
        date_text = row["cal_date"]
        parse_yyyymmdd(date_text, field="cal_date")
        if _normalize_binary_flag(row["is_open"], field="is_open") == 1:
            open_dates.add(date_text)
            raw_predecessor = row.get("pretrade_date")
            predecessor = (
                str(raw_predecessor).strip()
                if raw_predecessor is not None and str(raw_predecessor).strip()
                else None
            )
            if predecessor is not None:
                predecessor_date = parse_yyyymmdd(predecessor, field="pretrade_date")
                if predecessor_date >= parse_yyyymmdd(date_text, field="cal_date"):
                    raise DataValidationError(
                        "INVALID_PRETRADE_DATE", "pretrade_date must precede the open date"
                    )
            if date_text in predecessors and predecessors[date_text] != predecessor:
                raise DataValidationError(
                    "CALENDAR_CONFLICT", "exchanges disagree on pretrade_date"
                )
            predecessors[date_text] = predecessor
    return sorted(open_dates), predecessors


def _previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def compute_close_returns(
    rows: list[dict[str, Any]],
    calendar_rows: list[dict[str, Any]],
    as_of: str,
    *,
    date_field: str = "trade_date",
    close_field: str = "close",
) -> dict[str, Any]:
    """Compute exact 5D/MTD/YTD close-anchor returns for one industry series."""

    as_of_date = parse_yyyymmdd(as_of, field="as_of")
    open_dates, predecessors = _open_calendar_axis(calendar_rows)
    if as_of not in open_dates:
        raise DataValidationError("AS_OF_NOT_OPEN", "as_of is not an observed open day")

    close_by_date: dict[str, Decimal | None] = {}
    seen_codes = {
        str(row["ts_code"])
        for row in rows
        if isinstance(row, Mapping) and row.get("ts_code") not in (None, "")
    }
    if len(seen_codes) > 1:
        raise DataValidationError(
            "MULTIPLE_SERIES", "return input must contain exactly one industry series"
        )
    for position, row in enumerate(rows):
        if not isinstance(row, dict) or date_field not in row or close_field not in row:
            raise DataValidationError(
                "SCHEMA_MISMATCH", f"price row {position} lacks date/close field"
            )
        date_text = row[date_field]
        row_date = parse_yyyymmdd(date_text, field=date_field)
        if row_date > as_of_date:
            continue
        if date_text in close_by_date:
            raise DataValidationError("DUPLICATE_DATE", "price history contains a duplicate date")
        close_by_date[date_text] = _finite_positive_or_none(
            row[close_field], field=close_field
        )

    def walk_back(start: str, steps: int) -> str | None:
        current = start
        seen = {current}
        for _ in range(steps):
            predecessor = predecessors.get(current)
            if predecessor is None or predecessor in seen:
                return None
            seen.add(predecessor)
            current = predecessor
        return current

    def cross_period(start: str, *, period: str) -> str | None:
        current = start
        seen = {current}
        while True:
            predecessor = predecessors.get(current)
            if predecessor is None or predecessor in seen:
                return None
            seen.add(predecessor)
            previous_date = parse_yyyymmdd(predecessor, field="pretrade_date")
            if period == "month":
                if (previous_date.year, previous_date.month) == (
                    as_of_date.year,
                    as_of_date.month,
                ):
                    current = predecessor
                    continue
                expected_period = _previous_month(as_of_date.year, as_of_date.month)
                return predecessor if (previous_date.year, previous_date.month) == expected_period else None
            if previous_date.year == as_of_date.year:
                current = predecessor
                continue
            return predecessor if previous_date.year == as_of_date.year - 1 else None

    five_day_anchor = walk_back(as_of, 5)
    anchors = {
        "return_5d": five_day_anchor,
        "return_mtd": cross_period(as_of, period="month"),
        "return_ytd": cross_period(as_of, period="year"),
    }
    current_present = as_of in close_by_date
    current_close = close_by_date.get(as_of)
    result: dict[str, Any] = {"as_of": as_of, "close": current_close}
    for output_field, anchor_date in anchors.items():
        anchor_close = close_by_date.get(anchor_date) if anchor_date else None
        if current_close is None:
            status = STATUS_CURRENT_INVALID if current_present else STATUS_CURRENT_MISSING
            value = None
        elif anchor_date is None or anchor_close is None:
            status = STATUS_HISTORY_INSUFFICIENT
            value = None
        else:
            with localcontext() as context:
                context.prec = 50
                value = current_close / anchor_close - Decimal(1)
            status = STATUS_OK
        result[output_field] = value
        result[f"{output_field}_anchor_date"] = anchor_date
        result[f"{output_field}_anchor_close"] = anchor_close
        result[f"{output_field}_status"] = status
    return result


def normalize_history(
    rows: list[dict[str, Any]],
    classifications: list[dict[str, Any]],
    *,
    src: str,
) -> list[dict[str, Any]]:
    """Return the stable, version-labelled history table used by artifacts."""

    if src not in VALID_SOURCES:
        raise DataValidationError("INVALID_SOURCE", "src must be SW2021 or SW2014")
    whitelist = classification_whitelist(
        classifications,
        published_only=src == "SW2021",
        src=src,
    )
    normalized: list[dict[str, Any]] = []
    for position, row in enumerate(rows):
        code = row["ts_code"]
        if code not in whitelist:
            raise DataValidationError("CODE_OUTSIDE_WHITELIST", "history code is not approved")
        source_name = row.get("name")
        if not isinstance(source_name, str) or not source_name.strip():
            raise DataValidationError(
                "INVALID_INDUSTRY_NAME",
                "history source name must be a non-empty string",
                details={"position": position, "ts_code": code},
            )
        normalized.append(
            {
                "src": src,
                "index_code": code,
                # The classification label is the frozen display identity.  It
                # must not be replaced by a historical upstream label.
                "industry_name": whitelist[code],
                "source_name": source_name,
                "trade_date": row["trade_date"],
                "close": parse_decimal(row["close"], field="close"),
                "pe": _finite_or_none(row["pe"], field="pe"),
                "pb": _finite_or_none(row["pb"], field="pb"),
            }
        )
    return sorted(normalized, key=lambda row: (row["index_code"], row["trade_date"]))


def summarize_axis(
    sw_daily_rows: list[dict[str, Any]],
    classifications: list[dict[str, Any]],
    calendar_rows: list[dict[str, Any]],
    *,
    src: str,
    as_of: str,
    minimum_valid_observations: int = 252,
) -> list[dict[str, Any]]:
    """Build one current summary row per published industry on one SW axis."""

    if src not in VALID_SOURCES:
        raise DataValidationError("INVALID_SOURCE", "src must be SW2021 or SW2014")
    whitelist = classification_whitelist(
        classifications,
        published_only=src == "SW2021",
        src=src,
    )
    validate_as_of_coverage(sw_daily_rows, whitelist=whitelist, as_of=as_of)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sw_daily_rows:
        code = row.get("ts_code")
        if code not in whitelist:
            raise DataValidationError(
                "CODE_OUTSIDE_WHITELIST", "summary input contains an unapproved code"
            )
        grouped[str(code)].append(row)

    summary: list[dict[str, Any]] = []
    for code in sorted(whitelist):
        series = grouped[code]
        current_rows = [row for row in series if row["trade_date"] == as_of]
        if len(current_rows) != 1:
            raise DataValidationError(
                "AS_OF_COVERAGE_MISMATCH", "industry does not have exactly one current row"
            )
        current = current_rows[0]
        output: dict[str, Any] = {
            "src": src,
            "index_code": code,
            "industry_name": whitelist[code],
            "as_of": as_of,
            "close": parse_decimal(current["close"], field="close"),
            "pe": _finite_or_none(current["pe"], field="pe"),
            "pb": _finite_or_none(current["pb"], field="pb"),
            "pe_raw": current["pe"],
            "pb_raw": current["pb"],
        }
        for field in ("pe", "pb"):
            metric = empirical_cdf_le(
                series,
                field,
                as_of,
                minimum_valid_observations=minimum_valid_observations,
            )
            for key in (
                "percentile_le",
                "valid_count",
                "tie_count",
                "tie_ratio",
                "tie_ratio_pct",
                "first_valid_date",
                "last_valid_date",
                "status",
                "history_label",
            ):
                output[f"{field}_{key}"] = metric[key]
        returns = compute_close_returns(series, calendar_rows, as_of)
        for key, value in returns.items():
            if key not in ("as_of", "close"):
                output[key] = value
        summary.append(output)
    return summary


# Clear aliases for callers that use longer domain names.
validate_trade_calendar = validate_trade_cal
validate_classification = validate_index_classify
compute_empirical_percentile = empirical_cdf_le
compute_anchor_returns = compute_close_returns
build_current_summary = summarize_axis
validate_per_code_continuity = validate_per_code_history_continuity


__all__ = [
    "AXIS_CLASSIFICATION_COUNTS",
    "DataValidationError",
    "NAME_HISTORY_POLICY",
    "OHLC_ORDERING_POLICY",
    "PER_CODE_CONTINUITY_POLICY",
    "RETIRED_PUBLICATION_STATE",
    "RETIRED_SELECTION_BASIS",
    "STATUS_CURRENT_INVALID",
    "STATUS_CURRENT_MISSING",
    "STATUS_HISTORY_INSUFFICIENT",
    "STATUS_OK",
    "build_current_summary",
    "classification_whitelist",
    "compute_anchor_returns",
    "compute_close_returns",
    "compute_empirical_percentile",
    "empirical_cdf_le",
    "history_position_label",
    "normalize_history",
    "ohlc_ordering_audit",
    "parse_decimal",
    "parse_yyyymmdd",
    "summarize_axis",
    "validate_as_of_coverage",
    "validate_classification",
    "validate_index_classify",
    "validate_name_history",
    "validate_per_code_history_continuity",
    "validate_per_code_continuity",
    "validate_sw_daily",
    "validate_trade_cal",
    "validate_trade_calendar",
]
