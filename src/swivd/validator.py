"""Independent fail-closed validation for SWIVD specifications and runs.

This module intentionally uses only the Python standard library and does not
import the collector, analytics, renderer, or shared IO helpers.  It is the
independent replay boundary for the files that those components produce.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


class ValidationError(ValueError):
    """Raised when a specification or run fails a mandatory validation gate."""


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_SPEC = PROJECT_ROOT / "PROJECT_SPEC.json"
CANONICAL_CONTRACT = PROJECT_ROOT / "PROJECT_CONTRACT.md"

EXPECTED_SPEC_SHA256 = "c872516118bda0f2b7429d384f6b4ab97d0105a81ba3e416faf5785bfd9a0b98"
EXPECTED_CONTRACT_SHA256 = "b27a8ac50a7c924cad9c9cbd8a326763a9cb4039955c1eaee617e97aee2e5b8d"
LEGACY_RUNS = {
    "SWIVD-RUN-20260828-001": {
        "manifest_schema": "swivd-run-manifest-v1",
        "manifest_sha256": "2e0e8de64ffa85383b3f163dde0fa9ee27cf008dd9f0352ed43d50b323324066",
        "spec_sha256": "dceb5afff3e6b16430d40193ec77a076c04d8baeeaa23f3bcaea53b358901f51",
        "contract_sha256": "e5ea859cf0439eb4c093bd079762b97e5b2dacc3a7451f1173e6c2fe7631f97f",
    },
    "SWIVD-RUN-20260828-002": {
        "manifest_schema": "swivd-run-manifest-v2",
        "manifest_sha256": "d2a6fca83e2a9d8bb7b8131178dcd0a3c113ce8efeebf57e3ff034ca939badd7",
        "spec_sha256": "6eaa417d36b3fd36176b1e1824e1304a3e12f2abd40750682abb694cf9f37516",
        "contract_sha256": "84c447c0a9f34b6058619f8b043b15893df69763609a2be3af7f4d70fbdd2d4e",
    },
}

PUBLICATION_STATE_RETIRED = "NOT_PROVIDED_FOR_RETIRED_TAXONOMY"
SELECTION_BASIS_RETIRED = "ALL_CLASSIFIED_L1_ROWS_FOR_RETIRED_TAXONOMY"
CONTINUITY_POLICY_RETIRED = "PER_CODE_OBSERVED_INCEPTION_TO_COMMON_END"
NAME_HISTORY_POLICY_RETIRED = "STABLE_TS_CODE_WITH_SOURCE_NAME_HISTORY"
PUBLICATION_STATE_CURRENT = "BINARY_FLAG_PROVIDED"
SELECTION_BASIS_CURRENT = "PUBLISHED_ONLY"
CONTINUITY_POLICY_CURRENT = "COMMON_START_RECTANGLE"
NAME_HISTORY_POLICY_CURRENT = "CONSTANT_NAME_REQUIRED"
PERCENTILE_FORMULA = "count(x <= current) / valid_count * 100"
HTML_SOURCE = "Tushare Pro sw_daily 原始行业指数字段"
HTML_TAXONOMY = "SW2021 L1 / SW2014 L1 独立轴"
MANIFEST_SOURCE = {
    "provider": "Tushare Pro",
    "transport": "HTTPS_POST_NO_REDIRECT",
    "apis": ["trade_cal", "index_classify", "sw_daily"],
}
RUN003_GRANDFATHER_ID = "SWIVD-RUN-20260828-003"
RUN003_GRANDFATHER_MANIFEST_SHA256 = (
    "52c4f055927a2660e79c2ea6bb4dd47d61d973b8477b3254989ebbb528310911"
)
RUN004_GRANDFATHER_ID = "SWIVD-RUN-20260828-004"
RUN004_GRANDFATHER_MANIFEST_SHA256 = (
    "5180fd5eb20e2cd556a5a5066039fba676272d735ae2d91b9b798e59b33daea0"
)
RUN_RE = re.compile(r"^SWIVD-RUN-(\d{8})-(\d{3})$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{8}$")

REQUIRED_FILES = {
    "inputs/normalized/classification_sw2021.csv",
    "inputs/normalized/classification_sw2014.csv",
    "inputs/normalized/trade_calendar.csv",
    "inputs/normalized/sw_daily_sw2021.csv",
    "inputs/normalized/sw_daily_sw2014.csv",
    "tables/sw2021_current.csv",
    "tables/sw2021_history.csv",
    "tables/sw2014_archive.csv",
    "tables/sw2014_history.csv",
    "dashboard.html",
    "audit.json",
    "manifest.json",
    "SHA256SUMS",
    "reports/adversarial_review.md",
}

SUMMARY_FIELDS = (
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
)
HISTORY_FIELDS = (
    "taxonomy",
    "index_code",
    "industry_name",
    "trade_date",
    "close",
    "pe",
    "pb",
    "is_pub",
)
SW2014_HISTORY_FIELDS = (
    "taxonomy",
    "index_code",
    "industry_name",
    "trade_date",
    "close",
    "pe",
    "pb",
    "source_name",
    "is_pub",
)
CLASSIFICATION_REQUIRED = {
    "index_code",
    "industry_name",
    "level",
    "is_pub",
}
CLASSIFICATION_SEMANTIC_REQUIRED = {"publication_state", "selection_basis"}
CALENDAR_REQUIRED = {"exchange", "cal_date", "is_open", "pretrade_date"}
SW_DAILY_REQUIRED = {"ts_code", "trade_date", "name", "close", "pe", "pb"}
NORMALIZED_CLASSIFICATION_FIELDS = (
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
NORMALIZED_CALENDAR_FIELDS = ("exchange", "cal_date", "is_open", "pretrade_date")
NORMALIZED_DAILY_FIELDS = (
    "taxonomy",
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
RAW_ENDPOINT_FIELDS = {
    "trade_cal": ("exchange", "cal_date", "is_open", "pretrade_date"),
    "index_classify": (
        "index_code",
        "industry_name",
        "parent_code",
        "level",
        "industry_code",
        "is_pub",
        "src",
    ),
    "sw_daily": (
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
    ),
}
RAW_NUMERIC_FIELDS = {
    "trade_cal": {"is_open"},
    "index_classify": {"is_pub"},
    "sw_daily": {
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
    },
}
ALLOWED_LABELS = {
    "",
    "历史极低位",
    "历史较低位",
    "历史中位",
    "历史较高位",
    "历史极高位",
}
FORBIDDEN_VALUATION_WORDS = ("低估", "高估", "undervalued", "overvalued")
PICKLE_SUFFIXES = {".pkl", ".pickle", ".pck", ".joblib"}

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?:tushare[_-]?token|api[_-]?token|access[_-]?token|auth(?:orization)?|"
    r"bearer|password|passwd|secret)\s*[\"']?\s*[:=]\s*[\"']?"
    r"([A-Za-z0-9._~+/=-]{12,})"
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PREFIXED_SECRET_RE = re.compile(
    r"\b(?:sk|ghp|github_pat|AKIA|ASIA)[-_][A-Za-z0-9_-]{12,}\b"
)


def _fail(message: str) -> None:
    raise ValidationError(message)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValidationError(f"unreadable UTF-8 JSON: {path}") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=lambda value: _fail(f"non-finite JSON number: {value}"),
        )
    except ValidationError:
        raise
    except (json.JSONDecodeError, InvalidOperation) as exc:
        raise ValidationError(f"invalid JSON: {path}") from exc


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be an object")
    return value


def _require_exact(value: Any, expected: Any, label: str) -> None:
    if value != expected or type(value) is not type(expected):
        _fail(f"{label} must equal {expected!r}, got {value!r}")


def validate_spec(path: str | Path) -> dict[str, Any]:
    """Validate the frozen v3 project specification and return its parsed object."""

    target = Path(path)
    if not target.is_file() or target.is_symlink():
        _fail("spec must be a regular non-symlink file")
    spec = _require_mapping(_load_json(target), "spec")
    if spec.get("schema_version") in {
        "swivd-project-spec-v4",
        "swivd-project-spec-v4.1",
        "swivd-project-spec-v4.2",
        "swivd-project-spec-v4.3",
    }:
        from .v2_validator import validate_spec_v4

        return validate_spec_v4(target)

    _require_exact(spec.get("schema_version"), "swivd-project-spec-v3", "schema_version")
    _require_exact(spec.get("contract_version"), "swivd-contract-v1.2.0", "contract_version")
    _require_exact(spec.get("project_id"), "sw-industry-valuation-dashboard", "project_id")
    _require_exact(spec.get("decision_id"), "GOV-20260830-001", "decision_id")
    _require_exact(
        spec.get("semantic_successor_id"),
        "GOV-20260830-001-SW2014-SEMANTICS-AUTHORIZED",
        "semantic_successor_id",
    )
    _require_exact(
        spec.get("name_history_successor_id"),
        "GOV-20260830-001-NAME-HISTORY-AUTHORIZED",
        "name_history_successor_id",
    )
    _require_exact(
        spec.get("canonical_root"),
        "/Users/chenyipeng/Desktop/Codex/Stock/sw_industry_valuation_dashboard",
        "canonical_root",
    )
    _require_exact(
        spec.get("status_defaults"),
        {
            "research_grade": "RESEARCH_ONLY",
            "decision_eligible": False,
            "production_approved": False,
        },
        "status_defaults",
    )

    network = _require_mapping(spec.get("network"), "network")
    _require_exact(network.get("base_url"), "https://api.tushare.pro", "network.base_url")
    _require_exact(network.get("allow_redirects"), False, "network.allow_redirects")
    _require_exact(
        network.get("allowed_apis"),
        ["trade_cal", "index_classify", "sw_daily"],
        "network.allowed_apis",
    )

    axes = _require_mapping(spec.get("axes"), "axes")
    _require_exact(set(axes), {"SW2021", "SW2014"}, "axes keys")
    _require_exact(axes["SW2021"].get("level"), "L1", "axes.SW2021.level")
    _require_exact(axes["SW2021"].get("expected_classification_count"), 31, "SW2021 count")
    _require_exact(axes["SW2021"].get("query_start"), "20211213", "SW2021 start")
    _require_exact(
        axes["SW2021"].get("publication_flag_rule"),
        "REQUIRED_BINARY",
        "axes.SW2021.publication_flag_rule",
    )
    _require_exact(
        axes["SW2021"].get("selection_basis"),
        "PUBLISHED_ONLY",
        "axes.SW2021.selection_basis",
    )
    _require_exact(
        axes["SW2021"].get("continuity_policy"),
        "COMMON_START_RECTANGLE",
        "axes.SW2021.continuity_policy",
    )
    _require_exact(axes["SW2021"].get("identity_key"), "ts_code", "axes.SW2021.identity_key")
    _require_exact(
        axes["SW2021"].get("name_history_policy"),
        "CONSTANT_NAME_REQUIRED",
        "axes.SW2021.name_history_policy",
    )
    _require_exact(axes["SW2014"].get("level"), "L1", "axes.SW2014.level")
    _require_exact(axes["SW2014"].get("expected_classification_count"), 28, "SW2014 count")
    _require_exact(axes["SW2014"].get("query_start"), "20140101", "SW2014 start")
    _require_exact(
        axes["SW2014"].get("publication_flag_rule"),
        "REQUIRED_NULL_FOR_RETIRED_TAXONOMY",
        "axes.SW2014.publication_flag_rule",
    )
    _require_exact(
        axes["SW2014"].get("publication_state"),
        PUBLICATION_STATE_RETIRED,
        "axes.SW2014.publication_state",
    )
    _require_exact(
        axes["SW2014"].get("selection_basis"),
        SELECTION_BASIS_RETIRED,
        "axes.SW2014.selection_basis",
    )
    _require_exact(
        axes["SW2014"].get("continuity_policy"),
        CONTINUITY_POLICY_RETIRED,
        "axes.SW2014.continuity_policy",
    )
    _require_exact(axes["SW2014"].get("allow_late_first_observation"), True, "SW2014 late start")
    _require_exact(axes["SW2014"].get("require_common_end_date"), True, "SW2014 common end")
    _require_exact(
        axes["SW2014"].get("require_no_internal_open_date_gaps"),
        True,
        "SW2014 internal continuity",
    )
    _require_exact(axes["SW2014"].get("identity_key"), "ts_code", "axes.SW2014.identity_key")
    _require_exact(
        axes["SW2014"].get("source_name_field"), "name", "axes.SW2014.source_name_field"
    )
    _require_exact(
        axes["SW2014"].get("name_history_policy"),
        NAME_HISTORY_POLICY_RETIRED,
        "axes.SW2014.name_history_policy",
    )
    _require_exact(
        axes["SW2014"].get("require_end_name_match_classification"),
        True,
        "axes.SW2014.require_end_name_match_classification",
    )
    _require_exact(
        axes["SW2014"].get("preserve_source_name"),
        True,
        "axes.SW2014.preserve_source_name",
    )

    normalized_classification = _require_mapping(
        spec.get("normalized_classification"), "normalized_classification"
    )
    _require_exact(
        normalized_classification.get("derived_fields"),
        ["publication_state", "selection_basis"],
        "normalized_classification.derived_fields",
    )
    _require_exact(
        normalized_classification.get("preserve_source_nulls"),
        True,
        "normalized_classification.preserve_source_nulls",
    )

    normalized_history = _require_mapping(spec.get("normalized_history"), "normalized_history")
    _require_exact(
        normalized_history.get("identity_field"), "index_code", "normalized_history.identity_field"
    )
    _require_exact(
        normalized_history.get("display_name_field"),
        "industry_name",
        "normalized_history.display_name_field",
    )
    _require_exact(
        normalized_history.get("source_name_scope"),
        "SW2014_ONLY",
        "normalized_history.source_name_scope",
    )
    _require_exact(
        normalized_history.get("source_name_field"),
        "source_name",
        "normalized_history.source_name_field",
    )
    _require_exact(
        normalized_history.get("source_name_origin"),
        "sw_daily.name",
        "normalized_history.source_name_origin",
    )
    _require_exact(
        normalized_history.get("preserve_source_name"),
        True,
        "normalized_history.preserve_source_name",
    )
    name_schema = _require_mapping(
        normalized_history.get("sw2014_name_history_schema"),
        "normalized_history.sw2014_name_history_schema",
    )
    _require_exact(
        name_schema.get("segments"),
        ["index_code", "source_name", "first_date", "last_date", "row_count"],
        "normalized_history.sw2014_name_history_schema.segments",
    )

    valuation = _require_mapping(spec.get("valuation"), "valuation")
    _require_exact(valuation.get("source_fields"), ["pe", "pb"], "valuation.source_fields")
    _require_exact(
        valuation.get("percentile_method"), "EMPIRICAL_CDF_LE", "valuation.percentile_method"
    )
    _require_exact(valuation.get("include_current"), True, "valuation.include_current")
    _require_exact(valuation.get("missing_fill"), "NONE", "valuation.missing_fill")

    returns = _require_mapping(spec.get("returns"), "returns")
    _require_exact(returns.get("price_field"), "close", "returns.price_field")
    _require_exact(returns.get("compound_pct_change"), False, "returns.compound_pct_change")

    output = _require_mapping(spec.get("output"), "output")
    _require_exact(output.get("allow_pickle"), False, "output.allow_pickle")
    _require_exact(output.get("allow_remote_assets"), False, "output.allow_remote_assets")
    _require_exact(output.get("auto_open_browser"), False, "output.auto_open_browser")
    _require_exact(output.get("immutable_runs"), True, "output.immutable_runs")

    digest = _sha256(target)
    if digest != EXPECTED_SPEC_SHA256:
        _fail(f"spec byte hash drift: {digest}")
    return dict(spec)


def _safe_relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        _fail(f"invalid {label}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        _fail(f"unsafe {label}: {value!r}")
    normalized = path.as_posix()
    if normalized != value:
        _fail(f"non-canonical {label}: {value!r}")
    return value


def _inventory(root: Path) -> dict[str, Path]:
    if root.is_symlink() or not root.is_dir():
        _fail("run_dir must be a regular directory, not a symlink")
    inventory: dict[str, Path] = {}
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            candidate = current_path / directory
            if candidate.is_symlink():
                _fail(f"symlink directory forbidden: {candidate.relative_to(root)}")
        for filename in files:
            candidate = current_path / filename
            relative = candidate.relative_to(root).as_posix()
            _safe_relative(relative, "run path")
            if candidate.is_symlink() or not candidate.is_file():
                _fail(f"non-regular file forbidden: {relative}")
            inventory[relative] = candidate
    return inventory


def _parse_manifest_artifacts(manifest: Mapping[str, Any]) -> dict[str, tuple[str, int]]:
    raw = manifest.get("artifacts")
    if not isinstance(raw, list):
        _fail("manifest.artifacts must be a list")
    result: dict[str, tuple[str, int]] = {}
    for index, item in enumerate(raw):
        record = _require_mapping(item, f"manifest.artifacts[{index}]")
        if set(record) != {"path", "sha256", "bytes"}:
            _fail(f"manifest artifact record has schema drift at index {index}")
        relative = _safe_relative(record.get("path"), "artifact path")
        digest = record.get("sha256")
        size = record.get("bytes")
        if not isinstance(digest, str) or not SHA_RE.fullmatch(digest):
            _fail(f"invalid artifact sha256: {relative}")
        if type(size) is not int or size < 0:
            _fail(f"invalid artifact byte count: {relative}")
        if relative in result:
            _fail(f"duplicate artifact path: {relative}")
        result[relative] = (digest, size)
    return result


def _parse_sha256sums(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValidationError("SHA256SUMS is not readable UTF-8") from exc
    if not lines:
        _fail("SHA256SUMS is empty")
    result: dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            _fail(f"invalid SHA256SUMS line {line_number}")
        relative = _safe_relative(match.group(2), "SHA256SUMS path")
        if relative in result:
            _fail(f"duplicate SHA256SUMS path: {relative}")
        result[relative] = match.group(1)
    return result


def _validate_hash_closure(
    root: Path, inventory: Mapping[str, Path], manifest: Mapping[str, Any]
) -> None:
    artifacts = _parse_manifest_artifacts(manifest)
    expected_artifacts = set(inventory) - {"manifest.json", "SHA256SUMS"}
    if set(artifacts) != expected_artifacts:
        missing = sorted(expected_artifacts - set(artifacts))
        extra = sorted(set(artifacts) - expected_artifacts)
        _fail(f"manifest artifact closure mismatch; missing={missing}, extra={extra}")
    for relative, (expected_hash, expected_size) in artifacts.items():
        target = inventory[relative]
        if target.stat().st_size != expected_size:
            _fail(f"artifact byte count drift: {relative}")
        if _sha256(target) != expected_hash:
            _fail(f"artifact hash drift: {relative}")

    sums = _parse_sha256sums(root / "SHA256SUMS")
    expected_sums = set(inventory) - {"SHA256SUMS"}
    if set(sums) != expected_sums:
        missing = sorted(expected_sums - set(sums))
        extra = sorted(set(sums) - expected_sums)
        _fail(f"SHA256SUMS closure mismatch; missing={missing}, extra={extra}")
    for relative, expected_hash in sums.items():
        if _sha256(inventory[relative]) != expected_hash:
            _fail(f"SHA256SUMS hash drift: {relative}")


def _read_csv(path: Path, *, exact_header: Sequence[str] | None = None) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            header = reader.fieldnames
            if header is None or any(not value for value in header):
                _fail(f"missing or blank CSV header: {path.name}")
            if len(header) != len(set(header)):
                _fail(f"duplicate CSV header: {path.name}")
            if exact_header is not None and tuple(header) != tuple(exact_header):
                _fail(f"CSV schema drift in {path.name}: {header}")
            rows: list[dict[str, str]] = []
            for line_number, row in enumerate(reader, start=2):
                if None in row:
                    _fail(f"extra CSV field in {path.name}:{line_number}")
                normalized = {str(key): str(value) for key, value in row.items()}
                if not any(value.strip() for value in normalized.values()):
                    _fail(f"blank CSV row in {path.name}:{line_number}")
                rows.append(normalized)
            return rows
    except ValidationError:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValidationError(f"unreadable CSV: {path}") from exc


def _require_columns(rows_path: Path, required: set[str]) -> list[dict[str, str]]:
    try:
        with rows_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            header = reader.fieldnames
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValidationError(f"unreadable CSV: {rows_path}") from exc
    if header is None or not required.issubset(set(header)):
        _fail(f"missing required columns in {rows_path.name}: {sorted(required - set(header or []))}")
    return _read_csv(rows_path)


def _unique(rows: Sequence[Mapping[str, str]], keys: Sequence[str], label: str) -> None:
    observed: set[tuple[str, ...]] = set()
    for row_number, row in enumerate(rows, start=2):
        key = tuple(row.get(field, "").strip() for field in keys)
        if any(not value for value in key):
            _fail(f"blank primary key in {label}:{row_number}")
        if key in observed:
            _fail(f"duplicate primary key in {label}: {key}")
        observed.add(key)


def _taxonomy(row: Mapping[str, str]) -> str:
    values = {
        value.strip()
        for key in ("taxonomy", "src")
        if (value := row.get(key, "")) and value.strip()
    }
    if len(values) != 1:
        _fail("row must carry one unambiguous taxonomy/src value")
    return next(iter(values))


def _validate_date(value: str, label: str) -> str:
    if not DATE_RE.fullmatch(value):
        _fail(f"invalid {label}: {value!r}")
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise ValidationError(f"invalid {label}: {value!r}") from exc
    return value


def _finite_decimal(value: str, label: str, *, allow_blank: bool = True) -> Decimal | None:
    stripped = value.strip()
    if not stripped and allow_blank:
        return None
    try:
        number = Decimal(stripped)
    except InvalidOperation as exc:
        raise ValidationError(f"invalid decimal {label}: {value!r}") from exc
    if not number.is_finite():
        _fail(f"non-finite decimal {label}")
    return number


def _require_decimal_match(
    actual: str, expected: Decimal | None, label: str
) -> None:
    parsed = _finite_decimal(actual, label, allow_blank=True)
    if parsed != expected:
        _fail(f"independently derived numeric mismatch for {label}: {parsed!r} != {expected!r}")


def _history_label(percentile: Decimal | None) -> str:
    if percentile is None:
        return ""
    if percentile < 20:
        return "历史极低位"
    if percentile < 40:
        return "历史较低位"
    if percentile < 60:
        return "历史中位"
    if percentile < 80:
        return "历史较高位"
    return "历史极高位"


def _independent_metric(
    series: Sequence[Mapping[str, str]], field: str, as_of: str
) -> dict[str, Any]:
    current_rows = [row for row in series if row["trade_date"].strip() == as_of]
    if len(current_rows) != 1:
        _fail(f"independent {field} replay needs exactly one as-of row")
    current_raw = _finite_decimal(
        current_rows[0][field], f"normalized current {field}", allow_blank=True
    )
    current_value = current_raw if current_raw is not None and current_raw > 0 else None
    dated: list[tuple[str, Decimal]] = []
    for row in series:
        date = row["trade_date"].strip()
        if date > as_of:
            continue
        value = _finite_decimal(row[field], f"normalized {field}", allow_blank=True)
        if value is not None and value > 0:
            dated.append((date, value))
    dated.sort(key=lambda item: item[0])
    valid_count = len(dated)
    status = (
        "CURRENT_INVALID"
        if current_value is None
        else "HISTORY_INSUFFICIENT"
        if valid_count < 252
        else "OK"
    )
    tie_count = 0
    tie_ratio: Decimal | None = None
    percentile: Decimal | None = None
    if current_value is not None and valid_count:
        tie_count = sum(value == current_value for _, value in dated)
        less_or_equal = sum(value <= current_value for _, value in dated)
        with localcontext() as context:
            context.prec = 50
            tie_ratio = Decimal(tie_count) / Decimal(valid_count)
            if status == "OK":
                percentile = Decimal(less_or_equal) * Decimal(100) / Decimal(valid_count)
    return {
        "current": current_raw,
        "percentile": percentile,
        "valid_count": valid_count,
        "tie_count": tie_count,
        "tie_ratio": tie_ratio,
        "first_valid_date": dated[0][0] if dated else "",
        "last_valid_date": dated[-1][0] if dated else "",
        "label": _history_label(percentile),
        "status": status,
    }


def _calendar_predecessors(
    calendar: Sequence[Mapping[str, str]],
) -> dict[str, str | None]:
    predecessors: dict[str, str | None] = {}
    last_open: str | None = None
    for row in sorted(calendar, key=lambda item: item["cal_date"].strip()):
        if row["is_open"].strip() != "1":
            continue
        date = row["cal_date"].strip()
        previous = row["pretrade_date"].strip() or None
        if previous is not None:
            _validate_date(previous, "trade_calendar.pretrade_date")
            if previous >= date:
                _fail("trade_calendar.pretrade_date must precede cal_date")
            if last_open is not None and previous != last_open:
                _fail("trade_calendar.pretrade_date disagrees with the open-date sequence")
        predecessors[date] = previous
        last_open = date
    return predecessors


def _independent_returns(
    series: Sequence[Mapping[str, str]],
    calendar: Sequence[Mapping[str, str]],
    as_of: str,
) -> dict[str, tuple[Decimal | None, str]]:
    predecessors = _calendar_predecessors(calendar)
    if as_of not in predecessors:
        _fail("independent return replay cannot find as_of in the open calendar")
    close_by_date = {
        row["trade_date"].strip(): _finite_decimal(
            row["close"], "normalized close", allow_blank=True
        )
        for row in series
        if row["trade_date"].strip() <= as_of
    }

    def walk_back(steps: int) -> str | None:
        current = as_of
        seen = {current}
        for _ in range(steps):
            previous = predecessors.get(current)
            if previous is None or previous in seen:
                return None
            seen.add(previous)
            current = previous
        return current

    as_of_date = datetime.strptime(as_of, "%Y%m%d")

    def previous_period(period: str) -> str | None:
        current = as_of
        seen = {current}
        while True:
            previous = predecessors.get(current)
            if previous is None or previous in seen:
                return None
            seen.add(previous)
            parsed = datetime.strptime(previous, "%Y%m%d")
            if period == "month":
                if (parsed.year, parsed.month) == (as_of_date.year, as_of_date.month):
                    current = previous
                    continue
                expected = (
                    (as_of_date.year - 1, 12)
                    if as_of_date.month == 1
                    else (as_of_date.year, as_of_date.month - 1)
                )
                return previous if (parsed.year, parsed.month) == expected else None
            if parsed.year == as_of_date.year:
                current = previous
                continue
            return previous if parsed.year == as_of_date.year - 1 else None

    anchors = {
        "return_5d": walk_back(5),
        "return_mtd": previous_period("month"),
        "return_ytd": previous_period("year"),
    }
    current = close_by_date.get(as_of)
    result: dict[str, tuple[Decimal | None, str]] = {}
    for field, anchor in anchors.items():
        anchor_close = close_by_date.get(anchor) if anchor is not None else None
        if current is None or current <= 0:
            value, status = None, "CURRENT_INVALID"
        elif anchor is None or anchor_close is None or anchor_close <= 0:
            value, status = None, "HISTORY_INSUFFICIENT"
        else:
            with localcontext() as context:
                context.prec = 50
                value = current / anchor_close - Decimal(1)
            status = "OK"
        result[field] = (value, status)
    return result


def _validate_independent_summary(
    *,
    taxonomy: str,
    summary_rows: Sequence[Mapping[str, str]],
    daily_rows: Sequence[Mapping[str, str]],
    calendar: Sequence[Mapping[str, str]],
    selected_codes: set[str],
    as_of: str,
) -> None:
    summary_by_code = {row["index_code"].strip(): row for row in summary_rows}
    daily_by_code: dict[str, list[Mapping[str, str]]] = {
        code: [] for code in selected_codes
    }
    for row in daily_rows:
        code = row["ts_code"].strip()
        if code in daily_by_code:
            daily_by_code[code].append(row)
    for code in sorted(selected_codes):
        row = summary_by_code.get(code)
        if row is None:
            _fail(f"{taxonomy} summary omits selected code {code}")
        series = daily_by_code[code]
        metric_states: list[str] = []
        for field in ("pe", "pb"):
            metric = _independent_metric(series, field, as_of)
            _require_decimal_match(row[field], metric["current"], f"{taxonomy}.{code}.{field}")
            _require_decimal_match(
                row[f"{field}_percentile"],
                metric["percentile"],
                f"{taxonomy}.{code}.{field}_percentile",
            )
            for suffix in ("valid_count", "tie_count"):
                try:
                    actual_count = int(row[f"{field}_{suffix}"].strip())
                except ValueError as exc:
                    raise ValidationError(
                        f"invalid independently checked count {taxonomy}.{code}.{field}_{suffix}"
                    ) from exc
                if actual_count != metric[suffix]:
                    _fail(f"independently derived count mismatch for {taxonomy}.{code}.{field}_{suffix}")
            _require_decimal_match(
                row[f"{field}_tie_ratio"],
                metric["tie_ratio"],
                f"{taxonomy}.{code}.{field}_tie_ratio",
            )
            for suffix in ("first_valid_date", "last_valid_date"):
                if row[f"{field}_{suffix}"] != metric[suffix]:
                    _fail(f"independently derived date mismatch for {taxonomy}.{code}.{field}_{suffix}")
            if row[f"{field}_label"] != metric["label"]:
                _fail(f"independently derived label mismatch for {taxonomy}.{code}.{field}")
            metric_states.append(metric["status"])
        expected_valuation_state = (
            "OK"
            if metric_states == ["OK", "OK"]
            else f"PE={metric_states[0]};PB={metric_states[1]}"
        )
        if row["valuation_state"] != expected_valuation_state:
            _fail(f"independently derived valuation_state mismatch for {taxonomy}.{code}")

        returns = _independent_returns(series, calendar, as_of)
        return_states: list[str] = []
        for field in ("return_5d", "return_mtd", "return_ytd"):
            value, status = returns[field]
            _require_decimal_match(row[field], value, f"{taxonomy}.{code}.{field}")
            return_states.append(status)
        expected_return_state = (
            "OK" if return_states == ["OK", "OK", "OK"] else ";".join(return_states)
        )
        if row["return_state"] != expected_return_state:
            _fail(f"independently derived return_state mismatch for {taxonomy}.{code}")


def _validate_summary_metrics(rows: Sequence[Mapping[str, str]], label: str) -> None:
    for row_number, row in enumerate(rows, start=2):
        for field in ("pe_label", "pb_label"):
            value = row[field].strip()
            if value not in ALLOWED_LABELS:
                _fail(f"invalid history label in {label}:{row_number}:{field}")
        for forbidden in FORBIDDEN_VALUATION_WORDS:
            if forbidden.lower() in " ".join(row.values()).lower():
                _fail(f"forbidden valuation wording in {label}:{row_number}: {forbidden}")

        for metric in ("pe", "pb"):
            current = _finite_decimal(row[metric], f"{label}.{metric}")
            percentile = _finite_decimal(
                row[f"{metric}_percentile"], f"{label}.{metric}_percentile"
            )
            count_text = row[f"{metric}_valid_count"].strip()
            tie_text = row[f"{metric}_tie_count"].strip()
            ratio = _finite_decimal(row[f"{metric}_tie_ratio"], f"{label}.{metric}_tie_ratio")
            if percentile is not None:
                if current is None or current <= 0:
                    _fail(f"percentile present for invalid {metric} in {label}:{row_number}")
                if percentile < 0 or percentile > 100:
                    _fail(f"percentile out of range in {label}:{row_number}")
                try:
                    valid_count = int(count_text)
                    tie_count = int(tie_text)
                except ValueError as exc:
                    raise ValidationError(f"invalid count in {label}:{row_number}") from exc
                if valid_count < 252 or tie_count < 1 or tie_count > valid_count:
                    _fail(f"invalid history counts in {label}:{row_number}:{metric}")
                if ratio is None or ratio < 0 or ratio > 1:
                    _fail(f"invalid tie ratio in {label}:{row_number}:{metric}")
                for suffix in ("first_valid_date", "last_valid_date"):
                    _validate_date(row[f"{metric}_{suffix}"], f"{label}.{metric}_{suffix}")
            elif row[f"{metric}_label"].strip():
                _fail(f"label present without percentile in {label}:{row_number}:{metric}")

        for field in ("return_5d", "return_mtd", "return_ytd"):
            _finite_decimal(row[field], f"{label}.{field}")
        if not row["valuation_state"].strip() or not row["return_state"].strip():
            _fail(f"missing required state in {label}:{row_number}")


def _legacy_run_version(root: Path, manifest: Mapping[str, Any]) -> str | None:
    """Identify one immutable grandfathered run by path, fields, and manifest bytes."""

    run_id = manifest.get("run_id")
    expected = LEGACY_RUNS.get(str(run_id))
    if expected is None:
        return None
    try:
        exact_path = root.resolve() == (PROJECT_ROOT / "output" / "runs" / str(run_id)).resolve()
        manifest_hash = _sha256(root / "manifest.json")
    except OSError:
        return None
    if not (
        exact_path
        and manifest_hash == expected["manifest_sha256"]
        and manifest.get("schema_version") == expected["manifest_schema"]
        and manifest.get("spec_sha256") == expected["spec_sha256"]
        and manifest.get("contract_sha256") == expected["contract_sha256"]
        and "name_history_successor_id" not in manifest
        and isinstance(manifest.get("axes"), Mapping)
        and manifest["axes"].get("SW2021") == "PASS"
        and manifest["axes"].get("SW2014") == "BLOCKED"
        and manifest.get("execution_status") == "COMPLETED"
        and manifest.get("artifact_publish_state") == "LOCAL_RESEARCH_CANDIDATE_PARTIAL"
    ):
        return None
    return str(expected["manifest_schema"])


def _is_exact_run003_grandfather(root: Path, manifest: Mapping[str, Any]) -> bool:
    """Recognize one immutable v3 historical PARTIAL without changing its schema."""

    if (
        manifest.get("schema_version") != "swivd-run-manifest-v3"
        or manifest.get("run_id") != RUN003_GRANDFATHER_ID
    ):
        return False
    try:
        return (
            root.resolve()
            == (PROJECT_ROOT / "output" / "runs" / RUN003_GRANDFATHER_ID).resolve()
            and _sha256(root / "manifest.json")
            == RUN003_GRANDFATHER_MANIFEST_SHA256
        )
    except OSError:
        return False


def _validate_open_date_continuity(
    rows: Sequence[Mapping[str, str]],
    *,
    codes: set[str],
    code_field: str,
    date_field: str,
    open_dates: Sequence[str],
    common_end: str,
    label: str,
    common_start: str | None,
) -> None:
    """Validate observed rows from each code's permitted inception through one common end."""

    calendar = tuple(sorted(set(open_dates)))
    calendar_set = set(calendar)
    grouped: dict[str, set[str]] = {code: set() for code in codes}
    for row in rows:
        code = row[code_field].strip()
        date = row[date_field].strip()
        if code in grouped:
            grouped[code].add(date)

    for code in sorted(codes):
        observed = grouped[code]
        if not observed or common_end not in observed:
            _fail(f"{label} lacks the common end date for {code}")
        first = min(observed)
        if common_start is not None and first != common_start:
            _fail(f"{label} does not start at the frozen common start for {code}")
        if first not in calendar_set:
            _fail(f"{label} starts on a non-open date for {code}")
        expected = {date for date in calendar if first <= date <= common_end}
        if observed != expected:
            missing = sorted(expected - observed)
            extra = sorted(observed - expected)
            _fail(
                f"{label} has an internal open-date gap or non-calendar row for {code}; "
                f"missing={missing[:3]}, extra={extra[:3]}"
            )


def _recompute_sw2014_name_history(
    rows: Sequence[Mapping[str, str]],
    *,
    codes: set[str],
    classification_names: Mapping[str, str],
    common_end: str,
) -> dict[str, Any]:
    grouped: dict[str, list[tuple[str, str]]] = {code: [] for code in codes}
    for row in rows:
        code = row["ts_code"].strip()
        if code in grouped:
            source_name = row["name"]
            if not source_name.strip():
                _fail(f"SW2014 sw_daily has an empty source name for {code}")
            grouped[code].append((row["trade_date"].strip(), source_name))

    segments: list[dict[str, Any]] = []
    renamed_codes: list[str] = []
    for code in sorted(codes):
        observations = sorted(grouped[code])
        if not observations:
            _fail(f"SW2014 name history is empty for {code}")
        end_rows = [name for date, name in observations if date == common_end]
        if len(end_rows) != 1 or end_rows[0] != classification_names[code]:
            _fail(f"SW2014 common-end source name conflicts with classification for {code}")

        code_segments: list[dict[str, Any]] = []
        segment_name = observations[0][1]
        segment_first = observations[0][0]
        segment_last = observations[0][0]
        segment_count = 1
        for date, source_name in observations[1:]:
            if source_name == segment_name:
                segment_last = date
                segment_count += 1
                continue
            code_segments.append(
                {
                    "index_code": code,
                    "source_name": segment_name,
                    "first_date": segment_first,
                    "last_date": segment_last,
                    "row_count": segment_count,
                }
            )
            segment_name = source_name
            segment_first = date
            segment_last = date
            segment_count = 1
        code_segments.append(
            {
                "index_code": code,
                "source_name": segment_name,
                "first_date": segment_first,
                "last_date": segment_last,
                "row_count": segment_count,
            }
        )
        if len(code_segments) > 1:
            renamed_codes.append(code)
        segments.extend(code_segments)

    return {
        "code_count": len(codes),
        "renamed_code_count": len(renamed_codes),
        "renamed_codes": renamed_codes,
        "segment_count": len(segments),
        "segments": segments,
    }


def _recompute_ohlc_ordering(
    rows: Sequence[Mapping[str, str]], *, label: str
) -> dict[str, Any]:
    anomalies: list[dict[str, Any]] = []
    fully_observed = 0
    unchecked_missing = 0
    for position, row in enumerate(rows, start=1):
        close = _finite_decimal(
            row["close"], f"{label}[{position}].close", allow_blank=False
        )
        assert close is not None
        if close <= 0:
            _fail(f"{label}[{position}].close must be strictly positive")
        optional = {
            field: _finite_decimal(
                row[field], f"{label}[{position}].{field}", allow_blank=True
            )
            for field in ("open", "low", "high")
        }
        if any(value is not None and value <= 0 for value in optional.values()):
            _fail(f"{label}[{position}] present OHLC values must be strictly positive")
        if any(value is None for value in optional.values()):
            unchecked_missing += 1
            continue
        fully_observed += 1
        open_value = optional["open"]
        low = optional["low"]
        high = optional["high"]
        assert open_value is not None and low is not None and high is not None
        anomaly_codes: list[str] = []
        if high < low:
            anomaly_codes.append("HIGH_BELOW_LOW")
        if high < open_value:
            anomaly_codes.append("HIGH_BELOW_OPEN")
        if high < close:
            anomaly_codes.append("HIGH_BELOW_CLOSE")
        if low > open_value:
            anomaly_codes.append("LOW_ABOVE_OPEN")
        if low > close:
            anomaly_codes.append("LOW_ABOVE_CLOSE")
        if anomaly_codes:
            anomalies.append(
                {
                    "ts_code": row["ts_code"].strip(),
                    "trade_date": row["trade_date"].strip(),
                    "anomaly_codes": sorted(anomaly_codes),
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


def _validate_csvs(
    root: Path, manifest: Mapping[str, Any]
) -> tuple[
    dict[str, list[dict[str, str]]],
    dict[str, Any] | None,
    dict[str, dict[str, int]],
    dict[str, Any],
    dict[str, dict[str, Any]],
]:
    axes = manifest["axes"]
    as_of = str(manifest["as_of"])
    normalized = root / "inputs" / "normalized"
    tables = root / "tables"
    legacy_version = _legacy_run_version(root, manifest)
    is_legacy = legacy_version is not None

    classifications: dict[str, list[dict[str, str]]] = {}
    classification_codes: dict[str, set[str]] = {}
    classification_names: dict[str, dict[str, str]] = {}
    selected_codes: dict[str, set[str]] = {}
    for taxonomy, filename, expected_count in (
        ("SW2021", "classification_sw2021.csv", 31),
        ("SW2014", "classification_sw2014.csv", 28),
    ):
        required_columns = set(CLASSIFICATION_REQUIRED)
        if not is_legacy:
            required_columns.update(CLASSIFICATION_SEMANTIC_REQUIRED)
        rows = (
            _require_columns(normalized / filename, required_columns)
            if is_legacy
            else _read_csv(
                normalized / filename, exact_header=NORMALIZED_CLASSIFICATION_FIELDS
            )
        )
        if rows and not ({"src", "taxonomy"} & set(rows[0])):
            _fail(f"classification {taxonomy} lacks taxonomy/src column")
        for row in rows:
            if _taxonomy(row) != taxonomy or row["level"].strip() != "L1":
                _fail(f"classification version/level isolation failed for {taxonomy}")
            if not row["industry_name"].strip():
                _fail(f"blank industry_name in {taxonomy} classification")
            if taxonomy == "SW2021":
                publication_flag = row["is_pub"].strip()
                if publication_flag not in {"0", "1"}:
                    _fail("SW2021 classification is_pub must be 0 or 1")
                if not is_legacy:
                    expected_state = "PUBLISHED" if publication_flag == "1" else "NOT_PUBLISHED"
                    if row["publication_state"].strip() != expected_state:
                        _fail("SW2021 classification publication_state contradicts is_pub")
                    if row["selection_basis"].strip() != "PUBLISHED_ONLY":
                        _fail("SW2021 classification selection_basis must be PUBLISHED_ONLY")
            else:
                if row["is_pub"].strip():
                    _fail("SW2014 retired classification is_pub must preserve the source null")
                if not is_legacy:
                    if row["publication_state"].strip() != PUBLICATION_STATE_RETIRED:
                        _fail("SW2014 classification publication_state is incorrect")
                    if row["selection_basis"].strip() != SELECTION_BASIS_RETIRED:
                        _fail("SW2014 classification selection_basis is incorrect")
        _unique(rows, ("index_code",), f"classification_{taxonomy}")
        if taxonomy == "SW2021" and len(rows) != expected_count:
            _fail(f"SW2021 classification count must be {expected_count}")
        if taxonomy == "SW2014":
            allowed_counts = (
                {expected_count}
                if axes[taxonomy] == "PASS"
                else {0, expected_count}
                if is_legacy
                else {0}
            )
            if len(rows) not in allowed_counts:
                _fail("SW2014 classification count inconsistent with axis state")
        classifications[taxonomy] = rows
        classification_codes[taxonomy] = {row["index_code"].strip() for row in rows}
        classification_names[taxonomy] = {
            row["index_code"].strip(): row["industry_name"] for row in rows
        }
        if taxonomy == "SW2021":
            selected_codes[taxonomy] = {
                row["index_code"].strip() for row in rows if row["is_pub"].strip() == "1"
            }
        else:
            selected_codes[taxonomy] = set(classification_codes[taxonomy])

    axis_counts = {
        "SW2021": {
            "classification_count": len(classifications["SW2021"]),
            "selected_count": len(selected_codes["SW2021"]),
            "published_count": sum(
                row["is_pub"].strip() == "1" for row in classifications["SW2021"]
            ),
            "unpublished_count": sum(
                row["is_pub"].strip() == "0" for row in classifications["SW2021"]
            ),
            "publication_flag_null_count": sum(
                not row["is_pub"].strip() for row in classifications["SW2021"]
            ),
        },
        "SW2014": {
            "classification_count": len(classifications["SW2014"]),
            "selected_count": len(selected_codes["SW2014"]),
            "publication_flag_null_count": sum(
                not row["is_pub"].strip() for row in classifications["SW2014"]
            ),
        },
    }

    calendar = (
        _require_columns(normalized / "trade_calendar.csv", CALENDAR_REQUIRED)
        if is_legacy
        else _read_csv(
            normalized / "trade_calendar.csv", exact_header=NORMALIZED_CALENDAR_FIELDS
        )
    )
    _unique(calendar, ("exchange", "cal_date"), "trade_calendar")
    for row in calendar:
        _validate_date(row["cal_date"].strip(), "trade_calendar.cal_date")
        if row["exchange"].strip() != "SSE":
            _fail("trade_calendar must contain only the frozen SSE calendar")
        if row["is_open"].strip() not in {"0", "1"}:
            _fail("trade_calendar.is_open must be 0 or 1")
    if not any(
        row["cal_date"].strip() == as_of and row["is_open"].strip() == "1" for row in calendar
    ):
        _fail("as_of is not an explicitly open trade-calendar date")
    legacy_open_dates = [
        row["cal_date"].strip()
        for row in calendar
        if row["is_open"].strip() == "1" and row["cal_date"].strip() < "20211213"
    ]
    if not legacy_open_dates:
        _fail("trade calendar lacks an open day before the SW2021 boundary")
    legacy_end = max(legacy_open_dates)
    all_open_dates = sorted(
        row["cal_date"].strip()
        for row in calendar
        if row["is_open"].strip() == "1"
    )
    calendar_evidence = {
        "state": "PASS",
        "row_count": len(calendar),
        "first_open_date": all_open_dates[0],
        "last_open_date": all_open_dates[-1],
        "legacy_end": legacy_end,
    }
    current_open_dates = sorted(
        row["cal_date"].strip()
        for row in calendar
        if row["is_open"].strip() == "1" and "20211213" <= row["cal_date"].strip() <= as_of
    )

    daily_source_names: dict[str, dict[tuple[str, str], str]] = {}
    daily_rows_by_axis: dict[str, list[dict[str, str]]] = {}
    daily_by_key: dict[str, dict[tuple[str, str], dict[str, str]]] = {}
    sw2014_name_history: dict[str, Any] | None = None
    for taxonomy, filename in (
        ("SW2021", "sw_daily_sw2021.csv"),
        ("SW2014", "sw_daily_sw2014.csv"),
    ):
        rows = (
            _require_columns(normalized / filename, SW_DAILY_REQUIRED)
            if is_legacy
            else _read_csv(normalized / filename, exact_header=NORMALIZED_DAILY_FIELDS)
        )
        daily_rows_by_axis[taxonomy] = rows
        daily_source_names[taxonomy] = {}
        daily_by_key[taxonomy] = {}
        _unique(rows, ("ts_code", "trade_date"), f"sw_daily_{taxonomy}")
        for row in rows:
            code = row["ts_code"].strip()
            date = _validate_date(row["trade_date"].strip(), f"sw_daily_{taxonomy}.trade_date")
            source_name = row["name"]
            if not source_name.strip():
                _fail(f"sw_daily_{taxonomy} has an empty source name")
            if "taxonomy" in row or "src" in row:
                if _taxonomy(row) != taxonomy:
                    _fail(f"cross-version taxonomy marker in sw_daily_{taxonomy}")
            if code not in selected_codes[taxonomy]:
                _fail(f"cross-version, unpublished, or unclassified sw_daily code in {taxonomy}: {code}")
            if taxonomy == "SW2021" and not ("20211213" <= date <= as_of):
                _fail(f"SW2021 date outside its version interval: {date}")
            if taxonomy == "SW2014" and not ("20140101" <= date < "20211213"):
                _fail(f"SW2014 date outside its version interval: {date}")
            if taxonomy == "SW2021" and source_name != classification_names[taxonomy][code]:
                _fail(f"SW2021 source name differs from its constant classification name: {code}")
            daily_source_names[taxonomy][(code, date)] = source_name
            daily_by_key[taxonomy][(code, date)] = row
        if axes[taxonomy] == "PASS":
            expected_end = as_of if taxonomy == "SW2021" else legacy_end
            _validate_open_date_continuity(
                rows,
                codes=selected_codes[taxonomy],
                code_field="ts_code",
                date_field="trade_date",
                open_dates=current_open_dates if taxonomy == "SW2021" else legacy_open_dates,
                common_end=expected_end,
                label=f"sw_daily_{taxonomy}",
                common_start="20211213" if taxonomy == "SW2021" else None,
            )
            if taxonomy == "SW2014":
                sw2014_name_history = _recompute_sw2014_name_history(
                    rows,
                    codes=selected_codes[taxonomy],
                    classification_names=classification_names[taxonomy],
                    common_end=legacy_end,
                )

    ohlc_evidence = {
        taxonomy: _recompute_ohlc_ordering(
            daily_rows_by_axis[taxonomy], label=f"sw_daily_{taxonomy}"
        )
        for taxonomy in ("SW2021", "SW2014")
    }

    current = _read_csv(tables / "sw2021_current.csv", exact_header=SUMMARY_FIELDS)
    archive = _read_csv(tables / "sw2014_archive.csv", exact_header=SUMMARY_FIELDS)
    history_2021 = _read_csv(tables / "sw2021_history.csv", exact_header=HISTORY_FIELDS)
    history_2014 = _read_csv(
        tables / "sw2014_history.csv",
        exact_header=HISTORY_FIELDS if is_legacy else SW2014_HISTORY_FIELDS,
    )

    for taxonomy, rows, expected_count in (
        ("SW2021", current, 31),
        ("SW2014", archive, 28),
    ):
        _unique(rows, ("taxonomy", "index_code", "as_of"), f"{taxonomy}_summary")
        if axes[taxonomy] == "PASS" and len(rows) != expected_count:
            _fail(f"{taxonomy} summary count must be {expected_count} when PASS")
        if axes[taxonomy] == "BLOCKED" and rows:
            _fail(f"{taxonomy} summary must be header-only when BLOCKED")
        for row in rows:
            if row["taxonomy"].strip() != taxonomy:
                _fail(f"cross-version summary row in {taxonomy}")
            code = row["index_code"].strip()
            if code not in classification_codes[taxonomy]:
                _fail(f"unclassified summary code in {taxonomy}")
            if row["industry_name"] != classification_names[taxonomy][code]:
                _fail(f"summary industry identity differs from classification in {taxonomy}")
            row_date = _validate_date(row["as_of"].strip(), f"{taxonomy}.as_of")
            if taxonomy == "SW2021" and row_date != as_of:
                _fail("SW2021 summary as_of differs from manifest")
            if taxonomy == "SW2014" and row_date != legacy_end:
                _fail("SW2014 archive as_of is not the last open day before SW2021")
            if code not in selected_codes[taxonomy]:
                quantitative_fields = (
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
                )
                if any(row[field].strip() for field in quantitative_fields):
                    _fail(f"unpublished summary row carries fabricated metrics in {taxonomy}")
                if row["valuation_state"].strip() != "NA_NOT_PUBLISHED" or row[
                    "return_state"
                ].strip() != "NA_NOT_PUBLISHED":
                    _fail(f"unpublished summary row lacks explicit NA state in {taxonomy}")
            if taxonomy == "SW2014" and axes[taxonomy] == "PASS":
                for metric in ("pe", "pb"):
                    if _finite_decimal(
                        row[f"{metric}_percentile"],
                        f"SW2014.{metric}_percentile",
                        allow_blank=False,
                    ) is None:
                        _fail(f"SW2014 {metric} percentile must be available when axis PASS")
                    try:
                        valid_count = int(row[f"{metric}_valid_count"].strip())
                    except ValueError as exc:
                        raise ValidationError(f"invalid SW2014 {metric}_valid_count") from exc
                    if valid_count < 252:
                        _fail(f"SW2014 {metric} valid_count is below 252 while axis PASS")
        _validate_summary_metrics(rows, f"{taxonomy}_summary")
        if axes[taxonomy] == "PASS" and not is_legacy:
            _validate_independent_summary(
                taxonomy=taxonomy,
                summary_rows=rows,
                daily_rows=daily_rows_by_axis[taxonomy],
                calendar=calendar,
                selected_codes=selected_codes[taxonomy],
                as_of=as_of if taxonomy == "SW2021" else legacy_end,
            )

    for taxonomy, rows in (("SW2021", history_2021), ("SW2014", history_2014)):
        _unique(rows, ("taxonomy", "index_code", "trade_date"), f"{taxonomy}_history")
        if axes[taxonomy] == "PASS" and not rows:
            _fail(f"{taxonomy} history is empty while axis PASS")
        if axes[taxonomy] == "BLOCKED" and rows:
            _fail(f"{taxonomy} history must be header-only when BLOCKED")
        observed_codes: set[str] = set()
        for row in rows:
            if row["taxonomy"].strip() != taxonomy:
                _fail(f"cross-version history row in {taxonomy}")
            code = row["index_code"].strip()
            if code not in selected_codes[taxonomy]:
                _fail(f"unpublished or unclassified history code in {taxonomy}: {code}")
            if row["industry_name"] != classification_names[taxonomy][code]:
                _fail(f"history industry identity differs from classification in {taxonomy}")
            observed_codes.add(code)
            date = _validate_date(row["trade_date"].strip(), f"{taxonomy}.trade_date")
            if taxonomy == "SW2021" and not ("20211213" <= date <= as_of):
                _fail(f"SW2021 history date outside its version interval: {date}")
            if taxonomy == "SW2014" and not ("20140101" <= date < "20211213"):
                _fail(f"SW2014 history date outside its version interval: {date}")
            expected_publication_flag = "1" if taxonomy == "SW2021" else ""
            if row["is_pub"].strip() != expected_publication_flag:
                _fail(f"history is_pub contradicts the publication semantics in {taxonomy}")
            if taxonomy == "SW2014" and not is_legacy:
                source_name = row["source_name"]
                if not source_name.strip():
                    _fail(f"{taxonomy} history has an empty source_name")
                expected_source_name = daily_source_names[taxonomy].get((code, date))
                if expected_source_name is None or source_name != expected_source_name:
                    _fail(
                        f"{taxonomy} history source_name differs from normalized sw_daily.name"
                    )
            if not is_legacy:
                normalized_row = daily_by_key[taxonomy].get((code, date))
                if normalized_row is None:
                    _fail(f"{taxonomy} history key is absent from normalized sw_daily")
                for field in ("close", "pe", "pb"):
                    history_value = _finite_decimal(
                        row[field], f"{taxonomy} history {field}", allow_blank=field != "close"
                    )
                    normalized_value = _finite_decimal(
                        normalized_row[field],
                        f"normalized sw_daily {taxonomy} {field}",
                        allow_blank=field != "close",
                    )
                    if history_value != normalized_value:
                        _fail(
                            f"{taxonomy} history {field} differs from normalized sw_daily"
                        )
            for field in ("close", "pe", "pb"):
                value = _finite_decimal(
                    row[field], f"{taxonomy}.{field}", allow_blank=field != "close"
                )
                if field == "close" and (value is None or value <= 0):
                    _fail(f"history close must be finite and positive in {taxonomy}")
        if axes[taxonomy] == "PASS" and observed_codes != selected_codes[taxonomy]:
            _fail(f"{taxonomy} history does not cover the exact selected whitelist")
        if axes[taxonomy] == "PASS" and not is_legacy:
            history_keys = {
                (row["index_code"].strip(), row["trade_date"].strip()) for row in rows
            }
            if history_keys != set(daily_source_names[taxonomy]):
                _fail(f"{taxonomy} history keys differ from normalized sw_daily keys")
        if axes[taxonomy] == "PASS":
            expected_end = as_of if taxonomy == "SW2021" else legacy_end
            _validate_open_date_continuity(
                rows,
                codes=selected_codes[taxonomy],
                code_field="index_code",
                date_field="trade_date",
                open_dates=current_open_dates if taxonomy == "SW2021" else legacy_open_dates,
                common_end=expected_end,
                label=f"{taxonomy}_history",
                common_start="20211213" if taxonomy == "SW2021" else None,
            )

    return (
        {
            "current_rows": current,
            "sw2021_history": history_2021,
            "sw2014_summary": archive,
            "sw2014_history": history_2014,
        },
        sw2014_name_history,
        axis_counts,
        calendar_evidence,
        ohlc_evidence,
    )


def _decode_raw_response(
    path: Path, endpoint: str, *, allow_schema_blocked: bool = False
) -> tuple[bool, list[dict[str, Any]]]:
    response = _require_mapping(_load_json(path), f"raw response {path.name}")
    code = response.get("code")
    if type(code) is not int:
        _fail(f"raw {endpoint} response code must be an integer")
    if not isinstance(response.get("msg"), str):
        _fail(f"raw {endpoint} response msg must be a string")
    data = response.get("data")
    if code != 0:
        if data is not None and not isinstance(data, Mapping):
            _fail(f"blocked raw {endpoint} response has invalid data container")
        return False, []
    if not isinstance(data, Mapping):
        if allow_schema_blocked:
            return False, []
        _fail(f"raw {endpoint} response.data must be an object")
    fields = data.get("fields")
    items = data.get("items")
    expected_fields = RAW_ENDPOINT_FIELDS[endpoint]
    if fields != list(expected_fields):
        if allow_schema_blocked:
            return False, []
        _fail(f"raw {endpoint} fields differ from the frozen endpoint schema")
    if not isinstance(items, list):
        if allow_schema_blocked:
            return False, []
        _fail(f"raw {endpoint} items must be a list")
    row_limit = {"trade_cal": 6000, "index_classify": 5000, "sw_daily": 4000}[endpoint]
    if len(items) >= row_limit:
        if allow_schema_blocked:
            return False, []
        _fail(f"raw {endpoint} response touched its frozen row limit")
    rows: list[dict[str, Any]] = []
    for position, item in enumerate(items):
        if not isinstance(item, list) or len(item) != len(expected_fields):
            if allow_schema_blocked:
                return False, []
            _fail(f"raw {endpoint} item {position} has invalid width")
        rows.append(dict(zip(expected_fields, item)))
    return True, rows


def _raw_primary_key(
    row: Mapping[str, Any], fields: Sequence[str], label: str
) -> tuple[str, ...]:
    key: list[str] = []
    for field in fields:
        value = row.get(field)
        if value is None or not str(value).strip():
            _fail(f"raw {label} has a blank primary key")
        key.append(str(value).strip())
    return tuple(key)


def _raw_cell_matches(raw: Any, normalized: str, *, numeric: bool) -> bool:
    if raw is None:
        return normalized == ""
    if isinstance(raw, str) and raw == "":
        return normalized == ""
    if numeric:
        if isinstance(raw, bool) or not normalized.strip():
            return False
        try:
            left = Decimal(str(raw).strip())
            right = Decimal(normalized.strip())
        except InvalidOperation:
            return False
        return left.is_finite() and right.is_finite() and left == right
    return str(raw) == normalized


def _compare_raw_to_normalized(
    *,
    label: str,
    endpoint: str,
    raw_rows: Sequence[Mapping[str, Any]],
    normalized_rows: Sequence[Mapping[str, str]],
    primary_key: Sequence[str],
) -> None:
    fields = RAW_ENDPOINT_FIELDS[endpoint]
    raw_index: dict[tuple[str, ...], Mapping[str, Any]] = {}
    for row in raw_rows:
        key = _raw_primary_key(row, primary_key, label)
        if key in raw_index:
            _fail(f"duplicate raw primary key in {label}: {key}")
        raw_index[key] = row
    normalized_index: dict[tuple[str, ...], Mapping[str, str]] = {}
    for row in normalized_rows:
        key = tuple(row[field].strip() for field in primary_key)
        if key in normalized_index:
            _fail(f"duplicate normalized primary key in {label}: {key}")
        normalized_index[key] = row
    if set(raw_index) != set(normalized_index):
        _fail(f"raw/normalized primary-key mismatch for {label}")
    numeric_fields = RAW_NUMERIC_FIELDS[endpoint]
    for key, raw_row in raw_index.items():
        normalized_row = normalized_index[key]
        for field in fields:
            if field not in normalized_row:
                _fail(f"normalized {label} omits raw field {field}")
            if not _raw_cell_matches(
                raw_row[field], normalized_row[field], numeric=field in numeric_fields
            ):
                _fail(f"raw/normalized field mismatch for {label}:{key}:{field}")


def _validate_audit_requests(
    *,
    root: Path,
    audit: Mapping[str, Any],
    manifest: Mapping[str, Any],
    raw_records: Mapping[str, Mapping[str, Any]],
    legacy_end: str,
) -> None:
    requests = audit.get("requests")
    if not isinstance(requests, list):
        _fail("v3 audit.requests must be a list")
    if len(requests) != manifest["request_count"] or len(requests) != len(raw_records):
        _fail("audit.requests length does not close with manifest/raw files")
    required_keys = {
        "api_name",
        "params",
        "fields",
        "row_count",
        "decode_state",
        "raw_path",
        "raw_sha256",
    }
    allowed_keys = required_keys | {
        "http_status",
        "attempt_count",
        "reason_code",
    }
    observed_paths: set[str] = set()
    raw_root = (root / "inputs" / "raw").resolve()
    for position, item in enumerate(requests):
        request = _require_mapping(item, f"audit.requests[{position}]")
        if not required_keys <= set(request) or not set(request) <= allowed_keys:
            _fail(f"audit.requests[{position}] schema drift")
        api_name = request.get("api_name")
        if api_name not in RAW_ENDPOINT_FIELDS:
            _fail(f"audit.requests[{position}] uses a forbidden api_name")
        fields = request.get("fields")
        if fields != list(RAW_ENDPOINT_FIELDS[str(api_name)]):
            _fail(f"audit.requests[{position}] fields differ from the frozen request")
        raw_path = request.get("raw_path")
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
            _fail(f"audit.requests[{position}].raw_path must be absolute")
        resolved = Path(raw_path).resolve()
        try:
            relative = resolved.relative_to(raw_root).as_posix()
        except ValueError:
            _fail(f"audit.requests[{position}].raw_path escapes the current run")
        if relative in observed_paths:
            _fail(f"duplicate audit request raw_path: {relative}")
        observed_paths.add(relative)
        raw_record = raw_records.get(relative)
        if raw_record is None or resolved != Path(raw_record["path"]).resolve():
            _fail(f"audit request does not resolve to a frozen raw response: {relative}")
        if api_name != raw_record["api_name"]:
            _fail(f"audit request api/path mismatch: {relative}")
        digest = request.get("raw_sha256")
        if not isinstance(digest, str) or digest != _sha256(Path(raw_record["path"])):
            _fail(f"audit request raw_sha256 mismatch: {relative}")
        expected_state = "PASS" if raw_record["success"] else "BLOCKED"
        if request.get("decode_state") != expected_state:
            _fail(f"audit request decode_state mismatch: {relative}")
        if request.get("row_count") != raw_record["row_count"]:
            _fail(f"audit request row_count mismatch: {relative}")
        if expected_state == "BLOCKED":
            if not isinstance(request.get("reason_code"), str) or not request["reason_code"].strip():
                _fail(f"blocked audit request lacks a reason_code: {relative}")
        elif "reason_code" in request:
            _fail(f"successful audit request must not carry reason_code: {relative}")
        if "attempt_count" in request and (
            type(request["attempt_count"]) is not int or request["attempt_count"] < 1
        ):
            _fail(f"audit request attempt_count is invalid: {relative}")
        if "http_status" in request and request["http_status"] is not None and (
            type(request["http_status"]) is not int
            or not 100 <= request["http_status"] <= 599
        ):
            _fail(f"audit request http_status is invalid: {relative}")

        taxonomy = raw_record["taxonomy"]
        code = raw_record["code"]
        params = _require_mapping(request.get("params"), f"audit request params {relative}")
        if api_name == "trade_cal":
            expected_params = {
                "exchange": "SSE",
                "start_date": "20140101",
                "end_date": str(manifest["as_of"]),
                "is_open": "1",
            }
        elif api_name == "index_classify":
            expected_params = {"level": "L1", "src": taxonomy}
        else:
            expected_params = {
                "ts_code": code,
                "start_date": "20211213" if taxonomy == "SW2021" else "20140101",
                "end_date": str(manifest["as_of"]) if taxonomy == "SW2021" else legacy_end,
            }
        if dict(params) != expected_params:
            _fail(f"audit request params mismatch: {relative}")
    if observed_paths != set(raw_records):
        _fail("audit.requests does not cover every frozen raw response exactly once")


def _validate_raw_inputs(
    root: Path, manifest: Mapping[str, Any], audit: Mapping[str, Any]
) -> None:
    if manifest.get("schema_version") != "swivd-run-manifest-v3":
        return
    raw_root = root / "inputs" / "raw"
    raw_files = sorted(path for path in raw_root.rglob("*") if path.is_file())
    if any(path.suffix != ".json" for path in raw_files):
        _fail("v3 inputs/raw may contain only frozen JSON responses")
    request_count = manifest.get("request_count")
    if type(request_count) is not int or request_count < 1:
        _fail("manifest.request_count must be a positive integer")
    if request_count != len(raw_files):
        _fail("manifest.request_count differs from the frozen raw response file count")

    trade_rows: list[dict[str, Any]] = []
    classification_rows: dict[str, list[dict[str, Any]]] = {
        "SW2021": [],
        "SW2014": [],
    }
    classification_success: dict[str, bool] = {}
    daily_rows: dict[str, list[dict[str, Any]]] = {"SW2021": [], "SW2014": []}
    daily_files: dict[str, set[str]] = {"SW2021": set(), "SW2014": set()}
    raw_records: dict[str, dict[str, Any]] = {}
    trade_file_count = 0
    for path in raw_files:
        relative = path.relative_to(raw_root).as_posix()
        trade_match = re.fullmatch(r"trade_cal/SSE_20140101_(\d{8})\.json", relative)
        classify_match = re.fullmatch(
            r"index_classify/(SW2021|SW2014)_L1\.json", relative
        )
        daily_match = re.fullmatch(
            r"sw_daily/(SW2021|SW2014)/(\d{6})_([A-Z]{2})\.json", relative
        )
        if trade_match:
            trade_file_count += 1
            if trade_match.group(1) != str(manifest["as_of"]):
                _fail("trade_cal raw filename does not bind manifest.as_of")
            success, rows = _decode_raw_response(path, "trade_cal")
            if not success:
                _fail("v3 candidate requires a successful frozen trade_cal response")
            trade_rows.extend(rows)
            raw_records[relative] = {
                "api_name": "trade_cal",
                "taxonomy": None,
                "code": None,
                "success": success,
                "row_count": len(rows),
                "path": path,
            }
            continue
        if classify_match:
            taxonomy = classify_match.group(1)
            success, rows = _decode_raw_response(
                path,
                "index_classify",
                allow_schema_blocked=(
                    taxonomy == "SW2014" and manifest["axes"][taxonomy] == "BLOCKED"
                ),
            )
            classification_success[taxonomy] = success
            for row in rows:
                if row.get("src") != taxonomy or row.get("level") != "L1":
                    _fail(f"raw classification row is outside {taxonomy} L1")
            classification_rows[taxonomy].extend(rows)
            raw_records[relative] = {
                "api_name": "index_classify",
                "taxonomy": taxonomy,
                "code": None,
                "success": success,
                "row_count": len(rows) if success else None,
                "path": path,
            }
            continue
        if daily_match:
            taxonomy = daily_match.group(1)
            code = f"{daily_match.group(2)}.{daily_match.group(3)}"
            if code in daily_files[taxonomy]:
                _fail(f"duplicate raw sw_daily response file for {taxonomy}:{code}")
            daily_files[taxonomy].add(code)
            success, rows = _decode_raw_response(
                path,
                "sw_daily",
                allow_schema_blocked=(
                    taxonomy == "SW2014" and manifest["axes"][taxonomy] == "BLOCKED"
                ),
            )
            raw_records[relative] = {
                "api_name": "sw_daily",
                "taxonomy": taxonomy,
                "code": code,
                "success": success,
                "row_count": len(rows) if success else None,
                "path": path,
            }
            if not success:
                if taxonomy == "SW2021" or manifest["axes"][taxonomy] == "PASS":
                    _fail(f"PASS axis has a blocked raw sw_daily response: {taxonomy}:{code}")
                continue
            for row in rows:
                if row.get("ts_code") != code:
                    _fail(f"raw sw_daily filename/code mismatch for {taxonomy}:{code}")
            daily_rows[taxonomy].extend(rows)
            continue
        _fail(f"unrecognized raw response path: {relative}")

    if trade_file_count != 1:
        _fail("v3 run must contain exactly one frozen trade_cal response")
    if "SW2021" not in classification_success or not classification_success["SW2021"]:
        _fail("SW2021 requires a successful frozen index_classify response")
    if manifest["axes"]["SW2014"] == "PASS":
        if not classification_success.get("SW2014"):
            _fail("SW2014 PASS requires a successful frozen index_classify response")
    elif "SW2014" not in classification_success:
        if daily_files["SW2014"]:
            _fail("SW2014 raw daily responses exist without its classification response")
        axis_audit = _require_mapping(
            _require_mapping(audit.get("axes"), "audit.axes").get("SW2014"),
            "audit.axes.SW2014",
        )
        error = _require_mapping(axis_audit.get("error"), "audit.axes.SW2014.error")
        reason = error.get("reason_code")
        reason_codes = axis_audit.get("reason_codes")
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or not isinstance(reason_codes, list)
            or reason not in reason_codes
        ):
            _fail("missing SW2014 raw response lacks a scoped transport failure reason")

    if manifest["axes"]["SW2014"] == "BLOCKED":
        if classification_success.get("SW2014") is False and daily_files["SW2014"]:
            _fail("SW2014 raw daily responses exist after blocked classification decoding")
        if classification_success.get("SW2014") is True:
            classified_legacy_codes = {
                str(row["index_code"]).strip() for row in classification_rows["SW2014"]
            }
            unexpected_legacy_daily = (
                daily_files["SW2014"] - classified_legacy_codes
            )
            if unexpected_legacy_daily:
                _fail(
                    "SW2014 BLOCKED raw daily files exceed the successfully decoded "
                    "classification whitelist"
                )

    normalized = root / "inputs" / "normalized"
    trade_normalized = _require_columns(
        normalized / "trade_calendar.csv", set(RAW_ENDPOINT_FIELDS["trade_cal"])
    )
    _compare_raw_to_normalized(
        label="trade_cal",
        endpoint="trade_cal",
        raw_rows=trade_rows,
        normalized_rows=trade_normalized,
        primary_key=("exchange", "cal_date"),
    )
    for taxonomy, suffix in (("SW2021", "sw2021"), ("SW2014", "sw2014")):
        classifications = _require_columns(
            normalized / f"classification_{suffix}.csv",
            set(RAW_ENDPOINT_FIELDS["index_classify"]),
        )
        daily = _require_columns(
            normalized / f"sw_daily_{suffix}.csv",
            {"taxonomy", *RAW_ENDPOINT_FIELDS["sw_daily"]},
        )
        if taxonomy == "SW2014" and manifest["axes"][taxonomy] == "BLOCKED":
            if classifications or daily:
                _fail("SW2014 BLOCKED must not carry normalized classification or daily rows")
            continue
        _compare_raw_to_normalized(
            label=f"classification_{taxonomy}",
            endpoint="index_classify",
            raw_rows=classification_rows[taxonomy],
            normalized_rows=classifications,
            primary_key=("src", "index_code"),
        )
        _compare_raw_to_normalized(
            label=f"sw_daily_{taxonomy}",
            endpoint="sw_daily",
            raw_rows=daily_rows[taxonomy],
            normalized_rows=daily,
            primary_key=("ts_code", "trade_date"),
        )
        classified_codes = {
            str(row["index_code"]).strip()
            for row in classification_rows[taxonomy]
            if taxonomy == "SW2014" or str(row.get("is_pub")).strip() == "1"
        }
        if daily_files[taxonomy] != classified_codes:
            _fail(f"raw sw_daily file set differs from the selected {taxonomy} whitelist")

    legacy_end = max(
        row["cal_date"].strip()
        for row in trade_normalized
        if row["is_open"].strip() == "1" and row["cal_date"].strip() < "20211213"
    )
    _validate_audit_requests(
        root=root,
        audit=audit,
        manifest=manifest,
        raw_records=raw_records,
        legacy_end=legacy_end,
    )


def _scan_forbidden_files(inventory: Mapping[str, Path]) -> None:
    for relative, path in inventory.items():
        if path.suffix.lower() in PICKLE_SUFFIXES:
            _fail(f"Pickle-like artifact forbidden: {relative}")
        prefix = path.read_bytes()[:2]
        if len(prefix) == 2 and prefix[0] == 0x80 and 2 <= prefix[1] <= 5:
            _fail(f"Pickle protocol header forbidden: {relative}")


def _scan_secret_text(relative: str, text: str) -> None:
    for match in _SECRET_ASSIGNMENT_RE.finditer(text):
        value = match.group(1).strip("\"'")
        if value.lower() not in {"false", "null", "none", "redacted"} and "redacted" not in value.lower():
            _fail(f"suspected persisted token/secret in {relative}")
    if _JWT_RE.search(text) or _PREFIXED_SECRET_RE.search(text):
        _fail(f"suspected persisted token/secret in {relative}")


def _scan_secrets(inventory: Mapping[str, Path]) -> None:
    for relative, path in inventory.items():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            continue
        _scan_secret_text(relative, text)


class _PayloadParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.payloads: list[str] = []
        self._capturing = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): value for key, value in attrs}
        if tag.lower() == "script" and attributes.get("id") == "swivd-data":
            if attributes.get("type") != "application/json":
                _fail("swivd-data script must use type=application/json")
            self._capturing = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capturing:
            self.payloads.append("".join(self._parts))
            self._capturing = False
            self._parts = []


def _canonical_scalar(value: Any, *, numeric: bool) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    if numeric:
        try:
            number = Decimal(str(value).strip())
        except InvalidOperation as exc:
            raise ValidationError(f"invalid numeric payload value: {value!r}") from exc
        if not number.is_finite():
            _fail("non-finite numeric payload value")
        if number == 0:
            return "0"
        return format(number.normalize(), "f")
    return str(value).strip()


def _compare_payload_rows(
    payload_rows: Any,
    csv_rows: Sequence[Mapping[str, str]],
    *,
    label: str,
    mappings: Sequence[tuple[str, tuple[str, ...], bool]],
    key_csv: Sequence[str],
    key_payload: Sequence[tuple[str, ...]],
) -> None:
    if not isinstance(payload_rows, list) or any(not isinstance(row, Mapping) for row in payload_rows):
        _fail(f"payload.{label} must be a list of objects")
    if len(payload_rows) != len(csv_rows):
        _fail(f"payload/CSV row-count mismatch for {label}")

    def payload_value(
        row: Mapping[str, Any], aliases: tuple[str, ...], *, numeric: bool = False
    ) -> Any:
        present = [name for name in aliases if name in row]
        if not present:
            _fail(f"payload {label} needs at least one of {aliases}")
        normalized = {
            _canonical_scalar(row[name], numeric=numeric)
            for name in present
        }
        if len(normalized) != 1:
            _fail(f"payload {label} carries conflicting aliases {present}")
        return row[present[0]]

    csv_index: dict[tuple[str, ...], Mapping[str, str]] = {}
    for row in csv_rows:
        key = tuple(str(row[field]).strip() for field in key_csv)
        csv_index[key] = row

    payload_index: dict[tuple[str, ...], Mapping[str, Any]] = {}
    for row in payload_rows:
        key = tuple(
            _canonical_scalar(payload_value(row, aliases), numeric=False)
            for aliases in key_payload
        )
        if key in payload_index:
            _fail(f"duplicate embedded payload primary key in {label}: {key}")
        payload_index[key] = row
    if set(csv_index) != set(payload_index):
        _fail(f"payload/CSV primary-key mismatch for {label}")

    for key, csv_row in csv_index.items():
        payload_row = payload_index[key]
        for csv_field, aliases, numeric in mappings:
            left = _canonical_scalar(csv_row[csv_field], numeric=numeric)
            right = _canonical_scalar(
                payload_value(payload_row, aliases, numeric=numeric), numeric=numeric
            )
            if left != right:
                _fail(f"payload/CSV value mismatch for {label}:{key}:{csv_field}")


def _expected_statuses(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "execution_status": manifest["execution_status"],
        "artifact_publish_state": manifest["artifact_publish_state"],
        "live_validation_state": manifest["live_validation_state"],
        "sw2021_axis_state": manifest["axes"]["SW2021"],
        "sw2014_axis_state": manifest["axes"]["SW2014"],
        "research_grade": manifest["research_grade"],
        "decision_eligible": manifest["decision_eligible"],
        "production_approved": manifest["production_approved"],
    }


def _validate_html(
    root: Path,
    csvs: Mapping[str, list[dict[str, str]]],
    manifest: Mapping[str, Any],
    audit: Mapping[str, Any],
) -> None:
    html_path = root / "dashboard.html"
    try:
        html = html_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValidationError("dashboard.html must be readable UTF-8") from exc
    lowered = html.lower()
    forbidden_patterns = {
        "remote URL": r"https?://",
        "protocol-relative URL": (
            r"(?:src|href|action|poster)\s*=\s*['\"]?\s*//|"
            r"url\(\s*['\"]?\s*//"
        ),
        "CDN": r"\b(?:cdn|jsdelivr|unpkg|cdnjs)\b",
        "external resource attribute": (
            r"<(?:script|link|img|iframe|audio|video|source|object|embed|form)\b"
            r"[^>]+(?:src|href|action|poster)\s*="
        ),
        "network API": r"\b(?:fetch|xmlhttprequest|websocket|eventsource)\s*\(",
        "CSS import": r"@import\b",
        "external frame/object": r"<(?:iframe|object|embed)\b",
    }
    for label, pattern in forbidden_patterns.items():
        if re.search(pattern, lowered, flags=re.IGNORECASE):
            _fail(f"dashboard contains forbidden {label}")
    for forbidden in FORBIDDEN_VALUATION_WORDS:
        if forbidden.lower() in lowered:
            _fail(f"dashboard contains forbidden valuation wording: {forbidden}")

    parser = _PayloadParser()
    try:
        parser.feed(html)
        parser.close()
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError("dashboard HTML parsing failed") from exc
    if len(parser.payloads) != 1:
        _fail("dashboard must contain exactly one script#swivd-data payload")
    try:
        payload = json.loads(
            parser.payloads[0],
            object_pairs_hook=_strict_object,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=lambda value: _fail(f"non-finite payload number: {value}"),
        )
    except ValidationError:
        raise
    except (json.JSONDecodeError, InvalidOperation) as exc:
        raise ValidationError("invalid embedded dashboard payload JSON") from exc
    payload = _require_mapping(payload, "dashboard payload")
    _require_exact(
        payload.get("schema_version"),
        "swivd-dashboard-payload-v1",
        "dashboard payload schema_version",
    )
    metadata = _require_mapping(payload.get("metadata"), "dashboard payload.metadata")
    payload_audit = _require_mapping(payload.get("audit"), "dashboard payload.audit")
    if manifest.get("schema_version") == "swivd-run-manifest-v3":
        calendar = _read_csv(
            root / "inputs" / "normalized" / "trade_calendar.csv",
            exact_header=NORMALIZED_CALENDAR_FIELDS,
        )
        legacy_end = max(
            row["cal_date"].strip()
            for row in calendar
            if row["is_open"].strip() == "1" and row["cal_date"].strip() < "20211213"
        )
        expected_metadata = {
            "run_id": manifest["run_id"],
            "as_of": manifest["as_of"],
            "source": HTML_SOURCE,
            "taxonomy": HTML_TAXONOMY,
            "spec_version": manifest["spec_version"],
            "contract_version": manifest["contract_version"],
            "formula": PERCENTILE_FORMULA,
            "archive_end": legacy_end,
            "generated_at": manifest["completed_at"],
            "statuses": _expected_statuses(manifest),
        }
        _require_exact(
            dict(metadata), expected_metadata, "dashboard payload.metadata"
        )
        _require_exact(
            dict(payload_audit), dict(audit), "dashboard payload.audit"
        )

        source_axes = _require_mapping(audit.get("axes"), "audit.axes")
        source_legacy = _require_mapping(source_axes.get("SW2014"), "audit.axes.SW2014")
        expected_policy: dict[str, Any] = {
            "publication_state": PUBLICATION_STATE_RETIRED,
            "selection_basis": SELECTION_BASIS_RETIRED,
            "continuity_policy": CONTINUITY_POLICY_RETIRED,
            "selected_count": None,
            "publication_flag_null_count": None,
        }
        if manifest["axes"]["SW2014"] == "PASS":
            expected_policy.update(
                {
                    "selected_count": source_legacy["selected_count"],
                    "publication_flag_null_count": source_legacy[
                        "publication_flag_null_count"
                    ],
                    "name_history_policy": source_legacy["name_history_policy"],
                    "name_history": source_legacy["name_history"],
                }
            )
        policy = _require_mapping(
            payload.get("sw2014_policy"), "dashboard payload.sw2014_policy"
        )
        _require_exact(
            dict(policy), expected_policy, "dashboard payload.sw2014_policy"
        )
    else:
        if "run_id" in metadata and metadata["run_id"] != manifest["run_id"]:
            _fail("dashboard metadata.run_id differs from manifest")
        if "as_of" in metadata and str(metadata["as_of"]) != str(manifest["as_of"]):
            _fail("dashboard metadata.as_of differs from manifest")

    summary_mappings: tuple[tuple[str, tuple[str, ...], bool], ...] = (
        ("index_code", ("ts_code", "index_code"), False),
        ("industry_name", ("industry_name",), False),
        ("as_of", ("trade_date", "as_of"), False),
        ("pe", ("pe",), True),
        ("pb", ("pb",), True),
        ("pe_percentile", ("pe_percentile_le", "pe_percentile"), True),
        ("pb_percentile", ("pb_percentile_le", "pb_percentile"), True),
        ("pe_valid_count", ("pe_valid_count",), True),
        ("pb_valid_count", ("pb_valid_count",), True),
        ("pe_tie_count", ("pe_tie_count",), True),
        ("pb_tie_count", ("pb_tie_count",), True),
        ("pe_tie_ratio", ("pe_tie_ratio",), True),
        ("pb_tie_ratio", ("pb_tie_ratio",), True),
        ("pe_first_valid_date", ("pe_first_valid_date",), False),
        ("pe_last_valid_date", ("pe_last_valid_date",), False),
        ("pb_first_valid_date", ("pb_first_valid_date",), False),
        ("pb_last_valid_date", ("pb_last_valid_date",), False),
        ("return_5d", ("return_5d",), True),
        ("return_mtd", ("return_mtd",), True),
        ("return_ytd", ("return_ytd",), True),
    )
    history_mappings: tuple[tuple[str, tuple[str, ...], bool], ...] = (
        ("index_code", ("ts_code", "index_code"), False),
        ("industry_name", ("industry_name",), False),
        ("trade_date", ("trade_date",), False),
        ("close", ("close",), True),
        ("pe", ("pe",), True),
        ("pb", ("pb",), True),
        ("is_pub", ("is_pub",), False),
    )
    if manifest.get("schema_version") == "swivd-run-manifest-v3":
        summary_mappings += (
            ("taxonomy", ("taxonomy", "src"), False),
            ("pe_label", ("pe_label", "pe_history_label"), False),
            ("pb_label", ("pb_label", "pb_history_label"), False),
            ("valuation_state", ("valuation_state", "status", "row_status"), False),
            ("return_state", ("return_state",), False),
        )
        history_mappings += (("taxonomy", ("taxonomy", "src"), False),)
    for payload_key, csv_key in (
        ("current_rows", "current_rows"),
        ("sw2014_summary", "sw2014_summary"),
    ):
        _compare_payload_rows(
            payload.get(payload_key),
            csvs[csv_key],
            label=payload_key,
            mappings=summary_mappings,
            key_csv=("index_code", "as_of"),
            key_payload=(("ts_code", "index_code"), ("trade_date", "as_of")),
        )
        if payload_key == "sw2014_summary":
            for row in payload[payload_key]:
                if "is_pub" not in row or row["is_pub"] is not None:
                    _fail("SW2014 summary payload is_pub must be explicit JSON null")
    for payload_key, csv_key in (
        ("sw2021_history", "sw2021_history"),
        ("sw2014_history", "sw2014_history"),
    ):
        mappings = history_mappings
        if (
            manifest.get("schema_version") == "swivd-run-manifest-v3"
            and payload_key == "sw2014_history"
        ):
            mappings += (("source_name", ("source_name",), False),)
        _compare_payload_rows(
            payload.get(payload_key),
            csvs[csv_key],
            label=payload_key,
            mappings=mappings,
            key_csv=("index_code", "trade_date"),
            key_payload=(("ts_code", "index_code"), ("trade_date",)),
        )
        if payload_key == "sw2014_history":
            for row in payload[payload_key]:
                if "is_pub" not in row or row["is_pub"] is not None:
                    _fail("SW2014 history payload is_pub must be explicit JSON null")
            if manifest.get("schema_version") == "swivd-run-manifest-v3":
                csv_source_names = {
                    (row["index_code"].strip(), row["trade_date"].strip()): row["source_name"]
                    for row in csvs[csv_key]
                }
                for row in payload[payload_key]:
                    code = _canonical_scalar(row.get("ts_code", row.get("index_code")), numeric=False)
                    date = _canonical_scalar(row.get("trade_date"), numeric=False)
                    if row.get("source_name") != csv_source_names[(code, date)]:
                        _fail("SW2014 payload source_name is not byte-exact with its CSV value")

    if manifest.get("schema_version") == "swivd-run-manifest-v3":
        try:
            from .render import render_dashboard

            rendered = render_dashboard(
                current_rows=payload["current_rows"],
                sw2021_history=payload["sw2021_history"],
                sw2014_summary=payload["sw2014_summary"],
                sw2014_history=payload["sw2014_history"],
                metadata=expected_metadata,
                audit=audit,
            )
        except Exception as exc:
            raise ValidationError(
                "deterministic dashboard re-render failed"
            ) from exc
        if html_path.read_bytes() != rendered.encode("utf-8"):
            _fail("dashboard.html differs from deterministic renderer output")


def _validate_audit(
    audit: Mapping[str, Any],
    manifest: Mapping[str, Any],
    sw2014_name_history: Mapping[str, Any] | None,
    axis_counts: Mapping[str, Mapping[str, int]],
    calendar_evidence: Mapping[str, Any],
    ohlc_evidence: Mapping[str, Mapping[str, Any]],
    *,
    skip_posthoc_ohlc_audit: bool,
) -> None:
    manifest_schema = manifest.get("schema_version")
    expected_audit_schema = {
        "swivd-run-manifest-v1": "swivd-audit-v1",
        "swivd-run-manifest-v2": "swivd-audit-v2",
        "swivd-run-manifest-v3": "swivd-audit-v3",
    }.get(str(manifest_schema))
    if expected_audit_schema is None:
        _fail("unsupported manifest schema for audit validation")
    _require_exact(
        audit.get("schema_version"),
        expected_audit_schema,
        "audit.schema_version",
    )
    _require_exact(audit.get("run_id"), manifest["run_id"], "audit.run_id")
    _require_exact(str(audit.get("as_of")), str(manifest["as_of"]), "audit.as_of")
    if manifest_schema in {"swivd-run-manifest-v2", "swivd-run-manifest-v3"}:
        is_v3 = manifest_schema == "swivd-run-manifest-v3"
        _require_exact(
            audit.get("contract_version"),
            "swivd-contract-v1.2.0" if is_v3 else "swivd-contract-v1.1.0",
            "audit.contract_version",
        )
        _require_exact(
            audit.get("spec_version"),
            "swivd-project-spec-v3" if is_v3 else "swivd-project-spec-v2",
            "audit.spec_version",
        )
        _require_exact(
            audit.get("semantic_successor_id"),
            "GOV-20260830-001-SW2014-SEMANTICS-AUTHORIZED",
            "audit.semantic_successor_id",
        )
        if is_v3:
            _require_exact(
                audit.get("name_history_successor_id"),
                "GOV-20260830-001-NAME-HISTORY-AUTHORIZED",
                "audit.name_history_successor_id",
            )
            _require_exact(
                audit.get("created_at"), manifest["created_at"], "audit.created_at"
            )
            _require_exact(
                audit.get("completed_at"), manifest["completed_at"], "audit.completed_at"
            )
            _require_exact(
                audit.get("status"), _expected_statuses(manifest), "audit.status"
            )
            _require_exact(
                audit.get("trade_calendar"),
                dict(calendar_evidence),
                "audit.trade_calendar",
            )
    axes = _require_mapping(audit.get("axes"), "audit.axes")
    if set(axes) != {"SW2021", "SW2014"}:
        _fail("audit.axes must contain exactly SW2021 and SW2014")
    for taxonomy in ("SW2021", "SW2014"):
        axis = _require_mapping(axes[taxonomy], f"audit.axes.{taxonomy}")
        if axis.get("state") != manifest["axes"][taxonomy]:
            _fail(f"audit and manifest axis state differ for {taxonomy}")
        if manifest["axes"][taxonomy] == "PASS" and not skip_posthoc_ohlc_audit:
            _require_exact(
                axis.get("ohlc_ordering"),
                dict(ohlc_evidence[taxonomy]),
                f"audit.axes.{taxonomy}.ohlc_ordering",
            )

    if manifest_schema == "swivd-run-manifest-v3":
        current = _require_mapping(axes["SW2021"], "audit.axes.SW2021")
        current_semantics: dict[str, Any] = {
            "publication_state": PUBLICATION_STATE_CURRENT,
            "selection_basis": SELECTION_BASIS_CURRENT,
            "continuity_policy": CONTINUITY_POLICY_CURRENT,
            "name_history_policy": NAME_HISTORY_POLICY_CURRENT,
            **axis_counts["SW2021"],
        }
        for field, expected in current_semantics.items():
            _require_exact(current.get(field), expected, f"audit.axes.SW2021.{field}")

    if manifest["axes"]["SW2014"] == "PASS":
        legacy = _require_mapping(axes["SW2014"], "audit.axes.SW2014")
        required_semantics = {
            "publication_state": PUBLICATION_STATE_RETIRED,
            "selection_basis": SELECTION_BASIS_RETIRED,
            "selected_count": 28,
            "publication_flag_null_count": 28,
            "continuity_policy": CONTINUITY_POLICY_RETIRED,
            "name_history_policy": NAME_HISTORY_POLICY_RETIRED,
        }
        for field, expected in required_semantics.items():
            _require_exact(legacy.get(field), expected, f"audit.axes.SW2014.{field}")
        if manifest_schema == "swivd-run-manifest-v3":
            _require_exact(
                legacy.get("classification_count"),
                axis_counts["SW2014"]["classification_count"],
                "audit.axes.SW2014.classification_count",
            )
            _require_exact(
                legacy.get("selected_count"),
                axis_counts["SW2014"]["selected_count"],
                "audit.axes.SW2014.selected_count",
            )
            _require_exact(
                legacy.get("publication_flag_null_count"),
                axis_counts["SW2014"]["publication_flag_null_count"],
                "audit.axes.SW2014.publication_flag_null_count",
            )
            if sw2014_name_history is None:
                _fail("SW2014 PASS lacks independently recomputed name history")
            _require_exact(
                legacy.get("name_history"),
                dict(sw2014_name_history),
                "audit.axes.SW2014.name_history",
            )

    if manifest["axes"]["SW2014"] == "BLOCKED":
        legacy = _require_mapping(axes["SW2014"], "audit.axes.SW2014")
        raw_codes = legacy.get("reason_codes")
        if not isinstance(raw_codes, list) or not raw_codes:
            _fail("SW2014 BLOCKED requires scoped audit reason_codes")
        codes = [code.strip() for code in raw_codes if isinstance(code, str) and code.strip()]
        if len(codes) != len(raw_codes) or len(codes) != len(set(codes)):
            _fail("SW2014 audit reason_codes must be unique nonblank strings")
        error = legacy.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("reason_code"), str):
            if error["reason_code"].strip() not in codes:
                _fail("SW2014 audit error reason_code is absent from reason_codes")


def _parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        _fail(f"missing {label}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"invalid ISO time: {label}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(f"{label} must be timezone-aware")
    return parsed


def _validate_source_files(source_files: Any, *, compare_current_bytes: bool = True) -> None:
    if not isinstance(source_files, list):
        _fail("manifest.source_files must be a list")
    expected_paths = {
        "PROJECT_CONTRACT.md",
        "PROJECT_SPEC.json",
        "README.md",
        "run_dashboard.py",
        *(
            path.relative_to(PROJECT_ROOT).as_posix()
            for path in (PROJECT_ROOT / "src" / "swivd").glob("*.py")
        ),
        *(
            path.relative_to(PROJECT_ROOT).as_posix()
            for path in (PROJECT_ROOT / "tests").glob("test_*.py")
        ),
    }
    observed: dict[str, Mapping[str, Any]] = {}
    for position, item in enumerate(source_files):
        record = _require_mapping(item, f"manifest.source_files[{position}]")
        if set(record) != {"path", "bytes", "sha256"}:
            _fail("manifest.source_files record schema drift")
        relative = _safe_relative(record.get("path"), "source file path")
        if relative in observed:
            _fail(f"duplicate manifest.source_files path: {relative}")
        size = record.get("bytes")
        digest = record.get("sha256")
        if type(size) is not int or size < 0:
            _fail(f"invalid source file byte count: {relative}")
        if not isinstance(digest, str) or not SHA_RE.fullmatch(digest):
            _fail(f"invalid source file sha256: {relative}")
        observed[relative] = record
    if set(observed) != expected_paths:
        _fail("manifest.source_files does not equal the canonical source closure")
    if compare_current_bytes:
        for relative, record in observed.items():
            target = PROJECT_ROOT / relative
            if not target.is_file() or target.is_symlink():
                _fail(f"canonical source file is missing or not regular: {relative}")
            if target.stat().st_size != record["bytes"] or _sha256(target) != record["sha256"]:
                _fail(f"canonical source file hash/size drift: {relative}")


def _validate_runtime(runtime: Any) -> None:
    record = _require_mapping(runtime, "manifest.runtime")
    expected_keys = {
        "python_implementation",
        "python_version",
        "executable",
        "platform",
        "dependency_policy",
        "package_versions",
    }
    if set(record) != expected_keys:
        _fail("manifest.runtime schema drift")
    for field in (
        "python_implementation",
        "python_version",
        "executable",
        "platform",
        "dependency_policy",
    ):
        if not isinstance(record[field], str) or not record[field].strip():
            _fail(f"manifest.runtime.{field} must be a nonblank string")
    if not Path(record["executable"]).is_absolute():
        _fail("manifest.runtime.executable must be an absolute historical path")
    _require_exact(
        record["dependency_policy"],
        "STDLIB_HTTP_WITH_OPTIONAL_CERTIFI_CA_AND_TUSHARE_TOKEN_LOOKUP",
        "manifest.runtime.dependency_policy",
    )
    versions = _require_mapping(record["package_versions"], "manifest.runtime.package_versions")
    if set(versions) != {"certifi", "tushare"}:
        _fail("manifest.runtime.package_versions schema drift")
    for package, version in versions.items():
        if version is not None and (not isinstance(version, str) or not version.strip()):
            _fail(f"manifest.runtime package version is invalid: {package}")


def _validate_manifest(
    root: Path,
    manifest: Mapping[str, Any],
    *,
    run003_grandfather: bool,
    run004_grandfather: bool,
) -> None:
    legacy_version = _legacy_run_version(root, manifest)
    required = {
        "schema_version",
        "run_id",
        "as_of",
        "created_at",
        "completed_at",
        "execution_status",
        "artifact_publish_state",
        "live_validation_state",
        "axes",
        "research_grade",
        "decision_eligible",
        "production_approved",
        "source",
        "spec_sha256",
        "contract_sha256",
        "artifacts",
        "validation",
    }
    if legacy_version is None:
        required.update(
            {
                "contract_version",
                "spec_version",
                "semantic_successor_id",
                "name_history_successor_id",
                "request_count",
                "runtime",
                "source_files",
            }
        )
    missing = sorted(required - set(manifest))
    if missing:
        _fail(f"manifest missing required fields: {missing}")
    _require_exact(
        manifest["schema_version"],
        legacy_version or "swivd-run-manifest-v3",
        "manifest.schema_version",
    )
    if legacy_version is None:
        _require_exact(
            manifest["contract_version"],
            "swivd-contract-v1.2.0",
            "manifest.contract_version",
        )
        _require_exact(
            manifest["spec_version"],
            "swivd-project-spec-v3",
            "manifest.spec_version",
        )
        _require_exact(
            manifest["semantic_successor_id"],
            "GOV-20260830-001-SW2014-SEMANTICS-AUTHORIZED",
            "manifest.semantic_successor_id",
        )
        _require_exact(
            manifest["name_history_successor_id"],
            "GOV-20260830-001-NAME-HISTORY-AUTHORIZED",
            "manifest.name_history_successor_id",
        )
        if not (run003_grandfather or run004_grandfather):
            _validate_source_files(
                manifest["source_files"], compare_current_bytes=not run003_grandfather
            )
        _validate_runtime(manifest["runtime"])
    elif legacy_version == "swivd-run-manifest-v2":
        _require_exact(
            manifest.get("contract_version"),
            "swivd-contract-v1.1.0",
            "legacy manifest.contract_version",
        )
        _require_exact(
            manifest.get("spec_version"),
            "swivd-project-spec-v2",
            "legacy manifest.spec_version",
        )
        _require_exact(
            manifest.get("semantic_successor_id"),
            "GOV-20260830-001-SW2014-SEMANTICS-AUTHORIZED",
            "legacy manifest.semantic_successor_id",
        )
    run_id = manifest["run_id"]
    match = RUN_RE.fullmatch(str(run_id))
    if not match or root.name != run_id:
        _fail("manifest run_id does not match the immutable run directory")
    as_of = _validate_date(str(manifest["as_of"]), "manifest.as_of")
    if match.group(1) != as_of:
        _fail("run_id date differs from manifest.as_of")
    created = _parse_time(manifest["created_at"], "manifest.created_at")
    completed = _parse_time(manifest["completed_at"], "manifest.completed_at")
    if completed < created:
        _fail("manifest completed_at precedes created_at")

    axes = _require_mapping(manifest["axes"], "manifest.axes")
    if set(axes) != {"SW2021", "SW2014"}:
        _fail("manifest.axes must contain exactly SW2021 and SW2014")
    if axes["SW2021"] != "PASS" or axes["SW2014"] not in {"PASS", "BLOCKED"}:
        _fail("invalid or non-deliverable axis states")

    _require_exact(manifest["execution_status"], "COMPLETED", "execution_status")
    if axes["SW2014"] == "PASS":
        _require_exact(
            manifest["artifact_publish_state"],
            "LOCAL_RESEARCH_CANDIDATE_COMPLETE",
            "artifact_publish_state",
        )
        _require_exact(manifest["live_validation_state"], "PASS", "live_validation_state")
    else:
        _require_exact(
            manifest["artifact_publish_state"],
            "LOCAL_RESEARCH_CANDIDATE_PARTIAL",
            "artifact_publish_state",
        )
        _require_exact(manifest["live_validation_state"], "PARTIAL", "live_validation_state")
    if "PUBLISHED" in str(manifest["artifact_publish_state"]).upper():
        _fail("PUBLISHED state is forbidden")
    _require_exact(manifest["research_grade"], "RESEARCH_ONLY", "research_grade")
    _require_exact(manifest["decision_eligible"], False, "decision_eligible")
    _require_exact(manifest["production_approved"], False, "production_approved")
    source = _require_mapping(manifest["source"], "manifest.source")
    if legacy_version is None:
        _require_exact(dict(source), MANIFEST_SOURCE, "manifest.source")
        validation = _require_mapping(manifest["validation"], "manifest.validation")
        _require_exact(
            dict(validation),
            {
                "status": "PASS",
                "entrypoint": "swivd.validator.validate_run",
            },
            "manifest.validation",
        )
    elif not isinstance(manifest["validation"], (Mapping, list)):
        _fail("manifest.validation must be an object or list")

    if legacy_version is not None:
        legacy_expected = LEGACY_RUNS[str(manifest["run_id"])]
        _require_exact(
            manifest["spec_sha256"], legacy_expected["spec_sha256"], "manifest.spec_sha256"
        )
        _require_exact(
            manifest["contract_sha256"],
            legacy_expected["contract_sha256"],
            "manifest.contract_sha256",
        )
    else:
        _require_exact(manifest["spec_sha256"], EXPECTED_SPEC_SHA256, "manifest.spec_sha256")
        _require_exact(
            manifest["contract_sha256"], EXPECTED_CONTRACT_SHA256, "manifest.contract_sha256"
        )
    if not run004_grandfather:
        if _sha256(CANONICAL_SPEC) != EXPECTED_SPEC_SHA256:
            _fail("canonical PROJECT_SPEC.json drifted after validator freeze")
        if _sha256(CANONICAL_CONTRACT) != EXPECTED_CONTRACT_SHA256:
            _fail("canonical PROJECT_CONTRACT.md drifted after validator freeze")


def _validate_offline_validation(root: Path, manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != "swivd-run-manifest-v3":
        return
    path = root / "offline_validation.json"
    if not path.exists():
        return
    record = _require_mapping(_load_json(path), "offline_validation")
    if set(record) != {"schema_version", "validated_at", "result"}:
        _fail("offline_validation.json schema drift")
    _require_exact(
        record.get("schema_version"),
        "swivd-offline-validation-v1",
        "offline_validation.schema_version",
    )
    _parse_time(record.get("validated_at"), "offline_validation.validated_at")
    result = _require_mapping(record.get("result"), "offline_validation.result")
    for field in (
        "run_id",
        "as_of",
        "axes",
        "execution_status",
        "artifact_publish_state",
        "live_validation_state",
        "research_grade",
        "decision_eligible",
        "production_approved",
    ):
        _require_exact(
            result.get(field), manifest[field], f"offline_validation.result.{field}"
        )


def _validate_adversarial_report(
    root: Path,
    manifest: Mapping[str, Any],
    ohlc_evidence: Mapping[str, Mapping[str, Any]],
    *,
    run003_grandfather: bool,
) -> None:
    if manifest.get("schema_version") != "swivd-run-manifest-v3":
        return
    path = root / "reports" / "adversarial_review.md"
    try:
        report = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValidationError("adversarial review must be readable UTF-8") from exc
    if "\x00" in report:
        _fail("adversarial review contains a NUL byte")

    run_id = str(manifest["run_id"])
    as_of = str(manifest["as_of"])
    run_ids = set(re.findall(r"SWIVD-RUN-\d{8}-\d{3}", report))
    if run_ids != {run_id}:
        _fail("adversarial review run_id identity is missing or conflicting")
    if len(re.findall(rf"^# {re.escape(run_id)} 对抗式审查$", report, re.MULTILINE)) != 1:
        _fail("adversarial review must declare its run_id exactly once")
    as_of_lines = re.findall(r"^- 数据截止日：`([^`]+)`$", report, re.MULTILINE)
    if as_of_lines != [as_of]:
        _fail("adversarial review must declare the manifest as_of exactly once")

    for taxonomy in ("SW2021", "SW2014"):
        declarations = re.findall(
            rf"^- {taxonomy} 轴：`([^`]+)`$", report, re.MULTILINE
        )
        if declarations != [manifest["axes"][taxonomy]]:
            _fail(f"adversarial review {taxonomy} state contradicts the run")
    required_declarations = (
        "- 研究等级：`RESEARCH_ONLY`",
        "- 决策资格：`false`",
        "- 生产批准：`false`",
    )
    for declaration in required_declarations:
        if report.count(declaration) != 1:
            _fail(f"adversarial review lacks exact research-only declaration: {declaration}")
    if "经验分布函数 `count(x <= current) / N`" not in report:
        _fail("adversarial review lacks the empirical-CDF formula disclosure")
    if "`ts_code` 是身份，`source_name` 保留" not in report:
        _fail("adversarial review lacks the SW2014 name/identity disclosure")

    if (
        manifest["artifact_publish_state"] == "LOCAL_RESEARCH_CANDIDATE_COMPLETE"
        and not run003_grandfather
    ):
        ohlc_disclosures = (
            "`open/low/high` 不参与本项目公式",
            "排序异常原样保留并披露",
            "不改写源值，也不冒充 `close/pe/pb` 有效性",
            f"SW2021 异常行 `{ohlc_evidence['SW2021']['anomaly_count']}`",
            f"SW2014 异常行 `{ohlc_evidence['SW2014']['anomaly_count']}`",
        )
        for disclosure in ohlc_disclosures:
            if report.count(disclosure) != 1:
                _fail("adversarial review lacks exact non-blocking OHLC disclosure")

    if manifest["axes"]["SW2014"] == "PASS":
        if "SW2014 轴已闭合：" not in report or "SW2014 轴失败关闭：" in report:
            _fail("adversarial review SW2014 conclusion contradicts PASS")
    elif "SW2014 轴失败关闭：" not in report or "SW2014 轴已闭合：" in report:
        _fail("adversarial review SW2014 conclusion contradicts BLOCKED")

    for line in report.splitlines():
        stripped = line.strip()
        if re.match(
            r"^(?:结论[：:]\s*)?(?:可交易|生产可用|已正式发布|双轴完整)",
            stripped,
            re.IGNORECASE,
        ):
            _fail("adversarial review makes a forbidden delivery/production claim")


def validate_run(run_dir: str | Path) -> dict[str, Any]:
    """Validate one immutable run and return its parsed manifest.

    The function performs no writes and no network access.  Any ambiguity is a
    failure; callers must not update a latest pointer when this function raises.
    """

    root = Path(run_dir)
    manifest_path = root / "manifest.json"
    if manifest_path.is_file() and not manifest_path.is_symlink():
        early_manifest = _require_mapping(_load_json(manifest_path), "manifest")
        if early_manifest.get("schema_version") in {
            "swivd-local-snapshot-manifest-v2",
            "swivd-local-snapshot-manifest-v3",
            "swivd-local-snapshot-manifest-v4",
        }:
            from .v2_validator import validate_run_v2

            return validate_run_v2(root)
    inventory = _inventory(root)
    missing = sorted(REQUIRED_FILES - set(inventory))
    if missing:
        _fail(f"run missing mandatory artifacts: {missing}")
    raw_dir = root / "inputs" / "raw"
    if not raw_dir.is_dir() or raw_dir.is_symlink():
        _fail("inputs/raw must be a regular directory")
    if not any(path.is_file() and not path.is_symlink() for path in raw_dir.rglob("*")):
        _fail("inputs/raw must contain frozen provider responses")

    manifest = _require_mapping(_load_json(root / "manifest.json"), "manifest")
    run003_grandfather = _is_exact_run003_grandfather(root, manifest)
    run004_grandfather = (
        root.name == RUN004_GRANDFATHER_ID
        and manifest.get("run_id") == RUN004_GRANDFATHER_ID
        and _sha256(root / "manifest.json") == RUN004_GRANDFATHER_MANIFEST_SHA256
    )
    legacy_version = _legacy_run_version(root, manifest)
    _validate_manifest(
        root,
        manifest,
        run003_grandfather=run003_grandfather,
        run004_grandfather=run004_grandfather,
    )
    _validate_hash_closure(root, inventory, manifest)
    _validate_offline_validation(root, manifest)
    _scan_forbidden_files(inventory)
    _scan_secrets(inventory)

    audit = _require_mapping(_load_json(root / "audit.json"), "audit")
    (
        csvs,
        sw2014_name_history,
        axis_counts,
        calendar_evidence,
        ohlc_evidence,
    ) = _validate_csvs(root, manifest)
    _validate_raw_inputs(root, manifest, audit)
    _validate_audit(
        audit,
        manifest,
        sw2014_name_history,
        axis_counts,
        calendar_evidence,
        ohlc_evidence,
        skip_posthoc_ohlc_audit=(
            legacy_version is not None or run003_grandfather
        ),
    )
    _validate_html(root, csvs, manifest, audit)
    _validate_adversarial_report(
        root,
        manifest,
        ohlc_evidence,
        run003_grandfather=run003_grandfather,
    )
    return dict(manifest)


__all__ = [
    "HISTORY_FIELDS",
    "SUMMARY_FIELDS",
    "SW2014_HISTORY_FIELDS",
    "ValidationError",
    "validate_run",
    "validate_spec",
]
