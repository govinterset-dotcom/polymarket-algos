#!/usr/bin/env python3
"""algorithms/rozi.py — runs the full bot from ../rozi/rozi.py"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TARGET = _ROOT / "rozi" / "rozi.py"
if not _TARGET.is_file():
    sys.stderr.write(f"missing {_TARGET}\n")
    sys.exit(1)
sys.argv[0] = str(_TARGET)
runpy.run_path(str(_TARGET), run_name="__main__")
