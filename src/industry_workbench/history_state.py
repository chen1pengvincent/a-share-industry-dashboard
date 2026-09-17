"""Durable, date-scoped backfill failures; never a substitute for daily data."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import gzip
import io
import json
import re
import tarfile
import uuid

from .models import DataError, json_bytes, parse_date
from .storage import atomic_write, utc_now


POLICY_VERSION = "historical-universe-failure-v1"
_DATA_FILES = ("src/industry_workbench/provider.py", "src/industry_workbench/taxonomy.py",
               "src/industry_workbench/transport.py", "src/industry_workbench/models.py")


def evidence_version(source):
    """UI/test/doc changes must not repeatedly retry an unchanged bad date."""
    files = source.get("files", {})
    return hashlib.sha256(json_bytes({"policy": POLICY_VERSION,
                                     "data_code": {name: files.get(name) for name in _DATA_FILES}})).hexdigest()


class HistoryState:
    def __init__(self, store, source, *, verify_evidence=True, published_failures=()):
        self.store = store
        self.version = evidence_version(source)
        self.evidence_validation = "FULL_CLOSURE" if verify_evidence else "RECORDS_ONLY"
        self._verified_sources = set()
        self.index = {"schema_version": "history-attempt-index-v1", "head": None, "dates": {}}
        path = store.path("history/index.json")
        if not path.exists() and path.parent.exists():
            raise DataError("HISTORY_STATE_INVALID")
        if path.exists():
            try:
                value = json.loads(path.read_bytes())
                if (value.get("schema_version") != self.index["schema_version"]
                        or not isinstance(value.get("dates"), dict) or not value["dates"] or not value.get("head")):
                    raise ValueError()
                self.index = value
            except (OSError, ValueError, AttributeError):
                raise DataError("HISTORY_STATE_INVALID") from None
        self.records = {}
        latest, oldest, seen, chain_refs, chain_records = {}, {}, set(), {}, {}
        reference = self.index["head"]
        # The immutable chain is the evidence; dates is only its exact index.
        # Walk metadata even for RECORDS_ONLY, without loading financial raw.
        while reference is not None:
            if not isinstance(reference, dict) or reference.get("kind") != "json" or reference.get("sha256") in seen:
                raise DataError("HISTORY_STATE_INVALID")
            seen.add(reference.get("sha256"))
            record = store.read_json(reference)
            self._validate_record(record)
            chain_refs[reference["sha256"]] = reference
            chain_records[reference["sha256"]] = record
            day = record["trade_date"]
            if day not in latest:
                latest[day] = reference
                self.records[day] = record
            elif oldest[day]["supersedes"] != reference:
                raise DataError("HISTORY_STATE_INVALID")
            oldest[day] = record
            reference = record["previous_attempt"]
        if latest != self.index["dates"] or any(record["supersedes"] is not None for record in oldest.values()):
            raise DataError("HISTORY_STATE_INVALID")
        # Published failure references survive a later resolution. They anchor
        # the operational index against accidental deletion or rollback.
        for published in published_failures:
            try:
                anchor = published["evidence_ref"]
                recorded = chain_records.get(anchor["sha256"], {})
                if (chain_refs.get(anchor["sha256"]) != anchor or recorded.get("status") != "BLOCKED"
                        or recorded.get("trade_date") != published["trade_date"]):
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise DataError("HISTORY_STATE_INVALID") from None
        # A record is allowed to suppress a request only while its referenced
        # evidence is intact; a hash failure is never a recoverable day gap.
        if verify_evidence:
            store.verify_refs(self.index)

    def _validate_record(self, record):
        try:
            if (record["schema_version"] != "history-attempt-v1" or record["status"] not in {"BLOCKED", "RESOLVED"}
                    or not re.fullmatch(r"[a-f0-9]{64}", record["evidence_version"])):
                raise ValueError()
            parse_date(record["trade_date"])
            for field in ("previous_attempt", "supersedes"):
                if record[field] is not None and not isinstance(record[field], dict):
                    raise ValueError()
            if record["policy_version"] != POLICY_VERSION or record["scope"] != "HISTORICAL_TRADE_DATE":
                raise ValueError()
            capture = record["capture_source"]
            if capture["snapshot"]["kind"] != "source_snapshot_tar_gz" or capture["inventory"]["kind"] != "source_identity":
                raise ValueError()
            inventory = self.store.read_json(capture["inventory"])
            if any(capture["identity"][key] != inventory[key] for key in ("commit", "git_dirty", "tree_sha256")):
                raise ValueError()
            if inventory["tree_sha256"] != hashlib.sha256(json_bytes(inventory["files"])).hexdigest():
                raise ValueError()
            source_key = (capture["snapshot"]["sha256"], capture["inventory"]["sha256"])
            if source_key not in self._verified_sources:
                archive_bytes = gzip.decompress(self.store.read_bytes(capture["snapshot"]))
                with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
                    observed = {}
                    for member in archive.getmembers():
                        if not member.isfile() or member.name in observed or member.name not in inventory["files"]:
                            raise ValueError()
                        observed[member.name] = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
                if observed != inventory["files"]:
                    raise ValueError()
                self._verified_sources.add(source_key)
            if record["evidence_version"] != evidence_version(inventory):
                raise ValueError()
            if not isinstance(record["source_refs"], list) or not record["source_refs"]:
                raise ValueError()
            if record["status"] == "BLOCKED":
                day = record["trade_date"]
                parse_date(record["target_date"])
                if record["code"] != "INDEPENDENT_UNIVERSE_INCOMPLETE" or day >= record["target_date"]:
                    raise ValueError()
                requests = [ref.get("request", {}) for ref in record["source_refs"]]
                target_apis = {r.get("api_name") for r in requests if r.get("params", {}).get("trade_date") == day}
                states = {r.get("params", {}).get("list_status") for r in requests if r.get("api_name") == "stock_basic"}
                if not {"daily", "daily_basic", "moneyflow", "suspend_d"} <= target_apis or states != {"L", "D", "P"}:
                    raise ValueError()
            elif record["code"] is not None or not record.get("batch_id"):
                raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError, OSError, EOFError, tarfile.TarError):
            raise DataError("HISTORY_STATE_INVALID") from None

    def is_blocked(self, day, *, retry_failed=False, retry_id=None):
        record = self.records.get(day)
        if not record or record["status"] != "BLOCKED":
            return False
        # A user retry gets one attempt per date for the whole logical job.
        if retry_failed and record.get("retry_id") != retry_id:
            return False
        return record["evidence_version"] == self.version

    def summary(self, missing, *, retry_failed=False, retry_id=None, attempted_dates=()):
        blocked, pending = [], []
        attempted = set(attempted_dates)
        for day in sorted(set(missing)):
            record = self.records.get(day)
            if ((day in attempted and record and record["status"] == "BLOCKED")
                    or self.is_blocked(day, retry_failed=retry_failed, retry_id=retry_id)):
                record = self.records[day]
                blocked.append({"trade_date": day, "code": record["code"],
                                "reason": "当前证据下未通过；未认定永久不可恢复",
                                "evidence_version": record["evidence_version"],
                                "evidence_ref": self.index["dates"][day]})
            else:
                pending.append(day)
        return {"blocked_dates": blocked, "pending_dates": pending, "remaining_days": len(blocked) + len(pending),
                "remaining_attemptable_days": len(pending), "history_complete": not (blocked or pending),
                "evidence_validation": self.evidence_validation}

    def _record(self, day, status, *, code, capture_source, source_refs, retry_id=None, job_id=None, batch_id=None, target_date=None):
        self.store._require_writer()
        parse_date(day)
        # Verify the actual evidence before it may suppress future attempts.
        self.store.verify_refs({"capture_source": capture_source, "source_refs": source_refs})
        record = {"schema_version": "history-attempt-v1", "attempt_id": "ATTEMPT-" + uuid.uuid4().hex,
                  "trade_date": day, "status": status, "code": code, "at": utc_now(),
                  "scope": "HISTORICAL_TRADE_DATE", "policy_version": POLICY_VERSION,
                  "evidence_version": self.version, "capture_source": capture_source,
                  "source_refs": source_refs, "retry_id": retry_id, "job_id": job_id,
                  "batch_id": batch_id, "target_date": target_date, "previous_attempt": self.index["head"],
                  "supersedes": self.index["dates"].get(day)}
        self._validate_record(record)
        reference = self.store.put_json(record)
        successor = deepcopy(self.index)
        successor["head"] = reference
        successor["dates"][day] = reference
        try:
            atomic_write(self.store.path("history/index.json"), json_bytes(successor))
        except OSError:
            raise DataError("HISTORY_STATE_WRITE_FAILED") from None
        self.index = successor
        self.records[day] = record
        return reference

    def block(self, day, **kwargs):
        return self._record(day, "BLOCKED", **kwargs)

    def resolve(self, day, **kwargs):
        if self.records.get(day, {}).get("status") == "BLOCKED":
            return self._record(day, "RESOLVED", code=None, **kwargs)
        return None
