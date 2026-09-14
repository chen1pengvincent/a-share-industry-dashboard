"""Evidence-gated identity resolution for the SW2021 special-steel anomaly.

The upstream ``index_classify``, ``sw_daily`` and ``index_member_all`` APIs do
not currently agree on the L3 index code for SW2021 industry ``230501``.  This
module keeps those three facts separate.  It contains no network or filesystem
code: callers must first freeze the raw responses, then pass decoded rows here.

Names are rejection gates only.  They are deliberately never used as join
keys, because a same-name row is not proof that two upstream identities are
equivalent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from .v2_domain import normalize_members


IDENTITY_SCHEMA_VERSION = "swivd-industry-identity-resolution-v1"
IDENTITY_RULE_VERSION = "swivd-special-steel-identity-v1"
SPECIAL_STEEL_INDUSTRY_UID = "SW2021:L3:230501"
SPECIAL_STEEL_INDUSTRY_CODE = "230501"
SPECIAL_STEEL_INDUSTRY_NAME = "特钢Ⅲ"
SPECIAL_STEEL_PARENT_INDUSTRY_CODE = "230500"
SPECIAL_STEEL_CANDIDATE_CODES = ("850401.SI", "850412.SI")

# Concise aliases are the public names used by the provider/pipeline contract.
IDENTITY_CANDIDATE_CODES = SPECIAL_STEEL_CANDIDATE_CODES
SPECIAL_INDUSTRY_UID = SPECIAL_STEEL_INDUSTRY_UID

SPECIAL_STEEL_MEMBER_PATH = {
    "l1_code": "801040.SI",
    "l1_name": "钢铁",
    "l2_code": "801045.SI",
    "l2_name": "特钢Ⅱ",
    "l3_name": SPECIAL_STEEL_INDUSTRY_NAME,
}

IDENTITY_STATES = (
    "DIRECT",
    "EVIDENCE_GATED_ALIAS",
    "EVIDENCE_GATED_TRANSITION",
)

# This is the exact normalized classification projection owned by this module.
# The pipeline can use the constant instead of independently recreating an
# order that could drift from :func:`project_classification`.
PROJECTED_CLASSIFICATION_FIELDS = (
    "src",
    "level",
    "index_code",
    "catalog_index_code",
    "quote_index_code",
    "member_index_code",
    "industry_uid",
    "industry_name",
    "industry_code",
    "parent_code",
    "is_pub",
    "identity_state",
    "identity_rule",
    "identity_rule_version",
)

_MEMBER_FIELDS = (
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


class IdentityResolutionError(ValueError):
    """Fail-closed identity error with a stable, non-secret machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _fail(code: str, message: str) -> None:
    raise IdentityResolutionError(code, message)


def _text(value: Any, field: str, *, allow_blank: bool = False) -> str:
    rendered = "" if value is None else str(value).strip()
    if not allow_blank and not rendered:
        _fail("IDENTITY_SCHEMA_MISMATCH", f"{field} is blank")
    if "\x00" in rendered or "\r" in rendered or "\n" in rendered:
        _fail("IDENTITY_SCHEMA_MISMATCH", f"{field} contains forbidden characters")
    return rendered


def _date(value: Any, field: str) -> str:
    rendered = _text(value, field)
    if len(rendered) != 8 or not rendered.isdigit():
        _fail("IDENTITY_INVALID_DATE", f"{field} must be YYYYMMDD")
    try:
        datetime.strptime(rendered, "%Y%m%d")
    except ValueError:
        _fail("IDENTITY_INVALID_DATE", f"{field} is not a calendar date")
    return rendered


def make_industry_uid(src: Any, level: Any, industry_code: Any) -> str:
    """Return the stable taxonomy identity; never use a display name to join."""

    return f"{_text(src, 'src')}:{_text(level, 'level')}:{_text(industry_code, 'industry_code')}"


@dataclass(frozen=True)
class IdentityInterval:
    """One contiguous open-day interval served by one upstream quote code."""

    start_date: str
    end_date: str
    source_ts_code: str
    row_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_date": self.start_date,
            "end_date": self.end_date,
            "source_ts_code": self.source_ts_code,
            "row_count": self.row_count,
        }


@dataclass(frozen=True)
class QuoteIdentityResolution:
    """Classification and quote evidence before membership is admitted."""

    industry_uid: str
    catalog_index_code: str
    quote_index_code: str
    identity_state: str
    identity_rule: str
    target_trade_date: str
    open_date_count: int
    intervals: tuple[IdentityInterval, ...]
    evidence: tuple[tuple[str, int | str], ...]

    @property
    def index_code(self) -> str:
        return self.quote_index_code

    @property
    def current_index_code(self) -> str:
        return self.quote_index_code

    def source_code_on(self, trade_date: str) -> str:
        target = _date(trade_date, "trade_date")
        matches = [
            interval.source_ts_code
            for interval in self.intervals
            if interval.start_date <= target <= interval.end_date
        ]
        if len(matches) != 1:
            _fail(
                "IDENTITY_QUOTE_DATE_OUTSIDE_EVIDENCE",
                "trade_date is outside the resolved quote intervals",
            )
        return matches[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": IDENTITY_SCHEMA_VERSION,
            "rule_version": IDENTITY_RULE_VERSION,
            "industry_uid": self.industry_uid,
            "src": "SW2021",
            "level": "L3",
            "industry_code": SPECIAL_STEEL_INDUSTRY_CODE,
            "industry_name": SPECIAL_STEEL_INDUSTRY_NAME,
            "catalog_index_code": self.catalog_index_code,
            "index_code": self.index_code,
            "current_index_code": self.current_index_code,
            "quote_index_code": self.quote_index_code,
            "member_index_code": None,
            "identity_state": self.identity_state,
            "identity_rule": self.identity_rule,
            "target_trade_date": self.target_trade_date,
            "open_date_count": self.open_date_count,
            "intervals": [interval.to_dict() for interval in self.intervals],
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class IndustryIdentityResolution:
    """Fully admitted endpoint-aware identity shared by pipeline and validator."""

    industry_uid: str
    catalog_index_code: str
    quote_index_code: str
    member_index_code: str
    identity_state: str
    identity_rule: str
    target_trade_date: str
    open_date_count: int
    intervals: tuple[IdentityInterval, ...]
    evidence: tuple[tuple[str, int | str], ...]

    @property
    def index_code(self) -> str:
        return self.quote_index_code

    @property
    def current_index_code(self) -> str:
        return self.quote_index_code

    def source_code_on(self, trade_date: str) -> str:
        quote = QuoteIdentityResolution(
            industry_uid=self.industry_uid,
            catalog_index_code=self.catalog_index_code,
            quote_index_code=self.quote_index_code,
            identity_state=self.identity_state,
            identity_rule=self.identity_rule,
            target_trade_date=self.target_trade_date,
            open_date_count=self.open_date_count,
            intervals=self.intervals,
            evidence=(),
        )
        return quote.source_code_on(trade_date)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": IDENTITY_SCHEMA_VERSION,
            "rule_version": IDENTITY_RULE_VERSION,
            "industry_uid": self.industry_uid,
            "src": "SW2021",
            "level": "L3",
            "industry_code": SPECIAL_STEEL_INDUSTRY_CODE,
            "industry_name": SPECIAL_STEEL_INDUSTRY_NAME,
            "catalog_index_code": self.catalog_index_code,
            "index_code": self.index_code,
            "current_index_code": self.current_index_code,
            "quote_index_code": self.quote_index_code,
            "member_index_code": self.member_index_code,
            "identity_state": self.identity_state,
            "identity_rule": self.identity_rule,
            "target_trade_date": self.target_trade_date,
            "open_date_count": self.open_date_count,
            "intervals": [interval.to_dict() for interval in self.intervals],
            "evidence": dict(self.evidence),
        }


def _resolve_catalog(classification_rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    target_rows = [
        row
        for row in classification_rows
        if str(row.get("src", "")).strip() == "SW2021"
        and str(row.get("level", "")).strip() == "L3"
        and str(row.get("industry_code", "")).strip() == SPECIAL_STEEL_INDUSTRY_CODE
    ]
    if len(target_rows) != 1:
        _fail(
            "IDENTITY_CATALOG_SINGLETON_REQUIRED",
            f"industry 230501 expected one catalog row, got {len(target_rows)}",
        )
    target = target_rows[0]
    catalog_code = _text(target.get("index_code"), "index_code")
    if catalog_code not in SPECIAL_STEEL_CANDIDATE_CODES:
        _fail("IDENTITY_CATALOG_CODE_UNEXPECTED", "230501 has an unapproved catalog code")
    if _text(target.get("industry_name"), "industry_name") != SPECIAL_STEEL_INDUSTRY_NAME:
        _fail("IDENTITY_NAME_MISMATCH", "230501 catalog name is not 特钢Ⅲ")
    if _text(target.get("parent_code"), "parent_code") != SPECIAL_STEEL_PARENT_INDUSTRY_CODE:
        _fail("IDENTITY_CATALOG_PATH_MISMATCH", "230501 parent is not 230500")
    if str(target.get("is_pub", "")).strip() != "1":
        _fail("IDENTITY_CATALOG_PUBLICATION_MISMATCH", "230501 is not published")

    expected_parents = (
        {
            "level": "L1",
            "industry_code": "230000",
            "industry_name": "钢铁",
            "parent_code": "0",
            "index_code": SPECIAL_STEEL_MEMBER_PATH["l1_code"],
        },
        {
            "level": "L2",
            "industry_code": SPECIAL_STEEL_PARENT_INDUSTRY_CODE,
            "industry_name": "特钢Ⅱ",
            "parent_code": "230000",
            "index_code": SPECIAL_STEEL_MEMBER_PATH["l2_code"],
        },
    )
    for expected in expected_parents:
        matches = [
            row
            for row in classification_rows
            if str(row.get("src", "")).strip() == "SW2021"
            and str(row.get("level", "")).strip() == expected["level"]
            and str(row.get("industry_code", "")).strip()
            == expected["industry_code"]
        ]
        if len(matches) != 1:
            _fail(
                "IDENTITY_CATALOG_PATH_MISMATCH",
                f"{expected['level']} parent is not a singleton",
            )
        parent = matches[0]
        for field in ("industry_name", "parent_code", "index_code"):
            if str(parent.get(field, "")).strip() != expected[field]:
                _fail(
                    "IDENTITY_CATALOG_PATH_MISMATCH",
                    f"{expected['level']} parent differs at {field}",
                )

    candidate_owners: dict[str, list[str]] = {code: [] for code in SPECIAL_STEEL_CANDIDATE_CODES}
    for row in classification_rows:
        code = str(row.get("index_code", "")).strip()
        if code in candidate_owners:
            owner = make_industry_uid(row.get("src"), row.get("level"), row.get("industry_code"))
            candidate_owners[code].append(owner)
    for code, owners in candidate_owners.items():
        if len(owners) > 1 or (owners and owners != [SPECIAL_STEEL_INDUSTRY_UID]):
            _fail("IDENTITY_CATALOG_COLLISION", f"candidate code {code} has another owner")
    return target


def _canonical_open_dates(open_dates: Sequence[Any], target_trade_date: str) -> tuple[str, ...]:
    dates = tuple(_date(value, "open_date") for value in open_dates)
    if not dates:
        _fail("IDENTITY_OPEN_DATES_EMPTY", "quote identity requires at least one open date")
    if len(set(dates)) != len(dates):
        _fail("IDENTITY_OPEN_DATE_DUPLICATE", "open_dates contains a duplicate")
    if tuple(sorted(dates)) != dates:
        _fail("IDENTITY_OPEN_DATES_UNSORTED", "open_dates must be strictly ascending")
    if dates[-1] != target_trade_date:
        _fail("IDENTITY_TARGET_NOT_LATEST_OPEN_DATE", "target must equal the final open date")
    return dates


def _quote_identity(row: Mapping[str, Any], expected_code: str | None = None) -> tuple[str, str, str]:
    code = _text(row.get("ts_code"), "ts_code")
    trade_date = _date(row.get("trade_date"), "trade_date")
    name = _text(row.get("name"), "name")
    if code not in SPECIAL_STEEL_CANDIDATE_CODES or (expected_code and code != expected_code):
        _fail("IDENTITY_QUOTE_CODE_MISMATCH", "quote row has an unexpected candidate code")
    if name != SPECIAL_STEEL_INDUSTRY_NAME:
        _fail("IDENTITY_NAME_MISMATCH", "candidate quote name is not 特钢Ⅲ")
    return code, trade_date, name


def _compress_intervals(
    open_dates: Sequence[str], code_by_date: Mapping[str, str]
) -> tuple[IdentityInterval, ...]:
    intervals: list[IdentityInterval] = []
    start = open_dates[0]
    previous = start
    code = code_by_date[start]
    count = 1
    for trade_date in open_dates[1:]:
        current = code_by_date[trade_date]
        if current == code:
            previous = trade_date
            count += 1
            continue
        intervals.append(IdentityInterval(start, previous, code, count))
        start = previous = trade_date
        code = current
        count = 1
    intervals.append(IdentityInterval(start, previous, code, count))
    return tuple(intervals)


def resolve_quote_identity(
    classification_rows: Sequence[Mapping[str, Any]],
    target_day_rows: Sequence[Mapping[str, Any]],
    histories_by_code: Mapping[str, Sequence[Mapping[str, Any]]],
    open_dates: Sequence[Any],
    *,
    target_trade_date: str,
) -> QuoteIdentityResolution:
    """Resolve catalog/quote identity only after all exact evidence gates pass.

    ``histories_by_code`` must contain both candidate keys, even when one query
    returned zero rows.  For every supplied open date, exactly one candidate
    must have exactly one row.  A single code switch is admitted as a dated
    transition; a second switch is treated as oscillation and fails closed.
    """

    target = _date(target_trade_date, "target_trade_date")
    dates = _canonical_open_dates(open_dates, target)
    catalog = _resolve_catalog(classification_rows)
    catalog_code = _text(catalog.get("index_code"), "index_code")
    if set(histories_by_code) != set(SPECIAL_STEEL_CANDIDATE_CODES):
        _fail(
            "IDENTITY_DUAL_HISTORY_REQUIRED",
            "histories must contain exactly the two approved candidate queries",
        )

    code_by_date: dict[str, str] = {}
    history_row_by_date: dict[str, Mapping[str, Any]] = {}
    history_rows = 0
    for expected_code in SPECIAL_STEEL_CANDIDATE_CODES:
        per_code_seen: set[str] = set()
        for row in histories_by_code[expected_code]:
            code, trade_date, _ = _quote_identity(row, expected_code)
            if trade_date not in dates:
                _fail("IDENTITY_HISTORY_DATE_OUT_OF_RANGE", "history row is outside open_dates")
            if trade_date in per_code_seen:
                _fail("IDENTITY_HISTORY_DUPLICATE", "one candidate has duplicate date rows")
            per_code_seen.add(trade_date)
            if trade_date in code_by_date:
                _fail("IDENTITY_HISTORY_OVERLAP", "both candidate codes exist on one open date")
            code_by_date[trade_date] = code
            history_row_by_date[trade_date] = row
            history_rows += 1
    missing = [trade_date for trade_date in dates if trade_date not in code_by_date]
    if missing:
        _fail("IDENTITY_HISTORY_GAP", f"candidate histories miss {len(missing)} open dates")

    target_candidates: list[tuple[str, str, str]] = []
    for row in target_day_rows:
        code = str(row.get("ts_code", "")).strip()
        name = str(row.get("name", "")).strip()
        if name == SPECIAL_STEEL_INDUSTRY_NAME and code not in SPECIAL_STEEL_CANDIDATE_CODES:
            _fail(
                "IDENTITY_SECOND_ALIAS",
                "target day contains an unapproved same-name quote code",
            )
        if code not in SPECIAL_STEEL_CANDIDATE_CODES:
            continue
        target_candidates.append(_quote_identity(row))
    if len(target_candidates) != 1:
        _fail(
            "IDENTITY_TARGET_DAY_SINGLETON_REQUIRED",
            f"target day expected one candidate row, got {len(target_candidates)}",
        )
    target_code, target_row_date, _ = target_candidates[0]
    if target_row_date != target:
        _fail("IDENTITY_TARGET_DAY_DATE_MISMATCH", "candidate target row has another date")
    if code_by_date[target] != target_code:
        _fail("IDENTITY_TARGET_HISTORY_MISMATCH", "target-day and history codes disagree")
    if dict(history_row_by_date[target]) != dict(
        next(
            row
            for row in target_day_rows
            if str(row.get("ts_code", "")).strip() == target_code
        )
    ):
        _fail(
            "IDENTITY_TARGET_HISTORY_ROW_MISMATCH",
            "target-day and per-code history rows differ",
        )

    intervals = _compress_intervals(dates, code_by_date)
    if len(intervals) > 2:
        _fail("IDENTITY_HISTORY_OSCILLATION", "candidate quote code switches more than once")
    if len(intervals) == 2:
        state = "EVIDENCE_GATED_TRANSITION"
        rule = "ONE_CONTIGUOUS_OPEN_DAY_CODE_TRANSITION"
    elif target_code == catalog_code:
        state = "DIRECT"
        rule = "CATALOG_EQUALS_SINGLE_CONTINUOUS_QUOTE_CODE"
    else:
        state = "EVIDENCE_GATED_ALIAS"
        rule = "CATALOG_ALIASED_TO_SINGLE_CONTINUOUS_QUOTE_CODE"
    evidence = tuple(
        sorted(
            {
                "catalog_match_count": 1,
                "target_day_candidate_count": 1,
                "history_candidate_row_count": history_rows,
                "history_covered_open_date_count": len(code_by_date),
                "history_interval_count": len(intervals),
            }.items()
        )
    )
    return QuoteIdentityResolution(
        industry_uid=SPECIAL_STEEL_INDUSTRY_UID,
        catalog_index_code=catalog_code,
        quote_index_code=target_code,
        identity_state=state,
        identity_rule=rule,
        target_trade_date=target,
        open_date_count=len(dates),
        intervals=intervals,
        evidence=evidence,
    )


def _require_rounds(value: Mapping[Any, Any], label: str) -> None:
    if set(value) != {1, 2}:
        _fail("IDENTITY_MEMBER_ROUNDS_REQUIRED", f"{label} must contain rounds 1 and 2")


def _canonical_member(
    source: Mapping[str, Any], *, expected_code: str, expected_is_new: str
) -> tuple[str, ...]:
    missing = [field for field in _MEMBER_FIELDS if field not in source]
    if missing:
        _fail("IDENTITY_MEMBER_SCHEMA_MISMATCH", "member row fields drifted")
    row = {
        field: _text(source.get(field), field, allow_blank=field == "out_date")
        for field in _MEMBER_FIELDS
    }
    _date(row["in_date"], "in_date")
    if row["out_date"]:
        _date(row["out_date"], "out_date")
    if row["l3_code"] != expected_code:
        _fail("IDENTITY_MEMBER_CODE_MISMATCH", "member row differs from its query code")
    if row["is_new"] != expected_is_new:
        _fail("IDENTITY_MEMBER_STATE_MISMATCH", "member row differs from its Y/N query")
    for field, expected in SPECIAL_STEEL_MEMBER_PATH.items():
        if row[field] != expected:
            _fail("IDENTITY_MEMBER_PATH_MISMATCH", f"member path differs at {field}")
    if row["is_new"] == "Y" and row["out_date"]:
        _fail("IDENTITY_MEMBER_STATE_MISMATCH", "current member has an out_date")
    if row["is_new"] == "N" and not row["out_date"]:
        _fail("IDENTITY_MEMBER_STATE_MISMATCH", "historical member lacks out_date")
    if row["out_date"] and row["in_date"] > row["out_date"]:
        _fail("IDENTITY_MEMBER_INTERVAL_INVALID", "in_date is after out_date")
    return tuple(row[field] for field in _MEMBER_FIELDS)


def _canonical_member_query(
    rows: Iterable[Mapping[str, Any]], *, expected_code: str, expected_is_new: str
) -> tuple[tuple[str, ...], ...]:
    normalized = [
        _canonical_member(row, expected_code=expected_code, expected_is_new=expected_is_new)
        for row in rows
    ]
    if len(set(normalized)) != len(normalized):
        _fail("IDENTITY_MEMBER_DUPLICATE", "member query contains duplicate rows")
    return tuple(sorted(normalized))


def _candidate_queries(
    candidate_rounds: Mapping[int, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]]
) -> dict[int, dict[str, dict[str, tuple[tuple[str, ...], ...]]]]:
    _require_rounds(candidate_rounds, "candidate_rounds")
    result: dict[int, dict[str, dict[str, tuple[tuple[str, ...], ...]]]] = {}
    for round_number in (1, 2):
        by_code = candidate_rounds[round_number]
        if set(by_code) != set(SPECIAL_STEEL_CANDIDATE_CODES):
            _fail("IDENTITY_MEMBER_DUAL_CODE_REQUIRED", "each round must query both codes")
        result[round_number] = {}
        for code in SPECIAL_STEEL_CANDIDATE_CODES:
            by_state = by_code[code]
            if set(by_state) != {"Y", "N"}:
                _fail("IDENTITY_MEMBER_YN_REQUIRED", "each code must include Y and N queries")
            result[round_number][code] = {
                state: _canonical_member_query(
                    by_state[state], expected_code=code, expected_is_new=state
                )
                for state in ("Y", "N")
            }
    if result[1] != result[2]:
        _fail("IDENTITY_MEMBER_ROUNDS_DRIFT", "candidate member rounds are not stable")
    return result


def _l1_filtered_queries(
    l1_rounds: Mapping[int, Mapping[str, Sequence[Mapping[str, Any]]]]
) -> dict[int, dict[str, dict[str, tuple[tuple[str, ...], ...]]]]:
    _require_rounds(l1_rounds, "l1_rounds")
    result: dict[int, dict[str, dict[str, tuple[tuple[str, ...], ...]]]] = {}
    for round_number in (1, 2):
        by_state = l1_rounds[round_number]
        if set(by_state) != {"Y", "N"}:
            _fail("IDENTITY_MEMBER_L1_YN_REQUIRED", "each L1 round must include Y and N")
        result[round_number] = {}
        for code in SPECIAL_STEEL_CANDIDATE_CODES:
            result[round_number][code] = {}
            for state in ("Y", "N"):
                filtered = [
                    row
                    for row in by_state[state]
                    if str(row.get("l3_code", "")).strip() == code
                ]
                result[round_number][code][state] = _canonical_member_query(
                    filtered, expected_code=code, expected_is_new=state
                )
    if result[1] != result[2]:
        _fail("IDENTITY_MEMBER_L1_ROUNDS_DRIFT", "filtered L1 member rounds are not stable")
    return result


def _as_of_member_code(
    candidate: Mapping[str, Mapping[str, Sequence[tuple[str, ...]]]],
    target: str,
) -> tuple[str, int]:
    """Resolve dated membership evidence without using today's Y as history.

    Boundary rows remain candidates.  One candidate code lets the downstream
    domain retain its boundary-unknown state; candidates spanning both codes
    cannot establish an identity and must never be resolved by picking one.
    """

    rows = [
        dict(zip(_MEMBER_FIELDS, row, strict=True))
        for code in SPECIAL_STEEL_CANDIDATE_CODES
        for state in ("Y", "N")
        for row in candidate[code][state]
    ]
    paths = {
        code: {
            **SPECIAL_STEEL_MEMBER_PATH,
            "l3_code": code,
            "industry_uid": SPECIAL_STEEL_INDUSTRY_UID,
            "identity_rule_version": IDENTITY_RULE_VERSION,
        }
        for code in SPECIAL_STEEL_CANDIDATE_CODES
    }
    episodes = normalize_members(rows, l3_paths=paths)
    selected = [
        row
        for row in episodes
        if row["in_date"] <= target
        and (not row["out_date"] or target <= row["out_date"])
    ]
    codes = {row["l3_code"] for row in selected}
    if not codes:
        _fail("IDENTITY_MEMBER_AS_OF_MISSING", "target date has no member candidates")
    if len(codes) > 1:
        boundary = any(
            row["in_date"] == target or row["out_date"] == target for row in selected
        )
        _fail(
            "IDENTITY_MEMBER_AS_OF_BOUNDARY_UNKNOWN"
            if boundary
            else "IDENTITY_MEMBER_DUAL_AS_OF",
            "target-date member candidates span both identity codes",
        )
    return next(iter(codes)), len(selected)


def resolve_membership_identity(
    quote_resolution: QuoteIdentityResolution,
    candidate_rounds: Mapping[
        int, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]
    ],
    l1_rounds: Mapping[int, Mapping[str, Sequence[Mapping[str, Any]]]],
    *,
    membership_as_of: bool = False,
) -> IndustryIdentityResolution:
    """Admit a member code only when dual-code/two-round evidence is stable.

    Direct L3 candidate queries must equal the same rows filtered from the main
    L1 batches.  Presence of a key is material: an empty query result must be
    represented by an empty sequence, not by an omitted code or Y/N state.
    Historical materialization additionally resolves the code from dated Y/N
    episodes; the endpoint's current Y code remains independent evidence.
    """

    if quote_resolution.industry_uid != SPECIAL_STEEL_INDUSTRY_UID:
        _fail("IDENTITY_RESOLUTION_SCOPE_MISMATCH", "unexpected industry resolution")
    candidate = _candidate_queries(candidate_rounds)
    l1_filtered = _l1_filtered_queries(l1_rounds)
    if candidate != l1_filtered:
        _fail(
            "IDENTITY_MEMBER_L1_COMPARISON_MISMATCH",
            "direct candidate queries differ from the main L1 batch filter",
        )

    current_codes = [
        code
        for code in SPECIAL_STEEL_CANDIDATE_CODES
        if candidate[1][code]["Y"]
    ]
    if len(current_codes) > 1:
        _fail("IDENTITY_MEMBER_DUAL_CURRENT", "both candidate codes have current members")
    if not current_codes:
        _fail("IDENTITY_MEMBER_CURRENT_MISSING", "neither candidate has current members")
    endpoint_current_code = current_codes[0]
    member_code = endpoint_current_code
    as_of_row_count = 0
    if membership_as_of:
        member_code, as_of_row_count = _as_of_member_code(
            candidate[1], quote_resolution.target_trade_date
        )
    if member_code != quote_resolution.quote_index_code:
        _fail(
            "IDENTITY_MEMBER_QUOTE_MISMATCH",
            "reference-date member code and quote code are not synchronized",
        )
    evidence = dict(quote_resolution.evidence)
    evidence.update(
        {
            "member_probe_round_count": 2,
            "member_candidate_query_count": 8,
            "member_l1_query_count": 4,
            "member_current_row_count": len(candidate[1][endpoint_current_code]["Y"]),
            "member_candidate_row_count": sum(
                len(candidate[1][code][state])
                for code in SPECIAL_STEEL_CANDIDATE_CODES
                for state in ("Y", "N")
            ),
        }
    )
    if membership_as_of:
        evidence.update(
            {
                "endpoint_current_member_code": endpoint_current_code,
                "membership_reference_date": quote_resolution.target_trade_date,
                "member_as_of_row_count": as_of_row_count,
            }
        )
    return IndustryIdentityResolution(
        industry_uid=quote_resolution.industry_uid,
        catalog_index_code=quote_resolution.catalog_index_code,
        quote_index_code=quote_resolution.quote_index_code,
        member_index_code=member_code,
        identity_state=quote_resolution.identity_state,
        identity_rule=quote_resolution.identity_rule,
        target_trade_date=quote_resolution.target_trade_date,
        open_date_count=quote_resolution.open_date_count,
        intervals=quote_resolution.intervals,
        evidence=tuple(sorted(evidence.items())),
    )


def project_classification(
    row: Mapping[str, Any], resolution: IndustryIdentityResolution
) -> dict[str, Any]:
    """Project the special catalog row while preserving its upstream code."""

    uid = make_industry_uid(row.get("src"), row.get("level"), row.get("industry_code"))
    if uid != resolution.industry_uid:
        _fail("IDENTITY_PROJECTION_SCOPE_MISMATCH", "classification row is not 230501")
    if _text(row.get("index_code"), "index_code") != resolution.catalog_index_code:
        _fail("IDENTITY_PROJECTION_SOURCE_MISMATCH", "catalog source code has drifted")
    if _text(row.get("industry_name"), "industry_name") != SPECIAL_STEEL_INDUSTRY_NAME:
        _fail("IDENTITY_NAME_MISMATCH", "classification projection name drifted")
    projected = {
        "src": "SW2021",
        "level": "L3",
        "index_code": resolution.index_code,
        "catalog_index_code": resolution.catalog_index_code,
        "quote_index_code": resolution.quote_index_code,
        "member_index_code": resolution.member_index_code,
        "industry_uid": resolution.industry_uid,
        "industry_name": SPECIAL_STEEL_INDUSTRY_NAME,
        "industry_code": SPECIAL_STEEL_INDUSTRY_CODE,
        "parent_code": _text(row.get("parent_code"), "parent_code"),
        "is_pub": row.get("is_pub"),
        "identity_state": resolution.identity_state,
        "identity_rule": resolution.identity_rule,
        "identity_rule_version": IDENTITY_RULE_VERSION,
    }
    if tuple(projected) != PROJECTED_CLASSIFICATION_FIELDS:
        raise AssertionError("projected classification field order drifted")
    return projected


def project_quote(
    row: Mapping[str, Any],
    resolution: QuoteIdentityResolution | IndustryIdentityResolution,
) -> dict[str, Any]:
    """Project one quote to the current code and preserve ``source_ts_code``."""

    source_code, trade_date, _ = _quote_identity(row)
    if source_code != resolution.source_code_on(trade_date):
        _fail("IDENTITY_PROJECTION_SOURCE_MISMATCH", "quote code conflicts with its interval")
    existing_source = row.get("source_ts_code")
    if existing_source not in (None, "") and str(existing_source).strip() != source_code:
        _fail("IDENTITY_PROVENANCE_CONFLICT", "source_ts_code would be overwritten")
    projected = dict(row)
    projected["source_ts_code"] = source_code
    projected["ts_code"] = resolution.quote_index_code
    projected["industry_uid"] = resolution.industry_uid
    projected["identity_state"] = resolution.identity_state
    projected["identity_rule_version"] = IDENTITY_RULE_VERSION
    return projected


def project_member(
    row: Mapping[str, Any], resolution: IndustryIdentityResolution
) -> dict[str, Any]:
    """Project one member row and preserve its endpoint-native L3 code."""

    source_code = _text(row.get("l3_code"), "l3_code")
    state = _text(row.get("is_new"), "is_new")
    if source_code not in SPECIAL_STEEL_CANDIDATE_CODES or state not in {"Y", "N"}:
        _fail("IDENTITY_PROJECTION_SCOPE_MISMATCH", "member row is not a candidate row")
    _canonical_member(row, expected_code=source_code, expected_is_new=state)
    existing_source = row.get("source_l3_code")
    if existing_source not in (None, "") and str(existing_source).strip() != source_code:
        _fail("IDENTITY_PROVENANCE_CONFLICT", "source_l3_code would be overwritten")
    projected = dict(row)
    projected["source_l3_code"] = source_code
    projected["l3_code"] = resolution.member_index_code
    projected["industry_uid"] = resolution.industry_uid
    projected["identity_state"] = resolution.identity_state
    projected["identity_rule_version"] = IDENTITY_RULE_VERSION
    return projected


__all__ = [
    "IDENTITY_CANDIDATE_CODES",
    "IDENTITY_RULE_VERSION",
    "IDENTITY_SCHEMA_VERSION",
    "IDENTITY_STATES",
    "PROJECTED_CLASSIFICATION_FIELDS",
    "SPECIAL_STEEL_CANDIDATE_CODES",
    "SPECIAL_STEEL_INDUSTRY_CODE",
    "SPECIAL_STEEL_INDUSTRY_NAME",
    "SPECIAL_STEEL_INDUSTRY_UID",
    "SPECIAL_INDUSTRY_UID",
    "SPECIAL_STEEL_MEMBER_PATH",
    "IdentityInterval",
    "IdentityResolutionError",
    "IndustryIdentityResolution",
    "QuoteIdentityResolution",
    "make_industry_uid",
    "project_classification",
    "project_member",
    "project_quote",
    "resolve_membership_identity",
    "resolve_quote_identity",
]
