"""Adapter tests use a synthetic manifest and an isolated validator seam.

These tests verify routing, no writes, and hash checks, not real SWIVD replay.
Real imported-snapshot compatibility is a separate acceptance requirement.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from industry_workbench.legacy import LegacySnapshotReader


RUN = "SWIVD2-RUN-20260901-001"


class LegacyReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workbench-legacy-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / RUN

    def fixture(self):
        self.root.mkdir()
        (self.root / "ui").mkdir()
        catalog = {"schema_version": "swivd-ui-catalog-v3", "as_of": "20260901", "industries": [], "levels": ["L1", "L2", "L3"]}
        body = json.dumps(catalog).encode()
        (self.root / "ui/catalog.json").write_bytes(body)
        self.manifest = {"run_id": RUN, "as_of": "20260901", "artifacts": [{"path": "ui/catalog.json", "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}]}
        (self.root / "manifest.json").write_text(json.dumps(self.manifest))
        return catalog

    def inventory(self):
        return {str(path.relative_to(self.base)): path.read_bytes() for path in self.base.rglob("*") if path.is_file()}

    def test_missing_import_is_optional_and_creates_nothing(self):
        reader = LegacySnapshotReader(self.root)
        self.assertEqual(reader.status()["state"], "NOT_INSTALLED")
        self.assertTrue(reader.bootstrap()["read_only"])
        self.assertFalse(self.root.exists())
        self.assertEqual(list(self.base.iterdir()), [])

    def test_direct_snapshot_reads_without_jobs_or_writes(self):
        catalog = self.fixture()
        before = self.inventory()
        with patch("industry_workbench.legacy.validate_run_v2", return_value=self.manifest) as validate, patch("swivd.v2_jobs.JobManager.__init__", side_effect=AssertionError("JobManager must not be created")):
            reader = LegacySnapshotReader(self.root)
            self.assertEqual(reader.status()["state"], "AVAILABLE")
            self.assertEqual(reader.bootstrap()["current_run_id"], RUN)
            self.assertEqual(reader.get_json(f"/api/v1/snapshots/{RUN}/catalog"), (200, catalog))
            self.assertEqual(reader.get_json(f"/legacy/api/v1/snapshots/{RUN}/catalog"), (200, catalog))
            validate.assert_called_once_with(self.root)
        self.assertEqual(before, self.inventory())

    def test_served_artifact_is_rechecked_after_initial_validation(self):
        self.fixture()
        with patch("industry_workbench.legacy.validate_run_v2", return_value=self.manifest):
            reader = LegacySnapshotReader(self.root)
            self.assertEqual(reader.get_json(f"/api/v1/snapshots/{RUN}/catalog")[0], 200)
            (self.root / "ui/catalog.json").write_bytes(b'{"tampered":true}')
            self.assertEqual(reader.get_json(f"/api/v1/snapshots/{RUN}/catalog"), (404, {"error": "LEGACY_SNAPSHOT_UNAVAILABLE"}))

    def test_validator_failure_is_not_softened_or_exposed(self):
        self.fixture()
        with patch("industry_workbench.legacy.validate_run_v2", side_effect=ValueError("SECRET_CANARY_PRIVATE_DETAIL")):
            reader = LegacySnapshotReader(self.root)
            state = reader.status()
            self.assertEqual(state["state"], "INVALID")
            self.assertNotIn("SECRET_CANARY", json.dumps(state))
            self.assertIsNone(reader.bootstrap()["current_run_id"])

    def test_path_traversal_and_unknown_routes_have_no_reader_escape(self):
        self.fixture()
        with patch("industry_workbench.legacy.validate_run_v2", return_value=self.manifest):
            reader = LegacySnapshotReader(self.root)
            with self.assertRaisesRegex(ValueError, "PATH_INVALID"):
                reader.verified_bytes(RUN, "../outside")
            self.assertIsNone(reader.get_json("/api/v1/../../manifest.json"))
            self.assertEqual(reader.get_json("/api/v1/snapshots/other/catalog")[0], 404)

    def test_job_routes_never_create_or_recover_a_job(self):
        reader = LegacySnapshotReader(self.root)
        self.assertEqual(reader.get_json("/api/v1/jobs/active"), (200, {"jobs": []}))
        self.assertEqual(reader.get_json("/api/v1/jobs/something"), (404, {"error": "READ_ONLY_ARCHIVE"}))
        self.assertIsNone(reader.get_json("/api/v1/jobs"))
        self.assertFalse(self.root.exists())

    def test_manifest_change_is_validated_again(self):
        self.fixture()
        with patch("industry_workbench.legacy.validate_run_v2", return_value=self.manifest) as validate:
            reader = LegacySnapshotReader(self.root)
            reader.status()
            (self.root / "manifest.json").write_text(json.dumps({**self.manifest, "altered": True}))
            reader.status()
            self.assertEqual(validate.call_count, 2)


if __name__ == "__main__":
    unittest.main()
