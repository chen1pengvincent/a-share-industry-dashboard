"""Flat, self-contained raw evidence; immutable ancestor manifests stay verbatim."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

RUN = re.compile(r"SWIVD2-RUN-[0-9]{8}-[0-9]{3}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
LAYOUT = "FLAT_ANCESTOR_RAW_V1"
VERSIONS = {
    "swivd-local-snapshot-manifest-v3": ("swivd-project-spec-v4.2", "swivd-contract-v2.2.0", "GOV-20260901-004"),
    "swivd-local-snapshot-manifest-v4": ("swivd-project-spec-v4.3", "swivd-contract-v2.3.0", "GOV-20260906-001"),
}


def _relative(value: Any) -> str:
    if not isinstance(value, str) or "\\" in value:
        raise ValueError("LINEAGE_PATH_INVALID")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(p in {".", ".."} for p in value.split("/")):
        raise ValueError("LINEAGE_PATH_INVALID")
    if path.as_posix() != value:
        raise ValueError("LINEAGE_PATH_INVALID")
    return value


def flat_relative(owner: str, relative: str) -> str:
    """Resolve a legacy nested manifest path without recreating its directory depth."""
    if not RUN.fullmatch(owner):
        raise ValueError("LINEAGE_RUN_INVALID")
    parts = _relative(relative).split("/")
    while parts and parts[0] == "lineage":
        if len(parts) < 3 or not RUN.fullmatch(parts[1]):
            raise ValueError("LINEAGE_PATH_INVALID")
        owner, parts = parts[1], parts[2:]
    tail = "/".join(parts)
    if tail != "manifest.json" and not tail.startswith("inputs/raw/"):
        raise ValueError("LINEAGE_ARTIFACT_NOT_RAW")
    return f"lineage/{owner}/{tail}"


def _checked_bytes(path: Path, record: Mapping[str, Any]) -> bytes:
    if path.is_symlink() or any(p.is_symlink() for p in path.parents) or not path.is_file():
        raise ValueError("LINEAGE_FILE_INVALID")
    body = path.read_bytes()
    size, digest = record.get("bytes"), record.get("sha256")
    if (type(size) is not int or size < 0 or not isinstance(digest, str)
            or not SHA.fullmatch(digest) or len(body) != size
            or hashlib.sha256(body).hexdigest() != digest):
        raise ValueError("LINEAGE_ARTIFACT_IDENTITY_MISMATCH")
    return body


def copy_flat_lineage(parent_root: Path, run_root: Path, parent_manifest: Mapping[str, Any]) -> None:
    parent_root, run_root = parent_root.resolve(), run_root.resolve()
    owner = str(parent_manifest["run_id"])
    destinations: dict[str, tuple[int, str]] = {}

    def copy_one(relative: str, body: bytes) -> None:
        identity = (len(body), hashlib.sha256(body).hexdigest())
        if relative in destinations:
            if destinations[relative] != identity:
                raise ValueError("LINEAGE_COLLISION")
            return
        target = run_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as output:
            output.write(body)
        destinations[relative] = identity

    manifest_body = (parent_root / "manifest.json").read_bytes()
    if json.loads(manifest_body) != parent_manifest:
        raise ValueError("LINEAGE_PARENT_CHANGED")
    copy_one(flat_relative(owner, "manifest.json"), manifest_body)
    for record in parent_manifest["artifacts"]:
        relative = _relative(record["path"])
        if not relative.startswith(("inputs/raw/", "lineage/")):
            continue
        target = flat_relative(owner, relative)
        body = _checked_bytes(parent_root / relative, record)
        copy_one(target, body)


def validate_flat_lineage(root: Path, parent: Mapping[str, Any], *, provider_kind: str) -> None:
    """Bind every relocated byte to the unchanged manifests of the ancestor chain."""
    root = root.resolve()
    expected: dict[str, tuple[int, str]] = {}
    owned: set[str] = set()
    seen: set[str] = set()
    link: Mapping[str, Any] | None = parent
    while link is not None:
        owner = link.get("run_id")
        if not isinstance(owner, str) or not RUN.fullmatch(owner) or owner in seen:
            raise ValueError("LINEAGE_CYCLE_OR_IDENTITY_INVALID")
        seen.add(owner)
        if set(link) != {"run_id", "as_of", "manifest_sha256"} or owner.split("-")[2] != link["as_of"]:
            raise ValueError("LINEAGE_PARENT_INVALID")
        manifest_path = root / flat_relative(owner, "manifest.json")
        if not manifest_path.is_file():
            raise ValueError("LINEAGE_MANIFEST_MISSING")
        body = _checked_bytes(manifest_path, {"bytes": manifest_path.stat().st_size, "sha256": link["manifest_sha256"]})
        manifest = json.loads(body)
        if (manifest.get("run_id") != owner or manifest.get("as_of") != link["as_of"]
                or manifest.get("purpose") != "UPDATE_LATEST"
                or manifest.get("provider_kind") != provider_kind
                or manifest.get("schema_version") not in {"swivd-local-snapshot-manifest-v3", "swivd-local-snapshot-manifest-v4"}):
            raise ValueError("LINEAGE_MANIFEST_IDENTITY_MISMATCH")
        if (manifest.get("execution_status") != "COMPLETED"
                or not isinstance(manifest.get("validation"), dict)
                or manifest["validation"].get("status") != "PASS"
                or manifest.get("research_grade") != "RESEARCH_ONLY"
                or manifest.get("decision_eligible") is not False
                or manifest.get("production_approved") is not False):
            raise ValueError("LINEAGE_ANCESTOR_NOT_SUCCESSFUL_RESEARCH")
        if tuple(manifest.get(key) for key in ("spec_version", "contract_version", "decision_id")) != VERSIONS[manifest["schema_version"]]:
            raise ValueError("LINEAGE_ANCESTOR_VERSION_MISMATCH")
        if manifest["schema_version"].endswith("-v4") and manifest.get("lineage_layout") != LAYOUT:
            raise ValueError("LINEAGE_LAYOUT_MISMATCH")
        expected[flat_relative(owner, "manifest.json")] = (len(body), link["manifest_sha256"])
        owned.add(flat_relative(owner, "manifest.json"))
        records = manifest.get("artifacts")
        if not isinstance(records, list):
            raise ValueError("LINEAGE_ARTIFACTS_INVALID")
        local_seen: set[str] = set()
        for record in records:
            relative = _relative(record["path"])
            if relative in local_seen:
                raise ValueError("LINEAGE_DUPLICATE_ARTIFACT")
            local_seen.add(relative)
            if not relative.startswith(("inputs/raw/", "lineage/")):
                continue
            destination = flat_relative(owner, relative)
            if relative.startswith("inputs/raw/"):
                owned.add(destination)
            _checked_bytes(root / destination, record)
            identity = (record["bytes"], record["sha256"])
            if destination in expected and expected[destination] != identity:
                raise ValueError("LINEAGE_COLLISION")
            expected[destination] = identity
        link = manifest.get("parent")
        if link is not None and not isinstance(link, dict):
            raise ValueError("LINEAGE_PARENT_INVALID")
        if link is not None and (not isinstance(link.get("as_of"), str) or link["as_of"] >= manifest["as_of"]):
            raise ValueError("LINEAGE_PARENT_DATE_NOT_EARLIER")
    actual = {p.relative_to(root).as_posix() for p in (root / "lineage").rglob("*") if p.is_file()}
    if any(relative.split("/")[1] not in seen for relative in expected):
        raise ValueError("LINEAGE_UNRELATED_ANCESTOR")
    if set(expected) != owned:
        raise ValueError("LINEAGE_RAW_WITHOUT_OWNER")
    if actual != set(expected):
        raise ValueError("LINEAGE_FILE_SET_MISMATCH")
