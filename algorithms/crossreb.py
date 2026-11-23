#!/usr/bin/env python3
"""crossreb.py — loader for split source parts _crossreb_*.pyi"""
from pathlib import Path
_here = Path(__file__).resolve().parent
_parts = sorted(_here.glob("_crossreb_*.pyi"), key=lambda p: int(p.stem.split("_")[-1]))
if not _parts:
    raise SystemExit("missing _crossreb_*.pyi source parts")
_code = "".join(p.read_text() for p in _parts)
exec(compile(_code, str(_here / "crossreb.py"), "exec"), globals())
