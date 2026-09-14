#!/usr/bin/env python3
"""Offline-first launcher for the self-contained SWIVD distribution.

The manifest detects corruption; it is not a publisher signature. All writable
application state lives under data/state, separate from the verified payload.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import importlib.util
import io
import json
import os
import platform
import re
import shutil
import socket
import stat
import sys
import tempfile
import warnings
from pathlib import Path, PurePosixPath
from typing import Any


TARGETS = {"macos-arm64", "macos-x86_64", "windows-x86_64"}
SHA256 = re.compile(r"[0-9a-f]{64}")


class PortableError(Exception):
    """Only fixed, non-secret diagnostic codes cross the launcher boundary."""


def _error(code: str) -> None:
    raise PortableError(code)


def _is_link(path: Path) -> bool:
    # NTFS directory junctions are not reported by is_symlink().
    return path.is_symlink() or path.is_junction()


def _safe_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        _error("PACKAGE_PATH_INVALID")
    path = PurePosixPath(value)
    if (path.is_absolute() or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(ord(char) < 32 for char in value)):
        _error("PACKAGE_PATH_INVALID")
    if path.parts[0] == "data" or value == "package-manifest.json":
        _error("PACKAGE_PATH_INVALID")
    return value


def _regular_tree(root: Path, *, package: bool = False) -> dict[str, Path]:
    if _is_link(root) or not root.is_dir():
        _error("DIRECTORY_UNSAFE")
    files: dict[str, Path] = {}

    def walk_error(_exc: OSError) -> None:
        _error("DIRECTORY_UNREADABLE")

    for directory, names, filenames in os.walk(root, followlinks=False, onerror=walk_error):
        base = Path(directory)
        for name in list(names):
            path = base / name
            if _is_link(path):
                _error("SYMLINK_FORBIDDEN")
            if package and base == root and name == "data":
                names.remove(name)
        for name in filenames:
            path = base / name
            if _is_link(path):
                _error("SYMLINK_FORBIDDEN")
            if not stat.S_ISREG(path.stat().st_mode):
                _error("NON_REGULAR_FILE")
            relative = path.relative_to(root).as_posix()
            if package:
                if relative == "package-manifest.json":
                    continue
                # Ignore unbound Finder metadata outside immutable snapshots.
                # Within a run it changes the original exact-file inventory.
                if name == ".DS_Store":
                    if relative.startswith(("seed-data/runs/", "app/output/runs/")):
                        _error("SNAPSHOT_METADATA_UNEXPECTED")
                    continue
            files[relative] = path
    return files


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_package(root: Path) -> dict[str, Any]:
    """Verify every immutable file before importing project or wheel code."""
    manifest_path = root / "package-manifest.json"
    if _is_link(manifest_path) or not manifest_path.is_file():
        _error("PACKAGE_MANIFEST_MISSING")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        _error("PACKAGE_MANIFEST_INVALID")
    if (not isinstance(manifest, dict)
            or manifest.get("schema_version") != "swivd-portable-v1"
            or manifest.get("target") not in TARGETS
            or manifest.get("python_version") != "3.14.6"
            or not isinstance(manifest.get("files"), list)):
        _error("PACKAGE_MANIFEST_INVALID")
    expected: dict[str, dict[str, Any]] = {}
    for record in manifest["files"]:
        if not isinstance(record, dict) or set(record) != {"path", "bytes", "sha256"}:
            _error("PACKAGE_MANIFEST_INVALID")
        relative = _safe_path(record["path"])
        if (relative in expected or type(record["bytes"]) is not int or record["bytes"] < 0
                or not isinstance(record["sha256"], str) or SHA256.fullmatch(record["sha256"]) is None):
            _error("PACKAGE_MANIFEST_INVALID")
        expected[relative] = record
    required = {"launcher.py", "app/run_dashboard.py", "app/PROJECT_SPEC_V4.json", "seed-data/latest_run.json"}
    if not expected or not required <= expected.keys():
        _error("PACKAGE_REQUIRED_FILE_MISSING")
    actual = _regular_tree(root, package=True)
    if set(actual) != set(expected):
        _error("PACKAGE_INVENTORY_MISMATCH")
    for relative, path in actual.items():
        record = expected[relative]
        if path.stat().st_size != record["bytes"] or _sha256(path) != record["sha256"]:
            _error("PACKAGE_HASH_MISMATCH")
    return manifest


def verify_runtime(root: Path, manifest: dict[str, Any]) -> None:
    target = manifest["target"]
    system = platform.system()
    machine = platform.machine().lower()
    actual = {
        ("Darwin", "arm64"): "macos-arm64",
        ("Darwin", "aarch64"): "macos-arm64",
        ("Darwin", "x86_64"): "macos-x86_64",
        ("Windows", "amd64"): "windows-x86_64",
        ("Windows", "x86_64"): "windows-x86_64",
    }.get((system, machine))
    if actual != target:
        _error("PLATFORM_MISMATCH")
    if sys.implementation.name != "cpython" or sys.version_info[:3] != (3, 14, 6):
        _error("PYTHON_VERSION_MISMATCH")
    executable = root / ("runtime/python.exe" if target.startswith("windows") else "runtime/bin/python3.14")
    if Path(sys.executable).resolve() != executable.resolve():
        _error("BUNDLED_PYTHON_REQUIRED")
    if not sys.flags.isolated or not sys.dont_write_bytecode:
        _error("ISOLATED_PYTHON_REQUIRED")


def _load_app(root: Path) -> Any:
    # Explicit paths also work with Windows' isolated python314._pth runtime.
    for path in (root / "packages", root / "app" / "src"):
        if not path.is_dir() or _is_link(path):
            _error("PACKAGE_REQUIRED_DIRECTORY_MISSING")
        sys.path.insert(0, str(path))
    spec = importlib.util.spec_from_file_location("_swivd_portable_app", root / "app" / "run_dashboard.py")
    if spec is None or spec.loader is None:
        _error("APP_ENTRYPOINT_UNAVAILABLE")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    return entry


def validate_state(data_dir: Path) -> dict[str, Any]:
    """Read-only verification of current and explicitly indexed history."""
    from swivd.v2_storage import resolve_pointer
    from swivd.v2_validator import validate_run_v2

    _regular_tree(data_dir)
    if (data_dir / "transactions" / "publication.json").exists():
        _error("DATA_RECOVERY_REQUIRED")
    try:
        current = resolve_pointer(data_dir)
        if current is None:
            _error("DATA_CURRENT_MISSING")
        validate_run_v2(current[0])
        checked = {current[0]}
        index_path = data_dir / "historical_index.json"
        if index_path.exists():
            index = json.loads(index_path.read_text(encoding="utf-8"))
            if not isinstance(index, dict) or not isinstance(index.get("dates"), dict):
                _error("DATA_HISTORY_INVALID")
            for date in index["dates"]:
                historical = resolve_pointer(data_dir, historical_date=date)
                if historical is None:
                    _error("DATA_HISTORY_INVALID")
                if historical[0] not in checked:
                    validate_run_v2(historical[0])
                    checked.add(historical[0])
        return {"state_verified": True, "as_of": current[1]["as_of"], "snapshots_verified": len(checked)}
    except PortableError:
        raise
    except Exception:
        _error("DATA_VALIDATION_FAILED")


@contextlib.contextmanager
def _import_lock(container: Path):
    path = container / ".import.lock"
    if _is_link(path):
        _error("SYMLINK_FORBIDDEN")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    handle = os.fdopen(fd, "r+b")
    locked = False
    try:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            _error("DATA_LOCK_INVALID")
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            _error("DATA_IMPORT_BUSY")
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _recover_state(state: Path) -> None:
    from swivd.v2_storage import SingleWriterLock, recover_publication

    _regular_tree(state)
    try:
        with SingleWriterLock(state):
            recover_publication(state)
    except Exception:
        _error("DATA_BUSY_OR_RECOVERY_FAILED")


def prepare_data(root: Path) -> Path:
    """Import only into an absent state directory; never merge or overwrite."""
    container = root / "data"
    if _is_link(container) or (container.exists() and not container.is_dir()):
        _error("DATA_CONTAINER_UNSAFE")
    container.mkdir(exist_ok=True)
    with _import_lock(container):
        for path in container.iterdir():
            if _is_link(path):
                _error("SYMLINK_FORBIDDEN")
            if path.name not in {"state", ".import.lock"} and not path.name.startswith(".import-"):
                _error("DATA_CONTAINER_UNRECOGNIZED")
        state = container / "state"
        if state.exists():
            if not state.is_dir():
                _error("DATA_STATE_UNSAFE")
            _recover_state(state)
            validate_state(state)
            return state
        # A failed/interrupted staging copy remains evidence but is never used.
        staging = Path(tempfile.mkdtemp(prefix=".import-", dir=container))
        try:
            shutil.copytree(root / "seed-data", staging, dirs_exist_ok=True, symlinks=True,
                            ignore=shutil.ignore_patterns(".DS_Store"))
            validate_state(staging)
            if state.exists() or _is_link(state):
                _error("DATA_IMPORT_CONFLICT")
            staging.rename(state)
        except PortableError:
            raise
        except Exception:
            _error("DATA_IMPORT_FAILED")
        return state


def _check_token(token: Any) -> str:
    if not isinstance(token, str):
        _error("TOKEN_INVALID")
    if any(character in token for character in ("\r", "\n", "\x00")):
        _error("TOKEN_INVALID")
    return token.strip()


def configure_token(*, interactive: bool) -> str:
    """ENV → silent local SDK lookup → optional non-echo terminal input."""
    token = _check_token(os.environ.get("TUSHARE_TOKEN", ""))
    source = "ENVIRONMENT" if token else "NONE"
    if not token:
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                import tushare
                local = tushare.get_token()
            token = _check_token(local or "")
        except PortableError:
            raise
        except Exception:
            _error("TOKEN_LOOKUP_FAILED")
        if token:
            source = "TUSHARE_LOOKUP"
    if not token and interactive:
        if not sys.stdin.isatty() or not sys.stderr.isatty():
            _error("TOKEN_TERMINAL_REQUIRED")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                token = _check_token(getpass.getpass("Tushare token（输入不显示；直接回车只浏览已有数据）："))
        except (getpass.GetPassWarning, EOFError):
            _error("TOKEN_TERMINAL_REQUIRED")
        if token:
            source = "SESSION_INPUT"
    if token:
        os.environ["TUSHARE_TOKEN"] = token
    return source


def _check_port(port: int) -> None:
    if not 1 <= port <= 65535:
        _error("INVALID_PORT")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    except OSError:
        _error("PORT_UNAVAILABLE")


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        _error("INVALID_ARGUMENTS")


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    parser = _Parser(description="申万估值仪表盘便携版：无需安装 Python，不保存 token")
    parser.add_argument("command", nargs="?", choices=("serve", "verify", "doctor", "self-test"), default="serve")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.command == "serve":
        print("正在校验便携包和冻结数据，请稍候...", flush=True)
    package_root = root if root is not None else Path(__file__).absolute().parent
    if _is_link(package_root):
        _error("SYMLINK_FORBIDDEN")
    package_root = package_root.resolve()
    manifest = verify_package(package_root)
    verify_runtime(package_root, manifest)
    entry = _load_app(package_root)
    seed = validate_state(package_root / "seed-data")
    state = package_root / "data" / "state"
    if args.command in {"verify", "self-test"}:
        result: dict[str, Any] = {"package_verified": True, "target": manifest["target"], "seed": seed}
        if state.exists() or _is_link(state):
            result["data"] = validate_state(state)
        if args.command == "self-test":
            result["doctor"] = entry._doctor(state, "127.0.0.1", args.port)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "doctor":
        result = entry._doctor(state, "127.0.0.1", args.port)
        if state.exists() or _is_link(state):
            result["data_verified"] = bool(validate_state(state)["state_verified"])
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    _check_port(args.port)
    source = configure_token(interactive=True)
    state = prepare_data(package_root)
    print("仅本机运行。请手动在浏览器打开 http://127.0.0.1:" + str(args.port))
    print("停止：在本终端按 Ctrl+C；迁移前请先等待更新完成，再停止服务。")
    if source == "NONE":
        print("未配置 token：可以浏览已有数据，更新需要重启后输入有效 token。")
    return entry.main(["serve", "--data-dir", str(state), "--host", "127.0.0.1", "--port", str(args.port)])


def cli(argv: list[str] | None = None) -> int:
    try:
        return main(argv)
    except KeyboardInterrupt:
        print("INTERRUPTED", file=sys.stderr)
        return 130
    except PortableError as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1
    except Exception:
        # No traceback, arbitrary SDK message, path or input is user-visible.
        print("ERROR: PORTABLE_OPERATION_FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
