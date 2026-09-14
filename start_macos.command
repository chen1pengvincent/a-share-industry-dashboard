#!/bin/sh
cd "$(dirname "$0")" || exit 1

if [ -e .venv ] || [ -L .venv ]; then
    if ! .venv/bin/python -B -c 'import sys; sys.exit(not (sys.implementation.name == "cpython" and (3, 11) <= sys.version_info[:2] < (3, 15)))' 2>/dev/null; then
        echo "The project .venv is unusable. Recreate it with Python 3.11-3.14 as described in README.md." >&2
        exit 1
    fi
    exec .venv/bin/python -B run_dashboard.py serve "$@"
fi

for swivd_python in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$swivd_python" >/dev/null 2>&1 &&
        "$swivd_python" -B -c 'import sys; sys.exit(not (sys.implementation.name == "cpython" and (3, 11) <= sys.version_info[:2] < (3, 15)))' 2>/dev/null; then
        exec "$swivd_python" -B run_dashboard.py serve "$@"
    fi
done
echo "Python 3.11-3.14 is required. Create .venv and install requirements.lock as described in README.md." >&2
exit 1
