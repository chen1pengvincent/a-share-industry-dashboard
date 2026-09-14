#!/bin/sh
set -eu
SWIVD_PACKAGE_ROOT=$(CDPATH= cd -P "$(/usr/bin/dirname "$0")" && pwd)
if [ ! -x "$SWIVD_PACKAGE_ROOT/runtime/bin/python3.14" ]; then
    echo "ERROR: BUNDLED_PYTHON_UNAVAILABLE. Extract the complete matching Mac package." >&2
    exit 1
fi
exec "$SWIVD_PACKAGE_ROOT/runtime/bin/python3.14" -I -B -X utf8 "$SWIVD_PACKAGE_ROOT/launcher.py" "$@"
