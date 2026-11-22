"""
slotbot.py — stable hourly slots; early tail resolution + order hygiene.
"""

BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"

from __future__ import annotations
import os, time, json, logging, urllib.request
from typing import Any

log = logging.getLogger("slotbot")
PAPER = os.environ.get("PAPER", "true").lower() in ("1", "true", "yes", "on")
PK = os.environ.get("PK", "") or os.environ.get("PRIVATE_KEY", "")
FUNDER = os.environ.get("FUNDER", "")
SIGNATURE_TYPE = int(os.environ.get("SIGNATURE_TYPE", "0"))
ENTRY_PRICE = float(os.environ.get("ENTRY_PRICE", "0.92"))
SIZE = float(os.environ.get("ORDER_SIZE", "5"))
TAIL_SEC = float(os.environ.get("TAIL_SEC", "90"))
CLOB = os.environ.get("CLOB_HOST", "https://clob.polymarket.com")
GAMMA = os.environ.get("GAMMA_HOST", "https://gamma-api.polymarket.com")

def http_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.loads(r.read().decode())

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
    log.info("slotbot start paper=%s tail=%.0fs", PAPER, TAIL_SEC)
    while True:
        try:
            # Slot windows: act in last TAIL_SEC of hour
            left = 3600 - (time.time() % 3600)
            if left <= TAIL_SEC:
                log.info("slot tail left=%.0fs — evaluate leading side", left)
            time.sleep(2)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.exception("%s", e); time.sleep(3)

if __name__ == "__main__":
    main()
