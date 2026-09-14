"""Loopback-only HTTP server for browsing verified immutable snapshots."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import mimetypes
import re
import secrets
import threading
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import unquote, urlsplit

from .io_utils import sha256_file
from .v2_jobs import JobConflict, JobManager
from .v2_pipeline import catalog_summary_projection
from .v2_storage import resolve_pointer
from .v2_validator import validate_run_v2


RUN_RE = re.compile(r"^SWIVD2-RUN-\d{8}-\d{3}$")
CODE_RE = re.compile(r"^\d{6}\.SI$")
LEGACY_RUN_ID = "SWIVD-RUN-20260828-004"


class LoopbackHTTPServer(ThreadingHTTPServer):
    """HTTP server that avoids reverse-DNS lookup during local bind."""

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class LocalApp:
    def __init__(self, *, project_root: Path, data_dir: Path, host: str, port: int) -> None:
        if host != "127.0.0.1":
            raise ValueError("NON_LOOPBACK_BIND_FORBIDDEN")
        if not 1 <= port <= 65535:
            raise ValueError("INVALID_PORT")
        self.project_root = project_root
        self.data_dir = data_dir
        self.host = host
        self.port = port
        self.nonce = secrets.token_urlsafe(32)
        self.jobs = JobManager(data_dir=data_dir, spec_path=project_root / "PROJECT_SPEC_V4.json")
        self.validated: dict[str, dict[str, tuple[int, str]]] = {}
        self._validated_manifests: dict[str, str] = {}
        self._validation_mutex = threading.Lock()
        self._validation_flights: dict[tuple[str, str], Future[None]] = {}

    @property
    def allowed_origins(self) -> set[str]:
        return {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}

    def snapshot(self, run_id: str) -> Path:
        if not RUN_RE.fullmatch(run_id):
            raise KeyError(run_id)
        root = self.data_dir / "runs" / run_id
        manifest_path = root / "manifest.json"
        if root.is_symlink() or manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("SNAPSHOT_MANIFEST_NOT_VERIFIED")
        manifest_hash = sha256_file(manifest_path)
        key = (run_id, manifest_hash)
        with self._validation_mutex:
            if self._validated_manifests.get(run_id) == manifest_hash:
                return root
            future = self._validation_flights.get(key)
            owns_flight = future is None
            if future is None:
                future = Future()
                self._validation_flights[key] = future
        if not owns_flight:
            future.result()
            return root
        try:
            manifest = validate_run_v2(root)
            expected = {
                str(record["path"]): (int(record["bytes"]), str(record["sha256"]))
                for record in manifest["artifacts"]
            }
            if manifest_path.is_symlink() or sha256_file(manifest_path) != manifest_hash:
                raise ValueError("SNAPSHOT_MANIFEST_DRIFT")
            with self._validation_mutex:
                self.validated[run_id] = expected
                self._validated_manifests[run_id] = manifest_hash
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
        path = root / relative
        if expected is None or not path.is_file() or path.is_symlink():
            raise ValueError("SNAPSHOT_ARTIFACT_NOT_VERIFIED")
        body = path.read_bytes()
        if len(body) != expected[0] or hashlib.sha256(body).hexdigest() != expected[1]:
            raise ValueError("SNAPSHOT_ARTIFACT_DRIFT")
        return body

    def verified_json(self, run_id: str, relative: str) -> object:
        return json.loads(self.verified_bytes(run_id, relative).decode("utf-8"))

    def verified_csv(self, run_id: str, relative: str) -> list[dict[str, str]]:
        text = self.verified_bytes(run_id, relative).decode("utf-8")
        return list(csv.DictReader(io.StringIO(text, newline="")))

    def ui_catalog(self, run_id: str) -> dict[str, object]:
        self.snapshot(run_id)
        loaded = self.verified_json(run_id, "ui/catalog.json")
        if not isinstance(loaded, dict):
            raise ValueError("INVALID_UI_CATALOG")
        catalog = dict(loaded)
        if catalog.get("schema_version") in {
            "swivd-ui-catalog-v2",
            "swivd-ui-catalog-v3",
        }:
            return catalog
        if catalog.get("schema_version") != "swivd-ui-catalog-v1":
            raise ValueError("UNSUPPORTED_UI_CATALOG")
        parents: dict[tuple[str, str], str] = {}
        for level in ("L1", "L2", "L3"):
            for row in self.verified_csv(
                run_id, f"inputs/normalized/classification_sw2021_{level.lower()}.csv"
            ):
                parents[(level, row["index_code"])] = row.get("parent_code", "")
        upgraded: list[dict[str, object]] = []
        industries = catalog.get("industries")
        if not isinstance(industries, list):
            raise ValueError("INVALID_UI_CATALOG")
        for value in industries:
            if not isinstance(value, dict):
                raise ValueError("INVALID_UI_CATALOG_ENTRY")
            level, code, shard_path = value.get("level"), value.get("index_code"), value.get("shard")
            if not all(isinstance(item, str) for item in (level, code, shard_path)):
                raise ValueError("INVALID_UI_CATALOG_ENTRY")
            shard = self.verified_json(run_id, f"ui/{shard_path}")
            if not isinstance(shard, dict) or not isinstance(shard.get("industry"), dict):
                raise ValueError("INVALID_UI_SHARD")
            upgraded.append(
                {
                    "level": level,
                    "index_code": code,
                    "industry_name": value.get("industry_name"),
                    "parent_code": parents[(level, code)],
                    "is_pub": value.get("is_pub"),
                    "member_row_count": value.get("member_row_count"),
                    **catalog_summary_projection(shard["industry"]),
                    "shard": shard_path,
                }
            )
        catalog["schema_version"] = "swivd-ui-catalog-v2"
        catalog["industries"] = upgraded
        return catalog

    def current_snapshot(self) -> dict[str, object]:
        resolved = resolve_pointer(self.data_dir)
        return {
            "run_id": resolved[1]["run_id"] if resolved else None,
            "as_of": resolved[1]["as_of"] if resolved else None,
        }

    def bootstrap(self) -> dict[str, object]:
        try:
            current = self.current_snapshot()
            current_state = "READY" if current["run_id"] else "EMPTY"
        except (OSError, ValueError) as exc:
            current = {"run_id": None, "as_of": None}
            current_state = (
                "PUBLICATION_RECOVERY_REQUIRED"
                if str(exc) == "PUBLICATION_RECOVERY_REQUIRED"
                else "CURRENT_SNAPSHOT_UNAVAILABLE"
            )
        return {
            "nonce": self.nonce,
            "current_run_id": current["run_id"],
            "current_snapshot_state": current_state,
            "active_jobs": self.jobs.active(),
            "research_grade": "RESEARCH_ONLY",
            "decision_eligible": False,
            "production_approved": False,
        }

    def sw2014_archive(
        self, run_id: str, *, include_history: bool = True
    ) -> tuple[dict[str, str], list[dict[str, str]], list[dict[str, str]]]:
        catalog = self.ui_catalog(run_id)
        metadata = catalog.get("legacy_archive")
        if metadata != {"run_id": LEGACY_RUN_ID, "level": "L1", "taxonomy": "SW2014"}:
            raise ValueError("INVALID_SW2014_METADATA")
        legacy = dict(metadata)
        prefix = f"legacy/{legacy['run_id']}/tables"
        summaries = self.verified_csv(run_id, f"{prefix}/sw2014_archive.csv")
        histories = (
            self.verified_csv(run_id, f"{prefix}/sw2014_history.csv")
            if include_history
            else []
        )
        if (
            len(summaries) != 28
            or len({row.get("index_code") for row in summaries}) != 28
            or any(
                row.get("taxonomy") != "SW2014"
                or not CODE_RE.fullmatch(str(row.get("index_code", "")))
                for row in summaries
            )
        ):
            raise ValueError("INVALID_SW2014_ARCHIVE")
        allowed = {str(row["index_code"]) for row in summaries}
        if any(
            row.get("taxonomy") != "SW2014"
            or row.get("index_code") not in allowed
            for row in histories
        ):
            raise ValueError("INVALID_SW2014_HISTORY")
        return (
            legacy,
            sorted(summaries, key=lambda row: row["index_code"]),
            sorted(histories, key=lambda row: (row["index_code"], row["trade_date"])),
        )


def _handler(app: LocalApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "SWIVDLocal/2"

        def log_message(self, format: str, *args: object) -> None:
            # Never interpolate BaseHTTPRequestHandler's request line: it
            # contains the full target, including a possibly secret query.
            method = self.command if self.command in {"GET", "POST"} else "OTHER"
            print(f"{self.client_address[0]} {method} request completed")

        def _headers(self, status: int, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()

        def _send(self, status: int, payload: object) -> None:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(body))
            self.wfile.write(body)

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in {
                f"127.0.0.1:{app.port}", f"localhost:{app.port}"
            }

        def _static(self, path: Path, *, replacements: dict[bytes, bytes] | None = None) -> None:
            if not path.is_file() or path.is_symlink():
                self._send(404, {"error": "NOT_FOUND"})
                return
            body = path.read_bytes()
            for before, after in (replacements or {}).items():
                body = body.replace(before, after)
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
                content_type += "; charset=utf-8"
            self._headers(200, content_type, len(body))
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                self._send(421, {"error": "INVALID_HOST"})
                return
            route = unquote(urlsplit(self.path).path)
            if route == "/":
                self._static(app.project_root / "web" / "index.html")
                return
            if route == "/bootstrap.js":
                body = (
                    "window.__SWIVD__="
                    + json.dumps(app.bootstrap(), ensure_ascii=False, separators=(",", ":"))
                    + ";\n"
                ).encode("utf-8")
                self._headers(200, "application/javascript; charset=utf-8", len(body))
                self.wfile.write(body)
                return
            if route in {"/app.js", "/app.css", "/history.css"}:
                self._static(app.project_root / "web" / route[1:])
                return
            if route == "/api/v1/jobs/active":
                self._send(200, {"jobs": app.jobs.active()})
                return
            if route == "/api/v1/snapshots/current":
                try:
                    self._send(200, app.current_snapshot())
                except (OSError, ValueError) as exc:
                    code = (
                        "PUBLICATION_RECOVERY_REQUIRED"
                        if str(exc) == "PUBLICATION_RECOVERY_REQUIRED"
                        else "CURRENT_SNAPSHOT_UNAVAILABLE"
                    )
                    self._send(503, {"error": code})
                return
            match = re.fullmatch(r"/api/v1/jobs/([0-9a-f-]+)", route)
            if match:
                try:
                    self._send(200, app.jobs.get_public(match.group(1)))
                except (KeyError, OSError, ValueError, TypeError):
                    self._send(404, {"error": "JOB_NOT_FOUND"})
                return
            match = re.fullmatch(r"/api/v1/snapshots/([^/]+)/catalog", route)
            if match:
                try:
                    self._send(200, app.ui_catalog(match.group(1)))
                except (KeyError, OSError, ValueError):
                    self._send(404, {"error": "SNAPSHOT_NOT_FOUND"})
                return
            match = re.fullmatch(
                r"/api/v1/snapshots/([^/]+)/archive/sw2014/catalog", route
            )
            if match:
                try:
                    legacy, summaries, _histories = app.sw2014_archive(
                        match.group(1), include_history=False
                    )
                    as_of_values = {row.get("as_of") for row in summaries}
                    if len(as_of_values) != 1:
                        raise ValueError("INVALID_SW2014_AS_OF")
                    self._send(
                        200,
                        {
                            "schema_version": "swivd-sw2014-catalog-v1",
                            "taxonomy": legacy["taxonomy"],
                            "level": legacy["level"],
                            "source_run_id": legacy["run_id"],
                            "as_of": next(iter(as_of_values)),
                            "industries": summaries,
                        },
                    )
                except (KeyError, OSError, ValueError):
                    self._send(404, {"error": "ARCHIVE_NOT_FOUND"})
                return
            match = re.fullmatch(
                r"/api/v1/snapshots/([^/]+)/archive/sw2014/industries/([^/]+)",
                route,
            )
            if match:
                run_id, code = match.groups()
                if not CODE_RE.fullmatch(code):
                    self._send(404, {"error": "ARCHIVE_INDUSTRY_NOT_FOUND"})
                    return
                try:
                    legacy, summaries, histories = app.sw2014_archive(run_id)
                    summary_by_code = {row["index_code"]: row for row in summaries}
                    if code not in summary_by_code:
                        raise KeyError(code)
                    self._send(
                        200,
                        {
                            "schema_version": "swivd-sw2014-industry-v1",
                            "taxonomy": legacy["taxonomy"],
                            "level": legacy["level"],
                            "source_run_id": legacy["run_id"],
                            "as_of": summary_by_code[code]["as_of"],
                            "index_code": code,
                            "summary": summary_by_code[code],
                            "history": [
                                row for row in histories if row["index_code"] == code
                            ],
                        },
                    )
                except (KeyError, OSError, ValueError):
                    self._send(404, {"error": "ARCHIVE_INDUSTRY_NOT_FOUND"})
                return
            match = re.fullmatch(r"/api/v1/snapshots/([^/]+)/industries/(L[123])/([^/]+)", route)
            if match:
                run_id, level, code = match.groups()
                try:
                    catalog = app.ui_catalog(run_id)
                    entry = next(
                        row
                        for row in catalog["industries"]
                        if row["level"] == level and row["index_code"] == code
                    )
                    self._send(200, app.verified_json(run_id, f"ui/{entry['shard']}"))
                except (KeyError, OSError, StopIteration, TypeError, ValueError):
                    self._send(404, {"error": "INDUSTRY_NOT_FOUND"})
                return
            self._send(404, {"error": "NOT_FOUND"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok():
                self._send(421, {"error": "INVALID_HOST"})
                return
            if self.headers.get("Origin") not in app.allowed_origins:
                self._send(403, {"error": "INVALID_ORIGIN"})
                return
            if self.headers.get("X-SWIVD-Nonce") != app.nonce:
                self._send(403, {"error": "INVALID_NONCE"})
                return
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                self._send(415, {"error": "JSON_REQUIRED"})
                return
            if urlsplit(self.path).path != "/api/v1/jobs":
                self._send(404, {"error": "NOT_FOUND"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 2 or length > 4096:
                    raise ValueError("INVALID_BODY_SIZE")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict) or set(payload) - {"kind", "as_of"}:
                    raise ValueError("INVALID_BODY")
                record = app.jobs.create(kind=payload.get("kind"), as_of=payload.get("as_of"))
                self._send(202, {"job_id": record["job_id"]})
            except JobConflict:
                self._send(409, {"error": "CONCURRENT_UPDATE"})
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, {"error": "INVALID_REQUEST"})

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._send(405, {"error": "CORS_DISABLED"})

    return Handler


def serve(*, project_root: Path, data_dir: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    app = LocalApp(project_root=project_root, data_dir=data_dir, host=host, port=port)
    server = LoopbackHTTPServer((host, port), _handler(app))
    if server.server_address[0] != "127.0.0.1":
        server.server_close()
        raise RuntimeError("NON_LOOPBACK_BIND_FORBIDDEN")
    print(json.dumps({"status": "SERVING", "url": f"http://127.0.0.1:{port}"}, ensure_ascii=False))
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


__all__ = ["LocalApp", "LoopbackHTTPServer", "serve"]
