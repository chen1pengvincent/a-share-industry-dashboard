"""Portable Chinese collation and exact financial ordering shared by exports/UI."""
from __future__ import annotations

from functools import cmp_to_key
from pypinyin import Style, lazy_pinyin
from .models import DataError, decimal_value

PAGES = {"valuation", "moneyflow", "fusion"}
VIEWS = {"overview", "heatmap", "trend", "ranking", "scatter", "detail"}
STRING_KEYS = {"name", "code", "uid", "ts_code", "status", "membership_state", "evidence_kind"}
NUMERIC_KEYS = {
    "pe_ttm_median", "pb_median", "official_pe", "official_pb", "close", "flow_cent", "net_mf_vol",
    "pe_percentile", "pb_percentile", "official_pe_percentile", "official_pb_percentile",
    "return_5d", "return_mtd", "return_ytd", "member_count", "unknown_member_count", "basic_received",
    "pe_valid", "pb_valid", "flow_expected", "flow_received", "flow_rank", "pe_ttm", "pb", "pe",
    "ps_ttm", "dv_ttm", "total_mv", "circ_mv", "pe_peer_percentile", "pb_peer_percentile",
}


def name_sort_key(name: object) -> str:
    """Pypinyin's pinned phrase dictionary; unknown characters retain identity."""
    if name is None:
        return ""
    return " ".join(lazy_pinyin(str(name), style=Style.NORMAL, errors=lambda chars: list(chars))).lower()


def add_name_sort_keys(rows: list[dict]) -> list[dict]:
    return [{**row, "name_sort_key": name_sort_key(row.get("name"))} for row in rows]


def _text_key(value: object) -> bytes:
    # JS compares UTF-16 code units. Pinyin is the Chinese primary key; this
    # secondary ordering keeps uncommon characters identical in Python and JS.
    return str(value).encode("utf-16-be", errors="surrogatepass")


def row_value(row: dict, key: str):
    if key == "name":
        return row.get("name_sort_key") or name_sort_key(row.get("name"))
    if key in row.get("metrics", {}):
        return row["metrics"][key].get("value")
    if key in row.get("counts", {}):
        return row["counts"][key]
    return row.get(key)


def default_sort(page="fusion", view="detail", *, members=False, contribution=False) -> tuple[str, str]:
    if contribution:
        return "flow_cent", "desc"
    if members:
        return "ts_code", "asc"
    if page not in PAGES or view not in VIEWS:
        raise DataError("INVALID_EXPORT_VIEW")
    if page == "moneyflow":
        return "flow_cent", "desc"
    if page == "valuation":
        return ("return_5d", "desc") if view == "ranking" else ("name", "asc")
    return "code", "asc"


def sort_rows(rows: list[dict], sort_key=None, sort_direction="default", *, page="fusion", view="detail",
              members=False, contribution=False) -> list[dict]:
    if sort_direction not in {"default", "asc", "desc"}:
        raise DataError("INVALID_SORT_DIRECTION")
    if sort_direction == "default" or sort_key is None:
        key, direction = default_sort(page, view, members=members, contribution=contribution)
    else:
        key, direction = sort_key, sort_direction
    if key not in STRING_KEYS | NUMERIC_KEYS:
        raise DataError("INVALID_SORT_KEY")
    numeric = key in NUMERIC_KEYS

    def compare(left, right):
        lv, rv = row_value(left, key), row_value(right, key)
        if numeric:
            lv, rv = decimal_value(lv), decimal_value(rv)
        lm, rm = lv is None or lv == "", rv is None or rv == ""
        if lm != rm:
            return 1 if lm else -1
        primary = 0
        if not lm:
            a, b = (lv, rv) if numeric else (_text_key(lv), _text_key(rv))
            primary = (a > b) - (a < b)
        if primary:
            return -primary if direction == "desc" else primary
        if key == "name":
            a, b = _text_key(left.get("name") or ""), _text_key(right.get("name") or "")
            if a != b:
                return (a > b) - (a < b)
        identity = lambda row: _text_key(str(row.get("uid") or "") + "/" + str(row.get("ts_code") or ""))
        a, b = identity(left), identity(right)
        return (a > b) - (a < b)

    return sorted(rows, key=cmp_to_key(compare))


def filter_and_sort(rows: list[dict], params: dict) -> list[dict]:
    parent = params.get("parent_uid")
    if parent is not None:
        if not isinstance(parent, str) or not parent or len(parent) > 200 or any(ord(c) < 32 for c in parent):
            raise DataError("INVALID_PARENT_FILTER")
        rows = [row for row in rows if row.get("parent_uid") == parent]
    query = params.get("query", "")
    if not isinstance(query, str) or len(query) > 500:
        raise DataError("INVALID_EXPORT_QUERY")
    query = query.strip().lower()
    rows = [row for row in rows if not query or any(query in str(row.get(key) or "").lower() for key in ("name", "code"))]
    return sort_rows(rows, params.get("sort_key"), params.get("sort_direction", "default"),
                     page=params.get("page", "fusion"), view=params.get("view", "detail"))
