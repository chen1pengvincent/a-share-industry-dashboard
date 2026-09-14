#!/usr/bin/env python3
"""Single explicit entrypoint for SWIVD."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import socket
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="申万行业估值全景仪表盘（research-only）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate_spec = sub.add_parser("validate-spec", help="只读验证项目机器规格")
    validate_spec.add_argument("--spec", default="PROJECT_SPEC.json")

    run = sub.add_parser("run", help="联网获取 Tushare 数据并创建唯一运行目录")
    run.add_argument("--as-of", required=True, help="必须是开市日，格式 YYYYMMDD")
    run.add_argument("--spec", default="PROJECT_SPEC.json")
    run.add_argument("--output-root", default="output")

    rebuild = sub.add_parser("rebuild", help="从冻结规范化输入断网重建派生产物")
    rebuild.add_argument("--run-dir", required=True)
    rebuild.add_argument("--output-dir", required=True)

    validate_run = sub.add_parser("validate-run", help="只读验证某次运行的完整性")
    validate_run.add_argument("--run-dir", required=True)

    doctor = sub.add_parser("doctor", help="只输出本地运行环境布尔状态，不显示 token")
    doctor.add_argument("--data-dir")
    doctor.add_argument("--host", default="127.0.0.1")
    doctor.add_argument("--port", type=int, default=8765)

    serve = sub.add_parser("serve", help="启动仅监听 127.0.0.1 的本地服务")
    serve.add_argument("--data-dir")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)

    update = sub.add_parser(
        "update",
        help="按上海时区 18:30 截止规则更新至最近可更新交易日",
    )
    update.add_argument("--data-dir")
    update.add_argument("--spec", default="PROJECT_SPEC_V4.json")

    materialize = sub.add_parser("materialize-date", help="显式建立一个历史开市日快照")
    materialize.add_argument("--as-of", required=True)
    materialize.add_argument("--data-dir")
    materialize.add_argument("--spec", default="PROJECT_SPEC_V4.json")

    rebuild_v2 = sub.add_parser("rebuild-v2", help="从 v2 冻结输入断网重建表和 UI")
    rebuild_v2.add_argument("--run-dir", required=True)
    rebuild_v2.add_argument("--output-dir", required=True)

    return parser


def _resolve(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return PROJECT_ROOT / candidate


def _data_dir(value: str | None) -> Path:
    from swivd.v2_storage import resolve_data_dir

    return resolve_data_dir(value)


def _doctor(data_dir: Path | None, host: str, port: int) -> dict[str, object]:
    from swivd.runtime_environment import (
        dependency_status,
        legacy_archive_available,
        timezone_available,
    )

    python_ok = sys.implementation.name == "cpython" and (3, 11) <= sys.version_info[:2] < (3, 15)
    env_token = bool((os.environ.get("TUSHARE_TOKEN") or "").strip())
    local_token = False
    if not env_token:
        try:
            # The SDK lookup may print diagnostics. Doctor emits only its own
            # status object, never SDK output or exception text.
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                import tushare  # type: ignore[import-not-found]

                local_token = bool(str(tushare.get_token() or "").strip())
        except Exception:
            local_token = False
    data_dir_accessible = data_dir is not None
    data_dir_exists = False
    data_dir_writable = False
    if data_dir is not None:
        try:
            parent = data_dir if data_dir.exists() else next(
                (candidate for candidate in data_dir.parents if candidate.exists()), data_dir.parent
            )
            data_dir_exists = data_dir.is_dir()
            data_dir_writable = parent.is_dir() and os.access(parent, os.W_OK)
        except (OSError, RuntimeError, ValueError):
            data_dir_accessible = False
    port_available = False
    if host == "127.0.0.1" and 1 <= port <= 65535:
        probe = None
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.bind((host, port))
            port_available = True
        except OSError:
            port_available = False
        finally:
            if probe is not None:
                probe.close()
    return {
        "python_supported": python_ok,
        **dependency_status(PROJECT_ROOT),
        "timezone_available": timezone_available(),
        "legacy_archive_available": legacy_archive_available(PROJECT_ROOT),
        "token_configured": env_token or local_token,
        "token_source_type": "ENVIRONMENT" if env_token else "TUSHARE_LOOKUP" if local_token else "NONE",
        "data_dir_accessible": data_dir_accessible,
        "data_dir_exists": data_dir_exists,
        "data_dir_writable": data_dir_writable,
        "loopback_host": host == "127.0.0.1",
        "port_available": port_available,
    }


def _progress(phase: str, completed: int, total: int, item: str) -> None:
    print(
        json.dumps(
            {"phase": phase, "completed_units": completed, "total_units": total, "current_item": item},
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.command == "validate-spec":
        from swivd.validator import validate_spec

        result = validate_spec(_resolve(args.spec))
    elif args.command == "run":
        from swivd.pipeline import run_live

        result = run_live(
            spec_path=_resolve(args.spec),
            as_of=args.as_of,
            output_root=_resolve(args.output_root),
        )
    elif args.command == "rebuild":
        from swivd.pipeline import rebuild_derived

        result = rebuild_derived(
            run_dir=_resolve(args.run_dir),
            output_dir=Path(args.output_dir).resolve(),
        )
    elif args.command == "validate-run":
        from swivd.validator import validate_run

        result = validate_run(_resolve(args.run_dir))
    elif args.command == "doctor":
        try:
            data_dir = _data_dir(args.data_dir)
        except (OSError, RuntimeError, ValueError):
            data_dir = None
        result = _doctor(data_dir, args.host, args.port)
    elif args.command == "serve":
        from swivd.v2_server import serve

        serve(
            project_root=PROJECT_ROOT,
            data_dir=_data_dir(args.data_dir),
            host=args.host,
            port=args.port,
        )
        return 0
    elif args.command in {"update", "materialize-date"}:
        from swivd.v2_pipeline import execute_locked, execute_update_locked

        purpose = "UPDATE_LATEST" if args.command == "update" else "MATERIALIZE_DATE"
        if purpose == "UPDATE_LATEST":
            def report_target(target):
                print(
                    json.dumps(
                        {"event": "UPDATE_TARGET_SELECTED", **target.as_dict()},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )

            result = execute_update_locked(
                spec_path=_resolve(args.spec),
                data_dir=_data_dir(args.data_dir),
                progress=_progress,
                target_selected=report_target,
            )
        else:
            result = execute_locked(
                spec_path=_resolve(args.spec),
                data_dir=_data_dir(args.data_dir),
                as_of=args.as_of,
                purpose=purpose,
                progress=_progress,
            )
    elif args.command == "rebuild-v2":
        from swivd.v2_pipeline import rebuild_v2

        result = rebuild_v2(
            run_dir=_resolve(args.run_dir),
            output_dir=Path(args.output_dir).resolve(),
        )
    else:  # pragma: no cover
        raise AssertionError(args.command)

    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        # Never include environment values or request bodies in the CLI error.
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
