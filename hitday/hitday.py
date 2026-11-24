"""
hitday.py — daily hit-price ladders vs Binance 1m reference.
"""


from __future__ import annotations
BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"

import os, time, json, logging, urllib.request
from typing import Optional, Any, List

log = logging.getLogger("hitday")

PAPER = os.environ.get("PAPER", "true").lower() in ("1", "true", "yes", "on")
PK = os.environ.get("PK", "") or os.environ.get("PRIVATE_KEY", "")
FUNDER = os.environ.get("FUNDER", "")
ENTRY_PRICE = float(os.environ.get("ENTRY_PRICE", "0.55"))
SIZE = float(os.environ.get("ORDER_SIZE", "5"))
CLOB = os.environ.get("CLOB_HOST", "https://clob.polymarket.com")
GAMMA = os.environ.get("GAMMA_HOST", "https://gamma-api.polymarket.com")

def http_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.loads(r.read().decode())

def binance_btc() -> Optional[float]:
    try:
        d = http_json("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
        return float(d["price"])
    except Exception:
        return None

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("hitday start paper=%s", PAPER)
    while True:
        try:
            px = binance_btc()
            if px:
                log.info("BTC ref=%.2f — scan daily hit ladders on gamma (paper=%s)", px, PAPER)
            time.sleep(15)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.exception("%s", e); time.sleep(5)

if __name__ == "__main__":
    main()
