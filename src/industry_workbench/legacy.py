"""Read-only access to a verified imported SWIVD snapshot.

This adapter never creates LocalApp, JobManager, a HTTP server or a provider.
Old schema validation stays authoritative; new application writes are separate.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from concurrent.futures import Future
from pathlib import Path
from urllib.parse import unquote, urlsplit

from swivd.io_utils import sha256_file
from swivd.v2_server import LocalApp
from swivd.v2_storage import resolve_pointer
from swivd.v2_validator import validate_run_v2


RUN_RE = re.compile(r"SWIVD2-RUN-\d{8}-\d{3}")
CODE_RE = re.compile(r"\d{6}\.SI")


class LegacySnapshotReader:
    """Read one imported run root, or a complete legacy data root with pointer.

    The exact manifest remains unchanged. No newest-directory guessing occurs.
    The reused LocalApp methods below only read; its constructor is never called.
    """

    verified_json = LocalApp.verified_json
    verified_csv = LocalApp.verified_csv
    ui_catalog = LocalApp.ui_catalog
    sw2014_archive = LocalApp.sw2014_archive

    def __init__(self, root: str | Path):
        self.root = Path(root).absolute()
        self.validated: dict[str, dict[str, tuple[int, str]]] = {}
        self._validated_manifests: dict[str, str] = {}
        self._validation_mutex = threading.Lock()
        self._validation_flights: dict[tuple[str, str], Future] = {}

    def _run_root(self, run_id: str) -> Path:
        if not RUN_RE.fullmatch(run_id):
            raise ValueError("INVALID_LEGACY_RUN_ID")
        if (self.root / "manifest.json").is_file():
            if self.root.name != run_id:
                raise ValueError("LEGACY_RUN_ID_MISMATCH")
            return self.root
        return self.root / "runs" / run_id

    def snapshot(self, run_id: str) -> Path:
        root = self._run_root(run_id)
        manifest_path = root / "manifest.json"
        if self.root.is_symlink() or root.is_symlink() or manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("LEGACY_SNAPSHOT_NOT_VERIFIED")
        digest = sha256_file(manifest_path)
        key = (run_id, digest)
        with self._validation_mutex:
            if self._validated_manifests.get(run_id) == digest:
                return root
            future = self._validation_flights.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._validation_flights[key] = future
        if not owner:
            future.result()
            return root
        try:
            manifest = validate_run_v2(root)
            if manifest_path.is_symlink() or sha256_file(manifest_path) != digest:
                raise ValueError("LEGACY_MANIFEST_DRIFT")
            inventory = {str(item["path"]): (int(item["bytes"]), str(item["sha256"])) for item in manifest["artifacts"]}
            with self._validation_mutex:
                self.validated[run_id] = inventory
                self._validated_manifests[run_id] = digest
            future.set_result(None)
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._validation_mutex:
                self._validation_flights.pop(key, None)
        return root

    def verified_bytes(self, run_id: str, relative: str) -> bytes:
        root = self.snapshot(run_id)
        expected = self.validated[run_id].get(relative)
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts or "\\" in relative:
            raise ValueError("LEGACY_ARTIFACT_PATH_INVALID")
        path = root / candidate
        if expected is None or not path.is_file() or path.is_symlink():
            raise ValueError("LEGACY_ARTIFACT_NOT_VERIFIED")
        if any(parent.is_symlink() for parent in path.parents if parent != root and root in parent.parents):
            raise ValueError("LEGACY_ARTIFACT_PATH_INVALID")
        body = path.read_bytes()
        if (len(body), hashlib.sha256(body).hexdigest()) != expected:
            raise ValueError("LEGACY_ARTIFACT_DRIFT")
        return body

    def current_snapshot(self) -> dict:
        if not self.root.exists():
            return {"run_id": None, "as_of": None}
        if self.root.is_symlink():
            raise ValueError("LEGACY_ROOT_INVALID")
        path = self.root / "manifest.json"
        if path.is_file():
            if path.is_symlink():
                raise ValueError("LEGACY_MANIFEST_INVALID")
            manifest = json.loads(path.read_text(encoding="utf-8"))
            run_id = str(manifest.get("run_id", ""))
            self.snapshot(run_id)
            return {"run_id": run_id, "as_of": manifest["as_of"]}
        resolved = resolve_pointer(self.root)
        if resolved is None:
            return {"run_id": None, "as_of": None}
        run_id = resolved[1]["run_id"]
        self.snapshot(run_id)
        return {"run_id": run_id, "as_of": resolved[1]["as_of"]}

    def status(self) -> dict:
        try:
            current = self.current_snapshot()
            return {"state": "AVAILABLE" if current["run_id"] else "NOT_INSTALLED", **current}
        except (OSError, ValueError, KeyError, TypeError):
            return {"state": "INVALID", "run_id": None, "as_of": None, "reason": "LEGACY_VALIDATION_FAILED"}

    def bootstrap(self, nonce: str = "READ_ONLY") -> dict:
        status = self.status()
        return {"nonce": nonce, "api_base": "/legacy", "read_only": True,
                "current_run_id": status["run_id"], "current_snapshot_state": status["state"],
                "legacy_state": status["state"], "active_jobs": [], "research_grade": "RESEARCH_ONLY",
                "decision_eligible": False, "production_approved": False}

    def get_json(self, path: str) -> tuple[int, dict] | None:
        route = unquote(urlsplit(path).path)
        if route.startswith("/legacy/"):
            route = route[len("/legacy"):]
        if route == "/api/v1/jobs/active":
            return 200, {"jobs": []}
        if route.startswith("/api/v1/jobs/"):
            return 404, {"error": "READ_ONLY_ARCHIVE"}
        try:
            if route == "/api/v1/snapshots/current":
                return 200, self.current_snapshot()
            match = re.fullmatch(r"/api/v1/snapshots/([^/]+)/catalog", route)
            if match:
                return 200, self.ui_catalog(match[1])
            match = re.fullmatch(r"/api/v1/snapshots/([^/]+)/industries/(L[123])/([^/]+)", route)
            if match:
                run_id, level, code = match.groups()
                if not CODE_RE.fullmatch(code):
                    raise KeyError(code)
                catalog = self.ui_catalog(run_id)
                row = next(row for row in catalog["industries"] if row["level"] == level and row["index_code"] == code)
                return 200, self.verified_json(run_id, "ui/" + row["shard"])
            match = re.fullmatch(r"/api/v1/snapshots/([^/]+)/archive/sw2014/(catalog|industries/([^/]+))", route)
            if match:
                run_id, suffix, code = match.groups()
                if code is not None and not CODE_RE.fullmatch(code):
                    raise KeyError(code)
                metadata, summaries, histories = self.sw2014_archive(run_id, include_history=code is not None)
                if code is None:
                    as_of = {row["as_of"] for row in summaries}
                    if len(as_of) != 1:
                        raise ValueError("ARCHIVE_AS_OF_INVALID")
                    return 200, {"schema_version": "swivd-sw2014-catalog-v1", "taxonomy": "SW2014", "level": "L1", "source_run_id": metadata["run_id"], "as_of": next(iter(as_of)), "industries": summaries}
                summary = next(row for row in summaries if row["index_code"] == code)
                return 200, {"schema_version": "swivd-sw2014-industry-v1", "taxonomy": "SW2014", "level": "L1", "source_run_id": metadata["run_id"], "as_of": summary["as_of"], "index_code": code, "summary": summary, "history": [row for row in histories if row["index_code"] == code]}
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            return 404, {"error": "LEGACY_SNAPSHOT_UNAVAILABLE"}
        return None
