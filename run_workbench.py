#!/usr/bin/env python3
"""Run the unified application from this checkout, without installing a wheel."""
from pathlib import Path
import sys

sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(SOURCE))

from industry_workbench.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
