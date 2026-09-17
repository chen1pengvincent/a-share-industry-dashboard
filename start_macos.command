#!/bin/sh
# Start only this checkout's unified app. Never install packages implicitly.
cd "$(dirname "$0")" || exit 1
if [ "$(uname -s)" != "Darwin" ]; then
    echo "此启动器仅用于 macOS。" >&2
    exit 1
fi
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -B -c 'import sys; sys.exit(not (sys.implementation.name == "cpython" and (3, 11) <= sys.version_info[:2] < (3, 15)))' 2>/dev/null; then
    echo "未找到本项目可用的 .venv（CPython 3.11–3.14）。先按 docs/handbook/工作台安装使用手册.md 创建环境。" >&2
    echo "首次安装：python3.14 -m venv .venv" >&2
    echo "安装依赖：.venv/bin/python -m pip install --require-hashes -r requirements-workbench.lock" >&2
    exit 1
fi
if ! .venv/bin/python -B -c 'import sys; sys.path.insert(0,"src"); from industry_workbench.cli import dependency_status; sys.exit(not dependency_status()["dependencies_locked"])'; then
    echo "依赖缺失或与锁文件不一致。请先执行：" >&2
    echo ".venv/bin/python -m pip install --require-hashes -r requirements-workbench.lock" >&2
    echo ".venv/bin/python -m pip check" >&2
    exit 1
fi
exec .venv/bin/python -B run_workbench.py serve "$@"
