"""Domain rules for the portable SWIVD v2 application.

This module is intentionally free of network, filesystem and UI concerns.  It
owns the SW2021 hierarchy, point-in-time membership states and stock peer
percentiles.  Industry index valuation remains in :mod:`swivd.core` and is
never derived here from stock rows.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Iterable, Mapping, Sequence


LEVELS = ("L1", "L2", "L3")
EXPECTED_COUNTS = {"L1": 31, "L2": 134, "L3": 346}
RAW_MEMBER_FIELDS = (
    "l1_code",
    "l1_name",
    "l2_code",
    "l2_name",
    "l3_code",
    "l3_name",
    "ts_code",
    "name",
    "in_date",
    "out_date",
    "is_new",
)
MEMBERSHIP_PRIMARY_KEY_FIELDS = (
    "l3_code",
    "ts_code",
    "in_date",
    "out_date",
    "is_new",
)
MEMBER_FIELDS = (
    *RAW_MEMBER_FIELDS,
    "industry_uid",
    "source_l3_code",
    "identity_state",
    "identity_rule_version",
)
VALUATION_FIELDS = (
    "ts_code",
    "trade_date",
    "close",
    "pe",
    "pe_ttm",
    "pb",
    "ps_ttm",
    "dv_ttm",
    "total_mv",
    "circ_mv",
)
PEER_FIELDS = ("pe_ttm", "pb")


class V2DataError(ValueError):
    """Fail-closed domain error with a stable, non-secret code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def validate_membership_primary_keys(
    rows: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Freeze the endpoint primary-key contract without silent deduplication.

    Tushare's non-key display fields are not allowed to distinguish two
    lifecycle records.  Returning the same key twice is therefore ambiguous
    even when every byte of the two rows is identical.
    """

    materialized = list(rows)
    seen: set[tuple[str, ...]] = set()
    for source in materialized:
        if not set(MEMBERSHIP_PRIMARY_KEY_FIELDS).issubset(source):
            raise V2DataError("SCHEMA_MISMATCH", "index_member_all primary key drifted")
        key = (
            _text(source.get("l3_code"), "l3_code"),
            _text(source.get("ts_code"), "ts_code"),
            _date(source.get("in_date"), "in_date"),
            _date(source.get("out_date"), "out_date", allow_blank=True),
            _text(source.get("is_new"), "is_new"),
        )
        if key in seen:
            raise V2DataError(
                "MEMBERSHIP_PRIMARY_KEY_DUPLICATE",
                "index_member_all returned the same lifecycle primary key more than once",
            )
        seen.add(key)
    return materialized


def _text(value: Any, field: str, *, allow_blank: bool = False) -> str:
    if value is None:
        rendered = ""
    else:
        rendered = str(value).strip()
    if not allow_blank and not rendered:
        raise V2DataError("SCHEMA_MISMATCH", f"{field} is blank")
    if "\x00" in rendered or "\r" in rendered or "\n" in rendered:
        raise V2DataError("SCHEMA_MISMATCH", f"{field} contains forbidden characters")
    return rendered


def _date(value: Any, field: str, *, allow_blank: bool = False) -> str:
    rendered = _text(value, field, allow_blank=allow_blank)
    if not rendered and allow_blank:
        return ""
    if len(rendered) != 8 or not rendered.isdigit():
        raise V2DataError("INVALID_DATE", f"{field} must be YYYYMMDD")
    try:
        import datetime as _datetime

        _datetime.datetime.strptime(rendered, "%Y%m%d")
    except ValueError as exc:
        raise V2DataError("INVALID_DATE", f"{field} is not a calendar date") from exc
    return rendered


def _positive_decimal(value: Any) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def normalize_classifications(
    raw_by_level: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Validate all three current SW2021 levels and build exact L3 paths."""

    if set(raw_by_level) != set(LEVELS):
        raise V2DataError("CLASSIFICATION_LEVELS_MISSING", "L1/L2/L3 are required")
    normalized: dict[str, list[dict[str, Any]]] = {}
    required = {
        "index_code",
        "industry_name",
        "parent_code",
        "level",
        "industry_code",
        "is_pub",
        "src",
    }
    for level in LEVELS:
        source_rows = list(raw_by_level[level])
        if len(source_rows) != EXPECTED_COUNTS[level]:
            raise V2DataError(
                "CLASSIFICATION_COUNT_MISMATCH",
                f"{level} expected {EXPECTED_COUNTS[level]}, got {len(source_rows)}",
            )
        rows: list[dict[str, Any]] = []
        seen_index: set[str] = set()
        seen_industry: set[str] = set()
        for source in source_rows:
            if not required.issubset(source):
                raise V2DataError("SCHEMA_MISMATCH", f"index_classify {level} fields drifted")
            if source.get("src") != "SW2021" or source.get("level") != level:
                raise V2DataError("CLASSIFICATION_AXIS_MISMATCH", f"unexpected {level} axis")
            index_code = _text(source.get("index_code"), "index_code")
            industry_code = _text(source.get("industry_code"), "industry_code")
            if index_code in seen_index or industry_code in seen_industry:
                raise V2DataError("DUPLICATE_CLASSIFICATION", f"duplicate {level} identity")
            seen_index.add(index_code)
            seen_industry.add(industry_code)
            is_pub_raw = source.get("is_pub")
            if isinstance(is_pub_raw, bool):
                raise V2DataError("INVALID_PUBLICATION_FLAG", "is_pub must be 0 or 1")
            try:
                is_pub = int(str(is_pub_raw))
            except (TypeError, ValueError) as exc:
                raise V2DataError("INVALID_PUBLICATION_FLAG", "is_pub must be 0 or 1") from exc
            if is_pub not in (0, 1):
                raise V2DataError("INVALID_PUBLICATION_FLAG", "is_pub must be 0 or 1")
            industry_uid = _text(
                source.get("industry_uid") or f"SW2021:{level}:{industry_code}",
                "industry_uid",
            )
            catalog_index_code = _text(
                source.get("catalog_index_code") or index_code,
                "catalog_index_code",
            )
            quote_index_code = _text(
                source.get("quote_index_code") or index_code,
                "quote_index_code",
            )
            member_index_code = _text(
                source.get("member_index_code") or index_code,
                "member_index_code",
            )
            rows.append(
                {
                    "src": "SW2021",
                    "level": level,
                    "index_code": index_code,
                    "catalog_index_code": catalog_index_code,
                    "quote_index_code": quote_index_code,
                    "member_index_code": member_index_code,
                    "industry_uid": industry_uid,
                    "industry_name": _text(source.get("industry_name"), "industry_name"),
                    "industry_code": industry_code,
                    "parent_code": _text(
                        source.get("parent_code"), "parent_code", allow_blank=level == "L1"
                    ),
                    "is_pub": is_pub,
                    "identity_state": _text(
                        source.get("identity_state") or "DIRECT",
                        "identity_state",
                    ),
                    "identity_rule": _text(
                        source.get("identity_rule")
                        or "CATALOG_EQUALS_QUOTE_AND_MEMBER_CODE",
                        "identity_rule",
                    ),
                    "identity_rule_version": _text(
                        source.get("identity_rule_version")
                        or "swivd-special-steel-identity-v1",
                        "identity_rule_version",
                    ),
                }
            )
        normalized[level] = sorted(rows, key=lambda row: row["index_code"])

    l1_by_industry = {row["industry_code"]: row for row in normalized["L1"]}
    l2_paths: dict[str, dict[str, Any]] = {}
    for row in normalized["L2"]:
        parent = l1_by_industry.get(row["parent_code"])
        if parent is None:
            raise V2DataError("CLASSIFICATION_PARENT_MISSING", "L2 parent is not an L1")
        l2_paths[row["index_code"]] = {
            "l1_code": parent["index_code"],
            "l1_name": parent["industry_name"],
            "l2_code": row["index_code"],
            "l2_name": row["industry_name"],
            "l2_industry_code": row["industry_code"],
        }
    l2_by_industry = {path["l2_industry_code"]: path for path in l2_paths.values()}
    l3_paths: dict[str, dict[str, Any]] = {}
    for row in normalized["L3"]:
        parent = l2_by_industry.get(row["parent_code"])
        if parent is None:
            raise V2DataError("CLASSIFICATION_PARENT_MISSING", "L3 parent is not an L2")
        l3_paths[row["index_code"]] = {
            "l1_code": parent["l1_code"],
            "l1_name": parent["l1_name"],
            "l2_code": parent["l2_code"],
            "l2_name": parent["l2_name"],
            "l3_code": row["index_code"],
            "l3_name": row["industry_name"],
            "industry_uid": row["industry_uid"],
            "identity_state": row["identity_state"],
            "identity_rule_version": row["identity_rule_version"],
        }
    return normalized, l3_paths


def normalize_members(
    rows: Iterable[Mapping[str, Any]],
    *,
    l3_paths: Mapping[str, Mapping[str, Any]],
    legacy_display_name_key: bool = False,
) -> list[dict[str, str]]:
    """Normalize Y/N episodes without inventing identity mappings.

    Display names do not distinguish an episode.  The closed response owns
    the selected row, including its unchanged display name; both source names
    remain in the separately frozen raw responses.  The legacy flag exists
    only for replaying snapshots written before contract v2.3.
    """

    exact: dict[tuple[str, ...], dict[str, str]] = {}
    for source in validate_membership_primary_keys(rows):
        if not set(RAW_MEMBER_FIELDS).issubset(source):
            raise V2DataError("SCHEMA_MISMATCH", "index_member_all fields drifted")
        row = {
            field: _text(source.get(field), field, allow_blank=field == "out_date")
            for field in RAW_MEMBER_FIELDS
        }
        row["in_date"] = _date(row["in_date"], "in_date")
        row["out_date"] = _date(row["out_date"], "out_date", allow_blank=True)
        if row["is_new"] not in {"Y", "N"}:
            raise V2DataError("INVALID_MEMBER_STATE", "is_new must be Y or N")
        if (row["is_new"] == "Y" and row["out_date"]) or (
            row["is_new"] == "N" and not row["out_date"]
        ):
            raise V2DataError("MEMBER_STATE_DATE_CONFLICT", "is_new conflicts with out_date")
        if row["out_date"] and row["in_date"] > row["out_date"]:
            raise V2DataError("INVALID_MEMBER_INTERVAL", "in_date is after out_date")
        path = l3_paths.get(row["l3_code"])
        if path is None:
            raise V2DataError("MEMBER_PATH_UNKNOWN", "membership L3 code is outside SW2021")
        for field in ("l1_code", "l1_name", "l2_code", "l2_name", "l3_code", "l3_name"):
            if row[field] != path[field]:
                raise V2DataError("MEMBER_PATH_MISMATCH", f"membership path differs at {field}")
        row["industry_uid"] = _text(
            source.get("industry_uid") or path.get("industry_uid"),
            "industry_uid",
        )
        row["source_l3_code"] = _text(
            source.get("source_l3_code") or row["l3_code"],
            "source_l3_code",
        )
        row["identity_state"] = _text(
            source.get("identity_state") or path.get("identity_state") or "DIRECT",
            "identity_state",
        )
        row["identity_rule_version"] = _text(
            source.get("identity_rule_version")
            or path.get("identity_rule_version")
            or "swivd-special-steel-identity-v1",
            "identity_rule_version",
        )
        key = tuple(row[field] for field in MEMBER_FIELDS)
        exact[key] = row
    if not exact:
        raise V2DataError("EMPTY_MEMBERSHIP", "index_member_all returned no episodes")

    # Tushare can return the same episode in both queries: the Y response is an
    # older open-ended view while N carries its eventual out_date.  Reconcile
    # only records with the exact same stock, hierarchy and in_date.  Different
    # paths or start dates remain separate episodes and are handled as overlap.
    episode_fields = (
        "l1_code",
        "l1_name",
        "l2_code",
        "l2_name",
        "l3_code",
        "l3_name",
        "ts_code",
        "name",
        "in_date",
        "industry_uid",
        "source_l3_code",
    )
    if not legacy_display_name_key:
        episode_fields = tuple(field for field in episode_fields if field != "name")
    grouped: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    for row in exact.values():
        grouped[tuple(row[field] for field in episode_fields)].append(row)
    reconciled: list[dict[str, str]] = []
    for key in sorted(grouped):
        candidates = grouped[key]
        closed = {row["out_date"]: row for row in candidates if row["is_new"] == "N"}
        if len(closed) > 1:
            raise V2DataError(
                "MEMBERSHIP_EPISODE_CONFLICT",
                "one episode has multiple authoritative out_date values",
            )
        if closed:
            reconciled.append(closed[sorted(closed)[0]])
            continue
        opened = [row for row in candidates if row["is_new"] == "Y"]
        if len(opened) != 1:
            raise V2DataError("MEMBERSHIP_EPISODE_CONFLICT", "open episode is ambiguous")
        reconciled.append(opened[0])
    return sorted(reconciled, key=lambda row: tuple(row[field] for field in MEMBER_FIELDS))


def select_members_as_of(
    episodes: Sequence[Mapping[str, str]], as_of: str
) -> list[dict[str, str]]:
    """Return candidates for one date, localizing unresolved interval boundaries."""

    target = _date(as_of, "as_of")
    by_stock: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in episodes:
        by_stock[row["ts_code"]].append(row)
    selected: list[dict[str, str]] = []
    for ts_code in sorted(by_stock):
        rows = by_stock[ts_code]
        boundary = [
            row
            for row in rows
            if row["in_date"] == target or (row["out_date"] and row["out_date"] == target)
        ]
        active = [
            row
            for row in rows
            if row["in_date"] < target and (not row["out_date"] or target < row["out_date"])
        ]
        if boundary:
            candidates = {tuple(row[field] for field in MEMBER_FIELDS): row for row in boundary + active}
            for key in sorted(candidates):
                output = dict(candidates[key])
                output["membership_state"] = "MEMBERSHIP_BOUNDARY_UNKNOWN"
                selected.append(output)
            continue
        if not active:
            continue
        unique_paths = {row["l3_code"] for row in active}
        unique_episodes = {
            (row["l3_code"], row["in_date"], row["out_date"], row["is_new"])
            for row in active
        }
        state = (
            "ACTIVE"
            if len(unique_paths) == 1 and len(unique_episodes) == 1
            else "MEMBERSHIP_OVERLAP_UNKNOWN"
        )
        for row in active:
            output = dict(row)
            output["membership_state"] = state
            selected.append(output)
    if not selected:
        raise V2DataError("NO_MEMBERS_AS_OF", "no membership candidates exist on target date")
    return sorted(selected, key=lambda row: (row["l3_code"], row["ts_code"], row["in_date"]))


def normalize_daily_basic(
    rows: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
    row_limit: int = 6000,
    allow_empty: bool = False,
) -> dict[str, dict[str, Any]]:
    """Validate one market snapshot, distinguishing total absence from gaps.

    A missing member row remains permitted by ``compute_peer_rows``.  An
    entirely empty market response cannot establish target-date availability.
    ``allow_empty`` is reserved for replay of pre-v2.3 snapshots.
    """

    if not rows and not allow_empty:
        raise V2DataError("DAILY_BASIC_EMPTY", "daily_basic has no target-date rows")
    if len(rows) >= row_limit:
        raise V2DataError("DAILY_BASIC_ROW_LIMIT", "daily_basic may be truncated")
    target = _date(as_of, "as_of")
    output: dict[str, dict[str, Any]] = {}
    for source in rows:
        if not set(VALUATION_FIELDS).issubset(source):
            raise V2DataError("SCHEMA_MISMATCH", "daily_basic fields drifted")
        code = _text(source.get("ts_code"), "ts_code")
        if _date(source.get("trade_date"), "trade_date") != target:
            raise V2DataError("VALUATION_DATE_MISMATCH", "daily_basic mixed target dates")
        if code in output:
            raise V2DataError("DUPLICATE_DAILY_BASIC", "daily_basic primary key duplicated")
        output[code] = {field: source.get(field) for field in VALUATION_FIELDS}
    return output


def _percentile(values: Sequence[Decimal], current: Decimal) -> tuple[Decimal, int]:
    with localcontext() as context:
        context.prec = 50
        less_equal = sum(value <= current for value in values)
        percentile = Decimal(less_equal) / Decimal(len(values)) * Decimal(100)
    return percentile, sum(value == current for value in values)


def compute_peer_rows(
    members: Sequence[Mapping[str, str]],
    valuations: Mapping[str, Mapping[str, Any]],
    *,
    as_of: str,
    minimum_peers: int = 5,
) -> list[dict[str, Any]]:
    """Expand members to L1/L2/L3 rows and compute level-specific peer positions."""

    expanded: list[dict[str, Any]] = []
    for member in members:
        valuation = valuations.get(member["ts_code"])
        for level in LEVELS:
            prefix = level.lower()
            row: dict[str, Any] = {
                "as_of": as_of,
                "level": level,
                "index_code": member[f"{prefix}_code"],
                "industry_name": member[f"{prefix}_name"],
                "l1_code": member["l1_code"],
                "l1_name": member["l1_name"],
                "l2_code": member["l2_code"],
                "l2_name": member["l2_name"],
                "l3_code": member["l3_code"],
                "l3_name": member["l3_name"],
                "ts_code": member["ts_code"],
                "stock_name": member["name"],
                "in_date": member["in_date"],
                "out_date": member["out_date"],
                "membership_state": member["membership_state"],
                "valuation_state": "OK" if valuation is not None else "VALUATION_UNAVAILABLE",
            }
            for field in VALUATION_FIELDS[2:]:
                row[field] = valuation.get(field) if valuation is not None else None
            expanded.append(row)

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in expanded:
        groups[(row["level"], row["index_code"])].append(row)
    for group_rows in groups.values():
        member_count = len({row["ts_code"] for row in group_rows})
        group_unknown = any(row["membership_state"] != "ACTIVE" for row in group_rows)
        for field in PEER_FIELDS:
            values = [
                parsed
                for row in group_rows
                if row["membership_state"] == "ACTIVE"
                for parsed in [_positive_decimal(row[field])]
                if parsed is not None
            ]
            valid_n = len(values)
            coverage = Decimal(valid_n) / Decimal(member_count) if member_count else Decimal(0)
            for row in group_rows:
                row[f"{field}_member_count"] = member_count
                row[f"{field}_valid_n"] = valid_n
                row[f"{field}_coverage"] = coverage
                row[f"{field}_percentile_le"] = None
                row[f"{field}_tie_count"] = 0
                if group_unknown:
                    row[f"{field}_percentile_state"] = "MEMBERSHIP_UNKNOWN"
                    continue
                current = _positive_decimal(row[field])
                if current is None:
                    row[f"{field}_percentile_state"] = "CURRENT_INVALID"
                elif valid_n < minimum_peers:
                    row[f"{field}_percentile_state"] = "INSUFFICIENT_PEERS"
                else:
                    percentile, tie_count = _percentile(values, current)
                    row[f"{field}_percentile_le"] = percentile
                    row[f"{field}_tie_count"] = tie_count
                    row[f"{field}_percentile_state"] = "OK"
    return sorted(expanded, key=lambda row: (row["level"], row["index_code"], row["ts_code"], row["in_date"]))


def validate_history_continuity(
    rows: Sequence[Mapping[str, Any]],
    *,
    published_codes: Iterable[str],
    open_dates: Sequence[str],
    start_date: str,
    as_of: str,
    level: str,
) -> dict[str, Any]:
    """Require L1 common inception and L2/L3 per-code observed inception."""

    target = _date(as_of, "as_of")
    start = _date(start_date, "start_date")
    allowed = set(published_codes)
    calendar = [value for value in open_dates if start <= value <= target]
    grouped: dict[str, set[str]] = defaultdict(set)
    seen: set[tuple[str, str]] = set()
    for row in rows:
        code = _text(row.get("ts_code"), "ts_code")
        date = _date(row.get("trade_date"), "trade_date")
        if code not in allowed or date not in calendar:
            raise V2DataError("INDUSTRY_HISTORY_OUTSIDE_AXIS", "history is outside whitelist/calendar")
        if (code, date) in seen:
            raise V2DataError("DUPLICATE_INDUSTRY_HISTORY", "industry history key duplicated")
        seen.add((code, date))
        grouped[code].add(date)
    if set(grouped) != allowed:
        raise V2DataError("MISSING_INDUSTRY_HISTORY", "published industry series is missing")
    details: list[dict[str, Any]] = []
    for code in sorted(allowed):
        observed = sorted(grouped[code])
        if observed[-1] != target:
            raise V2DataError("CURRENT_INDUSTRY_ROW_MISSING", f"{code} has no as_of row")
        first = start if level == "L1" else observed[0]
        expected = {date for date in calendar if first <= date <= target}
        if grouped[code] != expected:
            raise V2DataError("INDUSTRY_HISTORY_GAP", f"{code} has an internal open-day gap")
        details.append({"index_code": code, "first_date": observed[0], "row_count": len(observed)})
    return {"level": level, "code_count": len(allowed), "per_code": details}


__all__ = [
    "EXPECTED_COUNTS",
    "LEVELS",
    "MEMBER_FIELDS",
    "RAW_MEMBER_FIELDS",
    "PEER_FIELDS",
    "VALUATION_FIELDS",
    "V2DataError",
    "compute_peer_rows",
    "normalize_classifications",
    "normalize_daily_basic",
    "normalize_members",
    "validate_membership_primary_keys",
    "select_members_as_of",
    "validate_history_continuity",
]
