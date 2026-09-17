"""Synthetic secrets only; no real SDK configuration or service is used."""
from __future__ import annotations

import contextlib
import getpass
import io
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import warnings

from industry_workbench import cli, credentials
from industry_workbench.models import DataError


CANARY = "SYNTHETIC_STARTUP_SECRET_8d342d61"


class StartupCredentialTests(unittest.TestCase):
    def setUp(self):
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.sdk = types.ModuleType("tushare")
        self.sdk.get_token = Mock(return_value=None)
        self.sdk.set_token = Mock(side_effect=AssertionError("must not persist"))
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for manager in (
            patch.dict(os.environ, {}, clear=True),
            patch.dict("sys.modules", {"tushare": self.sdk}),
            contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr),
        ):
            self.stack.enter_context(manager)
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="startup-credentials-"))

    def tearDown(self):
        self.sdk.set_token.assert_not_called()
        self.assertNotIn(CANARY, self.stdout.getvalue() + self.stderr.getvalue())
        self.assertEqual(list(Path(self.temp).iterdir()), [])

    def terminal(self):
        for target in ("sys.stdin.isatty", "sys.stderr.isatty"):
            self.stack.enter_context(patch(target, return_value=True))

    def test_environment_precedes_local_and_never_prompts(self):
        os.environ["TUSHARE_TOKEN"] = " " + CANARY + " "
        with patch.object(getpass, "getpass", side_effect=AssertionError("no prompt")):
            self.assertEqual(credentials.configure_startup_token(), "ENVIRONMENT")
        self.sdk.get_token.assert_not_called()
        self.assertEqual(os.environ["TUSHARE_TOKEN"], CANARY)

    def test_local_token_is_silent_and_does_not_prompt(self):
        def noisy_lookup():
            print(CANARY)
            print(CANARY, file=credentials.sys.stderr)
            return CANARY
        self.sdk.get_token.side_effect = noisy_lookup
        with patch.object(getpass, "getpass", side_effect=AssertionError("no prompt")):
            self.assertEqual(credentials.configure_startup_token(), "TUSHARE_LOOKUP")
        self.assertEqual(os.environ["TUSHARE_TOKEN"], CANARY)

    def test_hidden_input_stays_in_this_process_and_returns_only_source(self):
        self.terminal()
        with patch.object(getpass, "getpass", return_value=CANARY) as prompt:
            self.assertEqual(credentials.configure_startup_token(), "SESSION_INPUT")
        self.assertEqual(os.environ["TUSHARE_TOKEN"], CANARY)
        prompt.assert_called_once()
        self.assertEqual(prompt.call_args.kwargs, {})  # No 3.14-only echo_char.

    def test_blank_allows_browsing_without_creating_a_token(self):
        self.terminal()
        with patch.object(getpass, "getpass", return_value="   "):
            self.assertEqual(credentials.configure_startup_token(), "NONE")
        self.assertNotIn("TUSHARE_TOKEN", os.environ)

    def test_noninteractive_offline_does_not_prompt(self):
        with patch.object(getpass, "getpass", side_effect=AssertionError("no prompt")):
            self.assertEqual(credentials.configure_startup_token(interactive=False), "NONE")

    def test_non_terminal_aborts_before_reading_a_secret(self):
        for stdin_tty, stderr_tty in ((False, True), (True, False), (False, False)):
            with self.subTest(stdin=stdin_tty, stderr=stderr_tty), patch("sys.stdin.isatty", return_value=stdin_tty), patch("sys.stderr.isatty", return_value=stderr_tty), patch.object(getpass, "getpass") as prompt:
                with self.assertRaisesRegex(DataError, "TOKEN_TERMINAL_REQUIRED"):
                    credentials.configure_startup_token()
                prompt.assert_not_called()

    def test_unsafe_getpass_fallback_or_eof_aborts_without_exposing_error(self):
        self.terminal()
        def warning():
            warnings.warn("unsafe " + CANARY, getpass.GetPassWarning)
            raise AssertionError("must not reach clear text fallback")
        for action in (lambda *a: warning(), EOFError(CANARY)):
            with self.subTest(action=type(action).__name__), patch.object(getpass, "getpass", side_effect=action):
                with self.assertRaisesRegex(DataError, "TOKEN_TERMINAL_REQUIRED") as caught:
                    credentials.configure_startup_token()
                self.assertNotIn(CANARY, str(caught.exception))

    def test_local_lookup_failure_is_opaque(self):
        self.sdk.get_token.side_effect = ValueError(CANARY)
        with self.assertRaisesRegex(DataError, "TOKEN_LOOKUP_FAILED") as caught:
            credentials.configure_startup_token()
        self.assertNotIn(CANARY, str(caught.exception))

    def test_control_characters_rejected_from_all_sources(self):
        self.terminal()
        for value in (CANARY + "\n", "\r" + CANARY, CANARY + "\x00"):
            for origin in ("environment", "local", "prompt"):
                if origin == "environment" and "\x00" in value:
                    continue  # The operating system itself rejects NUL env values.
                with self.subTest(origin=origin, value_suffix=repr(value[-1:])):
                    os.environ.pop("TUSHARE_TOKEN", None)
                    self.sdk.get_token.return_value = value if origin == "local" else None
                    if origin == "environment":
                        os.environ["TUSHARE_TOKEN"] = value
                    with patch.object(getpass, "getpass", return_value=value), self.assertRaisesRegex(DataError, "TOKEN_INVALID"):
                        credentials.configure_startup_token()

    def test_cli_blank_and_explicit_offline_disable_scheduler(self):
        for source, extra, disabled in (("NONE", [], True), ("SESSION_INPUT", [], False), ("ENVIRONMENT", ["--no-scheduler"], True)):
            with self.subTest(source=source, extra=extra), patch.object(cli, "configure_startup_token", return_value=source) as configure, patch.object(cli, "serve") as serve:
                code = cli.main(["serve", "--data-dir", self.temp, "--development", *extra])
                self.assertEqual(code, 0)
                if extra:
                    configure.assert_not_called()
                else:
                    configure.assert_called_once_with()
                self.assertEqual(serve.call_args.kwargs["no_scheduler"], disabled)

    def test_cli_missing_offline_skips_prompt_and_starts_without_scheduler(self):
        with patch.object(getpass, "getpass", side_effect=AssertionError("no prompt")), patch.object(cli, "serve") as serve:
            self.assertEqual(cli.main(["serve", "--data-dir", self.temp, "--development", "--no-scheduler"]), 0)
        self.assertIs(serve.call_args.kwargs["no_scheduler"], True)
        self.sdk.get_token.assert_not_called()

    def test_explicit_offline_remains_usable_with_broken_sdk_config(self):
        self.sdk.get_token.side_effect = ValueError(CANARY)
        with patch.object(cli, "serve") as serve:
            self.assertEqual(cli.main(["serve", "--data-dir", self.temp, "--development", "--no-scheduler"]), 0)
        self.sdk.get_token.assert_not_called()
        self.assertIs(serve.call_args.kwargs["no_scheduler"], True)

    def test_explicit_offline_remains_usable_with_invalid_environment_token(self):
        os.environ["TUSHARE_TOKEN"] = CANARY + "\n"
        with patch.object(cli, "serve") as serve:
            self.assertEqual(cli.main(["serve", "--data-dir", self.temp, "--development", "--no-scheduler"]), 0)
        self.sdk.get_token.assert_not_called()
        self.assertIs(serve.call_args.kwargs["no_scheduler"], True)

    def test_cli_cancel_and_unsafe_input_never_start_server(self):
        self.terminal()
        for error, expected in ((KeyboardInterrupt(), 130), (EOFError(CANARY), 1)):
            with self.subTest(expected=expected), patch.object(getpass, "getpass", side_effect=error), patch.object(cli, "serve") as serve:
                self.assertEqual(cli.main(["serve", "--data-dir", self.temp, "--development"]), expected)
                serve.assert_not_called()

    def test_invalid_port_and_diagnostics_do_not_prompt(self):
        with patch.object(cli, "configure_startup_token", side_effect=AssertionError("no credential read")):
            self.assertEqual(cli.main(["serve", "--port", "0", "--data-dir", self.temp]), 1)
            self.assertEqual(cli.main(["verify", "--data-dir", self.temp, "--development"]), 0)


if __name__ == "__main__":
    unittest.main()
