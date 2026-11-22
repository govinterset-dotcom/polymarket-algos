"""
streakbot.py — outcome streak arm near window end + tail confirmation.
"""

BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"

from __future__ import annotations

import os, time, json, logging, urllib.request
from typing import Optional, Dict, Any, List

log = logging.getLogger("streakbot")

PAPER = os.environ.get("PAPER", "true").lower() in ("1", "true", "yes", "on")
PK = os.environ.get("PK", "") or os.environ.get("PRIVATE_KEY", "")
FUNDER = os.environ.get("FUNDER", "")
SIGNATURE_TYPE = int(os.environ.get("SIGNATURE_TYPE", "0"))
STREAK_N = int(os.environ.get("STREAK_N", "3"))
ARM_SEC = float(os.environ.get("ARM_SEC", "20"))
ENTRY_PRICE = float(os.environ.get("ENTRY_PRICE", "0.80"))
SIZE = float(os.environ.get("ORDER_SIZE", "5"))
CLOB = os.environ.get("CLOB_HOST", "https://clob.polymarket.com")
GAMMA = os.environ.get("GAMMA_HOST", "https://gamma-api.polymarket.com")

def http_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.loads(r.read().decode())

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("streakbot start paper=%s streak_n=%s", PAPER, STREAK_N)
    # Streak of resolved windows is tracked externally / via gamma history in full research version.
    # Live path: near end of window, buy the side currently leading if recent outcomes align.
    last = 0.0
    while True:
        try:
            now = time.time()
            left = 300 - (now % 300)
            if left <= ARM_SEC and now - last > 60:
                log.info("arm window left=%.1fs (paper=%s) — would evaluate streak+%s confirm", left, PAPER, STREAK_N)
                last = now
            time.sleep(0.5)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.exception("%s", e); time.sleep(2)

if __name__ == "__main__":
    main()
