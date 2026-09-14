"""Render the research-only dashboard as one completely self-contained HTML file.

The renderer deliberately has no file-system or network side effects.  It accepts
already validated records, normalises their presentation fields, and returns an
HTML string.  The caller owns writing that string into an immutable run directory.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
import json
import math
import re
from typing import Any


_PAYLOAD_SCHEMA = "swivd-dashboard-payload-v1"
_DEFAULT_FORMULA = "count(history_value <= current_value) / valid_count * 100"
_SW2014_PUBLICATION_STATE = "NOT_PROVIDED_FOR_RETIRED_TAXONOMY"
_SW2014_SELECTION_BASIS = "ALL_CLASSIFIED_L1_ROWS_FOR_RETIRED_TAXONOMY"
_SW2014_CONTINUITY_POLICY = "PER_CODE_OBSERVED_INCEPTION_TO_COMMON_END"
_SW2014_NAME_HISTORY_POLICY = "STABLE_TS_CODE_WITH_SOURCE_NAME_HISTORY"
_COMPONENT_STATUSES = frozenset(
    {"OK", "CURRENT_MISSING", "CURRENT_INVALID", "HISTORY_INSUFFICIENT"}
)
_NOT_PUBLISHED_STATUS = "NA_NOT_PUBLISHED"
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "credential",
    "password",
    "secret",
    "token",
)
_REMOTE_URL_RE = re.compile(r"\bhttps?://[^\s<>'\"]+", flags=re.IGNORECASE)


def _first(row: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return default


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _local_text(value: Any, default: str = "") -> str:
    """Create display text without carrying a remote URL into the artifact."""

    if isinstance(value, Mapping):
        parts: list[str] = []
        for key in ("provider", "name", "api", "interface", "dataset", "source"):
            item = value.get(key)
            if item is not None and not isinstance(item, (Mapping, list, tuple, set)):
                text = _REMOTE_URL_RE.sub("[REMOTE_URL_REDACTED]", _text(item)).strip()
                if text and text not in parts:
                    parts.append(text)
        return " · ".join(parts) or default
    return _REMOTE_URL_RE.sub("[REMOTE_URL_REDACTED]", _text(value, default))


def _number(value: Any) -> str | int | None:
    """Return an exact finite decimal scalar usable by JavaScript ``Number``.

    JSON binary floats would shorten high-precision empirical percentiles and
    make the embedded payload differ from the canonical CSV.  Decimal strings
    keep the provider-derived value exact; the page converts them only for
    drawing and display.
    """

    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (ArithmeticError, TypeError, ValueError):
        return None
    if not number.is_finite():
        return None
    if isinstance(value, int):
        return value
    if number == 0:
        return "0"
    return format(number, "f")


def _boolean(value: Any, *, default: bool | None = None) -> bool | None:
    """Parse manifest booleans without treating non-empty strings as true."""

    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalised = value.strip().lower()
        if normalised in {"false", "0", "no"}:
            return False
        if normalised in {"true", "1", "yes"}:
            return True
    return default


def _json_ready(value: Any) -> Any:
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.lower() for part in _SENSITIVE_KEY_PARTS):
                cleaned[key_text] = "[REDACTED]"
            else:
                cleaned[key_text] = _json_ready(item)
        return cleaned
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(item) for item in value]
    if isinstance(value, Decimal):
        return _number(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _REMOTE_URL_RE.sub("[REMOTE_URL_REDACTED]", value)
    if value is None or isinstance(value, (int, bool)):
        return value
    return str(value)


def _safe_json(value: Any) -> str:
    """Encode JSON safely for an ``application/json`` script element."""

    encoded = json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        encoded.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _canonical_optional_text(
    row: Mapping[str, Any], canonical_name: str, *fallback_names: str
) -> str:
    """Preserve a canonical blank instead of replacing it with a fallback label."""

    if canonical_name in row:
        return _text(row[canonical_name], "")
    return _text(_first(row, *fallback_names), "")


def _required_state(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"summary {field} must be a nonblank canonical state string")
    return value


def _require_matching_component(
    row: Mapping[str, Any], field: str, expected: str
) -> None:
    """Reject a supplied component status that conflicts with its aggregate state."""

    if field in row and row[field] != expected:
        raise ValueError(f"summary {field} conflicts with its canonical aggregate state")


def _derive_valuation_statuses(row: Mapping[str, Any]) -> tuple[str, str, str]:
    state = _required_state(row, "valuation_state")
    if state == "OK":
        pe_status = pb_status = "OK"
    elif state == _NOT_PUBLISHED_STATUS:
        pe_status = pb_status = _NOT_PUBLISHED_STATUS
    else:
        match = re.fullmatch(r"PE=([^;]+);PB=([^;]+)", state)
        if match is None:
            raise ValueError("summary valuation_state has a noncanonical format")
        pe_status, pb_status = match.groups()
        if pe_status not in _COMPONENT_STATUSES or pb_status not in _COMPONENT_STATUSES:
            raise ValueError("summary valuation_state contains an unknown component status")
        if pe_status == pb_status == "OK":
            raise ValueError("summary valuation_state must collapse two OK components to OK")

    _require_matching_component(row, "pe_status", pe_status)
    _require_matching_component(row, "pb_status", pb_status)
    return state, pe_status, pb_status


def _derive_return_statuses(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    state = _required_state(row, "return_state")
    if state == "OK":
        statuses = ("OK", "OK", "OK")
    elif state == _NOT_PUBLISHED_STATUS:
        statuses = (_NOT_PUBLISHED_STATUS,) * 3
    else:
        parts = state.split(";")
        if len(parts) != 3 or any(part not in _COMPONENT_STATUSES for part in parts):
            raise ValueError("summary return_state must contain canonical 5D/MTD/YTD states")
        if all(part == "OK" for part in parts):
            raise ValueError("summary return_state must collapse three OK components to OK")
        statuses = (parts[0], parts[1], parts[2])

    for field, expected in zip(
        ("return_5d_status", "return_mtd_status", "return_ytd_status"), statuses
    ):
        _require_matching_component(row, field, expected)
    return state, statuses[0], statuses[1], statuses[2]


def _normalise_current(row: Mapping[str, Any], *, default_taxonomy: str) -> dict[str, Any]:
    valuation_state, pe_status, pb_status = _derive_valuation_statuses(row)
    return_state, return_5d_status, return_mtd_status, return_ytd_status = (
        _derive_return_statuses(row)
    )
    pe_percentile = _number(
        _first(row, "pe_percentile", "pe_percentile_le", "pe_pctile", "pe_percentile_rank")
    )
    pb_percentile = _number(
        _first(row, "pb_percentile", "pb_percentile_le", "pb_pctile", "pb_percentile_rank")
    )
    index_code = _text(_first(row, "index_code", "ts_code", "code"))
    industry_name = _text(_first(row, "industry_name", "name"), index_code or "未知行业")
    taxonomy = _text(_first(row, "taxonomy", "src"), default_taxonomy)
    if taxonomy != default_taxonomy:
        raise ValueError(
            f"{default_taxonomy} summary contains a row from another taxonomy: {taxonomy}"
        )
    normalised = {
        "taxonomy": taxonomy,
        "index_code": index_code,
        "industry_name": industry_name,
        "as_of": _text(_first(row, "as_of", "trade_date", "date")),
        "close": _number(_first(row, "close")),
        "pe": _number(_first(row, "pe")),
        "pb": _number(_first(row, "pb")),
        "pe_percentile": pe_percentile,
        "pb_percentile": pb_percentile,
        "pe_valid_count": _number(_first(row, "pe_valid_count")),
        "pb_valid_count": _number(_first(row, "pb_valid_count")),
        "pe_tie_count": _number(_first(row, "pe_tie_count")),
        "pb_tie_count": _number(_first(row, "pb_tie_count")),
        "pe_tie_ratio": _number(_first(row, "pe_tie_ratio")),
        "pb_tie_ratio": _number(_first(row, "pb_tie_ratio")),
        "pe_first_valid_date": _text(_first(row, "pe_first_valid_date")),
        "pe_last_valid_date": _text(_first(row, "pe_last_valid_date")),
        "pb_first_valid_date": _text(_first(row, "pb_first_valid_date")),
        "pb_last_valid_date": _text(_first(row, "pb_last_valid_date")),
        "pe_label": _canonical_optional_text(row, "pe_label", "pe_history_label"),
        "pb_label": _canonical_optional_text(row, "pb_label", "pb_history_label"),
        "pe_status": pe_status,
        "pb_status": pb_status,
        "return_5d": _number(_first(row, "return_5d")),
        "return_mtd": _number(_first(row, "return_mtd")),
        "return_ytd": _number(_first(row, "return_ytd")),
        "return_5d_anchor_date": _text(_first(row, "return_5d_anchor_date")),
        "return_mtd_anchor_date": _text(_first(row, "return_mtd_anchor_date")),
        "return_ytd_anchor_date": _text(_first(row, "return_ytd_anchor_date")),
        "return_5d_status": return_5d_status,
        "return_mtd_status": return_mtd_status,
        "return_ytd_status": return_ytd_status,
        "valuation_state": valuation_state,
        "return_state": return_state,
    }
    if default_taxonomy == "SW2014":
        publication_flag = row.get("is_pub")
        if publication_flag not in (None, ""):
            raise ValueError(
                "SW2014 retired-taxonomy summary must not carry a publication flag"
            )
        normalised["is_pub"] = None
    return normalised


def _normalise_history(row: Mapping[str, Any], *, default_taxonomy: str) -> dict[str, Any]:
    index_code = _text(_first(row, "index_code", "ts_code", "code"))
    taxonomy = _text(_first(row, "taxonomy", "src"), default_taxonomy)
    if taxonomy != default_taxonomy:
        raise ValueError(
            f"{default_taxonomy} history contains a row from another taxonomy: {taxonomy}"
        )
    if default_taxonomy == "SW2014":
        if not isinstance(row.get("industry_name"), str) or not row["industry_name"].strip():
            raise ValueError("SW2014 history requires a nonblank frozen industry_name")
        if not isinstance(row.get("source_name"), str) or not row["source_name"].strip():
            raise ValueError("SW2014 history requires a nonblank source_name")
        industry_name = row["industry_name"]
    else:
        industry_name = _text(
            _first(row, "industry_name", "name"), index_code or "未知行业"
        )
    normalised = {
        "taxonomy": taxonomy,
        "index_code": index_code,
        "industry_name": industry_name,
        "trade_date": _text(_first(row, "trade_date", "as_of", "date")),
        "close": _number(_first(row, "close")),
        "pe": _number(_first(row, "pe")),
        "pb": _number(_first(row, "pb")),
    }
    # SW2014 is retired and its classification response does not provide a
    # publication flag.  A non-null flag is a contract violation rather than a
    # value that the renderer may rewrite.  SW2021 remains unchanged.
    if default_taxonomy == "SW2021":
        normalised["is_pub"] = _text(_first(row, "is_pub"))
    else:
        publication_flag = row.get("is_pub")
        if publication_flag not in (None, ""):
            raise ValueError(
                "SW2014 retired-taxonomy history must not carry a publication flag"
            )
        normalised["is_pub"] = None
        # Preserve the original provider label byte-for-byte as a distinct
        # attribute.  It is never used as the stable identity or overwritten by
        # the frozen classification display name.
        normalised["source_name"] = row["source_name"]
    return normalised


def _as_mapping_rows(value: Any, *, label: str) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)):
        raise TypeError(f"{label} must be a sequence of mappings")
    if not isinstance(value, Sequence):
        raise TypeError(f"{label} must be a sequence of mappings")
    if any(not isinstance(item, Mapping) for item in value):
        raise TypeError(f"every item in {label} must be a mapping")
    return list(value)


def _split_archive(
    sw2014_archive: Any,
    explicit_summary: Sequence[Mapping[str, Any]] | None,
    explicit_history: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    summary = _as_mapping_rows(explicit_summary, label="sw2014_summary")
    history = _as_mapping_rows(explicit_history, label="sw2014_history")
    if isinstance(sw2014_archive, Mapping):
        if not summary:
            summary = _as_mapping_rows(
                _first(sw2014_archive, "summary", "current_rows", "archive_summary", default=[]),
                label="sw2014_archive.summary",
            )
        if not history:
            history = _as_mapping_rows(
                _first(sw2014_archive, "history", "rows", "archive_history", default=[]),
                label="sw2014_archive.history",
            )
    elif not history:
        history = _as_mapping_rows(sw2014_archive, label="sw2014_archive")
    return summary, history


def _derive_archive_end_rows(
    history: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Select raw archive end rows when no calculated archive summary is supplied.

    This fallback does not calculate percentiles or fill values.  It merely makes
    the independently validated final raw observation visible, while every
    unavailable derived field remains explicitly unavailable in the UI.
    """

    latest: dict[str, Mapping[str, Any]] = {}
    for row in history:
        code = _text(_first(row, "index_code", "ts_code", "code"))
        trade_date = _text(_first(row, "trade_date", "as_of", "date"))
        if not code:
            continue
        incumbent = latest.get(code)
        incumbent_date = (
            _text(_first(incumbent, "trade_date", "as_of", "date")) if incumbent else ""
        )
        if incumbent is None or trade_date > incumbent_date:
            latest[code] = row
    return list(latest.values())


def _metadata_value(
    metadata: Mapping[str, Any], audit: Mapping[str, Any], *names: str, default: Any = None
) -> Any:
    sources: list[Mapping[str, Any]] = [metadata, audit]
    for source in (metadata, audit):
        for container_name in ("statuses", "status", "run_status", "summary"):
            container = source.get(container_name)
            if isinstance(container, Mapping):
                sources.append(container)
    for source in sources:
        found = _first(source, *names)
        if found is not None:
            return found
    return default


def _normalise_metadata(
    metadata: Mapping[str, Any] | None, audit: Mapping[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    meta = metadata if isinstance(metadata, Mapping) else {}
    audit_map = audit if isinstance(audit, Mapping) else {}
    statuses = {
        name: _text(_metadata_value(meta, audit_map, name, default="UNKNOWN"), "UNKNOWN")
        for name in (
            "execution_status",
            "artifact_publish_state",
            "live_validation_state",
            "sw2021_axis_state",
            "sw2014_axis_state",
        )
    }
    for source in (meta, audit_map):
        axes = source.get("axes")
        if not isinstance(axes, Mapping):
            continue
        for taxonomy, status_key in (
            ("SW2021", "sw2021_axis_state"),
            ("SW2014", "sw2014_axis_state"),
        ):
            if statuses[status_key] != "UNKNOWN" or taxonomy not in axes:
                continue
            candidate = axes[taxonomy]
            if isinstance(candidate, Mapping):
                candidate = _first(candidate, "state", "status", default="UNKNOWN")
            statuses[status_key] = _text(candidate, "UNKNOWN")
    statuses.update(
        {
            "research_grade": _text(
                _metadata_value(meta, audit_map, "research_grade", default="RESEARCH_ONLY"),
                "RESEARCH_ONLY",
            ),
            "decision_eligible": _boolean(
                _metadata_value(meta, audit_map, "decision_eligible", default=False),
                default=None,
            ),
            "production_approved": _boolean(
                _metadata_value(meta, audit_map, "production_approved", default=False),
                default=None,
            ),
        }
    )
    normalised = {
        "run_id": _text(_metadata_value(meta, audit_map, "run_id", default="UNKNOWN"), "UNKNOWN"),
        "as_of": _text(_metadata_value(meta, audit_map, "as_of", default="UNKNOWN"), "UNKNOWN"),
        "source": _local_text(
            _metadata_value(meta, audit_map, "source", "data_source", default="UNKNOWN"),
            "UNKNOWN",
        ),
        "taxonomy": _text(
            _metadata_value(meta, audit_map, "taxonomy", "classification", default="UNKNOWN"),
            "UNKNOWN",
        ),
        "spec_version": _text(
            _metadata_value(meta, audit_map, "spec_version", "schema_version", default="UNKNOWN"),
            "UNKNOWN",
        ),
        "contract_version": _text(
            _metadata_value(meta, audit_map, "contract_version", default="swivd-contract-v1.0.0"),
            "UNKNOWN",
        ),
        "formula": _text(
            _metadata_value(meta, audit_map, "formula", "percentile_formula", default=_DEFAULT_FORMULA),
            _DEFAULT_FORMULA,
        ),
        "archive_end": _text(
            _metadata_value(meta, audit_map, "archive_end", "sw2014_end", default="20211213 前最后开市日")
        ),
        "generated_at": _text(
            _metadata_value(meta, audit_map, "generated_at", "created_at", default="UNKNOWN"),
            "UNKNOWN",
        ),
        "statuses": statuses,
    }
    return normalised, _json_ready(audit_map)


def _ensure_sw2014_policy(audit_payload: dict[str, Any]) -> dict[str, Any]:
    """Preserve the approved retired-taxonomy policy in the embedded audit.

    Missing policy keys are supplied from the approved project-level contract so
    older offline rebuild inputs remain displayable.  Conflicting non-empty
    values fail closed; the renderer never silently rewrites them.
    """

    axes = audit_payload.get("axes")
    if not isinstance(axes, dict):
        raise ValueError("audit.axes must be a mapping")

    legacy = axes.get("SW2014")
    if not isinstance(legacy, dict):
        raise ValueError("audit.axes.SW2014 must be a mapping")

    approved = {
        "publication_state": _SW2014_PUBLICATION_STATE,
        "selection_basis": _SW2014_SELECTION_BASIS,
        "continuity_policy": _SW2014_CONTINUITY_POLICY,
    }
    if legacy.get("state") != "PASS":
        # A blocked/partial axis carries only failure evidence.  Do not mutate
        # that evidence by injecting publication or name-history assertions.
        return {
            **approved,
            "selected_count": None,
            "publication_flag_null_count": None,
        }
    for key, expected in approved.items():
        observed = legacy.get(key)
        if observed not in (None, "", expected):
            raise ValueError(
                f"audit.axes.SW2014.{key} conflicts with the approved retired-taxonomy policy"
            )
        legacy[key] = expected

    return {
        **approved,
        "selected_count": legacy.get("selected_count"),
        "publication_flag_null_count": legacy.get("publication_flag_null_count"),
    }


def _derive_sw2014_name_history(
    history: Sequence[Mapping[str, Any]],
    summary: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Recompute the exact, code-keyed provider-name segment evidence."""

    by_code: dict[str, list[Mapping[str, Any]]] = {}
    seen_keys: set[tuple[str, str]] = set()
    for row in history:
        code = row.get("index_code")
        trade_date = row.get("trade_date")
        source_name = row.get("source_name")
        industry_name = row.get("industry_name")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("SW2014 history name audit requires a nonblank index_code")
        if not isinstance(trade_date, str) or re.fullmatch(r"[0-9]{8}", trade_date) is None:
            raise ValueError("SW2014 history name audit requires YYYYMMDD trade_date")
        if not isinstance(source_name, str) or not source_name.strip():
            raise ValueError("SW2014 history name audit requires nonblank source_name")
        if not isinstance(industry_name, str) or not industry_name.strip():
            raise ValueError("SW2014 history name audit requires nonblank industry_name")
        key = (code, trade_date)
        if key in seen_keys:
            raise ValueError("SW2014 history name audit found a duplicate code/date")
        seen_keys.add(key)
        by_code.setdefault(code, []).append(row)

    summary_names: dict[str, str] = {}
    for row in summary:
        code = row.get("index_code")
        industry_name = row.get("industry_name")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("SW2014 summary name audit requires a nonblank index_code")
        if not isinstance(industry_name, str) or not industry_name.strip():
            raise ValueError("SW2014 summary name audit requires a nonblank industry_name")
        if code in summary_names:
            raise ValueError("SW2014 summary name audit found a duplicate index_code")
        summary_names[code] = industry_name

    if set(summary_names) != set(by_code):
        raise ValueError("SW2014 summary/history code sets conflict in the name audit")

    segments: list[dict[str, Any]] = []
    renamed_codes: list[str] = []
    for code in sorted(by_code):
        rows = sorted(by_code[code], key=lambda item: item["trade_date"])
        display_names = {row["industry_name"] for row in rows}
        if len(display_names) != 1:
            raise ValueError(f"SW2014 industry_name drifted within stable code {code}")
        display_name = next(iter(display_names))
        if summary_names[code] != display_name:
            raise ValueError(f"SW2014 summary/history industry_name conflicts for {code}")
        if rows[-1]["source_name"] != display_name:
            raise ValueError(
                f"SW2014 terminal source_name does not equal frozen industry_name for {code}"
            )

        code_segments: list[dict[str, Any]] = []
        for row in rows:
            if code_segments and code_segments[-1]["source_name"] == row["source_name"]:
                code_segments[-1]["last_date"] = row["trade_date"]
                code_segments[-1]["row_count"] += 1
            else:
                code_segments.append(
                    {
                        "index_code": code,
                        "source_name": row["source_name"],
                        "first_date": row["trade_date"],
                        "last_date": row["trade_date"],
                        "row_count": 1,
                    }
                )
        if len(code_segments) > 1:
            renamed_codes.append(code)
        segments.extend(code_segments)

    return {
        "code_count": len(by_code),
        "renamed_code_count": len(renamed_codes),
        "renamed_codes": renamed_codes,
        "segment_count": len(segments),
        "segments": segments,
    }


def _ensure_sw2014_name_history(
    audit_payload: dict[str, Any],
    history: Sequence[Mapping[str, Any]],
    summary: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Verify that the embedded audit exactly matches the history labels."""

    axes = audit_payload.get("axes")
    if not isinstance(axes, Mapping):
        raise ValueError("audit.axes must be a mapping for SW2014 name history")
    legacy = axes.get("SW2014")
    if not isinstance(legacy, Mapping):
        raise ValueError("audit.axes.SW2014 must be a mapping for name history")
    if legacy.get("state") != "PASS":
        if history or summary:
            raise ValueError("blocked SW2014 axis must not carry name history rows")
        return {}
    if legacy.get("name_history_policy") != _SW2014_NAME_HISTORY_POLICY:
        raise ValueError("audit.axes.SW2014.name_history_policy is missing or conflicting")
    observed = legacy.get("name_history")
    if not isinstance(observed, Mapping):
        raise ValueError("audit.axes.SW2014.name_history must be a mapping")
    required_keys = {
        "code_count",
        "renamed_code_count",
        "renamed_codes",
        "segment_count",
        "segments",
    }
    if set(observed) != required_keys:
        raise ValueError("audit.axes.SW2014.name_history has missing or unexpected fields")
    if not isinstance(observed.get("renamed_codes"), list) or not isinstance(
        observed.get("segments"), list
    ):
        raise ValueError("SW2014 name_history lists have invalid types")
    for field in ("code_count", "renamed_code_count", "segment_count"):
        if type(observed.get(field)) is not int or observed[field] < 0:
            raise ValueError(f"SW2014 name_history.{field} must be a nonnegative integer")
    renamed_codes = observed["renamed_codes"]
    if any(not isinstance(code, str) or not code.strip() for code in renamed_codes):
        raise ValueError("SW2014 name_history.renamed_codes contains an invalid code")
    if renamed_codes != sorted(set(renamed_codes)):
        raise ValueError("SW2014 name_history.renamed_codes must be unique and sorted")
    segment_keys = {"index_code", "source_name", "first_date", "last_date", "row_count"}
    if any(not isinstance(item, Mapping) or set(item) != segment_keys for item in observed["segments"]):
        raise ValueError("SW2014 name_history segment shape is invalid")
    for segment in observed["segments"]:
        for field in ("index_code", "source_name"):
            if not isinstance(segment[field], str) or not segment[field].strip():
                raise ValueError(f"SW2014 name_history segment {field} is invalid")
        for field in ("first_date", "last_date"):
            if not isinstance(segment[field], str) or re.fullmatch(
                r"[0-9]{8}", segment[field]
            ) is None:
                raise ValueError(f"SW2014 name_history segment {field} is invalid")
        if type(segment["row_count"]) is not int or segment["row_count"] <= 0:
            raise ValueError("SW2014 name_history segment row_count must be positive")
    segment_order = [
        (segment["index_code"], segment["first_date"]) for segment in observed["segments"]
    ]
    if segment_order != sorted(segment_order):
        raise ValueError("SW2014 name_history segments must use canonical ordering")

    expected = _derive_sw2014_name_history(history, summary)
    if dict(observed) != expected:
        raise ValueError("audit.axes.SW2014.name_history conflicts with history rows")
    return expected


def render_dashboard(
    current_rows: Sequence[Mapping[str, Any]],
    sw2021_history: Sequence[Mapping[str, Any]],
    sw2014_archive: Any = None,
    metadata: Mapping[str, Any] | None = None,
    audit: Mapping[str, Any] | None = None,
    *,
    sw2014_summary: Sequence[Mapping[str, Any]] | None = None,
    sw2014_history: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Return a complete, offline dashboard document.

    ``sw2014_archive`` may be either a history sequence or a mapping with
    ``summary`` and ``history`` sequences.  The explicit keyword arguments take
    precedence.  This compatibility keeps the renderer independent of the
    acquisition layer while preserving one canonical embedded payload.
    """

    if isinstance(current_rows, (str, bytes, Mapping)) or not isinstance(current_rows, Sequence):
        raise TypeError("current_rows must be a sequence of mappings")
    if isinstance(sw2021_history, (str, bytes, Mapping)) or not isinstance(
        sw2021_history, Sequence
    ):
        raise TypeError("sw2021_history must be a sequence of mappings")

    if any(not isinstance(row, Mapping) for row in current_rows):
        raise TypeError("every item in current_rows must be a mapping")
    if any(not isinstance(row, Mapping) for row in sw2021_history):
        raise TypeError("every item in sw2021_history must be a mapping")

    current = [_normalise_current(row, default_taxonomy="SW2021") for row in current_rows]
    current.sort(key=lambda row: (row["industry_name"], row["index_code"]))
    history_2021 = [
        _normalise_history(row, default_taxonomy="SW2021") for row in sw2021_history
    ]
    history_2021.sort(key=lambda row: (row["index_code"], row["trade_date"]))

    archive_summary_rows, archive_history_rows = _split_archive(
        sw2014_archive, sw2014_summary, sw2014_history
    )
    if not archive_summary_rows and archive_history_rows:
        archive_summary_rows = _derive_archive_end_rows(archive_history_rows)
    archive_summary = [
        _normalise_current(row, default_taxonomy="SW2014") for row in archive_summary_rows
    ]
    archive_summary.sort(key=lambda row: (row["industry_name"], row["index_code"]))
    history_2014 = [
        _normalise_history(row, default_taxonomy="SW2014") for row in archive_history_rows
    ]
    history_2014.sort(key=lambda row: (row["index_code"], row["trade_date"]))
    meta, audit_payload = _normalise_metadata(metadata, audit)
    sw2014_policy = _ensure_sw2014_policy(audit_payload)
    name_history = _ensure_sw2014_name_history(
        audit_payload, history_2014, archive_summary
    )
    if name_history:
        sw2014_policy["name_history_policy"] = _SW2014_NAME_HISTORY_POLICY
        sw2014_policy["name_history"] = name_history

    payload = {
        "schema_version": _PAYLOAD_SCHEMA,
        "metadata": meta,
        "audit": audit_payload,
        "sw2014_policy": sw2014_policy,
        "current_rows": current,
        "sw2021_history": history_2021,
        "sw2014_summary": archive_summary,
        "sw2014_history": history_2014,
    }
    payload_json = _safe_json(payload)

    return _HTML_TEMPLATE.replace("__SWIVD_PAYLOAD__", payload_json)


_HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>申万行业估值全景仪表盘</title>
  <style>
    :root {
      --ink: #17202b;
      --muted: #687384;
      --subtle: #8a95a4;
      --paper: #f5f2ec;
      --surface: #fffdf9;
      --surface-2: #f0ece4;
      --line: #ddd6ca;
      --line-strong: #c9bfae;
      --accent: #0f5f5b;
      --accent-soft: #dcecea;
      --navy: #17324d;
      --positive: #b9483d;
      --positive-soft: #f7e4df;
      --negative: #18816f;
      --negative-soft: #dff1ec;
      --warning: #a26717;
      --warning-soft: #f6ead4;
      --danger: #a13d35;
      --danger-soft: #f6dfdc;
      --shadow: 0 14px 35px rgba(36, 44, 50, .08);
      --radius: 14px;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      --sans: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    }

    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
      margin: 0;
      color: var(--ink);
      background:
        linear-gradient(135deg, rgba(15, 95, 91, .045), transparent 32rem),
        var(--paper);
      font-family: var(--sans);
      font-size: 14px;
      line-height: 1.55;
    }
    button, input, select { font: inherit; }
    button { color: inherit; }
    .shell { max-width: 1480px; margin: 0 auto; padding: 28px 26px 48px; }
    .topbar {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 24px;
      align-items: end;
      margin-bottom: 18px;
    }
    .eyebrow {
      color: var(--accent);
      font-size: 12px;
      font-weight: 760;
      letter-spacing: .12em;
      text-transform: uppercase;
    }
    h1 { margin: 4px 0 6px; font-size: clamp(25px, 3vw, 38px); line-height: 1.2; letter-spacing: -.025em; }
    .subtitle { max-width: 880px; color: var(--muted); font-size: 15px; }
    .asof-box {
      min-width: 210px;
      padding: 13px 16px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: rgba(255,253,249,.82);
      box-shadow: var(--shadow);
    }
    .asof-box span { display: block; color: var(--muted); font-size: 12px; }
    .asof-box strong { display: block; margin-top: 1px; font-family: var(--mono); font-size: 18px; }
    .research-banner {
      display: flex;
      gap: 12px;
      align-items: flex-start;
      padding: 13px 16px;
      border: 1px solid #d7b880;
      border-left: 5px solid var(--warning);
      border-radius: 10px;
      background: var(--warning-soft);
      margin-bottom: 18px;
    }
    .research-banner strong { white-space: nowrap; color: #6d4610; }
    .research-banner span { color: #6e5a3a; }
    .meta-strip {
      display: grid;
      grid-template-columns: repeat(4, minmax(150px, 1fr));
      gap: 1px;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--line);
      box-shadow: var(--shadow);
      margin-bottom: 16px;
    }
    .meta-item { min-height: 77px; padding: 12px 14px; background: var(--surface); }
    .meta-item .label { color: var(--muted); font-size: 11px; letter-spacing: .05em; }
    .meta-item .value { margin-top: 4px; font-weight: 680; overflow-wrap: anywhere; }
    .meta-item.formula { grid-column: span 2; }
    .meta-item.formula .value { font-family: var(--mono); font-size: 12px; }
    .status-row { display: flex; flex-wrap: wrap; gap: 7px; margin: 0 0 16px; }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      min-height: 27px;
      padding: 4px 9px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--surface);
      color: var(--muted);
      font-size: 11px;
    }
    .badge b { color: var(--ink); }
    .badge.good { border-color: #9fcfc2; background: var(--negative-soft); }
    .badge.warn { border-color: #ddc08e; background: var(--warning-soft); }
    .badge.bad { border-color: #dfaaa4; background: var(--danger-soft); }
    .toolbar {
      position: sticky;
      top: 0;
      z-index: 20;
      display: flex;
      gap: 12px;
      align-items: center;
      justify-content: space-between;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 13px;
      background: rgba(255,253,249,.96);
      box-shadow: 0 8px 24px rgba(36, 44, 50, .1);
      backdrop-filter: blur(12px);
    }
    .tabs { display: flex; min-width: 0; gap: 3px; overflow-x: auto; scrollbar-width: thin; }
    .tab {
      flex: 0 0 auto;
      padding: 8px 11px;
      border: 0;
      border-radius: 8px;
      background: transparent;
      cursor: pointer;
      color: var(--muted);
      font-weight: 650;
    }
    .tab:hover { background: var(--surface-2); color: var(--ink); }
    .tab[aria-selected="true"] { background: var(--navy); color: white; }
    .search-wrap { position: relative; flex: 0 0 min(260px, 29vw); }
    .search-wrap input {
      width: 100%;
      min-height: 36px;
      padding: 7px 11px 7px 31px;
      border: 1px solid var(--line-strong);
      border-radius: 8px;
      outline: none;
      background: white;
    }
    .search-wrap input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
    .search-icon { position: absolute; left: 10px; top: 8px; color: var(--subtle); }
    .panel { display: none; padding-top: 18px; }
    .panel.active { display: block; }
    .section-head { display: flex; gap: 16px; align-items: end; justify-content: space-between; margin: 4px 0 12px; }
    .section-head h2 { margin: 0; font-size: 20px; }
    .section-head p { margin: 2px 0 0; color: var(--muted); }
    .cards { display: grid; grid-template-columns: repeat(5, minmax(135px, 1fr)); gap: 11px; margin-bottom: 14px; }
    .card {
      min-width: 0;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--surface);
      box-shadow: var(--shadow);
    }
    .card .kicker { color: var(--muted); font-size: 11px; }
    .card .big { margin-top: 5px; font: 730 25px/1.15 var(--mono); letter-spacing: -.03em; }
    .card .note { margin-top: 5px; color: var(--subtle); font-size: 11px; }
    .grid-2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
    .surface {
      min-width: 0;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--surface);
      box-shadow: var(--shadow);
    }
    .surface h3 { margin: 0 0 2px; font-size: 16px; }
    .surface .hint { margin-bottom: 13px; color: var(--muted); font-size: 12px; }
    .table-wrap { overflow: auto; border: 1px solid var(--line); border-radius: 10px; background: white; }
    table { width: 100%; border-collapse: collapse; white-space: nowrap; }
    th, td { padding: 10px 11px; border-bottom: 1px solid #ebe6dd; text-align: right; }
    th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align: left; }
    th {
      position: sticky;
      top: 0;
      z-index: 2;
      background: #f3efe8;
      color: #535f6e;
      font-size: 11px;
      font-weight: 720;
      letter-spacing: .02em;
    }
    th[data-sort] { cursor: pointer; user-select: none; }
    th[data-sort]:hover { color: var(--accent); }
    tbody tr:hover { background: #faf7f1; }
    tbody tr:last-child td { border-bottom: 0; }
    td.num { font-family: var(--mono); font-variant-numeric: tabular-nums; }
    .up { color: var(--positive); }
    .down { color: var(--negative); }
    .na { color: var(--subtle); }
    .position-pill { display: inline-block; min-width: 64px; padding: 2px 7px; border-radius: 999px; text-align: center; font-size: 11px; }
    .p0 { background: #dcefe8; color: #176e60; }
    .p1 { background: #e8f1dc; color: #54712a; }
    .p2 { background: #f2edda; color: #76631d; }
    .p3 { background: #f6e5d8; color: #94552f; }
    .p4 { background: #f4ddda; color: #9d4037; }
    .pna { background: #ececec; color: #727272; }
    .heat-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(142px, 1fr)); gap: 9px; }
    .heat-tile { min-height: 104px; padding: 12px; border: 1px solid rgba(80,80,80,.12); border-radius: 11px; }
    .heat-tile .name { font-weight: 720; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .heat-tile .value { margin-top: 7px; font: 740 22px/1 var(--mono); }
    .heat-tile .meta { margin-top: 7px; font-size: 11px; opacity: .78; }
    .controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    select, .seg button {
      min-height: 35px;
      padding: 7px 10px;
      border: 1px solid var(--line-strong);
      border-radius: 8px;
      background: white;
    }
    select { min-width: 210px; }
    .seg { display: inline-flex; padding: 2px; border-radius: 9px; background: var(--surface-2); }
    .seg button { min-height: 30px; border: 0; background: transparent; cursor: pointer; }
    .seg button.active { background: var(--navy); color: white; }
    .chart-box { position: relative; min-height: 390px; }
    .chart-box svg { display: block; width: 100%; height: 390px; overflow: visible; }
    .chart-empty { display: grid; place-items: center; min-height: 310px; color: var(--muted); text-align: center; }
    .chart-stats { display: flex; flex-wrap: wrap; gap: 16px; margin-top: 8px; color: var(--muted); font-size: 12px; }
    .chart-stats b { color: var(--ink); font-family: var(--mono); }
    .rank-list { display: grid; gap: 7px; }
    .rank-row { display: grid; grid-template-columns: 28px minmax(95px, 150px) 1fr 76px; gap: 9px; align-items: center; }
    .rank-no { color: var(--subtle); font: 11px var(--mono); }
    .rank-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 650; }
    .rank-track { position: relative; height: 20px; border-radius: 4px; background: #eee9e0; overflow: hidden; }
    .rank-bar { position: absolute; top: 0; bottom: 0; }
    .rank-bar.up { left: 50%; background: #d87a70; }
    .rank-bar.down { right: 50%; background: #53a998; }
    .rank-zero { position: absolute; top: 0; bottom: 0; left: 50%; width: 1px; background: #a7a095; }
    .rank-value { text-align: right; font: 12px var(--mono); }
    .scatter-tooltip {
      position: absolute;
      z-index: 3;
      display: none;
      max-width: 220px;
      padding: 8px 10px;
      border-radius: 8px;
      background: rgba(23,32,43,.94);
      color: white;
      font-size: 12px;
      pointer-events: none;
      box-shadow: var(--shadow);
    }
    .method-note { margin-top: 14px; padding: 12px 14px; border-radius: 10px; background: var(--accent-soft); color: #385d5b; font-size: 12px; }
    .archive-banner { margin-bottom: 13px; padding: 12px 14px; border-radius: 10px; background: #e8edf2; color: #435363; }
    .archive-policy { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 9px; margin-bottom: 14px; }
    .policy-item { min-width: 0; padding: 11px 12px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface); }
    .policy-item .label { display: block; margin-bottom: 4px; color: var(--muted); font-size: 11px; }
    .policy-item code { color: var(--navy); font: 11px/1.45 var(--mono); overflow-wrap: anywhere; white-space: normal; }
    .rename-summary { margin: 10px 0 13px; color: var(--muted); font-size: 12px; }
    .code-chip { display: inline-block; margin: 3px 4px 3px 0; padding: 3px 7px; border: 1px solid #a9c9c5; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font: 11px var(--mono); }
    .empty { padding: 32px 18px; border: 1px dashed var(--line-strong); border-radius: 10px; color: var(--muted); text-align: center; }
    .footer { margin-top: 22px; padding-top: 14px; border-top: 1px solid var(--line); color: var(--muted); font-size: 11px; }
    .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }

    @media (max-width: 980px) {
      .topbar { grid-template-columns: 1fr; }
      .asof-box { width: 100%; }
      .meta-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .grid-2 { grid-template-columns: 1fr; }
      .archive-policy { grid-template-columns: 1fr; }
      .toolbar { align-items: stretch; flex-direction: column; }
      .search-wrap { flex-basis: auto; width: 100%; }
    }
    @media (max-width: 580px) {
      .shell { padding: 18px 12px 36px; }
      .meta-strip { grid-template-columns: 1fr; }
      .meta-item.formula { grid-column: auto; }
      .cards { grid-template-columns: 1fr; }
      .section-head { align-items: flex-start; flex-direction: column; }
      .rank-row { grid-template-columns: 24px 90px 1fr 62px; }
    }
    @media print {
      .toolbar { position: static; }
      .panel { display: block !important; break-before: page; }
      .panel:first-of-type { break-before: auto; }
      .surface, .card { box-shadow: none; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div>
        <div class="eyebrow">SW Industry Valuation · L1</div>
        <h1>申万行业估值全景仪表盘</h1>
        <div class="subtitle">用官方行业指数原始 PE、PB 与收盘价，描述各行业在同一分类版本内的历史位置和区间表现。</div>
      </div>
      <div class="asof-box"><span>当前轴日期（显式 as_of）</span><strong id="asof">—</strong></div>
    </header>

    <div class="research-banner" role="note">
      <strong>RESEARCH ONLY</strong>
      <span>本页面只做历史分布描述，不构成选股、估值模型、回测、交易信号或投资建议；历史位置不预测未来收益。</span>
    </div>

    <section class="meta-strip" aria-label="数据口径">
      <div class="meta-item"><div class="label">数据来源</div><div class="value" id="meta-source">—</div></div>
      <div class="meta-item"><div class="label">分类与层级</div><div class="value" id="meta-taxonomy">—</div></div>
      <div class="meta-item"><div class="label">运行编号</div><div class="value" id="meta-run">—</div></div>
      <div class="meta-item"><div class="label">合同 / 规格版本</div><div class="value" id="meta-version">—</div></div>
      <div class="meta-item formula"><div class="label">历史百分位公式（当前观测包含在样本内）</div><div class="value" id="meta-formula">—</div></div>
      <div class="meta-item"><div class="label">生成时间</div><div class="value" id="meta-generated">—</div></div>
      <div class="meta-item"><div class="label">SW2014 档案截止</div><div class="value" id="meta-archive-end">—</div></div>
    </section>
    <div class="status-row" id="status-row" aria-label="运行状态"></div>

    <div class="toolbar">
      <nav class="tabs" role="tablist" aria-label="仪表盘视图">
        <button class="tab" role="tab" aria-selected="true" aria-controls="overview" data-tab="overview">总览</button>
        <button class="tab" role="tab" aria-selected="false" aria-controls="heatmap" data-tab="heatmap" tabindex="-1">热力图</button>
        <button class="tab" role="tab" aria-selected="false" aria-controls="history" data-tab="history" tabindex="-1">历史走势</button>
        <button class="tab" role="tab" aria-selected="false" aria-controls="ranking" data-tab="ranking" tabindex="-1">涨跌排行</button>
        <button class="tab" role="tab" aria-selected="false" aria-controls="scatter" data-tab="scatter" tabindex="-1">PE-PB 象限</button>
        <button class="tab" role="tab" aria-selected="false" aria-controls="details" data-tab="details" tabindex="-1">数据明细</button>
        <button class="tab" role="tab" aria-selected="false" aria-controls="archive" data-tab="archive" tabindex="-1">SW2014 历史档案</button>
      </nav>
      <label class="search-wrap">
        <span class="search-icon" aria-hidden="true">⌕</span>
        <span class="sr-only">搜索行业或代码</span>
        <input id="industry-search" type="search" placeholder="搜索行业或指数代码" autocomplete="off">
      </label>
    </div>

    <section class="panel active" id="overview" role="tabpanel">
      <div class="section-head"><div><h2>当前全景</h2><p>SW2021 一级行业当前观测；无效或样本不足字段明确留空。</p></div></div>
      <div class="cards" id="overview-cards"></div>
      <div class="surface"><h3>行业快照</h3><div class="hint">点击表头排序；样本 N 与并列比例分别按 PE、PB 独立计算。</div><div id="overview-table"></div></div>
    </section>

    <section class="panel" id="heatmap" role="tabpanel" hidden>
      <div class="section-head"><div><h2>估值历史位置热力图</h2><p>颜色只对应经验分布位置，不代表价值判断。</p></div></div>
      <div class="grid-2">
        <div class="surface"><h3>PE 历史百分位</h3><div class="hint">percentile_le · 当前观测包含在样本内</div><div class="heat-grid" id="pe-heat"></div></div>
        <div class="surface"><h3>PB 历史百分位</h3><div class="hint">percentile_le · 当前观测包含在样本内</div><div class="heat-grid" id="pb-heat"></div></div>
      </div>
    </section>

    <section class="panel" id="history" role="tabpanel" hidden>
      <div class="section-head">
        <div><h2>SW2021 历史走势</h2><p>只展示冻结的官方原始时间序列；不填补缺失值。</p></div>
        <div class="controls"><label>行业 <select id="history-industry"></select></label><div class="seg" id="history-metric" aria-label="历史指标"><button class="active" data-metric="pe">PE</button><button data-metric="pb">PB</button><button data-metric="close">收盘价</button></div></div>
      </div>
      <div class="surface"><div class="chart-box" id="history-chart"></div><div class="chart-stats" id="history-stats"></div></div>
    </section>

    <section class="panel" id="ranking" role="tabpanel" hidden>
      <div class="section-head"><div><h2>官方指数收盘价收益排行</h2><p>精确锚点缺失时不计算，不以日涨跌幅连乘替代。</p></div><div class="seg" id="rank-period" aria-label="排行区间"><button class="active" data-period="return_5d">5 日</button><button data-period="return_mtd">MTD</button><button data-period="return_ytd">YTD</button></div></div>
      <div class="surface"><div class="rank-list" id="rank-list"></div></div>
    </section>

    <section class="panel" id="scatter" role="tabpanel" hidden>
      <div class="section-head"><div><h2>PE-PB 象限</h2><p>线性坐标展示当前原始值；中线为当前行业样本中位数。</p></div></div>
      <div class="surface"><div class="chart-box" id="scatter-chart"><div class="scatter-tooltip" id="scatter-tooltip"></div></div><div class="method-note">横轴为 PE，纵轴为 PB。点的颜色对应 PE 历史位置；无法同时取得正数 PE、PB 的行业不进入图形，但仍保留在数据明细中。</div></div>
    </section>

    <section class="panel" id="details" role="tabpanel" hidden>
      <div class="section-head"><div><h2>数据明细</h2><p>原始值、样本量、并列观测和每项状态并列披露。</p></div></div>
      <div id="details-table"></div>
    </section>

    <section class="panel" id="archive" role="tabpanel" hidden>
      <div class="section-head"><div><h2>SW2014 历史档案</h2><p>旧分类版本独立保存，不与 SW2021 拼接、映射或跨版本排序。</p></div><div class="controls"><label>行业 <select id="archive-industry"></select></label><div class="seg" id="archive-metric" aria-label="档案指标"><button class="active" data-metric="pe">PE</button><button data-metric="pb">PB</button><button data-metric="close">收盘价</button></div></div></div>
      <div class="archive-banner" id="archive-state"></div>
      <div class="archive-policy" aria-label="SW2014 退役分类口径">
        <div class="policy-item"><span class="label">发布字段状态</span><code id="archive-publication-state">—</code></div>
        <div class="policy-item"><span class="label">样本选择依据</span><code id="archive-selection-basis">—</code></div>
        <div class="policy-item"><span class="label">历史连续性策略</span><code id="archive-continuity-policy">—</code></div>
        <div class="policy-item"><span class="label">选定一级行业数</span><code id="archive-selected-count">—</code></div>
        <div class="policy-item"><span class="label">发布标记空值数</span><code id="archive-null-count">—</code></div>
        <div class="policy-item"><span class="label">历史名称保真策略</span><code id="archive-name-policy">—</code></div>
      </div>
      <div class="surface" style="margin-bottom:14px"><h3>历史名称分段</h3><div class="hint"><code>ts_code</code> 是跨期稳定身份；<code>source_name</code> 是各交易日原始 <code>sw_daily.name</code> 标签；<code>industry_name</code> 是冻结分类的末期显示名。名称只作为标签展示，不用于拼接或重映射身份。</div><div class="rename-summary" id="archive-rename-summary"></div><div id="archive-name-table"></div></div>
      <div class="surface"><div class="chart-box" id="archive-chart"></div><div class="chart-stats" id="archive-stats"></div></div>
      <div class="surface" style="margin-top:14px"><h3>档案末值摘要</h3><div class="hint">仅描述档案期末在 SW2014 自身历史中的位置，不表示当前估值。</div><div id="archive-table"></div></div>
    </section>

    <footer class="footer">数据与口径以本文件内嵌 payload、同一运行的 manifest 和审计产物为准。页面不联网、不自动刷新，且不产生任何交易或生产权限。</footer>
  </main>

  <script id="swivd-data" type="application/json">__SWIVD_PAYLOAD__</script>
  <script>
  (() => {
    'use strict';
    const data = JSON.parse(document.getElementById('swivd-data').textContent);
    const current = Array.isArray(data.current_rows) ? data.current_rows : [];
    const history21 = Array.isArray(data.sw2021_history) ? data.sw2021_history : [];
    const archiveSummary = Array.isArray(data.sw2014_summary) ? data.sw2014_summary : [];
    const history14 = Array.isArray(data.sw2014_history) ? data.sw2014_history : [];
    const meta = data.metadata || {};
    const archivePolicy = data.sw2014_policy || {};
    const archiveNameHistory = archivePolicy.name_history || null;
    const archiveAxisAudit = (((data.audit || {}).axes || {}).SW2014) || {};
    let query = '';
    let historyMetric = 'pe';
    let archiveMetric = 'pe';
    let rankPeriod = 'return_5d';

    const el = id => document.getElementById(id);
    const esc = value => String(value == null ? '' : value)
      .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
    const num = value => {
      if (value === null || value === undefined || value === '') return null;
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    };
    const fmt = (value, digits = 2) => {
      const n = num(value);
      return n === null ? '<span class="na">—</span>' : n.toLocaleString('zh-CN', {minimumFractionDigits: digits, maximumFractionDigits: digits});
    };
    const fmtText = value => value === null || value === undefined || value === '' ? '<span class="na">—</span>' : esc(value);
    const fmtPctile = value => {
      const n = num(value);
      return n === null ? '<span class="na">—</span>' : `${n.toFixed(1)}%`;
    };
    const fmtTie = value => {
      const n = num(value);
      return n === null ? '<span class="na">—</span>' : `${(n * 100).toFixed(2)}%`;
    };
    const fmtReturn = value => {
      const n = num(value);
      if (n === null) return '<span class="na">—</span>';
      const cls = n > 0 ? 'up' : n < 0 ? 'down' : '';
      const sign = n > 0 ? '+' : '';
      return `<span class="${cls}">${sign}${(n * 100).toFixed(2)}%</span>`;
    };
    const median = values => {
      const clean = values.map(num).filter(v => v !== null).sort((a, b) => a - b);
      if (!clean.length) return null;
      const mid = Math.floor(clean.length / 2);
      return clean.length % 2 ? clean[mid] : (clean[mid - 1] + clean[mid]) / 2;
    };
    const match = row => !query || `${row.industry_name || ''} ${row.source_name || ''} ${row.index_code || ''}`.toLowerCase().includes(query);
    const positionClass = value => {
      const n = num(value);
      if (n === null) return 'pna';
      return `p${Math.max(0, Math.min(4, Math.floor(n === 100 ? 4 : n / 20)))}`;
    };
    const positionPill = (value, label) => `<span class="position-pill ${positionClass(value)}">${num(value) === null ? '不可用' : esc(label || `${Number(value).toFixed(1)}%`)}</span>`;
    const statusClass = value => {
      const text = String(value || '').toUpperCase();
      if (text.includes('FAIL') || text.includes('BLOCK') || text.includes('ERROR') || text.includes('INVALID') || text.includes('DENIED')) return 'bad';
      if (text.includes('UNKNOWN') || text.includes('NOT_VALIDATED') || text.includes('UNVALIDATED') || text.includes('NOT_COMPLETE') || text.includes('INCOMPLETE') || text.includes('PARTIAL') || text.includes('WARN')) return 'warn';
      if (text.includes('PASS') || text.includes('COMPLETE') || text.includes('SUCCESS') || text.includes('VALIDATED')) return 'good';
      return 'warn';
    };

    function fillMetadata() {
      el('asof').textContent = meta.as_of || 'UNKNOWN';
      el('meta-source').textContent = meta.source || 'UNKNOWN';
      el('meta-taxonomy').textContent = meta.taxonomy || 'SW2021 L1';
      el('meta-run').textContent = meta.run_id || 'UNKNOWN';
      el('meta-version').textContent = `${meta.contract_version || 'UNKNOWN'} / ${meta.spec_version || 'UNKNOWN'}`;
      el('meta-formula').textContent = meta.formula || 'UNKNOWN';
      el('meta-generated').textContent = meta.generated_at || 'UNKNOWN';
      el('meta-archive-end').textContent = meta.archive_end || 'UNKNOWN';
      const labels = {
        execution_status: '执行', artifact_publish_state: '候选产物', live_validation_state: '实时校验',
        sw2021_axis_state: 'SW2021', sw2014_axis_state: 'SW2014', research_grade: '研究等级'
      };
      const statuses = meta.statuses || {};
      const badges = Object.entries(labels).map(([key, label]) =>
        `<span class="badge ${statusClass(statuses[key])}"><span>${label}</span><b>${esc(statuses[key] ?? 'UNKNOWN')}</b></span>`
      );
      badges.push(`<span class="badge ${statuses.decision_eligible === false ? 'good' : 'bad'}"><span>决策资格</span><b>${statuses.decision_eligible === false ? '否' : '是 / UNKNOWN'}</b></span>`);
      badges.push(`<span class="badge ${statuses.production_approved === false ? 'good' : 'bad'}"><span>生产批准</span><b>${statuses.production_approved === false ? '否' : '是 / UNKNOWN'}</b></span>`);
      el('status-row').innerHTML = badges.join('');
      el('archive-publication-state').textContent = archivePolicy.publication_state || 'UNKNOWN';
      el('archive-selection-basis').textContent = archivePolicy.selection_basis || 'UNKNOWN';
      el('archive-continuity-policy').textContent = archivePolicy.continuity_policy || 'UNKNOWN';
      el('archive-selected-count').textContent = archivePolicy.selected_count ?? 'UNKNOWN';
      el('archive-null-count').textContent = archivePolicy.publication_flag_null_count ?? 'UNKNOWN';
      el('archive-name-policy').textContent = archivePolicy.name_history_policy || '未声明（轴已阻断）';
      const archiveState = archiveAxisAudit.state || statuses.sw2014_axis_state || 'UNKNOWN';
      if (archiveState === 'PASS') {
        el('archive-state').innerHTML = `轴状态：<b>PASS</b>。退役分类的 <code>is_pub</code> 未提供（<code>null</code>）；档案按全部 28 个 L1 身份纳入，不按发布标记筛选，也不生成“已发布”状态。该页不表示 ${esc(meta.as_of || '当前日期')} 的估值。`;
      } else {
        const reasonCodes = Array.isArray(archiveAxisAudit.reason_codes) ? archiveAxisAudit.reason_codes : [];
        const error = archiveAxisAudit.error && typeof archiveAxisAudit.error === 'object' ? archiveAxisAudit.error : {};
        const reasons = [...reasonCodes];
        if (error.reason_code && !reasons.includes(error.reason_code)) reasons.push(error.reason_code);
        const reasonText = reasons.length ? reasons.join('、') : (error.message || '未提供可核验原因');
        el('archive-state').innerHTML = `轴状态：<b>${esc(archiveState)}</b>。SW2014 名称历史没有形成合规证据，因此未声明名称策略、未展示历史分段。失败原因：<b>${esc(reasonText)}</b>。`;
      }
    }

    function renderCards() {
      const filtered = current.filter(match);
      const pe = filtered.map(r => r.pe).filter(v => num(v) !== null && num(v) > 0);
      const pb = filtered.map(r => r.pb).filter(v => num(v) !== null && num(v) > 0);
      const bothAvailable = filtered.filter(r => num(r.pe_percentile) !== null && num(r.pb_percentile) !== null).length;
      const rising = filtered.filter(r => num(r.return_5d) !== null && num(r.return_5d) > 0).length;
      const cards = [
        ['行业数', filtered.length, query ? `搜索后 / 全部 ${current.length}` : 'SW2021 L1 当前轴'],
        ['PE 中位数', median(pe), `有效当前值 ${pe.length}`],
        ['PB 中位数', median(pb), `有效当前值 ${pb.length}`],
        ['双指标百分位可用', bothAvailable, `占当前行业 ${filtered.length ? (bothAvailable / filtered.length * 100).toFixed(1) : '0.0'}%`],
        ['近 5 日上涨行业', rising, `有收益数据 ${filtered.filter(r => num(r.return_5d) !== null).length}`]
      ];
      el('overview-cards').innerHTML = cards.map(([label, value, note], index) =>
        `<div class="card"><div class="kicker">${esc(label)}</div><div class="big">${index === 1 || index === 2 ? fmt(value) : fmt(value, 0)}</div><div class="note">${esc(note)}</div></div>`
      ).join('');
    }

    const overviewColumns = [
      ['industry_name', '行业', 'text'], ['index_code', '指数代码', 'text'], ['pe', 'PE', 'number'], ['pe_percentile', 'PE 百分位', 'percentile'],
      ['pb', 'PB', 'number'], ['pb_percentile', 'PB 百分位', 'percentile'], ['return_5d', '5 日', 'return'],
      ['return_mtd', 'MTD', 'return'], ['return_ytd', 'YTD', 'return']
    ];
    const detailColumns = [
      ...overviewColumns,
      ['pe_valid_count', 'PE N', 'integer'], ['pe_tie_count', 'PE 并列数', 'integer'], ['pe_tie_ratio', 'PE 并列比例', 'tie'],
      ['pe_first_valid_date', 'PE 首个有效日', 'text'], ['pe_last_valid_date', 'PE 末个有效日', 'text'], ['pe_status', 'PE 状态', 'text'],
      ['pb_valid_count', 'PB N', 'integer'], ['pb_tie_count', 'PB 并列数', 'integer'], ['pb_tie_ratio', 'PB 并列比例', 'tie'],
      ['pb_first_valid_date', 'PB 首个有效日', 'text'], ['pb_last_valid_date', 'PB 末个有效日', 'text'], ['pb_status', 'PB 状态', 'text'],
      ['valuation_state', '估值字段状态', 'text'], ['return_state', '收益状态', 'text']
    ];
    const archiveColumns = [
      ['industry_name', '行业', 'text'], ['index_code', '指数代码', 'text'], ['as_of', '档案末日', 'text'],
      ['pe', 'PE 末值', 'number'], ['pe_percentile', 'PE 档案百分位', 'percentile'], ['pe_valid_count', 'PE N', 'integer'], ['pe_tie_ratio', 'PE 并列比例', 'tie'],
      ['pb', 'PB 末值', 'number'], ['pb_percentile', 'PB 档案百分位', 'percentile'], ['pb_valid_count', 'PB N', 'integer'], ['pb_tie_ratio', 'PB 并列比例', 'tie'],
      ['valuation_state', '状态', 'text']
    ];
    const archiveNameColumns = [
      ['index_code', '稳定身份 ts_code', 'text'], ['source_name', '原始名称 source_name', 'text'],
      ['first_date', '分段首日', 'text'], ['last_date', '分段末日', 'text'], ['row_count', '观测数', 'integer']
    ];
    function formatCell(row, key, kind) {
      if (kind === 'number') return fmt(row[key]);
      if (kind === 'integer') return fmt(row[key], 0);
      if (kind === 'return') return fmtReturn(row[key]);
      if (kind === 'tie') return fmtTie(row[key]);
      if (kind === 'percentile') return `${fmtPctile(row[key])} ${positionPill(row[key], key.startsWith('pe') ? row.pe_label : row.pb_label)}`;
      return fmtText(row[key]);
    }
    function renderTable(targetId, rows, columns, label) {
      const target = el(targetId);
      const filtered = rows.filter(match);
      if (!filtered.length) {
        target.innerHTML = `<div class="empty">${query ? '没有匹配的行业。' : `${esc(label)}暂无可展示数据。`}</div>`;
        return;
      }
      target.innerHTML = `<div class="table-wrap"><table data-label="${esc(label)}"><thead><tr>${columns.map(([key, title]) => `<th data-sort="${esc(key)}" aria-sort="none">${esc(title)} ↕</th>`).join('')}</tr></thead><tbody>${filtered.map(row => `<tr>${columns.map(([key, _title, kind]) => `<td class="${kind === 'text' ? '' : 'num'}">${formatCell(row, key, kind)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
      const table = target.querySelector('table');
      table.querySelectorAll('th[data-sort]').forEach(th => th.addEventListener('click', () => {
        const key = th.dataset.sort;
        const ascending = th.getAttribute('aria-sort') !== 'ascending';
        const sorted = [...filtered].sort((a, b) => {
          const av = num(a[key]), bv = num(b[key]);
          let result;
          if (av !== null && bv !== null) result = av - bv;
          else if (av !== null) result = -1;
          else if (bv !== null) result = 1;
          else result = String(a[key] ?? '').localeCompare(String(b[key] ?? ''), 'zh-CN');
          return ascending ? result : -result;
        });
        table.querySelectorAll('th').forEach(header => header.setAttribute('aria-sort', 'none'));
        th.setAttribute('aria-sort', ascending ? 'ascending' : 'descending');
        table.querySelector('tbody').innerHTML = sorted.map(row => `<tr>${columns.map(([cellKey, _title, kind]) => `<td class="${kind === 'text' ? '' : 'num'}">${formatCell(row, cellKey, kind)}</td>`).join('')}</tr>`).join('');
      }));
    }

    function renderHeat() {
      const rows = current.filter(match);
      [['pe', 'pe-heat'], ['pb', 'pb-heat']].forEach(([metric, targetId]) => {
        const target = el(targetId);
        if (!rows.length) { target.innerHTML = '<div class="empty">没有匹配的行业。</div>'; return; }
        target.innerHTML = [...rows].sort((a, b) => (num(a[`${metric}_percentile`]) ?? 101) - (num(b[`${metric}_percentile`]) ?? 101)).map(row => {
          const percentile = row[`${metric}_percentile`];
          const count = row[`${metric}_valid_count`];
          const tie = row[`${metric}_tie_ratio`];
          const label = row[`${metric}_label`];
          return `<article class="heat-tile ${positionClass(percentile)}" title="${esc(row.industry_name)}"><div class="name">${esc(row.industry_name)}</div><div class="value">${fmtPctile(percentile)}</div><div class="meta">${esc(label || '不可用')} · N=${num(count) === null ? '—' : Math.trunc(Number(count))}<br>并列 ${num(tie) === null ? '—' : (Number(tie) * 100).toFixed(2) + '%'}</div></article>`;
        }).join('');
      });
    }

    function uniqueIndustries(rows) {
      const found = new Map();
      rows.forEach(row => {
        if (row.index_code && !found.has(row.index_code)) found.set(row.index_code, row.industry_name || row.index_code);
      });
      return [...found.entries()].sort((a, b) => a[1].localeCompare(b[1], 'zh-CN'));
    }
    function fillSelect(selectId, rows) {
      const select = el(selectId);
      const previous = select.value;
      const industries = uniqueIndustries(rows);
      select.innerHTML = industries.map(([code, name]) => `<option value="${esc(code)}">${esc(name)} · ${esc(code)}</option>`).join('');
      if (industries.some(([code]) => code === previous)) select.value = previous;
      return industries;
    }

    function svgNode(svg, tag, attrs = {}, text = '') {
      const node = document.createElementNS(svg.namespaceURI, tag);
      Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
      if (text) node.textContent = text;
      return node;
    }
    function renderLineChart(targetId, statsId, rows, code, metric, archiveMode = false) {
      const target = el(targetId);
      const observations = rows
        .filter(row => row.index_code === code && num(row[metric]) !== null && num(row[metric]) > 0)
        .sort((a, b) => String(a.trade_date).localeCompare(String(b.trade_date)));
      if (!observations.length) {
        target.innerHTML = '<div class="chart-empty">该行业在所选指标下没有有限且大于零的有效观测。</div>';
        el(statsId).innerHTML = '<span>有效样本 <b>0</b></span>';
        return;
      }
      target.innerHTML = '<svg role="img" aria-label="历史序列折线图" viewBox="0 0 960 390" preserveAspectRatio="none"></svg>';
      const svg = target.querySelector('svg');
      const W = 960, H = 390, left = 64, right = 20, top = 24, bottom = 46;
      const values = observations.map(row => Number(row[metric]));
      let min = Math.min(...values), max = Math.max(...values);
      if (min === max) { min *= .98; max *= 1.02; if (min === max) { min -= 1; max += 1; } }
      const x = index => left + index / Math.max(1, observations.length - 1) * (W - left - right);
      const y = value => top + (max - value) / (max - min) * (H - top - bottom);
      for (let i = 0; i <= 4; i++) {
        const gy = top + i / 4 * (H - top - bottom);
        const labelValue = max - i / 4 * (max - min);
        svg.appendChild(svgNode(svg, 'line', {x1:left, y1:gy, x2:W-right, y2:gy, stroke:'#ded8ce', 'stroke-width':1}));
        svg.appendChild(svgNode(svg, 'text', {x:left-10, y:gy+4, 'text-anchor':'end', fill:'#687384', 'font-size':11}, labelValue.toFixed(2)));
      }
      const path = observations.map((row, index) => `${index ? 'L' : 'M'}${x(index).toFixed(2)},${y(Number(row[metric])).toFixed(2)}`).join(' ');
      svg.appendChild(svgNode(svg, 'path', {d:path, fill:'none', stroke:'#0f5f5b', 'stroke-width':2.2, 'vector-effect':'non-scaling-stroke'}));
      const first = observations[0], last = observations[observations.length - 1];
      svg.appendChild(svgNode(svg, 'circle', {cx:x(observations.length-1), cy:y(Number(last[metric])), r:4.5, fill:'#b9483d'}));
      svg.appendChild(svgNode(svg, 'text', {x:left, y:H-17, fill:'#687384', 'font-size':11}, first.trade_date));
      svg.appendChild(svgNode(svg, 'text', {x:W-right, y:H-17, 'text-anchor':'end', fill:'#687384', 'font-size':11}, last.trade_date));
      const title = last.industry_name || last.index_code;
      el(statsId).innerHTML = `<span>行业 <b>${esc(title)}</b></span><span>指标 <b>${metric.toUpperCase()}</b></span><span>有效样本 <b>${observations.length}</b></span><span>首日 <b>${esc(first.trade_date)}</b></span><span>末日 <b>${esc(last.trade_date)}</b></span><span>${archiveMode ? '档案末值' : '当前值'} <b>${Number(last[metric]).toFixed(2)}</b></span>`;
    }

    function renderRanking() {
      const rows = current.filter(match).filter(row => num(row[rankPeriod]) !== null).sort((a, b) => Number(b[rankPeriod]) - Number(a[rankPeriod]));
      const target = el('rank-list');
      if (!rows.length) { target.innerHTML = '<div class="empty">所选区间没有可用收益数据。</div>'; return; }
      const maxAbs = Math.max(...rows.map(row => Math.abs(Number(row[rankPeriod]))), .000001);
      target.innerHTML = rows.map((row, index) => {
        const value = Number(row[rankPeriod]);
        const width = Math.abs(value) / maxAbs * 48;
        const status = row[`${rankPeriod}_status`] || row.return_state || 'UNKNOWN';
        return `<div class="rank-row" title="${esc(status)}"><div class="rank-no">${String(index + 1).padStart(2, '0')}</div><div class="rank-name">${esc(row.industry_name)}</div><div class="rank-track"><span class="rank-zero"></span><span class="rank-bar ${value >= 0 ? 'up' : 'down'}" style="width:${width.toFixed(2)}%"></span></div><div class="rank-value">${fmtReturn(value)}</div></div>`;
      }).join('');
    }

    function renderScatter() {
      const target = el('scatter-chart');
      const tooltip = el('scatter-tooltip');
      const rows = current.filter(match).filter(row => num(row.pe) !== null && num(row.pb) !== null && num(row.pe) > 0 && num(row.pb) > 0);
      if (!rows.length) { target.innerHTML = '<div class="chart-empty">没有同时具备正数 PE 与 PB 的匹配行业。</div>'; return; }
      target.innerHTML = '<div class="scatter-tooltip" id="scatter-tooltip"></div><svg role="img" aria-label="PE-PB 当前值散点图" viewBox="0 0 960 390" preserveAspectRatio="none"></svg>';
      const tip = el('scatter-tooltip');
      const svg = target.querySelector('svg');
      const W = 960, H = 390, left = 64, right = 28, top = 24, bottom = 50;
      const maxPe = Math.max(...rows.map(row => Number(row.pe))) * 1.06;
      const maxPb = Math.max(...rows.map(row => Number(row.pb))) * 1.08;
      const medPe = median(rows.map(row => row.pe));
      const medPb = median(rows.map(row => row.pb));
      const x = value => left + Number(value) / maxPe * (W - left - right);
      const y = value => top + (maxPb - Number(value)) / maxPb * (H - top - bottom);
      for (let i = 0; i <= 4; i++) {
        const gx = left + i / 4 * (W - left - right);
        const gy = top + i / 4 * (H - top - bottom);
        svg.appendChild(svgNode(svg, 'line', {x1:gx, y1:top, x2:gx, y2:H-bottom, stroke:'#e5dfd6'}));
        svg.appendChild(svgNode(svg, 'line', {x1:left, y1:gy, x2:W-right, y2:gy, stroke:'#e5dfd6'}));
        svg.appendChild(svgNode(svg, 'text', {x:gx, y:H-25, 'text-anchor':'middle', fill:'#687384', 'font-size':11}, (maxPe*i/4).toFixed(1)));
        svg.appendChild(svgNode(svg, 'text', {x:left-9, y:gy+4, 'text-anchor':'end', fill:'#687384', 'font-size':11}, (maxPb*(4-i)/4).toFixed(1)));
      }
      svg.appendChild(svgNode(svg, 'line', {x1:x(medPe), y1:top, x2:x(medPe), y2:H-bottom, stroke:'#a26717', 'stroke-dasharray':'5 4', 'stroke-width':1.4}));
      svg.appendChild(svgNode(svg, 'line', {x1:left, y1:y(medPb), x2:W-right, y2:y(medPb), stroke:'#a26717', 'stroke-dasharray':'5 4', 'stroke-width':1.4}));
      svg.appendChild(svgNode(svg, 'text', {x:W-right, y:H-7, 'text-anchor':'end', fill:'#435363', 'font-size':12}, 'PE →'));
      svg.appendChild(svgNode(svg, 'text', {x:left, y:14, fill:'#435363', 'font-size':12}, 'PB ↑'));
      const palette = ['#18816f','#6d8d45','#ad8b2f','#c36a45','#b9483d','#7d8794'];
      rows.forEach(row => {
        const cls = positionClass(row.pe_percentile);
        const paletteIndex = cls === 'pna' ? 5 : Number(cls.slice(1));
        const point = svgNode(svg, 'circle', {cx:x(row.pe), cy:y(row.pb), r:6, fill:palette[paletteIndex], stroke:'#fffdf9', 'stroke-width':1.5, tabindex:0});
        const show = event => {
          tip.style.display = 'block';
          tip.style.left = `${Math.min(event.offsetX + 12, target.clientWidth - 190)}px`;
          tip.style.top = `${Math.max(5, event.offsetY - 65)}px`;
          tip.innerHTML = `<b>${esc(row.industry_name)}</b><br>PE ${fmt(row.pe)} · PB ${fmt(row.pb)}<br>PE 历史百分位 ${fmtPctile(row.pe_percentile)}<br>PB 历史百分位 ${fmtPctile(row.pb_percentile)}`;
        };
        point.addEventListener('mousemove', show);
        point.addEventListener('focus', () => { tip.style.display = 'block'; tip.style.left = `${x(row.pe) / W * target.clientWidth}px`; tip.style.top = `${Math.max(5, y(row.pb) - 65)}px`; tip.innerHTML = `<b>${esc(row.industry_name)}</b><br>PE ${fmt(row.pe)} · PB ${fmt(row.pb)}<br>PE 历史百分位 ${fmtPctile(row.pe_percentile)}<br>PB 历史百分位 ${fmtPctile(row.pb_percentile)}`; });
        point.addEventListener('mouseleave', () => tip.style.display = 'none');
        point.addEventListener('blur', () => tip.style.display = 'none');
        svg.appendChild(point);
      });
    }

    function selectedCode(selectId) { return el(selectId).value || ''; }
    function renderHistory() { renderLineChart('history-chart', 'history-stats', history21, selectedCode('history-industry'), historyMetric); }
    function renderArchiveNameHistory() {
      if (!archiveNameHistory) {
        el('archive-rename-summary').textContent = '轴已阻断；没有合规名称历史可供展示。';
        el('archive-name-table').innerHTML = '<div class="empty">名称历史不可用，详见上方失败原因。</div>';
        return;
      }
      const renamedCodes = Array.isArray(archiveNameHistory.renamed_codes) ? archiveNameHistory.renamed_codes : [];
      const renamedSet = new Set(renamedCodes);
      const segments = Array.isArray(archiveNameHistory.segments) ? archiveNameHistory.segments.filter(row => renamedSet.has(row.index_code)) : [];
      el('archive-rename-summary').innerHTML = renamedCodes.length
        ? `实际发生名称变化的代码（${renamedCodes.length}）：${renamedCodes.map(code => `<span class="code-chip">${esc(code)}</span>`).join('')}`
        : '审计未发现历史名称发生变化的代码。';
      renderTable('archive-name-table', segments, archiveNameColumns, 'SW2014 历史名称分段');
    }
    function renderArchive() {
      renderArchiveNameHistory();
      renderLineChart('archive-chart', 'archive-stats', history14, selectedCode('archive-industry'), archiveMetric, true);
      renderTable('archive-table', archiveSummary, archiveColumns, 'SW2014 档案摘要');
    }
    function renderAll() {
      renderCards();
      renderTable('overview-table', current, overviewColumns, '当前行业快照');
      renderHeat();
      renderRanking();
      renderScatter();
      renderTable('details-table', current, detailColumns, '数据明细');
      renderArchive();
    }

    function activateTab(button) {
      document.querySelectorAll('.tab').forEach(tab => {
        const active = tab === button;
        tab.setAttribute('aria-selected', active ? 'true' : 'false');
        tab.tabIndex = active ? 0 : -1;
      });
      document.querySelectorAll('.panel').forEach(panel => {
        const active = panel.id === button.dataset.tab;
        panel.classList.toggle('active', active);
        panel.hidden = !active;
      });
      if (button.dataset.tab === 'scatter') renderScatter();
      if (button.dataset.tab === 'history') renderHistory();
      if (button.dataset.tab === 'archive') renderArchive();
    }
    const tabs = [...document.querySelectorAll('.tab')];
    tabs.forEach((tab, index) => {
      tab.addEventListener('click', () => activateTab(tab));
      tab.addEventListener('keydown', event => {
        if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
        event.preventDefault();
        let next = index;
        if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
        if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
        if (event.key === 'Home') next = 0;
        if (event.key === 'End') next = tabs.length - 1;
        activateTab(tabs[next]); tabs[next].focus();
      });
    });

    el('industry-search').addEventListener('input', event => { query = event.target.value.trim().toLowerCase(); renderAll(); });
    el('history-industry').addEventListener('change', renderHistory);
    el('archive-industry').addEventListener('change', renderArchive);
    document.querySelectorAll('#history-metric button').forEach(button => button.addEventListener('click', () => {
      historyMetric = button.dataset.metric;
      document.querySelectorAll('#history-metric button').forEach(item => item.classList.toggle('active', item === button));
      renderHistory();
    }));
    document.querySelectorAll('#archive-metric button').forEach(button => button.addEventListener('click', () => {
      archiveMetric = button.dataset.metric;
      document.querySelectorAll('#archive-metric button').forEach(item => item.classList.toggle('active', item === button));
      renderArchive();
    }));
    document.querySelectorAll('#rank-period button').forEach(button => button.addEventListener('click', () => {
      rankPeriod = button.dataset.period;
      document.querySelectorAll('#rank-period button').forEach(item => item.classList.toggle('active', item === button));
      renderRanking();
    }));

    fillMetadata();
    fillSelect('history-industry', history21);
    fillSelect('archive-industry', history14);
    renderHistory();
    renderAll();
  })();
  </script>
</body>
</html>
'''


__all__ = ["render_dashboard"]
