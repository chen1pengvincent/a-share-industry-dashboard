"""Runtime checks read temporary locks and mocked metadata; no installation or network."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import importlib.metadata
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from industry_workbench import cli, runtime
from industry_workbench.models import DataError


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workbench-runtime-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pins = {"certifi": "2026.1.1", "tushare": "1.4.0", "tzdata": "2026.1",
                     "pypinyin": "0.55.0"}
        digest = "a" * 64
        (self.root / "requirements.lock").write_text(
            "--require-hashes\n--only-binary=:all:\n" + "\n".join(
                f"{name}=={self.pins[name]} \\\n    --hash=sha256:{digest}" for name in ("certifi", "tushare", "tzdata")), encoding="utf-8")
        self.extension = "-r requirements.lock\n" + "\n".join(
            f"{name}=={self.pins[name]} --hash=sha256:{digest}"
            for name in ("pypinyin",)) + "\n"
        (self.root / "requirements-workbench.lock").write_text(self.extension, encoding="utf-8")

    @contextmanager
    def host(self, *, system="Darwin", implementation="cpython", version=(3, 14, 2), installed=None):
        metadata = self.pins if installed is None else installed
        def installed_version(name):
            if name not in metadata:
                raise importlib.metadata.PackageNotFoundError(name)
            return metadata[name]
        with ExitStack() as stack:
            stack.enter_context(patch.object(runtime, "sys", SimpleNamespace(
                implementation=SimpleNamespace(name=implementation), version_info=version)))
            stack.enter_context(patch.object(runtime.platform, "system", return_value=system))
            stack.enter_context(patch.object(runtime.platform, "release", return_value="25.0.0"))
            stack.enter_context(patch.object(runtime.platform, "machine", return_value="arm64"))
            stack.enter_context(patch.object(runtime.platform, "mac_ver", return_value=("26.0", (), "")))
            stack.enter_context(patch.object(runtime.importlib.metadata, "version", side_effect=installed_version))
            yield

    def inventory(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def assert_code(self, code):
        with self.assertRaises(DataError) as caught:
            runtime.require_supported_environment(self.root)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_supported_runtime_records_actual_and_expected_versions_read_only(self):
        before = self.inventory()
        with self.host():
            identity = runtime.require_supported_environment(self.root)
            status = runtime.dependency_status(self.root)
            self.assertEqual(status, cli.dependency_status(self.root))
        self.assertEqual(identity["schema_version"], "industry-workbench-environment-v1")
        self.assertEqual(identity["python"], {"implementation": "CPython", "version": "3.14.2", "supported": True})
        self.assertEqual(identity["platform"], {"system": "Darwin", "release": "25.0.0", "architecture": "arm64", "macos_version": "26.0"})
        self.assertTrue(identity["dependencies"]["locked"])
        self.assertEqual(identity["dependencies"]["packages"]["pypinyin"],
                         {"expected": "0.55.0", "actual": "0.55.0", "matched": True})
        self.assertTrue({"XlsxWriter", "openpyxl", "et_xmlfile"}.isdisjoint(identity["dependencies"]["packages"]))
        self.assertTrue(status["dependency_lock_valid"] and status["dependencies_present"] and status["dependencies_locked"])
        self.assertEqual(before, self.inventory())

    def test_each_supported_python_minor_and_unsupported_boundaries(self):
        for minor in (11, 12, 13, 14):
            with self.subTest(minor=minor), self.host(version=(3, minor, 0)):
                self.assertTrue(runtime.require_supported_environment(self.root)["python"]["supported"])
        for version in ((3, 10, 9), (3, 15, 0), (4, 0, 0)):
            with self.subTest(version=version), self.host(version=version):
                self.assert_code("PYTHON_NOT_SUPPORTED")
        with self.host(implementation="pypy"):
            self.assert_code("PYTHON_NOT_SUPPORTED")

    def test_formal_non_macos_rejected_but_identity_is_available(self):
        for system in ("Linux", "Windows"):
            with self.subTest(system=system), self.host(system=system):
                identity = runtime.environment_identity(self.root)
                self.assertEqual(identity["platform"]["system"], system)
                self.assertIsNone(identity["platform"]["macos_version"])
                self.assert_code("PLATFORM_NOT_SUPPORTED")

    def test_missing_locks_recorded_without_creating_source_directory(self):
        empty_root = self.root / "nonexistent"
        with self.host():
            identity = runtime.environment_identity(empty_root)
            self.assertEqual(identity["dependencies"], {"lock_valid": False, "present": False, "locked": False, "packages": {}})
            with self.assertRaises(DataError) as caught:
                runtime.require_supported_environment(empty_root)
            self.assertEqual(caught.exception.code, "DEPENDENCY_LOCK_INVALID")
        self.assertFalse(empty_root.exists())
        for filename in ("requirements.lock", "requirements-workbench.lock"):
            path = self.root / filename
            original = path.read_bytes()
            path.unlink()
            with self.host():
                self.assert_code("DEPENDENCY_LOCK_INVALID")
            path.write_bytes(original)

    def test_invalid_extension_include_hash_duplicate_or_missing_pin(self):
        variations = (
            self.extension.replace("-r requirements.lock\n", ""),
            self.extension + "-r requirements.lock\n",
            self.extension.replace("a" * 64, "bad", 1),
            self.extension + "PyPinyin==0.55.0 --hash=sha256:" + "a" * 64 + "\n",
            "\n".join(line for line in self.extension.splitlines() if not line.startswith("pypinyin==")),
        )
        for text in variations:
            with self.subTest(text=text):
                (self.root / "requirements-workbench.lock").write_text(text, encoding="utf-8")
                before = self.inventory()
                with self.host():
                    self.assert_code("DEPENDENCY_LOCK_INVALID")
                    self.assertFalse(runtime.dependency_status(self.root)["dependency_lock_valid"])
                self.assertEqual(before, self.inventory())

    def test_dependency_version_drift_and_absence_have_stable_failure(self):
        installed = {**self.pins, "pypinyin": "0.54.0"}
        with self.host(installed=installed):
            identity = runtime.environment_identity(self.root)
            self.assertEqual(identity["dependencies"]["packages"]["pypinyin"],
                             {"expected": "0.55.0", "actual": "0.54.0", "matched": False})
            self.assertTrue(identity["dependencies"]["present"])
            self.assert_code("DEPENDENCY_LOCK_MISMATCH")
        del installed["pypinyin"]
        with self.host(installed=installed):
            dependencies = runtime.environment_identity(self.root)["dependencies"]
            self.assertFalse(dependencies["present"])
            self.assertIsNone(dependencies["packages"]["pypinyin"]["actual"])
            self.assert_code("DEPENDENCY_LOCK_MISMATCH")

    def test_private_metadata_and_platform_text_are_never_persisted(self):
        private = "PRIVATE_TOKEN_CANARY/Users/example/secrets"
        with self.host(installed={**self.pins, "pypinyin": "0.55.0+" + private}), \
             patch.object(runtime.platform, "release", return_value=private), \
             patch.object(runtime.platform, "machine", return_value=private), \
             patch.object(runtime.platform, "mac_ver", return_value=(private, (), "")):
            identity = runtime.environment_identity(self.root)
            self.assertNotIn(private, json.dumps(identity))
            self.assertNotIn(str(self.root), json.dumps(identity))
            self.assertIsNone(identity["dependencies"]["packages"]["pypinyin"]["actual"])
            self.assertEqual(identity["platform"]["architecture"], "UNKNOWN")
            self.assert_code("DEPENDENCY_LOCK_MISMATCH")


if __name__ == "__main__":
    unittest.main()
