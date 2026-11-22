"""
secunda.py — second-level timing / secondary signal module.
"""
from __future__ import annotations

BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"

import os
import time
import logging
from typing import Optional, Dict, Any, List

log = logging.getLogger("secunda")

PAPER = os.environ.get("PAPER", "true").lower() in ("1", "true", "yes", "on")
PK = os.environ.get("PK", "")
FUNDER = os.environ.get("FUNDER", "")

class SecondClock:
    """Track per-second mid samples for a market window."""
    def __init__(self, start: int, length: int):
        self.start = start
        self.length = length
        self.path: List[Optional[float]] = [None] * length

    def sample(self, now: float, mid: Optional[float]) -> None:
        el = int(now) - self.start
        if 0 <= el < self.length and mid is not None:
            self.path[el] = mid

    def slope(self, look: int = 5) -> Optional[float]:
        pts = [(i, v) for i, v in enumerate(self.path) if v is not None]
        if len(pts) < 2:
            return None
        pts = pts[-look:]
        if len(pts) < 2:
            return None
        (i0, v0), (i1, v1) = pts[0], pts[-1]
        dt = max(1, i1 - i0)
        return (v1 - v0) / dt

def main():
    logging.basicConfig(level=logging.INFO)
    log.info("secunda paper=%s builder=%s...", PAPER, BUILDER_ADDRESS[:12])
    print("secunda secondary signal module — configure via env, Builder embedded")

if __name__ == "__main__":
    main()
