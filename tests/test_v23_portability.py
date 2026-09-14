from __future__ import annotations

import hashlib
import importlib.util
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfoNotFoundError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANARY = "CANARY_FAKE_NOT_A_REAL_TOKEN"
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from swivd import runtime_environment as runtime
from swivd import v2_validator

_entry_spec = importlib.util.spec_from_file_location("portability_entrypoint", PROJECT_ROOT / "run_dashboard.py")
assert _entry_spec is not None and _entry_spec.loader is not None
entrypoint = importlib.util.module_from_spec(_entry_spec)
_entry_spec.loader.exec_module(entrypoint)


class RuntimeEnvironmentTests(unittest.TestCase):
    def test_lock_contains_complete_known_runtime_and_direct_timezone(self):
        pins = runtime.frozen_dependency_versions(PROJECT_ROOT)
        self.assertEqual(pins["tushare"], "1.4.29")
        self.assertEqual(pins["certifi"], "2026.6.17")
        self.assertIn("tzdata", pins)
        self.assertTrue({"pandas", "numpy", "requests", "lxml", "simplejson", "bs4",
                         "websocket-client", "tqdm", "python-dateutil", "six",
                         "beautifulsoup4", "soupsieve", "typing-extensions",
                         "urllib3", "idna", "charset-normalizer", "colorama"} <= pins.keys())
        import tomllib
        project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
        self.assertIn(f"tzdata=={pins['tzdata']}", project["project"]["dependencies"])
        self.assertEqual(project["project"]["requires-python"], ">=3.11,<3.15")

    def test_missing_mismatched_and_private_metadata_are_not_locked(self):
        pins = runtime.frozen_dependency_versions(PROJECT_ROOT)
        with mock.patch.object(runtime.importlib.metadata, "version", side_effect=pins.__getitem__):
            self.assertTrue(runtime.dependency_status(PROJECT_ROOT)["dependencies_locked"])
        for bad in (None, "0.0.1", "1.2.3+CANARY_PRIVATE_MACHINE_NAME", "/CANARY_PRIVATE_PATH"):
            def version(package):
                if package == "tushare":
                    if bad is None:
                        raise runtime.importlib.metadata.PackageNotFoundError(package)
                    return bad
                return pins[package]
            with self.subTest(bad=bad), mock.patch.object(runtime.importlib.metadata, "version", side_effect=version):
                state = runtime.dependency_status(PROJECT_ROOT)
                self.assertFalse(state["dependencies_locked"])
                self.assertFalse(state["dependency_matches"]["tushare"])
                record = runtime.safe_runtime_record(PROJECT_ROOT)
                self.assertNotIn("CANARY_PRIVATE", json.dumps(record))
                self.assertNotIn("CANARY_PRIVATE", json.dumps(state))

    def test_invalid_lock_fails_closed_without_echoing_its_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "requirements.lock").write_text("https://CANARY_PRIVATE_LOCATION\n")
            with self.assertRaisesRegex(ValueError, "DEPENDENCY_LOCK_INVALID"):
                runtime.frozen_dependency_versions(root)
            state = runtime.dependency_status(root)
            self.assertFalse(state["dependency_lock_valid"])
            self.assertFalse(state["dependencies_locked"])
            self.assertNotIn("CANARY_PRIVATE", json.dumps(state))

    def test_timezone_absence_is_boolean(self):
        with mock.patch.object(runtime, "ZoneInfo", side_effect=ZoneInfoNotFoundError("CANARY_PRIVATE_LOCATION")):
            self.assertFalse(runtime.timezone_available())

    def test_runtime_does_not_read_token_or_machine_details(self):
        pins = runtime.frozen_dependency_versions(PROJECT_ROOT)
        fake_tushare = types.SimpleNamespace(get_token=mock.Mock(side_effect=AssertionError("token read")))
        with mock.patch.dict(sys.modules, {"tushare": fake_tushare}), \
             mock.patch.object(runtime.importlib.metadata, "version", side_effect=pins.__getitem__), \
             mock.patch("platform.platform", side_effect=AssertionError("private platform")), \
             mock.patch("platform.uname", side_effect=AssertionError("private uname")), \
             mock.patch.dict(os.environ, {"TUSHARE_TOKEN": CANARY}, clear=True):
            record = runtime.safe_runtime_record(PROJECT_ROOT)
        self.assertIn(record["platform"], {"Darwin", "Windows", "Linux", "UNKNOWN"})
        self.assertEqual(record["windows_e2e"], "UNVERIFIED")
        self.assertEqual(record["dependency_versions"], pins)
        self.assertNotIn("CANARY", json.dumps(record))
        fake_tushare.get_token.assert_not_called()


class DoctorTests(unittest.TestCase):
    def _doctor(self, root, *, environment, local_value=None, local_error=None, socket_error=None):
        getter = mock.Mock(return_value=local_value, side_effect=local_error)
        fake_tushare = types.SimpleNamespace(get_token=getter)
        pins = runtime.frozen_dependency_versions(PROJECT_ROOT)
        with mock.patch.dict(os.environ, environment, clear=True), \
             mock.patch.dict(sys.modules, {"tushare": fake_tushare}), \
             mock.patch.object(runtime.importlib.metadata, "version", side_effect=pins.__getitem__), \
             mock.patch.object(entrypoint.socket, "socket", side_effect=socket_error) as socket_factory, \
             mock.patch.object(runtime, "legacy_archive_available", return_value=True):
            result = entrypoint._doctor(root, "127.0.0.1", 8765)
        if socket_error is None:
            socket_factory.return_value.close.assert_called_once()
        return result, getter

    def test_environment_precedes_local_config_and_result_is_only_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "CANARY_PRIVATE_DATA_ROOT"
            state, getter = self._doctor(root, environment={"TUSHARE_TOKEN": CANARY})
            getter.assert_not_called()
            self.assertFalse(root.exists())
        self.assertTrue(state["token_configured"])
        self.assertEqual(state["token_source_type"], "ENVIRONMENT")
        self.assertTrue(state["dependencies_locked"])
        self.assertNotIn("CANARY", json.dumps(state))
        for key, value in state.items():
            if key == "token_source_type":
                continue
            if key == "dependency_matches":
                self.assertTrue(all(type(item) is bool for item in value.values()))
            else:
                self.assertIs(type(value), bool, key)

    def test_local_configuration_and_failure_only_report_source_type(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state, getter = self._doctor(root, environment={"TUSHARE_TOKEN": " "}, local_value="CANARY_FAKE_NOT_A_REAL_TOKEN")
            self.assertEqual(state["token_source_type"], "TUSHARE_LOOKUP")
            getter.assert_called_once_with()
            state, _ = self._doctor(root, environment={}, local_error=ValueError("CANARY_FAKE_NOT_A_REAL_TOKEN"))
            self.assertFalse(state["token_configured"])
            self.assertEqual(state["token_source_type"], "NONE")
            self.assertNotIn("CANARY", json.dumps(state))

    def test_noisy_sdk_lookup_cannot_leak_into_output(self):
        def noisy_lookup():
            print("CANARY_FAKE_NOT_A_REAL_TOKEN")
            print("CANARY_PRIVATE_LOCATION", file=sys.stderr)
            return "CANARY_FAKE_NOT_A_REAL_TOKEN"
        stdout, stderr = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as temp, \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            state, _ = self._doctor(Path(temp), environment={}, local_error=noisy_lookup)
        self.assertTrue(state["token_configured"])
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_path_probe_and_resolution_errors_do_not_escape(self):
        pins = runtime.frozen_dependency_versions(PROJECT_ROOT)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(Path, "exists", side_effect=PermissionError("CANARY_PRIVATE_LOCATION")):
                state, _ = self._doctor(root, environment={"TUSHARE_TOKEN": CANARY})
            self.assertFalse(state["data_dir_accessible"])
            self.assertFalse(state["data_dir_writable"])
        output = io.StringIO()
        with mock.patch.object(entrypoint, "_data_dir", side_effect=PermissionError("CANARY_PRIVATE_LOCATION")), \
             mock.patch.dict(os.environ, {"TUSHARE_TOKEN": CANARY}, clear=True), \
             mock.patch.object(runtime.importlib.metadata, "version", side_effect=pins.__getitem__), \
             mock.patch.object(entrypoint.socket, "socket"), \
             mock.patch.object(runtime, "legacy_archive_available", return_value=True), \
             contextlib.redirect_stdout(output):
            self.assertEqual(entrypoint.main(["doctor"]), 0)
        state = json.loads(output.getvalue())
        self.assertFalse(state["data_dir_accessible"])
        self.assertNotIn("CANARY", output.getvalue())

    def test_socket_failure_and_file_instead_of_directory_are_false(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "data-file"
            root.write_text("unrelated")
            state, _ = self._doctor(root, environment={"TUSHARE_TOKEN": CANARY}, socket_error=OSError("CANARY_PRIVATE_LOCATION"))
            self.assertFalse(state["port_available"])
            self.assertFalse(state["data_dir_exists"])
            self.assertFalse(state["data_dir_writable"])
            self.assertEqual(root.read_text(), "unrelated")


class LegacySourceTests(unittest.TestCase):
    def _fixture(self, root):
        source = root / "output" / "runs" / v2_validator.LEGACY_RUN_ID
        source.mkdir(parents=True)
        records = []
        for relative in v2_validator.LEGACY_SELECTED_FILES:
            if relative == "manifest.json":
                continue
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            content = (relative + "\n").encode()
            target.write_bytes(content)
            records.append({"path": relative, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})
        manifest = {"run_id": v2_validator.LEGACY_RUN_ID, "artifacts": records}
        payload = json.dumps(manifest).encode()
        (source / "manifest.json").write_bytes(payload)
        return source, hashlib.sha256(payload).hexdigest()

    def test_exact_source_is_available_without_copying_anything(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, digest = self._fixture(root)
            before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            with mock.patch.object(v2_validator, "LEGACY_MANIFEST_SHA256", digest):
                self.assertTrue(runtime.legacy_archive_available(root))
            self.assertEqual(before, {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_missing_or_tampered_source_is_unavailable(self):
        for kind in ("missing", "tampered", "manifest"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source, digest = self._fixture(root)
                target = source / ("manifest.json" if kind == "manifest" else "tables/sw2014_history.csv")
                if kind == "missing":
                    target.unlink()
                else:
                    target.write_text("CANARY_TAMPERED")
                with mock.patch.object(v2_validator, "LEGACY_MANIFEST_SHA256", digest):
                    self.assertFalse(runtime.legacy_archive_available(root))

    @unittest.skipIf(os.name == "nt", "Symlink creation requires separate Windows privileges")
    def test_symlinked_archive_input_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, digest = self._fixture(root)
            target = source / "tables/sw2014_history.csv"
            replacement = root / "external.csv"
            target.rename(replacement)
            target.symlink_to(replacement)
            with mock.patch.object(v2_validator, "LEGACY_MANIFEST_SHA256", digest):
                self.assertFalse(runtime.legacy_archive_available(root))


@unittest.skipIf(os.name == "nt", "POSIX launcher behavior; Windows E2E remains unverified")
class MacLauncherTests(unittest.TestCase):
    def _prepare(self, root):
        shutil.copyfile(PROJECT_ROOT / "start_macos.command", root / "start_macos.command")
        (root / "bin").mkdir()
        # The script's dirname is the only external command required before Python selection.
        (root / "bin" / "dirname").symlink_to(shutil.which("dirname"))

    def _python(self, path, label, *, supported=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n" +
            f'if [ "$2" = "-c" ]; then exit {0 if supported else 1}; fi\n' +
            f'echo "{label}"\n' + "for arg in \"$@\"; do printf '%s\\n' \"$arg\"; done\n")
        path.chmod(0o700)

    def _launch(self, root):
        return subprocess.run(["/bin/sh", str(root / "start_macos.command"), "--data-dir", "data with spaces", "--port", "9876"],
            cwd=root.parent, env={"PATH": str(root / "bin")}, text=True, capture_output=True, timeout=10)

    def test_project_venv_wins_and_arguments_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._prepare(root)
            self._python(root / ".venv/bin/python", "PROJECT_VENV")
            self._python(root / "bin/python3.14", "GLOBAL")
            result = self._launch(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ["PROJECT_VENV", "-B", "run_dashboard.py", "serve", "--data-dir", "data with spaces", "--port", "9876"])

    def test_broken_venv_does_not_silently_select_global_python(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._prepare(root)
            self._python(root / ".venv/bin/python", "UNSUPPORTED", supported=False)
            self._python(root / "bin/python3.14", "GLOBAL")
            result = self._launch(root)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("GLOBAL", result.stdout)
            self.assertIn(".venv is unusable", result.stderr)

    def test_supported_interpreters_are_selected_in_declared_order(self):
        for selected in ("3.14", "3.13", "3.12", "3.11"):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self._prepare(root)
                self._python(root / "bin/python3.14", "UNSUPPORTED", supported=False)
                self._python(root / f"bin/python{selected}", selected)
                result = self._launch(root)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines()[0], selected)

    def test_missing_supported_interpreter_fails_with_install_guidance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._prepare(root)
            self._python(root / "bin/python3", "UNSUPPORTED", supported=False)
            result = self._launch(root)
            self.assertEqual(result.returncode, 1)
            self.assertIn("requirements.lock", result.stderr)


if __name__ == "__main__":
    unittest.main()
