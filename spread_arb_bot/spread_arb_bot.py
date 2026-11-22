"""
spread_arb_bot.py — Up-BTC vs Up-ETH spread on the same live window.
"""

BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"

from __future__ import annotations

import os, time, json, logging, urllib.request
from typing import Optional, Any

log = logging.getLogger("spread_arb_bot")

PAPER = os.environ.get("PAPER", "true").lower() in ("1", "true", "yes", "on")
PK = os.environ.get("PK", "") or os.environ.get("PRIVATE_KEY", "")
FUNDER = os.environ.get("FUNDER", "")
EDGE = float(os.environ.get("MIN_EDGE", "0.03"))
SIZE = float(os.environ.get("ORDER_SIZE", "5"))
CLOB = os.environ.get("CLOB_HOST", "https://clob.polymarket.com")
GAMMA = os.environ.get("GAMMA_HOST", "https://gamma-api.polymarket.com")

def http_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.loads(r.read().decode())

def best_ask(token_id: str) -> Optional[float]:
    try:
        b = http_json(f"{CLOB}/book?token_id={token_id}")
        asks = b.get("asks") or []
        if not asks: return None
        def px(x): return float(x["price"] if isinstance(x, dict) else x[0])
        return min(px(x) for x in asks)
    except Exception:
        return None

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("spread_arb_bot start paper=%s edge=%.3f", PAPER, EDGE)
    while True:
        try:
            # Pair BTC/ETH up tokens from concurrent windows and trade relative mispricing.
            time.sleep(1.0)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.exception("%s", e); time.sleep(2)

if __name__ == "__main__":
    main()
