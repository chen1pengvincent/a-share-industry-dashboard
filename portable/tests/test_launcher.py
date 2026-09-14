from __future__ import annotations

import contextlib
import getpass
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import warnings
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("portable_launcher_under_test", ROOT / "launcher.py")
assert SPEC is not None and SPEC.loader is not None
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)
CANARY = "SYNTHETIC_PORTABLE_CANARY_" + "NOT_A_REAL_TOKEN"


class PackageTests(unittest.TestCase):
    def fixture(self, root):
        for relative in ("launcher.py", "app/run_dashboard.py", "app/PROJECT_SPEC_V4.json", "seed-data/latest_run.json"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n", encoding="utf-8")
        files = [{"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size,
                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                 for path in sorted(root.rglob("*")) if path.is_file()]
        manifest = {"schema_version": "swivd-portable-v1", "target": "macos-arm64", "python_version": "3.14.6", "files": files}
        (root / "package-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    def test_complete_manifest_data_and_finder_metadata_are_readonly(self):
        with tempfile.TemporaryDirectory(prefix="便携 包 & ! ") as temp:
            root = Path(temp)
            self.fixture(root)
            (root / "data").mkdir()
            (root / "data" / "user-state").write_text("keep")
            (root / ".DS_Store").write_bytes(b"finder")
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            self.assertEqual(launcher.verify_package(root)["target"], "macos-arm64")
            self.assertEqual(before, {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_tamper_and_unlisted_file_fail(self):
        for kind in ("tamper", "unlisted", "missing"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.fixture(root)
                if kind == "tamper":
                    (root / "launcher.py").write_text("changed")
                elif kind == "unlisted":
                    (root / "extra.py").write_text("unapproved")
                else:
                    (root / "launcher.py").unlink()
                with self.assertRaises(launcher.PortableError):
                    launcher.verify_package(root)

    def test_windows_junction_is_rejected_even_without_symlink_flag(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(Path, "is_symlink", return_value=False), \
                mock.patch.object(Path, "is_junction", return_value=True):
            self.assertTrue(launcher._is_link(Path(temp)))
            with self.assertRaisesRegex(launcher.PortableError, "DIRECTORY_UNSAFE"):
                launcher._regular_tree(Path(temp))

    def test_finder_metadata_inside_immutable_run_is_rejected(self):
        for prefix in ("seed-data/runs/EXAMPLE", "app/output/runs/EXAMPLE"):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.fixture(root)
                path = root / prefix / ".DS_Store"
                path.parent.mkdir(parents=True)
                path.write_bytes(b"finder")
                with self.assertRaisesRegex(launcher.PortableError, "SNAPSHOT_METADATA_UNEXPECTED"):
                    launcher.verify_package(root)

    def test_traversal_duplicate_and_invalid_hash_fail(self):
        for bad in ("../secret", "/secret", "C:/secret", "a\\b", "a//b", "a/./b", "data/user", "package-manifest.json"):
            with self.subTest(bad=bad), self.assertRaisesRegex(launcher.PortableError, "PACKAGE_PATH_INVALID"):
                launcher._safe_path(bad)
        for kind in ("duplicate", "hash", "bool_size"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                manifest = self.fixture(root)
                if kind == "duplicate":
                    manifest["files"].append(manifest["files"][0])
                elif kind == "hash":
                    manifest["files"][0]["sha256"] = CANARY
                else:
                    manifest["files"][0]["bytes"] = True
                (root / "package-manifest.json").write_text(json.dumps(manifest))
                with self.assertRaisesRegex(launcher.PortableError, "PACKAGE_MANIFEST_INVALID"):
                    launcher.verify_package(root)

    @unittest.skipIf(os.name == "nt", "Windows symlinks require host privileges")
    def test_package_and_data_symlink_fail(self):
        for destination in ("data", "app"):
            with self.subTest(destination=destination), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.fixture(root)
                external = root.parent / (root.name + "-not-created")
                if destination == "app":
                    shutil.rmtree(root / "app")
                (root / destination).symlink_to(external, target_is_directory=True)
                with self.assertRaisesRegex(launcher.PortableError, "SYMLINK_FORBIDDEN"):
                    launcher.verify_package(root)


class CredentialTests(unittest.TestCase):
    def token_context(self, value=None):
        return mock.patch.dict(sys.modules, {"tushare": types.SimpleNamespace(get_token=mock.Mock(return_value=value))})

    def test_environment_precedes_sdk_and_stays_out_of_output(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"TUSHARE_TOKEN": CANARY}, clear=True), \
                self.token_context() as modules, contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), \
                mock.patch.object(getpass, "getpass", side_effect=AssertionError("prompt forbidden")):
            self.assertEqual(launcher.configure_token(interactive=True), "ENVIRONMENT")
            modules["tushare"].get_token.assert_not_called()
            self.assertEqual(os.environ["TUSHARE_TOKEN"], CANARY)
        self.assertEqual(out.getvalue(), "")

    def test_sdk_output_and_errors_are_suppressed(self):
        def noisy():
            print(CANARY)
            print(CANARY, file=sys.stderr)
            return CANARY
        for fail in (False, True):
            getter = mock.Mock(side_effect=RuntimeError(CANARY) if fail else noisy)
            output = io.StringIO()
            with self.subTest(fail=fail), mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.dict(sys.modules, {"tushare": types.SimpleNamespace(get_token=getter)}), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                if fail:
                    with self.assertRaisesRegex(launcher.PortableError, "TOKEN_LOOKUP_FAILED"):
                        launcher.configure_token(interactive=False)
                else:
                    self.assertEqual(launcher.configure_token(interactive=False), "TUSHARE_LOOKUP")
                    self.assertEqual(os.environ["TUSHARE_TOKEN"], CANARY)
            self.assertNotIn(CANARY, output.getvalue())

    def test_missing_noninteractive_token_never_prompts(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.token_context(), \
                mock.patch.object(getpass, "getpass", side_effect=AssertionError("prompt forbidden")):
            self.assertEqual(launcher.configure_token(interactive=False), "NONE")
            self.assertNotIn("TUSHARE_TOKEN", os.environ)

    def test_prompt_token_is_process_only_and_empty_is_readonly(self):
        for token, source in ((CANARY, "SESSION_INPUT"), ("", "NONE")):
            with self.subTest(source=source), mock.patch.dict(os.environ, {}, clear=True), self.token_context(), \
                    mock.patch.object(sys.stdin, "isatty", return_value=True), \
                    mock.patch.object(sys.stderr, "isatty", return_value=True), \
                    mock.patch.object(getpass, "getpass", return_value=token), \
                    mock.patch.object(Path, "write_text", side_effect=AssertionError("token write forbidden")):
                self.assertEqual(launcher.configure_token(interactive=True), source)
                self.assertEqual(os.environ.get("TUSHARE_TOKEN", ""), token)

    def test_missing_tty_and_noecho_downgrade_are_rejected(self):
        def unsafe_prompt(_prompt):
            warnings.warn("no safe terminal " + CANARY, getpass.GetPassWarning)
            return CANARY
        for tty, action in ((False, AssertionError("must not prompt")), (True, unsafe_prompt), (True, EOFError(CANARY))):
            with self.subTest(tty=tty, action=type(action).__name__), mock.patch.dict(os.environ, {}, clear=True), \
                    self.token_context(), mock.patch.object(sys.stdin, "isatty", return_value=tty), \
                    mock.patch.object(sys.stderr, "isatty", return_value=tty), \
                    mock.patch.object(getpass, "getpass", side_effect=action):
                with self.assertRaisesRegex(launcher.PortableError, "TOKEN_TERMINAL_REQUIRED"):
                    launcher.configure_token(interactive=True)
                self.assertNotIn("TUSHARE_TOKEN", os.environ)

    def test_forbidden_token_characters_do_not_echo(self):
        for token in (CANARY + "\nX", CANARY + "\rX", CANARY + "\x00", CANARY + "\n"):
            with self.subTest(token=repr(token)), self.assertRaisesRegex(launcher.PortableError, "TOKEN_INVALID") as raised:
                launcher._check_token(token)
            self.assertNotIn(CANARY, str(raised.exception))

    def test_cli_and_argument_errors_never_echo_secrets(self):
        for exception in (ValueError(CANARY), KeyboardInterrupt()):
            output = io.StringIO()
            with mock.patch.object(launcher, "main", side_effect=exception), contextlib.redirect_stderr(output):
                self.assertIn(launcher.cli([]), {1, 130})
            self.assertNotIn(CANARY, output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(launcher.cli(["--token", CANARY]), 1)
        self.assertNotIn(CANARY, output.getvalue())


class DataTests(unittest.TestCase):
    def test_seed_import_is_atomic_and_existing_data_not_replaced(self):
        with tempfile.TemporaryDirectory(prefix="迁移 数据 ") as temp:
            root = Path(temp)
            (root / "seed-data").mkdir()
            (root / "seed-data" / "fixture").write_text("seed")
            (root / "seed-data" / ".DS_Store").write_bytes(b"finder")
            with mock.patch.object(launcher, "validate_state", return_value={"state_verified": True}), \
                    mock.patch.object(launcher, "_recover_state"):
                state = launcher.prepare_data(root)
                self.assertEqual(state, root / "data" / "state")
                self.assertEqual((state / "fixture").read_text(), "seed")
                self.assertFalse((state / ".DS_Store").exists())
                (state / "fixture").write_text("user changes")
                self.assertEqual(launcher.prepare_data(root), state)
                self.assertEqual((state / "fixture").read_text(), "user changes")
                self.assertFalse(list((root / "data").glob(".import-*")))

    def test_failed_import_never_creates_current_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "seed-data").mkdir()
            with mock.patch.object(launcher, "validate_state", side_effect=launcher.PortableError("DATA_VALIDATION_FAILED")):
                with self.assertRaisesRegex(launcher.PortableError, "DATA_VALIDATION_FAILED"):
                    launcher.prepare_data(root)
            self.assertFalse((root / "data" / "state").exists())
            self.assertEqual(len(list((root / "data").glob(".import-*"))), 1)

    def test_foreign_data_content_and_invalid_existing_state_fail_closed(self):
        for kind in ("foreign", "badstate"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / "data").mkdir()
                marker = root / "data" / ("user-file" if kind == "foreign" else "state")
                marker.write_text("do not modify")
                with self.assertRaises(launcher.PortableError):
                    launcher.prepare_data(root)
                self.assertEqual(marker.read_text(), "do not modify")

    def test_real_import_lock_conflict_and_release(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with launcher._import_lock(root):
                with self.assertRaisesRegex(launcher.PortableError, "DATA_IMPORT_BUSY"):
                    with launcher._import_lock(root):
                        self.fail("second writer must not acquire")
            with launcher._import_lock(root):
                pass


class EntrypointTests(unittest.TestCase):
    def test_runtime_rejects_system_python_wrong_platform_and_nonisolated_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime" / "bin" / "python3.14"
            cases = (("/system/python", "arm64", True, "BUNDLED_PYTHON_REQUIRED"),
                     (str(runtime), "x86_64", True, "PLATFORM_MISMATCH"),
                     (str(runtime), "arm64", False, "ISOLATED_PYTHON_REQUIRED"))
            for executable, machine, isolated, message in cases:
                with self.subTest(message=message), mock.patch.object(launcher.platform, "system", return_value="Darwin"), \
                        mock.patch.object(launcher.platform, "machine", return_value=machine), \
                        mock.patch.object(sys, "executable", executable), mock.patch.object(sys, "version_info", (3, 14, 6)), \
                        mock.patch.object(sys, "flags", types.SimpleNamespace(isolated=isolated)), \
                        mock.patch.object(sys, "dont_write_bytecode", True):
                    with self.assertRaisesRegex(launcher.PortableError, message):
                        launcher.verify_runtime(root, {"target": "macos-arm64"})

    def test_verify_and_doctor_do_not_prompt_initialize_or_serve(self):
        for command in ("verify", "doctor", "self-test"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                entry = types.SimpleNamespace(_doctor=mock.Mock(return_value={"token_configured": False}), main=mock.Mock())
                with mock.patch.object(launcher, "verify_package", return_value={"target": "macos-arm64"}), \
                        mock.patch.object(launcher, "verify_runtime"), mock.patch.object(launcher, "_load_app", return_value=entry), \
                        mock.patch.object(launcher, "validate_state", return_value={"state_verified": True}), \
                        mock.patch.object(launcher, "configure_token", side_effect=AssertionError("token prompt")), \
                        mock.patch.object(launcher, "prepare_data", side_effect=AssertionError("data write")), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(launcher.main([command], root=root), 0)
                entry.main.assert_not_called()
                self.assertEqual(list(root.iterdir()), [])

    def test_corrupted_package_is_rejected_before_app_import(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(launcher, "_load_app", side_effect=AssertionError("unverified import")):
            with self.assertRaisesRegex(launcher.PortableError, "PACKAGE_MANIFEST_MISSING"):
                launcher.main(["verify"], root=Path(temp))

    def test_only_loopback_port_is_probed(self):
        with mock.patch.object(socket := launcher.socket, "socket") as factory:
            launcher._check_port(8765)
            factory.return_value.__enter__.return_value.bind.assert_called_once_with(("127.0.0.1", 8765))
        for port in (0, -1, 65536):
            with self.assertRaisesRegex(launcher.PortableError, "INVALID_PORT"):
                launcher._check_port(port)

    @unittest.skipIf(os.name == "nt", "POSIX shell behavior; Windows E2E is separate")
    def test_mac_launcher_uses_only_bundled_interpreter_and_quotes_path(self):
        with tempfile.TemporaryDirectory(prefix="Mac 包 & ! ") as temp:
            root = Path(temp)
            shutil.copyfile(ROOT / "Start-Mac.command", root / "Start-Mac.command")
            executable = root / "runtime" / "bin" / "python3.14"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\nfor value in \"$@\"; do printf '%s\\n' \"$value\"; done\n")
            executable.chmod(0o700)
            result = subprocess.run(["/bin/sh", str(root / "Start-Mac.command"), "doctor", "--port", "8877"],
                                    cwd=root.parent, env={"PATH": "/does-not-exist"}, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ["-I", "-B", "-X", "utf8", str(root.resolve() / "launcher.py"), "doctor", "--port", "8877"])
            executable.unlink()
            result = subprocess.run(["/bin/sh", str(root / "Start-Mac.command")], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn("BUNDLED_PYTHON_UNAVAILABLE", result.stderr)

    def test_windows_script_does_not_install_save_or_fall_back(self):
        script = (ROOT / "Start-Windows.cmd").read_text(encoding="utf-8")
        self.assertIn("DisableDelayedExpansion", script)
        self.assertIn('runtime\\python.exe" -I -B -X utf8', script)
        for forbidden in ("setx", "pip install", "py -", "taskkill", "TUSHARE_TOKEN", "start http"):
            self.assertNotIn(forbidden, script)

    def test_explicit_utf8_applies_to_pipes_despite_environment_encoding(self):
        # Native CLI flag proof, not a substitute for Windows-host acceptance.
        command = [sys.executable, "-I", "-B", "-X", "utf8", "-c",
                   "import json,sys; print(json.dumps({'mode':sys.flags.utf8_mode,"
                   "'encoding':sys.stdout.encoding,'text':'中文验收'},ensure_ascii=False))"]
        environment = {"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONIOENCODING": "ascii"}
        for key in ("SYSTEMROOT", "WINDIR"):
            if key in os.environ:
                environment[key] = os.environ[key]
        result = subprocess.run(command, env=environment, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(payload, {"mode": 1, "encoding": "utf-8", "text": "中文验收"})


if __name__ == "__main__":
    unittest.main()
