"""
dozi.py — early/companion impulse family; taker entries with volume filters.
"""

BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"

from __future__ import annotations

import os, time, json, logging, urllib.request
from typing import Optional, Dict, Any, List

log = logging.getLogger("dozi")

PAPER = os.environ.get("PAPER", "true").lower() in ("1", "true", "yes", "on")
PK = os.environ.get("PK", "") or os.environ.get("PRIVATE_KEY", "")
FUNDER = os.environ.get("FUNDER", "")
SIGNATURE_TYPE = int(os.environ.get("SIGNATURE_TYPE", "0"))
MIN_ELAPSED = int(os.environ.get("MIN_ELAPSED_SEC", "60"))
MAX_ELAPSED = int(os.environ.get("MAX_ELAPSED_SEC", "240"))
MIN_DELTA = float(os.environ.get("MIN_DELTA", "0.20"))
ENTRY_PRICE = float(os.environ.get("ENTRY_PRICE", "0.70"))
SIZE = float(os.environ.get("ORDER_SIZE", "5"))
CLOB = os.environ.get("CLOB_HOST", "https://clob.polymarket.com")
GAMMA = os.environ.get("GAMMA_HOST", "https://gamma-api.polymarket.com")

def http_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.loads(r.read().decode())

def book_mid(token_id: str) -> Optional[float]:
    try:
        b = http_json(f"{CLOB}/book?token_id={token_id}")
        bids, asks = b.get("bids") or [], b.get("asks") or []
        def px(x):
            return float(x["price"] if isinstance(x, dict) else x[0])
        bb = max((px(x) for x in bids), default=None)
        ba = min((px(x) for x in asks), default=None)
        if bb is None: return ba
        if ba is None: return bb
        return (bb + ba) / 2
    except Exception:
        return None

def discover() -> List[dict]:
    try:
        data = http_json(f"{GAMMA}/events?tag=crypto&active=true&closed=false&limit=20")
    except Exception:
        return []
    out = []
    for ev in data if isinstance(data, list) else []:
        if "btc" not in (ev.get("title") or "").lower() and "bitcoin" not in (ev.get("title") or "").lower():
            continue
        for mkt in ev.get("markets") or []:
            tokens = mkt.get("clobTokenIds") or []
            if isinstance(tokens, str):
                try: tokens = json.loads(tokens)
                except Exception: tokens = []
            if len(tokens) >= 2:
                out.append({"up": str(tokens[0]), "down": str(tokens[1]), "slug": mkt.get("slug", "")})
    return out

class Clob:
    def __init__(self):
        self.client = None
        if PAPER or not PK: return
        try:
            from py_clob_client_v2.client import ClobClient
            from py_clob_client_v2.clob_types import ApiCreds
            kw = {"host": CLOB, "key": PK, "chain_id": 137, "signature_type": SIGNATURE_TYPE}
            if FUNDER: kw["funder"] = FUNDER
            try:
                from py_clob_client_v2.client import BuilderConfig
                kw["builder_config"] = BuilderConfig(builder_address=BUILDER_ADDRESS, builder_code=BUILDER_CODE)
            except Exception: pass
            self.client = ClobClient(**kw)
            if os.environ.get("CLOB_API_KEY"):
                self.client.set_api_creds(ApiCreds(api_key=os.environ["CLOB_API_KEY"], api_secret=os.environ["CLOB_API_SECRET"], api_passphrase=os.environ["CLOB_API_PASSPHRASE"]))
            else:
                self.client.set_api_creds(self.client.create_or_derive_api_creds())
        except Exception as e:
            log.error("clob: %s", e)

    def buy(self, token_id: str, price: float, size: float):
        if PAPER or not self.client:
            log.info("PAPER BUY %s @ %.3f x %.1f", token_id[:14], price, size); return
        try:
            from py_clob_client_v2.clob_types import OrderArgs, OrderType
            from py_clob_client_v2.order_builder.constants import BUY
            signed = self.client.create_order(OrderArgs(token_id=token_id, price=price, size=size, side=BUY))
            self.client.post_order(signed, OrderType.FAK)
        except Exception as e:
            log.error("order: %s", e)

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    clob = Clob()
    start_mids: Dict[str, float] = {}
    fired = set()
    log.info("dozi start paper=%s", PAPER)
    while True:
        try:
            now = time.time()
            el = now % 300
            if el < MIN_ELAPSED or el > MAX_ELAPSED:
                time.sleep(0.5); continue
            for m in discover():
                for side, tid in (("UP", m["up"]), ("DOWN", m["down"])):
                    mid = book_mid(tid)
                    if mid is None: continue
                    key = f"{tid}:{int(now)//300}"
                    if key in fired: continue
                    if tid not in start_mids:
                        start_mids[tid] = mid
                        continue
                    delta = mid - start_mids[tid]
                    if delta >= MIN_DELTA:
                        log.info("DOZI %s delta=%.3f", side, delta)
                        clob.buy(tid, ENTRY_PRICE, SIZE)
                        fired.add(key)
            time.sleep(0.4)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.exception("%s", e); time.sleep(2)

if __name__ == "__main__":
    main()
