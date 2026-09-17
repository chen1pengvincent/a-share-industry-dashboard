"""Application-lifetime scheduler using the same queue as manual jobs."""
from __future__ import annotations

from datetime import datetime
import json
import threading

from .jobs import SHANGHAI, TRANSIENT_CODES, safe_error
from .models import json_bytes
from .storage import atomic_write


class Scheduler:
    def __init__(self, jobs, *, enabled=True, clock=None):
        self.jobs = jobs
        self.store = jobs.store
        self.clock = clock or (lambda: datetime.now(SHANGHAI))
        self.enabled = enabled and not self.store.development
        self.state = {"date": None, "attempts": [], "pending_update": None, "pending_history": None,
                      "last_error": None, "history_error": None, "startup_checked": False,
                      "observed_update": None, "observed_history": None, "history_gaps": None}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        path = self.store.path("scheduler/state.json")
        if path.exists():
            try:
                stored = json.loads(path.read_bytes())
                if not isinstance(stored, dict) or not isinstance(stored.get("attempts", []), list):
                    raise ValueError("invalid scheduler state")
                self.state.update(stored)
            except (OSError, ValueError):
                self.enabled = False
                self.state["last_error"] = {"code": "SCHEDULER_STATE_INVALID", "message": "调度记录损坏，请检查后恢复。"}
        # Restart checks missed work but does not reset already consumed slots.
        self.state["startup_checked"] = False

    def status(self):
        with self._lock:
            return {"enabled": self.enabled, "times": ["19:30", "19:45", "20:15"],
                    "timezone": "Asia/Shanghai", "last_error": self.state["last_error"],
                    "history_error": self.state["history_error"],
                    "history_gaps": self.state["history_gaps"],
                    "pending_update": self.state["pending_update"],
                    "pending_history": self.state["pending_history"]}

    def _save(self):
        try:
            atomic_write(self.store.path("scheduler/state.json"), json_bytes(self.state))
        except Exception:
            # Without durable attempt slots a loop/restart could repeatedly
            # request data. Pause this scheduler while manual jobs stay usable.
            self.enabled = False
            self.state["last_error"] = {"code": "SCHEDULER_STATE_WRITE_FAILED", "message": "调度记录无法保存，自动调度已暂停；请恢复存储并重启服务。"}
            raise

    def _observe_jobs(self):
        changed = False
        def observe(kind, error_key, job):
            error = job.get("error")
            if kind == "backfill" and error and error.get("code") == "HISTORY_INCOMPLETE":
                self.state[error_key] = None
                self.state["history_gaps"] = job.get("result")
            else:
                self.state[error_key] = error
        legacy = self.state.pop("pending_job", None)
        if legacy:
            job = self.jobs.get(legacy)
            self.state["pending_history" if job["kind"] == "backfill" else "pending_update"] = legacy
            changed = True
        for kind, pending_key, observed_key, error_key in (
            ("update", "pending_update", "observed_update", "last_error"),
            ("backfill", "pending_history", "observed_history", "history_error"),
        ):
            pending = self.state[pending_key]
            if pending:
                try:
                    job = self.jobs.get(pending)
                except Exception as exc:
                    self.state[error_key] = safe_error(exc)
                    self.state[pending_key] = None
                    changed = True
                else:
                    if job["status"] not in {"QUEUED", "RUNNING"}:
                        observe(kind, error_key, job)
                        self.state[pending_key] = None
                        changed = True
            # A manual success is equally valid evidence of recovery. Observe
            # the last completion, not just the job submitted by this scheduler.
            latest = self.jobs.latest_finished(kind)
            if latest and latest["job_id"] != self.state[observed_key]:
                self.state[observed_key] = latest["job_id"]
                observe(kind, error_key, latest)
                changed = True
        return changed

    def tick(self):
        with self._lock:
            if not self.enabled:
                return
            try:
                self._tick()
            except Exception as exc:
                if self.enabled:  # _save already records its more precise error.
                    self.state["last_error"] = safe_error(exc)
                    try:
                        self._save()
                    except Exception:
                        pass

    def _tick(self):
        changed = False
        local = self.clock().astimezone(SHANGHAI)
        day = local.strftime("%Y%m%d")
        if self.state["date"] != day:
            self.state.update(date=day, attempts=[], last_error=None, startup_checked=False)
            # A transient historical error pauses the current day, not all
            # future initialization. Non-transient errors require manual success.
            error = self.state["history_error"]
            if error and error.get("code") in TRANSIENT_CODES | {"PROCESS_INTERRUPTED"}:
                self.state["history_error"] = None
            changed = True
        changed = self._observe_jobs() or changed
        self.jobs.pipeline.source_check()
        minutes = local.hour * 60 + local.minute
        due = [slot for slot, minute in (("19:30", 1170), ("19:45", 1185), ("20:15", 1215)) if minutes >= minute]
        busy = self.jobs.pending()
        # A writer may be between PREPARED and COMMITTED. Daily work can be
        # queued without reading its in-flight publication; the pipeline checks
        # the latest complete batch after it obtains the writer lock.
        current = None if busy else self.store.current()
        history = None
        if current and hasattr(self.jobs.pipeline, "history_status"):
            history = self.jobs.pipeline.history_status(current)
            if history != self.state["history_gaps"]:
                self.state["history_gaps"] = history
                changed = True
        today_complete = bool(current and current["as_of"] == day)
        error = self.state["last_error"]
        may_retry = not error or error.get("code") in TRANSIENT_CODES | {"PROCESS_INTERRUPTED"}
        slot = None
        if not self.state["startup_checked"] and not self.state["attempts"] and not today_complete:
            slot = due[-1] if due else "startup"
        elif not today_complete and may_retry:
            eligible = [s for s in due if s not in self.state["attempts"]]
            if eligible:
                slot = eligible[-1]
        if not self.state["startup_checked"]:
            self.state["startup_checked"] = True
            changed = True
        if slot:
            self.state["attempts"].extend(s for s in due if s not in self.state["attempts"])
            if slot not in self.state["attempts"]:
                self.state["attempts"].append(slot)
            self._save()  # Persist the attempt before dispatch, even on failure.
            job = self.jobs.submit("update")
            self.state["pending_update"] = job["job_id"]
            self._save()
            return
        # An existing manual whole-range backfill owns its continuation. The
        # scheduler submits only one bounded chunk when the queue is idle.
        if not busy and not self.state["pending_update"] and current and not self.state["history_error"]:
            coverage = current["views"]["coverage"]
            attemptable = history["remaining_attemptable_days"] if history is not None else coverage.get("missing_days", 0)
            if attemptable:
                job = self.jobs.submit("backfill", {"start_date": coverage["start"], "end_date": current["as_of"], "max_days": 3})
                self.state["pending_history"] = job["job_id"]
                changed = True
        if changed:
            self._save()

    def start(self):
        with self._lock:
            if not self.enabled or self._thread and self._thread.is_alive():
                return
            def loop():
                while not self._stop.is_set():
                    self.tick()
                    self._stop.wait(10)
            self._thread = threading.Thread(target=loop, name="workbench-scheduler", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(2)
