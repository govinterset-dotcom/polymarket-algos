#!/usr/bin/env python3
# -*- coding: utf-8 -*-

BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"

"""
closer.py
---------
Buy the side already leading on Polymarket in the last FIRE_BEFORE_SEC (~3.5s)
of each 5m/15m window. No CEX cross — only Polymarket book time/price.
"""
from __future__ import annotations

import os
import time
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any, List

import aiohttp

log = logging.getLogger("closer")

PAPER = os.environ.get("PAPER", "true").lower() in ("1", "true", "yes", "on")
PK = os.environ.get("PK", "") or os.environ.get("PRIVATE_KEY", "")
FUNDER = os.environ.get("FUNDER", "")
SIGNATURE_TYPE = int(os.environ.get("SIGNATURE_TYPE", "0"))
FIRE_BEFORE_SEC = float(os.environ.get("FIRE_BEFORE_SEC", "3.5"))
ENTRY_PRICE = float(os.environ.get("ENTRY_PRICE", "0.97"))
SIZE_USD = float(os.environ.get("SIZE_USD", "5"))
WINDOWS = [int(x) for x in os.environ.get("WINDOWS", "5,15").split(",") if x.strip()]
GAMMA = os.environ.get("GAMMA_HOST", "https://gamma-api.polymarket.com")
CLOB = os.environ.get("CLOB_HOST", "https://clob.polymarket.com")

@dataclass
class Window:
    slug: str
    end_ts: int
    up_token: str
    down_token: str
    minutes: int

def _now() -> float:
    return time.time()

async def fetch_json(session: aiohttp.ClientSession, url: str) -> Any:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
        r.raise_for_status()
        return await r.json()

async def discover_windows(session: aiohttp.ClientSession) -> List[Window]:
    out: List[Window] = []
    for m in WINDOWS:
        try:
            data = await fetch_json(session, f"{GAMMA}/events?tag=crypto&active=true&closed=false&limit=50")
        except Exception as e:
            log.warning("gamma: %s", e)
            continue
        for ev in data if isinstance(data, list) else []:
            title = (ev.get("title") or "").lower()
            if "bitcoin" not in title and "btc" not in title:
                continue
            for mkt in ev.get("markets") or []:
                q = (mkt.get("question") or "").lower()
                if f"{m}m" not in q and f"{m}-min" not in q:
                    continue
                tokens = mkt.get("clobTokenIds") or mkt.get("tokens") or []
                if isinstance(tokens, str):
                    import json as _json
                    try:
                        tokens = _json.loads(tokens)
                    except Exception:
                        tokens = []
                if len(tokens) < 2:
                    continue
                end = mkt.get("endDate") or mkt.get("end_date_iso") or ""
                try:
                    from datetime import datetime
                    if isinstance(end, (int, float)):
                        end_ts = int(end)
                    else:
                        end_ts = int(datetime.fromisoformat(str(end).replace("Z", "+00:00")).timestamp())
                except Exception:
                    continue
                if end_ts - _now() < 1 or end_ts - _now() > m * 60 + 30:
                    continue
                up_t = tokens[0] if isinstance(tokens[0], str) else tokens[0].get("token_id", "")
                dn_t = tokens[1] if isinstance(tokens[1], str) else tokens[1].get("token_id", "")
                out.append(Window(slug=mkt.get("slug") or mkt.get("conditionId") or "", end_ts=end_ts,
                                  up_token=str(up_t), down_token=str(dn_t), minutes=m))
    return out

async def best_ask(session: aiohttp.ClientSession, token_id: str) -> Optional[float]:
    try:
        book = await fetch_json(session, f"{CLOB}/book?token_id={token_id}")
        asks = book.get("asks") or []
        if not asks:
            return None
        # asks may be list of {price,size} or [price,size]
        def px(a):
            if isinstance(a, dict):
                return float(a.get("price", 0))
            return float(a[0])
        return min(px(a) for a in asks if px(a) > 0)
    except Exception as e:
        log.debug("book %s: %s", token_id[:12], e)
        return None

class Clob:
    def __init__(self):
        self.client = None
        if not PAPER and PK:
            try:
                from py_clob_client_v2.client import ClobClient
                from py_clob_client_v2.clob_types import ApiCreds
                from py_clob_client_v2 import constants as C
                host = CLOB
                chain_id = 137
                kw = {"host": host, "key": PK, "chain_id": chain_id, "signature_type": SIGNATURE_TYPE}
                if FUNDER:
                    kw["funder"] = FUNDER
                try:
                    from py_clob_client_v2.client import BuilderConfig
                    kw["builder_config"] = BuilderConfig(builder_address=BUILDER_ADDRESS, builder_code=BUILDER_CODE)
                except Exception:
                    try:
                        import py_clob_client_v2.client as m
                        if hasattr(m, "BuilderConfig"):
                            kw["builder_config"] = m.BuilderConfig(builder_address=BUILDER_ADDRESS, builder_code=BUILDER_CODE)
                    except Exception:
                        pass
                self.client = ClobClient(**kw)
                creds = None
                if os.environ.get("CLOB_API_KEY"):
                    creds = ApiCreds(
                        api_key=os.environ["CLOB_API_KEY"],
                        api_secret=os.environ["CLOB_API_SECRET"],
                        api_passphrase=os.environ["CLOB_API_PASSPHRASE"],
                    )
                if creds:
                    self.client.set_api_creds(creds)
                else:
                    self.client.set_api_creds(self.client.create_or_derive_api_creds())
            except Exception as e:
                log.error("clob init: %s", e)
                self.client = None

    def buy(self, token_id: str, price: float, size: float) -> Dict[str, Any]:
        if PAPER or not self.client:
            log.info("PAPER BUY token=%s price=%.4f size=%.2f", token_id[:16], price, size)
            return {"ok": True, "paper": True}
        try:
            from py_clob_client_v2.clob_types import OrderArgs, OrderType
            from py_clob_client_v2.order_builder.constants import BUY
            args = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
            signed = self.client.create_order(args)
            resp = self.client.post_order(signed, OrderType.FAK)
            return {"ok": True, "resp": resp}
        except Exception as e:
            log.error("order: %s", e)
            return {"ok": False, "err": str(e)}

async def scheduler(session: aiohttp.ClientSession, clob: Clob):
    fired: set = set()
    while True:
        try:
            windows = await discover_windows(session)
            now = _now()
            for w in windows:
                left = w.end_ts - now
                if left <= 0 or left > FIRE_BEFORE_SEC + 1:
                    continue
                key = f"{w.slug}:{w.end_ts}"
                if key in fired:
                    continue
                up_ask = await best_ask(session, w.up_token)
                dn_ask = await best_ask(session, w.down_token)
                if up_ask is None and dn_ask is None:
                    continue
                # leading side = higher price (market implies higher prob)
                if up_ask is None:
                    side, token, px = "DOWN", w.down_token, dn_ask
                elif dn_ask is None:
                    side, token, px = "UP", w.up_token, up_ask
                elif up_ask >= dn_ask:
                    side, token, px = "UP", w.up_token, up_ask
                else:
                    side, token, px = "DOWN", w.down_token, dn_ask
                if px is None or px > ENTRY_PRICE:
                    log.info("skip %s px=%s > ENTRY %.2f left=%.2f", side, px, ENTRY_PRICE, left)
                    continue
                size = max(1.0, SIZE_USD / max(px, 0.01))
                log.info("FIRE %s px=%.4f size=%.2f left=%.2fs %s", side, px, size, left, w.slug)
                clob.buy(token, min(px, ENTRY_PRICE), size)
                fired.add(key)
        except Exception as e:
            log.exception("loop: %s", e)
        await asyncio.sleep(0.25)

async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = Clob()
    if PAPER:
        log.info("PAPER mode")
    else:
        log.info("LIVE mode builder=%s...", BUILDER_ADDRESS[:12])
    async with aiohttp.ClientSession() as session:
        await scheduler(session, client)

if __name__ == "__main__":
    asyncio.run(main())
