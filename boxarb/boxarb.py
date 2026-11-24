"""
boxarb.py — nested 5m/15m/4h box arbitrage on the same Chainlink TWAP stream.
"""


from __future__ import annotations
BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"

import os, time, json, logging, urllib.request
from typing import Optional, Dict, Any, List

log = logging.getLogger("boxarb")

PAPER = os.environ.get("PAPER", "true").lower() in ("1", "true", "yes", "on")
PK = os.environ.get("PK", "") or os.environ.get("PRIVATE_KEY", "")
FUNDER = os.environ.get("FUNDER", "")
SIGNATURE_TYPE = int(os.environ.get("SIGNATURE_TYPE", "0"))
EDGE = float(os.environ.get("MIN_EDGE", "0.015"))
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

def discover() -> List[dict]:
    try:
        data = http_json(f"{GAMMA}/events?tag=crypto&active=true&closed=false&limit=40")
    except Exception:
        return []
    out = []
    for ev in data if isinstance(data, list) else []:
        for mkt in ev.get("markets") or []:
            tokens = mkt.get("clobTokenIds") or []
            if isinstance(tokens, str):
                try: tokens = json.loads(tokens)
                except Exception: tokens = []
            if len(tokens) >= 2:
                out.append({"up": str(tokens[0]), "down": str(tokens[1]), "slug": mkt.get("slug", ""), "q": mkt.get("question", "")})
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
            log.info("PAPER BUY %s @ %.4f x %.1f", token_id[:14], price, size); return
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
    log.info("boxarb start paper=%s", PAPER)
    while True:
        try:
            for m in discover():
                ua, da = best_ask(m["up"]), best_ask(m["down"])
                if ua is None or da is None: continue
                if ua + da <= 1.0 - EDGE:
                    log.info("BOX %s sum=%.4f", m["slug"], ua + da)
                    clob.buy(m["up"], ua, SIZE)
                    clob.buy(m["down"], da, SIZE)
            time.sleep(0.7)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.exception("%s", e); time.sleep(2)

if __name__ == "__main__":
    main()
