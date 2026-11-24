#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
closer.py
---------
Никаких бирж, никакого пересечения — только время и текущая цена самого
Polymarket. За FIRE_BEFORE_SEC (по умолчанию 3.5) секунд до конца каждого
5m/15m окна смотрим, какая сторона (Up/Down) СЕЙЧАС дороже на Polymarket
(т.е. рынок её уже считает более вероятным победителем) — и покупаем именно
её по цене до ENTRY_PRICE (по умолчанию 0.95), в несколько быстрых попыток.

ОСНОВАНИЕ (проверено на реальных данных):
  - На посекундных данных 60 дней: состояние "какая сторона впереди"
    ровно за 3-5 секунд до конца совпадает с финальным исходом в 97.5-100%
    случаев (BTC/ETH, оба таймфрейма) — почти всегда уже решено.
  - НО на реальном ордербуке Polymarket (480 15м-рынков, посекундные
    цены): даже за 3-5 секунд до конца в 5-9% случаев цена ПОБЕДИВШЕЙ
    стороны всё ещё была ниже 0.95 (минимум видели 0.01-0.03) — то есть
    рынок не всегда успевает это отразить в цене даже так поздно.
  - Идея бота: не пытаться предсказывать заранее (как flash.py), а просто
    систематически ловить именно этот узкий хвост "рынок ещё не дооценил
    почти решённый исход" в последние секунды перед закрытием.

ВАЖНО:
  - Это НЕ гарантия победы — 94-100% исторической точности состояния
    "кто сейчас впереди" не означает 100% для КАЖДОЙ конкретной сделки,
    и цена 0.95 всё равно оставляет риск (если ошибка случится — теряем
    почти всю ставку, выигрыш даёт только +0.05 на каждый доллар).
  - Резолв Polymarket идёт по Chainlink, а мы ориентируемся на цену
    самого Polymarket (не на биржи) — здесь другой тип риска: если
    ПОЛИМАРКЕТ сам предсказывает неверно (а не просто отстаёт от бирж),
    этот бот унаследует ту же ошибку, у него нет независимого сигнала.
  - WS/REST-эндпоинты Polymarket собраны по образцу других ботов этой же
    серии — но конкретно для XRP формат рынка не подтверждён, сверить в
    paper-режиме.
  - ДОБАВЛЕНО: проверка консенсуса бирж по исходу свечи перед выставлением
    ордеров. Ровно за CONSENSUS_BEFORE_SEC (по умолчанию 4.5с) до конца
    окна сверяем состояние свечи (цена выше/ниже open) на 3 биржах:
    Binance, OKX, Bybit. Нужен консенсус 3 из 3 (одинаковый исход у всех),
    иначе окно пропускается целиком. Остальной алгоритм (выбор более
    дорогой стороны на Polymarket, момент удара за FIRE_BEFORE_SEC=3.5с)
    не изменён.
"""

import os, sys, json, time, asyncio, logging
from decimal import Decimal, ROUND_HALF_UP
import aiohttp

_dotenv_loaded = False
try:
    from dotenv import load_dotenv
    _dotenv_loaded = load_dotenv()
except ImportError:
    print("ВНИМАНИЕ: python-dotenv не установлен — .env не будет прочитан! "
          "Установи: pip install python-dotenv", file=sys.stderr)

try:
    from py_clob_client_v2 import (
        ApiCreds, BuilderConfig, ClobClient, OrderArgs, OrderType,
        PartialCreateOrderOptions, Side,
    )
    HAVE_CLOB = True
except ImportError:
    HAVE_CLOB = False

BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"

# ===========================================================================
# КОНФИГ
# ===========================================================================
GAMMA_REST = "https://gamma-api.polymarket.com"
CLOB_REST  = "https://clob.polymarket.com"

# --- Источники для проверки консенсуса бирж по исходу свечи (см. ниже) ---
BINANCE_REST = "https://api.binance.com"
BINANCE_WS = "wss://stream.binance.com:9443/stream"
OKX_WS = "wss://ws.okx.com:8443/ws/v5/public"
BYBIT_WS = "wss://stream.bybit.com/v5/public/spot"

BINANCE_SYMBOL = {"btc": "BTCUSDT", "eth": "ETHUSDT", "xrp": "XRPUSDT", "sol": "SOLUSDT"}
OKX_INSTID     = {"btc": "BTC-USDT", "eth": "ETH-USDT", "xrp": "XRP-USDT", "sol": "SOL-USDT"}
BYBIT_SYMBOL   = {"btc": "BTCUSDT", "eth": "ETHUSDT", "xrp": "XRPUSDT", "sol": "SOLUSDT"}

EXECUTION_ENABLED = os.environ.get("EXECUTION_ENABLED", "false").lower() == "true"
PRIVATE_KEY       = os.environ.get("POLY_WALLET_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY", "")
FUNDER            = os.environ.get("POLY_FUNDER_ADDRESS") or os.environ.get("FUNDER", "")
SIGNATURE_TYPE    = int(os.environ.get("POLY_SIGNATURE_TYPE", "3"))
CHAIN_ID          = int(os.environ.get("POLY_CHAIN_ID", "137"))
API_KEY           = os.environ.get("CLOB_API_KEY", "")
API_SECRET        = os.environ.get("CLOB_API_SECRET", "")
API_PASSPHRASE    = os.environ.get("CLOB_API_PASSPHRASE", "")

# Все 4 актива в одном процессе, один .env на всё — здесь нет бирж и нет
# причин разносить по отдельным процессам (в отличие от flash.py, где
# разделение было нужно специально для проверки конкуренции между
# биржевыми WS-подключениями в одном event loop).
ASSETS = [a.strip().lower() for a in os.environ.get("ASSETS", "btc,eth,xrp,sol").split(",") if a.strip()]

SEGMENTS = {"5m": 300, "15m": 900}

# --- Когда взводить (заранее получить рынок) и когда бить ---
ARM_BEFORE_SEC   = float(os.environ.get("ARM_BEFORE_SEC", "10"))   # запас на получение рынка заранее
FIRE_BEFORE_SEC  = float(os.environ.get("FIRE_BEFORE_SEC", "3.5")) # момент удара

# --- Проверка консенсуса бирж по исходу СВЕЧИ перед выставлением ордеров ---
# Ровно за CONSENSUS_BEFORE_SEC (по умолчанию 4.5с) до конца окна смотрим
# текущее состояние свечи (цена выше/ниже open) на 3 биржах: Binance, OKX,
# Bybit. Нужен консенсус 3 из 3 (одинаковый исход у всех) — иначе сделка
# по этому окну пропускается целиком. Дальше (за FIRE_BEFORE_SEC, 3.5с)
# бот бьёт как раньше, без изменений в самом алгоритме выбора стороны —
# это только доп. фильтр "пропустить/не пропустить".
CONSENSUS_CHECK_ENABLED = os.environ.get("CONSENSUS_CHECK_ENABLED", "true").lower() == "true"
CONSENSUS_BEFORE_SEC    = float(os.environ.get("CONSENSUS_BEFORE_SEC", "4.5"))
CONSENSUS_EXCHANGES     = ["binance", "okx", "bybit"]
CONSENSUS_REQUIRED      = len(CONSENSUS_EXCHANGES)  # 3 из 3
# Максимальный возраст последней цены биржи, чтобы её "голос" считался
# актуальным, а не устаревшим значением (биржа давно не торговала активом).
MAX_PRICE_AGE_SEC = float(os.environ.get("MAX_PRICE_AGE_SEC", "2.0"))

# --- Цена и размер ---
ENTRY_PRICE = float(os.environ.get("ENTRY_PRICE", "0.95"))
SIZE = float(os.environ.get("SIZE", "10"))
MIN_ORDER_VALUE = float(os.environ.get("MIN_ORDER_VALUE", "1.05"))

# --- 3 попытки, каждая с шагом 0.3 секунды ---
FAK_ATTEMPTS = int(os.environ.get("FAK_ATTEMPTS", "3"))
FAK_INTERVAL_SEC = float(os.environ.get("FAK_INTERVAL_SEC", "0.3"))

# Как часто планировщик проверяет "не пора ли ударить" — должно быть
# заметно чаще 1 секунды, иначе легко проскочить узкое окно в 3.5с.
SCHEDULER_POLL_SEC = float(os.environ.get("SCHEDULER_POLL_SEC", "0.1"))

logging.basicConfig(level=logging.INFO)
class MillisecondFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        s = time.strftime("%H:%M:%S", ct)
        return f"{s}.{int(record.msecs):03d}"
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(MillisecondFormatter("%(asctime)s | %(levelname)s | %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_console_handler])
log = logging.getLogger("closer")
_fh = logging.FileHandler(f"closer_trades_{'_'.join(ASSETS)}.log", encoding="utf-8")
_fh.setFormatter(MillisecondFormatter("%(asctime)s | %(levelname)s | %(message)s"))
log.addHandler(_fh)

# ===========================================================================
# POLYMARKET
# ===========================================================================

async def fetch_event_markets(session, slug):
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

def extract_up_token(markets):
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

async def get_current_market(session, asset, tf_label, seg):
    now = int(time.time())
    slot = (now // seg) * seg
    slug = f"{asset}-updown-{tf_label}-{slot}"
    ms = await fetch_event_markets(session, slug)
    ty, tn, q = extract_up_token(ms)
    if ty is None:
        return None
    return {"slot": slot, "slug": slug, "token_up": ty, "token_down": tn, "question": q, "tf": tf_label, "seg": seg}

async def get_best_ask(session, token_id):
    try:
        async with session.get(
            f"{CLOB_REST}/price",
            params={"token_id": token_id, "side": "sell"},
            timeout=aiohttp.ClientTimeout(total=4),
        ) as r:
            if r.status == 200:
                data = await r.json()
                price = data.get("price")
                return float(price) if price is not None else None
    except Exception as e:
        log.debug("get_best_ask(%s): %s", token_id, e)
    return None

async def get_binance_window_open(session, symbol, tf_label, slot):
    interval = tf_label
    try:
        async with session.get(
            f"{BINANCE_REST}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "startTime": slot*1000, "limit": 1},
            timeout=aiohttp.ClientTimeout(total=6),
        ) as r:
            if r.status == 200:
                data = await r.json()
                if data:
                    return float(data[0][1])
    except Exception as e:
        log.debug("get_binance_window_open(%s): %s", symbol, e)
    return None

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

_tick_cache = {}
_neg_risk_cache = {}

def get_tick(client, token_id):
    if token_id not in _tick_cache:
        try:
            _tick_cache[token_id] = str(client.get_tick_size(token_id))
        except Exception:
            _tick_cache[token_id] = "0.01"
    return _tick_cache[token_id]

def get_neg_risk(client, token_id):
    if token_id not in _neg_risk_cache:
        try:
            _neg_risk_cache[token_id] = client.get_neg_risk(token_id)
        except Exception:
            _neg_risk_cache[token_id] = False
    return _neg_risk_cache[token_id]

def place_order(client, token_id, price, size, label, order_type=None):
    if order_type is None:
        order_type = OrderType.FAK
    if price > 0:
        min_size_for_value = MIN_ORDER_VALUE / price
        target_size = size if size >= min_size_for_value else min_size_for_value
        price_d = Decimal(str(round(price, 2)))
        size_d = Decimal(str(round(target_size, 4)))
        maker_d = (price_d * size_d).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if maker_d < Decimal(str(MIN_ORDER_VALUE)):
            maker_d = Decimal(str(MIN_ORDER_VALUE)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        size_d = (maker_d / price_d).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        step = Decimal("0.0001")
        for i in range(101):
            offset = (i // 2 + 1) * step if i % 2 else Decimal(0)
            if i % 4 >= 2:
                offset = -offset
            candidate = size_d + offset
            if candidate <= 0:
                continue
            check = price_d * candidate
            if check == check.quantize(Decimal("0.01")):
                size_d = candidate
                break
        price, size = float(price_d), float(size_d)
    else:
        size = round(size, 4)

    tick = get_tick(client, token_id)
    neg_risk = get_neg_risk(client, token_id)
    args = OrderArgs(token_id=token_id, price=price, size=size, side=Side.BUY)
    opts = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
    try:
        signed = client.create_order(args, opts)
        resp = client.post_order(signed, order_type)
        filled = float(resp.get("takingAmount", 0) or resp.get("size", 0) or 0)
        oid = resp.get("orderID") or resp.get("orderId")
        status = resp.get("status", "?")
        making_amount = resp.get("makingAmount")
        actual_price = None
        if making_amount is not None and filled > 0:
            try:
                actual_price = float(making_amount) / filled
            except (ValueError, ZeroDivisionError):
                actual_price = None
        if actual_price is not None:
            log.info("  [%s] запрошено=%.2f size=%.4f -> filled=%.4f РЕАЛЬНАЯ_цена=%.4f status=%s oid=%s",
                     label, price, size, filled, actual_price, status, oid)
        else:
            log.info("  [%s] запрошено=%.2f size=%.4f -> filled=%.4f status=%s oid=%s",
                     label, price, size, filled, status, oid)
        return filled, oid
    except Exception as e:
        log.error("  [%s] price=%.2f size=%.4f -> %s", label, price, size, e)
        return 0.0, None

# ===========================================================================
# ЖИВЫЕ ЦЕНЫ: WebSocket Binance + OKX + Bybit (только для проверки консенсуса)
# ===========================================================================

ASSET_UPPER = [a.upper() for a in ASSETS]
latest_price = {ex: {a: None for a in ASSET_UPPER} for ex in CONSENSUS_EXCHANGES}
latest_price_time = {ex: {a: None for a in ASSET_UPPER} for ex in CONSENSUS_EXCHANGES}
price_lock = asyncio.Lock()

def match_asset_prefix(symbol_or_instid):
    """BTCUSDT/BTC-USDT -> BTC, XRPUSDT/XRP-USDT -> XRP и т.д."""
    for a in ASSET_UPPER:
        if symbol_or_instid.startswith(a):
            return a
    return None

async def binance_ws_listener():
    streams = "/".join(f"{BINANCE_SYMBOL[a].lower()}@trade" for a in ASSETS)
    url = f"{BINANCE_WS}?streams={streams}"
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url, heartbeat=20) as ws:
                    log.info("Binance WS (консенсус) подключен")
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        data = json.loads(msg.data)
                        payload = data.get("data", {})
                        sym = payload.get("s", "")
                        price = payload.get("p")
                        if price is None:
                            continue
                        asset = match_asset_prefix(sym)
                        if asset is None:
                            continue
                        async with price_lock:
                            latest_price["binance"][asset] = float(price)
                        latest_price_time["binance"][asset] = time.time()
        except Exception as e:
            log.warning("Binance WS (консенсус) обрыв: %s - переподключение через 3с", e)
            await asyncio.sleep(3)

async def okx_ws_listener():
    sub_msg = {"op": "subscribe", "args": [
        {"channel": "trades", "instId": OKX_INSTID[a]} for a in ASSETS
    ]}
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(OKX_WS, heartbeat=20) as ws:
                    await ws.send_json(sub_msg)
                    log.info("OKX WS (консенсус) подключен и подписан")
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        data = json.loads(msg.data)
                        trades = data.get("data")
                        if not trades:
                            continue
                        inst_id = data.get("arg", {}).get("instId", "")
                        asset = match_asset_prefix(inst_id)
                        if asset is None:
                            continue
                        price = trades[-1].get("px")
                        if price is None:
                            continue
                        async with price_lock:
                            latest_price["okx"][asset] = float(price)
                        latest_price_time["okx"][asset] = time.time()
        except Exception as e:
            log.warning("OKX WS (консенсус) обрыв: %s - переподключение через 3с", e)
            await asyncio.sleep(3)

async def bybit_ws_listener():
    sub_msg = {"op": "subscribe", "args": [f"publicTrade.{BYBIT_SYMBOL[a]}" for a in ASSETS]}
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(BYBIT_WS, heartbeat=20) as ws:
                    await ws.send_json(sub_msg)
                    log.info("Bybit WS (консенсус) подключен и подписан")
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        data = json.loads(msg.data)
                        trades = data.get("data")
                        if not trades:
                            continue
                        for t in trades:
                            sym = t.get("s", "")
                            asset = match_asset_prefix(sym)
                            if asset is None:
                                continue
                            price = t.get("p")
                            if price is None:
                                continue
                            async with price_lock:
                                latest_price["bybit"][asset] = float(price)
                            latest_price_time["bybit"][asset] = time.time()
        except Exception as e:
            log.warning("Bybit WS (консенсус) обрыв: %s - переподключение через 3с", e)
            await asyncio.sleep(3)

def check_consensus(asset, window_open):
    """Сверяем состояние свечи (цена выше/ниже open) на всех 3 биржах по уже
    известным последним ценам (без ожидания новых тиков - максимально
    актуально на момент вызова, без доп. задержки). Нужен консенсус
    CONSENSUS_REQUIRED (3 из 3), иначе результат = (False, ...)."""
    if window_open is None:
        return False, ["window_open=?"]
    a = asset.upper()
    check_time = time.time()
    signs = []
    details = []
    for ex in CONSENSUS_EXCHANGES:
        p = latest_price[ex][a]
        ts = latest_price_time[ex][a]
        if p is None or ts is None:
            details.append(f"{ex}=?")
            continue
        age = check_time - ts
        if age > MAX_PRICE_AGE_SEC:
            details.append(f"{ex}=устарела({age:.1f}с)")
            continue
        sign = 1 if p > window_open else (-1 if p < window_open else 0)
        signs.append(sign)
        details.append(f"{ex}={'Up' if sign>0 else ('Down' if sign<0 else 'Flat')}({age:.2f}с)")
    if len(signs) < CONSENSUS_REQUIRED:
        return False, details
    if len(set(signs)) == 1 and signs[0] != 0:
        return True, details
    return False, details

# ===========================================================================
# СОСТОЯНИЕ ОКОН + УДАР ПО ВРЕМЕНИ
# ===========================================================================

class WindowState:
    __slots__ = ("slot", "market", "armed", "fired", "window_open",
                 "consensus_checked", "consensus_passed")
    def __init__(self):
        self.slot = None
        self.market = None
        self.armed = False
        self.fired = False
        self.window_open = None
        self.consensus_checked = False
        self.consensus_passed = False

states = {(a, tf): WindowState() for a in ASSETS for tf in SEGMENTS}

async def fire_at_deadline(session, client, asset, tf_label, market):
    """Смотрим ТЕКУЩИЕ цены Up и Down на самом Polymarket (не биржи!) —
    какая сторона сейчас дороже (>0.5), ту рынок и считает вероятным
    победителем. Покупаем именно её, если цена всё ещё <= ENTRY_PRICE."""
    t0 = time.time()
    token_up = market["token_up"]
    token_down = market["token_down"]
    ask_up, ask_down = await asyncio.gather(
        get_best_ask(session, token_up),
        get_best_ask(session, token_down),
    )
    if ask_up is None and ask_down is None:
        log.warning("[%s/%s] обе цены недоступны, пропуск удара", asset.upper(), tf_label)
        return
    # выбираем более дорогую (= более вероятного победителя по мнению рынка)
    if (ask_up or 0) >= (ask_down or 0):
        token_id, side_label, side_price = token_up, "Up", ask_up
    else:
        token_id, side_label, side_price = token_down, "Down", ask_down
    label = f"{asset.upper()}-{side_label}"
    log.warning("[%s/%s] УДАР по времени: Up=%s Down=%s -> выбрано %s (цена=%s), t+%.3fс",
                 asset.upper(), tf_label, ask_up, ask_down, label, side_price, time.time()-t0)

    for attempt in range(1, FAK_ATTEMPTS + 1):
        elapsed = time.time() - t0
        if EXECUTION_ENABLED:
            filled, oid = await asyncio.to_thread(place_order, client, token_id, ENTRY_PRICE, SIZE,
                                                    f"{label} {tf_label} попытка {attempt}/{FAK_ATTEMPTS} t+{elapsed:.3f}с")
            if filled > 0:
                log.info("[%s] исполнено %.4f за %.3fс от удара", label, filled, time.time()-t0)
                return
        else:
            log.info("PAPER [%s попытка %d/%d, t+%.3fс] price=%.2f size=%.2f",
                     label, attempt, FAK_ATTEMPTS, elapsed, ENTRY_PRICE, SIZE)
            return
        if attempt < FAK_ATTEMPTS:
            await asyncio.sleep(FAK_INTERVAL_SEC)
    log.warning("[%s] не набрано ничего за %d попыток", label, FAK_ATTEMPTS)

async def scheduler(session, client):
    while True:
        now = time.time()
        now_int = int(now)
        for asset in ASSETS:
            for tf_label, seg in SEGMENTS.items():
                st = states[(asset, tf_label)]
                slot = (now_int // seg) * seg
                remaining = slot + seg - now

                if st.slot != slot:
                    st.slot = slot
                    st.market = None
                    st.armed = False
                    st.fired = False
                    st.window_open = None
                    st.consensus_checked = False
                    st.consensus_passed = False

                if not st.armed and remaining <= ARM_BEFORE_SEC:
                    mkt = await get_current_market(session, asset, tf_label, seg)
                    wopen = None
                    if CONSENSUS_CHECK_ENABLED:
                        wopen = await get_binance_window_open(session, BINANCE_SYMBOL[asset], tf_label, slot)
                    if mkt is not None:
                        st.market = mkt
                        st.window_open = wopen
                        st.armed = True
                        log.info("[%s/%s] ВЗВЕДЕНО: осталось %.1fs, рынок=%s, open=%s",
                                  asset.upper(), tf_label, remaining, mkt["slug"], wopen)
                    else:
                        log.warning("[%s/%s] не удалось взвести (рынок не найден)", asset.upper(), tf_label)

                # Ровно за CONSENSUS_BEFORE_SEC (по умолчанию 4.5с) до конца -
                # разовая проверка консенсуса 3 бирж по исходу свечи.
                if (CONSENSUS_CHECK_ENABLED and st.armed and not st.consensus_checked
                        and remaining <= CONSENSUS_BEFORE_SEC):
                    st.consensus_checked = True
                    st.consensus_passed, details = check_consensus(asset, st.window_open)
                    if st.consensus_passed:
                        log.info("[%s/%s] консенсус ПОДТВЕРЖДЁН (%d из %d бирж согласны) [%s], t-%.2fс",
                                  asset.upper(), tf_label, CONSENSUS_REQUIRED, len(CONSENSUS_EXCHANGES),
                                  ", ".join(details), remaining)
                    else:
                        log.info("[%s/%s] консенсус НЕ набран [%s] - окно пропускается, t-%.2fс",
                                  asset.upper(), tf_label, ", ".join(details), remaining)

                if st.armed and not st.fired and remaining <= FIRE_BEFORE_SEC:
                    st.fired = True
                    if CONSENSUS_CHECK_ENABLED and not st.consensus_passed:
                        log.info("[%s/%s] пропуск удара: консенсус бирж не подтверждён", asset.upper(), tf_label)
                    else:
                        asyncio.create_task(fire_at_deadline(session, client, asset, tf_label, st.market))

        await asyncio.sleep(SCHEDULER_POLL_SEC)

# ===========================================================================
# MAIN
# ===========================================================================

async def main():
    log.info("=" * 70)
    log.info("  CLOSER BOT - удар по времени за %.1fс до конца окна, без бирж", FIRE_BEFORE_SEC)
    log.info("  .env найден и прочитан: %s", _dotenv_loaded)
    log.info("  АКТИВЫ (зашиты в коде): %s", ASSETS)
    log.info("  EXECUTION=%s", EXECUTION_ENABLED)
    log.info("  Цена входа: %.2f  Размер: %s  Попыток: %d, шаг %.1fс",
              ENTRY_PRICE, SIZE, FAK_ATTEMPTS, FAK_INTERVAL_SEC)
    log.info("  Консенсус бирж: %s (за %.1fс до конца, нужно %d из %d: %s)",
              CONSENSUS_CHECK_ENABLED, CONSENSUS_BEFORE_SEC, CONSENSUS_REQUIRED,
              len(CONSENSUS_EXCHANGES), ", ".join(CONSENSUS_EXCHANGES))
    log.info("  ВНИМАНИЕ: не проверено вживую — обязательно paper-режим перед реальными деньгами")
    log.info("=" * 70)

    client = None
    if EXECUTION_ENABLED:
        if not HAVE_CLOB:
            log.error("py_clob_client_v2 не установлен")
            return
        try:
            client = build_clob_client()
            log.warning("CLOB авторизован. РЕАЛЬНОЕ ИСПОЛНЕНИЕ!")
        except Exception as e:
            log.error("Ошибка авторизации: %s", e)
            return
    else:
        log.info("PAPER-РЕЖИМ")

    async with aiohttp.ClientSession() as session:
        if CONSENSUS_CHECK_ENABLED:
            await asyncio.gather(
                binance_ws_listener(),
                okx_ws_listener(),
                bybit_ws_listener(),
                scheduler(session, client),
            )
        else:
            await scheduler(session, client)

if __name__ == "__main__":
    asyncio.run(main())
