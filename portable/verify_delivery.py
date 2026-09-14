#!/usr/bin/env python3
"""Independent, credential-free native verification of an extracted delivery.

The controller copies only the packaged static files to a system temporary path
containing Chinese text, spaces and shell metacharacters.  The packaged Python,
never a fallback system interpreter, performs the actual application checks.
This is native HTTP acceptance, not a browser, live-token or other-OS test.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile


class AcceptanceError(ValueError):
    """An intentionally credential-free failure code."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise AcceptanceError(code)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_link(path: Path) -> bool:
    """NTFS junctions must not escape the package or mutable data container."""

    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def safe_path(root: Path, value: object) -> Path:
    _require(isinstance(value, str) and bool(value), "PACKAGE_PATH_INVALID")
    assert isinstance(value, str)
    path = PurePosixPath(value)
    _require(
        not path.is_absolute()
        and "\\" not in value
        and ":" not in value
        and path.as_posix() == value
        and not any(part in {"", ".", ".."} for part in value.split("/")),
        "PACKAGE_PATH_INVALID",
    )
    target = root.joinpath(*path.parts)
    for candidate in (target, *target.parents):
        if candidate == root:
            break
        _require(not _is_link(candidate), "PACKAGE_SYMLINK_FORBIDDEN")
    return target


def verify_inventory(root: Path) -> dict[str, object]:
    _require(root.is_dir() and not _is_link(root), "PACKAGE_ROOT_INVALID")
    manifest_path = safe_path(root, "package-manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _require(isinstance(manifest, dict), "PACKAGE_MANIFEST_INVALID")
    records = manifest.get("files")
    _require(isinstance(records, list) and bool(records), "PACKAGE_FILES_INVALID")
    expected: set[str] = set()
    for record in records:
        _require(isinstance(record, dict), "PACKAGE_FILE_RECORD_INVALID")
        relative = record.get("path")
        target = safe_path(root, relative)
        _require(relative not in expected, "PACKAGE_FILE_DUPLICATE")
        assert isinstance(relative, str)
        _require(not relative.startswith("data/") and relative != "package-manifest.json", "PACKAGE_FILE_RESERVED")
        expected.add(relative)
        _require(type(record.get("bytes")) is int and record["bytes"] >= 0, "PACKAGE_SIZE_INVALID")
        _require(isinstance(record.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is not None, "PACKAGE_HASH_INVALID")
        _require(target.is_file(), "PACKAGE_FILE_MISSING")
        _require(target.stat().st_size == record["bytes"] and _sha(target) == record["sha256"], "PACKAGE_FILE_CHANGED")
    observed: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        _require(not _is_link(path), "PACKAGE_SYMLINK_FORBIDDEN")
        _require(
            not (path.name == ".DS_Store" and relative.startswith(("seed-data/runs/", "app/output/runs/"))),
            "SNAPSHOT_METADATA_UNEXPECTED",
        )
        if relative == "data" or relative.startswith("data/"):
            continue
        if path.is_file() and relative != "package-manifest.json" and path.name != ".DS_Store":
            observed.add(relative)
    _require(observed == expected, "PACKAGE_INVENTORY_MISMATCH")
    executable = safe_path(root, manifest.get("python_executable"))
    _require(executable.relative_to(root).as_posix() in expected, "PACKAGE_PYTHON_NOT_BOUND")
    _require(executable.is_file(), "PACKAGE_PYTHON_MISSING")
    return manifest


def network_guard(event: str, args: tuple[object, ...]) -> None:
    """Allow literal loopback only; DNS names and all external sockets fail."""

    address = None
    if event in {"socket.connect", "socket.bind", "socket.sendto"}:
        address = args[1] if len(args) > 1 else None
        if event == "socket.sendto":
            address = args[-1]
        if not isinstance(address, tuple) or not address:
            raise AcceptanceError("OFFLINE_NON_LOOPBACK_DENIED")
        address = address[0]
    elif event == "socket.getaddrinfo":
        address = args[0] if args else None
    elif event in {"socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo"}:
        raise AcceptanceError("OFFLINE_DNS_DENIED")
    else:
        return
    try:
        is_loopback = ipaddress.ip_address(str(address)).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise AcceptanceError("OFFLINE_NON_LOOPBACK_DENIED")


def compare_derived(original: Path, rebuilt: Path) -> int:
    def inventory(root: Path) -> dict[str, Path]:
        return {
            path.relative_to(root).as_posix(): path
            for branch in ("tables", "ui")
            for path in (root / branch).rglob("*")
            if path.is_file()
        }

    expected, observed = inventory(original), inventory(rebuilt)
    _require(bool(expected) and set(expected) == set(observed), "REBUILD_FILE_SET_MISMATCH")
    _require(all(_sha(expected[name]) == _sha(observed[name]) for name in expected), "REBUILD_BYTES_MISMATCH")
    return len(expected)


def seed_location(resolved: object) -> tuple[Path, dict[str, object]]:
    """resolve_pointer returns the run directory itself, not its manifest."""

    _require(isinstance(resolved, tuple) and len(resolved) == 2, "SEED_POINTER_MISSING")
    run_dir, pointer = resolved
    _require(isinstance(run_dir, Path) and isinstance(pointer, dict), "SEED_POINTER_INVALID")
    _require(run_dir.name == pointer.get("run_id") and run_dir.is_dir(), "SEED_POINTER_LOCATION_MISMATCH")
    return run_dir, pointer


def _worker(root: Path) -> dict[str, object]:
    _require(not _is_link(root), "PACKAGE_SYMLINK_FORBIDDEN")
    temporary_root = Path(tempfile.gettempdir()).resolve()
    root = root.resolve()
    _require(root != temporary_root and root.is_relative_to(temporary_root), "WORKER_REQUIRES_SYSTEM_TEMP_DIRECTORY")
    import contextlib
    import http.client
    from http.server import BaseHTTPRequestHandler
    import importlib.util
    import io
    import platform
    import threading
    import types

    # Applied before importing any application module.  No real SDK getter or
    # inherited credential is used, even if the receiving host has a token.
    os.environ.pop("TUSHARE_TOKEN", None)
    sys.addaudithook(network_guard)
    token_stub = types.ModuleType("tushare")
    token_stub.get_token = lambda: None
    sys.modules["tushare"] = token_stub
    manifest = verify_inventory(root)
    app_root = root / "app"
    sys.path.insert(0, str(root / "packages"))
    sys.path.insert(0, str(app_root / "src"))
    spec = importlib.util.spec_from_file_location("swivd_delivery_entry", app_root / "run_dashboard.py")
    _require(spec is not None and spec.loader is not None, "APP_ENTRY_MISSING")
    assert spec is not None and spec.loader is not None
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    from swivd.v2_pipeline import rebuild_v2
    from swivd.v2_server import LocalApp, LoopbackHTTPServer, _handler
    from swivd.v2_storage import resolve_pointer

    data_dir = root / "data" / "state"
    _require(not data_dir.exists(), "ACCEPTANCE_DATA_ALREADY_EXISTS")
    data_dir.parent.mkdir()
    shutil.copytree(root / "seed-data", data_dir)
    pointer_before = (data_dir / "latest_run.json").read_bytes()
    ledger_before = (data_dir / "run_ledger.ndjson").read_bytes()
    run_dir, pointer = seed_location(resolve_pointer(data_dir))
    run_id = str(pointer["run_id"])

    # rebuild_v2 starts with full independent validate_run_v2, including raw
    # replay and source closure; do not falsely count it as a hash-only check.
    rebuilt = root.parent / "断网 重建 &!"
    rebuild_result = rebuild_v2(run_dir=run_dir, output_dir=rebuilt)
    _require(rebuild_result.get("status") == "PASS", "REBUILD_NOT_PASS")
    rebuilt_count = compare_derived(run_dir, rebuilt)
    _require(rebuilt_count == 516, "FROZEN_SEED_DERIVED_COUNT_MISMATCH")

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        server = LoopbackHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        port = int(server.server_address[1])
        # A separate free ephemeral port is used for the doctor's bind probe.
        import socket
        with socket.socket() as port_probe:
            port_probe.bind(("127.0.0.1", 0))
            doctor_port = int(port_probe.getsockname()[1])
        doctor = entry._doctor(data_dir, "127.0.0.1", doctor_port)
        required_doctor = (
            "python_supported", "dependency_lock_valid", "dependencies_present",
            "dependencies_locked", "timezone_available", "legacy_archive_available",
            "data_dir_accessible", "data_dir_exists", "data_dir_writable",
            "loopback_host", "port_available",
        )
        _require(all(doctor.get(key) is True for key in required_doctor), "DOCTOR_ENVIRONMENT_NOT_READY")
        _require(doctor.get("token_configured") is False and doctor.get("token_source_type") == "NONE", "CREDENTIAL_STUB_FAILED")
        app = LocalApp(project_root=app_root, data_dir=data_dir, host="127.0.0.1", port=port)
        server.RequestHandlerClass = _handler(app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(route: str, *, as_json: bool = True):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=300)
            try:
                connection.request("GET", route)
                response = connection.getresponse()
                payload = response.read()
                _require(response.status == 200, "READ_ONLY_HTTP_FAILED")
                return json.loads(payload) if as_json else payload
            finally:
                connection.close()

        try:
            html = request("/", as_json=False)
            _require(b"<!doctype html>" in html.lower() and b"<html" in html.lower(), "HTML_SHELL_MISSING")
            current = request("/api/v1/snapshots/current")
            _require(current.get("run_id") == run_id, "HTTP_CURRENT_MISMATCH")
            catalog = request(f"/api/v1/snapshots/{run_id}/catalog")
            counts = {
                level: sum(row.get("level") == level for row in catalog["industries"])
                for level in ("L1", "L2", "L3")
            }
            _require(counts == {"L1": 31, "L2": 134, "L3": 346}, "HTTP_CATALOG_COUNTS_MISMATCH")
            first = next(row for row in catalog["industries"] if row["level"] == "L1" and row.get("is_pub") in {1, "1"})
            shard = request(f"/api/v1/snapshots/{run_id}/industries/L1/{first['index_code']}")
            _require(isinstance(shard, dict) and bool(shard.get("history")) and isinstance(shard.get("constituents"), list), "HTTP_INDUSTRY_SHARD_INVALID")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)
            _require(not thread.is_alive(), "HTTP_THREAD_NOT_STOPPED")

    _require((data_dir / "latest_run.json").read_bytes() == pointer_before, "CURRENT_POINTER_CHANGED")
    _require((data_dir / "run_ledger.ndjson").read_bytes() == ledger_before, "LEDGER_CHANGED")
    _require(not (data_dir / "transactions" / "publication.json").exists(), "PUBLICATION_TRANSACTION_CREATED")
    _require(not list((data_dir / "jobs").glob("*.json")), "JOB_CREATED")
    verify_inventory(root)
    return {
        "status": "PASS", "native_platform": platform.system(), "native_machine": platform.machine(),
        "python_version": platform.python_version(), "python_source": "PACKAGED_RUNTIME_ONLY",
        "network_policy": "LITERAL_LOOPBACK_ONLY_EXTERNAL_NETWORK_DENIED",
        "credential_policy": "NO_REAL_TOKEN_READ_SDK_GETTER_STUBBED",
        "package_static_files": len(manifest["files"]),
        "seed_run_id": run_id, "seed_manifest_sha256": _sha(run_dir / "manifest.json"),
        "full_seed_validation": "PASS", "offline_rebuild": "PASS",
        "byte_identical_derived_files": rebuilt_count,
        "migration_path": "CHINESE_SPACES_AMPERSAND_EXCLAMATION_SYSTEM_TEMP",
        "doctor": doctor, "http_level_counts": counts, "http_industry_shard": "PASS",
        "seed_pointer_and_ledger_unchanged": True, "http_service_stopped": True,
        "browser_e2e": "NOT_RUN", "live_token_e2e": "NOT_RUN",
        "windows_e2e": "NATIVE_OFFLINE_HTTP_PASS_BROWSER_AND_LIVE_UNVERIFIED" if sys.platform == "win32" else "WINDOWS_E2E_UNVERIFIED",
        "other_platforms": "NOT_VERIFIED_BY_THIS_HOST",
    }


def verify_delivery(package: Path, *, timeout: int = 1800) -> dict[str, object]:
    _require(not _is_link(package), "PACKAGE_SYMLINK_FORBIDDEN")
    original = package.resolve()
    verify_inventory(original)
    with tempfile.TemporaryDirectory(prefix="swivd-delivery-") as temporary:
        migrated = Path(temporary) / "迁移 中文 空格 &!"

        def ignore_state(directory: str, names: list[str]) -> set[str]:
            ignored = {name for name in names if name == ".DS_Store"}
            if Path(directory) == original and "data" in names:
                ignored.add("data")
            return ignored

        shutil.copytree(original, migrated, ignore=ignore_state)
        manifest = verify_inventory(migrated)
        executable = safe_path(migrated, manifest["python_executable"])
        environment = {key: value for key, value in os.environ.items() if key not in {"TUSHARE_TOKEN", "PYTHONPATH", "PYTHONHOME"}}
        completed = subprocess.run(
            [str(executable), "-I", "-B", "-X", "utf8", str(Path(__file__).resolve()), "--worker", str(migrated)],
            cwd=temporary, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        )
        try:
            result = json.loads(completed.stdout)
        except (TypeError, ValueError) as exc:
            raise AcceptanceError("PACKAGED_WORKER_OUTPUT_INVALID") from exc
        if completed.returncode != 0:
            safe_code = result.get("safe_error_code") if isinstance(result, dict) else None
            if isinstance(safe_code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]+", safe_code):
                raise AcceptanceError("PACKAGED_NATIVE_WORKER_FAILED_" + safe_code)
            raise AcceptanceError("PACKAGED_NATIVE_WORKER_FAILED")
        _require(isinstance(result, dict) and result.get("status") == "PASS", "PACKAGED_WORKER_NOT_PASS")
        verify_inventory(original)
        result["original_package_static_files_unchanged"] = True
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--package", type=Path)
    group.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=int, default=1800)
    arguments = parser.parse_args(argv)
    try:
        result = _worker(arguments.worker) if arguments.worker else verify_delivery(arguments.package, timeout=arguments.timeout)
    except Exception as exc:
        code = str(exc) if isinstance(exc, AcceptanceError) else type(exc).__name__
        print(json.dumps({"status": "FAIL", "safe_error_code": code, "windows_e2e": "WINDOWS_E2E_UNVERIFIED"}, ensure_ascii=True))
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
