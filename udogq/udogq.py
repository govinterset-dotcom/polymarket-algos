"""
udogq.py — underdog signal from naked quotes (distance vs remaining sigma).
"""


from __future__ import annotations
BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"
import os, logging
log = logging.getLogger("udogq")

def main():
    logging.basicConfig(level=logging.INFO)
    log.info("udogq builder=%s...", BUILDER_ADDRESS[:12])
    print("Research runner: python3 udogq.py <klines_dir> btc,eth 1.5")

if __name__ == "__main__":
    main()
