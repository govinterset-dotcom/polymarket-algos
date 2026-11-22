#!/usr/bin/env python3
"""Reconstruct all_algos_src.zip from archive/part_XX.b64 and unpack into repo root."""
import base64, zipfile
from pathlib import Path

root = Path(__file__).resolve().parent
parts_dir = root / "archive"
parts = sorted(parts_dir.glob("part_*.b64"))
if not parts:
    raise SystemExit(f"no parts in {parts_dir} — push archive/part_*.b64 first")
b64 = "".join(p.read_text() for p in parts)
data = base64.b64decode(b64)
zip_path = root / "all_algos_src.zip"
zip_path.write_bytes(data)
print(f"wrote {zip_path} ({len(data)} bytes)")
with zipfile.ZipFile(zip_path) as zf:
    zf.extractall(root)
    print(f"extracted {len(zf.namelist())} entries")
print("done — full algorithm sources are in place")
