#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atr_reversal_bot.py
--------------------
Бот ловли реверс-свечей по BTC и ETH на Polymarket, ТОЛЬКО 15-минутные рынки.

Триггер: range(сигнальной свечи) > ATR_MULT * ATR14
  (ATR14 считается по предыдущим 14 закрытым свечам, без утечки текущей).

Направление: ОДНОСТОРОННЕЕ (не как в исходном spike_bot, где ставили на обе
стороны сразу). Ставим на РЕВЕРС направления сигнальной свечи:
  - если сигнальная свеча ЗАКРЫЛАСЬ ВЫШЕ открытия (рост) -> ставим NO
    (ждём, что следующая свеча развернётся вниз)
  - если сигнальная свеча ЗАКРЫЛАСЬ НИЖЕ открытия (падение) -> ставим YES
    (ждём разворота вверх)

Это подтверждено на реальных ордербуках (887 15m-рынков): одностороння
ставка даёт устойчивый ROI ~20-34%, тогда как двусторонняя сетка (обе
стороны сразу) даёт результат около нуля или в минусе — гарантированно
проигрышная сторона съедает весь edge.

Ордера: сетка цен 0.01/0.02/0.05/0.10/0.20/0.30/0.40, размер каждого уровня
задаётся через SIZE_BY_PRICE или SIZE_DEFAULT (env).

SKIP_FIRST_TRIGGER=true (default): первый цикл на каждый инструмент только
заполняет ATR-буфер молча, без ордеров (буфер иначе мгновенно заполняется
историей с OKX и может дать "выстрел из прошлого" на самом первом цикле).
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
OKX_REST   = "https://www.okx.com"

EXECUTION_ENABLED = os.environ.get("EXECUTION_ENABLED", "false").lower() == "true"
PRIVATE_KEY       = os.environ.get("POLY_WALLET_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY", "")
FUNDER            = os.environ.get("POLY_FUNDER_ADDRESS") or os.environ.get("FUNDER", "")
SIGNATURE_TYPE    = int(os.environ.get("POLY_SIGNATURE_TYPE", "3"))
CHAIN_ID          = int(os.environ.get("POLY_CHAIN_ID", "137"))
API_KEY           = os.environ.get("CLOB_API_KEY", "")
API_SECRET        = os.environ.get("CLOB_API_SECRET", "")
API_PASSPHRASE    = os.environ.get("CLOB_API_PASSPHRASE", "")

# --- Ордера ---
LIMIT_PRICES = [float(x) for x in os.environ.get("LIMIT_PRICES", "0.01,0.02,0.05,0.10,0.20,0.30,0.40").split(",")]
SIZE_DEFAULT = int(os.environ.get("SIZE_DEFAULT", "100"))
# SIZE_BY_PRICE: "0.01:100,0.02:100,0.05:100,0.10:100,0.20:100,0.30:100,0.40:100"
_size_env = os.environ.get("SIZE_BY_PRICE", "")
if _size_env:
    SIZE_BY_PRICE = {}
    for item in _size_env.split(","):
        p, s = item.strip().split(":")
        SIZE_BY_PRICE[float(p)] = int(s)
else:
    SIZE_BY_PRICE = {p: SIZE_DEFAULT for p in LIMIT_PRICES}

# --- Хедж на ПРОТИВОПОЛОЖНУЮ сторону (только по $0.01) ---
# Помимо основной сетки на bet_side, ставим ОДИН ордер по цене 0.01 на
# противоположный токен. Идея: если наша реверс-ставка проигрывает (то есть
# побеждает продолжение направления сигнальной свечи), эта лимитка на
# противоположной стороне иногда даёт исполниться и ловит "спорные" случаи,
# где противоположная сторона на время выглядела почти проигравшей (цена
# падала до 0.01), но всё же победила. Проверено на реальных ордербуках
# (887+1333 рынков BTC 15m): такой хедж даёт прибыль САМ ПО СЕБЕ в обоих
# протестированных периодах, почти удваивая итоговый суммарный результат.
HEDGE_ENABLED = os.environ.get("HEDGE_ENABLED", "true").lower() == "true"
HEDGE_PRICE   = float(os.environ.get("HEDGE_PRICE", "0.01"))
HEDGE_SIZE    = int(os.environ.get("HEDGE_SIZE", "120"))

# --- Рынки: ТОЛЬКО 15 минут ---
MARKETS_15M = int(os.environ.get("MARKETS_15M", "1"))   # горизонт +1 свеча
SEG_15M = 900

# --- ATR-триггер ---
ATR_PERIOD  = int(os.environ.get("ATR_PERIOD", "14"))
ATR_MULT    = float(os.environ.get("ATR_MULT", "1.5"))   # лучший найденный порог (ROI~34% на реальных данных)

# --- Защита от "выстрела из истории" на старте ---
SKIP_FIRST_TRIGGER = os.environ.get("SKIP_FIRST_TRIGGER", "true").lower() == "true"

# --- Защита от устаревшего триггера (пауза процесса, сетевой сбой, сон устройства) ---
STALE_THRESHOLD_SEC = int(os.environ.get("STALE_THRESHOLD_SEC", "60"))

# --- Цикл ---
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "30"))
COOLDOWN_SEC       = int(os.environ.get("COOLDOWN_SEC", "300"))

# Инструменты для мониторинга — только BTC и ETH, только 15m
INSTRUMENTS = [
    {"instId": "BTC-USDT", "asset": "BTC", "bar": "15m", "seg": SEG_15M, "markets_n": MARKETS_15M},
    {"instId": "ETH-USDT", "asset": "ETH", "bar": "15m", "seg": SEG_15M, "markets_n": MARKETS_15M},
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("atr_reversal_bot")

_trade_log = logging.getLogger("atr_reversal_bot.trades")
_trade_log.propagate = False
_fh = logging.FileHandler("atr_reversal_trades.log", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
_trade_log.addHandler(_fh)
_trade_log.setLevel(logging.INFO)

def tlog(msg: str):
    _trade_log.info(msg)

# ===========================================================================
# ATR14 — скользящий буфер диапазонов (range = high-low)
# ===========================================================================
_ranges: dict = {}
_last_candle_ts: dict = {}

def _key(instId, bar):
    return (instId, bar)

def atr_update(instId, bar, rng):
    k = _key(instId, bar)
    if k not in _ranges:
        _ranges[k] = collections.deque(maxlen=ATR_PERIOD)
    _ranges[k].append(rng)

def atr_value_prior(instId, bar):
    """ATR по ПРЕДЫДУЩИМ ATR_PERIOD свечам, БЕЗ учёта текущей (без утечки)."""
    k = _key(instId, bar)
    buf = _ranges.get(k)
    if not buf or len(buf) < ATR_PERIOD:
        return None
    return sum(buf) / len(buf)

# ===========================================================================
# OKX — свечи
# ===========================================================================
async def fetch_candles(session, instId, bar, limit=30):
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
# ПРОВЕРКА ТРИГГЕРА
# ===========================================================================
async def check_instrument(session, instId, bar):
    """
    Возвращает (triggered, bet_side, close, open_, atr, rng).
    bet_side: 'YES' если ждём разворот вверх (сигнальная упала),
              'NO'  если ждём разворот вниз   (сигнальная выросла).

    ВАЖНО: при первом вызове (буфер ATR ещё не существует) все limit свечей
    истории разом прогоняются через atr_update — буфер заполняется мгновенно
    историческими данными. Если SKIP_FIRST_TRIGGER=true, первый такой проход
    помечается как "прогрев" и НЕ считается триггером.
    """
    k = _key(instId, bar)
    is_first_call = k not in _last_candle_ts

    candles = await fetch_candles(session, instId, bar, limit=ATR_PERIOD + 5)
    if not candles:
        return False, None, None, None, None, None, None

    last_seen = _last_candle_ts.get(k, 0)
    new_candles = [(ts, o, h, l, c) for ts, o, h, l, c in reversed(candles) if ts > last_seen]
    if not new_candles:
        return False, None, None, None, None, None, None

    # ATR на МОМЕНТ сигнальной свечи считается ПО ПРЕДЫДУЩИМ свечам (без утечки) —
    # поэтому сначала читаем текущее значение ATR, ПОТОМ обновляем буфер.
    triggered_final = False
    bet_side_final = None
    ts_sig = o_sig = h_sig = l_sig = c_sig = None
    atr_final = None
    rng_final = None

    for ts, o, h, l, c in new_candles:
        atr_prior = atr_value_prior(instId, bar)
        rng = h - l
        if atr_prior is not None and atr_prior > 0:
            raw_triggered = rng > ATR_MULT * atr_prior
            if raw_triggered:
                triggered_final = True
                bet_side_final = "NO" if c > o else "YES"   # реверс направления сигнальной свечи
                ts_sig, o_sig, h_sig, l_sig, c_sig = ts // 1000 + SEG_15M, o, h, l, c  # OKX: ts=открытие свечи(мс) -> закрытие в секундах
                atr_final = atr_prior
                rng_final = rng
        atr_update(instId, bar, rng)

    _last_candle_ts[k] = new_candles[-1][0]

    if is_first_call and SKIP_FIRST_TRIGGER:
        if triggered_final:
            log.warning("%s/%s | ATR-триггер обнаружен в истории при прогреве — ИГНОРИРУЕМ", instId, bar)
        else:
            log.info("%s/%s | прогрев буфера ATR завершён", instId, bar)
        return False, None, None, None, None, None, None

    if triggered_final:
        log.info(
            "%s/%s | close=%.2f open=%.2f range=%.2f ATR%d=%.2f (x%.2f) | 🔥 bet=%s",
            instId, bar, c_sig, o_sig, rng_final, ATR_PERIOD, atr_final, rng_final/atr_final, bet_side_final,
        )
    else:
        last = new_candles[-1]
        log.info("%s/%s | close=%.2f | —", instId, bar, last[4])

    return triggered_final, bet_side_final, c_sig, o_sig, atr_final, rng_final, ts_sig

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
    """
    i=0 -> окно [ts_sig, ts_sig+seg) — ГОРИЗОНТ +1 свеча от сигнальной.
    ts_sig здесь уже = момент ЗАКРЫТИЯ сигнальной свечи = начало нужного
    (следующего) окна, поэтому дополнительный +seg не нужен.

    ВАЖНО: слот считается от времени закрытия сигнальной свечи (ts_sig), а НЕ
    от wall-clock time.time(). Если считать от now, при любой задержке между
    обнаружением триггера и отправкой ордера (пауза процесса, сетевой сбой,
    сон устройства) дольше одной свечи — можно попасть на уже наполовину
    прожитый рынок вместо следующего. См. инцидент в логах 8-9 июля.
    """
    tf = "15m"
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
# РАЗМЕЩЕНИЕ ОРДЕРОВ — ТОЛЬКО ОДНА СТОРОНА
# ===========================================================================
async def place_orders(session, client, asset, seg, markets_n, bet_side, trigger_info, ts_sig=None):
    """
    Ставим ТОЛЬКО на bet_side ('YES' или 'NO') — без второй, гарантированно
    проигрышной стороны. Это ключевое отличие от исходного spike_bot.py.

    Защитный чек: если между закрытием сигнальной свечи и этим вызовом
    прошло подозрительно много времени (например, процесс завис/спал), не
    ставим ордера на потенциально неверный/устаревший рынок, а просто
    пропускаем это срабатывание с предупреждением в лог.
    """
    tf = "15m"

    if ts_sig is not None:
        staleness = time.time() - ts_sig
        if staleness > STALE_THRESHOLD_SEC:
            log.warning(
                "⚠️  %s %s | триггер устарел (%.0fs с закрытия сигнальной свечи, порог=%ds) — "
                "ПРОПУСКАЕМ размещение ордеров, чтобы не попасть на неверный рынок",
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
                log.info("  📝 [%s] token=%s…%s size=%d [PAPER]",
                         label, token_id[:8], token_id[-6:], size)
                tlog(f"  ORDER  | {label} | size={size} | token={token_id[:10]}…{token_id[-6:]} | PAPER")
            count += 1
            log_rows.append({
                "ts": ts_now, "asset": asset, "tf": tf,
                "slug": mkt["slug"], "question": mkt["question"],
                "side": bet_side, "token_id": token_id,
                "price": price, "size": size, "trigger": trigger_info,
            })
            await asyncio.sleep(0.15)

        # --- Хедж на противоположную сторону, только по HEDGE_PRICE ---
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

    with open("atr_reversal_orders.jsonl", "a") as f:
        for row in log_rows:
            f.write(json.dumps(row) + "\n")

    log.info("  Итого ордеров выставлено: %d", count)
    return count

# ===========================================================================
# ГЛАВНЫЙ ЦИКЛ
# ===========================================================================
async def main():
    log.info("=" * 65)
    log.info("  ATR REVERSAL BOT | BTC + ETH | ТОЛЬКО 15m Polymarket")
    log.info("  EXECUTION=%s", EXECUTION_ENABLED)
    log.info("  ATR%d-триггер: mult=%.2f", ATR_PERIOD, ATR_MULT)
    log.info("  Направление: ОДНОСТОРОННЕЕ (реверс от направления сигнальной свечи)")
    log.info("  Рынков после триггера: %d", MARKETS_15M)
    log.info("  Цены ордеров: %s", LIMIT_PRICES)
    log.info("  Размеры: %s", SIZE_BY_PRICE)
    if HEDGE_ENABLED:
        log.info("  Хедж на противоположную сторону: ВКЛЮЧЁН, цена=%.2f, размер=%d", HEDGE_PRICE, HEDGE_SIZE)
    else:
        log.info("  Хедж на противоположную сторону: ВЫКЛЮЧЕН")
    log.info("  SKIP_FIRST_TRIGGER=%s", SKIP_FIRST_TRIGGER)
    log.info("  STALE_THRESHOLD_SEC=%ds (пропуск ордеров, если триггер старше этого)", STALE_THRESHOLD_SEC)
    log.info("  Cooldown=%ds | Интервал=%ds", COOLDOWN_SEC, CHECK_INTERVAL_SEC)
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
                    triggered, bet_side, close, open_, atr, rng, ts_sig = await check_instrument(session, instId, bar)
                except Exception as e:
                    log.error("check %s/%s: %s", instId, bar, e)
                    continue

                if triggered:
                    last_trigger[k] = time.time()
                    info = f"range={rng:.2f} atr{ATR_PERIOD}={atr:.2f} (x{rng/atr:.2f}) close={close:.2f} open={open_:.2f}"
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
