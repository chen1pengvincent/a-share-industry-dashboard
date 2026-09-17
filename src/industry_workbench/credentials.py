"""Terminal-only startup credentials; never persist or return the secret."""
from __future__ import annotations

import contextlib
import getpass
import io
import os
import sys
import warnings

from .models import DataError


STARTUP_ERROR_MESSAGES = {
    "TOKEN_INVALID": "本机 Token 格式无效，请检查环境变量或已有 Tushare 配置。",
    "TOKEN_LOOKUP_FAILED": "无法读取已有 Tushare 配置，请检查本机配置后重启；不会显示凭据。",
    "TOKEN_TERMINAL_REQUIRED": "无法安全隐藏输入。请在终端启动并输入 Token；只浏览已有数据可加 --no-scheduler。",
}


def _checked_token(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or any(c in value for c in ("\r", "\n", "\x00")):
        raise DataError("TOKEN_INVALID")
    return value.strip()


def configure_startup_token(*, interactive: bool = True) -> str:
    """Match the old launcher's env → local SDK → hidden session prompt order.

    Return a source label only. A blank prompt allows offline browsing. An
    unsafe getpass fallback is an error, never a clear-text stdin prompt.
    """
    token = _checked_token(os.environ.get("TUSHARE_TOKEN", ""))
    source = "ENVIRONMENT" if token else "NONE"
    if not token:
        try:
            # The inherited SDK may print when no local token exists.
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                import tushare
                local = tushare.get_token()
        except Exception:
            raise DataError("TOKEN_LOOKUP_FAILED") from None
        token = _checked_token(local)
        if token:
            source = "TUSHARE_LOOKUP"
    if not token and interactive:
        if not sys.stdin.isatty() or not sys.stderr.isatty():
            raise DataError("TOKEN_TERMINAL_REQUIRED")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                token = _checked_token(getpass.getpass(
                    "Tushare Token（输入不显示；直接回车只浏览已有数据并关闭本次自动更新）："
                ))
        except (getpass.GetPassWarning, EOFError):
            raise DataError("TOKEN_TERMINAL_REQUIRED") from None
        if token:
            source = "SESSION_INPUT"
    if token:
        # No set_token(), file, CLI argument, HTTP payload, or job record.
        os.environ["TUSHARE_TOKEN"] = token
    return source
