"""Loopback HTTP adapter. Reads are local; writes enter the shared job manager."""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import secrets
from urllib.parse import parse_qs, unquote, urlsplit

from .jobs import safe_error
from .legacy import LegacySnapshotReader
from .models import DataError, json_bytes
from .query import QueryService


class WorkbenchApp:
    def __init__(self, source_root: Path, jobs, scheduler=None, legacy_root=None):
        self.source_root = Path(source_root).resolve()
        self.jobs = jobs
        self.store = jobs.store
        self.query = QueryService(self.store)
        self.scheduler = scheduler
        self.nonce = secrets.token_urlsafe(32)
        self.legacy = LegacySnapshotReader(legacy_root or self.store.path("legacy"))

    def current(self):
        manifest = self.store.current()
        source_error = None
        try:
            self.jobs.pipeline.source_check()
        except Exception as exc:
            source_error = safe_error(exc)
        history_error = None
        try:
            history_scan = self.jobs.pipeline.history_status(current=manifest)
        except Exception as exc:
            # The operational retry index must not hide a verified published
            # snapshot. Writes remain blocked; unknown is never an empty scan.
            history_scan = None
            history_error = safe_error(exc)
        return {"batch_id": manifest["batch_id"] if manifest else None,
                "as_of": manifest["as_of"] if manifest else None,
                "created_at": manifest["created_at"] if manifest else None,
                "publication_state": manifest["publication_state"] if manifest else "EMPTY",
                "artifact_publish_state": manifest.get("artifact_publish_state") if manifest else None,
                "history": manifest["views"]["coverage"] if manifest else {},
                "history_scan": history_scan, "history_scan_error": history_error,
                "job": self.jobs.active(), "legacy": self.legacy.status(),
                "scheduler": self.scheduler.status() if self.scheduler else {"enabled": False},
                "development": self.store.development, "update_blocked": source_error or history_error,
                "research_grade": "RESEARCH_ONLY", "decision_eligible": False, "production_approved": False}


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, app: WorkbenchApp, port=8765):
        self.app = app
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    server_version = "IndustryWorkbench"
    sys_version = ""

    def log_message(self, format, *args):
        # URLs and bodies may be user-controlled; job records are the audit log.
        pass

    def _allowed(self, *, write=False):
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get_all("Host") != [expected]:
            raise DataError("INVALID_HOST")
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            raise DataError("INVALID_ORIGIN")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise DataError("CROSS_SITE_DENIED")
        if write:
            nonces = self.headers.get_all("X-Workbench-Nonce")
            if not nonces or len(nonces) != 1 or not secrets.compare_digest(nonces[0], self.server.app.nonce):
                raise DataError("INVALID_NONCE")

    def _send(self, status, content, content_type="application/json; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def _json(self, status, value):
        self._send(status, json_bytes(value))

    def _error(self, exc):
        error = safe_error(exc)
        code = error["code"]
        status = 403 if code in {"INVALID_HOST", "INVALID_ORIGIN", "INVALID_NONCE", "CROSS_SITE_DENIED", "READ_ONLY_ARCHIVE"} else 404 if code.endswith("NOT_FOUND") else 409 if code in {"CONCURRENT_UPDATE", "CLEAN_COMMITTED_SOURCE_REQUIRED", "PUBLICATION_RECOVERY_REQUIRED"} else 400
        self._json(status, {"error": error})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self._allowed()
            self._get()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self._error(exc)

    def _get(self):
        app = self.server.app
        url = urlsplit(self.path)
        route = url.path
        query = parse_qs(url.query, keep_blank_values=True)
        if any(len(values) != 1 for values in query.values()):
            raise DataError("DUPLICATE_QUERY_PARAMETER")
        params = {key: value[0] for key, value in query.items()}
        if route == "/api/v2/bootstrap":
            return self._json(200, {"nonce": app.nonce, "version": "0.1.0", "default_taxonomy": "SW", "default_level": "L1"})
        if route == "/api/v2/current":
            return self._json(200, app.current())
        if route == "/api/v2/jobs/active":
            return self._json(200, app.jobs.active() or {"job": None})
        match = re.fullmatch(r"/api/v2/jobs/(JOB-[a-f0-9]{20})", route)
        if match:
            return self._json(200, app.jobs.get(match[1]))
        match = re.fullmatch(r"/api/v2/batches/([^/]+)/quality", route)
        if match:
            if set(params)-{"period_kind","period_key"}:raise DataError("UNKNOWN_QUERY_PARAMETER")
            return self._json(200,app.query.quality(match[1],**params))
        match = re.fullmatch(r"/api/v2/batches/([^/]+)/(catalog|industries)(?:/([^/]+)/(history|members))?", route)
        if match:
            batch, resource, encoded_uid, detail = match.groups()
            if resource == "catalog" and not detail:
                return self._json(200, app.query.catalog(batch))
            if detail:
                uid = unquote(encoded_uid)
                if detail == "history":
                    result = app.query.history(batch, uid, period_kind=params.get("period_kind", "day"))
                else:
                    result = app.query.members(batch, uid, period_kind=params.get("period_kind", "day"), period_key=params.get("period_key"))
            else:
                expected = {"taxonomy", "level_or_series", "period_kind", "period_key"}
                if set(params) - expected:
                    raise DataError("UNKNOWN_QUERY_PARAMETER")
                result = app.query.industries(batch, **params)
            return self._json(200, result)
        match = re.fullmatch(r"/api/v2/exports/(JOB-[a-f0-9]{20}\.(csv|xlsx))", route)
        if match:
            job = app.jobs.get(match[1].rsplit(".", 1)[0])
            if job["kind"] != "export" or job["status"] != "SUCCEEDED":
                raise DataError("EXPORT_NOT_FOUND")
            content = app.store.path(f"exports/{match[1]}").read_bytes()
            if hashlib.sha256(content).hexdigest() != job["result"]["sha256"]:
                raise DataError("EXPORT_HASH_MISMATCH")
            mime = "text/csv; charset=utf-8" if match[2] == "csv" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            return self._send(200, content, mime, {"Content-Disposition": f'attachment; filename="{match[1]}"'})
        if route == "/legacy":
            return self._send(302, b"", "text/plain", {"Location": "/legacy/"})
        if route == "/legacy/bootstrap.js":
            body = b"window.__SWIVD__=" + json_bytes(app.legacy.bootstrap()).rstrip() + b";\n"
            return self._send(200, body, "text/javascript; charset=utf-8")
        if route.startswith("/legacy/api/v1/") or route.startswith("/api/v1/"):
            result = app.legacy.get_json(route)
            if result is not None:
                return self._json(*result)
        assets = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/app.css": "app.css",
                  "/legacy/": "legacy/index.html", "/legacy/app.js": "legacy/app.js", "/legacy/app.css": "legacy/app.css", "/legacy/history.css": "legacy/history.css"}
        if route in assets:
            path = app.source_root / "web" / assets[route]
            if not path.is_file() or path.is_symlink():
                raise DataError("ASSET_NOT_FOUND")
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            return self._send(200, path.read_bytes(), mime + "; charset=utf-8")
        raise DataError("ROUTE_NOT_FOUND")

    def do_POST(self):
        try:
            self._allowed(write=True)
            if self.path.startswith("/legacy/") or self.path.startswith("/api/v1/"):
                raise DataError("READ_ONLY_ARCHIVE")
            if self.headers.get("Transfer-Encoding") is not None:
                raise DataError("INVALID_REQUEST_BODY")
            lengths = self.headers.get_all("Content-Length")
            if not lengths or len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= 16384:
                raise DataError("INVALID_REQUEST_BODY")
            if self.headers.get_content_type() != "application/json":
                raise DataError("JSON_REQUIRED")
            raw = self.rfile.read(int(lengths[0]))
            if len(raw) != int(lengths[0]):
                raise DataError("INVALID_REQUEST_BODY")
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise DataError("DUPLICATE_JSON_KEY")
                    result[key] = value
                return result
            body = json.loads(raw, object_pairs_hook=unique_object, parse_constant=lambda _: (_ for _ in ()).throw(DataError("NONFINITE_JSON")))
            if not isinstance(body, dict):
                raise DataError("JSON_OBJECT_REQUIRED")
            match = re.fullmatch(r"/api/v2/jobs/(update|backfill|export)", self.path)
            if not match:
                raise DataError("ROUTE_NOT_FOUND")
            if match[1] == "update" and body:
                raise DataError("INVALID_UPDATE_PARAMETERS")
            if match[1] == "backfill":
                if not {"start_date", "end_date"} <= set(body) or set(body) - {"start_date", "end_date", "retry_failed"}:
                    raise DataError("INVALID_BACKFILL_PARAMETERS")
                if "retry_failed" in body and type(body["retry_failed"]) is not bool:
                    raise DataError("INVALID_RETRY_FAILED_PARAMETER")
            job = self.server.app.jobs.submit(match[1], body)
            self._json(202, job)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self._error(exc)
