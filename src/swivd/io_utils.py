"""Deterministic, token-safe file helpers for SWIVD."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


RUN_RE = re.compile(r"^SWIVD-RUN-(\d{8})-(\d{3})$")


def fsync_directory(path: str | Path) -> None:
    """Persist directory-entry changes on platforms that expose directory fsync."""

    directory = Path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(directory, flags)
    except OSError:
        if os.name == "nt":
            # Windows directory handles require platform-specific backup
            # semantics; real Windows durability remains an explicit E2E gate.
            return
        raise
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_unlink(path: str | Path, *, missing_ok: bool = False) -> None:
    """Unlink one file and fsync its parent so deletion survives a crash."""

    target = Path(path)
    try:
        target.unlink()
    except FileNotFoundError:
        if missing_ok:
            fsync_directory(target.parent)
            return
        raise
    fsync_directory(target.parent)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        fsync_directory(target.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: str | Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def write_json(path: str | Path, value: Any, *, pretty: bool = True) -> None:
    if pretty:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    else:
        text = canonical_json(value) + "\n"
    atomic_write_text(path, text)


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_csv(
    path: str | Path,
    rows: Iterable[Mapping[str, Any]],
    fieldnames: Sequence[str],
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="raise")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        fsync_directory(target.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def read_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def allocate_run_dir(output_root: str | Path, as_of: str) -> tuple[str, Path]:
    root = Path(output_root)
    runs = root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    used = []
    for item in runs.iterdir():
        match = RUN_RE.fullmatch(item.name)
        if item.is_dir() and match and match.group(1) == as_of:
            used.append(int(match.group(2)))
    sequence = max(used, default=0) + 1
    if sequence > 999:
        raise RuntimeError(f"run sequence exhausted for {as_of}")
    run_id = f"SWIVD-RUN-{as_of}-{sequence:03d}"
    run_dir = runs / run_id
    run_dir.mkdir(parents=False, exist_ok=False)
    return run_id, run_dir


def append_ndjson(path: str | Path, record: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    line = canonical_json(dict(record)) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(target, flags, 0o600)
    try:
        remaining = memoryview(line.encode("utf-8"))
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("append made no progress")
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    if not existed:
        fsync_directory(target.parent)


def inventory_files(root: str | Path, *, excluded: set[str] | None = None) -> list[dict[str, Any]]:
    base = Path(root)
    skip = excluded or set()
    result: list[dict[str, Any]] = []
    for path in sorted(p for p in base.rglob("*") if p.is_file()):
        relative = path.relative_to(base).as_posix()
        if relative in skip:
            continue
        result.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return result


def write_sha256sums(run_dir: str | Path) -> None:
    base = Path(run_dir)
    entries = inventory_files(base, excluded={"SHA256SUMS"})
    lines = [f"{item['sha256']}  {item['path']}" for item in entries]
    atomic_write_text(base / "SHA256SUMS", "\n".join(lines) + "\n")
