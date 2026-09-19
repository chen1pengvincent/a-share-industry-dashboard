"""Read-only runtime evidence and the formal execution environment gate."""
from __future__ import annotations

import importlib.metadata
from pathlib import Path
import platform
import re
import sys

from swivd.runtime_environment import frozen_dependency_versions

from .models import DataError


SOURCE_ROOT = Path(__file__).resolve().parents[2]
_RELEASE_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)+(?:\.post[0-9]+)?")
_EXTENSION_PIN = re.compile(
    r"([A-Za-z0-9_.-]+)==([0-9]+(?:\.[0-9]+)+(?:\.post[0-9]+)?) --hash=sha256:[a-f0-9]{64}"
)


def _package_key(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _locked_dependencies(source_root: Path) -> dict[str, str]:
    pins = frozen_dependency_versions(source_root)
    names = {_package_key(name) for name in pins}
    if len(names) != len(pins):
        raise ValueError("DEPENDENCY_LOCK_INVALID")
    included = False
    for raw in (source_root / "requirements-workbench.lock").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == "-r requirements.lock" and not included:
            included = True
            continue
        match = _EXTENSION_PIN.fullmatch(line)
        if not match or _package_key(match[1]) in names:
            raise ValueError("DEPENDENCY_LOCK_INVALID")
        names.add(_package_key(match[1]))
        pins[match[1]] = match[2]
    if not included or "pypinyin" not in names:
        raise ValueError("DEPENDENCY_LOCK_INVALID")
    return pins


def _installed_release(name: str) -> str | None:
    try:
        actual = importlib.metadata.version(name)
    except (importlib.metadata.PackageNotFoundError, OSError, ValueError):
        return None
    # Arbitrary local-version metadata can contain paths or other private text.
    return actual if isinstance(actual, str) and _RELEASE_VERSION.fullmatch(actual) else None


def _dependencies(source_root: Path) -> dict:
    try:
        pins = _locked_dependencies(source_root)
    except (OSError, UnicodeError, ValueError):
        return {"lock_valid": False, "present": False, "locked": False, "packages": {}}
    packages = {}
    for name, expected in pins.items():
        actual = _installed_release(name)
        packages[name] = {"expected": expected, "actual": actual, "matched": actual == expected}
    return {"lock_valid": True,
            "present": all(item["actual"] is not None for item in packages.values()),
            "locked": all(item["matched"] for item in packages.values()),
            "packages": packages}


def dependency_status(source_root: Path = SOURCE_ROOT) -> dict:
    """Preserve the CLI/launcher boolean contract without collecting private values."""
    dependencies = _dependencies(source_root)
    return {"dependency_lock_valid": dependencies["lock_valid"],
            "dependencies_present": dependencies["present"],
            "dependencies_locked": dependencies["locked"],
            "dependency_matches": {name: item["matched"] for name, item in dependencies["packages"].items()}}


def _numeric_version(value: str) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value) else None


def environment_identity(source_root: Path = SOURCE_ROOT) -> dict:
    """Record versions only; never include hostname, user paths, env vars or tokens.

    Missing locks and unmatched dependencies are evidence, not exceptions here,
    so isolated development fixtures can record their actual limitations.
    """
    implementation = {"cpython": "CPython", "pypy": "PyPy"}.get(sys.implementation.name, "UNKNOWN")
    system = platform.system()
    system = system if system in {"Darwin", "Windows", "Linux"} else "UNKNOWN"
    architecture = platform.machine()
    architecture = architecture if architecture in {"arm64", "aarch64", "x86_64", "AMD64", "i386", "i686", "x86"} else "UNKNOWN"
    return {
        "schema_version": "industry-workbench-environment-v1",
        "python": {"implementation": implementation,
                   "version": ".".join(str(part) for part in sys.version_info[:3]),
                   "supported": implementation == "CPython" and (3, 11) <= sys.version_info[:2] < (3, 15)},
        "platform": {"system": system, "release": _numeric_version(platform.release()),
                     "architecture": architecture,
                     "macos_version": _numeric_version(platform.mac_ver()[0]) if system == "Darwin" else None},
        "dependencies": _dependencies(source_root),
    }


def require_supported_environment(source_root: Path = SOURCE_ROOT) -> dict:
    """Fail closed before a formal data job if the supported runtime is absent."""
    identity = environment_identity(source_root)
    if identity["platform"]["system"] != "Darwin":
        raise DataError("PLATFORM_NOT_SUPPORTED")
    if not identity["python"]["supported"]:
        raise DataError("PYTHON_NOT_SUPPORTED")
    if not identity["dependencies"]["lock_valid"]:
        raise DataError("DEPENDENCY_LOCK_INVALID")
    if not identity["dependencies"]["locked"]:
        raise DataError("DEPENDENCY_LOCK_MISMATCH")
    return identity
