"""Application use cases: one data writer, durable checkpoints, shared batches.

The provider retrieves evidence, the domain computes, the store publishes, and
this module coordinates them. No HTTP or browser dependency belongs here.
"""
from __future__ import annotations

import contextlib
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import gzip
import io
import json
from pathlib import Path
import re
import tarfile
import threading
import uuid
from zoneinfo import ZoneInfo

from .metrics import compute_day
from .models import DataError, json_bytes, parse_date
from .periods import open_dates
from .query import compile_views
from .storage import atomic_write, source_identity, utc_now
from .validation import audit_day
from .runtime import environment_identity, require_supported_environment
from .history_state import HistoryState
from .day_integrity import validate_day_refs
from .taxonomy import DataError as ProviderDataError

SHANGHAI = ZoneInfo("Asia/Shanghai")
TRANSIENT_CODES = frozenset({"TushareTransportError", "BASE_TABLE_EMPTY", "MONEYFLOW_COVERAGE_GAP", "MARKET_COVERAGE_GAP", "DAILY_COVERAGE_GAP"})
ERROR_MESSAGES = {
    "CLEAN_COMMITTED_SOURCE_REQUIRED": "当前源码还有未提交的改动。完成审阅与本地提交后才能更新正式数据。",
    "SOURCE_CHANGED_DURING_JOB": "取数期间源码发生变化；本批次未发布，旧数据仍可读取。",
    "CONCURRENT_UPDATE": "已有更新或回补任务正在执行。",
    "NOTHING_TO_BACKFILL": "所选范围没有待回补的交易日。",
    "PUBLICATION_RECOVERY_REQUIRED": "发布事务需要恢复；请重新启动服务。",
    "TushareConfigurationError": "Tushare 凭据或接口配置不可用，请运行 doctor 检查。",
    "TushareApiError": "Tushare 接口未成功返回，请核对接口权限与请求额度。",
    "TushareTransportError": "Tushare 网络连接失败，当前批次保持不变。",
    "DEPENDENCY_LOCK_MISMATCH": "运行依赖与项目锁文件不一致，请按手册重新安装锁定依赖。",
    "DEPENDENCY_LOCK_INVALID": "依赖锁文件缺失或格式不正确，正式更新已停止。",
    "PLATFORM_NOT_SUPPORTED": "本版正式更新仅支持 macOS。",
    "PYTHON_NOT_SUPPORTED": "本版需要 CPython 3.11–3.14。",
    "MONEYFLOW_COVERAGE_GAP": "个股资金记录不完整，本批次未发布。",
    "HISTORY_INCOMPLETE": "可尝试日期已处理，仍有历史日期未通过验证；合格日期已保存，缺口不会自动重复请求。",
    "HISTORY_STATE_WRITE_FAILED": "历史失败记录无法保存，回补已停止；已提交日期保留，请恢复存储后重试。",
    "HISTORY_STATE_INVALID": "历史失败记录损坏，回补已停止；请核验记录与原始证据。",
}


def safe_error(exc: Exception) -> dict:
    code = getattr(exc, "code", None) or type(exc).__name__
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,96}", code):
        code = "UNEXPECTED_ERROR"
    return {"code": code, "message": ERROR_MESSAGES.get(code, f"数据检查未通过（{code}）；当前批次保持不变。")}


def five_year_start(day: str) -> str:
    value = parse_date(day)
    try:
        return value.replace(year=value.year - 5).strftime("%Y%m%d")
    except ValueError:  # February 29 -> February 28 in a non-leap start year.
        return value.replace(year=value.year - 5, day=28).strftime("%Y%m%d")


def candidate_day(calendar: list, now: datetime) -> str:
    if now.tzinfo is None:
        raise DataError("CLOCK_REQUIRES_AWARE_DATETIME")
    local = now.astimezone(SHANGHAI)
    today = local.strftime("%Y%m%d")
    eligible = [d for d in open_dates(calendar) if d < today or d == today and (local.hour, local.minute) >= (19, 30)]
    if not eligible:
        raise DataError("NO_CANDIDATE_TRADE_DAY")
    return eligible[-1]


class Pipeline:
    def __init__(self, store, source_root: Path, *, provider_factory=None, clock=None):
        self.store = store
        self.source_root = Path(source_root).resolve()
        self.provider_factory = provider_factory
        self.clock = clock or (lambda: datetime.now(SHANGHAI))

    def source_check(self) -> dict:
        source = source_identity(self.source_root)
        if not self.store.development and (source["git_dirty"] or not source["commit"]):
            raise DataError("CLEAN_COMMITTED_SOURCE_REQUIRED")
        source["runtime"] = environment_identity(self.source_root) if self.store.development else require_supported_environment(self.source_root)
        return source

    def _provider(self, progress,put_raw=None):
        put_raw=put_raw or self.store.put_raw
        if self.provider_factory is not None:
            return self.provider_factory(put_raw=put_raw, progress=progress, clock=self.clock)
        from .provider import TushareProvider
        # Existing local token readers can print diagnostics. Nothing from them
        # is allowed into job logs, HTTP replies, or the console.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return TushareProvider(put_raw=put_raw, progress=progress, clock=self.clock)

    def _freeze_source(self, source: dict) -> dict:
        content = io.BytesIO()
        with tarfile.open(fileobj=content, mode="w") as archive:
            for relative, expected in source["files"].items():
                raw = (self.source_root / relative).read_bytes()
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise DataError("SOURCE_CHANGED_DURING_JOB")
                info = tarfile.TarInfo(relative)
                info.size = len(raw)
                info.mode = 0o444
                info.mtime = 0
                archive.addfile(info, io.BytesIO(raw))
        return self.store.put_bytes(gzip.compress(content.getvalue(),mtime=0), "source_snapshot_tar_gz")

    def _checkpoint(self, day: str, value: dict):
        atomic_write(self.store.path(f"checkpoints/{day}.json"), json_bytes(value))

    def _load_checkpoint(self, day: str, source: dict):
        path = self.store.path(f"checkpoints/{day}.json")
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_bytes())
        except (ValueError, OSError):
            raise DataError("CHECKPOINT_INVALID") from None
        if value.get("trade_date") != day:
            raise DataError("CHECKPOINT_DATE_MISMATCH")
        if not isinstance(value.get("source_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", value["source_sha256"]):
            raise DataError("CHECKPOINT_SOURCE_INVALID")
        self.store.verify_refs(value)
        inputs, result = validate_day_refs(self.store, day, value.get("day_refs"),
            expected_result_source_sha256=value.get("source_sha256"))
        if value.get("source_sha256") != source["tree_sha256"]:
            return None
        audit_day(inputs, result)
        return value["day_refs"]

    def history_status(self, current=None) -> dict:
        """Read operational gaps without changing the fixed published batch."""
        current = current if current is not None else self.store.current()
        coverage = (current or {}).get("views", {}).get("coverage", {})
        return HistoryState(self.store, source_identity(self.source_root), verify_evidence=False,
                            published_failures=coverage.get("blocked_dates", [])).summary(coverage.get("missing_dates", []))

    def _publish_days(self, *, days, calendar, history_start, source, frozen_source,
                      calendar_source_refs, provider, history, progress):
        if self.source_check()["tree_sha256"] != source["tree_sha256"]:
            raise DataError("SOURCE_CHANGED_DURING_JOB")
        as_of = max(days)
        if history_start > as_of:
            raise DataError("HISTORY_RANGE_INVERTED")
        views = compile_views(self.store, days, calendar, progress,
                              expected_result_source_sha256=source["tree_sha256"])
        expected = [d for d in open_dates(calendar) if history_start <= d <= as_of]
        missing = [d for d in expected if d not in days]
        views["coverage"].update(start=history_start, first_available=min(days), end=as_of,
                                 expected_days=len(expected), missing_days=len(missing), missing_dates=missing,
                                 completed_days=sum(d in days for d in expected), **history.summary(missing))
        gaps = bool(missing or views["coverage"]["days_with_metric_gaps"])
        payload = {"as_of": as_of, "history_start": history_start, "source": source,
                   "source_snapshot": frozen_source, "contract_version": "industry-workbench-v1",
                   "provider_kind": getattr(provider, "provider_kind", "TEST_INJECTED_CLIENT"),
                   "days": days, "views": views, "calendar_source_refs": calendar_source_refs,
                   "publication_state": "PUBLISHED_WITH_GAPS" if gaps else "PUBLISHED",
                   "permissions": {"research_only": True, "trading": False, "external_publish": False}}
        progress("VERIFY", 0, 1, "校验数据引用闭包")
        self.store.verify_refs(payload)
        if self.source_check()["tree_sha256"] != source["tree_sha256"]:
            raise DataError("SOURCE_CHANGED_DURING_JOB")
        return self.store.publish(payload)

    def run(self, kind: str, params: dict, *, progress=None, should_yield=None) -> dict:
        progress = progress or (lambda *args: None)
        params = deepcopy(params)
        retry_failed = params.get("retry_failed", False)
        if not isinstance(retry_failed, bool) or retry_failed and kind != "backfill":
            raise DataError("INVALID_HISTORY_RETRY")
        retry_id = params.get("retry_id") or ("RETRY-" + uuid.uuid4().hex if retry_failed else None)
        prior_attempted = params.get("_attempted_dates", [])
        if not isinstance(prior_attempted, list):
            raise DataError("INVALID_HISTORY_ATTEMPTS")
        for day in prior_attempted:
            parse_date(day)
        source = self.source_check()
        with self.store.writer():
            current = self.store.current()
            rebuild_needed=bool(current and current["source"]["tree_sha256"]!=source["tree_sha256"])
            frozen_source = self._freeze_source(source)
            capture_source = {"identity":{k:source[k] for k in ("commit","git_dirty","tree_sha256")},
                              "snapshot":frozen_source,"inventory":self.store.put_json(source,"source_identity")}
            request_refs=[]
            def record_raw(metadata,raw_bytes):
                reference=self.store.put_raw(metadata,raw_bytes)
                request_refs.append(reference)
                return reference
            provider = self._provider(progress,put_raw=record_raw)
            now = self.clock().astimezone(SHANGHAI)
            today = now.strftime("%Y%m%d")
            default_start = five_year_start(today)
            start = params.get("start_date") or (current or {}).get("history_start") or default_start
            end = params.get("end_date") or today
            parse_date(start); parse_date(end)
            if start > end or end > today:
                raise DataError("INVALID_BACKFILL_RANGE")
            # Include the complete first natural month/week, and return anchors.
            calendar_start = (parse_date(min(start, (current or {}).get("history_start", start))) - timedelta(days=400)).strftime("%Y%m%d")
            calendar = provider.calendar(calendar_start, today)
            calendar_source_refs=list(request_refs)
            target = candidate_day(calendar, now)
            history = HistoryState(self.store, source,
                published_failures=(current or {}).get("views", {}).get("coverage", {}).get("blocked_dates", []))
            if kind == "update":
                if current and current["as_of"] == target and not rebuild_needed:
                    return {"batch_id": current["batch_id"], "as_of": target, "already_current": True,
                            "history": current["views"]["coverage"],"calendar_source_refs":calendar_source_refs}
                todo = [] if current and current["as_of"]==target else [target]
                history_start = (current or {}).get("history_start") or five_year_start(target)
            elif kind == "backfill":
                history_start = min(start, (current or {}).get("history_start", start))
                if not current:
                    history_start = min(history_start, target)
                end = min(end, target)
                todo = [d for d in open_dates(calendar) if start <= d <= end and d not in (current or {}).get("days", {})]
                # Always make the newest day usable before historical work.
                if not current and target not in todo:
                    todo.insert(0, target)
                elif not current and target in todo:
                    todo.remove(target); todo.insert(0, target)
            else:
                raise DataError("INVALID_DATA_JOB_KIND")
            max_days = params.get("max_days", 3 if kind == "backfill" else 1)
            if isinstance(max_days, bool) or not isinstance(max_days, int) or not 1 <= max_days <= 40:
                raise DataError("INVALID_CHUNK_SIZE")
            selected = [d for d in todo if d == target or (d not in prior_attempted and not history.is_blocked(
                d, retry_failed=retry_failed, retry_id=retry_id))][:max_days]
            if not selected and not rebuild_needed:
                summary = history.summary(todo, retry_failed=retry_failed, retry_id=retry_id, attempted_dates=prior_attempted)
                return {"batch_id": current["batch_id"] if current else None, "already_complete": not todo,
                        "captured_days": [], "attempted_days": [], "scan_complete": not summary["remaining_attemptable_days"],
                        "calendar_source_refs": calendar_source_refs, **summary}
            days = deepcopy((current or {}).get("days", {}))
            if current and current["source"]["tree_sha256"] != source["tree_sha256"]:
                # An old result is never relabeled as if it used the new code.
                # Recompute from the frozen normalized inputs and preserve their
                # capture source. New query views are always rebuilt below.
                verified_sources = set()
                for old_day, old_refs in sorted(days.items()):
                    inputs, _ = validate_day_refs(self.store, old_day, old_refs,
                        expected_result_source_sha256=current["source"]["tree_sha256"], verified_sources=verified_sources)
                    old_refs.setdefault("input_source", {"identity":{k:current["source"][k] for k in ("commit","git_dirty","tree_sha256")},"snapshot":current.get("source_snapshot"),"inventory":self.store.put_json(current["source"],"source_identity")})
                    rebuilt=compute_day(inputs)
                    rebuilt["audit"]["independent_recalculation"]=audit_day(inputs,rebuilt)
                    old_refs["result"] = self.store.put_json(rebuilt, "day_result")
                    old_refs["result_source_sha256"] = source["tree_sha256"]
            captured, attempted, published_batches = [], [], []
            def outcome():
                remaining = [d for d in todo if d not in days]
                summary = history.summary(remaining, retry_failed=retry_failed, retry_id=retry_id,
                                          attempted_dates=prior_attempted + attempted)
                return {"batch_id": current["batch_id"] if current else None,
                        "as_of": current["as_of"] if current else None,
                        "captured_days": list(captured), "attempted_days": list(attempted),
                        "published_batches": list(published_batches), "retry_id": retry_id,
                        "scan_complete": not summary["remaining_attemptable_days"], **summary,
                        "history": (current or {}).get("views", {}).get("coverage"),
                        "publication_state": (current or {}).get("publication_state"),
                        "calendar_source_refs": calendar_source_refs}
            try:
                for index, day in enumerate(selected, 1):
                    if attempted and should_yield and should_yield():
                        break  # Failed attempts also yield to the latest update.
                    progress("DAY", index - 1, len(selected), day)
                    attempted.append(day)
                    checkpoint = self._load_checkpoint(day, source)
                    if checkpoint:
                        refs = checkpoint
                    else:
                        try:
                            inputs = provider.fetch_day(day)
                        except (DataError, ProviderDataError) as exc:
                            # This code has one audited producer: audit_stock_day.
                            # All other exceptions remain global fail-closed.
                            if not (kind == "backfill" and day < target and exc.code == "INDEPENDENT_UNIVERSE_INCOMPLETE"):
                                raise
                            if self.source_check()["tree_sha256"] != source["tree_sha256"]:
                                raise DataError("SOURCE_CHANGED_DURING_JOB") from None
                            local_refs = getattr(provider, "_refs", {})
                            failed_refs = list(local_refs.values()) if isinstance(local_refs, dict) else []
                            failed_refs = failed_refs or list(request_refs)
                            requests = [ref.get("request", {}) for ref in failed_refs]
                            target_apis = {r.get("api_name") for r in requests if r.get("params", {}).get("trade_date") == day}
                            states = {r.get("params", {}).get("list_status") for r in requests if r.get("api_name") == "stock_basic"}
                            if not {"daily", "daily_basic", "moneyflow", "suspend_d"} <= target_apis or states != {"L", "D", "P"}:
                                raise DataError("HISTORY_FAILURE_EVIDENCE_MISSING") from None
                            history.block(day, code=exc.code, capture_source=capture_source,
                                          source_refs=failed_refs, retry_id=retry_id, job_id=params.get("job_id"), target_date=target)
                            continue
                        result = compute_day(inputs)
                        result["audit"]["independent_recalculation"] = audit_day(inputs, result)
                        refs = {"input": self.store.put_json(inputs, "day_input"),
                                "result": self.store.put_json(result, "day_result"),
                                "input_contract_version": "industry-workbench-day-input-v1",
                                "input_source": capture_source, "result_source_sha256": source["tree_sha256"]}
                        self._checkpoint(day, {"trade_date": day, "source_sha256": source["tree_sha256"], "day_refs": refs})
                    next_days = {**days, day: refs}
                    current = self._publish_days(days=next_days, calendar=calendar, history_start=history_start,
                        source=source, frozen_source=frozen_source, calendar_source_refs=calendar_source_refs,
                        provider=provider, history=history, progress=progress)
                    days = next_days
                    captured.append(day)
                    published_batches.append(current["batch_id"])
                    rebuild_needed = False
                    history.resolve(day, capture_source=refs.get("input_source", capture_source),
                                    source_refs=[refs["input"]], retry_id=retry_id, job_id=params.get("job_id"),
                                    batch_id=current["batch_id"])
                if rebuild_needed:
                    current = self._publish_days(days=days, calendar=calendar, history_start=history_start,
                        source=source, frozen_source=frozen_source, calendar_source_refs=calendar_source_refs,
                        provider=provider, history=history, progress=progress)
                    published_batches.append(current["batch_id"])
                return outcome()
            except Exception as exc:
                exc.pipeline_result = outcome()
                raise


class JobManager:
    """One writer with day-boundary preemption and durable, stable job IDs.

    A backfill without ``max_days`` is a whole-range request. Its pipeline
    invocations remain bounded, and it returns to the queue after each chunk.
    Explicit ``max_days`` retains the CLI/scheduler's one-chunk contract.
    """

    _PRIORITY = {"update": 0, "backfill": 1}
    _INTERRUPTED = {"code": "PROCESS_INTERRUPTED", "message": "任务因服务退出中断；已完成的每日检查点可在重试时复用。"}
    _PERSISTENCE_ERROR = {"code": "JOB_PERSISTENCE_FAILED", "message": "任务记录无法保存；已完成的每日检查点保留，请恢复存储后重试。"}

    def __init__(self, pipeline: Pipeline):
        self.pipeline = pipeline
        self.store = pipeline.store
        self._lock = threading.RLock()
        self._jobs = {}
        self._queue = []
        self._done = {}
        self._finished = {}
        self._active = None
        self._thread = None
        self._stop = threading.Event()

    def _save(self, job):
        atomic_write(self.store.path(f"jobs/{job['job_id']}.json"), json_bytes(job))

    def _persist(self, job):
        try:
            self._save(job)
        except Exception:
            raise DataError("JOB_PERSISTENCE_FAILED") from None

    def _next_id(self):
        return min(self._queue, key=lambda key: self._PRIORITY[self._jobs[key]["kind"]]) if self._queue else None

    def active(self):
        with self._lock:
            running = self._jobs.get(self._active)
            if running and running["status"] == "RUNNING":
                return deepcopy(running)
            queued = self._jobs.get(self._next_id())
            return deepcopy(queued or (running if running and running["status"] == "QUEUED" else None))

    def pending(self, kind=None):
        """Snapshot of running/queued work; never starts or recovers a job."""
        with self._lock:
            ids = dict.fromkeys(([self._active] if self._active else []) + sorted(
                self._queue, key=lambda key: self._PRIORITY[self._jobs[key]["kind"]]))
            return [deepcopy(self._jobs[key]) for key in ids
                    if self._jobs[key]["status"] in {"QUEUED", "RUNNING"}
                    and (kind is None or self._jobs[key]["kind"] == kind)]

    def latest_finished(self, kind):
        """Last completion in this process, including manually requested work."""
        with self._lock:
            return deepcopy(self._jobs.get(self._finished.get(kind)))

    def get(self, job_id):
        if not re.fullmatch(r"JOB-[a-f0-9]{20}", job_id):
            raise DataError("INVALID_JOB_ID")
        with self._lock:
            if job_id in self._jobs:
                return deepcopy(self._jobs[job_id])
        try:
            job = json.loads(self.store.path(f"jobs/{job_id}.json").read_bytes())
        except (OSError, ValueError):
            raise DataError("JOB_NOT_FOUND") from None
        if job.get("job_id") != job_id:
            raise DataError("JOB_IDENTITY_MISMATCH")
        if job.get("status") in {"RUNNING", "QUEUED"}:
            job.update(status="FAILED", phase="INTERRUPTED", error=deepcopy(self._INTERRUPTED))
        return job

    def submit(self, kind: str, params: dict | None = None, *, asynchronous=True) -> dict:
        params = deepcopy(params or {})
        if kind not in {"update", "backfill"}:
            raise DataError("INVALID_JOB_KIND")
        if not isinstance(params.get("retry_failed", False), bool) or params.get("retry_failed") and kind != "backfill":
            raise DataError("INVALID_HISTORY_RETRY")
        if kind == "backfill":
            # Internal identity survives every chunk; clients cannot select it.
            params.pop("retry_id", None)
            params.pop("job_id", None)
            params.pop("_attempted_dates", None)
            if params.get("retry_failed"):
                params["retry_id"] = "RETRY-" + uuid.uuid4().hex
        self.pipeline.source_check()
        with self._lock:
            if self._stop.is_set():
                raise DataError("SERVICE_STOPPING")
            # Manual and scheduled requests share a single outstanding update.
            existing = self.pending("update") if kind == "update" else []
            if existing:
                job_id = existing[0]["job_id"]
            else:
                job_id = "JOB-" + uuid.uuid4().hex[:20]
                if kind == "backfill":
                    params["job_id"] = job_id
                job = {"job_id": job_id, "kind": kind, "status": "QUEUED", "phase": "QUEUED",
                       "completed_units": 0, "total_units": 0, "message": "等待执行", "result": None,
                       "error": None, "created_at": utc_now(), "updated_at": utc_now(), "params": params}
                self._jobs[job_id] = job
                self._done[job_id] = threading.Event()
                try:
                    self._persist(job)
                except Exception as exc:
                    self._finish(job, error=safe_error(exc))
                else:
                    self._queue.append(job_id)
                    if self._thread is None:
                        self._thread = threading.Thread(target=self._work, name="workbench-jobs", daemon=True)
                        try:
                            self._thread.start()
                        except Exception as exc:
                            self._thread = None
                            self._queue.remove(job_id)
                            self._finish(job, error=safe_error(exc))
            snapshot = deepcopy(self._jobs[job_id])
            done = self._done[job_id]
        if asynchronous:
            return snapshot
        done.wait()
        return self.get(job_id)

    def _progress(self, job_id, phase, completed, total, message):
        with self._lock:
            job = self._jobs[job_id]
            job.update(phase=phase, message=message, updated_at=utc_now())
            if job["kind"] == "backfill":
                # FETCH/COMPILE count requests/rows, not completed trade days.
                if phase == "DAY":
                    prior_result = job.get("result") or {}
                    prior = len(prior_result.get("attempted_days", prior_result.get("captured_days", [])))
                    job.update(completed_units=prior + completed,
                               total_units=max(job["total_units"], prior + total))
            else:
                job.update(completed_units=completed, total_units=total)
            self._persist(job)

    def _finish(self, job, *, error=None):
        """Called under the lock. Completion notification survives failed IO."""
        result = job.get("result") or {}
        error = deepcopy(error)
        bounded = job["kind"] == "backfill" and result.get("remaining_days", 0) and not error
        phase = "INTERRUPTED" if error and error["code"] == "PROCESS_INTERRUPTED" else "COMPLETE_WITH_GAPS" if error and error["code"] == "HISTORY_INCOMPLETE" else "FAILED" if error else "CHUNK_COMPLETE" if bounded else "COMPLETE"
        message = error["message"] if error else f"本块完成；所选范围仍有 {result['remaining_days']} 个交易日未入库" if bounded else "已完成"
        if error and error["code"] == "HISTORY_INCOMPLETE":
            pending = result.get("remaining_attemptable_days", 0)
            phase = "CHUNK_COMPLETE_WITH_GAPS" if pending else "COMPLETE_WITH_GAPS"
            message = f"本块已处理；还有 {pending} 个日期待尝试，{len(result.get('blocked_dates', []))} 个日期未通过验证" if pending else f"本次可尝试日期已处理；仍有 {len(result.get('blocked_dates', []))} 个历史日期未通过验证"
            error["message"] = message
        elif error and job["kind"] == "backfill" and result.get("captured_days"):
            message = f"本任务已保存 {len(result['captured_days'])} 个合格交易日；回补随后停止（{error['code']}），请查看剩余日期。"
            error["message"] = message
        job.update(status="FAILED" if error else "SUCCEEDED", phase=phase, message=message,
                   error=deepcopy(error), updated_at=utc_now())
        if error and error["code"] == "JOB_PERSISTENCE_FAILED":
            job.update(message=self._PERSISTENCE_ERROR["message"], error=deepcopy(self._PERSISTENCE_ERROR))
        try:
            self._save(job)
        except Exception:
            job["persistence_error"] = deepcopy(self._PERSISTENCE_ERROR)
            if not error:
                job.update(status="FAILED", phase="FAILED", message=self._PERSISTENCE_ERROR["message"],
                           error=deepcopy(self._PERSISTENCE_ERROR))
            # Best effort to persist the safe failure if the failure was brief.
            # Never leave a completion event or the worker slot hostage to IO.
            try:
                self._save(job)
            except Exception:
                pass
        finally:
            self._finished[job["kind"]] = job["job_id"]
            self._done[job["job_id"]].set()

    def _work(self):
        while True:
            with self._lock:
                job_id = self._next_id()
                if job_id is None:
                    self._thread = None
                    return
                self._queue.remove(job_id)
                self._active = job_id
            try:
                self._run(job_id)
            except Exception as exc:
                # Keep later queued jobs recoverable even after an unexpected
                # bookkeeping failure outside the pipeline's exception path.
                with self._lock:
                    self._finish(self._jobs[job_id], error=safe_error(exc))
            finally:
                with self._lock:
                    self._active = None

    def _run(self, job_id):
        job = self._jobs[job_id]
        try:
            with self._lock:
                if self._stop.is_set():
                    self._finish(job, error=self._INTERRUPTED)
                    return
                job.update(status="RUNNING", updated_at=utc_now())
                self._persist(job)
            def yield_for_daily():
                with self._lock:
                    return self._stop.is_set() or job["kind"] == "backfill" and any(
                        self._jobs[key]["kind"] == "update" for key in self._queue)
            run_params = deepcopy(job["params"])
            if job["kind"] == "backfill":
                previous = job.get("result") or {}
                run_params["_attempted_dates"] = previous.get("attempted_days", previous.get("captured_days", []))
            result = self.pipeline.run(job["kind"], run_params, progress=lambda *args: self._progress(job_id, *args), should_yield=yield_for_daily)
            with self._lock:
                if job["kind"] == "backfill":
                    previous = (job.get("result") or {}).get("captured_days", [])
                    chunk_days = result.get("captured_days", [])
                    captured = list(dict.fromkeys(previous + chunk_days))
                    remaining = result.get("remaining_days", 0)
                    attemptable = result.get("remaining_attemptable_days", remaining)
                    old_attempted = (job.get("result") or {}).get("attempted_days", previous)
                    attempted = list(dict.fromkeys(old_attempted + result.get("attempted_days", chunk_days)))
                    result.update(captured_days=captured, chunk_captured_days=chunk_days,
                                  attempted_days=attempted,
                                  published_batches=list(dict.fromkeys((job.get("result") or {}).get("published_batches", []) + result.get("published_batches", []))),
                                  chunks_completed=(job.get("result") or {}).get("chunks_completed", 0) + 1)
                    job.update(completed_units=len(attempted), total_units=len(attempted) + attemptable)
                    job["result"] = result
                    if attemptable and self._stop.is_set():
                        self._finish(job, error=self._INTERRUPTED)
                        return
                    if attemptable and "max_days" not in job["params"]:
                        if len(attempted) == len(old_attempted):
                            raise DataError("BACKFILL_NO_PROGRESS")
                        job.update(status="QUEUED", phase="QUEUED", message="本批已保存，等待继续回补", updated_at=utc_now())
                        self._persist(job)
                        self._queue.append(job_id)
                        return
                    if result.get("blocked_dates"):
                        self._finish(job, error=safe_error(DataError("HISTORY_INCOMPLETE")))
                        return
                job["result"] = result
                self._finish(job)
        except Exception as exc:
            with self._lock:
                partial = getattr(exc, "pipeline_result", None)
                if isinstance(partial, dict):
                    previous = job.get("result") or {}
                    for field in ("captured_days", "attempted_days", "published_batches"):
                        partial[field] = list(dict.fromkeys(previous.get(field, []) + partial.get(field, [])))
                    job["result"] = partial
                    job.update(completed_units=len(partial.get("attempted_days", [])),
                               total_units=len(partial.get("attempted_days", [])) + partial.get("remaining_attemptable_days", 0))
                self._finish(job, error=safe_error(exc))

    def stop(self, timeout=2):
        with self._lock:
            self._stop.set()
            for job_id in self._queue:
                self._finish(self._jobs[job_id], error=self._INTERRUPTED)
            self._queue.clear()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)
