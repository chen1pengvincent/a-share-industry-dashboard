"""Read-only, privacy-safe environment diagnostics and runtime evidence."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_VERSION_PATTERN = r"[0-9]+(?:\.[0-9]+)+(?:\.post[0-9]+)?"
_RELEASE_VERSION = re.compile(_VERSION_PATTERN)
_LOCK_PIN = re.compile(r"([a-z0-9][a-z0-9._-]*)==(" + _VERSION_PATTERN + r")(?:\s+\\)?")
_LOCK_HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}(?:\s+\\)?")


def frozen_dependency_versions(project_root: Path = PROJECT_ROOT) -> dict[str, str]:
    """Read only exact release pins from the project's single cross-platform lock."""

    pins: dict[str, str] = {}
    for raw_line in (project_root / "requirements.lock").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line in {"--require-hashes", "--only-binary=:all:"}:
            continue
        if _LOCK_HASH.fullmatch(line):
            continue
        match = _LOCK_PIN.fullmatch(line)
        if match is None or match.group(1) in pins:
            raise ValueError("DEPENDENCY_LOCK_INVALID")
        pins[match.group(1)] = match.group(2)
    if not pins or not {"certifi", "tushare", "tzdata"} <= pins.keys():
        raise ValueError("DEPENDENCY_LOCK_INVALID")
    return pins


def _installed_release(package: str) -> str | None:
    try:
        value = importlib.metadata.version(package)
    except (importlib.metadata.PackageNotFoundError, OSError, ValueError):
        return None
    # Metadata may contain arbitrary local version labels. Never persist them.
    return value if isinstance(value, str) and _RELEASE_VERSION.fullmatch(value) else None


def dependency_status(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    try:
        pins = frozen_dependency_versions(project_root)
    except (OSError, UnicodeError, ValueError):
        return {
            "dependency_lock_valid": False,
            "dependencies_present": False,
            "dependencies_locked": False,
            "dependency_matches": {},
        }
    installed = {package: _installed_release(package) for package in pins}
    matches = {package: installed[package] == expected for package, expected in pins.items()}
    return {
        "dependency_lock_valid": True,
        "dependencies_present": all(version is not None for version in installed.values()),
        "dependencies_locked": all(matches.values()),
        "dependency_matches": matches,
    }


def timezone_available() -> bool:
    try:
        ZoneInfo("Asia/Shanghai")
    except (ZoneInfoNotFoundError, OSError, ValueError):
        return False
    return True


def legacy_archive_available(project_root: Path = PROJECT_ROOT) -> bool:
    """Check the exact immutable source files required to build a new snapshot.

    This is not a validation or promotion of the complete historical v1 run.
    No copy, cache, data-root initialization or provider is involved.
    """

    from .v2_validator import LEGACY_MANIFEST_SHA256, LEGACY_RUN_ID, LEGACY_SELECTED_FILES

    source = project_root / "output" / "runs" / LEGACY_RUN_ID
    try:
        for directory in (project_root / "output", source.parent, source):
            if directory.is_symlink() or not directory.is_dir():
                return False
        manifest_path = source / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            return False
        manifest_bytes = manifest_path.read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != LEGACY_MANIFEST_SHA256:
            return False
        manifest = json.loads(manifest_bytes)
        if not isinstance(manifest, dict) or manifest.get("run_id") != LEGACY_RUN_ID:
            return False
        records = manifest.get("artifacts")
        if not isinstance(records, list):
            return False
        by_path = {}
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                return False
            if record["path"] in by_path:
                return False
            by_path[record["path"]] = record
        for relative in LEGACY_SELECTED_FILES:
            target = source / relative
            for path in (target, *target.parents):
                if path == source:
                    break
                if path.is_symlink():
                    return False
            if not target.is_file():
                return False
            if relative == "manifest.json":
                continue
            record = by_path.get(relative)
            if not isinstance(record, dict):
                return False
            payload = target.read_bytes()
            if type(record.get("bytes")) is not int or len(payload) != record["bytes"]:
                return False
            if hashlib.sha256(payload).hexdigest() != record.get("sha256"):
                return False
    except (OSError, UnicodeError, ValueError):
        return False
    return True


def safe_runtime_record(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Return version evidence without host names, paths, environment values or tokens."""

    status = dependency_status(project_root)
    packages = status["dependency_matches"]
    implementation = sys.implementation.name
    return {
        "python_implementation": {"cpython": "CPython", "pypy": "PyPy"}.get(implementation, "UNKNOWN"),
        "python_version": ".".join(str(part) for part in sys.version_info[:3]),
        "platform": {"darwin": "Darwin", "win32": "Windows", "linux": "Linux"}.get(sys.platform, "UNKNOWN"),
        "path_policy": "RELATIVE_RUN_PATHS",
        "windows_e2e": "UNVERIFIED",
        "dependency_versions": {package: _installed_release(package) for package in packages},
        "dependencies_locked": status["dependencies_locked"],
        "timezone_available": timezone_available(),
    }
