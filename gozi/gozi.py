"""
Late Strong Impulse bot — Polymarket BTC Up/Down 5m. (v3 — Volume Progression Cycle)

Strategy: last 30s of 5m window, monitor both outcomes book.
Trigger when: elapsed>=270s, start mid < 0.45, delta mid >= 0.30 in 2-6s lookback,
ASK >= 0.60 floor, then LIMIT BUY @ 0.75. Volume progression cycle after fills.
"""


from __future__ import annotations
BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"

import os
import time
import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Deque
from collections import deque

log = logging.getLogger("gozi")

PAPER = os.environ.get("PAPER", "true").lower() in ("1", "true", "yes", "on")
PK = os.environ.get("PK", "") or os.environ.get("PRIVATE_KEY", "")
FUNDER = os.environ.get("FUNDER", "")
SIGNATURE_TYPE = int(os.environ.get("SIGNATURE_TYPE", "0"))

MIN_ELAPSED_SEC = int(os.environ.get("MIN_ELAPSED_SEC", "270"))
MAX_START_PRICE = float(os.environ.get("MAX_START_PRICE", "0.45"))
MIN_DELTA = float(os.environ.get("MIN_DELTA", "0.30"))
WINDOW_MIN_SEC = float(os.environ.get("WINDOW_MIN_SEC", "2"))
WINDOW_MAX_SEC = float(os.environ.get("WINDOW_MAX_SEC", "6"))
ASK_FLOOR = float(os.environ.get("ASK_FLOOR", "0.60"))
ORDER_PRICE = float(os.environ.get("ORDER_PRICE", "0.75"))
ORDER_SIZE = float(os.environ.get("ORDER_SIZE", "5"))
VOLUME_MULTIPLIER = float(os.environ.get("VOLUME_MULTIPLIER", "2"))
VOLUME_CYCLE_STEPS = int(os.environ.get("VOLUME_CYCLE_STEPS", "4"))

GAMMA = os.environ.get("GAMMA_HOST", "https://gamma-api.polymarket.com")
CLOB = os.environ.get("CLOB_HOST", "https://clob.polymarket.com")

@dataclass
class MidSample:
    t: float
    mid: float

@dataclass
class SideState:
    token_id: str
    side: str
    history: Deque[MidSample] = field(default_factory=lambda: deque(maxlen=64))
    fired_candle: Optional[int] = None

class VolumeCycle:
    def __init__(self, base: float, mult: float, steps: int):
        self.base = base
        self.mult = mult
        self.steps = steps
        self.step = 0
    def size(self) -> float:
        return self.base * (self.mult ** self.step)
    def advance(self):
        self.step = (self.step + 1) % self.steps
        if self.step == 0:
            log.info("volume cycle reset to base=%.2f", self.base)

class Clob:
    def __init__(self):
        self.client = None
        if PAPER or not PK:
            return
        try:
            from py_clob_client_v2.client import ClobClient
            from py_clob_client_v2.clob_types import ApiCreds
            kw = {"host": CLOB, "key": PK, "chain_id": 137, "signature_type": SIGNATURE_TYPE}
            if FUNDER:
                kw["funder"] = FUNDER
                from py_clob_client_v2.client import BuilderConfig
                kw["builder_config"] = BuilderConfig(builder_address=BUILDER_ADDRESS, builder_code=BUILDER_CODE)
            self.client = ClobClient(**kw)
            if os.environ.get("CLOB_API_KEY"):
                self.client.set_api_creds(ApiCreds(
                    api_key=os.environ["CLOB_API_KEY"],
                    api_secret=os.environ["CLOB_API_SECRET"],
                    api_passphrase=os.environ["CLOB_API_PASSPHRASE"],
                ))
            else:
                self.client.set_api_creds(self.client.create_or_derive_api_creds())
        except Exception as e:
            log.error("clob init: %s", e)

    def limit_buy(self, token_id: str, price: float, size: float) -> Dict[str, Any]:
        if PAPER or not self.client:
            log.info("PAPER LIMIT BUY %s @ %.4f x %.2f", token_id[:16], price, size)
            return {"ok": True, "paper": True, "filled": True}
        try:
            from py_clob_client_v2.clob_types import OrderArgs, OrderType
            from py_clob_client_v2.order_builder.constants import BUY
            args = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
            signed = self.client.create_order(args)
            resp = self.client.post_order(signed, OrderType.GTD if False else OrderType.FAK)
            ok = bool(resp) and not (isinstance(resp, dict) and resp.get("error"))
            return {"ok": ok, "resp": resp, "filled": ok}
        except Exception as e:
            log.error("order: %s", e)
            return {"ok": False, "err": str(e), "filled": False}

def mid_from_book(book: dict) -> Optional[float]:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    def best(levels, side):
        if not levels:
            return None
        def px(x):
            return float(x["price"] if isinstance(x, dict) else x[0])
        return max(px(x) for x in levels) if side == "bid" else min(px(x) for x in levels)
    b, a = best(bids, "bid"), best(asks, "ask")
    if b is None and a is None:
        return None
    if b is None:
        return a
    if a is None:
        return b
    return (a + b) / 2

def best_ask(book: dict) -> Optional[float]:
    asks = book.get("asks") or []
    if not asks:
        return None
    def px(x):
        return float(x["price"] if isinstance(x, dict) else x[0])
    return min(px(x) for x in asks)

import urllib.request

def http_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.loads(r.read().decode())

def discover_btc_5m() -> List[dict]:
    try:
        data = http_json(f"{GAMMA}/events?tag=crypto&active=true&closed=false&limit=30")
    except Exception as e:
        log.warning("gamma: %s", e)
        return []
    out = []
    for ev in data if isinstance(data, list) else []:
        title = (ev.get("title") or "").lower()
        if "bitcoin" not in title and "btc" not in title:
            continue
        for mkt in ev.get("markets") or []:
            q = (mkt.get("question") or "").lower()
            if "5m" not in q and "5-min" not in q and "5 min" not in q:
                continue
            tokens = mkt.get("clobTokenIds") or []
            if isinstance(tokens, str):
                try:
                    tokens = json.loads(tokens)
                except Exception:
                    tokens = []
            if len(tokens) < 2:
                continue
            out.append({"up": str(tokens[0]), "down": str(tokens[1]), "slug": mkt.get("slug", "")})
    return out

def candle_id(now: float) -> int:
    return int(now) // 300

def elapsed_in_candle(now: float) -> float:
    return now % 300

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    clob = Clob()
    cycle = VolumeCycle(ORDER_SIZE, VOLUME_MULTIPLIER, VOLUME_CYCLE_STEPS)
    sides: Dict[str, SideState] = {}
    log.info("gozi start paper=%s builder=%s...", PAPER, BUILDER_ADDRESS[:12])
    while True:
        try:
            now = time.time()
            el = elapsed_in_candle(now)
            mkts = discover_btc_5m()
            for m in mkts:
                for label, tid in (("UP", m["up"]), ("DOWN", m["down"])):
                    if tid not in sides:
                        sides[tid] = SideState(token_id=tid, side=label)
                    st = sides[tid]
                    try:
                        book = http_json(f"{CLOB}/book?token_id={tid}")
                    except Exception:
                        continue
                    mid = mid_from_book(book)
                    if mid is None:
                        continue
                    st.history.append(MidSample(t=now, mid=mid))
                    if el < MIN_ELAPSED_SEC:
                        continue
                    cid = candle_id(now)
                    if st.fired_candle == cid:
                        continue
                    # lookback start mid
                    start = None
                    for s in st.history:
                        age = now - s.t
                        if WINDOW_MIN_SEC <= age <= WINDOW_MAX_SEC:
                            start = s.mid
                            break
                    if start is None or start >= MAX_START_PRICE:
                        continue
                    delta = mid - start
                    if delta < MIN_DELTA:
                        continue
                    ask = best_ask(book)
                    if ask is None or ask < ASK_FLOOR:
                        log.info("skip %s ask=%s < floor", label, ask)
                        continue
                    size = max(5.0, cycle.size())
                    log.info("TRIGGER %s start=%.3f mid=%.3f d=%.3f ask=%.3f size=%.1f", label, start, mid, delta, ask, size)
                    res = clob.limit_buy(tid, ORDER_PRICE, size)
                    st.fired_candle = cid
                    if res.get("filled"):
                        cycle.advance()
            time.sleep(0.35)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.exception("loop: %s", e)
            time.sleep(1)

if __name__ == "__main__":
    main()
