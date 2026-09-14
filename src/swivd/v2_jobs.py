"""Background job state machine shared by the local API and CLI engine."""

from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .io_utils import read_json, sha256_file, write_json
from .v2_pipeline import V2PipelineError, resolve_update_target, run_snapshot
from .v2_storage import (
    SingleWriterLock,
    ensure_layout,
    ledger_terminals_by_job,
    recover_publication,
)


PHASES = (
    "QUEUED", "PREFLIGHT", "CLASSIFICATION", "INDUSTRY_DATA", "MEMBERSHIP",
    "STOCK_VALUATION", "BUILD", "VALIDATE", "PUBLISH",
)
TERMINAL = {"SUCCEEDED", "FAILED", "INTERRUPTED"}
SUCCESS_OUTCOMES = {"NEW_DATA_READY", "ALREADY_UP_TO_DATE"}
RUN_ID_RE = re.compile(r"^SWIVD2-RUN-(\d{8})-\d{3}$")
JOB_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TARGET_REASONS = {
    "CURRENT_OPEN_DAY_AT_OR_AFTER_SW_DAILY_CUTOFF",
    "PREVIOUS_OPEN_DAY_BEFORE_SW_DAILY_CUTOFF",
    "LATEST_OPEN_DAY_NON_TRADING_DATE",
}
# HTTP progress is a typed projection, never a passthrough of job files. New
# diagnostics fall back to the terminal state until explicitly published here.
PUBLIC_DIAGNOSTICS = set("""
AS_OF_NOT_OPEN CLASSIFICATION_AXIS_MISMATCH CLASSIFICATION_CHANGED_REQUIRES_BOOTSTRAP
CLASSIFICATION_COUNT_MISMATCH CLASSIFICATION_LEVELS_MISSING CLASSIFICATION_PARENT_MISSING
CURRENT_INDUSTRY_ROW_MISSING CURRENT_TRADE_CALENDAR_ROW_INVALID DAILY_BASIC_EMPTY
DAILY_BASIC_ROW_LIMIT DATE_BEFORE_SW2021 DUPLICATE_CLASSIFICATION DUPLICATE_DAILY_BASIC
DUPLICATE_INDUSTRY_DAY DUPLICATE_INDUSTRY_HISTORY EMPTY_MEMBERSHIP ENDPOINT_ROW_LIMIT
IDENTITY_CANDIDATE_DUPLICATE IDENTITY_CLASSIFICATION_SINGLETON
IDENTITY_CATALOG_CODE_UNEXPECTED IDENTITY_CATALOG_COLLISION IDENTITY_CATALOG_PATH_MISMATCH
IDENTITY_CATALOG_PUBLICATION_MISMATCH IDENTITY_CATALOG_SINGLETON_REQUIRED
IDENTITY_DUAL_HISTORY_REQUIRED IDENTITY_HISTORY_DATE_OUT_OF_RANGE IDENTITY_HISTORY_DUPLICATE
IDENTITY_HISTORY_GAP IDENTITY_HISTORY_OSCILLATION IDENTITY_HISTORY_OVERLAP IDENTITY_INVALID_DATE
IDENTITY_MEMBER_AS_OF_BOUNDARY_UNKNOWN IDENTITY_MEMBER_AS_OF_MISSING IDENTITY_MEMBER_CODE_MISMATCH
IDENTITY_MEMBER_CURRENT_MISSING IDENTITY_MEMBER_DUAL_AS_OF IDENTITY_MEMBER_DUAL_CODE_REQUIRED
IDENTITY_MEMBER_DUAL_CURRENT IDENTITY_MEMBER_DUPLICATE IDENTITY_MEMBER_INTERVAL_INVALID
IDENTITY_MEMBER_L1_COMPARISON_MISMATCH IDENTITY_MEMBER_L1_ROUNDS_DRIFT IDENTITY_MEMBER_L1_YN_REQUIRED
IDENTITY_MEMBER_PATH_MISMATCH IDENTITY_MEMBER_QUOTE_MISMATCH IDENTITY_MEMBER_ROUNDS_DRIFT
IDENTITY_MEMBER_ROUNDS_REQUIRED IDENTITY_MEMBER_SCHEMA_MISMATCH IDENTITY_MEMBER_STATE_MISMATCH
IDENTITY_MEMBER_YN_REQUIRED IDENTITY_NAME_MISMATCH IDENTITY_OPEN_DATES_EMPTY
IDENTITY_OPEN_DATES_UNSORTED IDENTITY_OPEN_DATE_DUPLICATE IDENTITY_PROJECTION_SCOPE_MISMATCH
IDENTITY_PROJECTION_SOURCE_MISMATCH IDENTITY_PROVENANCE_CONFLICT IDENTITY_QUOTE_CODE_MISMATCH
IDENTITY_QUOTE_DATE_OUTSIDE_EVIDENCE IDENTITY_RESOLUTION_SCOPE_MISMATCH IDENTITY_SCHEMA_MISMATCH
IDENTITY_SECOND_ALIAS IDENTITY_TARGET_DAY_DATE_MISMATCH IDENTITY_TARGET_HISTORY_MISMATCH
IDENTITY_TARGET_HISTORY_ROW_MISMATCH IDENTITY_TARGET_NOT_LATEST_OPEN_DATE
IDENTITY_TARGET_DAY_SINGLETON_REQUIRED INDUSTRY_CODE_MISMATCH INDUSTRY_HISTORY_GAP
INDUSTRY_HISTORY_OUTSIDE_AXIS INJECTED_CLIENT_DATA_DIR_FORBIDDEN INVALID_DATE
INVALID_JOB_KIND INVALID_MEMBER_INTERVAL INVALID_MEMBER_STATE INVALID_PUBLICATION_FLAG
JOB_RESULT_AS_OF_MISMATCH JOB_RESULT_INVALID JOB_RESULT_OUTCOME_INVALID
JOB_RESULT_RUN_ID_MISMATCH LEGACY_ARCHIVE_FILE_MISSING LEGACY_ARCHIVE_IDENTITY_MISMATCH
MEMBERSHIP_EPISODE_CONFLICT MEMBERSHIP_PRIMARY_KEY_DUPLICATE MEMBERSHIP_RAW_ROUNDS_DRIFT
MEMBERSHIP_ROUNDS_DRIFT MEMBER_PATH_MISMATCH MEMBER_PATH_UNKNOWN MEMBER_QUERY_SCOPE_MISMATCH
MEMBER_ROW_LIMIT MEMBER_SPLIT_INCOMPLETE MEMBER_SPLIT_MISSING MEMBER_STATE_DATE_CONFLICT
MISSING_INDUSTRY_HISTORY NAIVE_UPDATE_CLOCK NO_INCREMENT_DATES NO_MEMBERS_AS_OF
OFFLINE_REBUILD_MISMATCH PARENT_IDENTITY_EVIDENCE_MISSING PARENT_INPUT_INVALID
PARENT_PROVIDER_KIND_MISMATCH PREVIOUS_OPEN_DAY_INVALID PROVIDER_KIND_INTERNAL_MISMATCH
PUBLICATION_COMMITTED_RECOVERY_REQUIRED PUBLICATION_RECOVERY_REQUIRED
PUBLICATION_TERMINAL_CONFLICT REBUILD_OUTPUT_EXISTS SCHEMA_MISMATCH SOURCE_CLOSURE_INVALID
SYMLINK_FORBIDDEN TARGET_BEFORE_CURRENT TARGET_NOT_AFTER_PARENT VALUATION_DATE_MISMATCH
WRITER_LOCK_REQUIRED PROCESS_RESTARTED TUSHARECONFIGURATIONERROR TUSHAREPROTOCOLERROR
TUSHARETRANSPORTERROR TUSHAREREDIRECTERROR TUSHAREAPIERROR V2VALIDATIONERROR
VALUEERROR OSERROR PERMISSIONERROR FILENOTFOUNDERROR RUNTIMEERROR
""".split()) | set(PHASES) | TERMINAL | SUCCESS_OUTCOMES


def _public_date(value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
        return None
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return None
    return value


def _public_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00", value
    ):
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return value


def public_job(record: dict[str, Any], *, expected_job_id: str) -> dict[str, Any]:
    """Expose only validated progress fields without consulting credentials."""

    if (
        JOB_ID_RE.fullmatch(expected_job_id) is None
        or record.get("job_id") != expected_job_id
        or record.get("schema_version") != "swivd-job-v2"
        or record.get("kind") not in {"UPDATE_LATEST", "MATERIALIZE_DATE"}
        or record.get("state") not in set(PHASES) | TERMINAL
    ):
        raise KeyError(expected_job_id)
    for name in ("completed_units", "total_units", "percent"):
        if type(record.get(name)) is not int:
            raise KeyError(expected_job_id)
    if (
        not 0 <= record["percent"] <= 100
        or record["total_units"] <= 0
        or not 0 <= record["completed_units"] <= record["total_units"]
    ):
        raise KeyError(expected_job_id)
    state = record["state"]
    message = record.get("safe_message_code")
    code = message if isinstance(message, str) and (
        message in PUBLIC_DIAGNOSTICS or re.fullmatch(r"-?\d{1,5}", message)
    ) else state
    item = record.get("current_item")
    if not isinstance(item, str) or not (
        item == "SELECT_UPDATE_TARGET"
        or _public_date(item)
        or RUN_ID_RE.fullmatch(item)
        or re.fullmatch(r"L[123]|(?:L[123]|IDENTITY):\d{6}\.SI|R[12]:[YN]:\d{6}\.SI", item)
    ):
        item = ""
    run_id = record.get("run_id")
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        run_id = None
    selection = record.get("target_selection")
    target = None
    if isinstance(selection, dict) and (
        _public_date(selection.get("as_of")) is not None
        and selection.get("reason_code") in TARGET_REASONS
        and _public_timestamp(selection.get("evaluated_at")) is not None
        and selection.get("timezone") == "Asia/Shanghai"
        and selection.get("cutoff") == "18:30:00"
    ):
        target = {key: selection[key] for key in (
            "as_of", "reason_code", "evaluated_at", "timezone", "cutoff"
        )}
    return {
        "schema_version": "swivd-job-v2",
        "job_id": expected_job_id,
        "kind": record["kind"],
        "requested_as_of": _public_date(record.get("requested_as_of")),
        "as_of": _public_date(record.get("as_of")),
        "target_selection": target,
        "state": state,
        "completed_units": record["completed_units"],
        "total_units": record["total_units"],
        "percent": record["percent"],
        "current_item": item,
        "safe_message_code": code,
        "run_id": run_id,
        "created_at": _public_timestamp(record.get("created_at")),
        "updated_at": _public_timestamp(record.get("updated_at")),
    }


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


class JobConflict(RuntimeError):
    pass


def _validated_success(manifest: Any, *, expected_as_of: str) -> tuple[str, str]:
    """Validate the pipeline/UI hand-off before publishing a success state."""

    if not isinstance(manifest, dict):
        raise V2PipelineError("JOB_RESULT_INVALID")
    actual_as_of = str(manifest.get("as_of") or "")
    if actual_as_of != expected_as_of:
        raise V2PipelineError("JOB_RESULT_AS_OF_MISMATCH")
    run_id = str(manifest.get("run_id") or "")
    match = RUN_ID_RE.fullmatch(run_id)
    if match is None or match.group(1) != expected_as_of:
        raise V2PipelineError("JOB_RESULT_RUN_ID_MISMATCH")
    outcome = str(manifest.get("job_outcome") or "NEW_DATA_READY")
    if outcome not in SUCCESS_OUTCOMES:
        raise V2PipelineError("JOB_RESULT_OUTCOME_INVALID")
    return run_id, outcome


class JobManager:
    def __init__(self, *, data_dir: Path, spec_path: Path) -> None:
        self.data_dir = data_dir
        self.spec_path = spec_path
        self._mutex = threading.Lock()
        ensure_layout(data_dir)
        lock = SingleWriterLock(data_dir)
        try:
            lock.__enter__()
        except RuntimeError as exc:
            if str(exc) != "CONCURRENT_UPDATE":
                raise
            # A different CLI/server owns the writer. Browsing may start, but
            # only that owner may recover publication or relabel active jobs.
            return
        try:
            recover_publication(data_dir)
            self._recover_interrupted()
        finally:
            lock.__exit__(None, None, None)

    def _path(self, job_id: str) -> Path:
        if not job_id or any(character not in "0123456789abcdef-" for character in job_id):
            raise KeyError(job_id)
        return self.data_dir / "jobs" / f"{job_id}.json"

    def _recover_interrupted(self) -> None:
        terminals = ledger_terminals_by_job(self.data_dir)
        for path in sorted((self.data_dir / "jobs").glob("*.json")):
            try:
                record = read_json(path)
            except Exception:
                continue
            if not isinstance(record, dict) or record.get("state") in TERMINAL:
                continue
            job_id = record.get("job_id")
            terminal = terminals.get(job_id) if isinstance(job_id, str) else None
            if terminal is None:
                record.update(
                    state="INTERRUPTED",
                    safe_message_code="PROCESS_RESTARTED",
                    updated_at=_now(),
                )
            else:
                self._apply_terminal(record, terminal)
            write_json(path, record)

    def _apply_terminal(
        self, record: dict[str, Any], terminal: dict[str, Any]
    ) -> None:
        event = terminal.get("event")
        run_id = terminal.get("run_id")
        as_of = terminal.get("as_of")
        if (
            not isinstance(run_id, str)
            or (match := RUN_ID_RE.fullmatch(run_id)) is None
            or not isinstance(as_of, str)
            or match.group(1) != as_of
            or terminal.get("purpose") != record.get("kind")
            or (
                isinstance(record.get("as_of"), str)
                and bool(record["as_of"])
                and record["as_of"] != as_of
            )
        ):
            raise ValueError("JOB_LEDGER_TERMINAL_INVALID")
        if event == "RUN_SUCCEEDED":
            manifest_path = self.data_dir / "runs" / run_id / "manifest.json"
            manifest_sha = terminal.get("manifest_sha256")
            if (
                not isinstance(manifest_sha, str)
                or not manifest_path.is_file()
                or manifest_path.is_symlink()
                or sha256_file(manifest_path) != manifest_sha
            ):
                raise ValueError("JOB_LEDGER_TERMINAL_INVALID")
            manifest = read_json(manifest_path)
            if (
                not isinstance(manifest, dict)
                or manifest.get("run_id") != run_id
                or manifest.get("as_of") != as_of
                or manifest.get("purpose") != terminal.get("purpose")
            ):
                raise ValueError("JOB_LEDGER_TERMINAL_INVALID")
            record.update(
                state="SUCCEEDED",
                completed_units=1,
                total_units=1,
                percent=100,
                current_item="",
                safe_message_code="NEW_DATA_READY",
                run_id=run_id,
                as_of=as_of,
                updated_at=_now(),
            )
            return
        if event not in {"RUN_FAILED", "RUN_INTERRUPTED"}:
            raise ValueError("JOB_LEDGER_TERMINAL_INVALID")
        error = terminal.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        record.update(
            state="FAILED" if event == "RUN_FAILED" else "INTERRUPTED",
            current_item="",
            safe_message_code=str(code or event)[:80],
            run_id=run_id,
            as_of=as_of,
            updated_at=_now(),
        )

    def get(self, job_id: str) -> dict[str, Any]:
        path = self._path(job_id)
        if not path.is_file() or path.is_symlink():
            raise KeyError(job_id)
        record = read_json(path)
        if not isinstance(record, dict):
            raise KeyError(job_id)
        return record

    def get_public(self, job_id: str) -> dict[str, Any]:
        return public_job(self.get(job_id), expected_job_id=job_id)

    def active(self) -> list[dict[str, Any]]:
        """Read typed job records only; never inspect logs or acquire a writer."""

        records = []
        for path in sorted((self.data_dir / "jobs").glob("*.json")):
            if path.is_symlink() or JOB_ID_RE.fullmatch(path.stem) is None:
                continue
            try:
                record = self.get_public(path.stem)
            except (KeyError, OSError, ValueError, TypeError):
                continue
            if record["state"] not in TERMINAL:
                records.append(record)
        return sorted(records, key=lambda record: (
            record["created_at"] or "", record["job_id"]
        ))

    def create(self, *, kind: str, as_of: str | None = None) -> dict[str, Any]:
        if kind not in {"UPDATE_LATEST", "MATERIALIZE_DATE"}:
            raise ValueError("INVALID_JOB_KIND")
        if kind == "MATERIALIZE_DATE" and (
            as_of is None or len(as_of) != 8 or not as_of.isdigit()
        ):
            raise ValueError("INVALID_AS_OF")
        lock = SingleWriterLock(self.data_dir)
        with self._mutex:
            try:
                lock.__enter__()
            except RuntimeError as exc:
                raise JobConflict("CONCURRENT_UPDATE") from exc
            try:
                recover_publication(self.data_dir)
                self._recover_interrupted()
                job_id = str(uuid.uuid4())
                record: dict[str, Any] = {
                    "schema_version": "swivd-job-v2",
                    "job_id": job_id,
                    "kind": kind,
                    "requested_as_of": as_of,
                    "as_of": as_of,
                    "target_selection": None,
                    "state": "QUEUED",
                    "completed_units": 0,
                    "total_units": 1,
                    "percent": 0,
                    "current_item": "",
                    "safe_message_code": "QUEUED",
                    "run_id": None,
                    "created_at": _now(),
                    "updated_at": _now(),
                }
                write_json(self._path(job_id), record)
                thread = threading.Thread(
                    target=self._run,
                    args=(job_id, lock),
                    name=f"swivd-job-{job_id[:8]}",
                    daemon=True,
                )
                thread.start()
                return record
            except BaseException:
                lock.__exit__(None, None, None)
                raise

    def _write_progress(
        self, job_id: str, phase: str, completed: int, total: int, item: str
    ) -> None:
        if phase not in PHASES[1:]:
            return
        if isinstance(completed, bool) or isinstance(total, bool):
            return
        try:
            completed_value = int(completed)
            total_value = int(total)
        except (TypeError, ValueError, OverflowError):
            return
        if (
            completed_value != completed
            or total_value != total
            or total_value <= 0
            or completed_value < 0
            or completed_value > total_value
        ):
            return
        with self._mutex:
            record = self.get(job_id)
            current_state = str(record.get("state", ""))
            if current_state in TERMINAL:
                return
            try:
                current_phase_index = PHASES.index(current_state)
            except ValueError:
                return
            phase_index = PHASES.index(phase)
            if phase_index < current_phase_index:
                return
            if phase_index == current_phase_index:
                try:
                    previous_completed = int(record.get("completed_units", 0))
                    previous_total = int(record.get("total_units", 1))
                except (TypeError, ValueError, OverflowError):
                    return
                if (
                    completed_value < previous_completed
                    or total_value < previous_total
                ):
                    return
            # Units are phase-local.  Entering a later phase may start at 0/1;
            # within one phase, stale callbacks can never move either counter
            # backwards.  Overall percent remains global and monotonic.
            fraction = completed_value / total_value
            calculated = min(99, int(((phase_index + fraction) / len(PHASES)) * 100))
            record.update(
                state=phase,
                completed_units=completed_value,
                total_units=total_value,
                percent=max(int(record.get("percent", 0)), calculated),
                current_item=str(item)[:160],
                safe_message_code=phase,
                updated_at=_now(),
            )
            write_json(self._path(job_id), record)

    def _run(self, job_id: str, lock: SingleWriterLock) -> None:
        try:
            record = self.get(job_id)
            as_of = record.get("requested_as_of")
            if record["kind"] == "UPDATE_LATEST":
                self._write_progress(job_id, "PREFLIGHT", 0, 1, "SELECT_UPDATE_TARGET")
                target = resolve_update_target()
                as_of = target.as_of
                record = self.get(job_id)
                record["as_of"] = as_of
                record["target_selection"] = target.as_dict()
                write_json(self._path(job_id), record)
            manifest = run_snapshot(
                spec_path=self.spec_path,
                data_dir=self.data_dir,
                as_of=str(as_of),
                purpose=record["kind"],
                progress=lambda phase, completed, total, item: self._write_progress(
                    job_id, phase, completed, total, item
                ),
                job_id=job_id,
                _writer_lock=lock,
            )
            final = self.get(job_id)
            run_id, outcome = _validated_success(manifest, expected_as_of=str(as_of))
            final.update(
                state="SUCCEEDED",
                completed_units=1,
                total_units=1,
                percent=100,
                current_item="",
                safe_message_code=outcome,
                run_id=run_id,
                updated_at=_now(),
            )
            write_json(self._path(job_id), final)
        except BaseException as exc:
            try:
                recover_publication(self.data_dir)
                terminal = ledger_terminals_by_job(self.data_dir).get(job_id)
            except Exception:
                final = self.get(job_id)
                final.update(
                    current_item="",
                    safe_message_code="PUBLICATION_RECOVERY_REQUIRED",
                    updated_at=_now(),
                )
                write_json(self._path(job_id), final)
            else:
                final = self.get(job_id)
                if terminal is not None:
                    self._apply_terminal(final, terminal)
                else:
                    state = (
                        "INTERRUPTED"
                        if isinstance(exc, (KeyboardInterrupt, SystemExit))
                        else "FAILED"
                    )
                    final.update(
                        state=state,
                        current_item="",
                        safe_message_code=str(
                            getattr(exc, "code", type(exc).__name__.upper())
                        )[:80],
                        updated_at=_now(),
                    )
                write_json(self._path(job_id), final)
        finally:
            lock.__exit__(None, None, None)


__all__ = ["JobConflict", "JobManager", "PHASES", "TERMINAL", "public_job"]
