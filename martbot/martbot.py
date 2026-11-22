"""
martbot.py — selected slots after two losses; size doubling up to three attempts.
"""

BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"

from __future__ import annotations
import os, time, logging

log = logging.getLogger("martbot")
PAPER = os.environ.get("PAPER", "true").lower() in ("1", "true", "yes", "on")
BASE_SIZE = float(os.environ.get("ORDER_SIZE", "5"))
MAX_STEP = int(os.environ.get("MAX_STEP", "3"))
MULT = float(os.environ.get("MULT", "2.0"))

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    step = 0
    losses = 0
    log.info("martbot start paper=%s base=%.1f max_step=%d", PAPER, BASE_SIZE, MAX_STEP)
    while True:
        try:
            size = BASE_SIZE * (MULT ** step)
            log.info("ready size=%.1f step=%d losses=%d (wire to slot resolution journal)", size, step, losses)
            # Full research version advances step on loss, resets on win
            time.sleep(30)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.exception("%s", e); time.sleep(5)

if __name__ == "__main__":
    main()
