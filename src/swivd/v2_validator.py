"""Independent offline validator for the portable v2 snapshot format."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import shutil
import tempfile
from collections import defaultdict
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .tushare_client import ENDPOINT_FIELDS
from .v2_identity import (
    IDENTITY_CANDIDATE_CODES,
    project_classification,
    project_member,
    project_quote,
    resolve_membership_identity,
    resolve_quote_identity,
)


TRUSTED_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class V2ValidationError(ValueError):
    pass


LEVELS = ("L1", "L2", "L3")
COUNTS = {"L1": 31, "L2": 134, "L3": 346}
RUN_RE = re.compile(r"^SWIVD2-RUN-(\d{8})-(\d{3})$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
FORBIDDEN_REMOTE = re.compile(r"(?i)(?:https?:)?//|@import\s+url|url\s*\(")
FORBIDDEN_SOURCE_WEB_REMOTE = re.compile(
    r"(?i)https?://(?!(?:(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?"
    r"(?:[/\s<\"']|$)|www\.w3\.org/(?:2000/svg|1999/xlink)(?:[\"']|$)))|"
    r"(?:src|href|action)\s*=\s*[\"']\s*//|"
    r"@import\s+(?:url\s*\()?\s*[\"']?\s*//|"
    r"url\s*\(\s*[\"']?\s*//|[\"']\s*//[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)
SECRET_VALUE = re.compile(
    r"(?i)(?:tushare[_-]?token|authorization|bearer|password|secret)\s*[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9._~+/=-]{12,})"
)
LEGACY_RUN_ID = "SWIVD-RUN-20260828-004"
LEGACY_MANIFEST_SHA256 = "5180fd5eb20e2cd556a5a5066039fba676272d735ae2d91b9b798e59b33daea0"
LEGACY_SELECTED_FILES = (
    "manifest.json",
    "audit.json",
    "dashboard.html",
    "inputs/normalized/classification_sw2014.csv",
    "inputs/normalized/sw_daily_sw2014.csv",
    "tables/sw2014_archive.csv",
    "tables/sw2014_history.csv",
    "reports/adversarial_review.md",
)
V4_1_SPEC_IDENTITY = (
    "swivd-project-spec-v4.1",
    "swivd-contract-v2.1.0",
    "GOV-20260901-003",
)
V4_2_SPEC_IDENTITY = (
    "swivd-project-spec-v4.2",
    "swivd-contract-v2.2.0",
    "GOV-20260901-004",
)
CURRENT_SPEC_IDENTITY = ("swivd-project-spec-v4.3", "swivd-contract-v2.3.0", "GOV-20260906-001")
IDENTITY_SPEC_IDENTITIES = {V4_2_SPEC_IDENTITY, CURRENT_SPEC_IDENTITY}
LEGACY_V3_MANIFEST_SHA256 = "6f8422dedaf820866e38b9d009a7f8c28a3945c527535dee46001509bcd49514"
PROVIDER_POLICIES = {
    "LIVE_SECURE_TUSHARE": {
        "artifact_publish_state": "LOCAL_RESEARCH_CANDIDATE_COMPLETE",
        "live_validation_state": "PASS",
        "live_validation_reason": "SECURE_TUSHARE_CLIENT",
        "request_transport": "HTTPS_NO_REDIRECT",
    },
    "TEST_INJECTED_CLIENT": {
        "artifact_publish_state": "LOCAL_TEST_PROVIDER_COMPLETE",
        "live_validation_state": "NOT_LIVE_TEST_PROVIDER",
        "live_validation_reason": "EXPLICIT_CLIENT_INJECTION",
        "request_transport": "INJECTED_TEST_CLIENT",
    },
}
SUPPORTED_SPEC_IDENTITIES = {
    ("swivd-project-spec-v4", "swivd-contract-v2.0.0", "GOV-20260901-001"),
    V4_1_SPEC_IDENTITY,
    V4_2_SPEC_IDENTITY,
    CURRENT_SPEC_IDENTITY,
}
UPDATE_TARGET_SPEC_IDENTITIES = {V4_1_SPEC_IDENTITY, V4_2_SPEC_IDENTITY, CURRENT_SPEC_IDENTITY}
CLASSIFICATION_V3_FIELDS = (
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
SW_DAILY_V3_FIELDS = (
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
MEMBER_V3_FIELDS = (
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
    "industry_uid",
    "source_l3_code",
    "identity_state",
    "identity_rule_version",
)
IDENTITY_RULE_VERSION = "swivd-special-steel-identity-v1"
SPECIAL_INDUSTRY_UID = "SW2021:L3:230501"
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
CATALOG_V1_ENTRY_FIELDS = {
    "level",
    "index_code",
    "industry_name",
    "is_pub",
    "member_row_count",
    "valuation_state",
    "shard",
}
CATALOG_V2_ENTRY_FIELDS = CATALOG_V1_ENTRY_FIELDS | {
    "parent_code",
    *CATALOG_SUMMARY_FIELDS,
}
CATALOG_IDENTITY_FIELDS = {
    "industry_uid",
    "catalog_index_code",
    "quote_index_code",
    "member_index_code",
    "current_index_code",
    "identity_state",
    "identity_rule",
    "identity_rule_version",
    "identity_reason_disclosure",
}
CATALOG_V3_ENTRY_FIELDS = CATALOG_V2_ENTRY_FIELDS | CATALOG_IDENTITY_FIELDS
REQUIRED_SOURCE_FILES_V3 = {
    "AGENTS.md",
    "PROJECT_CONTRACT.md",
    "PROJECT_CONTRACT_V2.md",
    "PROJECT_SPEC_V4.json",
    "README.md",
    "pyproject.toml",
    "requirements.lock",
    "run_dashboard.py",
    "start_macos.command",
    "start_windows.cmd",
    "web/app.css",
    "web/app.js",
    "web/history.css",
    "web/index.html",
    "docs/architecture.md",
    "docs/data_dictionary.md",
    "docs/runbook.md",
    "docs/troubleshooting.md",
    "tests/test_adversarial.py",
    "tests/test_pipeline.py",
    "tests/test_sw2014_core.py",
    "tests/test_v2.py",
    "tests/test_v2_identity.py",
    "src/swivd/__init__.py",
    "src/swivd/core.py",
    "src/swivd/io_utils.py",
    "src/swivd/pipeline.py",
    "src/swivd/render.py",
    "src/swivd/tushare_client.py",
    "src/swivd/v2_domain.py",
    "src/swivd/v2_identity.py",
    "src/swivd/v2_jobs.py",
    "src/swivd/v2_pipeline.py",
    "src/swivd/v2_provider.py",
    "src/swivd/v2_server.py",
    "src/swivd/v2_storage.py",
    "src/swivd/v2_validator.py",
    "src/swivd/validator.py",
}


def _fail(message: str) -> None:
    raise V2ValidationError(message)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise V2ValidationError(f"invalid JSON: {path.name}") from exc


def _load_raw(path: Path) -> Any:
    """Decode an endpoint envelope with the live client's number semantics."""

    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            parse_float=Decimal,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-standard JSON constant: {value}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise V2ValidationError(f"invalid raw JSON: {path.name}") from exc


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be an object")
    return value


def _exact(value: Any, expected: Any, label: str) -> None:
    if type(value) is not type(expected) or value != expected:
        _fail(f"{label} differs from the frozen contract")


def _safe_relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        _fail(f"{label} is not a portable relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        _fail(f"{label} is unsafe")
    return value


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file() or path.is_symlink():
        _fail(f"missing regular CSV: {path.name}")
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or len(reader.fieldnames) != len(set(reader.fieldnames)):
            _fail(f"invalid CSV header: {path.name}")
        return list(reader.fieldnames), [dict(row) for row in reader]


def validate_spec_v4(
    path: str | Path,
    *,
    require_current: bool = False,
) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file() or target.is_symlink():
        _fail("spec must be a regular file")
    spec = dict(_mapping(_load(target), "spec"))
    identity = (
        spec.get("schema_version"),
        spec.get("contract_version"),
        spec.get("decision_id"),
    )
    if identity not in SUPPORTED_SPEC_IDENTITIES:
        _fail("spec identity is unsupported")
    if require_current and identity != CURRENT_SPEC_IDENTITY:
        _fail("new runs require the current spec successor")
    _exact(spec.get("contract_file"), "PROJECT_CONTRACT_V2.md", "contract_file")
    expected_manifest = (
        "swivd-local-snapshot-manifest-v4"
        if identity == CURRENT_SPEC_IDENTITY
        else "swivd-local-snapshot-manifest-v3"
        if identity == V4_2_SPEC_IDENTITY
        else "swivd-local-snapshot-manifest-v2"
    )
    _exact(spec.get("manifest_version"), expected_manifest, "manifest_version")
    _exact(spec.get("project_root_mode"), "ENTRYPOINT_RELATIVE", "project_root_mode")
    _exact(
        spec.get("status_defaults"),
        {"research_grade": "RESEARCH_ONLY", "decision_eligible": False, "production_approved": False, "windows_e2e": "UNVERIFIED"},
        "status_defaults",
    )
    network = _mapping(spec.get("network"), "network")
    _exact(network.get("base_url"), "https://api.tushare.pro", "network.base_url")
    _exact(network.get("allow_redirects"), False, "network.allow_redirects")
    _exact(network.get("allowed_apis"), ["trade_cal", "index_classify", "sw_daily", "index_member_all", "daily_basic"], "network.allowed_apis")
    levels = _mapping(_mapping(_mapping(spec.get("axes"), "axes").get("SW2021"), "SW2021").get("levels"), "levels")
    if set(levels) != set(LEVELS):
        _fail("SW2021 levels must be L1/L2/L3")
    for level in LEVELS:
        _exact(levels[level].get("expected_classification_count"), COUNTS[level], f"{level} count")
    membership = _mapping(spec.get("membership"), "membership")
    _exact(membership.get("fetch_states"), ["Y", "N"], "membership.fetch_states")
    _exact(membership.get("interval_policy"), "BOUNDARY_UNKNOWN_WITHOUT_AUTHORITY", "membership.interval_policy")
    stock = _mapping(spec.get("stock_valuation"), "stock_valuation")
    if identity == CURRENT_SPEC_IDENTITY:
        _exact(stock.get("empty_response_policy"), "FAIL_CLOSED", "empty valuation policy")
        _exact(spec.get("output", {}).get("lineage_layout"), "FLAT_ANCESTOR_RAW_V1", "lineage layout policy")
    _exact(stock.get("percentile_fields"), ["pe_ttm", "pb"], "stock.percentile_fields")
    _exact(stock.get("valid_rule"), "FINITE_AND_STRICTLY_POSITIVE", "stock.valid_rule")
    _exact(stock.get("percentile_method"), "EMPIRICAL_CDF_LE", "stock.percentile_method")
    _exact(stock.get("minimum_peer_count"), 5, "stock.minimum_peer_count")
    industry = _mapping(spec.get("industry_valuation"), "industry_valuation")
    _exact(industry.get("stock_aggregation_forbidden"), True, "industry.stock_aggregation_forbidden")
    jobs = _mapping(spec.get("jobs"), "jobs")
    _exact(jobs.get("kinds"), ["UPDATE_LATEST", "MATERIALIZE_DATE"], "jobs.kinds")
    _exact(jobs.get("single_writer"), True, "jobs.single_writer")
    if identity in UPDATE_TARGET_SPEC_IDENTITIES:
        if "latest_fallback" in jobs:
            _fail("current spec must not contain the legacy latest_fallback field")
        update_target = _mapping(jobs.get("update_target_policy"), "jobs.update_target_policy")
        _exact(update_target.get("timezone"), "Asia/Shanghai", "update target timezone")
        _exact(update_target.get("sw_daily_ready_cutoff"), "18:30:00", "update target cutoff")
        _exact(update_target.get("open_day_before_cutoff"), "PREVIOUS_OPEN_DAY", "before-cutoff policy")
        _exact(update_target.get("open_day_at_or_after_cutoff"), "CURRENT_OPEN_DAY", "after-cutoff policy")
        _exact(update_target.get("non_trading_day"), "PREVIOUS_OPEN_DAY", "non-trading policy")
        _exact(update_target.get("already_current"), "SUCCEED_NO_NEW_RUN", "already-current policy")
        _exact(
            update_target.get("selected_target_incomplete"),
            "FAIL_NO_FURTHER_FALLBACK",
            "selected-target completeness policy",
        )
        _exact(jobs.get("request_failure_fallback"), "FORBIDDEN", "request fallback policy")
    else:
        if "update_target_policy" in jobs or "request_failure_fallback" in jobs:
            _fail("legacy spec must not contain current update-target fields")
        _exact(jobs.get("latest_fallback"), "FORBIDDEN", "legacy latest fallback policy")
    if identity not in IDENTITY_SPEC_IDENTITIES and "progress_monotonicity" in jobs:
        _fail("legacy spec must not contain current progress fields")
    if identity in IDENTITY_SPEC_IDENTITIES:
        provider_execution = _mapping(
            spec.get("provider_execution"), "provider_execution"
        )
        _exact(
            provider_execution,
            {
                "live_provider_kind": "LIVE_SECURE_TUSHARE",
                "injected_provider_kind": "TEST_INJECTED_CLIENT",
                "injected_client_data_dir": "STRICT_SYSTEM_TEMP_DESCENDANT_ONLY",
                "request_audit_field": "provider_kind",
                "lineage_provider_kind_must_match": True,
                "status_by_provider": PROVIDER_POLICIES,
            },
            "provider execution",
        )
        _exact(spec.get("audit_version"), "swivd-v2-audit-v2", "audit version")
        _exact(spec.get("ui_catalog_version"), "swivd-ui-catalog-v3", "catalog version")
        _exact(
            spec.get("ui_industry_shard_version"),
            "swivd-industry-shard-v2",
            "industry shard version",
        )
        identity_spec = _mapping(spec.get("industry_identity"), "industry_identity")
        _exact(
            identity_spec.get("resolution_schema_version"),
            "swivd-industry-identity-resolution-v1",
            "identity resolution schema",
        )
        _exact(identity_spec.get("rule_version"), IDENTITY_RULE_VERSION, "identity rule")
        _exact(identity_spec.get("classification_fields"), list(CLASSIFICATION_V3_FIELDS), "identity classification fields")
        _exact(
            identity_spec.get("ui_identity_fields"),
            [
                "industry_uid",
                "catalog_index_code",
                "quote_index_code",
                "member_index_code",
                "current_index_code",
                "identity_state",
                "identity_rule",
                "identity_rule_version",
                "identity_reason_disclosure",
            ],
            "UI identity fields",
        )
        _exact(
            identity_spec.get("identity_reason_disclosure"),
            {
                "direct": "NOT_APPLICABLE_DIRECT_IDENTITY",
                "non_direct": "UNKNOWN_UPSTREAM_INTERNAL_CAUSE",
            },
            "identity reason disclosure",
        )
        _exact(identity_spec.get("revalidate_every_run"), True, "identity revalidation")
        _exact(identity_spec.get("name_fuzzy_join_forbidden"), True, "identity name join")
        scoped = _mapping(identity_spec.get("only_scoped_exception"), "identity scoped exception")
        _exact(scoped.get("industry_uid"), SPECIAL_INDUSTRY_UID, "identity industry uid")
        _exact(scoped.get("allowed_index_codes"), ["850401.SI", "850412.SI"], "identity candidate codes")
        _exact(
            jobs.get("progress_monotonicity"),
            {
                "phase": "NO_REGRESSION",
                "units": "NO_REGRESSION_WITHIN_PHASE",
                "percent": "NO_REGRESSION",
                "terminal_callbacks": "IGNORE",
            },
            "job progress monotonicity",
        )
    elif any(
        key in spec
        for key in (
            "audit_version",
            "ui_catalog_version",
            "ui_industry_shard_version",
            "industry_identity",
            "provider_execution",
        )
    ):
        _fail("legacy spec must not contain v4.2 identity fields")
    server = _mapping(spec.get("server"), "server")
    _exact(server.get("default_host"), "127.0.0.1", "server.default_host")
    _exact(server.get("cors"), False, "server.cors")
    _exact(server.get("auto_open_browser"), False, "server.auto_open_browser")
    output = _mapping(spec.get("output"), "output")
    _exact(output.get("immutable_runs"), True, "output.immutable_runs")
    _exact(output.get("paths_relative_in_manifest"), True, "output.paths_relative_in_manifest")
    _exact(output.get("historical_job_updates_current_pointer"), False, "historical pointer policy")
    if identity in IDENTITY_SPEC_IDENTITIES:
        _exact(
            output.get("pointer_scope"),
            "SW2021_L1_L2_L3_WITH_POINT_IN_TIME_MEMBERS",
            "pointer scope",
        )
        _exact(
            output.get("pointer_record_fields"),
            [
                "pointer_kind",
                "scope",
                "run_id",
                "as_of",
                "target_path",
                "target_sha256",
            ],
            "pointer record fields",
        )
        _exact(
            output.get("historical_index_version"),
            "swivd-historical-index-v2",
            "historical index version",
        )
        _exact(
            output.get("historical_index_v1_policy"),
            "READ_ONLY_ADAPTER_UPGRADE_ON_NEXT_PUBLISH",
            "historical index v1 policy",
        )
        _exact(
            output.get("publication_transaction"),
            {
                "schema_version": "swivd-publication-transaction-v1",
                "intent_path": "transactions/publication.json",
                "states": ["PREPARED", "COMMITTED"],
                "commit_point": "DURABLE_COMMITTED_INTENT_AFTER_POINTER_REPLACE",
                "precommit_recovery": "ROLLBACK_POINTER_AND_RUN_INTERRUPTED",
                "postcommit_recovery": "KEEP_POINTER_AND_IDEMPOTENT_RUN_SUCCEEDED",
                "reader_policy_while_pending": "FAIL_CLOSED",
                "terminal_uniqueness": True,
                "directory_fsync": True,
            },
            "publication transaction",
        )
    elif any(
        key in output
        for key in (
            "pointer_scope",
            "pointer_record_fields",
            "historical_index_version",
            "historical_index_v1_policy",
            "publication_transaction",
        )
    ):
        _fail("legacy spec must not contain current pointer fields")
    rendered = json.dumps(spec, ensure_ascii=False)
    if "/Users/" in rendered or "C:\\Users\\" in rendered:
        _fail("v4 spec embeds a machine-specific user path")
    return spec


def _inventory(root: Path) -> dict[str, Path]:
    if not root.is_dir() or root.is_symlink():
        _fail("run must be a regular directory")
    result: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            _fail("symlinks are forbidden")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            _safe_relative(relative, "artifact path")
            result[relative] = path
    return result


def _positive(value: str) -> Decimal | None:
    if not value.strip():
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _validate_classification(
    root: Path, *, identity_aware: bool
) -> dict[str, list[dict[str, str]]]:
    rows_by_level: dict[str, list[dict[str, str]]] = {}
    for level in LEVELS:
        header, rows = _read_csv(root / "inputs" / "normalized" / f"classification_sw2021_{level.lower()}.csv")
        if identity_aware and tuple(header) != CLASSIFICATION_V3_FIELDS:
            _fail(f"{level} identity-aware classification header mismatch")
        if len(rows) != COUNTS[level]:
            _fail(f"{level} classification count mismatch")
        if any(row.get("src") != "SW2021" or row.get("level") != level or row.get("is_pub") not in {"0", "1"} for row in rows):
            _fail(f"{level} classification semantics mismatch")
        codes = [row.get("index_code", "") for row in rows]
        industries = [row.get("industry_code", "") for row in rows]
        if "" in codes or len(codes) != len(set(codes)) or len(industries) != len(set(industries)):
            _fail(f"{level} classification identity mismatch")
        if identity_aware:
            for row in rows:
                expected_uid = f"SW2021:{level}:{row['industry_code']}"
                _exact(row.get("industry_uid"), expected_uid, "classification industry_uid")
                _exact(row.get("identity_rule_version"), IDENTITY_RULE_VERSION, "classification identity rule version")
                if expected_uid == SPECIAL_INDUSTRY_UID:
                    if row.get("catalog_index_code") not in {"850401.SI", "850412.SI"}:
                        _fail("special classification catalog code is outside the contract")
                    if row.get("quote_index_code") not in {"850401.SI", "850412.SI"}:
                        _fail("special classification quote code is outside the contract")
                    if row.get("member_index_code") != row.get("quote_index_code"):
                        _fail("special classification quote/member codes differ")
                    if row.get("index_code") != row.get("quote_index_code"):
                        _fail("special classification current code differs from quote code")
                    if row.get("identity_state") not in {
                        "DIRECT",
                        "EVIDENCE_GATED_ALIAS",
                        "EVIDENCE_GATED_TRANSITION",
                    }:
                        _fail("special classification state is unsupported")
                else:
                    direct_codes = {
                        row.get("index_code"),
                        row.get("catalog_index_code"),
                        row.get("quote_index_code"),
                        row.get("member_index_code"),
                    }
                    if len(direct_codes) != 1 or "" in direct_codes:
                        _fail("unaffected classification endpoint codes differ")
                    _exact(row.get("identity_state"), "DIRECT", "direct classification state")
        rows_by_level[level] = rows
    l1 = {row["industry_code"] for row in rows_by_level["L1"]}
    l2 = {row["industry_code"] for row in rows_by_level["L2"]}
    if any(row["parent_code"] not in l1 for row in rows_by_level["L2"]):
        _fail("L2 parent missing")
    if any(row["parent_code"] not in l2 for row in rows_by_level["L3"]):
        _fail("L3 parent missing")
    return rows_by_level


def _catalog_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    pe_status = summary.get("pe_status", "UNKNOWN")
    pb_status = summary.get("pb_status", "UNKNOWN")
    return_statuses = [
        summary.get("return_5d_status", "UNKNOWN"),
        summary.get("return_mtd_status", "UNKNOWN"),
        summary.get("return_ytd_status", "UNKNOWN"),
    ]
    not_published = summary.get("valuation_state") == "NA_NOT_PUBLISHED"
    return {
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


def _validate_legacy_archive(root: Path) -> dict[str, Any]:
    legacy_root = root / "legacy" / LEGACY_RUN_ID
    manifest_path = legacy_root / "manifest.json"
    if (
        not manifest_path.is_file()
        or manifest_path.is_symlink()
        or _sha(manifest_path) != LEGACY_MANIFEST_SHA256
    ):
        _fail("legacy SW2014 manifest identity mismatch")
    manifest = _mapping(_load(manifest_path), "legacy manifest")
    records = manifest.get("artifacts")
    if not isinstance(records, list):
        _fail("legacy manifest artifacts must be a list")
    by_path = {
        record.get("path"): record
        for record in records
        if isinstance(record, Mapping) and isinstance(record.get("path"), str)
    }
    actual_files = {
        path.relative_to(legacy_root).as_posix()
        for path in legacy_root.rglob("*")
        if path.is_file()
    }
    if actual_files != set(LEGACY_SELECTED_FILES):
        _fail("legacy SW2014 copied file set mismatch")
    copied_files: list[dict[str, Any]] = []
    for relative in LEGACY_SELECTED_FILES:
        target = legacy_root / relative
        if relative == "manifest.json":
            expected_sha = LEGACY_MANIFEST_SHA256
            expected_bytes = target.stat().st_size
        else:
            record = _mapping(by_path.get(relative), f"legacy artifact {relative}")
            expected_sha = record.get("sha256")
            expected_bytes = record.get("bytes")
        if (
            not target.is_file()
            or target.is_symlink()
            or not isinstance(expected_bytes, int)
            or not isinstance(expected_sha, str)
            or target.stat().st_size != expected_bytes
            or _sha(target) != expected_sha
        ):
            _fail(f"legacy SW2014 artifact identity mismatch: {relative}")
        copied_files.append(
            {"path": relative, "sha256": expected_sha, "bytes": expected_bytes}
        )
    return {
        "run_id": LEGACY_RUN_ID,
        "manifest_sha256": LEGACY_MANIFEST_SHA256,
        "copied_files": copied_files,
    }


def _validate_source_closure(root: Path, manifest: Mapping[str, Any]) -> None:
    records = manifest.get("source_files")
    if not isinstance(records, list):
        _fail("manifest source_files must be a list")
    source_root = root / "source"
    if not source_root.is_dir() or source_root.is_symlink():
        _fail("frozen source directory missing")
    inventory = {
        path.relative_to(source_root).as_posix(): path
        for path in source_root.rglob("*")
        if path.is_file()
    }
    observed: set[str] = set()
    for value in records:
        record = _mapping(value, "source file")
        if set(record) != {"path", "bytes", "sha256"}:
            _fail("source file record schema mismatch")
        relative = _safe_relative(record.get("path"), "source file")
        if relative in observed or relative not in inventory:
            _fail("source file inventory mismatch")
        path = inventory[relative]
        if (
            not isinstance(record.get("bytes"), int)
            or not isinstance(record.get("sha256"), str)
            or not SHA_RE.fullmatch(record["sha256"])
            or path.stat().st_size != record["bytes"]
            or _sha(path) != record["sha256"]
        ):
            _fail(f"source file identity mismatch: {relative}")
        observed.add(relative)
    if observed != set(inventory):
        _fail("source_files does not exactly cover frozen source")
    if not REQUIRED_SOURCE_FILES_V3.issubset(observed):
        _fail("required v3 source closure is incomplete")
    # Exact governed v4.2 snapshot; frozen bytes are still checked above.
    if _sha(root / "manifest.json") == LEGACY_V3_MANIFEST_SHA256:
        return
    for relative, frozen in inventory.items():
        trusted = TRUSTED_PROJECT_ROOT / relative
        if (
            not trusted.is_file()
            or trusted.is_symlink()
            or trusted.stat().st_size != frozen.stat().st_size
            or _sha(trusted) != _sha(frozen)
        ):
            _fail(f"frozen source differs from the trusted project source: {relative}")


def _validate_lineage_copy(
    root: Path,
    *,
    parent_id: str,
    parent_manifest: Mapping[str, Any],
) -> None:
    """Bind copied parent raw evidence to the frozen parent manifest records."""

    lineage_root = root / "lineage" / parent_id
    records = parent_manifest.get("artifacts")
    if not isinstance(records, list):
        _fail("frozen parent artifacts must be a list")
    expected: dict[str, Mapping[str, Any]] = {}
    for value in records:
        record = _mapping(value, "frozen parent artifact")
        relative = _safe_relative(record.get("path"), "frozen parent artifact")
        if not (
            relative.startswith("inputs/raw/")
            or relative.startswith("lineage/")
        ):
            continue
        if relative in expected:
            _fail("frozen parent artifact is duplicated")
        expected[relative] = record
    actual = {
        path.relative_to(lineage_root).as_posix(): path
        for path in lineage_root.rglob("*")
        if path.is_file()
    }
    if set(actual) != {"manifest.json", *expected}:
        _fail("lineage copy file set differs from frozen parent closure")
    for relative, record in expected.items():
        path = actual[relative]
        if (
            not isinstance(record.get("bytes"), int)
            or not isinstance(record.get("sha256"), str)
            or not SHA_RE.fullmatch(record["sha256"])
            or path.stat().st_size != record["bytes"]
            or _sha(path) != record["sha256"]
        ):
            _fail(f"lineage artifact identity mismatch: {relative}")


def _validate_catalog(
    root: Path,
    *,
    as_of: str,
    classifications: Mapping[str, Sequence[Mapping[str, str]]],
) -> None:
    catalog = _mapping(_load(root / "ui" / "catalog.json"), "catalog")
    schema = catalog.get("schema_version")
    if schema not in {"swivd-ui-catalog-v1", "swivd-ui-catalog-v2", "swivd-ui-catalog-v3"}:
        _fail("catalog schema is unsupported")
    _exact(catalog.get("as_of"), as_of, "catalog as_of")
    _exact(catalog.get("levels"), ["L1", "L2", "L3"], "catalog levels")
    _exact(catalog.get("research_grade"), "RESEARCH_ONLY", "catalog research grade")
    _exact(catalog.get("decision_eligible"), False, "catalog decision eligibility")
    _exact(catalog.get("production_approved"), False, "catalog production approval")
    _exact(
        catalog.get("legacy_archive"),
        {"run_id": LEGACY_RUN_ID, "level": "L1", "taxonomy": "SW2014"},
        "catalog legacy archive identity",
    )
    industries = catalog.get("industries")
    if not isinstance(industries, list) or len(industries) != sum(COUNTS.values()):
        _fail("catalog industry count mismatch")
    classification_by_key = {
        (level, row["index_code"]): row
        for level, rows in classifications.items()
        for row in rows
    }
    observed: set[tuple[str, str]] = set()
    for position, value in enumerate(industries):
        industry = _mapping(value, f"catalog industry {position}")
        key = (industry.get("level"), industry.get("index_code"))
        if key in observed or key not in classification_by_key:
            _fail("catalog industry identity mismatch")
        observed.add(key)
        level, code = key
        classification = classification_by_key[key]
        expected_fields = (
            CATALOG_V3_ENTRY_FIELDS
            if schema == "swivd-ui-catalog-v3"
            else CATALOG_V2_ENTRY_FIELDS
            if schema == "swivd-ui-catalog-v2"
            else CATALOG_V1_ENTRY_FIELDS
        )
        if set(industry) != expected_fields:
            _fail("catalog industry field schema mismatch")
        _exact(
            industry.get("industry_name"),
            classification["industry_name"],
            "catalog industry name",
        )
        _exact(
            industry.get("is_pub"),
            int(classification["is_pub"]),
            "catalog publication state",
        )
        expected_shard = f"industries/{level}/{str(code).replace('.', '_')}.json"
        _exact(industry.get("shard"), expected_shard, "catalog shard path")
        relative = _safe_relative(industry.get("shard"), "shard")
        target = root / "ui" / relative
        if not target.is_file() or target.is_symlink():
            _fail("catalog shard missing")
        shard = _mapping(_load(target), "industry shard")
        _exact(
            shard.get("schema_version"),
            "swivd-industry-shard-v2"
            if schema == "swivd-ui-catalog-v3"
            else "swivd-industry-shard-v1",
            "shard schema",
        )
        _exact(shard.get("as_of"), as_of, "shard as_of")
        _exact(shard.get("level"), level, "shard level")
        _exact(shard.get("index_code"), code, "shard code")
        shard_industry = _mapping(shard.get("industry"), "shard industry")
        _exact(shard_industry.get("index_code"), code, "shard industry code")
        _exact(
            shard_industry.get("industry_name"),
            industry["industry_name"],
            "shard industry name",
        )
        _exact(shard_industry.get("as_of"), as_of, "shard industry as_of")
        constituents = shard.get("constituents")
        if not isinstance(constituents, list):
            _fail("shard constituents must be a list")
        _exact(
            industry.get("member_row_count"),
            len(constituents),
            "catalog member row count",
        )
        if schema in {"swivd-ui-catalog-v2", "swivd-ui-catalog-v3"}:
            _exact(
                industry.get("parent_code"),
                classification.get("parent_code", ""),
                "catalog parent code",
            )
            projection = _catalog_summary(shard_industry)
            for field in CATALOG_SUMMARY_FIELDS:
                _exact(industry.get(field), projection[field], f"catalog {field}")
            if schema == "swivd-ui-catalog-v3":
                identity = _mapping(shard.get("identity"), "shard identity")
                if set(identity) != CATALOG_IDENTITY_FIELDS:
                    _fail("shard identity field schema mismatch")
                expected_identity = {
                    "industry_uid": classification.get("industry_uid"),
                    "catalog_index_code": classification.get("catalog_index_code"),
                    "quote_index_code": classification.get("quote_index_code"),
                    "member_index_code": classification.get("member_index_code"),
                    "current_index_code": classification.get("quote_index_code"),
                    "identity_state": classification.get("identity_state"),
                    "identity_rule": classification.get("identity_rule"),
                    "identity_rule_version": classification.get("identity_rule_version"),
                    "identity_reason_disclosure": (
                        "NOT_APPLICABLE_DIRECT_IDENTITY"
                        if classification.get("identity_state") == "DIRECT"
                        else "UNKNOWN_UPSTREAM_INTERNAL_CAUSE"
                    ),
                }
                for field in CATALOG_IDENTITY_FIELDS:
                    _exact(
                        industry.get(field),
                        expected_identity[field],
                        f"catalog identity {field}",
                    )
                    _exact(
                        identity.get(field),
                        expected_identity[field],
                        f"shard identity {field}",
                    )
                    _exact(
                        shard_industry.get(field),
                        expected_identity[field],
                        f"shard industry identity {field}",
                    )
        else:
            _exact(
                industry.get("valuation_state"),
                shard_industry.get("valuation_state", "OK"),
                "catalog valuation state",
            )
    if observed != set(classification_by_key):
        _fail("catalog classification coverage mismatch")


def _validate_peer_table(root: Path, as_of: str) -> None:
    _, rows = _read_csv(root / "tables" / "stock_peer_valuation.csv")
    if not rows:
        _fail("peer table is empty")
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row.get("as_of") != as_of or row.get("level") not in LEVELS:
            _fail("peer row axis/date mismatch")
        groups[(row["level"], row["index_code"])].append(row)
    for group in groups.values():
        members = len({row["ts_code"] for row in group})
        unknown = any(row["membership_state"] != "ACTIVE" for row in group)
        for field in ("pe_ttm", "pb"):
            values = [_positive(row[field]) for row in group if row["membership_state"] == "ACTIVE"]
            valid = [value for value in values if value is not None]
            for row in group:
                if int(row[f"{field}_member_count"]) != members or int(row[f"{field}_valid_n"]) != len(valid):
                    _fail("peer denominator disclosure mismatch")
                expected_coverage = Decimal(len(valid)) / Decimal(members)
                if Decimal(row[f"{field}_coverage"]) != expected_coverage:
                    _fail("peer coverage mismatch")
                current = _positive(row[field])
                if unknown:
                    state, percentile, ties = "MEMBERSHIP_UNKNOWN", None, 0
                elif current is None:
                    state, percentile, ties = "CURRENT_INVALID", None, 0
                elif len(valid) < 5:
                    state, percentile, ties = "INSUFFICIENT_PEERS", None, 0
                else:
                    with localcontext() as context:
                        context.prec = 50
                        percentile = Decimal(sum(value <= current for value in valid)) / Decimal(len(valid)) * Decimal(100)
                    ties = sum(value == current for value in valid)
                    state = "OK"
                if row[f"{field}_percentile_state"] != state:
                    _fail("peer percentile state mismatch")
                rendered = row[f"{field}_percentile_le"].strip()
                if percentile is None:
                    if rendered or int(row[f"{field}_tie_count"]) != 0:
                        _fail("unavailable peer percentile must be blank")
                elif Decimal(rendered) != percentile or int(row[f"{field}_tie_count"]) != ties:
                    _fail("peer percentile recomputation mismatch")


def _decode_raw_rows(path: Path, api_name: str) -> list[dict[str, Any]]:
    payload = _mapping(_load_raw(path), f"raw {api_name}")
    _exact(payload.get("code"), 0, f"raw {api_name} code")
    data = _mapping(payload.get("data"), f"raw {api_name} data")
    expected_fields = list(ENDPOINT_FIELDS[api_name])
    _exact(data.get("fields"), expected_fields, f"raw {api_name} fields")
    items = data.get("items")
    if not isinstance(items, list):
        _fail(f"raw {api_name} items must be a list")
    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, list) or len(item) != len(expected_fields):
            _fail(f"raw {api_name} item width mismatch")
        rows.append(dict(zip(expected_fields, item, strict=True)))
    return rows


def _safe_index_code(value: str) -> str:
    if not value.endswith("_SI") or "/" in value or "\\" in value:
        _fail("request raw path contains an invalid index code")
    return f"{value[:-3]}.SI"


def _validate_request_audit_record(
    root: Path,
    record: Mapping[str, Any],
    *,
    as_of: str,
    classifications: Mapping[str, Sequence[Mapping[str, str]]],
    open_dates: Sequence[str],
    scoped_candidate_queries: bool = False,
) -> str:
    expected_fields = {
        "api_name",
        "params",
        "fields",
        "row_count",
        "row_limit",
        "raw_path",
        "raw_sha256",
        "attempt_count",
        "transport",
        "provider_kind",
    }
    if set(record) != expected_fields:
        _fail("request audit field schema mismatch")
    relative = _safe_relative(record.get("raw_path"), "raw response")
    params = _mapping(record.get("params"), "request params")
    api_name: str
    expected_params: dict[str, str]
    row_limit: int
    membership_scope: tuple[str, str, str] | None = None
    response_scope: dict[str, str] = {}
    response_date_range: tuple[str, str, str] | None = None

    if relative == f"inputs/raw/trade_cal/SSE_20211213_{as_of}.json":
        api_name = "trade_cal"
        expected_params = {
            "exchange": "SSE",
            "start_date": "20211213",
            "end_date": as_of,
        }
        row_limit = 6000
        response_scope = {"exchange": "SSE"}
        response_date_range = ("cal_date", "20211213", as_of)
    elif (match := re.fullmatch(
        r"inputs/raw/index_classify/SW2021_(L1|L2|L3)\.json", relative
    )):
        api_name = "index_classify"
        expected_params = {"level": match.group(1), "src": "SW2021"}
        row_limit = 2000
        response_scope = {"level": match.group(1), "src": "SW2021"}
    elif relative == f"inputs/raw/sw_daily/by_date/{as_of}.json" or (
        (match := re.fullmatch(r"inputs/raw/sw_daily/by_date/(\d{8})\.json", relative))
        and match.group(1) in open_dates
    ):
        date = as_of if relative.endswith(f"/{as_of}.json") else match.group(1)
        api_name = "sw_daily"
        expected_params = {"trade_date": date}
        row_limit = 4000
        response_scope = {"trade_date": date}
    elif (match := re.fullmatch(
        r"inputs/raw/sw_daily/(L1|L2|L3)/([^/]+)_(\d{8})_(\d{8})\.json",
        relative,
    )):
        level, safe_code, start_date, end_date = match.groups()
        code = _safe_index_code(safe_code)
        if start_date != "20211213" or end_date != as_of:
            _fail("ordinary sw_daily request range differs from the baseline contract")
        allowed = {row["index_code"] for row in classifications[level]}
        if code not in allowed or code in IDENTITY_CANDIDATE_CODES:
            _fail("ordinary sw_daily request code is outside its level")
        api_name = "sw_daily"
        expected_params = {
            "ts_code": code,
            "start_date": start_date,
            "end_date": end_date,
        }
        row_limit = 4000
        response_scope = {"ts_code": code}
        response_date_range = ("trade_date", start_date, end_date)
    elif (match := re.fullmatch(
        r"inputs/raw/identity_resolution/sw_daily/([^/]+)_20211213_(\d{8})\.json",
        relative,
    )):
        code = _safe_index_code(match.group(1))
        if code not in IDENTITY_CANDIDATE_CODES or match.group(2) != as_of:
            _fail("identity sw_daily request path is outside the frozen candidates")
        api_name = "sw_daily"
        expected_params = {
            "ts_code": code,
            "start_date": "20211213",
            "end_date": as_of,
        }
        row_limit = 4000
        response_scope = {"ts_code": code}
        response_date_range = ("trade_date", "20211213", as_of)
    elif (match := re.fullmatch(
        r"inputs/raw/index_member_all/round_([12])/(Y|N)/(l[123]_code)/([^/]+)\.json",
        relative,
    )):
        _round, state, selector, safe_code = match.groups()
        level = selector[:2].upper()
        code = _safe_index_code(safe_code)
        allowed_member_codes = {row["index_code"] for row in classifications[level]}
        if scoped_candidate_queries and level == "L3":
            allowed_member_codes.update(IDENTITY_CANDIDATE_CODES)
        if code not in allowed_member_codes:
            _fail("membership request code is outside its selector level")
        api_name = "index_member_all"
        expected_params = {selector: code, "is_new": state}
        row_limit = 2000
        membership_scope = (selector, code, state)
        response_scope = {selector: code, "is_new": state}
    elif (match := re.fullmatch(
        r"inputs/raw/identity_resolution/index_member_all/round_([12])/(Y|N)/([^/]+)\.json",
        relative,
    )):
        _round, state, safe_code = match.groups()
        code = _safe_index_code(safe_code)
        if code not in IDENTITY_CANDIDATE_CODES:
            _fail("identity membership request path is outside the frozen candidates")
        api_name = "index_member_all"
        expected_params = {"l3_code": code, "is_new": state}
        row_limit = 2000
        membership_scope = ("l3_code", code, state)
        response_scope = {"l3_code": code, "is_new": state}
    elif relative == f"inputs/raw/daily_basic/{as_of}.json":
        api_name = "daily_basic"
        expected_params = {"trade_date": as_of}
        row_limit = 6000
        response_scope = {"trade_date": as_of}
    else:
        _fail("request raw path is unsupported")

    _exact(record.get("api_name"), api_name, "request api name")
    _exact(dict(params), expected_params, "request params")
    _exact(record.get("fields"), list(ENDPOINT_FIELDS[api_name]), "request fields")
    _exact(record.get("row_limit"), row_limit, "request row limit")
    rows = _decode_raw_rows(root / relative, api_name)
    _exact(record.get("row_count"), len(rows), "request row count")
    if any(
        any(str(row.get(field)) != expected for field, expected in response_scope.items())
        for row in rows
    ):
        _fail("response rows differ from their request selector")
    if response_date_range is not None:
        field, start_date, end_date = response_date_range
        if any(
            not (start_date <= str(row.get(field, "")) <= end_date)
            for row in rows
        ):
            _fail("response rows differ from their request date range")
    if membership_scope is not None:
        selector, code, state = membership_scope
        if any(
            str(row.get(selector)) != code or str(row.get("is_new")) != state
            for row in rows
        ):
            _fail("membership response differs from its query selector/state")
    attempt_count = record.get("attempt_count")
    if (
        isinstance(attempt_count, bool)
        or not isinstance(attempt_count, int)
        or not 1 <= attempt_count <= 3
    ):
        _fail("request attempt count is invalid")
    return relative


def _validate_derived_replay(root: Path, *, as_of: str) -> None:
    """Rebuild every table/UI shard and require an exact semantic closure."""

    from .v2_pipeline import build_derived

    with tempfile.TemporaryDirectory(prefix="swivd-v2-derived-replay-") as temporary:
        replay_root = Path(temporary) / "snapshot"
        shutil.copytree(
            root / "inputs" / "normalized",
            replay_root / "inputs" / "normalized",
        )
        build_derived(replay_root, as_of=as_of)
        for namespace in ("tables", "ui"):
            actual_root = root / namespace
            replay_namespace = replay_root / namespace
            actual = {
                path.relative_to(actual_root).as_posix(): path
                for path in actual_root.rglob("*")
                if path.is_file()
            }
            expected = {
                path.relative_to(replay_namespace).as_posix(): path
                for path in replay_namespace.rglob("*")
                if path.is_file()
            }
            if set(actual) != set(expected):
                _fail(f"derived {namespace} file set differs from replay")
            for relative in sorted(expected):
                if actual[relative].read_bytes() != expected[relative].read_bytes():
                    _fail(f"derived {namespace} differs from replay: {relative}")


def _csv_value(value: Any) -> str:
    return "" if value is None else str(value)


def _csv_projection(row: Mapping[str, Any], fields: Sequence[str]) -> dict[str, str]:
    return {field: _csv_value(row.get(field)) for field in fields}


def _validate_identity_resolution(
    root: Path,
    *,
    as_of: str,
    classifications: Mapping[str, Sequence[Mapping[str, str]]],
    audit: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> None:
    """Recompute the governed mapping from frozen raw responses.

    Hash closure alone is insufficient: an attacker could rewrite normalized
    files and then recompute every hash.  This check independently decodes the
    raw endpoint envelopes, reruns every identity gate, and compares both the
    normalized projections and the manifest/audit summaries.
    """

    from .core import validate_sw_daily, validate_trade_cal
    from .v2_domain import (
        VALUATION_FIELDS,
        normalize_classifications,
        normalize_daily_basic,
        normalize_members,
        select_members_as_of,
        validate_membership_primary_keys,
    )

    legacy_semantics = manifest.get("schema_version") != "swivd-local-snapshot-manifest-v4"
    evidence_relative = "inputs/normalized/industry_identity_resolution.json"
    evidence_path = root / evidence_relative
    evidence = _mapping(_load(evidence_path), "identity evidence")
    manifest_identity = _mapping(manifest.get("identity_resolution"), "manifest identity")
    expected_manifest_fields = {
        "rule_version",
        "industry_uid",
        "state",
        "current_index_code",
        "catalog_index_code",
        "quote_index_code",
        "member_index_code",
        "evidence_path",
        "evidence_sha256",
    }
    if set(manifest_identity) != expected_manifest_fields:
        _fail("manifest identity field schema mismatch")
    _exact(manifest_identity.get("evidence_path"), evidence_relative, "identity evidence path")
    _exact(manifest_identity.get("evidence_sha256"), _sha(evidence_path), "identity evidence hash")

    raw_by_level = {
        level: _decode_raw_rows(
            root / "inputs" / "raw" / "index_classify" / f"SW2021_{level}.json",
            "index_classify",
        )
        for level in LEVELS
    }
    if any(len(rows) >= 2000 for rows in raw_by_level.values()):
        _fail("successful index_classify response touches the row limit")
    raw_classes = [row for level in LEVELS for row in raw_by_level[level]]
    target_day = _decode_raw_rows(
        root / "inputs" / "raw" / "sw_daily" / "by_date" / f"{as_of}.json",
        "sw_daily",
    )
    if len(target_day) >= 4000:
        _fail("successful target-day sw_daily response touches the row limit")
    histories = {
        code: _decode_raw_rows(
            root
            / "inputs"
            / "raw"
            / "identity_resolution"
            / "sw_daily"
            / f"{code.replace('.', '_')}_20211213_{as_of}.json",
            "sw_daily",
        )
        for code in IDENTITY_CANDIDATE_CODES
    }
    if any(len(rows) >= 4000 for rows in histories.values()):
        _fail("successful identity sw_daily response touches the row limit")
    calendar_header, calendar_rows = _read_csv(
        root / "inputs" / "normalized" / "trade_calendar.csv"
    )
    calendar_fields = ENDPOINT_FIELDS["trade_cal"]
    if tuple(calendar_header) != calendar_fields:
        _fail("normalized trade calendar header mismatch")
    raw_calendar = _decode_raw_rows(
        root
        / "inputs"
        / "raw"
        / "trade_cal"
        / f"SSE_20211213_{as_of}.json",
        "trade_cal",
    )
    if len(raw_calendar) >= 6000:
        _fail("successful trade_cal response touches the row limit")
    try:
        validated_calendar = validate_trade_cal(
            raw_calendar,
            exchange="SSE",
            start_date="20211213",
            end_date=as_of,
            row_limit=None,
        )
    except Exception as exc:
        _fail(f"raw trade calendar replay failed: {getattr(exc, 'code', type(exc).__name__)}")
    expected_calendar = [
        _csv_projection(row, calendar_fields) for row in validated_calendar
    ]
    _exact(calendar_rows, expected_calendar, "normalized trade calendar")
    open_dates = sorted(
        row["cal_date"]
        for row in calendar_rows
        if row.get("is_open") == "1" and "20211213" <= row["cal_date"] <= as_of
    )
    for code, rows in histories.items():
        if not rows:
            continue
        try:
            validate_sw_daily(
                rows,
                whitelist={code: "特钢Ⅲ"},
                start_date="20211213",
                end_date=as_of,
                open_dates=open_dates,
                row_limit=None,
                strict_name_match=True,
            )
        except Exception as exc:
            _fail(
                "raw identity sw_daily replay failed: "
                f"{getattr(exc, 'code', type(exc).__name__)}"
            )
    try:
        quote = resolve_quote_identity(
            raw_classes,
            target_day,
            histories,
            open_dates,
            target_trade_date=as_of,
        )
    except Exception as exc:
        _fail(f"raw quote identity recomputation failed: {getattr(exc, 'code', type(exc).__name__)}")

    candidate_rounds: dict[int, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    for round_number in (1, 2):
        candidate_rounds[round_number] = {}
        for code in IDENTITY_CANDIDATE_CODES:
            safe = code.replace(".", "_")
            candidate_rounds[round_number][code] = {
                state: _decode_raw_rows(
                    root
                    / "inputs"
                    / "raw"
                    / "identity_resolution"
                    / "index_member_all"
                    / f"round_{round_number}"
                    / state
                    / f"{safe}.json",
                    "index_member_all",
                )
                for state in ("Y", "N")
            }
            if any(
                len(rows) >= 2000
                for rows in candidate_rounds[round_number][code].values()
            ):
                _fail("identity membership response touches the row limit")
            for state, rows in candidate_rounds[round_number][code].items():
                try:
                    validate_membership_primary_keys(rows)
                except Exception as exc:
                    _fail(
                        "raw identity membership primary key failed: "
                        f"{getattr(exc, 'code', type(exc).__name__)}"
                    )

    requests = audit.get("requests")
    if not isinstance(requests, list):
        _fail("identity audit requests must be a list")
    main_requests: dict[
        tuple[int, str, str, str], list[dict[str, Any]]
    ] = {}
    main_pattern = re.compile(
        r"^inputs/raw/index_member_all/round_([12])/(Y|N)/(l[123]_code)/[^/]+\.json$"
    )
    for request in requests:
        record = _mapping(request, "identity request")
        relative = str(record.get("raw_path", ""))
        match = main_pattern.match(relative)
        if match is None:
            if relative.startswith("inputs/raw/index_member_all/"):
                _fail("main membership raw path is unsupported")
            continue
        round_number = int(match.group(1))
        state = match.group(2)
        selector = match.group(3)
        params = _mapping(record.get("params"), "main membership params")
        if set(params) != {selector, "is_new"} or str(params.get("is_new")) != state:
            _fail("main membership request params differ from its raw path")
        code = str(params.get(selector, ""))
        expected_relative = (
            f"inputs/raw/index_member_all/round_{round_number}/{state}/"
            f"{selector}/{code.replace('.', '_')}.json"
        )
        if relative != expected_relative:
            _fail("main membership raw path differs from its selector code")
        key = (round_number, state, selector, code)
        if key in main_requests:
            _fail("main membership query is duplicated")
        decoded = _decode_raw_rows(root / relative, "index_member_all")
        if len(decoded) > 2000:
            _fail("main membership response exceeds the row limit")
        try:
            validate_membership_primary_keys(decoded)
        except Exception as exc:
            _fail(
                "raw main membership primary key failed: "
                f"{getattr(exc, 'code', type(exc).__name__)}"
            )
        _exact(record.get("row_count"), len(decoded), "main membership row count")
        main_requests[key] = decoded

    l2_children: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in classifications["L2"]:
        l2_children[row["parent_code"]].append(row)
    l3_children: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in classifications["L3"]:
        if not legacy_semantics and row.get("industry_uid") == SPECIAL_INDUSTRY_UID:
            l3_children[row["parent_code"]].extend({**row, "index_code": code} for code in IDENTITY_CANDIDATE_CODES)
        else:
            l3_children[row["parent_code"]].append(row)
    main_rounds: dict[int, dict[str, list[dict[str, Any]]]] = {
        1: {"Y": [], "N": []},
        2: {"Y": [], "N": []},
    }
    consumed: set[tuple[int, str, str, str]] = set()

    def raw_member_keys(rows: Sequence[Mapping[str, Any]]) -> set[tuple[str, ...]]:
        return {
            tuple(
                _csv_value(row.get(field))
                for field in ENDPOINT_FIELDS["index_member_all"]
            )
            for row in rows
        }

    def consume_query(
        round_number: int,
        state: str,
        selector: str,
        classification: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        key = (round_number, state, selector, classification["index_code"])
        if key not in main_requests:
            _fail("main membership split tree is incomplete")
        consumed.add(key)
        rows = main_requests[key]
        if len(rows) < 2000:
            return rows
        if selector == "l1_code":
            children = l2_children.get(classification["industry_code"], [])
            child_selector = "l2_code"
        elif selector == "l2_code":
            children = l3_children.get(classification["industry_code"], [])
            child_selector = "l3_code"
        else:
            _fail("main membership L3 query touches the row limit")
        if not children:
            _fail("main membership split tree has no children")
        expanded: list[dict[str, Any]] = []
        for child in children:
            expanded.extend(
                consume_query(round_number, state, child_selector, child)
            )
        if not raw_member_keys(rows).issubset(raw_member_keys(expanded)):
            _fail("main membership split omits rows returned by its parent query")
        return expanded

    for round_number in (1, 2):
        for state in ("Y", "N"):
            for l1 in classifications["L1"]:
                main_rounds[round_number][state].extend(
                    consume_query(round_number, state, "l1_code", l1)
                )
    if consumed != set(main_requests):
        _fail("main membership request tree contains an unexpected query")
    canonical_main = {
        round_number: sorted(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for state in ("Y", "N")
            for row in main_rounds[round_number][state]
        )
        for round_number in (1, 2)
    }
    if canonical_main[1] != canonical_main[2]:
        _fail("raw main membership rounds drifted")
    for round_number in (1, 2):
        try:
            validate_membership_primary_keys(
                row
                for state in ("Y", "N")
                for row in main_rounds[round_number][state]
            )
        except Exception as exc:
            _fail(
                "raw combined membership primary key failed: "
                f"{getattr(exc, 'code', type(exc).__name__)}"
            )
    try:
        resolution = resolve_membership_identity(quote, candidate_rounds, main_rounds, membership_as_of=not legacy_semantics and manifest.get("purpose") == "MATERIALIZE_DATE")
    except Exception as exc:
        _fail(f"raw member identity recomputation failed: {getattr(exc, 'code', type(exc).__name__)}")
    recomputed = resolution.to_dict()
    _exact(dict(evidence), recomputed, "recomputed identity evidence")
    _exact(audit.get("identity_resolution"), recomputed, "audit identity evidence")
    audit_evidence = _mapping(audit.get("identity_evidence"), "audit identity hash")
    _exact(audit_evidence.get("path"), evidence_relative, "audit identity path")
    _exact(audit_evidence.get("sha256"), _sha(evidence_path), "audit identity hash")
    expected_manifest = {
        "rule_version": recomputed["rule_version"],
        "industry_uid": recomputed["industry_uid"],
        "state": recomputed["identity_state"],
        "current_index_code": recomputed["current_index_code"],
        "catalog_index_code": recomputed["catalog_index_code"],
        "quote_index_code": recomputed["quote_index_code"],
        "member_index_code": recomputed["member_index_code"],
        "evidence_path": evidence_relative,
        "evidence_sha256": _sha(evidence_path),
    }
    _exact(dict(manifest_identity), expected_manifest, "manifest identity summary")

    base_classifications, _ = normalize_classifications(raw_by_level)
    raw_special = [
        row for row in base_classifications["L3"]
        if row.get("industry_uid") == SPECIAL_INDUSTRY_UID
    ]
    if len(raw_special) != 1:
        _fail("raw special classification is not a singleton")
    normalized_special = [
        row for row in classifications["L3"] if row.get("industry_uid") == SPECIAL_INDUSTRY_UID
    ]
    if len(normalized_special) != 1:
        _fail("normalized special classification is not a singleton")
    expected_special = _csv_projection(
        project_classification(raw_special[0], resolution),
        CLASSIFICATION_V3_FIELDS,
    )
    _exact(dict(normalized_special[0]), expected_special, "normalized special classification")
    for level in LEVELS:
        expected_level: list[dict[str, str]] = []
        for row in base_classifications[level]:
            if row.get("industry_uid") == SPECIAL_INDUSTRY_UID:
                projected = project_classification(row, resolution)
            else:
                code = str(row["index_code"])
                projected = {
                    **dict(row),
                    "catalog_index_code": code,
                    "quote_index_code": code,
                    "member_index_code": code,
                    "industry_uid": f"SW2021:{level}:{row['industry_code']}",
                    "identity_state": "DIRECT",
                    "identity_rule": "CATALOG_EQUALS_QUOTE_AND_MEMBER_CODE",
                    "identity_rule_version": IDENTITY_RULE_VERSION,
                }
            expected_level.append(
                _csv_projection(projected, CLASSIFICATION_V3_FIELDS)
            )
        expected_level.sort(key=lambda row: row["index_code"])
        actual_level = sorted(
            (dict(row) for row in classifications[level]),
            key=lambda row: row["index_code"],
        )
        _exact(actual_level, expected_level, f"normalized {level} classification")

    normalized_histories: dict[str, list[dict[str, str]]] = {}
    for level in LEVELS:
        header, normalized_histories[level] = _read_csv(
            root / "inputs" / "normalized" / f"sw_daily_sw2021_{level.lower()}.csv"
        )
        if tuple(header) != SW_DAILY_V3_FIELDS:
            _fail(f"{level} normalized quote header mismatch")
    normalized_l3_history = normalized_histories["L3"]
    actual_special_history = sorted(
        (
            _csv_projection(row, SW_DAILY_V3_FIELDS)
            for row in normalized_l3_history
            if row.get("industry_uid") == SPECIAL_INDUSTRY_UID
        ),
        key=lambda row: row["trade_date"],
    )
    expected_special_history = sorted(
        (
            _csv_projection(project_quote(row, resolution), SW_DAILY_V3_FIELDS)
            for code in IDENTITY_CANDIDATE_CODES
            for row in histories[code]
        ),
        key=lambda row: row["trade_date"],
    )
    _exact(actual_special_history, expected_special_history, "normalized special quote history")

    direct_rows = [
        row
        for level in LEVELS
        for row in classifications[level]
        if row["industry_uid"] != SPECIAL_INDUSTRY_UID and row["is_pub"] == "1"
    ]
    if len({row["index_code"] for row in direct_rows}) != len(direct_rows):
        _fail("published direct quote code collides across taxonomy levels")
    direct_by_code: dict[str, Mapping[str, str]] = {
        row["index_code"]: row for row in direct_rows
    }
    direct_level = {
        row["index_code"]: level
        for level in LEVELS
        for row in classifications[level]
        if row["industry_uid"] != SPECIAL_INDUSTRY_UID and row["is_pub"] == "1"
    }
    direct_raw_by_key: dict[tuple[str, str], dict[str, str]] = {}
    ordinary_raw_paths = [
        *root.glob("inputs/raw/sw_daily/**/*.json"),
        *root.glob("lineage/**/inputs/raw/sw_daily/**/*.json"),
    ]
    if not ordinary_raw_paths:
        _fail("ordinary sw_daily raw evidence is missing")
    for path in sorted(set(ordinary_raw_paths)):
        rows = _decode_raw_rows(path, "sw_daily")
        if len(rows) >= 4000:
            _fail("successful sw_daily raw response touches the row limit")
        if not rows:
            _fail("ordinary sw_daily raw response is empty")
        observed_codes = {str(row.get("ts_code", "")) for row in rows}
        try:
            validation_bounds = (
                {}
                if path.relative_to(root).as_posix().startswith("lineage/")
                else {
                    "start_date": "20211213",
                    "end_date": as_of,
                    "open_dates": open_dates,
                }
            )
            validate_sw_daily(
                rows,
                whitelist=observed_codes,
                row_limit=None,
                **validation_bounds,
            )
        except Exception as exc:
            _fail(
                "raw ordinary sw_daily replay failed: "
                f"{getattr(exc, 'code', type(exc).__name__)}"
            )
        for row in rows:
            code = str(row.get("ts_code", ""))
            trade_date = str(row.get("trade_date", ""))
            classification = direct_by_code.get(code)
            if classification is None or not ("20211213" <= trade_date <= as_of):
                continue
            if str(row.get("name", "")) != classification["industry_name"]:
                _fail("raw direct quote name differs from classification")
            close = _positive(_csv_value(row.get("close")))
            if close is None:
                _fail("raw direct quote close is not strictly positive")
            projection = _csv_projection(
                {
                    **row,
                    "industry_uid": classification["industry_uid"],
                    "source_ts_code": code,
                    "identity_state": "DIRECT",
                    "identity_rule_version": IDENTITY_RULE_VERSION,
                },
                SW_DAILY_V3_FIELDS,
            )
            key = (code, trade_date)
            previous = direct_raw_by_key.get(key)
            if previous is not None and previous != projection:
                _fail("overlapping direct sw_daily responses disagree")
            direct_raw_by_key[key] = projection

    expected_histories: dict[str, list[dict[str, str]]] = {
        level: [] for level in LEVELS
    }
    for (code, _trade_date), row in direct_raw_by_key.items():
        expected_histories[direct_level[code]].append(row)
    expected_histories["L3"].extend(expected_special_history)
    for level in LEVELS:
        expected = sorted(
            expected_histories[level],
            key=lambda row: (row["ts_code"], row["trade_date"]),
        )
        actual = sorted(
            normalized_histories[level],
            key=lambda row: (row["ts_code"], row["trade_date"]),
        )
        _exact(actual, expected, f"normalized {level} quote history")

    raw_valuation = _decode_raw_rows(
        root / "inputs" / "raw" / "daily_basic" / f"{as_of}.json",
        "daily_basic",
    )
    if len(raw_valuation) >= 6000:
        _fail("successful daily_basic response touches the row limit")
    try:
        normalized_valuation = normalize_daily_basic(raw_valuation, as_of=as_of, allow_empty=legacy_semantics)
    except Exception as exc:
        _fail(f"raw daily_basic replay failed: {getattr(exc, 'code', type(exc).__name__)}")
    valuation_header, actual_valuation = _read_csv(
        root / "inputs" / "normalized" / "stock_valuation_snapshot.csv"
    )
    if tuple(valuation_header) != VALUATION_FIELDS:
        _fail("normalized daily_basic header mismatch")
    expected_valuation = [
        _csv_projection(normalized_valuation[code], VALUATION_FIELDS)
        for code in sorted(normalized_valuation)
    ]
    _exact(actual_valuation, expected_valuation, "normalized daily_basic snapshot")

    normalized_classes = {level: [dict(row) for row in classifications[level]] for level in LEVELS}
    _, l3_paths = normalize_classifications(normalized_classes)
    l3_by_code = {row["index_code"]: row for row in normalized_classes["L3"]}
    projected_main: list[dict[str, Any]] = []
    for state in ("Y", "N"):
        for row in main_rounds[1][state]:
            source_code = str(row.get("l3_code"))
            if source_code in IDENTITY_CANDIDATE_CODES:
                projected_main.append(project_member(row, resolution))
                continue
            classification = l3_by_code.get(source_code)
            if classification is None:
                _fail("raw member L3 code is outside normalized classification")
            projected_main.append(
                {
                    **row,
                    "industry_uid": classification["industry_uid"],
                    "source_l3_code": source_code,
                    "identity_state": classification["identity_state"],
                    "identity_rule_version": classification["identity_rule_version"],
                }
            )
    expected_episodes = normalize_members(projected_main, l3_paths=l3_paths, legacy_display_name_key=legacy_semantics)
    member_header, actual_episodes = _read_csv(
        root / "inputs" / "normalized" / "membership_episodes.csv"
    )
    if tuple(member_header) != MEMBER_V3_FIELDS:
        _fail("normalized membership header mismatch")
    expected_episode_csv = [
        _csv_projection(row, MEMBER_V3_FIELDS) for row in expected_episodes
    ]
    _exact(actual_episodes, expected_episode_csv, "normalized membership episodes")
    snapshot_header, actual_snapshot = _read_csv(
        root / "inputs" / "normalized" / "membership_snapshot.csv"
    )
    if tuple(snapshot_header) != (*MEMBER_V3_FIELDS, "membership_state"):
        _fail("normalized membership snapshot header mismatch")
    expected_snapshot = [
        _csv_projection(row, (*MEMBER_V3_FIELDS, "membership_state"))
        for row in select_members_as_of(expected_episodes, as_of)
    ]
    _exact(actual_snapshot, expected_snapshot, "normalized membership snapshot")


def _validate_runtime_v4(root: Path, value: Any) -> None:
    from .runtime_environment import frozen_dependency_versions

    runtime = _mapping(value, "runtime")
    if set(runtime) != {"python_implementation", "python_version", "platform", "path_policy", "windows_e2e", "dependency_versions", "dependencies_locked", "timezone_available"}:
        _fail("runtime field schema mismatch")
    if runtime["platform"] not in {"Darwin", "Windows", "Linux", "UNKNOWN"} or runtime["python_implementation"] not in {"CPython", "PyPy", "UNKNOWN"}:
        _fail("runtime platform is not a safe enum")
    if not isinstance(runtime["python_version"], str) or re.fullmatch(r"3\.(?:11|12|13|14)\.[0-9]+", runtime["python_version"]) is None:
        _fail("runtime python version is unsupported")
    _exact(runtime["path_policy"], "RELATIVE_RUN_PATHS", "runtime path policy")
    _exact(runtime["windows_e2e"], "UNVERIFIED", "runtime windows evidence")
    if type(runtime["dependencies_locked"]) is not bool or type(runtime["timezone_available"]) is not bool:
        _fail("runtime diagnostic booleans invalid")
    try:
        pins = frozen_dependency_versions(root / "source")
    except (ValueError, OSError):
        _fail("frozen dependency lock invalid")
    versions = _mapping(runtime["dependency_versions"], "runtime dependencies")
    if set(versions) != set(pins):
        _fail("runtime dependency inventory mismatch")
    for version in versions.values():
        if version is not None and (not isinstance(version, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]+)+(?:\.post[0-9]+)?", version) is None):
            _fail("runtime dependency version is not a public release")
    _exact(runtime["dependencies_locked"], all(versions[p] == pins[p] for p in pins), "runtime lock match")


def validate_run_v2(run_dir: str | Path) -> dict[str, Any]:
    root = Path(run_dir)
    inventory = _inventory(root)
    manifest = dict(_mapping(_load(root / "manifest.json"), "manifest"))
    manifest_schema = manifest.get("schema_version")
    if manifest_schema not in {
        "swivd-local-snapshot-manifest-v2",
        "swivd-local-snapshot-manifest-v3",
        "swivd-local-snapshot-manifest-v4",
    }:
        _fail("manifest schema is unsupported")
    flat_lineage = manifest_schema == "swivd-local-snapshot-manifest-v4"
    identity_aware = manifest_schema in {"swivd-local-snapshot-manifest-v3", "swivd-local-snapshot-manifest-v4"}
    if identity_aware and set(manifest) != ({
        "schema_version",
        "contract_version",
        "spec_version",
        "decision_id",
        "run_id",
        "purpose",
        "as_of",
        "created_at",
        "completed_at",
        "execution_status",
        "artifact_publish_state",
        "live_validation_state",
        "live_validation_reason",
        "provider_kind",
        "research_grade",
        "decision_eligible",
        "production_approved",
        "windows_e2e",
        "spec_sha256",
        "contract_sha256",
        "parent",
        "identity_resolution",
        "request_count",
        "runtime",
        "source_files",
        "artifacts",
        "validation",
    } | ({"lineage_layout"} if flat_lineage else set())):
        _fail("identity-aware manifest field schema mismatch")
    if flat_lineage:
        _exact(manifest.get("lineage_layout"), "FLAT_ANCESTOR_RAW_V1", "lineage layout")
    required = {
        "manifest.json", "SHA256SUMS", "audit.json", "offline_validation.json",
        "reports/adversarial_review.md", "ui/catalog.json", "tables/stock_peer_valuation.csv",
        "inputs/normalized/trade_calendar.csv", "inputs/normalized/membership_episodes.csv",
        "inputs/normalized/membership_snapshot.csv", "inputs/normalized/stock_valuation_snapshot.csv",
        f"legacy/{LEGACY_RUN_ID}/manifest.json",
        f"legacy/{LEGACY_RUN_ID}/tables/sw2014_archive.csv",
        f"legacy/{LEGACY_RUN_ID}/tables/sw2014_history.csv",
    }
    for level in LEVELS:
        required.update({f"inputs/normalized/classification_sw2021_{level.lower()}.csv", f"inputs/normalized/sw_daily_sw2021_{level.lower()}.csv", f"tables/industry_summary_{level.lower()}.csv"})
    if identity_aware:
        required.add("inputs/normalized/industry_identity_resolution.json")
    missing = sorted(required - set(inventory))
    if missing:
        _fail(f"missing v2 artifacts: {missing}")
    match = RUN_RE.fullmatch(str(manifest.get("run_id", "")))
    if not match or root.name != manifest["run_id"] or match.group(1) != manifest.get("as_of"):
        _fail("run identity mismatch")
    manifest_identity = (
        manifest.get("spec_version"),
        manifest.get("contract_version"),
        manifest.get("decision_id"),
    )
    if manifest_identity not in SUPPORTED_SPEC_IDENTITIES:
        _fail("manifest contract/spec/decision identity is unsupported")
    if identity_aware:
        _exact(manifest_identity, CURRENT_SPEC_IDENTITY if flat_lineage else V4_2_SPEC_IDENTITY, "manifest identity")
    elif manifest_identity in IDENTITY_SPEC_IDENTITIES:
        _fail("current identity-aware spec cannot use manifest v2")
    _exact(manifest.get("purpose") in {"UPDATE_LATEST", "MATERIALIZE_DATE"}, True, "purpose")
    _exact(manifest.get("execution_status"), "COMPLETED", "execution status")
    if identity_aware:
        provider_kind = manifest.get("provider_kind")
        if provider_kind not in PROVIDER_POLICIES:
            _fail("manifest provider kind is unsupported")
        provider_policy = PROVIDER_POLICIES[str(provider_kind)]
        for field in (
            "artifact_publish_state",
            "live_validation_state",
            "live_validation_reason",
        ):
            _exact(manifest.get(field), provider_policy[field], f"provider {field}")
        if provider_kind == "TEST_INJECTED_CLIENT":
            try:
                resolved_root = root.resolve()
                temp_root = Path(tempfile.gettempdir()).resolve()
                resolved_root.relative_to(temp_root)
            except (OSError, RuntimeError, ValueError):
                _fail("test-provider snapshot is outside the system temporary directory")
            if resolved_root == temp_root:
                _fail("test-provider snapshot must be below the system temporary directory")
    else:
        provider_kind = None
        provider_policy = None
        _exact(manifest.get("live_validation_state"), "PASS", "live validation")
    _exact(manifest.get("research_grade"), "RESEARCH_ONLY", "research grade")
    _exact(manifest.get("decision_eligible"), False, "decision eligibility")
    _exact(manifest.get("production_approved"), False, "production approval")
    if identity_aware:
        _exact(
            manifest.get("validation"),
            {
                "status": "PASS",
                "entrypoint": "swivd.v2_validator.validate_run_v2",
            },
            "manifest validation receipt",
        )
    source_spec = root / "source" / "PROJECT_SPEC_V4.json"
    source_contract = root / "source" / "PROJECT_CONTRACT_V2.md"
    if not source_spec.is_file() or _sha(source_spec) != manifest.get("spec_sha256"):
        _fail("frozen v4 spec identity mismatch")
    source_identity = validate_spec_v4(source_spec)
    _exact(
        (
            source_identity.get("schema_version"),
            source_identity.get("contract_version"),
            source_identity.get("decision_id"),
        ),
        manifest_identity,
        "frozen spec identity",
    )
    if not source_contract.is_file() or _sha(source_contract) != manifest.get("contract_sha256"):
        _fail("frozen v2 contract identity mismatch")
    if identity_aware:
        _validate_source_closure(root, manifest)
    if flat_lineage:
        _validate_runtime_v4(root, manifest.get("runtime"))
    parent = manifest.get("parent")
    if parent is None:
        if any(relative.startswith("lineage/") for relative in inventory):
            _fail("root snapshot must not invent parent lineage")
    else:
        parent_record = _mapping(parent, "manifest.parent")
        if set(parent_record) != {"run_id", "as_of", "manifest_sha256"}:
            _fail("parent record schema mismatch")
        parent_id = parent_record.get("run_id")
        parent_match = RUN_RE.fullmatch(parent_id) if isinstance(parent_id, str) else None
        if parent_match is None:
            _fail("parent run identity invalid")
        if parent_record.get("as_of") != parent_match.group(1):
            _fail("parent as_of differs from parent run identity")
        if flat_lineage and manifest.get("purpose") == "UPDATE_LATEST":
            if parent_record["as_of"] >= manifest["as_of"]:
                _fail("latest parent date must precede current as_of")
        frozen_parent = root / "lineage" / parent_id / "manifest.json"
        if not frozen_parent.is_file() or _sha(frozen_parent) != parent_record.get("manifest_sha256"):
            _fail("parent manifest lineage mismatch")
        if identity_aware:
            frozen_parent_manifest = _mapping(_load(frozen_parent), "frozen parent manifest")
            _exact(frozen_parent_manifest.get("run_id"), parent_id, "parent manifest run_id")
            _exact(
                frozen_parent_manifest.get("as_of"),
                parent_record.get("as_of"),
                "parent manifest as_of",
            )
            _exact(
                frozen_parent_manifest.get("purpose"),
                "UPDATE_LATEST",
                "parent manifest purpose",
            )
            _exact(
                frozen_parent_manifest.get("provider_kind"),
                provider_kind,
                "parent provider kind",
            )
            if flat_lineage:
                from .v2_lineage import validate_flat_lineage
                try:
                    validate_flat_lineage(root, parent_record, provider_kind=str(provider_kind))
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    _fail("lineage artifact identity mismatch: flat evidence rejected")
            else:
                _validate_lineage_copy(root, parent_id=parent_id, parent_manifest=frozen_parent_manifest)
    if "/Users/" in json.dumps(manifest, ensure_ascii=False) or "C:\\Users\\" in json.dumps(manifest, ensure_ascii=False):
        _fail("manifest contains an absolute user path")
    records = manifest.get("artifacts")
    if not isinstance(records, list):
        _fail("manifest artifacts must be a list")
    expected_inventory = {path for path in inventory if path not in {"manifest.json", "SHA256SUMS"}}
    observed: set[str] = set()
    for record in records:
        item = _mapping(record, "artifact")
        relative = _safe_relative(item.get("path"), "artifact")
        if relative in observed or relative not in expected_inventory:
            _fail("artifact inventory mismatch")
        if not isinstance(item.get("bytes"), int) or not isinstance(item.get("sha256"), str) or not SHA_RE.fullmatch(item["sha256"]):
            _fail("artifact metadata invalid")
        path = root / relative
        if path.stat().st_size != item["bytes"] or _sha(path) != item["sha256"]:
            _fail("artifact hash mismatch")
        observed.add(relative)
    if observed != expected_inventory:
        _fail("artifact inventory is incomplete")
    expected_sums = [f"{_sha(path)}  {relative}" for relative, path in sorted(inventory.items()) if relative != "SHA256SUMS"]
    actual_sums = (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    if actual_sums != expected_sums:
        _fail("SHA256SUMS mismatch")
    classifications = _validate_classification(root, identity_aware=identity_aware)
    _validate_peer_table(root, str(manifest["as_of"]))
    legacy_expected = _validate_legacy_archive(root)
    audit = _mapping(_load(root / "audit.json"), "audit")
    _exact(
        audit.get("schema_version"),
        "swivd-v2-audit-v2" if identity_aware else "swivd-v2-audit-v1",
        "audit schema",
    )
    if identity_aware and set(audit) != {
        "schema_version",
        "run_id",
        "as_of",
        "purpose",
        "provider_kind",
        "live_validation_state",
        "live_validation_reason",
        "industry_valuation_source",
        "stock_aggregation_for_industry_valuation",
        "membership_boundary_policy",
        "request_count",
        "requests",
        "history",
        "identity_resolution",
        "identity_evidence",
        "counts",
        "membership_states",
        "legacy_archive",
    }:
        _fail("v2 audit field schema mismatch")
    _exact(audit.get("run_id"), manifest.get("run_id"), "audit run_id")
    _exact(audit.get("as_of"), manifest.get("as_of"), "audit as_of")
    _exact(audit.get("purpose"), manifest.get("purpose"), "audit purpose")
    _exact(audit.get("request_count"), manifest.get("request_count"), "audit request count")
    _exact(audit.get("industry_valuation_source"), "sw_daily", "industry valuation source")
    _exact(audit.get("stock_aggregation_for_industry_valuation"), False, "stock aggregation flag")
    _exact(audit.get("legacy_archive"), legacy_expected, "audit legacy archive closure")
    if identity_aware:
        _exact(audit.get("provider_kind"), provider_kind, "audit provider kind")
        _exact(
            audit.get("live_validation_state"),
            provider_policy["live_validation_state"],
            "audit live validation state",
        )
        _exact(
            audit.get("live_validation_reason"),
            provider_policy["live_validation_reason"],
            "audit live validation reason",
        )
        offline = _mapping(_load(root / "offline_validation.json"), "offline validation")
        _exact(
            dict(offline),
            {
                "schema_version": "swivd-v2-offline-validation-v1",
                "status": "PASS",
                "entrypoint": "swivd.v2_validator.validate_run_v2",
                "provider_kind": provider_kind,
            },
            "offline validation receipt",
        )
    requests = audit.get("requests")
    if not isinstance(requests, list) or len(requests) != manifest.get("request_count"):
        _fail("request audit count mismatch")
    raw_paths: set[str] = set()
    if identity_aware:
        _, request_calendar = _read_csv(
            root / "inputs" / "normalized" / "trade_calendar.csv"
        )
        request_open_dates = sorted(
            row["cal_date"]
            for row in request_calendar
            if row.get("is_open") == "1"
        )
    for request in requests:
        record = _mapping(request, "request")
        if identity_aware:
            relative = _validate_request_audit_record(
                root,
                record,
                as_of=str(manifest["as_of"]),
                classifications=classifications,
                open_dates=request_open_dates,
                scoped_candidate_queries=flat_lineage,
            )
        else:
            relative = _safe_relative(record.get("raw_path"), "raw response")
        if relative in raw_paths or relative not in inventory or _sha(root / relative) != record.get("raw_sha256"):
            _fail("raw response closure mismatch")
        raw_paths.add(relative)
        if identity_aware:
            _exact(record.get("provider_kind"), provider_kind, "request provider kind")
            _exact(
                record.get("transport"),
                provider_policy["request_transport"],
                "request transport",
            )
        if "token" in json.dumps(record, ensure_ascii=False).lower():
            _fail("request audit contains token material")
    if identity_aware:
        current_raw_paths = {
            relative
            for relative in inventory
            if relative.startswith("inputs/raw/")
        }
        if raw_paths != current_raw_paths:
            _fail("request audit does not exactly cover current raw responses")
    if identity_aware:
        catalog_schema = _mapping(_load(root / "ui" / "catalog.json"), "catalog").get(
            "schema_version"
        )
        _exact(catalog_schema, "swivd-ui-catalog-v3", "identity-aware catalog schema")
        _validate_identity_resolution(
            root,
            as_of=str(manifest["as_of"]),
            classifications=classifications,
            audit=audit,
            manifest=manifest,
        )
        identity_summary = _mapping(
            audit.get("identity_resolution"), "report identity resolution"
        )
        expected_report = (
            f"# {manifest['run_id']} 对抗式审查\n\n"
            f"- 截止日：`{manifest['as_of']}`\n- 作业：`{manifest['purpose']}`\n"
            "- 行业估值来源：`sw_daily`\n- 个股聚合替代行业估值：`false`\n"
            f"- 行业身份：`{identity_summary['industry_uid']}` / "
            f"`{identity_summary['identity_state']}`\n"
            f"- 目录/行情/成员代码：`{identity_summary['catalog_index_code']}` / "
            f"`{identity_summary['quote_index_code']}` / "
            f"`{identity_summary['member_index_code']}`\n"
            f"- Provider：`{provider_kind}`\n"
            f"- 实时验证：`{provider_policy['live_validation_state']}` / "
            f"`{provider_policy['live_validation_reason']}`\n"
            "- 成员边界：证据不足时 UNKNOWN，不任意选择\n"
            "- 研究等级：`RESEARCH_ONLY`\n- 决策资格：`false`\n- 生产批准：`false`\n"
        )
        if (root / "reports" / "adversarial_review.md").read_text(
            encoding="utf-8"
        ) != expected_report:
            _fail("adversarial report differs from governed projection")
    _validate_catalog(
        root,
        as_of=str(manifest["as_of"]),
        classifications=classifications,
    )
    if identity_aware:
        _validate_derived_replay(root, as_of=str(manifest["as_of"]))
    for relative, path in inventory.items():
        if relative.startswith("source/web/") and path.suffix.lower() in {
            ".html",
            ".css",
            ".js",
        }:
            text = path.read_text(encoding="utf-8")
            if FORBIDDEN_SOURCE_WEB_REMOTE.search(text):
                _fail("remote source web asset found")
        if path.suffix.lower() not in {
            ".py",
            ".json",
            ".csv",
            ".md",
            ".html",
            ".css",
            ".js",
            ".ndjson",
            ".txt",
        }:
            continue
        text = path.read_text(encoding="utf-8")
        if SECRET_VALUE.search(text):
            _fail("possible secret value found")
        if relative.startswith("ui/") and FORBIDDEN_REMOTE.search(text):
            _fail("remote UI asset found")
    return manifest


__all__ = ["V2ValidationError", "validate_run_v2", "validate_spec_v4"]
