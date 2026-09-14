"""Temp-only adversarial tests for the independent delivery verifier."""

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "verify_delivery.py"
SPEC = importlib.util.spec_from_file_location("swivd_delivery_verifier_tests", SCRIPT)
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)
BUILD_SPEC = importlib.util.spec_from_file_location("swivd_portable_builder_tests", SCRIPT.with_name("build.py"))
builder = importlib.util.module_from_spec(BUILD_SPEC)
BUILD_SPEC.loader.exec_module(builder)


class DeliveryVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="swivd-package-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "中文 空格 &!"
        self.root.mkdir()
        (self.root / "runtime").mkdir()
        (self.root / "runtime/python.exe").write_bytes(b"fixture executable is never executed")
        self.record = {"path": "runtime/python.exe", "bytes": 36, "sha256": ""}
        payload = (self.root / self.record["path"]).read_bytes()
        self.record.update(bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        self.manifest = {"python_executable": "runtime/python.exe", "files": [self.record]}
        self.write_manifest()

    def write_manifest(self):
        (self.root / "package-manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def test_exact_inventory_accepts_unicode_metacharacter_path(self):
        self.assertEqual(verifier.verify_inventory(self.root), self.manifest)

    def test_modified_file_is_rejected(self):
        (self.root / "runtime/python.exe").write_bytes(b"changed")
        with self.assertRaisesRegex(verifier.AcceptanceError, "PACKAGE_FILE_CHANGED"):
            verifier.verify_inventory(self.root)

    def test_extra_static_file_is_rejected(self):
        (self.root / "extra.txt").write_text("not bound", encoding="utf-8")
        with self.assertRaisesRegex(verifier.AcceptanceError, "PACKAGE_INVENTORY_MISMATCH"):
            verifier.verify_inventory(self.root)

    def test_existing_mutable_data_is_not_a_static_input(self):
        (self.root / "data").mkdir()
        (self.root / "data/user-content.txt").write_text("do not modify", encoding="utf-8")
        self.assertEqual(verifier.verify_inventory(self.root), self.manifest)

    def test_finder_metadata_does_not_become_a_static_input(self):
        (self.root / ".DS_Store").write_bytes(b"Finder metadata")
        self.assertEqual(verifier.verify_inventory(self.root), self.manifest)

    def test_finder_metadata_inside_frozen_run_is_rejected(self):
        frozen = self.root / "seed-data/runs/example"
        frozen.mkdir(parents=True)
        (frozen / ".DS_Store").write_bytes(b"not part of snapshot")
        with self.assertRaisesRegex(verifier.AcceptanceError, "SNAPSHOT_METADATA_UNEXPECTED"):
            verifier.verify_inventory(self.root)

    def test_ntfs_junction_root_data_and_file_ancestor_are_rejected(self):
        # Exercise Windows junction semantics on every host without creating
        # an actual reparse point or requiring Windows privileges.
        data = self.root / "data"
        data.mkdir()
        for junction in (self.root, data, self.root / "runtime"):
            with self.subTest(junction=junction.name), mock.patch.object(Path, "is_junction", lambda path: path == junction, create=True):
                with self.assertRaises(verifier.AcceptanceError):
                    verifier.verify_inventory(self.root)

    def test_data_container_symlink_is_rejected(self):
        outside = self.root.parent / "outside-data"
        outside.mkdir()
        try:
            (self.root / "data").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Host does not permit test symlinks")
        with self.assertRaisesRegex(verifier.AcceptanceError, "PACKAGE_SYMLINK_FORBIDDEN"):
            verifier.verify_inventory(self.root)

    def test_pointer_result_is_already_a_run_directory(self):
        run = self.root / "runs" / "SWIVD2-RUN-20260904-001"
        run.mkdir(parents=True)
        pointer = {"run_id": run.name}
        self.assertEqual(verifier.seed_location((run, pointer)), (run, pointer))
        with self.assertRaisesRegex(verifier.AcceptanceError, "SEED_POINTER_LOCATION_MISMATCH"):
            verifier.seed_location((run.parent, pointer))

    def test_traversal_absolute_drive_and_ambiguous_paths_rejected(self):
        for value in ("../bad", "/bad", "C:/bad", "a\\b", "a//b", "a/./b", "a/../b", ""):
            with self.subTest(value=value), self.assertRaises(verifier.AcceptanceError):
                verifier.safe_path(self.root, value)

    def test_duplicate_record_rejected(self):
        self.manifest["files"].append(dict(self.record))
        self.write_manifest()
        with self.assertRaisesRegex(verifier.AcceptanceError, "PACKAGE_FILE_DUPLICATE"):
            verifier.verify_inventory(self.root)

    def test_unbound_runtime_path_rejected_without_system_fallback(self):
        self.manifest["python_executable"] = "system-python"
        self.write_manifest()
        with self.assertRaisesRegex(verifier.AcceptanceError, "PACKAGE_PYTHON_NOT_BOUND"):
            verifier.verify_inventory(self.root)

    def test_packaged_worker_explicitly_enables_utf8_in_isolated_mode(self):
        completed = mock.Mock(returncode=0, stdout='{"status":"PASS"}')
        with mock.patch.object(verifier.subprocess, "run", return_value=completed) as process:
            self.assertEqual(verifier.verify_delivery(self.root)["status"], "PASS")
        argv = process.call_args.args[0]
        self.assertEqual(argv[1:5], ["-I", "-B", "-X", "utf8"])
        self.assertNotIn("TUSHARE_TOKEN", process.call_args.kwargs["env"])
        self.assertFalse(process.call_args.kwargs.get("shell", False))

    def test_symlink_parent_rejected(self):
        target = self.root / "outside"
        target.mkdir()
        try:
            (self.root / "link").symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Host does not permit test symlinks")
        with self.assertRaisesRegex(verifier.AcceptanceError, "PACKAGE_SYMLINK_FORBIDDEN"):
            verifier.safe_path(self.root, "link/file.txt")

    def test_network_guard_denies_dns_and_external_ipv4_ipv6(self):
        for event, arguments in (
            ("socket.connect", (None, ("8.8.8.8", 443))),
            ("socket.connect", (None, ("2001:4860:4860::8888", 443))),
            ("socket.bind", (None, ("0.0.0.0", 0))),
            ("socket.getaddrinfo", ("api.tushare.pro", 443)),
            ("socket.gethostbyname", ("localhost",)),
            ("socket.sendto", (None, b"payload", ("8.8.8.8", 53))),
        ):
            with self.subTest(event=event, arguments=arguments), self.assertRaises(verifier.AcceptanceError):
                verifier.network_guard(event, arguments)
        verifier.network_guard("socket.connect", (None, ("127.0.0.1", 8765)))
        verifier.network_guard("socket.getaddrinfo", ("127.0.0.1", 8765))
        verifier.network_guard("socket.bind", (None, ("::1", 0)))

    def test_derived_comparison_rejects_missing_and_changed_files(self):
        original, rebuilt = self.root / "original", self.root / "rebuilt"
        for root in (original, rebuilt):
            (root / "tables").mkdir(parents=True)
            (root / "tables/example.csv").write_bytes(b"same")
        self.assertEqual(verifier.compare_derived(original, rebuilt), 1)
        (rebuilt / "tables/example.csv").write_bytes(b"different")
        with self.assertRaisesRegex(verifier.AcceptanceError, "REBUILD_BYTES_MISMATCH"):
            verifier.compare_derived(original, rebuilt)
        (original / "tables/missing.csv").write_bytes(b"missing")
        with self.assertRaisesRegex(verifier.AcceptanceError, "REBUILD_FILE_SET_MISMATCH"):
            verifier.compare_derived(original, rebuilt)


class DeliveryBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="swivd-builder-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_traversal_and_windows_invalid_names_are_rejected(self):
        for value in ("../escape", "/absolute", "C:/drive", "bad\\name", "a//b", "name.", "bad:name", "CON", "aux.txt", "NUL.dat", "COM1.log", "LPT9", "folder/PRN"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                builder.safe_path(self.root, value)

    def test_record_requires_existing_exact_source_bytes(self):
        payload = b"frozen source"
        record = {"path": "source.py", "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        with self.assertRaises(ValueError):
            builder.verify_record(self.root, record)
        (self.root / "source.py").write_bytes(payload)
        builder.verify_record(self.root, record)
        (self.root / "source.py").write_bytes(b"drift")
        with self.assertRaises(ValueError):
            builder.verify_record(self.root, record)

    def test_inventory_rejects_windows_device_name(self):
        # A reserved device name cannot be created as an ordinary Windows file.
        import os
        if os.name == "nt":
            self.skipTest("Windows filesystem already rejects this fixture")
        (self.root / "CON.txt").write_text("not portable", encoding="utf-8")
        with self.assertRaises(ValueError):
            builder.inventory(self.root)

    def test_source_root_symlink_is_rejected(self):
        original = self.root / "original"
        original.mkdir()
        (original / "file.txt").write_text("test", encoding="utf-8")
        link = self.root / "link"
        try:
            link.symlink_to(original, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Host does not permit test symlinks")
        with self.assertRaises(ValueError):
            builder.copy_tree(link, self.root / "destination")

    def test_asset_inventory_checked_before_any_output_is_created(self):
        target = "windows-x86_64"
        assets = self.root / "assets"
        asset = assets / target
        (asset / "runtime").mkdir(parents=True)
        (asset / "packages").mkdir()
        (asset / "runtime/python.exe").write_bytes(b"not executed")
        (asset / "asset-manifest.json").write_text(json.dumps({"target": target, "python_version": "3.14.6", "files": []}), encoding="utf-8")
        output = self.root / "output"
        with self.assertRaisesRegex(ValueError, "ASSET_INVENTORY_MISMATCH"):
            builder.build(project=self.root / "missing-project", seed_data=self.root / "missing-seed", assets=assets, output_dir=output, target=target)
        self.assertFalse(output.exists())

    def self_consistent_asset(self):
        target = "windows-x86_64"
        assets, project = self.root / "assets", self.root / "project"
        asset = assets / target
        (asset / "runtime").mkdir(parents=True)
        (asset / "packages").mkdir()
        payload = b"hash-consistent inert runtime fixture"
        (asset / "runtime/python.exe").write_bytes(payload)
        project.mkdir()
        (project / "requirements.lock").write_bytes(b"frozen lock fixture")
        metadata = {
            "target": target, "python_version": "3.14.6",
            "requirements_sha256": builder.sha(project / "requirements.lock"),
            "runtime_source": builder.read(builder.HERE / "runtime-sources.json")["targets"][target],
            "files": [{"path": "runtime/python.exe", "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}],
        }
        return target, assets, asset, project, metadata

    def test_hash_consistent_asset_with_wrong_dependency_lock_is_rejected(self):
        target, assets, asset, project, metadata = self.self_consistent_asset()
        metadata["requirements_sha256"] = "0" * 64
        (asset / "asset-manifest.json").write_text(json.dumps(metadata), encoding="utf-8")
        output = self.root / "output"
        with mock.patch.object(builder, "source_and_seed") as source_copy:
            with self.assertRaisesRegex(ValueError, "ASSET_LOCK_MISMATCH"):
                builder.build(project=project, seed_data=self.root / "missing-seed", assets=assets, output_dir=output, target=target)
            source_copy.assert_not_called()
        self.assertFalse(output.exists())

    def test_hash_consistent_asset_with_wrong_upstream_identity_is_rejected(self):
        target, assets, asset, project, metadata = self.self_consistent_asset()
        metadata["runtime_source"] = {**metadata["runtime_source"], "architecture": "UNAPPROVED_ARCHITECTURE"}
        (asset / "asset-manifest.json").write_text(json.dumps(metadata), encoding="utf-8")
        output = self.root / "output"
        with mock.patch.object(builder, "source_and_seed") as source_copy:
            with self.assertRaisesRegex(ValueError, "ASSET_UPSTREAM_MISMATCH"):
                builder.build(project=project, seed_data=self.root / "missing-seed", assets=assets, output_dir=output, target=target)
            source_copy.assert_not_called()
        self.assertFalse(output.exists())

    def test_hash_lock_source_consistent_asset_requires_native_dependency_review(self):
        target, assets, asset, project, metadata = self.self_consistent_asset()
        (asset / "asset-manifest.json").write_text(json.dumps(metadata), encoding="utf-8")
        output = self.root / "output"
        with mock.patch.object(builder, "source_and_seed") as source_copy:
            with self.assertRaisesRegex(ValueError, "ASSET_NATIVE_DEPENDENCIES_UNVERIFIED"):
                builder.build(project=project, seed_data=self.root / "missing-seed", assets=assets, output_dir=output, target=target)
            source_copy.assert_not_called()
        self.assertFalse(output.exists())

    def test_extra_seed_file_is_not_silently_distributed(self):
        project, seed, output = self.root / "project", self.root / "seed", self.root / "output"
        project.mkdir()
        output.mkdir()
        (project / "PROJECT_SPEC.json").write_text("{}", encoding="utf-8")
        seed_run = seed / "runs" / builder.SEED_ID
        seed_run.mkdir(parents=True)
        seed_manifest = {"purpose": "UPDATE_LATEST", "decision_eligible": False, "production_approved": False, "artifacts": [], "source_files": []}
        (seed_run / "manifest.json").write_text(json.dumps(seed_manifest), encoding="utf-8")
        (seed_run / "SHA256SUMS").write_text("fixture", encoding="utf-8")
        (seed_run / "untracked-user-file.txt").write_text("must not distribute", encoding="utf-8")
        legacy = project / "output/runs" / builder.LEGACY_ID
        legacy.mkdir(parents=True)
        (legacy / "manifest.json").write_text(json.dumps({"artifacts": []}), encoding="utf-8")
        (legacy / "SHA256SUMS").write_text("fixture", encoding="utf-8")
        seed_sha, legacy_sha = builder.sha(seed_run / "manifest.json"), builder.sha(legacy / "manifest.json")
        (seed / "latest_run.json").write_text(json.dumps({"run_id": builder.SEED_ID, "target_sha256": seed_sha}), encoding="utf-8")
        (seed / "run_ledger.ndjson").write_text(json.dumps({"run_id": builder.SEED_ID, "event": "RUN_SUCCEEDED"}) + "\n", encoding="utf-8")
        with mock.patch.object(builder, "SEED_SHA", seed_sha), mock.patch.object(builder, "LEGACY_SHA", legacy_sha):
            with self.assertRaises(ValueError):
                builder.source_and_seed(project, seed, output)


if __name__ == "__main__":
    unittest.main()
