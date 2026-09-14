"""Frozen Tushare data acquisition for SWIVD v2."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .io_utils import atomic_write_bytes, sha256_bytes
from .tushare_client import ENDPOINT_FIELDS, SecureTushareClient, decode_rows
from .v2_domain import LEVELS, V2DataError, validate_membership_primary_keys


Progress = Callable[[str, int, int, str], None]
LIVE_PROVIDER_KIND = "LIVE_SECURE_TUSHARE"
TEST_PROVIDER_KIND = "TEST_INJECTED_CLIENT"
LIVE_VALIDATION_STATE = "PASS"
TEST_VALIDATION_STATE = "NOT_LIVE_TEST_PROVIDER"
LIVE_VALIDATION_REASON = "SECURE_TUSHARE_CLIENT"
TEST_VALIDATION_REASON = "EXPLICIT_CLIENT_INJECTION"
LIVE_ARTIFACT_PUBLISH_STATE = "LOCAL_RESEARCH_CANDIDATE_COMPLETE"
TEST_ARTIFACT_PUBLISH_STATE = "LOCAL_TEST_PROVIDER_COMPLETE"
LIVE_REQUEST_TRANSPORT = "HTTPS_NO_REDIRECT"
TEST_REQUEST_TRANSPORT = "INJECTED_TEST_CLIENT"


def _raw_member_keys(rows: Sequence[Mapping[str, Any]]) -> set[tuple[str, ...]]:
    fields = ENDPOINT_FIELDS["index_member_all"]
    return {
        tuple("" if row.get(field) is None else str(row.get(field)) for field in fields)
        for row in rows
    }


class V2TushareProvider:
    """One allow-listed provider whose every material response is frozen."""

    def __init__(
        self,
        *,
        raw_root: Path,
        request_log: list[dict[str, Any]],
        client: SecureTushareClient | None = None,
        progress: Progress | None = None,
    ) -> None:
        self.raw_root = raw_root
        self.request_log = request_log
        self.provider_kind = (
            TEST_PROVIDER_KIND if client is not None else LIVE_PROVIDER_KIND
        )
        self.client = client if client is not None else SecureTushareClient()
        self.progress = progress or (lambda phase, completed, total, item: None)

    def _fetch(
        self,
        api_name: str,
        params: Mapping[str, Any],
        *,
        relative: str,
        row_limit: int,
        permit_limit_touch: bool = False,
    ) -> list[dict[str, Any]]:
        response = self.client.call(api_name, params, ENDPOINT_FIELDS[api_name])
        target = self.raw_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        raw_bytes = response.raw_bytes
        atomic_write_bytes(target, raw_bytes)
        rows = decode_rows(
            response,
            api_name=api_name,
            expected_fields=ENDPOINT_FIELDS[api_name],
            row_limit=None,
        )
        if len(rows) > row_limit or (len(rows) == row_limit and not permit_limit_touch):
            raise V2DataError("ENDPOINT_ROW_LIMIT", f"{api_name} may be truncated")
        self.request_log.append(
            {
                "api_name": api_name,
                "params": dict(params),
                "fields": list(ENDPOINT_FIELDS[api_name]),
                "row_count": len(rows),
                "row_limit": row_limit,
                "raw_path": target.relative_to(self.raw_root.parent.parent).as_posix(),
                "raw_sha256": sha256_bytes(raw_bytes),
                "attempt_count": response.attempt_count,
                "transport": (
                    TEST_REQUEST_TRANSPORT
                    if self.provider_kind == TEST_PROVIDER_KIND
                    else LIVE_REQUEST_TRANSPORT
                ),
                "provider_kind": self.provider_kind,
            }
        )
        return rows

    def trade_calendar(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        return self._fetch(
            "trade_cal",
            {"exchange": "SSE", "start_date": start_date, "end_date": end_date},
            relative=f"trade_cal/SSE_{start_date}_{end_date}.json",
            row_limit=6000,
        )

    def classifications(self) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for position, level in enumerate(LEVELS, start=1):
            self.progress("CLASSIFICATION", position - 1, len(LEVELS), level)
            result[level] = self._fetch(
                "index_classify",
                {"level": level, "src": "SW2021"},
                relative=f"index_classify/SW2021_{level}.json",
                row_limit=2000,
            )
            self.progress("CLASSIFICATION", position, len(LEVELS), level)
        return result

    def industry_history(
        self,
        *,
        codes: Sequence[str],
        start_date: str,
        end_date: str,
        level: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        total = len(codes)
        for position, code in enumerate(sorted(codes), start=1):
            self.progress("INDUSTRY_DATA", position - 1, total, f"{level}:{code}")
            safe = code.replace(".", "_")
            part = self._fetch(
                "sw_daily",
                {"ts_code": code, "start_date": start_date, "end_date": end_date},
                relative=f"sw_daily/{level}/{safe}_{start_date}_{end_date}.json",
                row_limit=4000,
            )
            if {str(row.get("ts_code")) for row in part} != {code}:
                raise V2DataError("INDUSTRY_CODE_MISMATCH", f"sw_daily({code}) identity drift")
            rows.extend(part)
            self.progress("INDUSTRY_DATA", position, total, f"{level}:{code}")
        return rows

    def identity_histories(
        self,
        *,
        codes: Sequence[str],
        start_date: str,
        end_date: str,
    ) -> dict[str, list[dict[str, Any]]]:
        """Freeze exact candidate-code histories, including legitimate empties.

        The ordinary history method requires every requested published code to
        return rows.  Identity resolution instead needs to distinguish an empty
        catalog code from a populated endpoint alias, while still rejecting any
        row whose returned ``ts_code`` differs from the requested candidate.
        """

        result: dict[str, list[dict[str, Any]]] = {}
        ordered = sorted({str(code) for code in codes})
        if len(ordered) != len(codes):
            raise V2DataError("IDENTITY_CANDIDATE_DUPLICATE", "identity codes must be unique")
        for position, code in enumerate(ordered, start=1):
            self.progress(
                "INDUSTRY_DATA",
                position - 1,
                len(ordered),
                f"IDENTITY:{code}",
            )
            safe = code.replace(".", "_")
            rows = self._fetch(
                "sw_daily",
                {"ts_code": code, "start_date": start_date, "end_date": end_date},
                relative=(
                    "identity_resolution/sw_daily/"
                    f"{safe}_{start_date}_{end_date}.json"
                ),
                row_limit=4000,
            )
            returned = {str(row.get("ts_code")) for row in rows}
            if returned not in (set(), {code}):
                raise V2DataError(
                    "INDUSTRY_CODE_MISMATCH",
                    f"identity sw_daily({code}) returned another code",
                )
            result[code] = rows
            self.progress("INDUSTRY_DATA", position, len(ordered), f"IDENTITY:{code}")
        return result

    def industry_day(self, trade_date: str) -> list[dict[str, Any]]:
        return self._fetch(
            "sw_daily",
            {"trade_date": trade_date},
            relative=f"sw_daily/by_date/{trade_date}.json",
            row_limit=4000,
        )

    def _member_query(
        self,
        *,
        selector: str,
        code: str,
        is_new: str,
        round_number: int,
    ) -> list[dict[str, Any]]:
        safe = code.replace(".", "_")
        rows = self._fetch(
            "index_member_all",
            {selector: code, "is_new": is_new},
            relative=f"index_member_all/round_{round_number}/{is_new}/{selector}/{safe}.json",
            row_limit=2000,
            permit_limit_touch=True,
        )
        if any(
            str(row.get(selector)) != code or str(row.get("is_new")) != is_new
            for row in rows
        ):
            raise V2DataError(
                "MEMBER_QUERY_SCOPE_MISMATCH",
                f"{selector} response differs from its query selector/state",
            )
        return [dict(row) for row in validate_membership_primary_keys(rows)]

    def identity_membership_round(
        self,
        *,
        codes: Sequence[str],
        round_number: int,
    ) -> dict[str, dict[str, list[dict[str, Any]]]]:
        """Query both Y/N states for exact identity candidates in one round.

        These paths are deliberately separate from the normal L1/L2/L3 batch
        acquisition so the independent validator can compare the direct probe
        with the filtered main response without raw files overwriting each
        other.
        """

        ordered = sorted({str(code) for code in codes})
        if len(ordered) != len(codes):
            raise V2DataError("IDENTITY_CANDIDATE_DUPLICATE", "identity codes must be unique")
        result: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for code in ordered:
            safe = code.replace(".", "_")
            states: dict[str, list[dict[str, Any]]] = {}
            for is_new in ("Y", "N"):
                rows = self._fetch(
                    "index_member_all",
                    {"l3_code": code, "is_new": is_new},
                    relative=(
                        "identity_resolution/index_member_all/"
                        f"round_{round_number}/{is_new}/{safe}.json"
                    ),
                    row_limit=2000,
                    permit_limit_touch=True,
                )
                if len(rows) >= 2000:
                    raise V2DataError(
                        "MEMBER_ROW_LIMIT",
                        f"identity candidate {code} touches the 2000-row limit",
                    )
                returned = {str(row.get("l3_code")) for row in rows}
                if returned not in (set(), {code}):
                    raise V2DataError(
                        "MEMBER_PATH_MISMATCH",
                        f"identity membership({code}) returned another L3 code",
                    )
                if any(str(row.get("is_new")) != is_new for row in rows):
                    raise V2DataError(
                        "MEMBER_QUERY_SCOPE_MISMATCH",
                        "identity membership response differs from its query state",
                    )
                states[is_new] = [
                    dict(row) for row in validate_membership_primary_keys(rows)
                ]
            result[code] = states
        return result

    def membership_round(
        self,
        *,
        classifications: Mapping[str, Sequence[Mapping[str, Any]]],
        round_number: int,
    ) -> list[dict[str, Any]]:
        """Fetch Y/N by L1, splitting exact-limit batches to L2 then L3."""

        l2_children: dict[str, list[Mapping[str, Any]]] = {}
        for l1 in classifications["L1"]:
            l2_children[str(l1["industry_code"])] = [
                row for row in classifications["L2"] if row["parent_code"] == l1["industry_code"]
            ]
        l3_children: dict[str, list[Mapping[str, Any]]] = {}
        for l2 in classifications["L2"]:
            l3_children[str(l2["industry_code"])] = [
                row for row in classifications["L3"] if row["parent_code"] == l2["industry_code"]
            ]

        result: list[dict[str, Any]] = []
        work = 2 * len(classifications["L1"])
        completed = 0
        for is_new in ("Y", "N"):
            for l1 in classifications["L1"]:
                l1_code = str(l1["index_code"])
                self.progress("MEMBERSHIP", completed, work, f"R{round_number}:{is_new}:{l1_code}")
                rows = self._member_query(
                    selector="l1_code",
                    code=l1_code,
                    is_new=is_new,
                    round_number=round_number,
                )
                if len(rows) < 2000:
                    result.extend(rows)
                else:
                    children = l2_children.get(str(l1["industry_code"]), [])
                    if not children:
                        raise V2DataError("MEMBER_SPLIT_MISSING", "L1 limit has no L2 children")
                    split_rows: list[dict[str, Any]] = []
                    for l2 in children:
                        l2_code = str(l2["index_code"])
                        sub = self._member_query(
                            selector="l2_code",
                            code=l2_code,
                            is_new=is_new,
                            round_number=round_number,
                        )
                        if len(sub) < 2000:
                            split_rows.extend(sub)
                        else:
                            grandchildren = l3_children.get(str(l2["industry_code"]), [])
                            if not grandchildren:
                                raise V2DataError(
                                    "MEMBER_SPLIT_MISSING", "L2 limit has no L3 children"
                                )
                            leaf_rows: list[dict[str, Any]] = []
                            for l3 in grandchildren:
                                l3_code = str(l3["index_code"])
                                leaf = self._member_query(
                                    selector="l3_code",
                                    code=l3_code,
                                    is_new=is_new,
                                    round_number=round_number,
                                )
                                if len(leaf) >= 2000:
                                    raise V2DataError(
                                        "MEMBER_ROW_LIMIT", f"{l3_code} touches the 2000-row limit"
                                    )
                                leaf_rows.extend(leaf)
                            if not _raw_member_keys(sub).issubset(
                                _raw_member_keys(leaf_rows)
                            ):
                                raise V2DataError(
                                    "MEMBER_SPLIT_INCOMPLETE",
                                    f"{l2_code} parent rows are missing from L3 union",
                                )
                            split_rows.extend(leaf_rows)
                    if not _raw_member_keys(rows).issubset(
                        _raw_member_keys(split_rows)
                    ):
                        raise V2DataError(
                            "MEMBER_SPLIT_INCOMPLETE",
                            f"{l1_code} parent rows are missing from L2/L3 union",
                        )
                    result.extend(split_rows)
                completed += 1
                self.progress("MEMBERSHIP", completed, work, f"R{round_number}:{is_new}:{l1_code}")
        return [dict(row) for row in validate_membership_primary_keys(result)]

    def daily_basic(self, trade_date: str) -> list[dict[str, Any]]:
        self.progress("STOCK_VALUATION", 0, 1, trade_date)
        rows = self._fetch(
            "daily_basic",
            {"trade_date": trade_date},
            relative=f"daily_basic/{trade_date}.json",
            row_limit=6000,
        )
        self.progress("STOCK_VALUATION", 1, 1, trade_date)
        return rows


__all__ = [
    "LIVE_ARTIFACT_PUBLISH_STATE",
    "LIVE_PROVIDER_KIND",
    "LIVE_REQUEST_TRANSPORT",
    "LIVE_VALIDATION_REASON",
    "LIVE_VALIDATION_STATE",
    "TEST_ARTIFACT_PUBLISH_STATE",
    "TEST_PROVIDER_KIND",
    "TEST_REQUEST_TRANSPORT",
    "TEST_VALIDATION_REASON",
    "TEST_VALIDATION_STATE",
    "V2TushareProvider",
]
