"""
BTC Impulse v4 — impulse detection on second bars / agg trades.
"""


from __future__ import annotations
BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"
import os, logging
from pathlib import Path

log = logging.getLogger("btc_impulse_v4")

def main():
    logging.basicConfig(level=logging.INFO)
    log.info("btc_impulse_v4 builder=%s...", BUILDER_ADDRESS[:12])
    print("Provide agg trade CSVs / parquet via env paths; see README.")

if __name__ == "__main__":
    main()
