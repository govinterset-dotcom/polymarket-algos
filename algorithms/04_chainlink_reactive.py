#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chainlink_reactive_bot.py  (было polymizerbot.py / chainlink_two_scale_bot.py)
--------------------------------------------------------------------------
ПЕРЕАРХИТЕКТУРЕНО 08.08: ядро сигнала полностью смещено на "мелкие
колебания" Chainlink-фида (тот же RTDS-поток, по которому реально
резолвятся рынки), одноуровневая логика вместо прежней двухуровневой
(FIRST/PYRAMID). Идея: любой достаточно быстрый скачок цены Chainlink за
CL_LOOKBACK_SEC (по умолчанию ~5с, порог не откалиброван — см. ниже) даёт
сигнал на покупку; РАЗМЕР клипа ранжируется по силе скачка:
  |move| >= CL_STRONG_MOVE_PCT (0.01%)  → CLIP_SIZE_STRONG (120)
  CL_MIN_MOVE_PCT <= |move| < STRONG    → CLIP_SIZE_BASE   (60)
Все фильтры, заточенные под КНИГУ Polymarket (book_confirm, skew-cap,
dynamic max-price по книге, отдельные FIRST/PYRAMID окна и REVERSE/
BOOK_MOMENTUM триггеры) — УБРАНЫ. Книга (BookCache/WS) используется
ТОЛЬКО для получения цены исполнения (ask) при покупке, не как фильтр
сигнала. Ценовые фильтры: не покупать выше MAX_CLIP_PRICE (по умолчанию
0.90) и не покупать ниже MIN_CLIP_PRICE (по умолчанию 0.0 = выключен).

НЕОТКАЛИБРОВАНО (открытый вопрос, требует paper-прогона / доп. данных):
  - CL_LOOKBACK_SEC — временное окно скачка. ~5с — грубая оценка автора
    стратегии, не подтверждена. cl_calibration_logger() ниже пишет в
    signal-лог сырые колебания Chainlink (move% на нескольких соседних
    окнах сразу — 2/3/5/8/10/15с) с фиксированным тактом, чтобы потом
    сопоставить это с реальными сделками/резолюциями и подобрать окно.
  - CL_MIN_MOVE_PCT (нижний порог, ниже которого сигнала вообще нет) —
    тоже не подтверждён, дефолт взят с запасом вниз от STRONG-порога.
  - Оценка частоты триггеров на прокси-данных Binance 1s (24ч, 07.08,
    см. отдельный calib.py/отчёт): при 5с/0.01% срабатывает ~13.7% всех
    секунд (≈494 событий/ч без учёта cooldown) — Binance ≠ Chainlink
    (иная периодичность апдейтов и небольшая задержка оракула), поэтому
    это ТОЛЬКО грубая прикидка порядка величины, не точная калибровка.

ИСТОЧНИК ЦЕНЫ (сигнал): Polymarket RTDS, topic=crypto_prices_chainlink —
  тот же фид, по которому резолвятся рынки. wss://ws-live-data.polymarket.com
ИСТОЧНИК ЦЕНЫ (исполнение): ask книги Polymarket через CLOB — как раньше.

ЗАПУСК:
  pip install aiohttp python-dotenv
  pip install py_clob_client_v2  # только для live
  python3 chainlink_reactive_bot.py                     # paper (default)
  EXECUTION_ENABLED=true python3 chainlink_reactive_bot.py
"""

import os, sys, json, re, time, math, asyncio, logging
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
BOOK_WS    = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RTDS_WS    = "wss://ws-live-data.polymarket.com"

EXECUTION_ENABLED = os.environ.get("EXECUTION_ENABLED", "false").lower() == "true"
PRIVATE_KEY = os.environ.get("POLY_WALLET_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY", "")
FUNDER      = os.environ.get("POLY_FUNDER_ADDRESS") or os.environ.get("FUNDER", "")
SIGNATURE_TYPE = int(os.environ.get("POLY_SIGNATURE_TYPE", "1"))
CHAIN_ID    = int(os.environ.get("POLY_CHAIN_ID", "137"))
API_KEY     = os.environ.get("CLOB_API_KEY", "")
API_SECRET  = os.environ.get("CLOB_API_SECRET", "")
API_PASSPHRASE = os.environ.get("CLOB_API_PASSPHRASE", "")

ASSETS = [a.strip().lower() for a in os.environ.get("ASSETS", "btc").split(",") if a.strip()]
TIMEFRAMES = [tf.strip() for tf in os.environ.get("TIMEFRAMES", "5m").split(",") if tf.strip()]
SEG_BY_TF = {"5m": 300, "15m": 900}

CHAINLINK_SYMBOL = {"btc": "btc/usd", "eth": "eth/usd", "xrp": "xrp/usd", "sol": "sol/usd"}

# ── Сердце стратегии: ОДНОУРОВНЕВЫЙ реактивный сигнал по Chainlink ────────
# Единое окно на весь срок жизни рынка (никакого отдельного FIRST/PYRAMID):
# при |move| >= CL_MIN_MOVE_PCT за CL_LOOKBACK_SEC — покупка, размер клипа
# ранжируется по силе скачка (см. clip_size_for_move ниже). Все пороги —
# намеренно "распахнуты" (widened), т.к. это ЧЕРНОВАЯ калибровка (см.
# докстринг файла): CL_LOOKBACK_SEC и CL_MIN_MOVE_PCT — параметры,
# которые ещё предстоит подобрать по логам cl_calibration_logger().
CL_LOOKBACK_SEC      = float(os.environ.get("CL_LOOKBACK_SEC", "5.0"))
CL_MIN_MOVE_PCT      = float(os.environ.get("CL_MIN_MOVE_PCT", "0.005"))   # нижний порог (60)
CL_STRONG_MOVE_PCT   = float(os.environ.get("CL_STRONG_MOVE_PCT", "0.01"))  # верхний порог (120)
CL_COOLDOWN_SEC      = float(os.environ.get("CL_COOLDOWN_SEC", "1.5"))     # мин. пауза между клипами

CLIP_SIZE_BASE       = float(os.environ.get("CLIP_SIZE_BASE", "60"))
CLIP_SIZE_STRONG     = float(os.environ.get("CLIP_SIZE_STRONG", "120"))

# Ценовые фильтры (заменяют весь прежний
# DYNAMIC_MAX_PRICE/MAX_CLIP_PRICE_STRONG/STRONG_MOVE_PCT-комплекс).
MAX_CLIP_PRICE       = float(os.environ.get("MAX_CLIP_PRICE", "0.90"))
# MIN_CLIP_PRICE: 0.0 = выключен (поведение не меняется, пока явно не задан).
# По данным калибровки (chainlink_two_scale_trades.log, финальный конфиг,
# 10 чистых рынков): клипы ask<0.10 — 24/24 на проигравшей стороне (0% win),
# зона 0.10-0.20 менее однозначна (там же единственный пойманный разворот).
# Рекомендация из анализа: ~0.08-0.10, не выше — выше 0.20 уже режет и часть
# редких, но крупных разворотных выигрышей.
MIN_CLIP_PRICE       = float(os.environ.get("MIN_CLIP_PRICE", "0.0"))

MAX_CLIPS_PER_MARKET = int(os.environ.get("MAX_CLIPS_PER_MARKET", "80"))
BUY_PRICE_BUFFER     = float(os.environ.get("BUY_PRICE_BUFFER", "0.01"))

# ── Логирование сырых колебаний Chainlink для будущей калибровки ──────────
# Пишет в signal-лог с фиксированным тактом (независимо от того, открыт ли
# сейчас рынок) move% сразу на нескольких окнах — по этим данным потом
# подбираются CL_LOOKBACK_SEC / CL_MIN_MOVE_PCT / CL_STRONG_MOVE_PCT.
CL_CALIB_LOG_SEC     = float(os.environ.get("CL_CALIB_LOG_SEC", "1.0"))
CL_CALIB_WINDOWS     = [2.0, 3.0, 5.0, 8.0, 10.0, 15.0]

# ── Redeem-пауза ────────────────────────────────────────────────────────
SKIP_MARKETS_AFTER_TRADE = int(os.environ.get("SKIP_MARKETS_AFTER_TRADE", "0"))

# ── Ордера / защита от double-buy (перенесено без изменений) ──────────────
MIN_ORDER_VALUE  = float(os.environ.get("MIN_ORDER_VALUE", "1.05"))
STOP_BEFORE_SEC  = float(os.environ.get("STOP_BEFORE_SEC", "0.3"))
FAK_ATTEMPTS     = int(os.environ.get("FAK_ATTEMPTS", "3"))
FAK_INTERVAL_SEC = float(os.environ.get("FAK_INTERVAL_SEC", "0.15"))
FAK_PRICE_ESCALATION_STEP = float(os.environ.get("FAK_PRICE_ESCALATION_STEP", "0.01"))
MONITOR_POLL_SEC = float(os.environ.get("MONITOR_POLL_SEC", "0.5"))
ORDER_STATUS_POLL_SEC = float(os.environ.get("ORDER_STATUS_POLL_SEC", "0.3"))

# ── RTDS (chainlink) мониторинг ────────────────────────────────────────────
RTDS_STALE_TIMEOUT_SEC = float(os.environ.get("RTDS_STALE_TIMEOUT_SEC", "5.0"))
RTDS_RESUB_SEC         = float(os.environ.get("RTDS_RESUB_SEC", "25.0"))
RTDS_PING_INTERVAL_SEC = float(os.environ.get("RTDS_PING_INTERVAL_SEC", "5.0"))

# ── Discovery — с опережением на один слот, чтобы книга/RTDS были готовы
# ── К ОТКРЫТИЮ рынка, а не через минуту после (см. докстринг выше) ────────
DISCOVERY_SEC = float(os.environ.get("DISCOVERY_SEC", "2.0"))
# За сколько секунд до реального открытия слота прогревать WS-книгу
# (подписываться заранее), НЕ запуская при этом вход/монитор рынка.
# Это и есть тот самый смысл дискавери-с-опережением — но раньше он был
# перепутан с запуском monitor_market(), из-за чего бот входил в окно
# входа за 5 минут до открытия рынка и всегда промахивался (см. фикс
# active_markets()/book_warmup_loop() ниже).
BOOK_WARMUP_SEC = float(os.environ.get("BOOK_WARMUP_SEC", "20.0"))

# ── WebSocket-мониторинг книги (реконнект + детект тихой смерти) ──────────
USE_WS_MONITORING     = os.environ.get("USE_WS_MONITORING", "true").lower() == "true"
WS_PING_INTERVAL_SEC  = float(os.environ.get("WS_PING_INTERVAL_SEC",  "5.0"))
WS_SILENT_TIMEOUT_SEC = float(os.environ.get("WS_SILENT_TIMEOUT_SEC", "15.0"))
WS_RECONNECT_BASE_SEC = float(os.environ.get("WS_RECONNECT_BASE_SEC", "1.0"))
WS_RECONNECT_MAX_SEC  = float(os.environ.get("WS_RECONNECT_MAX_SEC",  "20.0"))
WS_STALE_FALLBACK_SEC = float(os.environ.get("WS_STALE_FALLBACK_SEC", "3.0"))
BOOK_STALE_RECONNECT_SEC = float(os.environ.get("BOOK_STALE_RECONNECT_SEC", "30.0"))

EPS = 1e-9

# ===========================================================================
# ЛОГИРОВАНИЕ
# ===========================================================================
class MsFmt(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        return time.strftime("%H:%M:%S", ct) + f".{int(record.msecs):03d}"

_h = logging.StreamHandler()
_h.setFormatter(MsFmt("%(asctime)s | %(levelname)s | %(message)s"))
log = logging.getLogger("cl2scale")
log.setLevel(logging.INFO)
log.propagate = False
log.addHandler(_h)
_fh = logging.FileHandler("chainlink_two_scale_trades.log", encoding="utf-8")
_fh.setFormatter(MsFmt("%(asctime)s | %(levelname)s | %(message)s"))
log.addHandler(_fh)

SIGNAL_LOG_PATH = "chainlink_two_scale_signals.jsonl"

def jlog(rec: dict):
    rec["ts"] = time.time()
    try:
        with open(SIGNAL_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception as e:
        log.debug("jlog: %s", e)


# ===========================================================================
# RTDS (CHAINLINK) — перенесено из polymizerbot__chinlink.py без изменений
# ===========================================================================
_cl_hist: dict[str, deque] = {a.upper(): deque(maxlen=600) for a in ASSETS}
_cl_last: dict[str, tuple] = {}
_cl_lock = asyncio.Lock()
_rtds_msg_count = 0
_last_rtds_any = time.time()


def _push_cl(asset_upper: str, ts_sec: float, price: float):
    global _rtds_msg_count, _last_rtds_any
    hist = _cl_hist[asset_upper]
    if hist and abs(hist[-1][0] - ts_sec) < 1e-6 and abs(hist[-1][1] - price) < 1e-9:
        return
    hist.append((ts_sec, price))
    _cl_last[asset_upper] = (ts_sec, price)
    _rtds_msg_count += 1
    _last_rtds_any = time.time()


def cl_price(asset_upper: str):
    v = _cl_last.get(asset_upper)
    if not v:
        return None
    if time.time() - v[0] > RTDS_STALE_TIMEOUT_SEC:
        return None
    return v


def cl_price_at(asset_upper: str, target_ts: float, tol: float = 3.0):
    """Ближайшая известная цена chainlink на момент target_ts (не только
    'сейчас' — нужна и историческая точка для длинного/короткого окна)."""
    hist = _cl_hist.get(asset_upper)
    if not hist:
        return None
    best, best_dt = None, None
    for ts, px in hist:
        dt = abs(ts - target_ts)
        if dt <= tol and (best_dt is None or dt < best_dt):
            best, best_dt = px, dt
    return best


def cl_move_pct(asset_upper: str, now_ts: float, window_sec: float, tol: float = 3.0):
    """(px_now - px_past)/px_past * 100, или None если данных не хватает.
    ИСПРАВЛЕНО: допуск для 'текущей' точки был жёстко зашит в 1.0с — при
    реальном темпе RTDS ~1 тик/сек с естественным дрожанием это почти
    всегда проваливалось (все входы падали с 'нет RTDS-сигнала', хотя
    сам RTDS был живой и STATUS показывал текущую цену). Используем
    RTDS_STALE_TIMEOUT_SEC — тот же порог, которым уже определяется
    'протухла ли chainlink-цена' в cl_price() — а не отдельную более
    жёсткую константу."""
    now_px = cl_price_at(asset_upper, now_ts, tol=RTDS_STALE_TIMEOUT_SEC)
    past_px = cl_price_at(asset_upper, now_ts - window_sec, tol=tol)
    if now_px is None or past_px is None or past_px == 0:
        return None
    return (now_px - past_px) / past_px * 100.0


async def chainlink_rtds_listener():
    """Polymarket RTDS → crypto_prices_chainlink (тот же источник, что и
    резолюция рынков). Реконнект с backoff + пинг + периодическая
    ресабскрайб (некоторые деплойменты RTDS шлют данные только сразу после
    (ре)подписки)."""
    sub_msg = {
        "action": "subscribe",
        "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}],
    }
    sym_to_asset = {v: k.upper() for k, v in CHAINLINK_SYMBOL.items() if k in ASSETS}

    backoff = WS_RECONNECT_BASE_SEC
    while True:
        ping_task = resub_task = watchdog_task = None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(RTDS_WS, heartbeat=5) as ws:
                    await ws.send_json(sub_msg)
                    log.info("RTDS подключён, символы=%s", list(sym_to_asset.keys()))
                    backoff = WS_RECONNECT_BASE_SEC

                    async def _pings():
                        while not ws.closed:
                            await asyncio.sleep(RTDS_PING_INTERVAL_SEC)
                            if not ws.closed:
                                try:
                                    await ws.ping()
                                except Exception:
                                    return

                    async def _resub():
                        while not ws.closed:
                            await asyncio.sleep(RTDS_RESUB_SEC)
                            if not ws.closed:
                                try:
                                    await ws.send_json(sub_msg)
                                except Exception:
                                    return

                    async def _watchdog():
                        while not ws.closed:
                            await asyncio.sleep(2)
                            if time.time() - _last_rtds_any > RTDS_STALE_TIMEOUT_SEC * 2:
                                log.warning("RTDS молчит >%.0fс — реконнект", RTDS_STALE_TIMEOUT_SEC * 2)
                                await ws.close()
                                return

                    ping_task = asyncio.create_task(_pings())
                    resub_task = asyncio.create_task(_resub())
                    watchdog_task = asyncio.create_task(_watchdog())

                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT or not msg.data:
                            continue
                        try:
                            data = json.loads(msg.data)
                        except Exception:
                            continue
                        events = data if isinstance(data, list) else [data]
                        for ev in events:
                            if not isinstance(ev, dict):
                                continue
                            payload = ev.get("payload") or {}
                            symbol = (payload.get("symbol") or "").lower()
                            if not symbol:
                                continue
                            asset_u = sym_to_asset.get(symbol)
                            if not asset_u:
                                continue
                            points = []
                            data_list = payload.get("data")
                            if isinstance(data_list, list) and data_list:
                                for pt in data_list:
                                    try:
                                        val = float(pt.get("value"))
                                        ts_ms = pt.get("timestamp")
                                        ts = float(ts_ms) / 1000.0 if ts_ms else time.time()
                                        points.append((ts, val))
                                    except (TypeError, ValueError):
                                        continue
                            elif payload.get("value") is not None:
                                try:
                                    val = float(payload["value"])
                                    ts_ms = payload.get("timestamp")
                                    ts = float(ts_ms) / 1000.0 if ts_ms else time.time()
                                    points.append((ts, val))
                                except (TypeError, ValueError):
                                    pass
                            if not points:
                                continue
                            async with _cl_lock:
                                for ts, val in points:
                                    _push_cl(asset_u, ts, val)
        except Exception as e:
            log.warning("RTDS обрыв (%s) — реконнект через %.1fс", e, backoff)
        finally:
            for t in (ping_task, resub_task, watchdog_task):
                if t:
                    t.cancel()
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, WS_RECONNECT_MAX_SEC)


# ===========================================================================
# КНИГА POLYMARKET (только для исполнения — не для сигнала)
# ===========================================================================
class BookCache:
    def __init__(self):
        self.best_ask: dict[str, float] = {}
        self.updated_at: dict[str, float] = {}
        self.ask_hist: dict[str, deque] = {}  # token -> deque[(ts, ask)]
        self.last_any_message_ts: float = 0.0
        self.last_book_update_ts: float = 0.0
        self.connected: bool = False

    def update(self, token_id: str, ask):
        now = time.time()
        if ask is not None:
            self.best_ask[token_id] = ask
            self.updated_at[token_id] = now
            self.last_book_update_ts = now
            hist = self.ask_hist.setdefault(token_id, deque(maxlen=120))
            if not hist or abs(hist[-1][1] - ask) > 1e-9 or (now - hist[-1][0]) >= 0.4:
                hist.append((now, ask))
        self.last_any_message_ts = now

    def get_ask(self, token_id: str):
        ts = self.updated_at.get(token_id)
        if ts is None or (time.time() - ts) > WS_STALE_FALLBACK_SEC:
            return None
        return self.best_ask.get(token_id)

    def ask_move_pct(self, token_id: str, window_sec: float):
        """Рост best ask за window_sec в % (положительный = ask вырос)."""
        hist = self.ask_hist.get(token_id)
        if not hist:
            return None
        now = time.time()
        now_ask = self.best_ask.get(token_id)
        if now_ask is None:
            return None
        past = None
        best_dt = None
        target = now - window_sec
        for ts, px in hist:
            dt = abs(ts - target)
            if dt <= 2.0 and (best_dt is None or dt < best_dt):
                past, best_dt = px, dt
        if past is None or past <= 0:
            return None
        return (now_ask - past) / past * 100.0


book_cache = BookCache()
_ws_wanted_tokens: set[str] = set()
_ws_subscribed_tokens: set[str] = set()
_ws_resubscribe_event = asyncio.Event()
_rest_fallback_count = 0


def ws_want_tokens(token_ids):
    changed = False
    for t in token_ids:
        if t not in _ws_wanted_tokens:
            _ws_wanted_tokens.add(t)
            changed = True
    if changed:
        _ws_resubscribe_event.set()

def ws_forget_tokens(token_ids):
    changed = False
    for t in token_ids:
        if t in _ws_wanted_tokens:
            _ws_wanted_tokens.discard(t)
            changed = True
        book_cache.best_ask.pop(t, None)
        book_cache.updated_at.pop(t, None)
        book_cache.ask_hist.pop(t, None)
    if changed:
        _ws_resubscribe_event.set()

def _parse_book_message(ev: dict):
    token_id = ev.get("asset_id") or ev.get("market") or ev.get("token_id")
    if token_id is None:
        return None, None
    ask = ev.get("best_ask")
    if ask is not None:
        ask = float(ask)
    if ask is None:
        asks = ev.get("asks") or ev.get("ask_prices")
        if asks:
            try:
                prices = [float(a["price"]) if isinstance(a, dict) else float(a) for a in asks]
                ask = min(prices) if prices else None
            except Exception:
                pass
    return token_id, ask

async def _ws_send_subscription(ws):
    tokens = list(_ws_wanted_tokens)
    if not tokens:
        return
    try:
        await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
        _ws_subscribed_tokens.clear()
        _ws_subscribed_tokens.update(tokens)
        log.info("WS книги: подписка на %d токенов", len(tokens))
    except Exception as e:
        log.warning("WS книги: не удалось отправить подписку: %s", e)

async def _ws_pinger(ws):
    try:
        while True:
            await asyncio.sleep(WS_PING_INTERVAL_SEC)
            await ws.send(json.dumps({"type": "ping"}))
    except (asyncio.CancelledError, Exception):
        return

async def ws_book_listener():
    """WS-книга нужна ТОЛЬКО для цены исполнения (ask), не для сигнала —
    сигнал теперь целиком из RTDS. Логика реконнекта/вотчдогов перенесена
    без изменений из momentum_ladder_v2.py (оба вотчдога: тишина сокета и
    отдельно тишина книги — второй ловит случаи, когда сокет формально
    жив, но конкретно book-события не идут)."""
    if not HAVE_WEBSOCKETS:
        log.error("WS: библиотека websockets не установлена — книга только через REST")
        return

    backoff = WS_RECONNECT_BASE_SEC
    while True:
        ping_task = None
        try:
            async with websockets.connect(BOOK_WS, ping_interval=None, close_timeout=3) as ws:
                book_cache.connected = True
                book_cache.last_any_message_ts = time.time()
                book_cache.last_book_update_ts = time.time()
                log.info("WS книги подключён")
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
                            log.warning("WS книги: молчит %.0fс при живом сокете — форсирую реконнект", book_silent)
                            break

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=WS_SILENT_TIMEOUT_SEC)
                    except asyncio.TimeoutError:
                        log.warning("WS книги: ни одного сообщения за %.0fс — реконнект", WS_SILENT_TIMEOUT_SEC)
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
                        token_id, ask = _parse_book_message(ev)
                        if token_id and ask is not None:
                            book_cache.update(token_id, ask)
        except Exception as e:
            log.warning("WS книги обрыв (%s) — реконнект через %.1fс", e, backoff)
        finally:
            book_cache.connected = False
            if ping_task:
                ping_task.cancel()
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, WS_RECONNECT_MAX_SEC)


async def get_best_ask_rest(session, token_id: str):
    try:
        async with session.get(f"{CLOB_REST}/price", params={"token_id": token_id, "side": "sell"},
                                timeout=aiohttp.ClientTimeout(total=4)) as r:
            if r.status == 200:
                data = await r.json()
                p = data.get("price")
                return float(p) if p is not None else None
    except Exception as e:
        log.debug("get_best_ask_rest(%s): %s", token_id, e)
    return None

async def get_best_ask(session, token_id: str):
    global _rest_fallback_count
    if USE_WS_MONITORING and HAVE_WEBSOCKETS:
        cached = book_cache.get_ask(token_id)
        if cached is not None:
            return cached
    _rest_fallback_count += 1
    return await get_best_ask_rest(session, token_id)


# ===========================================================================
# DISCOVERY — С ОПЕРЕЖЕНИЕМ на один слот (ключевой фикс против дыры в данных)
# ===========================================================================
_markets: dict[str, dict] = {}

async def _gamma_event(session, slug: str):
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

def _extract_tokens(ev: dict):
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

async def discover(session):
    """Дискаверит текущий слот И СЛЕДУЮЩИЙ (off=1) — чтобы к моменту
    реального открытия окна книга/RTDS уже были готовы, а не через минуту
    после. Это прямой фикс проблемы, найденной при анализе прошлой
    сессии: 52 из 59 рынков там не имели данных книги в первые 60с из-за
    позднего дискавери."""
    now = time.time()
    added = 0
    for asset in ASSETS:
        for tf in TIMEFRAMES:
            if tf not in SEG_BY_TF:
                continue
            seg = SEG_BY_TF[tf]
            for off in (0, 1):
                slot = (int(now) // seg) * seg + off * seg
                slug = f"{asset}-updown-{tf}-{slot}"
                if slug in _markets and _markets[slug]["end_ts"] > now - 30:
                    continue
                ev = await _gamma_event(session, slug)
                if not ev:
                    continue
                t_up, t_dn = _extract_tokens(ev)
                if not t_up:
                    continue
                _markets[slug] = dict(asset=asset, tf=tf, seg=seg, slot=slot, slug=slug,
                                       token_up=t_up, token_down=t_dn, end_ts=float(slot + seg),
                                       question=ev.get("title", slug))
                added += 1
                log.info("DISCOVERED %s (открытие через %.0fс)", slug, slot - now)
    dead = [s for s, m in _markets.items() if m["end_ts"] < now - 90]
    for s in dead:
        _markets.pop(s, None)
    return added

def live_markets():
    """Все обнаруженные и ещё не закрытые рынки (включая те, что
    физически ещё не открылись — это дискавери-с-опережением off=1).
    Используется только для статистики/статуса. НЕ использовать для
    запуска monitor_market — для этого см. tradeable_markets()."""
    now = time.time()
    return [m for m in _markets.values() if m["end_ts"] - now > STOP_BEFORE_SEC]

def tradeable_markets():
    """Рынки, которые УЖЕ реально открылись (slot <= now) и ещё не
    закрылись. ИСПРАВЛЕНИЕ: раньше market_loop использовал live_markets(),
    который не проверял нижнюю границу (m['slot'] <= now) — из-за этого
    monitor_market() запускался сразу в момент дискавери-с-опережением,
    т.е. за целый сегмент (299с) ДО открытия рынка. Вход всегда промахивался
    (60-секундное окно входа проходило впустую до открытия), WS-книга
    подписывалась/отписывалась на несуществующий ещё поток данных
    (бесконечные 'книга молчит 30с' реконнекты), а next_eligible_slot
    блокировал повторный запуск на реальном открытии окна — то есть
    каждое окно молча пропускалось целиком."""
    now = time.time()
    return [m for m in _markets.values()
            if m["slot"] <= now and m["end_ts"] - now > STOP_BEFORE_SEC]


# ===========================================================================
# CLOB — ИСПОЛНЕНИЕ (перенесено без изменений из momentum_ladder_v2.py)
# ===========================================================================
_client = None
_tick_cache: dict[str, str] = {}
_neg_risk_cache: dict[str, bool] = {}
_clob_lock = asyncio.Lock()

def build_clob_client():
    global _client
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
    _client = cl
    return cl

def _get_tick(token_id: str) -> str:
    if token_id not in _tick_cache:
        try:
            _tick_cache[token_id] = str(_client.get_tick_size(token_id))
        except Exception:
            _tick_cache[token_id] = "0.01"
    return _tick_cache[token_id]

def _get_neg_risk(token_id: str) -> bool:
    if token_id not in _neg_risk_cache:
        try:
            _neg_risk_cache[token_id] = _client.get_neg_risk(token_id)
        except Exception:
            _neg_risk_cache[token_id] = False
    return _neg_risk_cache[token_id]


def place_order(token_id: str, price: float, size: float, label: str):
    order_type = OrderType.FAK
    if price > 0:
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

    tick = _get_tick(token_id)
    neg_risk = _get_neg_risk(token_id)
    args = OrderArgs(token_id=token_id, price=price, size=size, side=Side.BUY)
    opts = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
    try:
        signed = _client.create_order(args, opts)
        resp = _client.post_order(signed, order_type)
        filled = float(resp.get("takingAmount", 0) or resp.get("size", 0) or 0)
        oid = resp.get("orderID") or resp.get("orderId")
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


def get_order_status(order_id: str):
    try:
        info = _client.get_order(order_id)
        matched = float(info.get("size_matched", 0) or 0)
        status = str(info.get("status", "")).lower()
        is_terminal = status in ("matched", "cancelled", "canceled", "filled")
        return matched, is_terminal
    except Exception as e:
        log.debug("get_order_status(%s): %s", order_id, e)
        return None


async def _verify_fill(oid, reported_filled: float, label: str) -> float:
    if not EXECUTION_ENABLED or not oid:
        return reported_filled
    await asyncio.sleep(ORDER_STATUS_POLL_SEC)
    status = await asyncio.to_thread(get_order_status, oid)
    if status is None:
        return reported_filled
    real_filled, _ = status
    if real_filled > reported_filled + EPS:
        log.warning("[%s] get_order_status показал больший matched-объём (%.4f) чем ответ (%.4f)",
                    label, real_filled, reported_filled)
        return real_filled
    return reported_filled


async def taker_buy_clip(session, token_id: str, label: str, ask_now: float, size: float,
                         price_ceiling: float | None = None) -> tuple[float, float]:
    """FAK-покупка с растущим буфером на ретраях (эскалация цены) и защитой
    от задвоения покупки. price_ceiling — опциональный потолок (dynamic max)."""
    remaining = size
    total_filled = 0.0
    total_cost = 0.0
    if price_ceiling is None:
        price_ceiling = MAX_CLIP_PRICE + BUY_PRICE_BUFFER
    else:
        price_ceiling = price_ceiling + BUY_PRICE_BUFFER
    price = min(ask_now + BUY_PRICE_BUFFER, price_ceiling)

    for attempt in range(1, FAK_ATTEMPTS + 1):
        if remaining <= EPS:
            break
        if EXECUTION_ENABLED:
            async with _clob_lock:
                filled, oid, ambiguous = await asyncio.to_thread(
                    place_order, token_id, price, remaining, f"{label} попытка {attempt}/{FAK_ATTEMPTS}")
            if filled > 0:
                filled = await _verify_fill(oid, filled, label)
                total_filled += filled
                total_cost += filled * price
                remaining -= filled
                continue
            if ambiguous and oid:
                await asyncio.sleep(0.5)
                status = await asyncio.to_thread(get_order_status, oid)
                if status:
                    real_filled, _ = status
                    if real_filled > 0:
                        total_filled += real_filled
                        total_cost += real_filled * price
                        remaining -= real_filled
                        continue
        else:
            total_filled += remaining
            total_cost += remaining * price
            remaining = 0.0
            break

        await asyncio.sleep(FAK_INTERVAL_SEC)
        fresh = await get_best_ask(session, token_id)
        if fresh is not None:
            escalated_buffer = BUY_PRICE_BUFFER + attempt * FAK_PRICE_ESCALATION_STEP
            price = min(fresh + escalated_buffer, price_ceiling)

    if total_filled <= EPS:
        return 0.0, price
    return total_filled, total_cost / total_filled


def clip_size_for_move(move_abs: float) -> float:
    """Ранжирование объёма по силе скачка Chainlink — единственное
    правило сайзинга (нет CLIP_SIZE_GROWTH/пирамидинга по числу клипов,
    это было частью старой орderbook-заточенной архитектуры)."""
    return CLIP_SIZE_STRONG if move_abs >= CL_STRONG_MOVE_PCT else CLIP_SIZE_BASE


# ===========================================================================
# МОНИТОР РЫНКА — одноуровневый реактивный сигнал по Chainlink
# ===========================================================================
async def monitor_market(session, m: dict):
    asset_u = m["asset"].upper()
    token_up, token_down = m["token_up"], m["token_down"]
    end_ts = m["end_ts"]
    slug = m["slug"]
    tokens = {"UP": token_up, "DOWN": token_down}

    if USE_WS_MONITORING:
        ws_want_tokens([token_up, token_down])

    log.info("[%s/%s] МОНИТОРИНГ: %s (rem=%.0fs)", asset_u, m["tf"], m["question"], end_ts - time.time())

    q = {"UP": 0.0, "DOWN": 0.0}
    cost = {"UP": 0.0, "DOWN": 0.0}
    n_clips = 0
    last_clip_ts = 0.0

    async def _try_buy(side: str, move: float) -> bool:
        """Ценовые фильтры перед покупкой — MIN_CLIP_PRICE <= ask <= MAX_CLIP_PRICE.
        Никакого book_confirm, skew-cap или dynamic-ceiling — сигнал
        целиком идёт от Chainlink, книга нужна только чтобы взять цену
        исполнения."""
        nonlocal n_clips, last_clip_ts
        token = tokens[side]
        ask = await get_best_ask(session, token)
        if ask is None:
            return False
        if ask > MAX_CLIP_PRICE:
            log.info("[%s/%s] CL %s пропущен: ask=%.3f > MAX_CLIP_PRICE=%.2f",
                     asset_u, m["tf"], side, ask, MAX_CLIP_PRICE)
            return False
        if ask < MIN_CLIP_PRICE:
            log.info("[%s/%s] CL %s пропущен: ask=%.3f < MIN_CLIP_PRICE=%.2f",
                     asset_u, m["tf"], side, ask, MIN_CLIP_PRICE)
            return False
        size = clip_size_for_move(abs(move))
        tier = "STRONG" if abs(move) >= CL_STRONG_MOVE_PCT else "BASE"
        label = f"{asset_u}/{m['tf']} CL_{tier}_{side}"
        log.warning("[%s] move=%.4f%% ask=%.3f size=%.0f clips=%d — покупаем",
                    label, move, ask, size, n_clips)
        filled, exec_price = await taker_buy_clip(session, token, label, ask, size, price_ceiling=MAX_CLIP_PRICE)
        if filled <= 0:
            return False
        n_clips += 1
        last_clip_ts = time.time()
        q[side] += filled
        cost[side] += filled * exec_price
        jlog({"event": "cl_buy", "slug": slug, "side": side, "tier": tier, "move_pct": move,
              "ask": ask, "price": exec_price, "size": filled, "n_clips": n_clips})
        return True

    # ---- единый реактивный цикл на весь срок жизни рынка ----
    while time.time() < end_ts - STOP_BEFORE_SEC and n_clips < MAX_CLIPS_PER_MARKET:
        now = time.time()
        if now - last_clip_ts < CL_COOLDOWN_SEC:
            await asyncio.sleep(MONITOR_POLL_SEC)
            continue

        move = cl_move_pct(asset_u, now, CL_LOOKBACK_SEC)
        if move is not None and abs(move) >= CL_MIN_MOVE_PCT:
            side = "UP" if move > 0 else "DOWN"
            await _try_buy(side, move)

        await asyncio.sleep(MONITOR_POLL_SEC)

    skew_side = "UP" if q["UP"] > q["DOWN"] else ("DOWN" if q["DOWN"] > q["UP"] else "НЕТ")
    log.info("[%s/%s] окно закрыто: %s | q_up=%.0f (cost=%.2f) q_down=%.0f (cost=%.2f) | перекос=%s | клипов=%d",
             asset_u, m["tf"], slug, q["UP"], cost["UP"], q["DOWN"], cost["DOWN"], skew_side, n_clips)
    jlog({"event": "market_closed", "slug": slug, "q_up": q["UP"], "cost_up": cost["UP"],
          "q_down": q["DOWN"], "cost_down": cost["DOWN"], "skew": skew_side, "n_clips": n_clips})

    if USE_WS_MONITORING:
        ws_forget_tokens([token_up, token_down])



# ===========================================================================
# ПЛАНИРОВЩИК
# ===========================================================================
async def discovery_loop(session):
    while True:
        try:
            n = await discover(session)
            if n:
                log.info("discovery +%d live=%d rtds_msgs≈%d", n, len(live_markets()), _rtds_msg_count)
        except Exception as e:
            log.warning("discovery: %s", e)
        await asyncio.sleep(DISCOVERY_SEC)


async def book_warmup_loop():
    """Держит WS-книгу подписанной на токены рынков, которые откроются в
    ближайшие BOOK_WARMUP_SEC секунд — реализует исходный смысл
    дискавери-с-опережением (книга тёплая к открытию), но БЕЗ запуска
    самого monitor_market/входа раньше времени (это было исходной
    причиной регрессии, см. tradeable_markets()). Отписывает токены сама,
    как только рынок реально стартовал — дальше подпиской рулит уже
    monitor_market() (её вызов идемпотентен, повторная подписка на уже
    подписанные токены безопасна)."""
    warmed: set[str] = set()
    while True:
        now = time.time()
        for m in _markets.values():
            slug = m["slug"]
            opens_in = m["slot"] - now
            if slug not in warmed and 0.0 <= opens_in <= BOOK_WARMUP_SEC:
                warmed.add(slug)
                ws_want_tokens([m["token_up"], m["token_down"]])
                log.info("[%s/%s] книга прогрета заранее (%.0fс до открытия)",
                         m["asset"].upper(), m["tf"], opens_in)
            elif slug in warmed and opens_in < 0.0:
                warmed.discard(slug)
        # чистим слаги закрытых рынков, чтобы set не рос бесконечно
        warmed &= {m["slug"] for m in _markets.values()}
        await asyncio.sleep(2.0)


async def market_loop(session):
    active: dict = {}
    next_eligible_slot: dict = {}
    while True:
        for m in tradeable_markets():
            key = m["slug"]
            ekey = (m["asset"], m["tf"])
            if key in active and not active[key].done():
                continue
            if key in active and active[key].done():
                exc = active[key].exception()
                if exc:
                    log.error("monitor %s упал: %s", key, exc)
            if m["slot"] < next_eligible_slot.get(ekey, 0):
                continue
            task = asyncio.create_task(monitor_market(session, m))
            active[key] = task
            next_eligible_slot[ekey] = m["slot"] + (SKIP_MARKETS_AFTER_TRADE + 1) * m["seg"]
        for s in list(active):
            if active[s].done():
                del active[s]
        await asyncio.sleep(1.0)


async def status_loop():
    while True:
        await asyncio.sleep(30)
        cl_info = {a: (f"{cl_price(a.upper())[1]:.2f}" if cl_price(a.upper()) else "stale") for a in ASSETS}
        book_silent = time.time() - book_cache.last_book_update_ts if book_cache.last_book_update_ts else None
        log.info("STATUS live=%d cl=%s книга_молчит=%s REST-фолбэков=%d",
                 len(live_markets()), cl_info,
                 f"{book_silent:.1f}с" if book_silent is not None else "н/д", _rest_fallback_count)


async def cl_calibration_logger():
    """Пишет в signal-лог сырые колебания Chainlink с фиксированным тактом
    (CL_CALIB_LOG_SEC), НЕЗАВИСИМО от того, идёт ли сейчас торговля —
    move% сразу на нескольких окнах (CL_CALIB_WINDOWS). Единственная цель
    — накопить данные для подбора CL_LOOKBACK_SEC/CL_MIN_MOVE_PCT/
    CL_STRONG_MOVE_PCT (см. докстринг файла — сейчас это грубая прикидка,
    не откалиброванные значения)."""
    while True:
        await asyncio.sleep(CL_CALIB_LOG_SEC)
        now = time.time()
        for asset in ASSETS:
            asset_u = asset.upper()
            cur = cl_price(asset_u)
            if cur is None:
                continue
            moves = {}
            for w in CL_CALIB_WINDOWS:
                mv = cl_move_pct(asset_u, now, w)
                if mv is not None:
                    moves[f"{w:g}s"] = round(mv, 5)
            if moves:
                jlog({"event": "cl_calib", "asset": asset_u, "price": cur[1], "moves_pct": moves})


async def main():
    log.info("=" * 72)
    log.info("chainlink_reactive_bot.py — СТАРТ (paper=%s)", not EXECUTION_ENABLED)
    log.info("Активы: %s | Таймфреймы: %s", ASSETS, TIMEFRAMES)
    log.info("Сигнал (одноуровневый, чистый Chainlink): lookback=%.1fs min_move=%.3f%% "
             "strong_move=%.3f%% (>=strong -> size=%.0f, иначе size=%.0f) | cooldown=%.1fs | "
             "ценовые фильтры: MIN_CLIP_PRICE=%.2f MAX_CLIP_PRICE=%.2f",
             CL_LOOKBACK_SEC, CL_MIN_MOVE_PCT, CL_STRONG_MOVE_PCT,
             CLIP_SIZE_STRONG, CLIP_SIZE_BASE, CL_COOLDOWN_SEC, MIN_CLIP_PRICE, MAX_CLIP_PRICE)
    log.info("08.08 v3: убраны все book-заточенные фильтры (book_confirm/skew-cap/"
             "dynamic-price/FIRST-PYRAMID split) — ядро сдвинуто целиком на Chainlink. "
             "CL_LOOKBACK_SEC/CL_MIN_MOVE_PCT НЕ откалиброваны, см. докстринг и "
             "cl_calibration_logger(). Нужен paper-прогон.")
    log.info("=" * 72)

    if EXECUTION_ENABLED:
        if not HAVE_CLOB:
            log.error("EXECUTION_ENABLED=true, но py_clob_client_v2 не установлен — выход")
            return
        build_clob_client()
        log.info("CLOB-клиент инициализирован (боевой режим)")
    else:
        log.info("PAPER-режим")

    async with aiohttp.ClientSession() as session:
        await discover(session)
        tasks = [chainlink_rtds_listener(), discovery_loop(session), book_warmup_loop(),
                 market_loop(session), status_loop(), cl_calibration_logger()]
        if USE_WS_MONITORING:
            if HAVE_WEBSOCKETS:
                tasks.append(ws_book_listener())
            else:
                log.error("USE_WS_MONITORING=true, но 'websockets' не установлен — книга только через REST")
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено по Ctrl+C")