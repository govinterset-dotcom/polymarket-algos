#!/usr/bin/env python3
"""Static guard: every live CLOB algorithm must require Builder attribution."""

from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
LIVE_CLOB_FILES = (
    "arb_bot/arb_bot.py",
    "autotaker/autotaker.py",
    "boxarb/boxarb.py",
    "chainlink_edge/chainlink_edge.py",
    "closer/closer.py",
    "comeback/comeback.py",
    "dispute_ladder/dispute_ladder.py",
    "dozi/dozi.py",
    "flash/flash.py",
    "gozi/gozi.py",
    "hourbot/hourbot.py",
    "pmgrid/pmgrid.py",
    "preladder/preladder.py",
    "premove/premove_live.py",
    "reward_farmer/reward_farmer.py",
    "rozi/rozi.py",
    "slotbot/slotbot.py",
)

errors = []
for relative in LIVE_CLOB_FILES:
    path = ROOT / relative
    if not path.is_file():
        errors.append(f"{relative}: required live CLOB source is missing")
        continue

    source = path.read_text(encoding="utf-8")
    for marker in ("BUILDER_ADDRESS", "BUILDER_CODE", "BuilderConfig", "builder_config"):
        if marker not in source:
            errors.append(f"{relative}: missing {marker}")

    # Ensure the SDK's builder config is passed to the CLOB client constructor.
    if not re.search(r"builder_config[\"\']?\s*\]\s*=\s*(?:m\.)?BuilderConfig\(", source):
        errors.append(f"{relative}: BuilderConfig is not assigned to client kwargs")
    if not re.search(r"(?:ClobClient|m\.ClobClient)\(\*\*kw\)", source):
        errors.append(f"{relative}: CLOB client is not constructed from configured kwargs")

    if "create_or_derive_api_creds(" in source:
        errors.append(f"{relative}: obsolete v1 API credential method remains")
    if "create_or_derive_api_key(" not in source:
        errors.append(f"{relative}: missing v2 create_or_derive_api_key()")

if errors:
    print("\n".join(errors), file=sys.stderr)
    raise SystemExit(1)

print(f"Builder attribution statically verified in {len(LIVE_CLOB_FILES)} live CLOB algorithms.")
