"""
pmgrid.py — limit-order grid on Polymarket Up/Down (CLOB v2).
"""

BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"

from __future__ import annotations
import os, time, logging
log = logging.getLogger("pmgrid")
PAPER = os.environ.get("PAPER", "true").lower() in ("1", "true", "yes", "on")

def main():
    logging.basicConfig(level=logging.INFO)
    log.info("pmgrid start paper=%s builder=%s...", PAPER, BUILDER_ADDRESS[:12])
    while True:
        time.sleep(3)

if __name__ == "__main__":
    main()
