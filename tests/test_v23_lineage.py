"""Continuous-update evidence and migration regressions; system temp only."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from swivd.v2_lineage import copy_flat_lineage, flat_relative, validate_flat_lineage
from swivd.v2_storage import inventory_artifacts


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def link(run):
    manifest = json.loads((run / "manifest.json").read_text())
    return {"run_id": run.name, "as_of": manifest["as_of"], "manifest_sha256": hashlib.sha256((run / "manifest.json").read_bytes()).hexdigest()}


def fixture(root, number, parent=None, legacy=False):
    as_of = (date(2021, 12, 13) + timedelta(days=number)).strftime("%Y%m%d")
    run = root / f"SWIVD2-RUN-{as_of}-001"
    dump(run / "inputs/raw/sw_daily/L1/810001_SI.json", {"observation": number})
    if parent is not None:
        parent_manifest = json.loads((parent / "manifest.json").read_text())
        if legacy:
            target = run / "lineage" / parent.name
            target.mkdir(parents=True)
            shutil.copyfile(parent / "manifest.json", target / "manifest.json")
            shutil.copytree(parent / "inputs/raw", target / "inputs/raw")
            if (parent / "lineage").exists():
                shutil.copytree(parent / "lineage", target / "lineage")
        else:
            copy_flat_lineage(parent, run, parent_manifest)
    dump(run / "manifest.json", {
        "schema_version": f"swivd-local-snapshot-manifest-v{3 if legacy else 4}",
        "spec_version": "swivd-project-spec-v4.2" if legacy else "swivd-project-spec-v4.3",
        "contract_version": "swivd-contract-v2.2.0" if legacy else "swivd-contract-v2.3.0",
        "decision_id": "GOV-20260901-004" if legacy else "GOV-20260906-001",
        "lineage_layout": "FLAT_ANCESTOR_RAW_V1",
        "execution_status": "COMPLETED", "validation": {"status": "PASS"},
        "research_grade": "RESEARCH_ONLY", "decision_eligible": False, "production_approved": False,
        "run_id": run.name, "as_of": as_of, "purpose": "UPDATE_LATEST",
        "provider_kind": "TEST_INJECTED_CLIENT", "parent": link(parent) if parent else None,
        "artifacts": inventory_artifacts(run),
    })
    return run


class FlatLineageTests(unittest.TestCase):
    def test_forty_successors_have_constant_depth_and_self_contained_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "中文 空格迁移"
            parent = fixture(root, 0)
            for number in range(1, 41):
                run = fixture(root, number, parent)
                validate_flat_lineage(run, link(parent), provider_kind="TEST_INJECTED_CLIENT")
                paths = [p.relative_to(run).as_posix() for p in (run / "lineage").rglob("*") if p.is_file()]
                self.assertTrue(all(p.count("lineage/") == 1 for p in paths))
                self.assertEqual(len(paths), number * 2)
                self.assertLess(max(map(len, paths)), 100)
                parent = run
            moved = Path(temporary) / "另一个 迁移位置" / run.name
            shutil.copytree(run, moved)
            validate_flat_lineage(moved, json.loads((moved / "manifest.json").read_text())["parent"], provider_kind="TEST_INJECTED_CLIENT")

    def test_legacy_nested_evidence_is_flattened_without_rewriting_manifests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = fixture(root, 0, legacy=True)
            second = fixture(root, 1, first, legacy=True)
            third = fixture(root, 2, second, legacy=True)
            new = fixture(root, 3, third)
            validate_flat_lineage(new, link(third), provider_kind="TEST_INJECTED_CLIENT")
            for ancestor in (first, second, third):
                self.assertEqual((ancestor / "manifest.json").read_bytes(), (new / "lineage" / ancestor.name / "manifest.json").read_bytes())
            self.assertFalse((new / "lineage" / third.name / "lineage").exists())

    def test_tamper_extra_file_and_provider_mismatch_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = fixture(root, 0)
            child = fixture(root, 1, parent)
            with self.assertRaises(ValueError):
                validate_flat_lineage(child, link(parent), provider_kind="LIVE_SECURE_TUSHARE")
            extra = child / "lineage" / parent.name / "extra.json"
            dump(extra, {})
            with self.assertRaisesRegex(ValueError, "FILE_SET"):
                validate_flat_lineage(child, link(parent), provider_kind="TEST_INJECTED_CLIENT")
            extra.unlink()
            raw = child / "lineage" / parent.name / "inputs/raw/sw_daily/L1/810001_SI.json"
            dump(raw, {"observation": 999})
            with self.assertRaisesRegex(ValueError, "IDENTITY_MISMATCH"):
                validate_flat_lineage(child, link(parent), provider_kind="TEST_INJECTED_CLIENT")

    def test_paths_are_not_general_directory_traversal(self):
        for path in ("../raw.json", "/inputs/raw/x", "inputs/raw/../x", "lineage/evil/manifest.json", "inputs/raw//x", "inputs\\raw\\x"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                flat_relative("SWIVD2-RUN-20211213-001", path)


if __name__ == "__main__":
    unittest.main()
