"""
crossreb.py — CROSS + rebound book signals on 15m, taker.
"""

BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"

from __future__ import annotations

import os, time, json, logging, urllib.request
from typing import Optional, Any, List, Dict

log = logging.getLogger("crossreb")

PAPER = os.environ.get("PAPER", "true").lower() in ("1", "true", "yes", "on")
PK = os.environ.get("PK", "") or os.environ.get("PRIVATE_KEY", "")
FUNDER = os.environ.get("FUNDER", "")
SIGNATURE_TYPE = int(os.environ.get("SIGNATURE_TYPE", "0"))
ENTRY_PRICE = float(os.environ.get("ENTRY_PRICE", "0.68"))
SIZE = float(os.environ.get("ORDER_SIZE", "5"))
CLOB = os.environ.get("CLOB_HOST", "https://clob.polymarket.com")
GAMMA = os.environ.get("GAMMA_HOST", "https://gamma-api.polymarket.com")

def http_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.loads(r.read().decode())

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("crossreb start paper=%s", PAPER)
    while True:
        try:
            # Full research version monitors deep lows + cross of mids; here we keep the live loop skeleton with Builder.
            log.debug("scan 15m books...")
            time.sleep(1.0)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.exception("%s", e); time.sleep(2)

if __name__ == "__main__":
    main()
