"""Content-addressed local storage and recoverable single-writer publication."""
from __future__ import annotations
import fcntl
import gzip
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .models import DataError, json_bytes


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix=".writing-",dir=path.parent)
    try:
        with os.fdopen(fd,"wb") as stream:
            stream.write(value);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
        directory=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(name):os.unlink(name)


def source_identity(root: Path) -> dict:
    paths=[]
    for directory in ("src/industry_workbench","src/swivd","web","tests_workbench"):
        base=root/directory
        if base.exists():
            paths.extend(p for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    paths.extend(root/name for name in ("run_workbench.py","start_macos.command","AGENTS.md","PROJECT_INTEGRATION_SPEC.json","requirements.lock","requirements-workbench.lock","pyproject.toml","docs/implementation/INTERFACES.md","docs/implementation/APPROVED_PLAN.md") if (root/name).exists())
    files={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(paths))}
    try:
        commit=subprocess.check_output(["git","-C",str(root),"rev-parse","HEAD"],stderr=subprocess.DEVNULL,text=True).strip()
        dirty=bool(subprocess.check_output(["git","-C",str(root),"status","--porcelain"],stderr=subprocess.DEVNULL,text=True))
    except (OSError,subprocess.CalledProcessError):
        commit=None;dirty=True
    return {"commit":commit,"git_dirty":dirty,"tree_sha256":hashlib.sha256(json_bytes(files)).hexdigest(),"files":files}


class FileStore:
    def __init__(self, root: Path | str, *, development: bool = False):
        self.root=Path(root).expanduser().resolve()
        self.development=development
        self._held=False
        if development:
            parts=self.root.parts
            temporary=self.root.is_relative_to(Path(tempfile.gettempdir()).resolve())
            if not temporary and not (".local" in parts and "evidence" in parts):
                raise DataError("DEVELOPMENT_ROOT_REQUIRED")

    def path(self, relative: str) -> Path:
        if not isinstance(relative,str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise DataError("INVALID_STORAGE_PATH")
        path=self.root/relative
        if not path.resolve().is_relative_to(self.root):raise DataError("STORAGE_PATH_ESCAPE")
        return path

    @contextmanager
    def writer(self):
        if self._held:raise DataError("NESTED_WRITER")
        path=self.path("locks/writer.lock");path.parent.mkdir(parents=True,exist_ok=True)
        with path.open("a+b") as stream:
            try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise DataError("CONCURRENT_UPDATE") from None
            self._held=True
            try:
                self.recover()
                yield self
            finally:
                self._held=False;fcntl.flock(stream,fcntl.LOCK_UN)

    def _require_writer(self):
        if not self._held:raise DataError("WRITER_LOCK_REQUIRED")

    def put_bytes(self, content: bytes, kind: str = "json") -> dict:
        self._require_writer()
        sha=hashlib.sha256(content).hexdigest()
        relative=f"objects/{sha[:2]}/{sha}.gz";path=self.path(relative)
        ref={"path":relative,"sha256":sha,"bytes":len(content),"compression":"gzip","kind":kind}
        if path.exists():
            self.read_bytes(ref)
        else:atomic_write(path,gzip.compress(content,mtime=0))
        return ref

    def put_json(self,value: object,kind: str = "json") -> dict:
        return self.put_bytes(json_bytes(value),kind)

    def put_raw(self,metadata: dict,raw_bytes: bytes) -> dict:
        # Callers supply response bytes, never request headers/body.
        reference={**self.put_bytes(raw_bytes,"tushare_response"),"request":metadata}
        # This receipt also survives an already-current return or later failure.
        # Domain manifests reference their inputs; this journal audits actual
        # requests, including repeated reads of identical response bytes.
        with self.path("request_journal.ndjson").open("ab") as stream:
            stream.write(json_bytes(reference));stream.flush();os.fsync(stream.fileno())
        return reference

    def read_bytes(self,ref: dict) -> bytes:
        sha=ref.get("sha256","")
        if not re.fullmatch(r"[a-f0-9]{64}",sha):raise DataError("INVALID_OBJECT_HASH")
        if ref.get("path")!=f"objects/{sha[:2]}/{sha}.gz" or ref.get("compression")!="gzip":raise DataError("INVALID_OBJECT_REF")
        try:data=gzip.decompress(self.path(ref["path"]).read_bytes())
        except (OSError,EOFError):raise DataError("OBJECT_UNREADABLE") from None
        if len(data)!=ref.get("bytes") or hashlib.sha256(data).hexdigest()!=sha:raise DataError("OBJECT_HASH_MISMATCH")
        return data

    def read_json(self,ref: dict):
        raw=self.read_bytes(ref)
        try:return json.loads(raw,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        except (UnicodeError,ValueError):raise DataError("OBJECT_INVALID_JSON") from None

    def refs(self,value):
        if isinstance(value,dict):
            if "path" in value and "sha256" in value and "compression" in value:
                yield value
            else:
                for item in value.values():yield from self.refs(item)
        elif isinstance(value,list):
            for item in value:yield from self.refs(item)

    def verify_refs(self,value) -> int:
        checked={};pending=list(self.refs(value))
        while pending:
            ref=pending.pop()
            identity=(ref.get("path"),ref.get("bytes"),ref.get("compression"))
            if ref["sha256"] in checked:
                if checked[ref["sha256"]]!=identity:raise DataError("CONFLICTING_OBJECT_REF")
                continue
            raw=self.read_bytes(ref);checked[ref["sha256"]]=identity
            # A daily input/result contains the raw-response references. Merely
            # hashing the enclosing JSON does not verify that transitive closure.
            if ref.get("kind") in {"json","day_input","day_result","daily_close_supplement","daily_view","period_view","industry_history","calendar","industry_catalogue","source_identity"}:
                try:child=json.loads(raw,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
                except (UnicodeError,ValueError):raise DataError("OBJECT_INVALID_JSON") from None
                pending.extend(self.refs(child))
        return len(checked)

    def _manifest_path(self,batch: str) -> Path:
        if not isinstance(batch,str) or not re.fullmatch(r"BATCH-\d{8}T\d{6}-[a-f0-9]{12}",batch):raise DataError("INVALID_BATCH_ID")
        return self.path(f"batches/{batch}/manifest.json")

    def manifest(self,batch: str) -> dict:
        path=self._manifest_path(batch)
        try:
            content=path.read_bytes(); receipt=self.path(f"batches/{batch}/manifest.sha256").read_text().strip()
            if hashlib.sha256(content).hexdigest()!=receipt:raise DataError("MANIFEST_HASH_MISMATCH")
            value=json.loads(content,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        except DataError:raise
        except (OSError,ValueError):raise DataError("MANIFEST_UNREADABLE") from None
        if value.get("batch_id")!=batch or value.get("schema_version")!="industry-workbench-manifest-v1":raise DataError("MANIFEST_IDENTITY_MISMATCH")
        return value

    def published_manifest(self,batch: str) -> dict:
        value=self.manifest(batch)
        try:records=[json.loads(line) for line in self.path("run_ledger.ndjson").read_text().splitlines()]
        except (OSError,ValueError):raise DataError("LEDGER_INVALID") from None
        matching=[row for row in records if row.get("batch_id")==batch]
        digest=hashlib.sha256(self._manifest_path(batch).read_bytes()).hexdigest()
        if len(matching)!=1 or matching[0].get("status")!="COMMITTED" or matching[0].get("manifest_sha256")!=digest:
            raise DataError("BATCH_NOT_COMMITTED")
        return value

    def current(self) -> dict | None:
        if self.path("transactions/publication.json").exists():raise DataError("PUBLICATION_RECOVERY_REQUIRED")
        path=self.path("current.json")
        if not path.exists():return None
        try:pointer=json.loads(path.read_bytes())
        except (OSError,ValueError):raise DataError("CURRENT_POINTER_INVALID") from None
        value=self._validate_pointer(pointer)
        self.published_manifest(value["batch_id"])
        return value

    def _validate_pointer(self,pointer,*,verify_objects=False):
        if not isinstance(pointer,dict) or pointer.get("pointer_kind")!="manifest" or pointer.get("scope")!="industry-workbench":raise DataError("CURRENT_POINTER_INVALID")
        value=self.manifest(pointer.get("batch_id",""))
        if not self.development and (value.get("artifact_publish_state")=="DEVELOPMENT_ACCEPTANCE" or value.get("source",{}).get("git_dirty") or value.get("provider_kind")=="TEST_INJECTED_CLIENT"):
            raise DataError("DEVELOPMENT_BATCH_IN_FORMAL_ROOT")
        if pointer.get("path")!=f"batches/{value['batch_id']}/manifest.json" or pointer.get("as_of")!=value.get("as_of"):raise DataError("CURRENT_POINTER_SCOPE_MISMATCH")
        raw=self._manifest_path(value["batch_id"]).read_bytes()
        if pointer.get("sha256")!=hashlib.sha256(raw).hexdigest():raise DataError("CURRENT_POINTER_HASH_MISMATCH")
        if verify_objects:self.verify_refs(value)
        return value

    def _append_success(self,transaction: dict):
        ledger=self.path("run_ledger.ndjson")
        existing=[]
        if ledger.exists():
            for line in ledger.read_text().splitlines():
                try:existing.append(json.loads(line))
                except ValueError:raise DataError("LEDGER_INVALID") from None
        batch=transaction["after"]["batch_id"]
        matching=[r for r in existing if r.get("batch_id")==batch]
        if len(matching)>1:raise DataError("DUPLICATE_LEDGER_RECORD")
        if matching and (matching[0].get("status")!="COMMITTED" or matching[0].get("manifest_sha256")!=transaction["after"]["sha256"]):raise DataError("LEDGER_MANIFEST_CONFLICT")
        if not matching:
            with ledger.open("ab") as stream:
                stream.write(json_bytes({"batch_id":batch,"status":"COMMITTED","at":transaction["at"],"manifest_sha256":transaction["after"]["sha256"]}));stream.flush();os.fsync(stream.fileno())

    def recover(self):
        self._require_writer()
        path=self.path("transactions/publication.json")
        if not path.exists():return
        try:tx=json.loads(path.read_bytes())
        except (OSError,ValueError):raise DataError("TRANSACTION_INVALID") from None
        if tx.get("state")=="COMMITTED":
            self._validate_pointer(tx["after"],verify_objects=True)
            # Detect ledger conflicts before exposing the recovered pointer.
            self._append_success(tx)
            atomic_write(self.path("current.json"),json_bytes(tx["after"]))
        elif tx.get("state")=="PREPARED":
            if tx.get("before") is None:self.path("current.json").unlink(missing_ok=True)
            else:
                self._validate_pointer(tx["before"],verify_objects=True)
                atomic_write(self.path("current.json"),json_bytes(tx["before"]))
        else:raise DataError("TRANSACTION_INVALID")
        path.unlink()

    def publish(self,payload: dict,*,fault=None) -> dict:
        self._require_writer()
        source=payload["source"]
        if not self.development and (source.get("git_dirty") or not source.get("commit")):
            raise DataError("CLEAN_COMMITTED_SOURCE_REQUIRED")
        if payload.get("provider_kind")=="TEST_INJECTED_CLIENT" and not self.development:raise DataError("TEST_PROVIDER_FORMAL_PUBLISH_DENIED")
        self.verify_refs(payload)
        current=self.current()
        if current and payload["as_of"]<current["as_of"]:raise DataError("AS_OF_REGRESSION")
        stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S");batch=f"BATCH-{stamp}-{uuid.uuid4().hex[:12]}"
        manifest={**payload,"schema_version":"industry-workbench-manifest-v1","batch_id":batch,"created_at":utc_now(),
                  "parent_batch_id":current["batch_id"] if current else None,"research_grade":"RESEARCH_ONLY","decision_eligible":False,"production_approved":False,
                  "execution_status":"SUCCEEDED","artifact_publish_state":"DEVELOPMENT_ACCEPTANCE" if self.development else "LOCAL_RESEARCH_SNAPSHOT",
                  "live_validation_state":"NOT_LIVE_TEST_PROVIDER" if payload.get("provider_kind")=="TEST_INJECTED_CLIENT" else "LIVE_CAPTURED_NOT_RESEARCH_PROMOTION"}
        raw=json_bytes(manifest);sha=hashlib.sha256(raw).hexdigest()
        atomic_write(self._manifest_path(batch),raw);atomic_write(self.path(f"batches/{batch}/manifest.sha256"),(sha+"\n").encode())
        pointer={"pointer_kind":"manifest","scope":"industry-workbench","batch_id":batch,"path":f"batches/{batch}/manifest.json","sha256":sha,"as_of":payload["as_of"]}
        before=json.loads(self.path("current.json").read_bytes()) if self.path("current.json").exists() else None
        tx={"state":"PREPARED","before":before,"after":pointer,"at":utc_now()};txpath=self.path("transactions/publication.json")
        atomic_write(txpath,json_bytes(tx))
        if fault:fault("prepared")
        atomic_write(self.path("current.json"),json_bytes(pointer))
        if fault:fault("pointer_replaced")
        tx["state"]="COMMITTED";atomic_write(txpath,json_bytes(tx))
        if fault:fault("committed")
        self._append_success(tx);txpath.unlink()
        return manifest
