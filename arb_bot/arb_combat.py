#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
arb_combat.py — боевой complementary arb (YES + NO < 1 − buffer)
================================================================
Широкие спреды only. Market WebSocket (проверенный паттерн из rozi/lena).
Параллельные FAK обе ноги. 5m/15m/1h × BTC/ETH/SOL/XRP.

  EXECUTION_ENABLED=false python arb_combat.py   # paper
  EXECUTION_ENABLED=true  python arb_combat.py   # live
"""

from __future__ import annotations
import os, sys, json, time, asyncio, logging
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Dict, Optional, List, Tuple, Set

import aiohttp

# ---------------------------------------------------------------------------
_dotenv = False
try:
    from dotenv import load_dotenv
    _dotenv = load_dotenv()
except ImportError:
    pass

HAVE_CLOB = False
try:
    from py_clob_client_v2 import (
        ApiCreds, ClobClient, OrderArgs, OrderType,
        PartialCreateOrderOptions, Side,
    )
    HAVE_CLOB = True
except ImportError:
    try:
        from py_clob_client import (
            ApiCreds, ClobClient, OrderArgs, OrderType,
            PartialCreateOrderOptions, Side,
        )
        HAVE_CLOB = True
    except ImportError:
        pass

# ===========================================================================
# CONFIG
# ===========================================================================
def _b(n, d="false"): return os.environ.get(n, d).lower() in ("1", "true", "yes", "on")
def _f(n, d): return float(os.environ.get(n, str(d)))
def _i(n, d): return int(os.environ.get(n, str(d)))
def _l(n, d): return [x.strip().lower() for x in os.environ.get(n, d).split(",") if x.strip()]

GAMMA_REST = "https://gamma-api.polymarket.com"
CLOB_REST  = "https://clob.polymarket.com"
CLOB_WS    = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

EXECUTION_ENABLED = _b("EXECUTION_ENABLED", "false")
PRIVATE_KEY    = os.environ.get("POLY_WALLET_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY", "")
FUNDER         = os.environ.get("POLY_FUNDER_ADDRESS") or os.environ.get("FUNDER", "")
SIGNATURE_TYPE = _i("POLY_SIGNATURE_TYPE", 3)
CHAIN_ID       = _i("POLY_CHAIN_ID", 137)
API_KEY        = os.environ.get("CLOB_API_KEY", "")
API_SECRET     = os.environ.get("CLOB_API_SECRET", "")
API_PASSPHRASE = os.environ.get("CLOB_API_PASSPHRASE", "")

ASSETS     = _l("ASSETS", "btc,eth,sol,xrp")
SEGMENTS   = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
ENABLED_TF = _l("ENABLED_TF", "5m,15m,1h")

# Wide edge
MIN_EDGE        = _f("MIN_EDGE", 0.020)
FEE_BUFFER      = _f("FEE_BUFFER", 0.005)
SLIPPAGE_BUFFER = _f("SLIPPAGE_BUFFER", 0.005)
MAX_SUM_ASKS    = 1.0 - MIN_EDGE - FEE_BUFFER - SLIPPAGE_BUFFER  # default 0.970

MAX_SHARES      = _f("MAX_SHARES", 40)
MAX_USD_MARKET  = _f("MAX_USD_MARKET", 35)
MAX_OPEN_USD    = _f("MAX_OPEN_USD", 250)
MIN_DEPTH       = _f("MIN_DEPTH", 3)
BOOK_STALE_SEC  = _f("BOOK_STALE_SEC", 8.0)   # WS иногда молчит — не убивать книгу слишком рано
REST_BOOK_SEC   = _f("REST_BOOK_SEC", 1.5)    # REST fallback poll
REST_BOOK_ON    = _b("REST_BOOK_ON", "true")

DISCOVERY_SEC   = _f("DISCOVERY_SEC", 2.0)
STOP_BEFORE_SEC = _f("STOP_BEFORE_SEC", 3.0)
PARTIAL_CD_SEC  = _f("PARTIAL_CD_SEC", 25.0)
REFIRE_CD_SEC   = _f("REFIRE_CD_SEC", 4.0)
SCAN_IDLE_SEC   = _f("SCAN_IDLE_SEC", 0.08)

ORDER_TYPE      = os.environ.get("ORDER_TYPE", "FAK").upper()
FAK_ATTEMPTS    = _i("FAK_ATTEMPTS", 2)
PARALLEL_LEGS   = _b("PARALLEL_LEGS", "true")
MIN_ORDER_VALUE = _f("MIN_ORDER_VALUE", 1.05)
ENTRY_SLIP      = _f("ENTRY_SLIP", 0.01)

SIGNAL_LOG = os.environ.get("SIGNAL_LOG", "arb_signals.jsonl")
TRADE_LOG  = os.environ.get("TRADE_LOG", "arb_combat.log")
OPS_LOG    = os.environ.get("OPS_LOG", "arb_ops.jsonl")

# ===========================================================================
# LOGGING
# ===========================================================================
class MsFmt(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        return time.strftime("%H:%M:%S", ct) + f".{int(record.msecs):03d}"

_h = logging.StreamHandler()
_h.setFormatter(MsFmt("%(asctime)s | %(levelname)s | %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_h])
log = logging.getLogger("arb")
_fh = logging.FileHandler(TRADE_LOG, encoding="utf-8")
_fh.setFormatter(MsFmt("%(asctime)s | %(levelname)s | %(message)s"))
log.addHandler(_fh)


def jlog(path: str, rec: dict):
    rec["ts"] = time.time()
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass

# ===========================================================================
# BOOK STATE (WS-driven)
# ===========================================================================
@dataclass
class BestAsk:
    price: float
    size: float
    ts: float


_book: Dict[str, BestAsk] = {}  # token_id → BestAsk


def update_ask(token_id: str, price: float, size: float = 0.0):
    if price and price > 0:
        _book[token_id] = BestAsk(float(price), float(size or 0), time.time())


def get_pair(token_up: str, token_down: str) -> Optional[Tuple[BestAsk, BestAsk]]:
    a, b = _book.get(token_up), _book.get(token_down)
    if a is None or b is None:
        return None
    now = time.time()
    if (now - a.ts) > BOOK_STALE_SEC or (now - b.ts) > BOOK_STALE_SEC:
        return None
    return a, b

# ===========================================================================
# MARKET REGISTRY
# ===========================================================================
@dataclass
class Market:
    asset: str
    tf: str
    seg: int
    slot: int
    slug: str
    token_up: str
    token_down: str
    end_ts: float
    discovered_at: float = field(default_factory=time.time)

    @property
    def remaining(self) -> float:
        return self.end_ts - time.time()

    def tradeable(self) -> bool:
        return self.remaining > STOP_BEFORE_SEC


_markets: Dict[str, Market] = {}          # slug → Market
_token_to_slug: Dict[str, str] = {}       # token → slug
_token_side: Dict[str, str] = {}          # token → "up"|"down"

_ws_pending: asyncio.Queue = asyncio.Queue()
_ws_subscribed: Set[str] = set()


async def _gamma_event(session: aiohttp.ClientSession, slug: str) -> Optional[dict]:
    try:
        async with session.get(f"{GAMMA_REST}/events", params={"slug": slug},
                               timeout=aiohttp.ClientTimeout(total=6)) as r:
            if r.status != 200:
                return None
            data = await r.json()
            if not data:
                return None
            return data[0] if isinstance(data, list) else data
    except Exception as e:
        log.debug("gamma %s: %s", slug, e)
        return None


def _extract_tokens(ev: dict) -> Tuple[Optional[str], Optional[str]]:
    for m in ev.get("markets") or []:
        if not (m.get("active") and not m.get("closed")):
            continue
        ids = m.get("clobTokenIds")
        if isinstance(ids, str):
            try:
                ids = json.loads(ids)
            except Exception:
                continue
        if not ids or len(ids) < 2:
            continue
        outs = m.get("outcomes")
        if isinstance(outs, str):
            try:
                outs = json.loads(outs)
            except Exception:
                outs = []
        t_up, t_dn = str(ids[0]), str(ids[1])
        for kw in ("Up", "Yes", "up", "yes"):
            if outs and kw in outs:
                i = outs.index(kw)
                t_up, t_dn = str(ids[i]), str(ids[1 - i])
                break
        return t_up, t_dn
    return None, None


async def discover(session: aiohttp.ClientSession) -> int:
    now = time.time()
    added = 0
    for asset in ASSETS:
        for tf in ENABLED_TF:
            if tf not in SEGMENTS:
                continue
            seg = SEGMENTS[tf]
            for off in (0, 1):
                slot = (int(now) // seg) * seg + off * seg
                slug = f"{asset}-updown-{tf}-{slot}"
                if slug in _markets and _markets[slug].remaining > -60:
                    continue
                ev = await _gamma_event(session, slug)
                if not ev:
                    continue
                t_up, t_dn = _extract_tokens(ev)
                if not t_up:
                    continue
                m = Market(asset=asset, tf=tf, seg=seg, slot=slot, slug=slug,
                           token_up=t_up, token_down=t_dn, end_ts=float(slot + seg))
                _markets[slug] = m
                _token_to_slug[t_up] = slug
                _token_to_slug[t_dn] = slug
                _token_side[t_up] = "up"
                _token_side[t_dn] = "down"
                _ws_pending.put_nowait(t_up)
                _ws_pending.put_nowait(t_dn)
                added += 1
                log.info("DISCOVERED %s rem=%.0fs", slug, m.remaining)
    # purge
    dead = [s for s, m in _markets.items() if m.remaining < -90]
    for s in dead:
        m = _markets.pop(s, None)
        if m:
            _token_to_slug.pop(m.token_up, None)
            _token_to_slug.pop(m.token_down, None)
            _token_side.pop(m.token_up, None)
            _token_side.pop(m.token_down, None)
    return added


def live_markets() -> List[Market]:
    return [m for m in _markets.values() if m.tradeable()]

# ===========================================================================
# ARB ENGINE
# ===========================================================================
class State(str, Enum):
    IDLE = "idle"
    ATTEMPTING = "attempting"
    COMPLETE = "complete"
    PARTIAL = "partial"
    ABORTED = "aborted"


@dataclass
class Opp:
    market: Market
    ask_up: float
    ask_down: float
    size_up: float
    size_down: float
    sum_asks: float
    edge: float
    shares: float
    ts: float = field(default_factory=time.time)


@dataclass
class Open:
    market: Market
    state: State
    opp: Opp
    filled_up: float = 0.0
    filled_down: float = 0.0
    t0: float = field(default_factory=time.time)


_open: Dict[str, Open] = {}
_cooldown: Dict[str, float] = {}
_open_usd = 0.0
_stats = {"opps": 0, "complete": 0, "partial": 0, "aborted": 0}


def evaluate(m: Market) -> Optional[Opp]:
    if not m.tradeable():
        return None
    if m.slug in _cooldown and time.time() < _cooldown[m.slug]:
        return None
    if m.slug in _open and _open[m.slug].state in (State.ATTEMPTING, State.COMPLETE):
        return None

    pair = get_pair(m.token_up, m.token_down)
    if not pair:
        return None
    a_up, a_dn = pair
    s = a_up.price + a_dn.price
    if s > MAX_SUM_ASKS:
        return None
    edge = 1.0 - s - FEE_BUFFER - SLIPPAGE_BUFFER
    if edge < MIN_EDGE:
        return None

    depth = min(a_up.size or 1e9, a_dn.size or 1e9)
    shares = min(MAX_SHARES, MAX_USD_MARKET / max(s, 0.01))
    if depth > 0:
        shares = min(shares, depth)
    if shares < MIN_DEPTH and depth > 0:
        return None

    est = shares * s
    if _open_usd + est > MAX_OPEN_USD:
        return None

    return Opp(market=m, ask_up=a_up.price, ask_down=a_dn.price,
               size_up=a_up.size, size_down=a_dn.size,
               sum_asks=s, edge=edge, shares=shares)


def scan() -> List[Opp]:
    opps = [o for m in live_markets() if (o := evaluate(m))]
    opps.sort(key=lambda x: (-x.edge, x.market.seg))  # best edge, then shorter TF
    return opps


def begin(opp: Opp) -> Open:
    global _open_usd
    o = Open(market=opp.market, state=State.ATTEMPTING, opp=opp)
    _open[opp.market.slug] = o
    _open_usd += opp.shares * opp.sum_asks
    _stats["opps"] += 1
    jlog(SIGNAL_LOG, {
        "event": "opportunity", "slug": opp.market.slug,
        "asset": opp.market.asset, "tf": opp.market.tf,
        "ask_up": opp.ask_up, "ask_down": opp.ask_down,
        "sum": opp.sum_asks, "edge": opp.edge, "shares": opp.shares,
        "remaining": opp.market.remaining,
    })
    log.warning("OPP %s | up=%.3f dn=%.3f sum=%.4f edge=%.4f sh=%.1f rem=%.0fs",
                opp.market.slug, opp.ask_up, opp.ask_down, opp.sum_asks,
                opp.edge, opp.shares, opp.market.remaining)
    return o


def on_fill(slug: str, side: str, filled: float):
    o = _open.get(slug)
    if not o:
        return
    if side == "up":
        o.filled_up += filled
    else:
        o.filled_down += filled
    if o.filled_up > 0 and o.filled_down > 0:
        o.state = State.COMPLETE
        _stats["complete"] += 1
        log.info("COMPLETE %s up=%.2f dn=%.2f edge=%.4f",
                 slug, o.filled_up, o.filled_down, o.opp.edge)
        jlog(OPS_LOG, {"event": "complete", "slug": slug,
                       "filled_up": o.filled_up, "filled_down": o.filled_down,
                       "edge": o.opp.edge})


def on_fail(slug: str, reason: str):
    global _open_usd
    o = _open.get(slug)
    if not o:
        return
    if o.filled_up > 0 or o.filled_down > 0:
        o.state = State.PARTIAL
        _stats["partial"] += 1
        _cooldown[slug] = time.time() + PARTIAL_CD_SEC
        log.error("PARTIAL %s up=%.2f dn=%.2f | %s — DIRECTIONAL RISK",
                  slug, o.filled_up, o.filled_down, reason)
        jlog(OPS_LOG, {"event": "partial", "slug": slug,
                       "filled_up": o.filled_up, "filled_down": o.filled_down,
                       "reason": reason})
    else:
        o.state = State.ABORTED
        _stats["aborted"] += 1
        _open_usd = max(0.0, _open_usd - o.opp.shares * o.opp.sum_asks)
        _cooldown[slug] = time.time() + REFIRE_CD_SEC
        log.info("ABORTED %s | %s", slug, reason)
        jlog(OPS_LOG, {"event": "aborted", "slug": slug, "reason": reason})
        _open.pop(slug, None)

# ===========================================================================
# EXECUTION
# ===========================================================================
_client = None
_tick: Dict[str, str] = {}
_neg: Dict[str, bool] = {}
_exec_lock = asyncio.Lock()


def build_client():
    global _client
    if not HAVE_CLOB:
        raise RuntimeError("py_clob_client_v2 not installed")
    if API_KEY:
        creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)
        cl = ClobClient(host=CLOB_REST, chain_id=CHAIN_ID, key=PRIVATE_KEY,
                        creds=creds, signature_type=SIGNATURE_TYPE, funder=FUNDER)
    else:
        tmp = ClobClient(host=CLOB_REST, chain_id=CHAIN_ID, key=PRIVATE_KEY,
                         signature_type=SIGNATURE_TYPE, funder=FUNDER)
        creds = tmp.create_or_derive_api_key()
        cl = ClobClient(host=CLOB_REST, chain_id=CHAIN_ID, key=PRIVATE_KEY,
                        creds=creds, signature_type=SIGNATURE_TYPE, funder=FUNDER)
    cl.assert_level_2_auth()
    _client = cl
    return cl


def _tick_sz(tid: str) -> str:
    if tid not in _tick:
        try:
            _tick[tid] = str(_client.get_tick_size(tid))
        except Exception:
            _tick[tid] = "0.01"
    return _tick[tid]


def _neg_risk(tid: str) -> bool:
    if tid not in _neg:
        try:
            _neg[tid] = _client.get_neg_risk(tid)
        except Exception:
            _neg[tid] = False
    return _neg[tid]


def _place_sync(token_id: str, price: float, size: float, label: str) -> Tuple[float, Optional[str]]:
    if price <= 0 or size <= 0:
        return 0.0, None
    price = min(max(round(price, 2), 0.01), 0.99)
    min_sz = MIN_ORDER_VALUE / price
    target = max(size, min_sz)
    pd = Decimal(str(price))
    sd = Decimal(str(round(target, 4)))
    md = (pd * sd).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if md < Decimal(str(MIN_ORDER_VALUE)):
        md = Decimal(str(MIN_ORDER_VALUE)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sd = (md / pd).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    step = Decimal("0.0001")
    for i in range(101):
        off = (i // 2 + 1) * step if i % 2 else Decimal(0)
        if i % 4 >= 2:
            off = -off
        c = sd + off
        if c > 0 and (pd * c) == (pd * c).quantize(Decimal("0.01")):
            sd = c
            break
    pf, sf = float(pd), float(sd)
    ot = OrderType.FAK if ORDER_TYPE == "FAK" else OrderType.FOK
    args = OrderArgs(token_id=token_id, price=pf, size=sf, side=Side.BUY)
    opts = PartialCreateOrderOptions(tick_size=_tick_sz(token_id), neg_risk=_neg_risk(token_id))
    try:
        signed = _client.create_order(args, opts)
        resp = _client.post_order(signed, ot)
        filled = float(resp.get("takingAmount", 0) or resp.get("size", 0) or 0)
        oid = resp.get("orderID") or resp.get("orderId")
        making = resp.get("makingAmount")
        actual = None
        if making is not None and filled > 0:
            try:
                actual = float(making) / filled
            except Exception:
                pass
        if actual is not None:
            log.info("  [%s] %.3f×%.4f → fill=%.4f real=%.4f", label, pf, sf, filled, actual)
        else:
            log.info("  [%s] %.3f×%.4f → fill=%.4f st=%s", label, pf, sf, filled, resp.get("status", "?"))
        return filled, oid
    except Exception as e:
        log.error("  [%s] %.3f×%.4f → %s", label, pf, sf, e)
        return 0.0, None


async def place_leg(token_id: str, price: float, size: float, label: str) -> float:
    if not EXECUTION_ENABLED:
        log.info("PAPER [%s] px=%.3f sz=%.2f", label, price, size)
        return size
    async with _exec_lock:
        filled, _ = await asyncio.to_thread(_place_sync, token_id, price, size, label)
    return filled


async def execute(opp: Opp) -> bool:
    m = opp.market
    lu = f"{m.asset.upper()}-Up/{m.tf}"
    ld = f"{m.asset.upper()}-Dn/{m.tf}"
    px_u = min(round(opp.ask_up + ENTRY_SLIP, 2), 0.99)
    px_d = min(round(opp.ask_down + ENTRY_SLIP, 2), 0.99)
    sh = opp.shares
    t0 = time.time()

    if PARALLEL_LEGS:
        fu, fd = await asyncio.gather(
            place_leg(m.token_up, px_u, sh, lu),
            place_leg(m.token_down, px_d, sh, ld),
        )
    else:
        fu = await place_leg(m.token_up, px_u, sh, lu)
        fd = await place_leg(m.token_down, px_d, sh, ld)

    log.info("LEGS %s | up=%.2f dn=%.2f lat=%.3fs", m.slug, fu, fd, time.time() - t0)

    if fu > 0:
        on_fill(m.slug, "up", fu)
    if fd > 0:
        on_fill(m.slug, "down", fd)

    if fu > 0 and fd > 0:
        return True

    # retry missing leg
    if fu > 0 and fd <= 0:
        for _ in range(FAK_ATTEMPTS - 1):
            fd = await place_leg(m.token_down, px_d, sh, ld + "-r")
            if fd > 0:
                on_fill(m.slug, "down", fd)
                return True
            await asyncio.sleep(0.12)
        on_fail(m.slug, "down_miss")
        return False

    if fd > 0 and fu <= 0:
        for _ in range(FAK_ATTEMPTS - 1):
            fu = await place_leg(m.token_up, px_u, sh, lu + "-r")
            if fu > 0:
                on_fill(m.slug, "up", fu)
                return True
            await asyncio.sleep(0.12)
        on_fail(m.slug, "up_miss")
        return False

    on_fail(m.slug, "both_miss")
    return False


async def warm(tokens: List[str]):
    if not EXECUTION_ENABLED or _client is None:
        return
    for t in tokens:
        try:
            async with _exec_lock:
                await asyncio.to_thread(_tick_sz, t)
                await asyncio.to_thread(_neg_risk, t)
        except Exception:
            pass

# ===========================================================================
# MARKET WEBSOCKET (проверенный паттерн rozi/lena)
# ===========================================================================
def _extract_levels(levels) -> Tuple[List[float], List[float]]:
    if not levels:
        return [], []
    prices, sizes = [], []
    for lvl in levels:
        try:
            prices.append(float(lvl.get("price")))
            sizes.append(float(lvl.get("size")))
        except (TypeError, ValueError, AttributeError):
            continue
    return prices, sizes


_ws_msg_count = 0
_ws_book_updates = 0


async def handle_book_event(ev: dict):
    """Update best ask from WS book / best_bid_ask / price_change events."""
    global _ws_book_updates
    asset_id = ev.get("asset_id")
    if asset_id is not None:
        asset_id = str(asset_id)
    if not asset_id or asset_id not in _token_to_slug:
        return
    et = ev.get("event_type")
    if et == "book":
        ask_p, ask_s = _extract_levels(ev.get("asks"))
        if ask_p:
            best_p = min(ask_p)
            best_sz = sum(s for p, s in zip(ask_p, ask_s) if abs(p - best_p) < 1e-9)
            update_ask(asset_id, best_p, best_sz)
            _ws_book_updates += 1
    elif et == "best_bid_ask":
        ba = ev.get("best_ask")
        if ba is not None:
            try:
                update_ask(asset_id, float(ba), 0.0)
                _ws_book_updates += 1
            except (TypeError, ValueError):
                pass
    elif et == "price_change":
        # partial updates — take lowest sell side if present
        for pc in ev.get("price_changes") or []:
            try:
                if str(pc.get("side", "")).upper() in ("SELL", "ASK"):
                    p = float(pc.get("price"))
                    s = float(pc.get("size") or 0)
                    cur = _book.get(asset_id)
                    if cur is None or p <= cur.price:
                        update_ask(asset_id, p, s)
                        _ws_book_updates += 1
            except (TypeError, ValueError):
                continue


async def market_ws_loop():
    """Stable Market WS — reconnect only on real failure; log silent closes."""
    global _ws_subscribed, _ws_msg_count
    while True:
        ping_t = flush_t = None
        t_conn = time.time()
        try:
            async with aiohttp.ClientSession() as session:
                prev = list(_ws_subscribed)
                _ws_subscribed = set()
                for t in prev:
                    await _ws_pending.put(t)
                # also ensure all live tokens are queued
                for m in list(_markets.values()):
                    await _ws_pending.put(m.token_up)
                    await _ws_pending.put(m.token_down)

                # no aiohttp heartbeat — only our keepalives (avoids double-ping drops)
                async with session.ws_connect(
                    CLOB_WS,
                    heartbeat=None,
                    autoping=True,
                    receive_timeout=90,
                ) as ws:
                    log.info("Market WS connected")

                    async def _pings():
                        while not ws.closed:
                            await asyncio.sleep(15)
                            if not ws.closed:
                                try:
                                    await ws.ping()
                                except Exception:
                                    break
                    ping_t = asyncio.create_task(_pings())

                    async def _flush():
                        while not ws.closed:
                            tid = await _ws_pending.get()
                            batch = [str(tid)]
                            # drain queue, max 40 per message
                            while len(batch) < 40 and not _ws_pending.empty():
                                try:
                                    batch.append(str(_ws_pending.get_nowait()))
                                except asyncio.QueueEmpty:
                                    break
                            new = [t for t in batch if t not in _ws_subscribed]
                            if not new:
                                continue
                            try:
                                if not _ws_subscribed:
                                    await ws.send_json({
                                        "assets_ids": new,
                                        "type": "market",
                                        "custom_feature_enabled": True,
                                    })
                                else:
                                    await ws.send_json({
                                        "operation": "subscribe",
                                        "assets_ids": new,
                                        "custom_feature_enabled": True,
                                    })
                                _ws_subscribed.update(new)
                                log.info("WS subscribe +%d (total %d)", len(new), len(_ws_subscribed))
                            except Exception as e:
                                log.warning("WS subscribe fail: %s", e)
                                break
                    flush_t = asyncio.create_task(_flush())

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.CLOSED:
                            log.warning("Market WS CLOSED by server after %.1fs", time.time() - t_conn)
                            break
                        if msg.type == aiohttp.WSMsgType.ERROR:
                            log.warning("Market WS ERROR: %s", ws.exception())
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT or not msg.data:
                            continue
                        _ws_msg_count += 1
                        try:
                            data = json.loads(msg.data)
                        except Exception:
                            continue
                        if not data:
                            continue
                        events = data if isinstance(data, list) else [data]
                        for ev in events:
                            if isinstance(ev, dict):
                                await handle_book_event(ev)

                    log.warning("Market WS stream ended after %.1fs msgs=%d book_upd=%d — reconnect",
                                time.time() - t_conn, _ws_msg_count, _ws_book_updates)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Market WS error: %s — reconnect 2s", e)
        finally:
            if ping_t:
                ping_t.cancel()
            if flush_t:
                flush_t.cancel()
        await asyncio.sleep(2)


async def rest_book_loop(session: aiohttp.ClientSession):
    """Fallback: REST /book for pairs missing or stale WS data."""
    if not REST_BOOK_ON:
        return
    while True:
        try:
            markets = live_markets()
            # prioritize markets without fresh pair
            need = []
            for m in markets:
                pair = get_pair(m.token_up, m.token_down)
                if pair is None:
                    need.append(m)
            # also refresh a rotating sample of live markets
            if not need:
                need = markets[:6]
            sem = asyncio.Semaphore(6)

            async def one(token_id: str):
                async with sem:
                    try:
                        async with session.get(
                            f"{CLOB_REST}/book",
                            params={"token_id": token_id},
                            timeout=aiohttp.ClientTimeout(total=4),
                        ) as r:
                            if r.status != 200:
                                # fallback /price
                                async with session.get(
                                    f"{CLOB_REST}/price",
                                    params={"token_id": token_id, "side": "sell"},
                                    timeout=aiohttp.ClientTimeout(total=3),
                                ) as r2:
                                    if r2.status == 200:
                                        d = await r2.json()
                                        p = d.get("price")
                                        if p is not None:
                                            update_ask(token_id, float(p), 0.0)
                                return
                            data = await r.json()
                            asks = data.get("asks") or []
                            if not asks:
                                return
                            best = min(asks, key=lambda x: float(x.get("price", 999)))
                            update_ask(token_id, float(best["price"]), float(best.get("size") or 0))
                    except Exception:
                        pass

            toks = []
            for m in need:
                toks.append(m.token_up)
                toks.append(m.token_down)
            if toks:
                await asyncio.gather(*[one(t) for t in toks], return_exceptions=True)
        except Exception as e:
            log.debug("rest_book: %s", e)
        await asyncio.sleep(REST_BOOK_SEC)

# ===========================================================================
# LOOPS
# ===========================================================================
async def discovery_loop(session: aiohttp.ClientSession):
    while True:
        try:
            n = await discover(session)
            if n:
                log.info("discovery +%d live=%d open_usd=%.0f", n, len(live_markets()), _open_usd)
        except Exception as e:
            log.warning("discovery: %s", e)
        await asyncio.sleep(DISCOVERY_SEC)


async def arb_loop():
    """Scan opportunities continuously; fire best one at a time."""
    last_status = time.time()
    while True:
        try:
            opps = scan()
            if opps:
                best = opps[0]
                begin(best)
                ok = await execute(best)
                if ok:
                    log.info("ARB OK %s edge=%.4f | stats=%s",
                             best.market.slug, best.edge, _stats)
                await asyncio.sleep(0.25)
            else:
                await asyncio.sleep(SCAN_IDLE_SEC)

            if time.time() - last_status > 20:
                live = live_markets()
                books = sum(1 for m in live if get_pair(m.token_up, m.token_down))
                # tightest sums — see if markets even approach MIN_EDGE
                sums = []
                for m in live:
                    pair = get_pair(m.token_up, m.token_down)
                    if pair:
                        s = pair[0].price + pair[1].price
                        sums.append((s, m.slug, pair[0].price, pair[1].price))
                sums.sort(key=lambda x: x[0])
                tight = ", ".join(f"{slug[-12:]}={s:.3f}({u:.2f}+{d:.2f})"
                                  for s, slug, u, d in sums[:3]) if sums else "none"
                log.info(
                    "STATUS live=%d books=%d open_usd=%.0f stats=%s ws_tok=%d "
                    "ws_msg=%d book_upd=%d | tightest: %s | need_sum<=%.3f",
                    len(live), books, _open_usd, _stats, len(_ws_subscribed),
                    _ws_msg_count, _ws_book_updates, tight, MAX_SUM_ASKS,
                )
                last_status = time.time()
        except Exception as e:
            log.error("arb_loop: %s", e)
            await asyncio.sleep(1)

# ===========================================================================
# MAIN
# ===========================================================================
async def main():
    log.info("=" * 72)
    log.info("  ARB COMBAT — complementary YES+NO (wide edges)")
    log.info("  dotenv=%s EXECUTION=%s CLOB=%s", _dotenv, EXECUTION_ENABLED, HAVE_CLOB)
    log.info("  ASSETS=%s TF=%s", ASSETS, ENABLED_TF)
    log.info("  MIN_EDGE=%.3f MAX_SUM=%.4f MAX_USD/mkt=%.0f MAX_OPEN=%.0f",
             MIN_EDGE, MAX_SUM_ASKS, MAX_USD_MARKET, MAX_OPEN_USD)
    log.info("  PARALLEL=%s ORDER=%s", PARALLEL_LEGS, ORDER_TYPE)
    log.info("=" * 72)

    if EXECUTION_ENABLED:
        if not HAVE_CLOB:
            log.error("install py_clob_client_v2")
            return
        try:
            build_client()
            log.warning("CLOB LIVE")
        except Exception as e:
            log.error("auth: %s", e)
            return
    else:
        log.info("PAPER MODE")

    async with aiohttp.ClientSession() as session:
        await discover(session)
        log.info("initial markets=%d", len(live_markets()))
        if EXECUTION_ENABLED:
            toks = []
            for m in live_markets():
                toks.extend([m.token_up, m.token_down])
            await warm(toks)

        await asyncio.gather(
            discovery_loop(session),
            market_ws_loop(),
            rest_book_loop(session),
            arb_loop(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped | final stats=%s", _stats)
