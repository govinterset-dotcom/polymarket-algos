"""
polymoly.py — experimental multi-market module.
"""

BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"

from __future__ import annotations
import os, time, logging
log = logging.getLogger("polymoly")
PAPER = os.environ.get("PAPER", "true").lower() in ("1", "true", "yes", "on")

def main():
    logging.basicConfig(level=logging.INFO)
    log.info("polymoly start paper=%s builder=%s...", PAPER, BUILDER_ADDRESS[:12])
    while True:
        time.sleep(5)

if __name__ == "__main__":
    main()
