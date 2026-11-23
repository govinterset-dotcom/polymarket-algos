#!/usr/bin/env python3
"""algorithms/preladder.py — runs the full bot from ../preladder/preladder.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TARGET = _ROOT / "preladder" / "preladder.py"
if not _TARGET.is_file():
    sys.stderr.write(f"missing {_TARGET}\n")
    sys.exit(1)
sys.argv[0] = str(_TARGET)
runpy.run_path(str(_TARGET), run_name="__main__")
