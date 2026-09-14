"""Immutable v2 snapshot pipeline and offline derived-output rebuild."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .core import summarize_axis, validate_sw_daily, validate_trade_cal
from .io_utils import (
    atomic_write_text,
    read_csv,
    read_json,
    sha256_file,
    write_csv,
    write_json,
)
from .tushare_client import SecureTushareClient
from .v2_domain import (
    LEVELS,
    MEMBER_FIELDS,
    VALUATION_FIELDS,
    V2DataError,
    compute_peer_rows,
    normalize_classifications,
    normalize_daily_basic,
    normalize_members,
    select_members_as_of,
    validate_history_continuity,
)
from .v2_identity import (
    IDENTITY_CANDIDATE_CODES,
    IDENTITY_RULE_VERSION,
    PROJECTED_CLASSIFICATION_FIELDS,
    SPECIAL_INDUSTRY_UID,
    project_classification,
    project_member,
    project_quote,
    resolve_membership_identity,
    resolve_quote_identity,
)
from .v2_provider import (
    LIVE_ARTIFACT_PUBLISH_STATE,
    LIVE_PROVIDER_KIND,
    LIVE_VALIDATION_REASON,
    LIVE_VALIDATION_STATE,
    TEST_ARTIFACT_PUBLISH_STATE,
    TEST_PROVIDER_KIND,
    TEST_VALIDATION_REASON,
    TEST_VALIDATION_STATE,
    V2TushareProvider,
)
from .v2_lineage import LAYOUT, copy_flat_lineage
from .v2_storage import (
    SingleWriterLock,
    abort_publication,
    allocate_run,
    apply_publication_pointer,
    append_ledger,
    commit_publication,
    complete_publication,
    inventory_artifacts,
    ledger_terminal_for_run,
    prepare_publication,
    publication_transaction_state,
    recover_publication,
    resolve_pointer,
    runtime_record,
    write_sums,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_RUN_ID = "SWIVD-RUN-20260828-004"
LEGACY_MANIFEST_SHA256 = "5180fd5eb20e2cd556a5a5066039fba676272d735ae2d91b9b798e59b33daea0"
SHANGHAI_TIMEZONE = "Asia/Shanghai"
SW_DAILY_READY_CUTOFF = "18:30:00"
CURRENT_CONTRACT_VERSION = "swivd-contract-v2.3.0"
CURRENT_SPEC_VERSION = "swivd-project-spec-v4.3"
CURRENT_DECISION_ID = "GOV-20260906-001"
CLASSIFICATION_FIELDS = PROJECTED_CLASSIFICATION_FIELDS
SW_DAILY_FIELDS = (
    "industry_uid",
    "ts_code",
    "source_ts_code",
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
    "identity_state",
    "identity_rule_version",
)
MEMBER_SNAPSHOT_FIELDS = (*MEMBER_FIELDS, "membership_state")
CATALOG_IDENTITY_FIELDS = (
    "industry_uid",
    "catalog_index_code",
    "quote_index_code",
    "member_index_code",
    "current_index_code",
    "identity_state",
    "identity_rule",
    "identity_rule_version",
    "identity_reason_disclosure",
)
CATALOG_SUMMARY_FIELDS = (
    "close",
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
    "pe_history_label",
    "pb_history_label",
    "pe_status",
    "pb_status",
    "return_5d",
    "return_mtd",
    "return_ytd",
    "return_5d_status",
    "return_mtd_status",
    "return_ytd_status",
    "valuation_state",
    "return_state",
)


Progress = Callable[[str, int, int, str], None]
TargetSelected = Callable[["UpdateTarget"], None]


class V2PipelineError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


@dataclass(frozen=True)
class UpdateTarget:
    """Server-selected UPDATE_LATEST date and its safe, auditable reason."""

    as_of: str
    reason_code: str
    evaluated_at: str
    timezone: str = SHANGHAI_TIMEZONE
    cutoff: str = SW_DAILY_READY_CUTOFF

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "reason_code": self.reason_code,
            "evaluated_at": self.evaluated_at,
            "timezone": self.timezone,
            "cutoff": self.cutoff,
        }


def _now() -> str:
    return datetime.now(ZoneInfo(SHANGHAI_TIMEZONE)).isoformat(timespec="seconds")


def _is_strict_system_temp_descendant(path: Path) -> bool:
    """Return whether ``path`` resolves below Python's system temp root."""

    try:
        resolved = path.expanduser().resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        resolved.relative_to(temp_root)
    except (OSError, RuntimeError, ValueError):
        return False
    return resolved != temp_root


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _safe_error(exc: BaseException) -> dict[str, str]:
    code = getattr(exc, "code", type(exc).__name__.upper())
    return {"code": str(code), "type": type(exc).__name__}


def _load_spec(path: Path) -> dict[str, Any]:
    from .v2_validator import validate_spec_v4

    return validate_spec_v4(path, require_current=True)


def _open_dates(calendar_rows: Sequence[Mapping[str, Any]], start: str, end: str) -> list[str]:
    return sorted(
        str(row["cal_date"])
        for row in calendar_rows
        if int(str(row["is_open"])) == 1 and start <= str(row["cal_date"]) <= end
    )


def _industry_uid(row: Mapping[str, Any]) -> str:
    return f"{row['src']}:{row['level']}:{row['industry_code']}"


def _direct_classification(row: Mapping[str, Any]) -> dict[str, Any]:
    """Add explicit endpoint identities for an unaffected classification row."""

    code = str(row["index_code"])
    projection = {
        **dict(row),
        "industry_uid": _industry_uid(row),
        "catalog_index_code": code,
        "quote_index_code": code,
        "member_index_code": code,
        "identity_state": "DIRECT",
        "identity_rule": "CATALOG_EQUALS_QUOTE_AND_MEMBER_CODE",
        "identity_rule_version": IDENTITY_RULE_VERSION,
    }
    return projection


def _project_classifications(
    classifications: Mapping[str, Sequence[Mapping[str, Any]]],
    resolution: Any,
) -> dict[str, list[dict[str, Any]]]:
    projected: dict[str, list[dict[str, Any]]] = {}
    special_seen = 0
    for level in LEVELS:
        rows: list[dict[str, Any]] = []
        for row in classifications[level]:
            if _industry_uid(row) == SPECIAL_INDUSTRY_UID:
                rows.append(project_classification(row, resolution))
                special_seen += 1
            else:
                rows.append(_direct_classification(row))
        projected[level] = sorted(rows, key=lambda item: item["index_code"])
    if special_seen != 1:
        raise V2DataError(
            "IDENTITY_CLASSIFICATION_SINGLETON",
            "the governed industry must occur exactly once",
        )
    return projected


def _project_classifications_for_membership(
    classifications: Mapping[str, Sequence[Mapping[str, Any]]],
    quote_resolution: Any,
) -> dict[str, list[dict[str, Any]]]:
    """Acquisition selectors cover both scoped codes, not extra catalog entities."""

    projected: dict[str, list[dict[str, Any]]] = {}
    for level in LEVELS:
        rows: list[dict[str, Any]] = []
        for row in classifications[level]:
            if _industry_uid(row) != SPECIAL_INDUSTRY_UID:
                rows.append(_direct_classification(row))
                continue
            rows.extend(
                [
                {
                    **dict(row),
                    "index_code": candidate_code,
                    "catalog_index_code": quote_resolution.catalog_index_code,
                    "quote_index_code": quote_resolution.quote_index_code,
                    "member_index_code": quote_resolution.quote_index_code,
                    "industry_uid": quote_resolution.industry_uid,
                    "identity_state": quote_resolution.identity_state,
                    "identity_rule": quote_resolution.identity_rule,
                    "identity_rule_version": IDENTITY_RULE_VERSION,
                }
                for candidate_code in IDENTITY_CANDIDATE_CODES
                ]
            )
        projected[level] = rows
    return projected


def _project_direct_quote(
    row: Mapping[str, Any], classification: Mapping[str, Any]
) -> dict[str, Any]:
    source_code = str(row.get("ts_code"))
    if source_code != str(classification["index_code"]):
        raise V2DataError("INDUSTRY_CODE_MISMATCH", "direct quote code drifted")
    projection = {
        **dict(row),
        "industry_uid": _industry_uid(classification),
        "source_ts_code": source_code,
        "identity_state": "DIRECT",
        "identity_rule_version": IDENTITY_RULE_VERSION,
    }
    return projection


def _project_members(
    rows: Sequence[Mapping[str, Any]],
    classifications: Mapping[str, Sequence[Mapping[str, Any]]],
    resolution: Any,
) -> list[dict[str, Any]]:
    l3_by_code = {
        str(row["index_code"]): row for row in classifications["L3"]
    }
    projected: list[dict[str, Any]] = []
    for source in rows:
        source_code = str(source.get("l3_code"))
        if source_code in IDENTITY_CANDIDATE_CODES:
            projected.append(project_member(source, resolution))
            continue
        classification = l3_by_code.get(source_code)
        if classification is None:
            raise V2DataError(
                "MEMBER_PATH_UNKNOWN",
                "membership L3 code is outside the resolved SW2021 taxonomy",
            )
        projected.append(
            {
                **dict(source),
                "industry_uid": classification["industry_uid"],
                "source_l3_code": source_code,
                "identity_state": classification["identity_state"],
                "identity_rule_version": classification["identity_rule_version"],
            }
        )
    return projected


def _classification_equal(
    current: Mapping[str, Sequence[Mapping[str, Any]]], parent_run: Path
) -> bool:
    def semantic(row: Mapping[str, Any]) -> dict[str, str]:
        uid = str(row.get("industry_uid") or _industry_uid(row))
        record = {
            "industry_uid": uid,
            "src": str(row.get("src", "")),
            "level": str(row.get("level", "")),
            "industry_name": str(row.get("industry_name", "")),
            "industry_code": str(row.get("industry_code", "")),
            "parent_code": str(row.get("parent_code", "")),
            "is_pub": str(row.get("is_pub", "")),
        }
        # Only the governed singleton may change its catalog endpoint code;
        # every other classification code remains part of taxonomy identity.
        if uid != SPECIAL_INDUSTRY_UID:
            record["catalog_index_code"] = str(
                row.get("catalog_index_code") or row.get("index_code", "")
            )
        return record

    for level in LEVELS:
        previous = read_csv(parent_run / "inputs" / "normalized" / f"classification_sw2021_{level.lower()}.csv")
        if any(
            _industry_uid(row) == SPECIAL_INDUSTRY_UID
            and (
                not str(row.get("industry_uid", ""))
                or not str(row.get("catalog_index_code", ""))
                or not str(row.get("identity_rule_version", ""))
            )
            for row in previous
        ):
            return False
        left = sorted((semantic(row) for row in current[level]), key=lambda row: row["industry_uid"])
        right = sorted((semantic(row) for row in previous), key=lambda row: row["industry_uid"])
        if left != right:
            return False
    return True


def _copy_tree_files(source: Path, target: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise V2PipelineError("PARENT_INPUT_INVALID")
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise V2PipelineError("SYMLINK_FORBIDDEN")
        if path.is_file():
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)


def _copy_source_snapshot(run_dir: Path) -> list[dict[str, Any]]:
    source_root = run_dir / "source"
    paths = [
        PROJECT_ROOT / "PROJECT_CONTRACT.md",
        PROJECT_ROOT / "PROJECT_CONTRACT_V2.md",
        PROJECT_ROOT / "PROJECT_SPEC_V4.json",
        PROJECT_ROOT / "AGENTS.md",
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "pyproject.toml",
        PROJECT_ROOT / "requirements.lock",
        PROJECT_ROOT / "run_dashboard.py",
        PROJECT_ROOT / "start_macos.command",
        PROJECT_ROOT / "start_windows.cmd",
        # Freeze required maintenance docs, not historical review evidence.
        PROJECT_ROOT / "docs" / "architecture.md",
        PROJECT_ROOT / "docs" / "data_dictionary.md",
        PROJECT_ROOT / "docs" / "runbook.md",
        PROJECT_ROOT / "docs" / "troubleshooting.md",
        *sorted((PROJECT_ROOT / "src" / "swivd").glob("*.py")),
    ]
    if (PROJECT_ROOT / "web").is_dir():
        paths.extend(sorted(path for path in (PROJECT_ROOT / "web").rglob("*") if path.is_file()))
    if (PROJECT_ROOT / "tests").is_dir():
        paths.extend(sorted(path for path in (PROJECT_ROOT / "tests").rglob("*") if path.is_file() and path.suffix in {".py", ".js", ".cjs"}))
    records: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise V2PipelineError("SOURCE_CLOSURE_INVALID", path.name)
        relative = path.relative_to(PROJECT_ROOT)
        target = source_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        records.append(
            {
                "path": relative.as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return records


def _copy_legacy_archive(run_dir: Path) -> dict[str, Any]:
    source = PROJECT_ROOT / "output" / "runs" / LEGACY_RUN_ID
    manifest = source / "manifest.json"
    if not manifest.is_file() or sha256_file(manifest) != LEGACY_MANIFEST_SHA256:
        raise V2PipelineError("LEGACY_ARCHIVE_IDENTITY_MISMATCH")
    selected = [
        "manifest.json",
        "audit.json",
        "dashboard.html",
        "inputs/normalized/classification_sw2014.csv",
        "inputs/normalized/sw_daily_sw2014.csv",
        "tables/sw2014_archive.csv",
        "tables/sw2014_history.csv",
        "reports/adversarial_review.md",
    ]
    target_root = run_dir / "legacy" / LEGACY_RUN_ID
    copied: list[dict[str, Any]] = []
    for relative in selected:
        path = source / relative
        if not path.is_file() or path.is_symlink():
            raise V2PipelineError("LEGACY_ARCHIVE_FILE_MISSING", relative)
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        copied.append({"path": relative, "sha256": sha256_file(path), "bytes": path.stat().st_size})
    return {
        "run_id": LEGACY_RUN_ID,
        "manifest_sha256": LEGACY_MANIFEST_SHA256,
        "copied_files": copied,
    }


def _write_normalized(
    run_dir: Path,
    *,
    calendar: Sequence[Mapping[str, Any]],
    classifications: Mapping[str, Sequence[Mapping[str, Any]]],
    histories: Mapping[str, Sequence[Mapping[str, Any]]],
    episodes: Sequence[Mapping[str, Any]],
    member_snapshot: Sequence[Mapping[str, Any]],
    valuations: Mapping[str, Mapping[str, Any]],
    identity_resolution: Mapping[str, Any],
) -> None:
    root = run_dir / "inputs" / "normalized"
    write_csv(root / "trade_calendar.csv", calendar, ("exchange", "cal_date", "is_open", "pretrade_date"))
    for level in LEVELS:
        write_csv(
            root / f"classification_sw2021_{level.lower()}.csv",
            classifications[level],
            CLASSIFICATION_FIELDS,
        )
        write_csv(
            root / f"sw_daily_sw2021_{level.lower()}.csv",
            histories[level],
            SW_DAILY_FIELDS,
        )
    write_csv(root / "membership_episodes.csv", episodes, MEMBER_FIELDS)
    write_csv(root / "membership_snapshot.csv", member_snapshot, MEMBER_SNAPSHOT_FIELDS)
    write_csv(
        root / "stock_valuation_snapshot.csv",
        [valuations[key] for key in sorted(valuations)],
        VALUATION_FIELDS,
    )
    write_json(root / "industry_identity_resolution.json", _jsonable(identity_resolution))


def _restore_classification(rows: Sequence[Mapping[str, str]]) -> list[dict[str, Any]]:
    return [
        {
            **dict(row),
            "is_pub": int(row["is_pub"]),
        }
        for row in rows
    ]


def _summary_rows(
    *,
    level: str,
    histories: list[dict[str, Any]],
    classifications: list[dict[str, Any]],
    calendar: list[dict[str, Any]],
    as_of: str,
) -> list[dict[str, Any]]:
    published = summarize_axis(
        histories,
        classifications,
        calendar,
        src="SW2021",
        as_of=as_of,
        minimum_valid_observations=252,
    )
    by_code = {row["index_code"]: {"level": level, **row, "industry_source": "sw_daily"} for row in published}
    for classification in classifications:
        if int(classification["is_pub"]) == 0:
            by_code[classification["index_code"]] = {
                "level": level,
                "src": "SW2021",
                "index_code": classification["index_code"],
                "industry_name": classification["industry_name"],
                "as_of": as_of,
                "valuation_state": "NA_NOT_PUBLISHED",
                "return_state": "NA_NOT_PUBLISHED",
                "industry_source": "sw_daily",
            }
    for classification in classifications:
        summary = by_code[classification["index_code"]]
        summary.update(catalog_identity_projection(classification))
    return [by_code[key] for key in sorted(by_code)]


def catalog_identity_projection(
    classification: Mapping[str, Any],
) -> dict[str, Any]:
    """Render the exact endpoint-aware identity disclosed by catalog/shards."""

    state = str(classification["identity_state"])
    projection = {
        "industry_uid": classification["industry_uid"],
        "catalog_index_code": classification["catalog_index_code"],
        "quote_index_code": classification["quote_index_code"],
        "member_index_code": classification["member_index_code"],
        "current_index_code": classification["quote_index_code"],
        "identity_state": state,
        "identity_rule": classification["identity_rule"],
        "identity_rule_version": classification["identity_rule_version"],
        "identity_reason_disclosure": (
            "NOT_APPLICABLE_DIRECT_IDENTITY"
            if state == "DIRECT"
            else "UNKNOWN_UPSTREAM_INTERNAL_CAUSE"
        ),
    }
    if tuple(projection) != CATALOG_IDENTITY_FIELDS:
        raise AssertionError("catalog identity field order drifted")
    return projection


def catalog_summary_projection(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Project one computed summary into the compact UI catalog shape."""

    pe_status = summary.get("pe_status", "UNKNOWN")
    pb_status = summary.get("pb_status", "UNKNOWN")
    return_statuses = [
        summary.get("return_5d_status", "UNKNOWN"),
        summary.get("return_mtd_status", "UNKNOWN"),
        summary.get("return_ytd_status", "UNKNOWN"),
    ]
    not_published = summary.get("valuation_state") == "NA_NOT_PUBLISHED"
    projection = {
        "close": summary.get("close"),
        "pe": summary.get("pe"),
        "pb": summary.get("pb"),
        "pe_percentile": summary.get("pe_percentile_le"),
        "pb_percentile": summary.get("pb_percentile_le"),
        "pe_valid_count": summary.get("pe_valid_count"),
        "pb_valid_count": summary.get("pb_valid_count"),
        "pe_tie_count": summary.get("pe_tie_count"),
        "pb_tie_count": summary.get("pb_tie_count"),
        "pe_tie_ratio": summary.get("pe_tie_ratio"),
        "pb_tie_ratio": summary.get("pb_tie_ratio"),
        "pe_history_label": summary.get("pe_history_label"),
        "pb_history_label": summary.get("pb_history_label"),
        "pe_status": summary.get("pe_status"),
        "pb_status": summary.get("pb_status"),
        "return_5d": summary.get("return_5d"),
        "return_mtd": summary.get("return_mtd"),
        "return_ytd": summary.get("return_ytd"),
        "return_5d_status": summary.get("return_5d_status"),
        "return_mtd_status": summary.get("return_mtd_status"),
        "return_ytd_status": summary.get("return_ytd_status"),
        "valuation_state": (
            "NA_NOT_PUBLISHED"
            if not_published
            else "OK"
            if pe_status == "OK" and pb_status == "OK"
            else f"PE={pe_status};PB={pb_status}"
        ),
        "return_state": (
            "NA_NOT_PUBLISHED"
            if not_published
            else "OK"
            if all(value == "OK" for value in return_statuses)
            else ";".join(str(value) for value in return_statuses)
        ),
    }
    if set(projection) != set(CATALOG_SUMMARY_FIELDS):  # pragma: no cover
        raise AssertionError("catalog summary projection drift")
    return projection


def build_derived(run_dir: Path, *, as_of: str) -> dict[str, Any]:
    """Build tables and lazy UI only from normalized inputs."""

    normalized = run_dir / "inputs" / "normalized"
    calendar = read_csv(normalized / "trade_calendar.csv")
    histories: dict[str, list[dict[str, Any]]] = {}
    classifications: dict[str, list[dict[str, Any]]] = {}
    summaries: dict[str, list[dict[str, Any]]] = {}
    for level in LEVELS:
        classifications[level] = _restore_classification(
            read_csv(normalized / f"classification_sw2021_{level.lower()}.csv")
        )
        histories[level] = read_csv(normalized / f"sw_daily_sw2021_{level.lower()}.csv")
        summaries[level] = _summary_rows(
            level=level,
            histories=histories[level],
            classifications=classifications[level],
            calendar=calendar,
            as_of=as_of,
        )
        fields = sorted({key for row in summaries[level] for key in row})
        write_csv(run_dir / "tables" / f"industry_summary_{level.lower()}.csv", summaries[level], fields)

    episodes = read_csv(normalized / "membership_episodes.csv")
    member_snapshot = read_csv(normalized / "membership_snapshot.csv")
    valuations_rows = read_csv(normalized / "stock_valuation_snapshot.csv")
    valuations = {row["ts_code"]: row for row in valuations_rows}
    peer_rows = compute_peer_rows(member_snapshot, valuations, as_of=as_of, minimum_peers=5)
    peer_fields = sorted({key for row in peer_rows for key in row})
    write_csv(run_dir / "tables" / "stock_peer_valuation.csv", peer_rows, peer_fields)

    counts: dict[tuple[str, str], int] = {}
    for row in peer_rows:
        key = (row["level"], row["index_code"])
        counts[key] = counts.get(key, 0) + 1
    catalog_entries: list[dict[str, Any]] = []
    ui_root = run_dir / "ui"
    for level in LEVELS:
        classification_by_code = {
            row["index_code"]: row for row in classifications[level]
        }
        history_by_code: dict[str, list[dict[str, Any]]] = {}
        for row in histories[level]:
            history_by_code.setdefault(row["ts_code"], []).append(
                {
                    field: row.get(field)
                    for field in ("trade_date", "close", "pe", "pb", "source_ts_code")
                }
            )
        constituents_by_code: dict[str, list[dict[str, Any]]] = {}
        for row in peer_rows:
            if row["level"] == level:
                constituents_by_code.setdefault(row["index_code"], []).append(row)
        for summary in summaries[level]:
            code = summary["index_code"]
            safe = code.replace(".", "_")
            classification = classification_by_code[code]
            identity = catalog_identity_projection(classification)
            shard = {
                "schema_version": "swivd-industry-shard-v2",
                "as_of": as_of,
                "level": level,
                "index_code": code,
                "industry": summary,
                "identity": identity,
                "history": sorted(history_by_code.get(code, []), key=lambda row: row["trade_date"]),
                "constituents": constituents_by_code.get(code, []),
            }
            write_json(ui_root / "industries" / level / f"{safe}.json", _jsonable(shard))
            catalog_entries.append(
                {
                    "level": level,
                    "index_code": code,
                    "industry_name": summary["industry_name"],
                    "parent_code": classification.get("parent_code", ""),
                    "is_pub": int(classification["is_pub"]),
                    "member_row_count": counts.get((level, code), 0),
                    **identity,
                    **catalog_summary_projection(summary),
                    "shard": f"industries/{level}/{safe}.json",
                }
            )
    catalog = {
        "schema_version": "swivd-ui-catalog-v3",
        "as_of": as_of,
        "research_grade": "RESEARCH_ONLY",
        "decision_eligible": False,
        "production_approved": False,
        "levels": list(LEVELS),
        "industries": catalog_entries,
        "legacy_archive": {"run_id": LEGACY_RUN_ID, "level": "L1", "taxonomy": "SW2014"},
    }
    write_json(ui_root / "catalog.json", _jsonable(catalog))
    return {
        "classification_counts": {level: len(classifications[level]) for level in LEVELS},
        "industry_history_rows": {level: len(histories[level]) for level in LEVELS},
        "membership_episode_rows": len(episodes),
        "membership_snapshot_rows": len(member_snapshot),
        "stock_valuation_rows": len(valuations_rows),
        "peer_rows": len(peer_rows),
        "identity_states": {
            state: sum(
                row["identity_state"] == state
                for level in LEVELS
                for row in classifications[level]
            )
            for state in sorted(
                {
                    row["identity_state"]
                    for level in LEVELS
                    for row in classifications[level]
                }
            )
        },
    }


def _prepare_histories(
    *,
    provider: V2TushareProvider,
    classifications: Mapping[str, Sequence[Mapping[str, Any]]],
    calendar: Sequence[Mapping[str, Any]],
    as_of: str,
    parent_run: Path | None,
    purpose: str,
    parent_manifest: Mapping[str, Any] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any], Any]:
    open_dates = _open_dates(calendar, "20211213", as_of)
    if as_of not in open_dates:
        raise V2DataError("AS_OF_NOT_OPEN", "target date is not an SSE open day")

    flat_classifications = [
        row for level in LEVELS for row in classifications[level]
    ]
    special_rows = [
        row for row in flat_classifications if _industry_uid(row) == SPECIAL_INDUSTRY_UID
    ]
    if len(special_rows) != 1:
        raise V2DataError(
            "IDENTITY_CLASSIFICATION_SINGLETON",
            "the governed industry must occur exactly once",
        )

    # Resolve the governed identity before any code is admitted to ordinary
    # history processing.  Both candidate histories are frozen every run so a
    # later upstream convergence or one-time transition cannot be hidden by a
    # parent snapshot.
    current_rows = provider.industry_day(as_of)
    candidate_histories = provider.identity_histories(
        codes=IDENTITY_CANDIDATE_CODES,
        start_date="20211213",
        end_date=as_of,
    )
    for candidate_code in IDENTITY_CANDIDATE_CODES:
        candidate_rows = list(candidate_histories[candidate_code])
        if candidate_rows:
            validate_sw_daily(
                candidate_rows,
                whitelist={candidate_code: special_rows[0]["industry_name"]},
                start_date="20211213",
                end_date=as_of,
                open_dates=open_dates,
                row_limit=None,
                strict_name_match=True,
            )
    quote_resolution = resolve_quote_identity(
        flat_classifications,
        current_rows,
        candidate_histories,
        open_dates,
        target_trade_date=as_of,
    )

    direct_by_code = {
        str(row["index_code"]): row
        for row in flat_classifications
        if _industry_uid(row) != SPECIAL_INDUSTRY_UID
    }
    known_endpoint_codes = set(direct_by_code) | set(IDENTITY_CANDIDATE_CODES)

    def projected_day(
        trade_date: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
        by_code: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            by_code.setdefault(str(row.get("ts_code")), []).append(row)
        extra_codes = set(by_code) - known_endpoint_codes
        output: dict[str, list[dict[str, Any]]] = {level: [] for level in LEVELS}
        for level in LEVELS:
            published = {
                str(row["index_code"]): row
                for row in classifications[level]
                if int(row["is_pub"]) == 1
                and _industry_uid(row) != SPECIAL_INDUSTRY_UID
            }
            missing = sorted(set(published) - set(by_code))
            if missing:
                raise V2DataError(
                    "CURRENT_INDUSTRY_ROW_MISSING",
                    ",".join(missing[:5]),
                )
            if any(len(by_code[code]) != 1 for code in published):
                raise V2DataError("DUPLICATE_INDUSTRY_DAY", level)
            direct_rows = [dict(by_code[code][0]) for code in sorted(published)]
            if direct_rows:
                validate_sw_daily(
                    direct_rows,
                    whitelist={code: row["industry_name"] for code, row in published.items()},
                    start_date=trade_date,
                    end_date=trade_date,
                    open_dates=[trade_date],
                    row_limit=None,
                    strict_name_match=True,
                )
            output[level].extend(
                _project_direct_quote(row, published[str(row["ts_code"])])
                for row in direct_rows
            )

        expected_special_code = quote_resolution.source_code_on(trade_date)
        special_day_rows = list(by_code.get(expected_special_code, []))
        other_candidate_rows = [
            row
            for code in IDENTITY_CANDIDATE_CODES
            if code != expected_special_code
            for row in by_code.get(code, [])
        ]
        if len(special_day_rows) != 1 or other_candidate_rows:
            raise V2DataError(
                "IDENTITY_TARGET_DAY_SINGLETON_REQUIRED",
                "the day response must contain exactly the interval candidate",
            )
        output["L3"].append(project_quote(special_day_rows[0], quote_resolution))
        for level in LEVELS:
            output[level] = sorted(output[level], key=lambda row: row["ts_code"])
        return output, extra_codes

    # Validate the already-frozen target-day response against every unaffected
    # published code too.  The alias gate may not conceal an unrelated missing
    # industry.
    _, current_extra_codes = projected_day(as_of, current_rows)

    histories: dict[str, list[dict[str, Any]]] = {}
    parent_info: dict[str, Any] = {}
    if parent_run is not None:
        from .v2_validator import validate_run_v2

        parent_manifest = parent_manifest or validate_run_v2(parent_run)
        if parent_manifest.get("schema_version") not in {"swivd-local-snapshot-manifest-v3", "swivd-local-snapshot-manifest-v4"}:
            raise V2DataError(
                "PARENT_IDENTITY_EVIDENCE_MISSING",
                "identity-aware updates require a v3 parent snapshot",
            )
        if parent_manifest.get("provider_kind") != provider.provider_kind:
            raise V2DataError(
                "PARENT_PROVIDER_KIND_MISMATCH",
                "live and injected-test snapshot lineages cannot be mixed",
            )
        if not _classification_equal(classifications, parent_run):
            raise V2DataError(
                "CLASSIFICATION_CHANGED_REQUIRES_BOOTSTRAP",
                "current classification differs from the parent snapshot",
            )
        parent_as_of = str(parent_manifest["as_of"])
        if purpose == "UPDATE_LATEST" and as_of <= parent_as_of:
            raise V2DataError("TARGET_NOT_AFTER_PARENT", "latest update must advance the date")
        parent_info = {
            "run_id": parent_manifest["run_id"],
            "as_of": parent_as_of,
            "manifest_sha256": sha256_file(parent_run / "manifest.json"),
        }

        def reproject_previous(
            level: str, *, through_date: str | None = None
        ) -> list[dict[str, Any]]:
            rows = read_csv(
                parent_run
                / "inputs"
                / "normalized"
                / f"sw_daily_sw2021_{level.lower()}.csv"
            )
            if through_date is not None:
                rows = [
                    row for row in rows if str(row.get("trade_date")) <= through_date
                ]
            projected: list[dict[str, Any]] = []
            for row in rows:
                if row.get("industry_uid") == SPECIAL_INDUSTRY_UID:
                    source_code = str(row.get("source_ts_code") or "")
                    if source_code not in IDENTITY_CANDIDATE_CODES:
                        raise V2DataError(
                            "PARENT_IDENTITY_EVIDENCE_MISSING",
                            "parent special history lacks source_ts_code",
                        )
                    projected.append(
                        project_quote({**row, "ts_code": source_code}, quote_resolution)
                    )
                else:
                    source_code = str(row.get("source_ts_code") or row.get("ts_code") or "")
                    classification = direct_by_code.get(source_code)
                    if classification is None:
                        raise V2DataError(
                            "PARENT_IDENTITY_EVIDENCE_MISSING",
                            "parent direct history is outside current taxonomy",
                        )
                    projected.append(
                        _project_direct_quote({**row, "ts_code": source_code}, classification)
                    )
            return projected

        if purpose == "MATERIALIZE_DATE" and as_of <= parent_as_of:
            for level in LEVELS:
                histories[level] = reproject_previous(level, through_date=as_of)
        else:
            missing_dates = [date for date in open_dates if parent_as_of < date <= as_of]
            if not missing_dates:
                raise V2DataError("NO_INCREMENT_DATES", "there is no open date after the parent")
            by_level_parts: dict[str, list[dict[str, Any]]] = {level: [] for level in LEVELS}
            extra_codes: set[str] = set(current_extra_codes)
            for position, trade_date in enumerate(missing_dates, start=1):
                provider.progress(
                    "INDUSTRY_DATA", position - 1, len(missing_dates), trade_date
                )
                daily_rows = current_rows if trade_date == as_of else provider.industry_day(trade_date)
                projected, observed_extra = projected_day(trade_date, daily_rows)
                extra_codes.update(observed_extra)
                for level in LEVELS:
                    by_level_parts[level].extend(projected[level])
                provider.progress("INDUSTRY_DATA", position, len(missing_dates), trade_date)
            for level in LEVELS:
                previous = reproject_previous(level)
                histories[level] = [*previous, *by_level_parts[level]]
            parent_info["increment_dates"] = missing_dates
            parent_info["ignored_non_current_codes"] = sorted(extra_codes)
    else:
        for level in LEVELS:
            published = {
                str(row["index_code"]): row
                for row in classifications[level]
                if int(row["is_pub"]) == 1
                and _industry_uid(row) != SPECIAL_INDUSTRY_UID
            }
            rows = provider.industry_history(
                codes=sorted(published),
                start_date="20211213",
                end_date=as_of,
                level=level,
            )
            validate_sw_daily(
                rows,
                whitelist={code: row["industry_name"] for code, row in published.items()},
                start_date="20211213",
                end_date=as_of,
                open_dates=open_dates,
                row_limit=None,
                strict_name_match=True,
            )
            histories[level] = [
                _project_direct_quote(row, published[str(row["ts_code"])])
                for row in rows
            ]
            if level == "L3":
                for candidate_code in IDENTITY_CANDIDATE_CODES:
                    candidate_rows = list(candidate_histories[candidate_code])
                    if candidate_rows:
                        validate_sw_daily(
                            candidate_rows,
                            whitelist={candidate_code: special_rows[0]["industry_name"]},
                            start_date="20211213",
                            end_date=as_of,
                            open_dates=open_dates,
                            row_limit=None,
                            strict_name_match=True,
                        )
                    histories[level].extend(
                        project_quote(row, quote_resolution) for row in candidate_rows
                    )
            histories[level] = sorted(
                histories[level], key=lambda row: (row["ts_code"], row["trade_date"])
            )

    continuity: dict[str, Any] = {}
    for level in LEVELS:
        published_codes = [
            (
                quote_resolution.quote_index_code
                if _industry_uid(row) == SPECIAL_INDUSTRY_UID
                else row["index_code"]
            )
            for row in classifications[level]
            if int(row["is_pub"]) == 1
        ]
        continuity[level] = validate_history_continuity(
            histories[level],
            published_codes=published_codes,
            open_dates=open_dates,
            start_date="20211213",
            as_of=as_of,
            level=level,
        )
    return (
        histories,
        {
            "parent": parent_info or None,
            "continuity": continuity,
            "quote_identity": quote_resolution.to_dict(),
            "ignored_non_current_codes": sorted(current_extra_codes),
        },
        quote_resolution,
    )


def run_snapshot(
    *,
    spec_path: Path,
    data_dir: Path,
    as_of: str,
    purpose: str,
    progress: Progress | None = None,
    client: SecureTushareClient | None = None,
    job_id: str | None = None,
    _writer_lock: SingleWriterLock | None = None,
) -> dict[str, Any]:
    """Create and optionally publish one immutable v2 snapshot."""

    if purpose not in {"UPDATE_LATEST", "MATERIALIZE_DATE"}:
        raise V2PipelineError("INVALID_JOB_KIND")
    provider_kind = TEST_PROVIDER_KIND if client is not None else LIVE_PROVIDER_KIND
    if client is not None and not _is_strict_system_temp_descendant(data_dir):
        raise V2PipelineError(
            "INJECTED_CLIENT_DATA_DIR_FORBIDDEN",
            "an injected client may write only below the system temporary directory",
        )
    if provider_kind == LIVE_PROVIDER_KIND:
        live_validation_state = LIVE_VALIDATION_STATE
        live_validation_reason = LIVE_VALIDATION_REASON
        artifact_publish_state = LIVE_ARTIFACT_PUBLISH_STATE
    else:
        live_validation_state = TEST_VALIDATION_STATE
        live_validation_reason = TEST_VALIDATION_REASON
        artifact_publish_state = TEST_ARTIFACT_PUBLISH_STATE
    progress = progress or (lambda phase, completed, total, item: None)
    spec = _load_spec(spec_path)
    if as_of < spec["membership"]["history_start"]:
        raise V2PipelineError("DATE_BEFORE_SW2021")
    parent_resolved = resolve_pointer(data_dir)
    parent_run = parent_resolved[0] if parent_resolved is not None else None
    current_manifest = None
    if purpose == "UPDATE_LATEST" and parent_run is not None:
        from .v2_validator import validate_run_v2

        current_manifest = validate_run_v2(parent_run)
        current_as_of = str(current_manifest["as_of"])
        if as_of < current_as_of:
            raise V2PipelineError(
                "TARGET_BEFORE_CURRENT",
                f"selected target {as_of} is before current {current_as_of}",
            )
        if as_of == current_as_of:
            return {**current_manifest, "job_outcome": "ALREADY_UP_TO_DATE"}
    run_id, run_dir = allocate_run(data_dir, as_of)
    created_at = _now()
    request_log: list[dict[str, Any]] = []
    publication_lock = _writer_lock
    owns_publication_lock = False
    publication_started = False
    publication_committed = False
    try:
        progress("PREFLIGHT", 0, 1, as_of)
        provider = V2TushareProvider(
            raw_root=run_dir / "inputs" / "raw",
            request_log=request_log,
            client=client,
            progress=progress,
        )
        if provider.provider_kind != provider_kind:
            raise V2PipelineError("PROVIDER_KIND_INTERNAL_MISMATCH")
        calendar = validate_trade_cal(
            provider.trade_calendar("20211213", as_of),
            exchange="SSE",
            start_date="20211213",
            end_date=as_of,
        )
        progress("PREFLIGHT", 1, 1, as_of)
        raw_classes = provider.classifications()
        base_classifications, _ = normalize_classifications(raw_classes)
        histories, history_audit, quote_resolution = _prepare_histories(
            provider=provider,
            classifications=base_classifications,
            calendar=calendar,
            as_of=as_of,
            parent_run=parent_run,
            purpose=purpose,
            parent_manifest=current_manifest,
        )
        membership_classifications = _project_classifications_for_membership(
            base_classifications,
            quote_resolution,
        )
        main_rounds = {
            round_number: provider.membership_round(
                classifications=membership_classifications,
                round_number=round_number,
            )
            for round_number in (1, 2)
        }
        if sorted(
            json.dumps(_jsonable(row), ensure_ascii=False, sort_keys=True)
            for row in main_rounds[1]
        ) != sorted(
            json.dumps(_jsonable(row), ensure_ascii=False, sort_keys=True)
            for row in main_rounds[2]
        ):
            raise V2DataError(
                "MEMBERSHIP_RAW_ROUNDS_DRIFT",
                "raw main membership changed between the two rounds",
            )
        candidate_rounds = {
            round_number: provider.identity_membership_round(
                codes=IDENTITY_CANDIDATE_CODES,
                round_number=round_number,
            )
            for round_number in (1, 2)
        }
        main_by_state = {
            round_number: {
                state: [
                    row
                    for row in main_rounds[round_number]
                    if str(row.get("is_new")) == state
                ]
                for state in ("Y", "N")
            }
            for round_number in (1, 2)
        }
        identity_resolution = resolve_membership_identity(
            quote_resolution,
            candidate_rounds,
            main_by_state,
            membership_as_of=purpose == "MATERIALIZE_DATE",
        )
        classifications = _project_classifications(
            base_classifications,
            identity_resolution,
        )
        _, l3_paths = normalize_classifications(classifications)
        round_one = normalize_members(
            _project_members(main_rounds[1], classifications, identity_resolution),
            l3_paths=l3_paths,
        )
        round_two = normalize_members(
            _project_members(main_rounds[2], classifications, identity_resolution),
            l3_paths=l3_paths,
        )
        if round_one != round_two:
            raise V2DataError("MEMBERSHIP_ROUNDS_DRIFT", "normalized membership changed")
        history_audit["identity_resolution"] = identity_resolution.to_dict()
        member_snapshot = select_members_as_of(round_one, as_of)
        valuations = normalize_daily_basic(provider.daily_basic(as_of), as_of=as_of)

        _write_normalized(
            run_dir,
            calendar=calendar,
            classifications=classifications,
            histories=histories,
            episodes=round_one,
            member_snapshot=member_snapshot,
            valuations=valuations,
            identity_resolution=identity_resolution.to_dict(),
        )
        if parent_run is not None:
            copy_flat_lineage(parent_run, run_dir, read_json(parent_run / "manifest.json"))
        progress("BUILD", 0, 1, run_id)
        legacy = _copy_legacy_archive(run_dir)
        build_counts = build_derived(run_dir, as_of=as_of)
        source_files = _copy_source_snapshot(run_dir)
        identity_evidence_path = "inputs/normalized/industry_identity_resolution.json"
        identity_evidence_sha256 = sha256_file(run_dir / identity_evidence_path)
        audit = {
            "schema_version": "swivd-v2-audit-v2",
            "run_id": run_id,
            "as_of": as_of,
            "purpose": purpose,
            "provider_kind": provider_kind,
            "live_validation_state": live_validation_state,
            "live_validation_reason": live_validation_reason,
            "industry_valuation_source": "sw_daily",
            "stock_aggregation_for_industry_valuation": False,
            "membership_boundary_policy": "UNKNOWN_WITHOUT_AUTHORITY",
            "request_count": len(request_log),
            "requests": request_log,
            "history": history_audit,
            "identity_resolution": identity_resolution.to_dict(),
            "identity_evidence": {
                "path": identity_evidence_path,
                "sha256": identity_evidence_sha256,
            },
            "counts": build_counts,
            "membership_states": {
                state: sum(row["membership_state"] == state for row in member_snapshot)
                for state in sorted({row["membership_state"] for row in member_snapshot})
            },
            "legacy_archive": legacy,
        }
        write_json(run_dir / "audit.json", _jsonable(audit))
        report = (
            f"# {run_id} 对抗式审查\n\n"
            f"- 截止日：`{as_of}`\n- 作业：`{purpose}`\n"
            "- 行业估值来源：`sw_daily`\n- 个股聚合替代行业估值：`false`\n"
            f"- 行业身份：`{identity_resolution.industry_uid}` / "
            f"`{identity_resolution.identity_state}`\n"
            f"- 目录/行情/成员代码：`{identity_resolution.catalog_index_code}` / "
            f"`{identity_resolution.quote_index_code}` / "
            f"`{identity_resolution.member_index_code}`\n"
            f"- Provider：`{provider_kind}`\n"
            f"- 实时验证：`{live_validation_state}` / `{live_validation_reason}`\n"
            "- 成员边界：证据不足时 UNKNOWN，不任意选择\n"
            "- 研究等级：`RESEARCH_ONLY`\n- 决策资格：`false`\n- 生产批准：`false`\n"
        )
        atomic_write_text(run_dir / "reports" / "adversarial_review.md", report)
        write_json(
            run_dir / "offline_validation.json",
            {
                "schema_version": "swivd-v2-offline-validation-v1",
                "status": "PASS",
                "entrypoint": "swivd.v2_validator.validate_run_v2",
                "provider_kind": provider_kind,
            },
        )
        progress("BUILD", 1, 1, run_id)

        manifest = {
            "schema_version": "swivd-local-snapshot-manifest-v4",
            "lineage_layout": LAYOUT,
            "contract_version": CURRENT_CONTRACT_VERSION,
            "spec_version": CURRENT_SPEC_VERSION,
            "decision_id": CURRENT_DECISION_ID,
            "run_id": run_id,
            "purpose": purpose,
            "as_of": as_of,
            "created_at": created_at,
            "completed_at": _now(),
            "execution_status": "COMPLETED",
            "artifact_publish_state": artifact_publish_state,
            "live_validation_state": live_validation_state,
            "live_validation_reason": live_validation_reason,
            "provider_kind": provider_kind,
            "research_grade": "RESEARCH_ONLY",
            "decision_eligible": False,
            "production_approved": False,
            "windows_e2e": "UNVERIFIED",
            "spec_sha256": sha256_file(spec_path),
            "contract_sha256": sha256_file(PROJECT_ROOT / "PROJECT_CONTRACT_V2.md"),
            "parent": (
                {
                    field: history_audit["parent"][field]
                    for field in ("run_id", "as_of", "manifest_sha256")
                }
                if history_audit["parent"] is not None
                else None
            ),
            "identity_resolution": {
                "rule_version": IDENTITY_RULE_VERSION,
                "industry_uid": identity_resolution.industry_uid,
                "state": identity_resolution.identity_state,
                "current_index_code": identity_resolution.current_index_code,
                "catalog_index_code": identity_resolution.catalog_index_code,
                "quote_index_code": identity_resolution.quote_index_code,
                "member_index_code": identity_resolution.member_index_code,
                "evidence_path": identity_evidence_path,
                "evidence_sha256": identity_evidence_sha256,
            },
            "request_count": len(request_log),
            "runtime": runtime_record(),
            "source_files": source_files,
            "artifacts": inventory_artifacts(run_dir),
            "validation": {"status": "PASS", "entrypoint": "swivd.v2_validator.validate_run_v2"},
        }
        write_json(run_dir / "manifest.json", manifest)
        write_sums(run_dir)
        progress("VALIDATE", 0, 1, run_id)
        from .v2_validator import validate_run_v2

        validate_run_v2(run_dir)
        progress("VALIDATE", 1, 1, run_id)
        manifest_sha = sha256_file(run_dir / "manifest.json")
        progress("PUBLISH", 0, 1, run_id)
        if publication_lock is None:
            publication_lock = SingleWriterLock(data_dir)
            publication_lock.__enter__()
            owns_publication_lock = True
        elif not publication_lock.held_for(data_dir):
            raise V2PipelineError("WRITER_LOCK_REQUIRED")
        publication_started = True
        prepare_publication(
            data_dir,
            run_id=run_id,
            as_of=as_of,
            purpose=purpose,
            manifest_sha256=manifest_sha,
            provider_kind=provider_kind,
            job_id=job_id,
        )
        apply_publication_pointer(data_dir, transaction_id=run_id)
        commit_publication(data_dir, transaction_id=run_id)
        publication_committed = True
        complete_publication(data_dir, transaction_id=run_id)
        # Publication is already committed.  A presentation-only progress
        # callback must never relabel the immutable run or roll back latest.
        try:
            progress("PUBLISH", 1, 1, run_id)
        except Exception:
            pass
        return manifest
    except Exception as exc:
        failure = {
            "schema_version": "swivd-v2-failure-v1",
            "run_id": run_id,
            "purpose": purpose,
            "as_of": as_of,
            "created_at": created_at,
            "failed_at": _now(),
            "provider_kind": provider_kind,
            "live_validation_state": live_validation_state,
            "live_validation_reason": live_validation_reason,
            "error": _safe_error(exc),
            "request_count": len(request_log),
            "requests": request_log,
        }
        transaction_state: str | None = None
        if publication_started:
            try:
                transaction_state = publication_transaction_state(
                    data_dir, transaction_id=run_id
                )
            except Exception as recovery_exc:
                raise V2PipelineError(
                    "PUBLICATION_RECOVERY_REQUIRED"
                ) from recovery_exc
        if publication_committed or transaction_state == "COMMITTED":
            try:
                recover_publication(data_dir)
                terminal = ledger_terminal_for_run(data_dir, run_id)
                durable_success = terminal is not None and all(
                    terminal.get(field) == value
                    for field, value in {
                        "event": "RUN_SUCCEEDED",
                        "run_id": run_id,
                        "purpose": purpose,
                        "as_of": as_of,
                        "provider_kind": provider_kind,
                        "job_id": job_id,
                        "manifest_sha256": manifest_sha,
                    }.items()
                )
                if durable_success:
                    return manifest
            except Exception as recovery_exc:
                raise V2PipelineError(
                    "PUBLICATION_COMMITTED_RECOVERY_REQUIRED"
                ) from recovery_exc
            raise V2PipelineError(
                "PUBLICATION_COMMITTED_RECOVERY_REQUIRED"
            ) from exc
        write_json(run_dir / "failure.json", failure)
        if transaction_state == "PREPARED":
            abort_publication(
                data_dir,
                transaction_id=run_id,
                terminal_record={
                    "event": "RUN_FAILED",
                    "error": failure["error"],
                    "recorded_at": _now(),
                },
            )
            raise
        existing_terminal = ledger_terminal_for_run(data_dir, run_id)
        if existing_terminal is not None:
            if existing_terminal.get("event") != "RUN_FAILED":
                raise V2PipelineError("PUBLICATION_TERMINAL_CONFLICT") from exc
            raise
        append_ledger(
            data_dir,
            {
                "event": "RUN_FAILED",
                "run_id": run_id,
                "purpose": purpose,
                "as_of": as_of,
                "provider_kind": provider_kind,
                "job_id": job_id,
                "error": failure["error"],
                "recorded_at": _now(),
            },
        )
        raise
    finally:
        if owns_publication_lock and publication_lock is not None:
            publication_lock.__exit__(None, None, None)


def rebuild_v2(*, run_dir: Path, output_dir: Path) -> dict[str, Any]:
    """Rebuild tables/UI from frozen normalized inputs without network."""

    if output_dir.exists():
        raise V2PipelineError("REBUILD_OUTPUT_EXISTS")
    from .v2_validator import validate_run_v2

    manifest = validate_run_v2(run_dir)
    output_dir.mkdir(parents=True)
    _copy_tree_files(run_dir / "inputs" / "normalized", output_dir / "inputs" / "normalized")
    counts = build_derived(output_dir, as_of=str(manifest["as_of"]))
    compared: list[dict[str, Any]] = []
    for relative_root in ("tables", "ui"):
        for rebuilt in sorted((output_dir / relative_root).rglob("*")):
            if rebuilt.is_file():
                relative = rebuilt.relative_to(output_dir)
                original = run_dir / relative
                same = original.is_file() and sha256_file(original) == sha256_file(rebuilt)
                compared.append({"path": relative.as_posix(), "byte_identical": same})
                if not same:
                    raise V2PipelineError("OFFLINE_REBUILD_MISMATCH", relative.as_posix())
    return {"status": "PASS", "counts": counts, "files": compared}


def resolve_update_target(
    *,
    client: SecureTushareClient | None = None,
    now: datetime | None = None,
) -> UpdateTarget:
    """Select UPDATE_LATEST's target before fetching any target-day market data.

    ``sw_daily`` is scheduled for 18:30 Asia/Shanghai.  Before that cutoff an
    open calendar date is not yet eligible, so its authoritative
    ``pretrade_date`` is the target.  Once a target is selected, downstream
    completeness checks remain fail-closed and never trigger another fallback.
    """

    timezone = ZoneInfo(SHANGHAI_TIMEZONE)
    if now is None:
        local_now = datetime.now(timezone)
    else:
        if now.tzinfo is None or now.utcoffset() is None:
            raise V2PipelineError("NAIVE_UPDATE_CLOCK")
        local_now = now.astimezone(timezone)
    today = local_now.strftime("%Y%m%d")
    temporary = Path(tempfile.mkdtemp(prefix="swivd-date-probe-"))
    try:
        provider = V2TushareProvider(raw_root=temporary / "inputs" / "raw", request_log=[], client=client)
        rows = validate_trade_cal(
            provider.trade_calendar(today, today),
            exchange="SSE",
            start_date=today,
            end_date=today,
        )
        today_rows = [row for row in rows if str(row.get("cal_date")) == today]
        if len(today_rows) != 1:
            raise V2PipelineError("CURRENT_TRADE_CALENDAR_ROW_INVALID")
        today_row = today_rows[0]
        today_is_open = int(str(today_row["is_open"])) == 1
        cutoff_reached = (
            local_now.hour,
            local_now.minute,
            local_now.second,
            local_now.microsecond,
        ) >= (18, 30, 0, 0)
        if today_is_open and cutoff_reached:
            return UpdateTarget(
                as_of=today,
                reason_code="CURRENT_OPEN_DAY_AT_OR_AFTER_SW_DAILY_CUTOFF",
                evaluated_at=local_now.isoformat(timespec="seconds"),
            )

        previous = str(today_row.get("pretrade_date") or "").strip()
        try:
            parsed_previous = datetime.strptime(previous, "%Y%m%d")
            parsed_today = datetime.strptime(today, "%Y%m%d")
        except ValueError as exc:
            raise V2PipelineError("PREVIOUS_OPEN_DAY_INVALID") from exc
        if parsed_previous >= parsed_today:
            raise V2PipelineError("PREVIOUS_OPEN_DAY_INVALID")
        reason = (
            "PREVIOUS_OPEN_DAY_BEFORE_SW_DAILY_CUTOFF"
            if today_is_open
            else "LATEST_OPEN_DAY_NON_TRADING_DATE"
        )
        return UpdateTarget(
            as_of=previous,
            reason_code=reason,
            evaluated_at=local_now.isoformat(timespec="seconds"),
        )
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def resolve_latest_open_date(
    *,
    client: SecureTushareClient | None = None,
    now: datetime | None = None,
) -> str:
    """Compatibility wrapper returning only the cutoff-aware target date."""

    return resolve_update_target(client=client, now=now).as_of


def execute_locked(
    *,
    spec_path: Path,
    data_dir: Path,
    as_of: str,
    purpose: str,
    progress: Progress | None = None,
    client: SecureTushareClient | None = None,
) -> dict[str, Any]:
    with SingleWriterLock(data_dir) as lock:
        recover_publication(data_dir)
        return run_snapshot(
            spec_path=spec_path,
            data_dir=data_dir,
            as_of=as_of,
            purpose=purpose,
            progress=progress,
            client=client,
            _writer_lock=lock,
        )


def execute_update_locked(
    *,
    spec_path: Path,
    data_dir: Path,
    progress: Progress | None = None,
    client: SecureTushareClient | None = None,
    now: datetime | None = None,
    target_selected: TargetSelected | None = None,
) -> dict[str, Any]:
    """Select and execute UPDATE_LATEST under the same cross-process lock."""

    with SingleWriterLock(data_dir) as lock:
        recover_publication(data_dir)
        target = resolve_update_target(client=client, now=now)
        if target_selected is not None:
            target_selected(target)
        manifest = run_snapshot(
            spec_path=spec_path,
            data_dir=data_dir,
            as_of=target.as_of,
            purpose="UPDATE_LATEST",
            progress=progress,
            client=client,
            _writer_lock=lock,
        )
        return {**manifest, "target_selection": target.as_dict()}


__all__ = [
    "V2PipelineError",
    "build_derived",
    "execute_locked",
    "execute_update_locked",
    "rebuild_v2",
    "resolve_latest_open_date",
    "resolve_update_target",
    "run_snapshot",
    "UpdateTarget",
]
