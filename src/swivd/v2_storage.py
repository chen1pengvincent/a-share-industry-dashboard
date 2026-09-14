"""Portable data roots, immutable run allocation and single-writer locking."""

from __future__ import annotations

import base64
import json
import os
import platform
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .io_utils import (
    append_ndjson,
    atomic_write_bytes,
    atomic_write_text,
    durable_unlink,
    fsync_directory,
    read_json,
    sha256_bytes,
    sha256_file,
    write_json,
)


RUN_RE = re.compile(r"SWIVD2-RUN-(\d{8})-(\d{3})")
SHA_RE = re.compile(r"[0-9a-f]{64}")
POINTER_SCOPE = "SW2021_L1_L2_L3_WITH_POINT_IN_TIME_MEMBERS"
POINTER_FIELDS = {
    "pointer_kind",
    "scope",
    "run_id",
    "as_of",
    "target_path",
    "target_sha256",
}
HISTORICAL_INDEX_V1 = "swivd-historical-index-v1"
HISTORICAL_INDEX_V2 = "swivd-historical-index-v2"
PUBLICATION_SCHEMA = "swivd-publication-transaction-v1"
PUBLICATION_STATES = {"PREPARED", "COMMITTED"}
PUBLICATION_INTENT = Path("transactions") / "publication.json"
TERMINAL_EVENTS = {"RUN_SUCCEEDED", "RUN_FAILED", "RUN_INTERRUPTED"}


@dataclass(frozen=True)
class PublicationRecovery:
    outcome: str
    run_id: str
    purpose: str
    as_of: str
    job_id: str | None
    terminal_event: str


def default_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "swivd"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "swivd"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "swivd"


def resolve_data_dir(value: str | Path | None) -> Path:
    target = Path(value).expanduser() if value is not None else default_data_dir()
    return target.resolve()


def ensure_layout(data_dir: Path) -> None:
    root_existed = data_dir.exists()
    data_dir.mkdir(parents=True, exist_ok=True)
    if not root_existed:
        fsync_directory(data_dir.parent)
    for relative in ("runs", "jobs", "locks", "transactions"):
        target = data_dir / relative
        existed = target.exists()
        target.mkdir(parents=False, exist_ok=True)
        if not existed:
            fsync_directory(data_dir)


def allocate_run(data_dir: Path, as_of: str) -> tuple[str, Path]:
    ensure_layout(data_dir)
    if len(as_of) != 8 or not as_of.isdigit():
        raise ValueError("as_of must be YYYYMMDD")
    for sequence in range(1, 1000):
        run_id = f"SWIVD2-RUN-{as_of}-{sequence:03d}"
        run_dir = data_dir / "runs" / run_id
        try:
            run_dir.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            continue
        fsync_directory(run_dir.parent)
        return run_id, run_dir
    raise RuntimeError("RUN_ID_EXHAUSTED")


def inventory_artifacts(run_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(run_dir).as_posix()
        if relative in {"manifest.json", "SHA256SUMS"}:
            continue
        records.append(
            {"path": relative, "bytes": path.stat().st_size, "sha256": sha256_file(path)}
        )
    return records


def write_sums(run_dir: Path) -> None:
    lines = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.name == "SHA256SUMS":
            continue
        lines.append(f"{sha256_file(path)}  {path.relative_to(run_dir).as_posix()}")
    atomic_write_text(run_dir / "SHA256SUMS", "\n".join(lines) + "\n")


def append_ledger(data_dir: Path, record: Mapping[str, Any]) -> None:
    append_ndjson(data_dir / "run_ledger.ndjson", record)


def _pointer_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _write_pointer(path: Path, payload: Mapping[str, Any]) -> None:
    atomic_write_bytes(path, _pointer_bytes(payload))


def _manifest_pointer_record(
    *, run_id: str, as_of: str, manifest_sha256: str
) -> dict[str, Any]:
    return {
        "pointer_kind": "manifest",
        "scope": POINTER_SCOPE,
        "run_id": run_id,
        "as_of": as_of,
        "target_path": f"runs/{run_id}/manifest.json",
        "target_sha256": manifest_sha256,
    }


def publish_current(data_dir: Path, *, run_id: str, as_of: str, manifest_sha256: str) -> None:
    record = _manifest_pointer_record(
        run_id=run_id,
        as_of=as_of,
        manifest_sha256=manifest_sha256,
    )
    _resolve_record(
        data_dir,
        record,
        expected_as_of=as_of,
        expected_purpose="UPDATE_LATEST",
    )
    _write_pointer(data_dir / "latest_run.json", record)


def _historical_payload(
    data_dir: Path, *, run_id: str, as_of: str, manifest_sha256: str
) -> dict[str, Any]:
    target = data_dir / "historical_index.json"
    dates: dict[str, dict[str, Any]] = {}
    if target.is_file():
        existing = read_json(target)
        if not isinstance(existing, Mapping):
            raise ValueError("HISTORICAL_INDEX_SCHEMA_MISMATCH")
        schema = existing.get("schema_version")
        existing_dates = existing.get("dates")
        if not isinstance(existing_dates, Mapping):
            raise ValueError("HISTORICAL_INDEX_SCHEMA_MISMATCH")
        if schema == HISTORICAL_INDEX_V2:
            if set(existing) != {"schema_version", "pointer_kind", "scope", "dates"}:
                raise ValueError("HISTORICAL_INDEX_SCHEMA_MISMATCH")
            if (
                existing.get("pointer_kind") != "historical_manifest_index"
                or existing.get("scope") != POINTER_SCOPE
            ):
                raise ValueError("HISTORICAL_INDEX_IDENTITY_MISMATCH")
            for date, value in existing_dates.items():
                if not isinstance(value, Mapping):
                    raise ValueError("HISTORICAL_INDEX_SCHEMA_MISMATCH")
                record = dict(value)
                _resolve_record(
                    data_dir,
                    record,
                    expected_as_of=str(date),
                    expected_purpose="MATERIALIZE_DATE",
                )
                dates[str(date)] = record
        elif schema == HISTORICAL_INDEX_V1:
            if set(existing) != {"schema_version", "dates"}:
                raise ValueError("HISTORICAL_INDEX_SCHEMA_MISMATCH")
            for date, value in existing_dates.items():
                if not isinstance(value, Mapping) or set(value) != {
                    "run_id",
                    "target_path",
                    "target_sha256",
                }:
                    raise ValueError("HISTORICAL_INDEX_SCHEMA_MISMATCH")
                record = {
                    "pointer_kind": "manifest",
                    "scope": POINTER_SCOPE,
                    "run_id": value.get("run_id"),
                    "as_of": str(date),
                    "target_path": value.get("target_path"),
                    "target_sha256": value.get("target_sha256"),
                }
                _resolve_record(
                    data_dir,
                    record,
                    expected_as_of=str(date),
                    expected_purpose="MATERIALIZE_DATE",
                )
                dates[str(date)] = record
        else:
            raise ValueError("HISTORICAL_INDEX_SCHEMA_MISMATCH")
    record = _manifest_pointer_record(
        run_id=run_id,
        as_of=as_of,
        manifest_sha256=manifest_sha256,
    )
    _resolve_record(
        data_dir,
        record,
        expected_as_of=as_of,
        expected_purpose="MATERIALIZE_DATE",
    )
    dates[as_of] = record
    return {
        "schema_version": HISTORICAL_INDEX_V2,
        "pointer_kind": "historical_manifest_index",
        "scope": POINTER_SCOPE,
        "dates": {key: dates[key] for key in sorted(dates)},
    }


def publish_historical(data_dir: Path, *, run_id: str, as_of: str, manifest_sha256: str) -> None:
    target = data_dir / "historical_index.json"
    payload = _historical_payload(
        data_dir,
        run_id=run_id,
        as_of=as_of,
        manifest_sha256=manifest_sha256,
    )
    _write_pointer(target, payload)


def _resolve_record(
    data_dir: Path,
    record: Mapping[str, Any],
    *,
    expected_as_of: str | None,
    expected_purpose: str,
) -> Path:
    if set(record) != POINTER_FIELDS:
        raise ValueError("POINTER_SCHEMA_MISMATCH")
    if record.get("pointer_kind") != "manifest" or record.get("scope") != POINTER_SCOPE:
        raise ValueError("POINTER_IDENTITY_MISMATCH")
    run_id = record.get("run_id")
    as_of = record.get("as_of")
    if not isinstance(run_id, str) or (match := RUN_RE.fullmatch(run_id)) is None:
        raise ValueError("POINTER_RUN_ID_MISMATCH")
    if not isinstance(as_of, str) or len(as_of) != 8 or not as_of.isdigit():
        raise ValueError("POINTER_AS_OF_MISMATCH")
    if match.group(1) != as_of or (expected_as_of is not None and as_of != expected_as_of):
        raise ValueError("POINTER_AS_OF_MISMATCH")
    expected_path = f"runs/{run_id}/manifest.json"
    if record.get("target_path") != expected_path:
        raise ValueError("POINTER_TARGET_PATH_MISMATCH")
    target_sha = record.get("target_sha256")
    if not isinstance(target_sha, str) or SHA_RE.fullmatch(target_sha) is None:
        raise ValueError("POINTER_HASH_FORMAT_MISMATCH")
    manifest = (data_dir / expected_path).resolve()
    if data_dir.resolve() not in manifest.parents or not manifest.is_file() or manifest.is_symlink():
        raise ValueError("POINTER_TARGET_INVALID")
    if sha256_file(manifest) != target_sha:
        raise ValueError("POINTER_HASH_MISMATCH")
    target_manifest = read_json(manifest)
    if not isinstance(target_manifest, Mapping):
        raise ValueError("POINTER_MANIFEST_SCHEMA_MISMATCH")
    if target_manifest.get("schema_version") not in {
        "swivd-local-snapshot-manifest-v2",
        "swivd-local-snapshot-manifest-v3",
        "swivd-local-snapshot-manifest-v4",
    }:
        raise ValueError("POINTER_MANIFEST_SCHEMA_MISMATCH")
    if target_manifest.get("run_id") != run_id:
        raise ValueError("POINTER_MANIFEST_RUN_ID_MISMATCH")
    if target_manifest.get("as_of") != as_of:
        raise ValueError("POINTER_MANIFEST_AS_OF_MISMATCH")
    if target_manifest.get("purpose") != expected_purpose:
        raise ValueError("POINTER_MANIFEST_PURPOSE_MISMATCH")
    return manifest


_PUBLICATION_FIELDS = {
    "schema_version",
    "transaction_id",
    "state",
    "run_id",
    "purpose",
    "as_of",
    "provider_kind",
    "job_id",
    "manifest_sha256",
    "pointer_path",
    "previous_pointer_existed",
    "previous_pointer_base64",
    "previous_pointer_sha256",
    "new_pointer_base64",
    "new_pointer_sha256",
    "success_record",
    "prepared_at",
    "committed_at",
}


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def publication_intent_path(data_dir: Path) -> Path:
    return data_dir / PUBLICATION_INTENT


def _decode_bytes(value: Any, label: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"{label}_INVALID")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError(f"{label}_INVALID") from exc


def _load_publication(data_dir: Path) -> dict[str, Any] | None:
    path = publication_intent_path(data_dir)
    if path.is_symlink():
        raise ValueError("PUBLICATION_INTENT_INVALID")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("PUBLICATION_INTENT_INVALID")
    value = read_json(path)
    if not isinstance(value, Mapping) or set(value) != _PUBLICATION_FIELDS:
        raise ValueError("PUBLICATION_INTENT_SCHEMA_MISMATCH")
    intent = dict(value)
    if intent.get("schema_version") != PUBLICATION_SCHEMA:
        raise ValueError("PUBLICATION_INTENT_SCHEMA_MISMATCH")
    if intent.get("state") not in PUBLICATION_STATES:
        raise ValueError("PUBLICATION_INTENT_STATE_INVALID")
    run_id = intent.get("run_id")
    as_of = intent.get("as_of")
    if (
        not isinstance(run_id, str)
        or (match := RUN_RE.fullmatch(run_id)) is None
        or not isinstance(as_of, str)
        or match.group(1) != as_of
        or intent.get("transaction_id") != run_id
    ):
        raise ValueError("PUBLICATION_INTENT_IDENTITY_MISMATCH")
    purpose = intent.get("purpose")
    expected_pointer = (
        "latest_run.json" if purpose == "UPDATE_LATEST" else "historical_index.json"
    )
    if purpose not in {"UPDATE_LATEST", "MATERIALIZE_DATE"} or intent.get(
        "pointer_path"
    ) != expected_pointer:
        raise ValueError("PUBLICATION_INTENT_PURPOSE_MISMATCH")
    if not isinstance(intent.get("provider_kind"), str) or not intent["provider_kind"]:
        raise ValueError("PUBLICATION_INTENT_PROVIDER_MISMATCH")
    if intent.get("job_id") is not None and (
        not isinstance(intent.get("job_id"), str) or not intent["job_id"]
    ):
        raise ValueError("PUBLICATION_INTENT_JOB_MISMATCH")
    manifest_sha = intent.get("manifest_sha256")
    if not isinstance(manifest_sha, str) or SHA_RE.fullmatch(manifest_sha) is None:
        raise ValueError("PUBLICATION_INTENT_HASH_MISMATCH")
    previous = _decode_bytes(intent.get("previous_pointer_base64"), "PREVIOUS_POINTER")
    new = _decode_bytes(intent.get("new_pointer_base64"), "NEW_POINTER")
    if not isinstance(intent.get("previous_pointer_existed"), bool):
        raise ValueError("PUBLICATION_INTENT_PREVIOUS_POINTER_MISMATCH")
    expected_previous_sha = sha256_bytes(previous) if intent["previous_pointer_existed"] else None
    if (
        (not intent["previous_pointer_existed"] and previous)
        or intent.get("previous_pointer_sha256") != expected_previous_sha
        or intent.get("new_pointer_sha256") != sha256_bytes(new)
    ):
        raise ValueError("PUBLICATION_INTENT_HASH_MISMATCH")
    success = intent.get("success_record")
    if not isinstance(success, Mapping):
        raise ValueError("PUBLICATION_INTENT_SUCCESS_MISMATCH")
    if success.get("event") != "RUN_SUCCEEDED":
        raise ValueError("PUBLICATION_INTENT_SUCCESS_MISMATCH")
    for field in ("run_id", "purpose", "as_of", "provider_kind", "job_id", "manifest_sha256"):
        if success.get(field) != intent.get(field):
            raise ValueError("PUBLICATION_INTENT_SUCCESS_MISMATCH")
    if intent["state"] == "PREPARED":
        if intent.get("committed_at") is not None or success.get("recorded_at") is not None:
            raise ValueError("PUBLICATION_INTENT_STATE_INVALID")
    elif (
        not isinstance(intent.get("committed_at"), str)
        or success.get("recorded_at") != intent.get("committed_at")
    ):
        raise ValueError("PUBLICATION_INTENT_STATE_INVALID")
    return intent


def _pointer_target(data_dir: Path, intent: Mapping[str, Any]) -> Path:
    return data_dir / str(intent["pointer_path"])


def _verify_new_pointer(data_dir: Path, intent: Mapping[str, Any]) -> None:
    target = _pointer_target(data_dir, intent)
    if not target.is_file() or target.is_symlink():
        raise ValueError("PUBLICATION_POINTER_NOT_DURABLE")
    payload = target.read_bytes()
    if sha256_bytes(payload) != intent.get("new_pointer_sha256"):
        raise ValueError("PUBLICATION_POINTER_MISMATCH")


def prepare_publication(
    data_dir: Path,
    *,
    run_id: str,
    as_of: str,
    purpose: str,
    manifest_sha256: str,
    provider_kind: str,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Durably write a PREPARED publication intent before pointer mutation."""

    ensure_layout(data_dir)
    intent_path = publication_intent_path(data_dir)
    if intent_path.exists() or intent_path.is_symlink():
        raise ValueError("PUBLICATION_RECOVERY_REQUIRED")
    if purpose == "UPDATE_LATEST":
        pointer_path = "latest_run.json"
        payload: Mapping[str, Any] = _manifest_pointer_record(
            run_id=run_id,
            as_of=as_of,
            manifest_sha256=manifest_sha256,
        )
        _resolve_record(
            data_dir,
            payload,
            expected_as_of=as_of,
            expected_purpose=purpose,
        )
    elif purpose == "MATERIALIZE_DATE":
        pointer_path = "historical_index.json"
        payload = _historical_payload(
            data_dir,
            run_id=run_id,
            as_of=as_of,
            manifest_sha256=manifest_sha256,
        )
    else:
        raise ValueError("PUBLICATION_PURPOSE_INVALID")
    target = data_dir / pointer_path
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError("PUBLICATION_POINTER_INVALID")
    previous = target.read_bytes() if target.is_file() else b""
    new = _pointer_bytes(payload)
    prepared_at = _now()
    intent = {
        "schema_version": PUBLICATION_SCHEMA,
        "transaction_id": run_id,
        "state": "PREPARED",
        "run_id": run_id,
        "purpose": purpose,
        "as_of": as_of,
        "provider_kind": provider_kind,
        "job_id": job_id,
        "manifest_sha256": manifest_sha256,
        "pointer_path": pointer_path,
        "previous_pointer_existed": target.is_file(),
        "previous_pointer_base64": base64.b64encode(previous).decode("ascii"),
        "previous_pointer_sha256": sha256_bytes(previous) if target.is_file() else None,
        "new_pointer_base64": base64.b64encode(new).decode("ascii"),
        "new_pointer_sha256": sha256_bytes(new),
        "success_record": {
            "event": "RUN_SUCCEEDED",
            "run_id": run_id,
            "purpose": purpose,
            "as_of": as_of,
            "provider_kind": provider_kind,
            "job_id": job_id,
            "manifest_sha256": manifest_sha256,
            "recorded_at": None,
        },
        "prepared_at": prepared_at,
        "committed_at": None,
    }
    write_json(intent_path, intent)
    return intent


def apply_publication_pointer(data_dir: Path, *, transaction_id: str) -> None:
    intent = _load_publication(data_dir)
    if intent is None or intent.get("transaction_id") != transaction_id:
        raise ValueError("PUBLICATION_TRANSACTION_MISSING")
    if intent.get("state") != "PREPARED":
        raise ValueError("PUBLICATION_ALREADY_COMMITTED")
    payload = _decode_bytes(intent["new_pointer_base64"], "NEW_POINTER")
    atomic_write_bytes(_pointer_target(data_dir, intent), payload)
    _verify_new_pointer(data_dir, intent)


def commit_publication(data_dir: Path, *, transaction_id: str) -> None:
    """Persist COMMITTED; successful return is the sole publication commit point."""

    intent = _load_publication(data_dir)
    if intent is None or intent.get("transaction_id") != transaction_id:
        raise ValueError("PUBLICATION_TRANSACTION_MISSING")
    if intent.get("state") != "PREPARED":
        raise ValueError("PUBLICATION_ALREADY_COMMITTED")
    _verify_new_pointer(data_dir, intent)
    committed_at = _now()
    intent["state"] = "COMMITTED"
    intent["committed_at"] = committed_at
    success = dict(intent["success_record"])
    success["recorded_at"] = committed_at
    intent["success_record"] = success
    write_json(publication_intent_path(data_dir), intent)


def publication_transaction_state(
    data_dir: Path, *, transaction_id: str
) -> str | None:
    intent = _load_publication(data_dir)
    if intent is None:
        return None
    if intent.get("transaction_id") != transaction_id:
        raise ValueError("PUBLICATION_TRANSACTION_ID_MISMATCH")
    return str(intent["state"])


def _ledger_records(data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / "run_ledger.ndjson"
    if path.is_symlink():
        raise ValueError("LEDGER_INVALID")
    if not path.exists():
        return []
    if not path.is_file():
        raise ValueError("LEDGER_INVALID")
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise ValueError("LEDGER_INVALID")
            records.append(dict(value))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("LEDGER_INVALID") from exc
    return records


def _terminal_records(data_dir: Path, run_id: str) -> list[dict[str, Any]]:
    return [
        record
        for record in _ledger_records(data_dir)
        if record.get("run_id") == run_id and record.get("event") in TERMINAL_EVENTS
    ]


def _terminal_matches(
    record: Mapping[str, Any], intent: Mapping[str, Any], event: str
) -> bool:
    if record.get("event") != event:
        return False
    for field in ("run_id", "purpose", "as_of", "provider_kind", "job_id"):
        if record.get(field) != intent.get(field):
            return False
    if event == "RUN_SUCCEEDED" and record.get("manifest_sha256") != intent.get(
        "manifest_sha256"
    ):
        return False
    return isinstance(record.get("recorded_at"), str) and bool(record["recorded_at"])


def _append_terminal_once(
    data_dir: Path,
    intent: Mapping[str, Any],
    record: Mapping[str, Any],
) -> dict[str, Any]:
    run_id = str(intent["run_id"])
    existing = _terminal_records(data_dir, run_id)
    if existing:
        if len(existing) != 1 or not _terminal_matches(
            existing[0], intent, str(record.get("event"))
        ):
            raise ValueError("PUBLICATION_TERMINAL_CONFLICT")
        return existing[0]
    try:
        append_ledger(data_dir, record)
    except BaseException:
        # The append may have reached durable storage before its caller raised.
        # Re-read before deciding whether it is safe to retry or surface error.
        observed = _terminal_records(data_dir, run_id)
        if len(observed) == 1 and _terminal_matches(
            observed[0], intent, str(record.get("event"))
        ):
            return observed[0]
        raise
    observed = _terminal_records(data_dir, run_id)
    if len(observed) != 1 or not _terminal_matches(
        observed[0], intent, str(record.get("event"))
    ):
        raise ValueError("PUBLICATION_TERMINAL_NOT_DURABLE")
    return observed[0]


def complete_publication(
    data_dir: Path, *, transaction_id: str
) -> PublicationRecovery:
    intent = _load_publication(data_dir)
    if intent is None or intent.get("transaction_id") != transaction_id:
        raise ValueError("PUBLICATION_TRANSACTION_MISSING")
    if intent.get("state") != "COMMITTED":
        raise ValueError("PUBLICATION_NOT_COMMITTED")
    _verify_new_pointer(data_dir, intent)
    success = dict(intent["success_record"])
    _append_terminal_once(data_dir, intent, success)
    durable_unlink(publication_intent_path(data_dir))
    return PublicationRecovery(
        outcome="COMMITTED_SUCCESS",
        run_id=str(intent["run_id"]),
        purpose=str(intent["purpose"]),
        as_of=str(intent["as_of"]),
        job_id=intent.get("job_id"),
        terminal_event="RUN_SUCCEEDED",
    )


def _restore_previous_pointer(data_dir: Path, intent: Mapping[str, Any]) -> None:
    target = _pointer_target(data_dir, intent)
    if intent.get("previous_pointer_existed"):
        previous = _decode_bytes(intent["previous_pointer_base64"], "PREVIOUS_POINTER")
        atomic_write_bytes(target, previous)
        if sha256_bytes(target.read_bytes()) != intent.get("previous_pointer_sha256"):
            raise ValueError("PUBLICATION_ROLLBACK_MISMATCH")
    else:
        durable_unlink(target, missing_ok=True)
        if target.exists() or target.is_symlink():
            raise ValueError("PUBLICATION_ROLLBACK_MISMATCH")


def abort_publication(
    data_dir: Path,
    *,
    transaction_id: str,
    terminal_record: Mapping[str, Any],
) -> PublicationRecovery:
    intent = _load_publication(data_dir)
    if intent is None or intent.get("transaction_id") != transaction_id:
        raise ValueError("PUBLICATION_TRANSACTION_MISSING")
    if intent.get("state") != "PREPARED":
        raise ValueError("PUBLICATION_ALREADY_COMMITTED")
    event = terminal_record.get("event")
    if event not in {"RUN_FAILED", "RUN_INTERRUPTED"}:
        raise ValueError("PUBLICATION_ABORT_EVENT_INVALID")
    record = dict(terminal_record)
    record.update(
        run_id=intent["run_id"],
        purpose=intent["purpose"],
        as_of=intent["as_of"],
        provider_kind=intent["provider_kind"],
        job_id=intent["job_id"],
    )
    if not isinstance(record.get("recorded_at"), str) or not record["recorded_at"]:
        record["recorded_at"] = _now()
    _restore_previous_pointer(data_dir, intent)
    _append_terminal_once(data_dir, intent, record)
    durable_unlink(publication_intent_path(data_dir))
    return PublicationRecovery(
        outcome="ROLLED_BACK_TERMINAL",
        run_id=str(intent["run_id"]),
        purpose=str(intent["purpose"]),
        as_of=str(intent["as_of"]),
        job_id=intent.get("job_id"),
        terminal_event=str(event),
    )


def recover_publication(data_dir: Path) -> PublicationRecovery | None:
    """Recover the one possible publication transaction under writer lock."""

    intent = _load_publication(data_dir)
    if intent is None:
        return None
    if intent["state"] == "COMMITTED":
        return complete_publication(
            data_dir, transaction_id=str(intent["transaction_id"])
        )
    _restore_previous_pointer(data_dir, intent)
    existing = _terminal_records(data_dir, str(intent["run_id"]))
    if existing:
        if len(existing) != 1 or existing[0].get("event") not in {
            "RUN_FAILED",
            "RUN_INTERRUPTED",
        } or not _terminal_matches(
            existing[0], intent, str(existing[0].get("event"))
        ):
            raise ValueError("PUBLICATION_TERMINAL_CONFLICT")
        terminal = existing[0]
    else:
        terminal = {
            "event": "RUN_INTERRUPTED",
            "run_id": intent["run_id"],
            "purpose": intent["purpose"],
            "as_of": intent["as_of"],
            "provider_kind": intent["provider_kind"],
            "job_id": intent["job_id"],
            "error": {
                "code": "PUBLICATION_RECOVERED_BEFORE_COMMIT",
                "type": "PROCESS_INTERRUPTED",
            },
            "recorded_at": _now(),
        }
        _append_terminal_once(data_dir, intent, terminal)
    durable_unlink(publication_intent_path(data_dir))
    return PublicationRecovery(
        outcome=(
            "ROLLED_BACK_INTERRUPTED"
            if terminal["event"] == "RUN_INTERRUPTED"
            else "ROLLED_BACK_TERMINAL"
        ),
        run_id=str(intent["run_id"]),
        purpose=str(intent["purpose"]),
        as_of=str(intent["as_of"]),
        job_id=intent.get("job_id"),
        terminal_event=str(terminal["event"]),
    )


def ledger_terminals_by_job(data_dir: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in _ledger_records(data_dir):
        if record.get("event") not in TERMINAL_EVENTS:
            continue
        job_id = record.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            continue
        if job_id in result:
            raise ValueError("JOB_LEDGER_TERMINAL_CONFLICT")
        result[job_id] = record
    return result


def ledger_terminal_for_run(
    data_dir: Path, run_id: str
) -> dict[str, Any] | None:
    """Return the unique durable terminal for one run, failing on ambiguity."""

    records = _terminal_records(data_dir, run_id)
    if not records:
        return None
    if len(records) != 1:
        raise ValueError("PUBLICATION_TERMINAL_CONFLICT")
    return records[0]


def resolve_pointer(data_dir: Path, *, historical_date: str | None = None) -> tuple[Path, dict[str, Any]] | None:
    intent = publication_intent_path(data_dir)
    if intent.exists() or intent.is_symlink():
        raise ValueError("PUBLICATION_RECOVERY_REQUIRED")
    if historical_date is None:
        path = data_dir / "latest_run.json"
        if not path.is_file():
            return None
        pointer = read_json(path)
        if not isinstance(pointer, Mapping):
            raise ValueError("POINTER_SCHEMA_MISMATCH")
        record = dict(pointer)
        manifest = _resolve_record(
            data_dir,
            record,
            expected_as_of=None,
            expected_purpose="UPDATE_LATEST",
        )
    else:
        if len(historical_date) != 8 or not historical_date.isdigit():
            raise ValueError("POINTER_AS_OF_MISMATCH")
        path = data_dir / "historical_index.json"
        if not path.is_file():
            return None
        index = read_json(path)
        if not isinstance(index, Mapping):
            raise ValueError("HISTORICAL_INDEX_SCHEMA_MISMATCH")
        dates = index.get("dates")
        if not isinstance(dates, Mapping):
            raise ValueError("HISTORICAL_INDEX_SCHEMA_MISMATCH")
        if historical_date not in dates:
            return None
        schema = index.get("schema_version")
        value = dates[historical_date]
        if not isinstance(value, Mapping):
            raise ValueError("HISTORICAL_INDEX_SCHEMA_MISMATCH")
        if schema == HISTORICAL_INDEX_V2:
            if set(index) != {"schema_version", "pointer_kind", "scope", "dates"}:
                raise ValueError("HISTORICAL_INDEX_SCHEMA_MISMATCH")
            if (
                index.get("pointer_kind") != "historical_manifest_index"
                or index.get("scope") != POINTER_SCOPE
            ):
                raise ValueError("HISTORICAL_INDEX_IDENTITY_MISMATCH")
            record = dict(value)
        elif schema == HISTORICAL_INDEX_V1:
            if set(index) != {"schema_version", "dates"} or set(value) != {
                "run_id",
                "target_path",
                "target_sha256",
            }:
                raise ValueError("HISTORICAL_INDEX_SCHEMA_MISMATCH")
            record = {
                "pointer_kind": "manifest",
                "scope": POINTER_SCOPE,
                "run_id": value.get("run_id"),
                "as_of": historical_date,
                "target_path": value.get("target_path"),
                "target_sha256": value.get("target_sha256"),
            }
        else:
            raise ValueError("HISTORICAL_INDEX_SCHEMA_MISMATCH")
        manifest = _resolve_record(
            data_dir,
            record,
            expected_as_of=historical_date,
            expected_purpose="MATERIALIZE_DATE",
        )
    return manifest.parent, record


class SingleWriterLock:
    """OS-released non-blocking file lock for CLI/server coordination."""

    def __init__(self, data_dir: Path) -> None:
        ensure_layout(data_dir)
        self.path = (data_dir / "locks" / "writer.lock").resolve()
        self.handle: Any | None = None

    def held_for(self, data_dir: Path) -> bool:
        """Prove this live lock instance protects exactly ``data_dir``."""

        return (
            self.handle is not None
            and not self.handle.closed
            and self.path == (data_dir / "locks" / "writer.lock").resolve()
        )

    def __enter__(self) -> "SingleWriterLock":
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        if self.handle.read(1) == b"":
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("CONCURRENT_UPDATE") from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def runtime_record() -> dict[str, Any]:
    from .runtime_environment import safe_runtime_record

    return safe_runtime_record()


__all__ = [
    "PublicationRecovery",
    "RUN_RE",
    "SingleWriterLock",
    "abort_publication",
    "allocate_run",
    "apply_publication_pointer",
    "append_ledger",
    "commit_publication",
    "complete_publication",
    "default_data_dir",
    "ensure_layout",
    "inventory_artifacts",
    "ledger_terminal_for_run",
    "ledger_terminals_by_job",
    "prepare_publication",
    "publication_intent_path",
    "publication_transaction_state",
    "publish_current",
    "publish_historical",
    "recover_publication",
    "resolve_data_dir",
    "resolve_pointer",
    "runtime_record",
    "write_sums",
]
