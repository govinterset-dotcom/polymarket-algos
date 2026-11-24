#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
polymizer_hybrid.py
-------------------
Гибрид двух версий:

  Лидер  (ask ≥ 0.5):  процентный рост ≥ LEADER_MIN_RISE_PCT за LEADER_WINDOW_SEC
  Андердог (ask < 0.5): абсолютный рост ≥ UNDERDOG_MIN_RISE_ABS за UNDERDOG_WINDOW_SEC

Рывок всегда считается по последним тикам ордербука (AskHistory),
а не по цене предыдущей покупки / сигнала.

Размер клипа растёт по порядку срабатывания на рынке:
  1-й = CLIP_SIZE, 2-й = CLIP_SIZE*(1+growth), 3-й = CLIP_SIZE*(1+growth)^2 ...
"""

import os, sys, json, re, time, asyncio, logging, math
from decimal import Decimal
from collections import deque

import aiohttp

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("WARN: python-dotenv не установлен — .env не будет прочитан.", file=sys.stderr)

try:
    from py_clob_client_v2 import (
        ApiCreds, BuilderConfig, ClobClient, OrderArgs, OrderType,
        PartialCreateOrderOptions, Side,
    )
    HAVE_CLOB = True
except ImportError:
    HAVE_CLOB = False

try:
    import websockets
    HAVE_WEBSOCKETS = True
except ImportError:
    HAVE_WEBSOCKETS = False

BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"

# ===========================================================================
# КОНФИГ
# ===========================================================================
GAMMA_REST = "https://gamma-api.polymarket.com"
CLOB_REST  = "https://clob.polymarket.com"
WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

EXECUTION_ENABLED = os.environ.get("EXECUTION_ENABLED", "false").lower() == "true"
PRIVATE_KEY       = os.environ.get("POLY_WALLET_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY", "")
FUNDER            = os.environ.get("POLY_FUNDER_ADDRESS") or os.environ.get("FUNDER", "")
SIGNATURE_TYPE    = int(os.environ.get("POLY_SIGNATURE_TYPE", "3"))
CHAIN_ID          = int(os.environ.get("POLY_CHAIN_ID", "137"))
API_KEY           = os.environ.get("CLOB_API_KEY", "")
API_SECRET        = os.environ.get("CLOB_API_SECRET", "")
API_PASSPHRASE    = os.environ.get("CLOB_API_PASSPHRASE", "")

# ── WebSocket ──
USE_WS_MONITORING      = os.environ.get("USE_WS_MONITORING", "true").lower() == "true"
WS_PING_INTERVAL_SEC   = float(os.environ.get("WS_PING_INTERVAL_SEC",  "5.0"))
WS_SILENT_TIMEOUT_SEC  = float(os.environ.get("WS_SILENT_TIMEOUT_SEC", "15.0"))
WS_RECONNECT_BASE_SEC  = float(os.environ.get("WS_RECONNECT_BASE_SEC", "1.0"))
WS_RECONNECT_MAX_SEC   = float(os.environ.get("WS_RECONNECT_MAX_SEC",  "20.0"))
WS_STALE_FALLBACK_SEC  = float(os.environ.get("WS_STALE_FALLBACK_SEC", "3.0"))
BOOK_STALE_RECONNECT_SEC = float(os.environ.get("BOOK_STALE_RECONNECT_SEC", "30.0"))

LOG_RAW_TICKS      = os.environ.get("LOG_RAW_TICKS", "false").lower() == "true"
RAW_TICKS_LOG_PATH = os.environ.get("RAW_TICKS_LOG_PATH", "raw_ticks.jsonl")

ASSETS = [a.strip().lower() for a in os.environ.get("ASSETS", "btc").split(",") if a.strip()]
SEG = 300  # 5 минут

# ── ЯДРО ГИБРИДА ────────────────────────────────────────────────────────────
# Лидер (ask ≥ LEADER_THRESHOLD): процентный рост
LEADER_THRESHOLD       = float(os.environ.get("LEADER_THRESHOLD",       "0.50"))
LEADER_WINDOW_SEC      = float(os.environ.get("LEADER_WINDOW_SEC",      "2.0"))
LEADER_MIN_RISE_PCT    = float(os.environ.get("LEADER_MIN_RISE_PCT",    "0.10"))  # 10 %

# Андердог (ask < LEADER_THRESHOLD): абсолютный рост
UNDERDOG_WINDOW_SEC    = float(os.environ.get("UNDERDOG_WINDOW_SEC",    "5.0"))
UNDERDOG_MIN_RISE_ABS  = float(os.environ.get("UNDERDOG_MIN_RISE_ABS",  "0.08"))

# Общий запас на дыры в данных (секунды)
MOMENTUM_STALE_GAP_SEC = float(os.environ.get("MOMENTUM_STALE_GAP_SEC", "3.0"))

CLIP_SIZE              = float(os.environ.get("CLIP_SIZE",              "100"))
# Рост размера клипа по ПОРЯДКУ СРАБАТЫВАНИЯ на рынке (не по стороне).
# 1-й клип = CLIP_SIZE, 2-й = CLIP_SIZE*(1+growth), 3-й = CLIP_SIZE*(1+growth)^2 ...
CLIP_SIZE_GROWTH       = float(os.environ.get("CLIP_SIZE_GROWTH",       "0.10"))  # 0.10 = +10 % каждый следующий
# Независимые интервалы между клипами для лидера и андердога
LEADER_CLIP_INTERVAL_SEC   = float(os.environ.get("LEADER_CLIP_INTERVAL_SEC",   "2.0"))
UNDERDOG_CLIP_INTERVAL_SEC = float(os.environ.get("UNDERDOG_CLIP_INTERVAL_SEC", "2.0"))
MAX_CLIP_PRICE         = float(os.environ.get("MAX_CLIP_PRICE",         "0.93"))
MIN_REMAINING_FOR_ENTRY_SEC = float(os.environ.get("MIN_REMAINING_FOR_ENTRY_SEC", "1.0"))

# ── Гейт входа ──────────────────────────────────────────────────────────────
# Алгоритм начинает искать сигналы ТОЛЬКО когда выполнены ОБА условия:
#   1) до конца рынка осталось не больше ENTRY_WINDOW_SEC секунд;
#   2) цены исходов ещё в неопределённом коридоре [CORRIDOR_LOW, CORRIDOR_HIGH]
#      (лидер ещё не убежал выше CORRIDOR_HIGH — сделка имеет смысл только пока
#      направление реально не решено).
# Как только гейт открылся хотя бы раз в этом окне — алгоритм "взводится"
# (armed) и дальше работает как обычно до конца рынка, даже если цена потом
# уйдёт выше коридора (иначе новый рывок к 80%+ никогда бы не поймали).
ENTRY_GATE_ENABLED = os.environ.get("ENTRY_GATE_ENABLED", "true").lower() == "true"
ENTRY_WINDOW_SEC    = float(os.environ.get("ENTRY_WINDOW_SEC",    "30.0"))
CORRIDOR_LOW        = float(os.environ.get("CORRIDOR_LOW",        "0.50"))
CORRIDOR_HIGH       = float(os.environ.get("CORRIDOR_HIGH",       "0.60"))

MAX_CLIPS_PER_MARKET   = int(os.environ.get("MAX_CLIPS_PER_MARKET",     "40"))
MAX_SKEW_RATIO         = float(os.environ.get("MAX_SKEW_RATIO",         "8.0"))
# Пока вторая сторона = 0, разрешаем не больше N клипов на первую сторону.
# Защита от длинного одностороннего набора в начале окна.
MAX_CLIPS_WHEN_OTHER_ZERO = int(os.environ.get("MAX_CLIPS_WHEN_OTHER_ZERO", "3"))
BUY_PRICE_BUFFER       = float(os.environ.get("BUY_PRICE_BUFFER",       "0.02"))

SKIP_MARKETS_AFTER_TRADE = int(os.environ.get("SKIP_MARKETS_AFTER_TRADE", "1"))

MIN_ORDER_VALUE  = float(os.environ.get("MIN_ORDER_VALUE",  "1.05"))
STOP_BEFORE_SEC  = float(os.environ.get("STOP_BEFORE_SEC",  "0.3"))
FAK_ATTEMPTS     = int(os.environ.get("FAK_ATTEMPTS",       "3"))
FAK_INTERVAL_SEC = float(os.environ.get("FAK_INTERVAL_SEC", "0.15"))
MONITOR_POLL_SEC = float(os.environ.get("MONITOR_POLL_SEC", "1.0"))

EPS = 1e-9

# ===========================================================================
# ЛОГИРОВАНИЕ
# ===========================================================================
class MillisecondFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        s = time.strftime("%H:%M:%S", ct)
        return f"{s}.{int(record.msecs):03d}"

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(MillisecondFormatter("%(asctime)s | %(levelname)s | %(message)s"))
log = logging.getLogger("hybrid_momentum_bot")
log.setLevel(logging.INFO)
log.propagate = False
log.addHandler(_console_handler)
_fh = logging.FileHandler("hybrid_momentum_bot_trades.log", encoding="utf-8")
_fh.setFormatter(MillisecondFormatter("%(asctime)s | %(levelname)s | %(message)s"))
log.addHandler(_fh)

SIGNAL_LOG_PATH = "hybrid_momentum_bot_signal_log.jsonl"

def log_signal(record: dict) -> None:
    record["ts"] = time.time()
    try:
        with open(SIGNAL_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        log.debug("log_signal: %s", e)


# ===========================================================================
# POLYMARKET — рынки и цены
# ===========================================================================
async def fetch_event_markets(session: aiohttp.ClientSession, slug: str) -> list:
    try:
        async with session.get(
            f"{GAMMA_REST}/events", params={"slug": slug},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status == 200:
                data = await r.json()
                if data:
                    ev = data[0] if isinstance(data, list) else data
                    outs = []
                    for m in ev.get("markets") or []:
                        if isinstance(m.get("clobTokenIds"), str):
                            m["clobTokenIds"] = json.loads(m["clobTokenIds"])
                        if isinstance(m.get("outcomes"), str):
                            m["outcomes"] = json.loads(m["outcomes"])
                        outs.append(m)
                    return outs
    except Exception as e:
        log.debug("fetch_event_markets(%s): %s", slug, e)
    return []

def extract_up_token(markets: list):
    for m in markets:
        if not (m.get("active") and not m.get("closed")):
            continue
        ids = m.get("clobTokenIds") or []
        if len(ids) < 2:
            continue
        t_yes, t_no = ids[0], ids[1]
        outs = m.get("outcomes") or []
        for kw in ("Up", "Yes", "up", "yes"):
            if kw in outs:
                ui = outs.index(kw)
                t_yes, t_no = ids[ui], ids[1 - ui]
                break
        return str(t_yes), str(t_no), m.get("question", "?")
    return None, None, "?"

async def get_current_market(session: aiohttp.ClientSession, asset: str) -> dict | None:
    now  = int(time.time())
    slot = (now // SEG) * SEG
    slug = f"{asset}-updown-5m-{slot}"
    ms   = await fetch_event_markets(session, slug)
    ty, tn, q = extract_up_token(ms)
    if ty is None:
        return None
    return {"slot": slot, "slug": slug, "token_up": ty, "token_down": tn, "question": q}

async def get_best_ask_rest(session: aiohttp.ClientSession, token_id: str) -> float | None:
    try:
        async with session.get(
            f"{CLOB_REST}/price", params={"token_id": token_id, "side": "sell"},
            timeout=aiohttp.ClientTimeout(total=4),
        ) as r:
            if r.status == 200:
                data = await r.json()
                p = data.get("price")
                ask = float(p) if p is not None else None
                if ask is not None:
                    global _rest_fallback_count
                    _rest_fallback_count += 1
                _raw_tick_log(token_id, ask, None, source="rest")
                return ask
    except Exception as e:
        log.warning("get_best_ask_rest(%s): REST-фолбэк тоже не сработал: %s", token_id, e)
    return None


# ===========================================================================
# WEBSOCKET
# ===========================================================================
_raw_tick_buffer: list[str] = []
RAW_TICK_FLUSH_INTERVAL_SEC = 1.5

def _raw_tick_log(token_id: str, ask: float | None, bid: float | None, source: str = "ws"):
    if not LOG_RAW_TICKS:
        return
    try:
        _raw_tick_buffer.append(json.dumps({
            "ts": time.time(), "token_id": token_id, "ask": ask, "bid": bid, "source": source,
        }))
    except Exception:
        pass

def _raw_tick_write_batch(lines: list[str]):
    try:
        with open(RAW_TICKS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        log.debug("raw_tick_write_batch: %s", e)

async def raw_tick_flusher():
    if not LOG_RAW_TICKS:
        return
    log.info("Логирование сырых тиков включено: %s (сброс раз в %.1fс)",
             RAW_TICKS_LOG_PATH, RAW_TICK_FLUSH_INTERVAL_SEC)
    while True:
        await asyncio.sleep(RAW_TICK_FLUSH_INTERVAL_SEC)
        if not _raw_tick_buffer:
            continue
        batch = _raw_tick_buffer.copy()
        _raw_tick_buffer.clear()
        try:
            await asyncio.to_thread(_raw_tick_write_batch, batch)
        except Exception as e:
            log.debug("raw_tick_flusher: %s", e)


class BookCache:
    def __init__(self):
        self.best_ask: dict[str, float] = {}
        self.best_bid: dict[str, float] = {}
        self.updated_at: dict[str, float] = {}
        self.last_any_message_ts: float = 0.0
        self.last_book_update_ts: float = 0.0
        self.connected: bool = False

    def update(self, token_id: str, ask: float | None, bid: float | None):
        now = time.time()
        if ask is not None:
            self.best_ask[token_id] = ask
        if bid is not None:
            self.best_bid[token_id] = bid
        self.updated_at[token_id] = now
        self.last_any_message_ts = now
        self.last_book_update_ts = now

    def get_ask(self, token_id: str) -> float | None:
        ts = self.updated_at.get(token_id)
        if ts is None or (time.time() - ts) > WS_STALE_FALLBACK_SEC:
            return None
        return self.best_ask.get(token_id)


book_cache = BookCache()
_rest_fallback_count = 0
_ws_wanted_tokens: set[str] = set()
_ws_subscribed_tokens: set[str] = set()
_ws_resubscribe_event = asyncio.Event()


def ws_want_tokens(token_ids: list[str]):
    changed = False
    for t in token_ids:
        if t not in _ws_wanted_tokens:
            _ws_wanted_tokens.add(t)
            changed = True
    if changed:
        _ws_resubscribe_event.set()


def ws_forget_tokens(token_ids: list[str]):
    changed = False
    for t in token_ids:
        if t in _ws_wanted_tokens:
            _ws_wanted_tokens.discard(t)
            changed = True
        _ws_subscribed_tokens.discard(t)
        book_cache.best_ask.pop(t, None)
        book_cache.best_bid.pop(t, None)
        book_cache.updated_at.pop(t, None)
    if changed:
        _ws_resubscribe_event.set()


def _parse_book_message(ev: dict) -> tuple[str | None, float | None, float | None]:
    token_id = ev.get("asset_id") or ev.get("market") or ev.get("token_id")
    if token_id is None:
        return None, None, None

    ask = ev.get("best_ask")
    bid = ev.get("best_bid")
    if ask is not None:
        ask = float(ask)
    if bid is not None:
        bid = float(bid)

    if ask is None:
        asks = ev.get("asks") or ev.get("ask_prices")
        if asks:
            try:
                prices = [float(a["price"]) if isinstance(a, dict) else float(a) for a in asks]
                ask = min(prices) if prices else None
            except Exception:
                pass
    if bid is None:
        bids = ev.get("bids") or ev.get("bid_prices")
        if bids:
            try:
                prices = [float(b["price"]) if isinstance(b, dict) else float(b) for b in bids]
                bid = max(prices) if prices else None
            except Exception:
                pass
    return token_id, ask, bid


async def _ws_send_subscription(ws):
    tokens = list(_ws_wanted_tokens)
    if not tokens:
        _ws_subscribed_tokens.clear()
        return
    try:
        await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
        _ws_subscribed_tokens.clear()
        _ws_subscribed_tokens.update(tokens)
        book_cache.last_book_update_ts = time.time()
        log.info("WS: подписка отправлена на %d токенов", len(tokens))
    except Exception as e:
        log.warning("WS: не удалось отправить подписку: %s", e)


async def _ws_pinger(ws):
    try:
        while True:
            await asyncio.sleep(WS_PING_INTERVAL_SEC)
            await ws.send(json.dumps({"type": "ping"}))
    except (asyncio.CancelledError, Exception):
        return


async def ws_market_listener():
    if not HAVE_WEBSOCKETS:
        log.error("WS: библиотека websockets не установлена (pip install websockets) — "
                   "мониторинг остаётся на REST-поллинге")
        return

    backoff = WS_RECONNECT_BASE_SEC
    while True:
        ping_task = None
        try:
            async with websockets.connect(WS_MARKET_URL, ping_interval=None, close_timeout=3) as ws:
                book_cache.connected = True
                book_cache.last_any_message_ts = time.time()
                book_cache.last_book_update_ts = time.time()
                log.info("WS подключён (%s)", WS_MARKET_URL)
                backoff = WS_RECONNECT_BASE_SEC
                await _ws_send_subscription(ws)
                ping_task = asyncio.create_task(_ws_pinger(ws))
                _ws_resubscribe_event.clear()

                while True:
                    if _ws_resubscribe_event.is_set():
                        _ws_resubscribe_event.clear()
                        await _ws_send_subscription(ws)

                    if _ws_subscribed_tokens:
                        book_silent = time.time() - book_cache.last_book_update_ts
                        if book_silent > BOOK_STALE_RECONNECT_SEC:
                            log.warning("WS: книга молчит %.0fс при живом сокете — форсирую реконнект",
                                        book_silent)
                            break

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=WS_SILENT_TIMEOUT_SEC)
                    except asyncio.TimeoutError:
                        log.warning("WS: ни одного сообщения за %.0fс — переподключаюсь",
                                    WS_SILENT_TIMEOUT_SEC)
                        break

                    book_cache.last_any_message_ts = time.time()
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue
                    events = data if isinstance(data, list) else [data]
                    for ev in events:
                        if not isinstance(ev, dict):
                            continue
                        et = ev.get("event_type")
                        if et not in ("book", "price_change", None):
                            continue
                        token_id, ask, bid = _parse_book_message(ev)
                        if token_id and (ask is not None or bid is not None):
                            book_cache.update(token_id, ask, bid)
                            _raw_tick_log(token_id, ask, bid, source="ws")
        except Exception as e:
            log.warning("WS соединение упало (%s) — переподключаюсь через %.1fс", e, backoff)
        finally:
            book_cache.connected = False
            if ping_task:
                ping_task.cancel()
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, WS_RECONNECT_MAX_SEC)


async def get_best_ask(session: aiohttp.ClientSession, token_id: str) -> float | None:
    if USE_WS_MONITORING and HAVE_WEBSOCKETS:
        cached = book_cache.get_ask(token_id)
        if cached is not None:
            return cached
    return await get_best_ask_rest(session, token_id)


# ===========================================================================
# CLOB
# ===========================================================================
def build_clob_client():
    if not HAVE_CLOB:
        raise RuntimeError("py_clob_client_v2 не установлен")
    creds = None
    if API_KEY:
        creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)
    if creds:
        cl = ClobClient(host=CLOB_REST, chain_id=CHAIN_ID, key=PRIVATE_KEY,
                        creds=creds, signature_type=SIGNATURE_TYPE, funder=FUNDER, builder_config=BuilderConfig(
                builder_address=BUILDER_ADDRESS,
                builder_code=BUILDER_CODE))
    else:
        tmp = ClobClient(host=CLOB_REST, chain_id=CHAIN_ID, key=PRIVATE_KEY,
                         signature_type=SIGNATURE_TYPE, funder=FUNDER, builder_config=BuilderConfig(
                builder_address=BUILDER_ADDRESS,
                builder_code=BUILDER_CODE))
        creds = tmp.create_or_derive_api_key()
        cl = ClobClient(host=CLOB_REST, chain_id=CHAIN_ID, key=PRIVATE_KEY,
                        creds=creds, signature_type=SIGNATURE_TYPE, funder=FUNDER, builder_config=BuilderConfig(
                builder_address=BUILDER_ADDRESS,
                builder_code=BUILDER_CODE))
    cl.assert_level_2_auth()
    return cl

_tick_cache: dict[str, str] = {}
_neg_risk_cache: dict[str, bool] = {}
_clob_lock = asyncio.Lock()

def _get_tick(client, token_id: str) -> str:
    if token_id not in _tick_cache:
        try:
            _tick_cache[token_id] = str(client.get_tick_size(token_id))
        except Exception:
            _tick_cache[token_id] = "0.01"
    return _tick_cache[token_id]

def _get_neg_risk(client, token_id: str) -> bool:
    if token_id not in _neg_risk_cache:
        try:
            _neg_risk_cache[token_id] = client.get_neg_risk(token_id)
        except Exception:
            _neg_risk_cache[token_id] = False
    return _neg_risk_cache[token_id]


def place_order(client, token_id: str, price: float, size: float, label: str,
                side: str = "BUY", order_type=None):
    if order_type is None:
        order_type = OrderType.FAK
    order_side = Side.BUY if side == "BUY" else Side.SELL
    if side == "BUY" and price > 0:
        price_d = Decimal(str(round(price, 2)))
        min_size = MIN_ORDER_VALUE / price
        target = max(size, min_size)
        size_int = math.ceil(target)
        while price_d * Decimal(size_int) < Decimal(str(MIN_ORDER_VALUE)):
            size_int += 1
        size_d = Decimal(size_int)
        price, size = float(price_d), float(size_d)
    else:
        price = round(price, 2) if price > 0 else price
        size = round(size, 4)

    tick = _get_tick(client, token_id)
    neg_risk = _get_neg_risk(client, token_id)
    args = OrderArgs(token_id=token_id, price=price, size=size, side=order_side)
    opts = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
    try:
        signed = client.create_order(args, opts)
        resp   = client.post_order(signed, order_type)
        filled = float(resp.get("takingAmount", 0) or resp.get("size", 0) or 0)
        oid    = resp.get("orderID") or resp.get("orderId")
        status = resp.get("status", "?")
        log.info("  [%s] price=%.3f size=%.4f → filled=%.4f status=%s oid=%s",
                 label, price, size, filled, status, oid)
        return filled, oid, False
    except Exception as e:
        oid = None
        m = re.search(r"""orderID['"]?\s*[:=]\s*['"]?(0x[0-9a-fA-F]+)""", str(e))
        if m:
            oid = m.group(1)
        is_clean_reject = "no orders found to match" in str(e)
        log.error("  [%s] price=%.3f size=%.4f → %s%s", label, price, size, e,
                  "" if is_clean_reject else "  [НЕОДНОЗНАЧНО]")
        return 0.0, oid, not is_clean_reject


def get_order_status(client, order_id: str):
    try:
        info = client.get_order(order_id)
        matched = float(info.get("size_matched", 0) or 0)
        status = str(info.get("status", "")).lower()
        is_terminal = status in ("matched", "cancelled", "canceled", "filled")
        return matched, is_terminal
    except Exception as e:
        log.debug("get_order_status(%s): %s", order_id, e)
        return None


async def buy_clip(session, client, token_id: str, label: str, ask_now: float, size: float) -> tuple[float, float]:
    price = min(ask_now + BUY_PRICE_BUFFER, MAX_CLIP_PRICE + BUY_PRICE_BUFFER)
    for attempt in range(1, FAK_ATTEMPTS + 1):
        if EXECUTION_ENABLED:
            async with _clob_lock:
                filled, oid, ambiguous = await asyncio.to_thread(
                    place_order, client, token_id, price, size, f"{label} попытка {attempt}/{FAK_ATTEMPTS}")
            if filled > 0:
                return filled, price
            if ambiguous:
                if oid:
                    await asyncio.sleep(0.5)
                    status = await asyncio.to_thread(get_order_status, client, oid)
                    if status:
                        real_filled, _ = status
                        if real_filled > 0:
                            log.warning("[%s] попытка %d была НЕОДНОЗНАЧНОЙ, но реально исполнилась: %.4f — "
                                        "новый ордер НЕ шлём", label, attempt, real_filled)
                            return real_filled, price
                else:
                    log.warning("[%s] попытка %d неоднозначная и БЕЗ orderID — проверьте позицию вручную",
                                label, attempt)
        else:
            log.info("PAPER [%s] клип price=%.3f size=%.1f", label, price, size)
            return size, price
        await asyncio.sleep(FAK_INTERVAL_SEC)
        fresh = await get_best_ask(session, token_id)
        if fresh is not None:
            price = min(fresh + BUY_PRICE_BUFFER, MAX_CLIP_PRICE + BUY_PRICE_BUFFER)
    return 0.0, price


def clip_size_for_trigger(n_prior_clips: int) -> float:
    """Размер клипа с учётом роста по порядку срабатывания на рынке
    (не по стороне и не по исходу).
    n_prior_clips — сколько клипов уже было ДО текущей покупки
    (0 для первого, 1 для второго и т.д.).
    Округляем до целого сразу — Polymarket не принимает дробные акции.
    """
    raw = CLIP_SIZE * ((1.0 + CLIP_SIZE_GROWTH) ** n_prior_clips)
    return float(round(raw))


# ===========================================================================
# AskHistory — два независимых окна (лидер / андердог)
# ===========================================================================
class AskHistory:
    """Хранит тики и умеет считать и %-, и abs-рост за произвольное окно."""
    __slots__ = ("samples",)
    def __init__(self):
        self.samples: deque = deque()

    def push(self, ts: float, ask: float):
        self.samples.append((ts, ask))
        # держим запас под самое длинное окно + stale-gap
        max_win = max(LEADER_WINDOW_SEC, UNDERDOG_WINDOW_SEC)
        cutoff = ts - max_win - MOMENTUM_STALE_GAP_SEC - 2.0
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def _past_ask(self, now: float, window_sec: float) -> float | None:
        if len(self.samples) < 2:
            return None
        target_ts = now - window_sec
        oldest_ts = self.samples[0][0]
        if oldest_ts > target_ts:
            return None
        past_ask = None
        past_ts = None
        for ts, ask in self.samples:
            if ts <= target_ts:
                past_ask, past_ts = ask, ts
            else:
                break
        if past_ask is None or past_ts is None:
            return None
        if (target_ts - past_ts) > MOMENTUM_STALE_GAP_SEC:
            return None
        return past_ask

    def rise_pct(self, now: float, window_sec: float) -> float | None:
        past = self._past_ask(now, window_sec)
        if past is None or past <= 0:
            return None
        return (self.samples[-1][1] - past) / past

    def rise_abs(self, now: float, window_sec: float) -> float | None:
        past = self._past_ask(now, window_sec)
        if past is None:
            return None
        return self.samples[-1][1] - past


# ===========================================================================
# МОНИТОР РЫНКА
# ===========================================================================
async def monitor_market(session, client, market: dict, asset: str):
    token_up, token_down, q = market["token_up"], market["token_down"], market["question"]
    deadline = market["slot"] + SEG

    log.info("[%s/5m] МОНИТОРИНГ: %s", asset.upper(), q)
    if ENTRY_GATE_ENABLED:
        log.info("[%s/5m] гейт входа: последние %.0fс до конца И цены в коридоре [%.2f, %.2f]",
                  asset.upper(), ENTRY_WINDOW_SEC, CORRIDOR_LOW, CORRIDOR_HIGH)
    if USE_WS_MONITORING:
        ws_want_tokens([token_up, token_down])

    hist_up = AskHistory()
    hist_down = AskHistory()
    q_up = 0.0
    q_down = 0.0
    cost_up = 0.0
    cost_down = 0.0
    n_clips = 0
    last_leader_clip_ts = 0.0
    last_underdog_clip_ts = 0.0
    armed = not ENTRY_GATE_ENABLED  # если гейт выключен — торгуем как раньше, с самого начала

    while True:
        remaining = deadline - time.time()
        if remaining <= STOP_BEFORE_SEC:
            break
        if remaining < MIN_REMAINING_FOR_ENTRY_SEC:
            await asyncio.sleep(MONITOR_POLL_SEC)
            continue

        now = time.time()
        ask_up, ask_down = await asyncio.gather(
            get_best_ask(session, token_up), get_best_ask(session, token_down))
        if ask_up is not None:
            hist_up.push(now, ask_up)
        if ask_down is not None:
            hist_down.push(now, ask_down)

        # ── Гейт входа: пока не взведён — только копим историю тиков, сигналы
        #    не ищем и клипы не шлём. Взводится один раз и держится до конца рынка.
        if not armed:
            in_window = remaining <= ENTRY_WINDOW_SEC
            prices_ok = (
                ask_up is not None and ask_down is not None and
                CORRIDOR_LOW <= max(ask_up, ask_down) <= CORRIDOR_HIGH
            )
            if in_window and prices_ok:
                armed = True
                log.warning("[%s/5m] ГЕЙТ ОТКРЫТ: осталось %.1fс, ask_up=%.3f ask_down=%.3f — алгоритм включается",
                            asset.upper(), remaining, ask_up, ask_down)
            else:
                await asyncio.sleep(MONITOR_POLL_SEC)
                continue

        if n_clips >= MAX_CLIPS_PER_MARKET:
            await asyncio.sleep(MONITOR_POLL_SEC)
            continue

        leader_ready = (now - last_leader_clip_ts) >= LEADER_CLIP_INTERVAL_SEC
        underdog_ready = (now - last_underdog_clip_ts) >= UNDERDOG_CLIP_INTERVAL_SEC

        candidate = None  # (side, token, ask, rise, kind)

        # ── UP ──
        if ask_up is not None and ask_up <= MAX_CLIP_PRICE:
            if ask_up >= LEADER_THRESHOLD:
                if leader_ready:
                    rise = hist_up.rise_pct(now, LEADER_WINDOW_SEC)
                    if rise is not None and rise >= LEADER_MIN_RISE_PCT:
                        candidate = ("UP", token_up, ask_up, rise, "LEADER")
            else:
                if underdog_ready:
                    rise = hist_up.rise_abs(now, UNDERDOG_WINDOW_SEC)
                    if rise is not None and rise >= UNDERDOG_MIN_RISE_ABS:
                        candidate = ("UP", token_up, ask_up, rise, "UNDERDOG")

        # ── DOWN ──
        if ask_down is not None and ask_down <= MAX_CLIP_PRICE:
            if ask_down >= LEADER_THRESHOLD:
                if leader_ready:
                    rise = hist_down.rise_pct(now, LEADER_WINDOW_SEC)
                    if rise is not None and rise >= LEADER_MIN_RISE_PCT:
                        if candidate is None or (candidate[4] == "LEADER" and rise > candidate[3]):
                            candidate = ("DOWN", token_down, ask_down, rise, "LEADER")
            else:
                if underdog_ready:
                    rise = hist_down.rise_abs(now, UNDERDOG_WINDOW_SEC)
                    if rise is not None and rise >= UNDERDOG_MIN_RISE_ABS:
                        if candidate is None:
                            candidate = ("DOWN", token_down, ask_down, rise, "UNDERDOG")
                        elif candidate[4] == "UNDERDOG" and rise > candidate[3]:
                            candidate = ("DOWN", token_down, ask_down, rise, "UNDERDOG")
                        # если уже есть LEADER — оставляем лидера

        # потолок перекоса + лимит одностороннего набора
        if candidate is not None:
            side_label = candidate[0]
            cur_larger = "UP" if q_up >= q_down else "DOWN"
            size_guess = clip_size_for_trigger(n_clips)

            # Пока вторая сторона = 0 — не больше MAX_CLIPS_WHEN_OTHER_ZERO клипов на первую.
            if min(q_up, q_down) <= 0 and MAX_CLIPS_WHEN_OTHER_ZERO > 0:
                q_first = q_up if side_label == "UP" else q_down
                n_first = int(round(q_first / max(CLIP_SIZE, 1e-9)))
                if n_first >= MAX_CLIPS_WHEN_OTHER_ZERO:
                    log.info("[%s/5m %s_%s] клип пропущен: односторонний набор %d ≥ %d (вторая сторона=0)",
                             asset.upper(), candidate[4], side_label,
                             n_first, MAX_CLIPS_WHEN_OTHER_ZERO)
                    candidate = None

            # Обычный skew — только когда обе стороны уже > 0
            if candidate is not None and side_label == cur_larger and min(q_up, q_down) > 0:
                prospective_larger = max(q_up, q_down) + size_guess
                prospective_skew = prospective_larger / min(q_up, q_down)
                if prospective_skew > MAX_SKEW_RATIO:
                    log.info("[%s/5m %s_%s] клип пропущен: перекос уже %.1fx → %.1fx (>%.1fx)",
                             asset.upper(), candidate[4], side_label,
                             max(q_up, q_down) / min(q_up, q_down),
                             prospective_skew, MAX_SKEW_RATIO)
                    candidate = None

        if candidate is not None:
            side_label, token_id, ask_now, rise, kind = candidate
            label = f"{asset.upper()}/5m {kind}_{side_label}"
            if kind == "LEADER":
                log.warning("[%s] лидер: ask +%.1f%% за %.0fс (ask=%.3f ≥ %.2f) — клип",
                            label, rise * 100.0, LEADER_WINDOW_SEC, ask_now, LEADER_THRESHOLD)
            else:
                log.warning("[%s] андердог: ask +%.4f за %.0fс (ask=%.3f < %.2f) — клип",
                            label, rise, UNDERDOG_WINDOW_SEC, ask_now, LEADER_THRESHOLD)

            requested_size = clip_size_for_trigger(n_clips)
            filled, exec_price = await buy_clip(session, client, token_id, label, ask_now, requested_size)
            if filled > 0:
                n_clips += 1
                if kind == "LEADER":
                    last_leader_clip_ts = now
                else:
                    last_underdog_clip_ts = now
                if side_label == "UP":
                    q_up += filled
                    cost_up += filled * exec_price
                else:
                    q_down += filled
                    cost_down += filled * exec_price
                log_signal({
                    "event": "clip", "asset": asset.upper(), "slug": market["slug"],
                    "side": side_label, "price": exec_price, "signal_ask": ask_now,
                    "size": filled, "rise": rise, "kind": kind,
                    "q_up": q_up, "q_down": q_down, "n_clips": n_clips,
                })

        await asyncio.sleep(MONITOR_POLL_SEC)

    skew = "UP" if q_up > q_down else ("DOWN" if q_down > q_up else "НЕТ ПЕРЕКОСА")
    log.info("[%s/5m] окно закрыто: %s | q_up=%.0f (cost=%.2f) q_down=%.0f (cost=%.2f) | перекос=%s | клипов=%d",
             asset.upper(), market["slug"], q_up, cost_up, q_down, cost_down, skew, n_clips)
    log_signal({
        "event": "market_closed", "asset": asset.upper(), "slug": market["slug"],
        "q_up": q_up, "cost_up": cost_up, "q_down": q_down, "cost_down": cost_down,
        "skew": skew, "n_clips": n_clips,
    })
    if USE_WS_MONITORING:
        ws_forget_tokens([token_up, token_down])


# ===========================================================================
# ПЛАНИРОВЩИК
# ===========================================================================
async def scheduler():
    global _rest_fallback_count
    client = None
    if EXECUTION_ENABLED:
        if not HAVE_CLOB:
            log.error("EXECUTION_ENABLED=true, но py_clob_client_v2 не установлен — выход")
            return
        client = build_clob_client()
        log.info("CLOB-клиент инициализирован (боевой режим)")
    else:
        log.info("PAPER-режим (EXECUTION_ENABLED=false)")

    active_tasks: dict[tuple, asyncio.Task] = {}
    next_eligible_slot: dict[str, int] = {}
    last_logged_skip: dict[str, int] = {}

    if USE_WS_MONITORING:
        if HAVE_WEBSOCKETS:
            asyncio.create_task(ws_market_listener())
            log.info("WS-мониторинг включён")
        else:
            log.error("USE_WS_MONITORING=true, но 'websockets' не установлен — REST-поллинг")

    if LOG_RAW_TICKS:
        asyncio.create_task(raw_tick_flusher())

    async with aiohttp.ClientSession() as session:
        last_ws_health_log = 0.0
        while True:
            if USE_WS_MONITORING and HAVE_WEBSOCKETS and time.time() - last_ws_health_log >= 30.0:
                last_ws_health_log = time.time()
                silent_for = time.time() - book_cache.last_any_message_ts if book_cache.last_any_message_ts else None
                book_silent_for = time.time() - book_cache.last_book_update_ts if book_cache.last_book_update_ts else None
                log.info("WS статус: connected=%s | подписано=%d | тишина(сокет)=%s | тишина(книга)=%s | "
                         "REST-фолбэков за 30с=%d",
                         book_cache.connected, len(_ws_subscribed_tokens),
                         f"{silent_for:.1f}с" if silent_for is not None else "н/д",
                         f"{book_silent_for:.1f}с" if book_silent_for is not None else "н/д",
                         _rest_fallback_count)
                _rest_fallback_count = 0

            for asset in ASSETS:
                slot = int(time.time() // SEG) * SEG
                key = (asset, slot)
                if key in active_tasks and not active_tasks[key].done():
                    continue
                if slot < next_eligible_slot.get(asset, 0):
                    if last_logged_skip.get(asset) != slot:
                        last_logged_skip[asset] = slot
                        log.info("[%s/5m] окно %d сознательно пропущено (redeem-пауза)", asset.upper(), slot)
                    continue
                market = await get_current_market(session, asset)
                if market is None:
                    continue
                task = asyncio.create_task(monitor_market(session, client, market, asset))
                active_tasks[key] = task
                next_eligible_slot[asset] = slot + (SKIP_MARKETS_AFTER_TRADE + 1) * SEG
                log.info("[%s/5m] задача мониторинга запущена: %s", asset.upper(), market["slug"])

            for k in list(active_tasks):
                if active_tasks[k].done():
                    exc = active_tasks[k].exception()
                    if exc:
                        log.error("Задача %s упала: %s", k, exc)
                    del active_tasks[k]

            await asyncio.sleep(2)


async def main():
    log.info("=== polymizer_hybrid.py СТАРТ ===")
    log.info("Активы: %s | paper=%s", ASSETS, not EXECUTION_ENABLED)
    log.info("ЛИДЕР  (ask≥%.2f): ≥%.0f%% за %.0fс",
             LEADER_THRESHOLD, LEADER_MIN_RISE_PCT * 100, LEADER_WINDOW_SEC)
    log.info("АНДЕРДОГ (ask<%.2f): ≥%.2f abs за %.0fс",
             LEADER_THRESHOLD, UNDERDOG_MIN_RISE_ABS, UNDERDOG_WINDOW_SEC)
    log.info("клип=%.0f (рост +%.0f%% каждый следующий) | LEADER_interval≥%.1fс | UNDERDOG_interval≥%.1fс | MAX_CLIP_PRICE=%.2f | MAX_SKEW=%.1fx | MAX_CLIPS_WHEN_OTHER_ZERO=%d",
             CLIP_SIZE, CLIP_SIZE_GROWTH * 100, LEADER_CLIP_INTERVAL_SEC, UNDERDOG_CLIP_INTERVAL_SEC,
             MAX_CLIP_PRICE, MAX_SKEW_RATIO, MAX_CLIPS_WHEN_OTHER_ZERO)
    log.info("после сделки пропускаем %d окно(а)", SKIP_MARKETS_AFTER_TRADE)
    if ENTRY_GATE_ENABLED:
        log.info("ГЕЙТ ВХОДА включён: старт только за ≤%.0fс до конца рынка И цены в коридоре [%.2f, %.2f]",
                  ENTRY_WINDOW_SEC, CORRIDOR_LOW, CORRIDOR_HIGH)
    else:
        log.info("ГЕЙТ ВХОДА выключен — торговля с начала окна, как раньше")
    await scheduler()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено по Ctrl+C")
