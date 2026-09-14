from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from swivd import v2_storage as storage
from swivd.io_utils import read_json, sha256_file, write_json
from swivd.v2_jobs import JobManager


OLD_RUN = "SWIVD2-RUN-20211213-001"
NEW_RUN = "SWIVD2-RUN-20211214-001"
JOB_ID = "12345678-1234-1234-1234-123456789abc"
PROVIDER = "INJECTED_TEST_CLIENT"


def _make_run(data_dir: Path, run_id: str, *, purpose: str = "UPDATE_LATEST") -> Path:
    run_dir = data_dir / "runs" / run_id
    run_dir.mkdir(parents=True)
    write_json(
        run_dir / "manifest.json",
        {
            "schema_version": "swivd-local-snapshot-manifest-v3",
            "run_id": run_id,
            "as_of": run_id.split("-")[2],
            "purpose": purpose,
        },
    )
    return run_dir


def _write_job(data_dir: Path) -> None:
    write_json(
        data_dir / "jobs" / f"{JOB_ID}.json",
        {
            "schema_version": "swivd-job-v2",
            "job_id": JOB_ID,
            "kind": "UPDATE_LATEST",
            "requested_as_of": None,
            "as_of": "20211214",
            "target_selection": None,
            "state": "PUBLISH",
            "completed_units": 0,
            "total_units": 1,
            "percent": 88,
            "current_item": NEW_RUN,
            "safe_message_code": "PUBLISH",
            "run_id": None,
            "created_at": "2021-12-14T18:30:00+08:00",
            "updated_at": "2021-12-14T18:30:00+08:00",
        },
    )


def _ledger_for_run(data_dir: Path, run_id: str) -> list[dict[str, object]]:
    path = data_dir / "run_ledger.ndjson"
    if not path.is_file():
        return []
    return [
        value
        for line in path.read_text(encoding="utf-8").splitlines()
        if (value := json.loads(line)).get("run_id") == run_id
    ]


CRASH_SCRIPT = r"""
import os
import sys
from pathlib import Path
from swivd.io_utils import sha256_file
from swivd.v2_storage import (
    SingleWriterLock,
    apply_publication_pointer,
    commit_publication,
    prepare_publication,
)

data_dir = Path(sys.argv[1])
window = sys.argv[2]
run_id = "SWIVD2-RUN-20211214-001"
with SingleWriterLock(data_dir):
    prepare_publication(
        data_dir,
        run_id=run_id,
        as_of="20211214",
        purpose="UPDATE_LATEST",
        manifest_sha256=sha256_file(data_dir / "runs" / run_id / "manifest.json"),
        provider_kind="INJECTED_TEST_CLIENT",
        job_id="12345678-1234-1234-1234-123456789abc",
    )
    apply_publication_pointer(data_dir, transaction_id=run_id)
    if window == "AFTER_POINTER":
        os._exit(91)
    commit_publication(data_dir, transaction_id=run_id)
    os._exit(92)
"""


class PublicationRecoveryTests(unittest.TestCase):
    def _fixture(self, data_dir: Path) -> tuple[Path, Path]:
        storage.ensure_layout(data_dir)
        old = _make_run(data_dir, OLD_RUN)
        new = _make_run(data_dir, NEW_RUN)
        storage.publish_current(
            data_dir,
            run_id=OLD_RUN,
            as_of="20211213",
            manifest_sha256=sha256_file(old / "manifest.json"),
        )
        _write_job(data_dir)
        return old, new

    def _crash(self, data_dir: Path, window: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = str(SRC_ROOT)
        return subprocess.run(
            [sys.executable, "-c", CRASH_SCRIPT, str(data_dir), window],
            cwd=PROJECT_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_hard_crash_after_pointer_rolls_back_and_interrupts_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            old, _ = self._fixture(data_dir)
            result = self._crash(data_dir, "AFTER_POINTER")
            self.assertEqual(result.returncode, 91, result.stderr)
            self.assertEqual(
                read_json(storage.publication_intent_path(data_dir))["state"],
                "PREPARED",
            )
            self.assertEqual(read_json(data_dir / "latest_run.json")["run_id"], NEW_RUN)
            with self.assertRaisesRegex(ValueError, "PUBLICATION_RECOVERY_REQUIRED"):
                storage.resolve_pointer(data_dir)

            manager = JobManager(data_dir=data_dir, spec_path=Path("unused"))
            self.assertFalse(storage.publication_intent_path(data_dir).exists())
            self.assertEqual(storage.resolve_pointer(data_dir)[0], old.resolve())
            job = manager.get(JOB_ID)
            self.assertEqual(job["state"], "INTERRUPTED")
            self.assertEqual(
                job["safe_message_code"], "PUBLICATION_RECOVERED_BEFORE_COMMIT"
            )
            terminals = _ledger_for_run(data_dir, NEW_RUN)
            self.assertEqual([row["event"] for row in terminals], ["RUN_INTERRUPTED"])

    def test_hard_crash_after_commit_preserves_pointer_and_succeeds_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _, new = self._fixture(data_dir)
            result = self._crash(data_dir, "AFTER_COMMIT")
            self.assertEqual(result.returncode, 92, result.stderr)
            self.assertEqual(
                read_json(storage.publication_intent_path(data_dir))["state"],
                "COMMITTED",
            )
            with self.assertRaisesRegex(ValueError, "PUBLICATION_RECOVERY_REQUIRED"):
                storage.resolve_pointer(data_dir)

            manager = JobManager(data_dir=data_dir, spec_path=Path("unused"))
            self.assertFalse(storage.publication_intent_path(data_dir).exists())
            self.assertEqual(storage.resolve_pointer(data_dir)[0], new.resolve())
            job = manager.get(JOB_ID)
            self.assertEqual(job["state"], "SUCCEEDED")
            self.assertEqual(job["run_id"], NEW_RUN)
            self.assertEqual(job["percent"], 100)
            self.assertEqual(
                [row["event"] for row in _ledger_for_run(data_dir, NEW_RUN)],
                ["RUN_SUCCEEDED"],
            )

            JobManager(data_dir=data_dir, spec_path=Path("unused"))
            self.assertEqual(
                [row["event"] for row in _ledger_for_run(data_dir, NEW_RUN)],
                ["RUN_SUCCEEDED"],
            )

    def test_success_append_then_exception_is_observed_not_relabelled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _, new = self._fixture(data_dir)
            with storage.SingleWriterLock(data_dir):
                storage.prepare_publication(
                    data_dir,
                    run_id=NEW_RUN,
                    as_of="20211214",
                    purpose="UPDATE_LATEST",
                    manifest_sha256=sha256_file(new / "manifest.json"),
                    provider_kind=PROVIDER,
                    job_id=JOB_ID,
                )
                storage.apply_publication_pointer(data_dir, transaction_id=NEW_RUN)
                storage.commit_publication(data_dir, transaction_id=NEW_RUN)
                real_append = storage.append_ledger

                def append_then_raise(target: Path, record: dict[str, object]) -> None:
                    real_append(target, record)
                    raise OSError("simulated post-fsync caller failure")

                with mock.patch.object(
                    storage, "append_ledger", side_effect=append_then_raise
                ):
                    recovered = storage.complete_publication(
                        data_dir, transaction_id=NEW_RUN
                    )

            self.assertEqual(recovered.outcome, "COMMITTED_SUCCESS")
            self.assertFalse(storage.publication_intent_path(data_dir).exists())
            self.assertEqual(storage.resolve_pointer(data_dir)[0], new.resolve())
            self.assertEqual(
                [row["event"] for row in _ledger_for_run(data_dir, NEW_RUN)],
                ["RUN_SUCCEEDED"],
            )
            with self.assertRaisesRegex(ValueError, "PUBLICATION_TRANSACTION_MISSING"):
                storage.abort_publication(
                    data_dir,
                    transaction_id=NEW_RUN,
                    terminal_record={"event": "RUN_FAILED"},
                )
            self.assertEqual(
                [row["event"] for row in _ledger_for_run(data_dir, NEW_RUN)],
                ["RUN_SUCCEEDED"],
            )


if __name__ == "__main__":
    unittest.main()
