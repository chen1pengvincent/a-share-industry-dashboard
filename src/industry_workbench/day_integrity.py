"""Read-only semantic binding for frozen daily inputs, results and capture code."""
from __future__ import annotations

import hashlib
import io
import re
import tarfile

from .models import DataError, json_bytes, parse_date

INPUT_CONTRACT = "industry-workbench-day-input-v1"


def _capture_source(store, capture, verified_sources):
    try:
        identity = capture["identity"]
        inventory_ref, snapshot_ref = capture["inventory"], capture["snapshot"]
        if inventory_ref["kind"] != "source_identity" or snapshot_ref["kind"] != "source_snapshot_tar_gz":
            raise ValueError()
        inventory = store.read_json(inventory_ref)
        files = inventory["files"]
        if (not isinstance(files, dict) or not files
                or any(not isinstance(name, str) or not isinstance(digest, str)
                       or not re.fullmatch(r"[a-f0-9]{64}", digest) for name, digest in files.items())
                or hashlib.sha256(json_bytes(files)).hexdigest() != inventory["tree_sha256"]
                or any(identity[key] != inventory[key] for key in ("commit", "git_dirty", "tree_sha256"))):
            raise ValueError()
        key = (inventory_ref["sha256"], json_bytes(snapshot_ref))
        if key not in verified_sources:
            observed = {}
            with tarfile.open(fileobj=io.BytesIO(store.read_bytes(snapshot_ref)), mode="r:gz") as archive:
                for member in archive:
                    if not member.isfile() or member.name not in files or member.name in observed:
                        raise ValueError()
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError()
                    observed[member.name] = hashlib.sha256(stream.read()).hexdigest()
            if observed != files:
                raise ValueError()
            verified_sources.add(key)
    except DataError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, OSError, EOFError, tarfile.TarError):
        raise DataError("DAY_CAPTURE_SOURCE_INVALID") from None


def validate_day_refs(store, day, refs, *, expected_result_source_sha256=None, verified_sources=None):
    """Return (input, result) only when the date, contract and sources are bound.

    Capture source may be older than result source after an approved recompute.
    The expected result source is the enclosing checkpoint/manifest's frozen
    source, never the currently running checkout. No files or refs are changed.
    """
    parse_date(day)
    if not isinstance(refs, dict) or refs.get("input_contract_version") != INPUT_CONTRACT:
        raise DataError("DAY_INPUT_CONTRACT_MISMATCH")
    try:
        if refs["input"]["kind"] != "day_input" or refs["result"]["kind"] != "day_result":
            raise ValueError()
        result_source = refs["result_source_sha256"]
        if (not isinstance(result_source, str) or not re.fullmatch(r"[a-f0-9]{64}", result_source)
                or expected_result_source_sha256 is not None and result_source != expected_result_source_sha256):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise DataError("DAY_RESULT_SOURCE_MISMATCH") from None
    inputs, result = store.read_json(refs["input"]), store.read_json(refs["result"])
    if (not isinstance(inputs, dict) or not isinstance(result, dict)
            or inputs.get("trade_date") != day or result.get("trade_date") != day):
        raise DataError("DAY_DATE_MISMATCH")
    for row in result.get("industries", []):
        for metric in row.get("metrics", {}).values():
            if isinstance(metric, dict) and "metric_date" in metric and metric["metric_date"] != day:
                raise DataError("DAY_METRIC_DATE_MISMATCH")
    _capture_source(store, refs.get("input_source"), verified_sources if verified_sources is not None else set())
    if not store.development:
        capture = refs["input_source"]["identity"]
        commit = capture.get("commit")
        if (inputs.get("provider_kind") != "LIVE_SECURE_TUSHARE" or capture.get("git_dirty") is not False
                or not isinstance(commit, str) or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", commit)):
            raise DataError("DEVELOPMENT_INPUT_IN_FORMAL_ROOT")
    return inputs, result
