"""Source closure excludes review archives; security checks remain unchanged."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from swivd.io_utils import sha256_file
from swivd.v2_pipeline import V2PipelineError, _copy_source_snapshot
from swivd.v2_validator import REQUIRED_SOURCE_FILES_V3, SECRET_VALUE


DOCS = {
    "docs/architecture.md",
    "docs/data_dictionary.md",
    "docs/runbook.md",
    "docs/troubleshooting.md",
}
ROOT_FILES = {
    "PROJECT_CONTRACT.md", "PROJECT_CONTRACT_V2.md", "PROJECT_SPEC_V4.json",
    "AGENTS.md", "README.md", "pyproject.toml", "requirements.lock",
    "run_dashboard.py", "start_macos.command", "start_windows.cmd",
}


def project_fixture(root: Path) -> Path:
    project = root / "project"
    for relative in sorted(ROOT_FILES | DOCS):
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture for {relative}\n", encoding="utf-8")
    return project


class SourceSnapshotTests(unittest.TestCase):
    def test_only_required_docs_are_frozen_with_exact_bytes(self):
        self.assertEqual({p for p in REQUIRED_SOURCE_FILES_V3 if p.startswith("docs/")}, DOCS)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = project_fixture(root)
            marker = "tushare_" + "token=" + "PUBLIC_FAKE_REVIEW_CANARY"
            self.assertIsNotNone(SECRET_VALUE.search(marker))
            excluded = ("docs/reviews/old.md", "docs/reviews/nested/old.md", "docs/ad_hoc.md")
            for relative in excluded:
                path = project / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(marker, encoding="utf-8")
            run = root / "run"
            with mock.patch("swivd.v2_pipeline.PROJECT_ROOT", project):
                records = _copy_source_snapshot(run)
            self.assertEqual({r["path"] for r in records}, ROOT_FILES | DOCS)
            for record in records:
                frozen = run / "source" / record["path"]
                self.assertEqual(frozen.read_bytes(), (project / record["path"]).read_bytes())
                self.assertEqual(record["bytes"], frozen.stat().st_size)
                self.assertEqual(record["sha256"], sha256_file(frozen))
            for relative in excluded:
                self.assertFalse((run / "source" / relative).exists())
                self.assertEqual((project / relative).read_text(), marker)

    def test_each_required_doc_missing_fails_closed(self):
        for relative in sorted(DOCS):
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                project = project_fixture(root)
                (project / relative).unlink()
                with mock.patch("swivd.v2_pipeline.PROJECT_ROOT", project):
                    with self.assertRaisesRegex(V2PipelineError, "SOURCE_CLOSURE_INVALID"):
                        _copy_source_snapshot(root / "run")

    def test_symlink_required_doc_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = project_fixture(root)
            path = project / "docs/runbook.md"
            path.unlink()
            try:
                path.symlink_to(project / "docs/architecture.md")
            except OSError:
                self.skipTest("host does not permit creation of symlinks")
            with mock.patch("swivd.v2_pipeline.PROJECT_ROOT", project):
                with self.assertRaisesRegex(V2PipelineError, "SOURCE_CLOSURE_INVALID"):
                    _copy_source_snapshot(root / "run")


if __name__ == "__main__":
    unittest.main()
