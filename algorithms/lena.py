#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bb_rsi_5m_bot.py
-----------------
Бот ловли реверс-свечей по BTC и ETH на Polymarket, ТОЛЬКО 5-минутные рынки.

ДВА НЕЗАВИСИМЫХ триггера (любой из двух срабатывает — не требуется оба
одновременно, они почти не пересекаются: ~5-10% на реальных данных):

  1) Bollinger %B: |close - SMA20| / (2*STD20) >= BB_THRESHOLD (default 1.5)
  2) RSI21: <= RSI_LO или >= RSI_HI (default 20/80)

Оба посчитаны и провалидированы на 6-месячной OHLC-истории BTC 5m:
  - Bollinger20>=1.5: n=365, win_rate=59.73%, lift=x1.17
  - RSI21 20/80:      n=682, win_rate=57.04%, lift=x1.11

ВАЖНО: реальные ордербуки 5m у нас имеющиеся сильно обрезаны по времени
(не хватает последних 7-10 секунд каждого рынка) — финансовая экономика
именно для 5m ПОКА НЕ подтверждена так же надёжно, как для 15m. Работайте
с этим бота с повышенной осторожностью, желательно сначала в PAPER-режиме
дольше обычного, пока не накопится собственная статистика.

Направление: ОДНОСТОРОННЕЕ — реверс направления сигнальной свечи (как и
в 15-минутном боте):
  - сигнальная свеча ЗАКРЫЛАСЬ ВЫШЕ открытия (рост) -> ставим NO
  - сигнальная свеча ЗАКРЫЛАСЬ НИЖЕ открытия (падение) -> ставим YES

Ордера: сетка цен 0.01/0.02/0.05/0.10/0.20/0.30 (БЕЗ 0.40), размеры через
SIZE_BY_PRICE/SIZE_DEFAULT (env). Плюс хедж на противоположную сторону по
HEDGE_PRICE (default 0.01), как в 15-минутном боте.

SKIP_FIRST_TRIGGER=true (default): первый цикл на каждый инструмент только
заполняет буферы Bollinger/RSI молча, без ордеров.
"""

import os, json, time, asyncio, logging, collections
import aiohttp

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from py_clob_client_v2 import (
        ApiCreds, ClobClient, OrderArgs, OrderType,
        PartialCreateOrderOptions, Side,
    )
    HAVE_CLOB = True
except ImportError:
    HAVE_CLOB = False

# ===========================================================================
# КОНФИГ
# ===========================================================================
GAMMA_REST = "https://gamma-api.polymarket.com"
CLOB_REST  = "https://clob.polymarket.com"
OKX_REST   = "https://www.okx.com"

EXECUTION_ENABLED = os.environ.get("EXECUTION_ENABLED", "false").lower() == "true"
PRIVATE_KEY       = os.environ.get("POLY_WALLET_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY", "")
FUNDER            = os.environ.get("POLY_FUNDER_ADDRESS") or os.environ.get("FUNDER", "")
SIGNATURE_TYPE    = int(os.environ.get("POLY_SIGNATURE_TYPE", "3"))
CHAIN_ID          = int(os.environ.get("POLY_CHAIN_ID", "137"))
API_KEY           = os.environ.get("CLOB_API_KEY", "")
API_SECRET        = os.environ.get("CLOB_API_SECRET", "")
API_PASSPHRASE    = os.environ.get("CLOB_API_PASSPHRASE", "")

# --- Ордера (сетка БЕЗ 0.40) ---
LIMIT_PRICES = [float(x) for x in os.environ.get("LIMIT_PRICES", "0.01,0.02,0.05,0.10,0.20,0.30").split(",")]
SIZE_DEFAULT = int(os.environ.get("SIZE_DEFAULT", "20"))
_size_env = os.environ.get("SIZE_BY_PRICE", "")
if _size_env:
    SIZE_BY_PRICE = {}
    for item in _size_env.split(","):
        p, s = item.strip().split(":")
        SIZE_BY_PRICE[float(p)] = int(s)
else:
    SIZE_BY_PRICE = {p: SIZE_DEFAULT for p in LIMIT_PRICES}

# --- Хедж на противоположную сторону (только по HEDGE_PRICE) ---
HEDGE_ENABLED = os.environ.get("HEDGE_ENABLED", "true").lower() == "true"
HEDGE_PRICE   = float(os.environ.get("HEDGE_PRICE", "0.01"))
HEDGE_SIZE    = int(os.environ.get("HEDGE_SIZE", "120"))

# --- Рынки: ТОЛЬКО 5 минут ---
MARKETS_5M = int(os.environ.get("MARKETS_5M", "1"))   # горизонт +1 свеча
SEG_5M = 300

# --- Bollinger %B триггер ---
BB_PERIOD    = int(os.environ.get("BB_PERIOD", "20"))
BB_THRESHOLD = float(os.environ.get("BB_THRESHOLD", "1.5"))

# --- RSI триггер ---
RSI_PERIOD = int(os.environ.get("RSI_PERIOD", "21"))
RSI_LO     = float(os.environ.get("RSI_LO", "20"))
RSI_HI     = float(os.environ.get("RSI_HI", "80"))

# --- Защита от старта / устаревших триггеров ---
SKIP_FIRST_TRIGGER = os.environ.get("SKIP_FIRST_TRIGGER", "true").lower() == "true"
STALE_THRESHOLD_SEC = int(os.environ.get("STALE_THRESHOLD_SEC", "30"))  # 5м свеча короче -> порог тоже мягче

# --- Цикл ---
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "15"))  # 5м рынки короче -> опрашиваем чаще
COOLDOWN_SEC       = int(os.environ.get("COOLDOWN_SEC", "120"))

INSTRUMENTS = [
    {"instId": "BTC-USDT", "asset": "BTC", "bar": "5m", "seg": SEG_5M, "markets_n": MARKETS_5M},
    {"instId": "ETH-USDT", "asset": "ETH", "bar": "5m", "seg": SEG_5M, "markets_n": MARKETS_5M},
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bb_rsi_5m_bot")

_trade_log = logging.getLogger("bb_rsi_5m_bot.trades")
_trade_log.propagate = False
_fh = logging.FileHandler("bb_rsi_5m_trades.log", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
_trade_log.addHandler(_fh)
_trade_log.setLevel(logging.INFO)

def tlog(msg: str):
    _trade_log.info(msg)

# ===========================================================================
# Буферы индикаторов — Bollinger (close) и RSI (close)
# ===========================================================================
_closes_bb: dict = {}
_closes_rsi: dict = {}
_last_candle_ts: dict = {}

def _key(instId, bar):
    return (instId, bar)

def bb_update(instId, bar, close):
    k = _key(instId, bar)
    if k not in _closes_bb:
        _closes_bb[k] = collections.deque(maxlen=BB_PERIOD)
    _closes_bb[k].append(close)

def bb_pctB(instId, bar, close_including_current):
    """%B = (close - SMA) / (2*STD). Включает ТЕКУЩУЮ свечу (как в бэктесте:
    pandas .rolling(BB_PERIOD).mean() без .shift()), поэтому close нужно
    добавить в буфер ДО вызова этой функции."""
    k = _key(instId, bar)
    buf = _closes_bb.get(k)
    if not buf or len(buf) < BB_PERIOD:
        return None
    arr = list(buf)
    mean = sum(arr) / len(arr)
    var = sum((x - mean) ** 2 for x in arr) / (len(arr) - 1)  # ddof=1, как pandas .std() по умолчанию
    std = var ** 0.5
    if std == 0:
        return None
    return (close_including_current - mean) / (2 * std)

def rsi_update(instId, bar, close):
    k = _key(instId, bar)
    if k not in _closes_rsi:
        _closes_rsi[k] = collections.deque(maxlen=RSI_PERIOD + 1)
    _closes_rsi[k].append(close)

def rsi_value(instId, bar):
    k = _key(instId, bar)
    buf = _closes_rsi.get(k)
    if not buf or len(buf) < RSI_PERIOD + 1:
        return None
    closes = list(buf)
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains) / len(gains)
    avg_loss = sum(losses) / len(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)

# ===========================================================================
# OKX — свечи
# ===========================================================================
async def fetch_candles(session, instId, bar, limit=40):
    url = f"{OKX_REST}/api/v5/market/candles"
    params = {"instId": instId, "bar": bar, "limit": str(limit)}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                log.warning("OKX %s/%s HTTP %d", instId, bar, r.status)
                return []
            data = await r.json()
            result = []
            for c in (data.get("data") or []):
                if len(c) < 5:
                    continue
                confirm = c[8] if len(c) > 8 else "1"
                if confirm != "1":
                    continue
                try:
                    result.append((int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4])))
                except (ValueError, IndexError):
                    continue
            return result  # от новой к старой
    except Exception as e:
        log.warning("OKX %s/%s error: %s", instId, bar, e)
        return []

# ===========================================================================
# ПРОВЕРКА ТРИГГЕРА — Bollinger ИЛИ RSI, независимо друг от друга
# ===========================================================================
async def check_instrument(session, instId, bar):
    """
    Возвращает (triggered, bet_side, close, open_, trigger_name, indicator_value, ts_sig).
    trigger_name: 'BB' или 'RSI' — какой из двух независимых сигналов сработал
    (если сработали оба на одной свече — приоритет отдаётся Bollinger в логе,
    но это не влияет на сам факт триггера).
    """
    k = _key(instId, bar)
    is_first_call = k not in _last_candle_ts

    candles = await fetch_candles(session, instId, bar, limit=BB_PERIOD + 5)
    if not candles:
        return False, None, None, None, None, None, None

    last_seen = _last_candle_ts.get(k, 0)
    new_candles = [(ts, o, h, l, c) for ts, o, h, l, c in reversed(candles) if ts > last_seen]
    if not new_candles:
        return False, None, None, None, None, None, None

    triggered_final = False
    bet_side_final = None
    trigger_name_final = None
    indicator_value_final = None
    ts_sig = o_sig = c_sig = None

    for ts, o, h, l, c in new_candles:
        bb_update(instId, bar, c)
        rsi_update(instId, bar, c)
        pctB = bb_pctB(instId, bar, c)
        rsi_now = rsi_value(instId, bar)

        bb_ok = pctB is not None and abs(pctB) >= BB_THRESHOLD
        rsi_ok = rsi_now is not None and (rsi_now <= RSI_LO or rsi_now >= RSI_HI)

        if bb_ok or rsi_ok:
            triggered_final = True
            bet_side_final = "NO" if c > o else "YES"   # реверс направления сигнальной свечи
            trigger_name_final = "BB" if bb_ok else "RSI"
            if bb_ok and rsi_ok:
                trigger_name_final = "BB+RSI"
            indicator_value_final = pctB if bb_ok else rsi_now
            ts_sig, o_sig, c_sig = ts // 1000 + SEG_5M, o, c  # OKX: ts=открытие(мс) -> закрытие в секундах

    _last_candle_ts[k] = new_candles[-1][0]

    if is_first_call and SKIP_FIRST_TRIGGER:
        if triggered_final:
            log.warning("%s/%s | триггер обнаружен в истории при прогреве — ИГНОРИРУЕМ", instId, bar)
        else:
            log.info("%s/%s | прогрев буферов BB/RSI завершён", instId, bar)
        return False, None, None, None, None, None, None

    if triggered_final:
        log.info(
            "%s/%s | close=%.2f open=%.2f | %s=%.2f | 🔥 bet=%s",
            instId, bar, c_sig, o_sig, trigger_name_final, indicator_value_final, bet_side_final,
        )
    else:
        last = new_candles[-1]
        log.info("%s/%s | close=%.2f | —", instId, bar, last[4])

    return triggered_final, bet_side_final, c_sig, o_sig, trigger_name_final, indicator_value_final, ts_sig

# ===========================================================================
# POLYMARKET — поиск рынков
# ===========================================================================
async def fetch_event_markets(session, slug):
    try:
        async with session.get(
            f"{GAMMA_REST}/events",
            params={"slug": slug},
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

def extract_tokens(markets):
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

async def discover_markets(session, asset, seg, n, ts_sig=None):
    """i=0 -> окно [ts_sig, ts_sig+seg) — ГОРИЗОНТ +1 свеча от сигнальной.
    ts_sig = момент закрытия сигнальной свечи = начало нужного (следующего)
    окна, слот считается от него, а не от wall-clock (см. атр-бот, тот же
    принцип и то же обоснование)."""
    tf = "5m"
    if ts_sig is not None:
        base_slot = (ts_sig // seg) * seg
    else:
        now = int(time.time())
        base_slot = (now // seg) * seg
    found = []
    for i in range(n + 10):
        slot = base_slot + i * seg
        slug = f"{asset.lower()}-updown-{tf}-{slot}"
        ms = await fetch_event_markets(session, slug)
        ty, tn, q = extract_tokens(ms)
        if ty and tn:
            found.append({"slug": slug, "token_yes": ty, "token_no": tn, "question": q})
        if len(found) >= n:
            break
    return found[:n]

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
                        creds=creds, signature_type=SIGNATURE_TYPE, funder=FUNDER)
    else:
        tmp = ClobClient(host=CLOB_REST, chain_id=CHAIN_ID, key=PRIVATE_KEY,
                         signature_type=SIGNATURE_TYPE, funder=FUNDER)
        creds = tmp.create_or_derive_api_key()
        cl = ClobClient(host=CLOB_REST, chain_id=CHAIN_ID, key=PRIVATE_KEY,
                        creds=creds, signature_type=SIGNATURE_TYPE, funder=FUNDER)
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

def place_gtc_order(client, token_id, price, size, label):
    tick = get_tick(client, token_id)
    neg_risk = get_neg_risk(client, token_id)
    args = OrderArgs(token_id=token_id, price=price, size=size, side=Side.BUY)
    opts = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
    try:
        resp = client.create_and_post_order(args, opts, OrderType.GTC)
        oid = resp.get("orderID") or resp.get("id") or "?"
        log.info("  ✅ [%s] price=%.2f size=%d → %s %s", label, price, size, oid, resp.get("status",""))
        return resp
    except Exception as e:
        log.error("  ❌ [%s] price=%.2f size=%d → %s", label, price, size, e)
        return None

# ===========================================================================
# РАЗМЕЩЕНИЕ ОРДЕРОВ — основная сторона + хедж на противоположную (только $0.01)
# ===========================================================================
async def place_orders(session, client, asset, seg, markets_n, bet_side, trigger_info, ts_sig=None):
    tf = "5m"

    if ts_sig is not None:
        staleness = time.time() - ts_sig
        if staleness > STALE_THRESHOLD_SEC:
            log.warning(
                "⚠️  %s %s | триггер устарел (%.0fs, порог=%ds) — ПРОПУСКАЕМ",
                asset, tf, staleness, STALE_THRESHOLD_SEC,
            )
            tlog(f"SKIPPED_STALE | {asset} {tf} | staleness={staleness:.0f}s | {trigger_info}")
            return 0

    log.warning("▶️  %s %s | ставка=%s | %s | рынков: %d", asset, tf, bet_side, trigger_info, markets_n)

    markets = await discover_markets(session, asset, seg, markets_n, ts_sig=ts_sig)
    log.info("  Найдено: %d рынков", len(markets))

    count = 0
    log_rows = []
    ts_now = int(time.time())

    for mkt in markets:
        log.info("  📌 %s", mkt["question"])
        tlog(f"  MARKET | {asset} {tf} | {mkt['slug']} | {mkt['question']}")
        token_id = mkt["token_yes"] if bet_side == "YES" else mkt["token_no"]

        for price in LIMIT_PRICES:
            size = SIZE_BY_PRICE.get(price, SIZE_DEFAULT)
            label = f"{asset} {tf} {bet_side} @{price}"
            if EXECUTION_ENABLED and client:
                resp = place_gtc_order(client, token_id, price, size, label)
                oid = (resp or {}).get("orderID") or (resp or {}).get("id") or "?"
                tlog(f"  ORDER  | {label} | size={size} | token={token_id[:10]}…{token_id[-6:]} | id={oid}")
            else:
                log.info("  📝 [%s] token=%s…%s size=%d [PAPER]", label, token_id[:8], token_id[-6:], size)
                tlog(f"  ORDER  | {label} | size={size} | token={token_id[:10]}…{token_id[-6:]} | PAPER")
            count += 1
            log_rows.append({
                "ts": ts_now, "asset": asset, "tf": tf,
                "slug": mkt["slug"], "question": mkt["question"],
                "side": bet_side, "token_id": token_id,
                "price": price, "size": size, "trigger": trigger_info,
            })
            await asyncio.sleep(0.15)

        if HEDGE_ENABLED:
            opposite_side = "NO" if bet_side == "YES" else "YES"
            opposite_token = mkt["token_no"] if bet_side == "YES" else mkt["token_yes"]
            hedge_label = f"{asset} {tf} {opposite_side}(hedge) @{HEDGE_PRICE}"
            if EXECUTION_ENABLED and client:
                resp = place_gtc_order(client, opposite_token, HEDGE_PRICE, HEDGE_SIZE, hedge_label)
                oid = (resp or {}).get("orderID") or (resp or {}).get("id") or "?"
                tlog(f"  ORDER  | {hedge_label} | size={HEDGE_SIZE} | token={opposite_token[:10]}…{opposite_token[-6:]} | id={oid}")
            else:
                log.info("  📝 [%s] token=%s…%s size=%d [PAPER]",
                         hedge_label, opposite_token[:8], opposite_token[-6:], HEDGE_SIZE)
                tlog(f"  ORDER  | {hedge_label} | size={HEDGE_SIZE} | token={opposite_token[:10]}…{opposite_token[-6:]} | PAPER")
            count += 1
            log_rows.append({
                "ts": ts_now, "asset": asset, "tf": tf,
                "slug": mkt["slug"], "question": mkt["question"],
                "side": opposite_side, "token_id": opposite_token,
                "price": HEDGE_PRICE, "size": HEDGE_SIZE, "trigger": trigger_info + " [HEDGE]",
            })
            await asyncio.sleep(0.15)

    with open("bb_rsi_5m_orders.jsonl", "a") as f:
        for row in log_rows:
            f.write(json.dumps(row) + "\n")

    log.info("  Итого ордеров выставлено: %d", count)
    return count

# ===========================================================================
# ГЛАВНЫЙ ЦИКЛ
# ===========================================================================
async def main():
    log.info("=" * 65)
    log.info("  BOLLINGER + RSI REVERSAL BOT | BTC + ETH | ТОЛЬКО 5m Polymarket")
    log.info("  EXECUTION=%s", EXECUTION_ENABLED)
    log.info("  Bollinger%d-триггер: |%%B| >= %.2f (НЕЗАВИСИМО от RSI)", BB_PERIOD, BB_THRESHOLD)
    log.info("  RSI%d-триггер: <=%.0f или >=%.0f (НЕЗАВИСИМО от Bollinger)", RSI_PERIOD, RSI_LO, RSI_HI)
    log.info("  Направление: ОДНОСТОРОННЕЕ (реверс от направления сигнальной свечи)")
    log.info("  Рынков после триггера: %d", MARKETS_5M)
    log.info("  Цены ордеров: %s", LIMIT_PRICES)
    log.info("  Размеры: %s", SIZE_BY_PRICE)
    if HEDGE_ENABLED:
        log.info("  Хедж на противоположную сторону: ВКЛЮЧЁН, цена=%.2f, размер=%d", HEDGE_PRICE, HEDGE_SIZE)
    else:
        log.info("  Хедж на противоположную сторону: ВЫКЛЮЧЕН")
    log.info("  SKIP_FIRST_TRIGGER=%s", SKIP_FIRST_TRIGGER)
    log.info("  STALE_THRESHOLD_SEC=%ds", STALE_THRESHOLD_SEC)
    log.info("  Cooldown=%ds | Интервал=%ds", COOLDOWN_SEC, CHECK_INTERVAL_SEC)
    log.info("  ⚠️  ВНИМАНИЕ: финансовая экономика для 5m подтверждена слабее, чем для 15m")
    log.info("     (реальные ордербуки 5m, что у нас были, оказались обрезаны по времени)")
    log.info("=" * 65)

    client = None
    if EXECUTION_ENABLED:
        if not HAVE_CLOB:
            log.error("py_clob_client_v2 не установлен")
            return
        try:
            client = build_clob_client()
            log.warning("🔑 CLOB авторизован. РЕАЛЬНОЕ ИСПОЛНЕНИЕ!")
        except Exception as e:
            log.error("Ошибка авторизации: %s", e)
            return
    else:
        log.info("📄 PAPER-РЕЖИМ")

    last_trigger: dict = {}

    async with aiohttp.ClientSession() as session:
        while True:
            cycle_start = time.time()

            for instr in INSTRUMENTS:
                instId    = instr["instId"]
                bar       = instr["bar"]
                asset     = instr["asset"]
                seg       = instr["seg"]
                markets_n = instr["markets_n"]
                k = _key(instId, bar)

                since = time.time() - last_trigger.get(k, 0)
                if since < COOLDOWN_SEC:
                    log.debug("%s/%s: cooldown %.0fs", instId, bar, COOLDOWN_SEC - since)
                    continue

                try:
                    triggered, bet_side, close, open_, trigger_name, indicator_value, ts_sig = await check_instrument(session, instId, bar)
                except Exception as e:
                    log.error("check %s/%s: %s", instId, bar, e)
                    continue

                if triggered:
                    last_trigger[k] = time.time()
                    info = f"trigger={trigger_name} value={indicator_value:.2f} close={close:.2f} open={open_:.2f}"
                    tlog(f"TRIGGER | {asset} {bar} | bet={bet_side} | {info}")
                    try:
                        await place_orders(session, client, asset, seg, markets_n, bet_side, info, ts_sig=ts_sig)
                    except Exception as e:
                        log.error("place_orders %s/%s: %s", instId, bar, e)

                await asyncio.sleep(0.5)

            elapsed = time.time() - cycle_start
            wait = max(0, CHECK_INTERVAL_SEC - elapsed)
            log.info("⏳ цикл=%.1fs | ждём %.0fs", elapsed, wait)
            await asyncio.sleep(wait)

if __name__ == "__main__":
    asyncio.run(main())
