"""
secunda.py — second-level timing / secondary signal module.
"""


from __future__ import annotations
BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"
import os, time, logging
from typing import Optional, List
log = logging.getLogger("secunda")
PAPER = os.environ.get("PAPER", "true").lower() in ("1", "true", "yes", "on")

class SecondClock:
    def __init__(self, start: int, length: int):
        self.start = start
        self.length = length
        self.path: List[Optional[float]] = [None] * length
    def sample(self, now: float, mid: Optional[float]) -> None:
        el = int(now) - self.start
        if 0 <= el < self.length and mid is not None:
            self.path[el] = mid

def main():
    logging.basicConfig(level=logging.INFO)
    log.info("secunda start paper=%s builder=%s...", PAPER, BUILDER_ADDRESS[:12])
    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()
