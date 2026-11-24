#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deep_reversal_bot.py
---------------------
Четвёртый отдельный боевой алгоритм — под паттерн, найденный НЕ через
event-study лифта, а через прямой разбор реальных исходов резолва:

  ИССЛЕДОВАНИЕ (14 дней BTC, 15m, реальные L2-логи Polymarket с полем
  winning_outcome — авторитетный исход резолва, не эвристика):
    - Среди рынков, где ask когда-либо падал до <=0.02: из 686 случаев
      только 13 (1.9%) в итоге выиграли. Это НЕ входной сигнал — база
      входа сама по себе бесполезна (98% таких позиций проигрывают,
      если просто держать до резолва).
    - НО: сравнили поведение цены ПОСЛЕ падения к минимуму у победителей
      (n=13) и у проигравших (контрольная выборка n=35). У ВСЕХ 13
      победителей ask отскакивал (>=+0.005 от минимума/цены входа) в
      пределах 0.03–11.5 секунды. У проигравших отскок тоже иногда
      случается (шум ликвидности перед закрытием), но почти всегда
      намного позже (в основном 80-185с), и только у 3 из 35 (9%)
      выпадал в то же окно, что у победителей.
    - Порог 15 секунд: ловит 13/13 (100%) победителей, ошибочно
      "продлевает жизнь" только 3/35 (9%) проигравших.
    - Проверено на 12 днях, ПОДТВЕРЖДЕНО на оставшихся 2 днях, не
      участвовавших в подборе порога (13-й победитель, задержка 0.3с).

  ЛОГИКА БОТА (принципиально другая от трёх предыдущих):
    Не реагируем на сигнал — держим РЕЗЕРВНЫЕ ЛИМИТНЫЕ ОРДЕРА НА ПОКУПКУ
    по фиксированным ценам 0.01 и 0.02 сразу с открытия каждого рынка
    (обе стороны, оба уровня — по объёму в долларах на каждый уровень).
    Если/когда уровень исполняется (рынок сам падает туда) — ждём 15
    секунд. Если за это время цена НЕ дёрнулась вверх минимум на 0.005 —
    считаем позицию мёртвой и продаём по рынку через FAK немедленно,
    не дожидаясь резолва. Если дёрнулась — держим до резолва (по
    исследованию, это почти гарантированный выигрыш).

  ВАЖНАЯ ОГОВОРКА ПО ВАЛИДАЦИИ: паттерн подтверждён ТОЛЬКО на BTC, ТОЛЬКО
  на 15-минутных рынках, на относительно небольшой выборке (13
  победителей). ETH/XRP/SOL и 5-минутные рынки — НЕ проверены вообще.
  По умолчанию бот работает только на BTC/15m; расширение на другие
  активы/таймфреймы — чистый эксперимент.

ЗАПУСК:
  pip install aiohttp python-dotenv
  python3 deep_reversal_bot.py                        # paper
  EXECUTION_ENABLED=true python3 deep_reversal_bot.py # live
"""

import os, sys, json, re, time, asyncio, logging, math
from decimal import Decimal

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

BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"

# ===========================================================================
# КОНФИГ
# ===========================================================================
GAMMA_REST = "https://gamma-api.polymarket.com"
CLOB_REST  = "https://clob.polymarket.com"

EXECUTION_ENABLED = os.environ.get("EXECUTION_ENABLED", "false").lower() == "true"
PRIVATE_KEY       = os.environ.get("POLY_WALLET_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY", "")
FUNDER            = os.environ.get("POLY_FUNDER_ADDRESS") or os.environ.get("FUNDER", "")
SIGNATURE_TYPE    = int(os.environ.get("POLY_SIGNATURE_TYPE", "3"))
CHAIN_ID          = int(os.environ.get("POLY_CHAIN_ID", "137"))
API_KEY           = os.environ.get("CLOB_API_KEY", "")
API_SECRET        = os.environ.get("CLOB_API_SECRET", "")
API_PASSPHRASE    = os.environ.get("CLOB_API_PASSPHRASE", "")

# ВАЖНО: валидировано только BTC/15m. XRP/ETH/SOL и 5m — эксперимент.
ASSETS = [a.strip().lower() for a in os.environ.get("ASSETS", "btc").split(",") if a.strip()]
TFS = [tf.strip() for tf in os.environ.get("TFS", "15m").split(",") if tf.strip()]
SEGMENTS_ALL = {"5m": 300, "15m": 900}
SEGMENTS = {tf: SEGMENTS_ALL[tf] for tf in TFS if tf in SEGMENTS_ALL}

# ── Сердце стратегии: уровни резервных лимиток и подтверждение отскока ──
DEEP_BUY_PRICES = [float(p) for p in os.environ.get("DEEP_BUY_PRICES", "0.01,0.02").split(",")]
DEEP_BUY_USD_PER_LEVEL = float(os.environ.get("DEEP_BUY_USD_PER_LEVEL", "2.00"))
DEEP_CONFIRM_WAIT_SEC  = float(os.environ.get("DEEP_CONFIRM_WAIT_SEC",  "15.0"))
DEEP_CONFIRM_RISE      = float(os.environ.get("DEEP_CONFIRM_RISE",      "0.005"))
MIN_ORDER_VALUE        = float(os.environ.get("MIN_ORDER_VALUE",        "1.05"))

# ── Опрос / тайминг ──
ORDER_STATUS_POLL_SEC = float(os.environ.get("ORDER_STATUS_POLL_SEC", "1.0"))
STOP_BEFORE_SEC        = float(os.environ.get("STOP_BEFORE_SEC",        "0.5"))
FAK_ATTEMPTS            = int(os.environ.get("FAK_ATTEMPTS",            "5"))
FAK_INTERVAL_SEC        = float(os.environ.get("FAK_INTERVAL_SEC",      "0.15"))

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
log = logging.getLogger("deep_reversal_bot")
log.setLevel(logging.INFO)
log.propagate = False
log.addHandler(_console_handler)
_fh = logging.FileHandler("deep_reversal_bot_trades.log", encoding="utf-8")
_fh.setFormatter(MillisecondFormatter("%(asctime)s | %(levelname)s | %(message)s"))
log.addHandler(_fh)

SIGNAL_LOG_PATH = "deep_reversal_bot_signal_log.jsonl"

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

async def get_current_market(session: aiohttp.ClientSession, asset: str, tf_label: str, seg: int) -> dict | None:
    now  = int(time.time())
    slot = (now // seg) * seg
    slug = f"{asset}-updown-{tf_label}-{slot}"
    ms   = await fetch_event_markets(session, slug)
    ty, tn, q = extract_up_token(ms)
    if ty is None:
        return None
    return {"slot": slot, "slug": slug, "token_up": ty, "token_down": tn,
            "question": q, "tf": tf_label, "seg": seg}

async def get_best_ask(session: aiohttp.ClientSession, token_id: str) -> float | None:
    try:
        async with session.get(
            f"{CLOB_REST}/price", params={"token_id": token_id, "side": "sell"},
            timeout=aiohttp.ClientTimeout(total=4),
        ) as r:
            if r.status == 200:
                data = await r.json()
                p = data.get("price")
                return float(p) if p is not None else None
    except Exception as e:
        log.debug("get_best_ask(%s): %s", token_id, e)
    return None

async def get_best_bid(session: aiohttp.ClientSession, token_id: str) -> float | None:
    try:
        async with session.get(
            f"{CLOB_REST}/price", params={"token_id": token_id, "side": "buy"},
            timeout=aiohttp.ClientTimeout(total=4),
        ) as r:
            if r.status == 200:
                data = await r.json()
                p = data.get("price")
                return float(p) if p is not None else None
    except Exception as e:
        log.debug("get_best_bid(%s): %s", token_id, e)
    return None


# ===========================================================================
# CLOB — исполнение (тот же проверенный код, что и в трёх предыдущих ботах)
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
    """Возвращает (filled, oid, ambiguous). ambiguous=True — исход неизвестен
    (таймаут/500), нельзя трактовать как гарантированный отказ."""
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
    """(matched_size, is_terminal) или None при ошибке."""
    try:
        info = client.get_order(order_id)
        matched = float(info.get("size_matched", 0) or 0)
        status = str(info.get("status", "")).lower()
        is_terminal = status in ("matched", "cancelled", "canceled", "filled")
        return matched, is_terminal
    except Exception as e:
        log.debug("get_order_status(%s): %s", order_id, e)
        return None

def cancel_order(client, order_id: str) -> bool:
    try:
        client.cancel(order_id)
        return True
    except Exception as e:
        log.debug("cancel_order(%s): %s", order_id, e)
        return False


async def fak_sell(session, client, token_id: str, label: str, size: float, deadline: float):
    """Аварийная продажа по рынку — паттерн не подтвердился за 15с,
    закрываем позицию, не дожидаясь резолва."""
    remaining_size = size
    total_sold = 0.0
    for attempt in range(1, FAK_ATTEMPTS + 1):
        if deadline - time.time() <= 0.05:
            log.warning("[%s] рынок закрылся во время аварийной продажи (продано %.4f/%.4f)",
                        label, total_sold, size)
            return total_sold
        current_bid = await get_best_bid(session, token_id)
        if current_bid is None:
            await asyncio.sleep(FAK_INTERVAL_SEC)
            continue
        if EXECUTION_ENABLED:
            async with _clob_lock:
                filled, oid, ambiguous = await asyncio.to_thread(
                    place_order, client, token_id, current_bid, remaining_size,
                    f"{label} АВАРИЙНАЯ ПРОДАЖА попытка {attempt}/{FAK_ATTEMPTS}", "SELL")
            confirmed = filled
            if oid:
                await asyncio.sleep(0.5)
                status = await asyncio.to_thread(get_order_status, client, oid)
                if status:
                    real_filled, _ = status
                    confirmed = real_filled
            if confirmed > 0:
                total_sold += confirmed
                remaining_size = max(0.0, remaining_size - confirmed)
                log.info("[%s] ПРОДАНО (аварийно) %.4f по %.3f, всего %.4f/%.4f",
                          label, confirmed, current_bid, total_sold, size)
                if remaining_size <= size * 0.01:
                    return total_sold
        else:
            log.info("PAPER [%s] аварийная продажа: price=%.3f size=%.4f", label, current_bid, remaining_size)
            return size
        await asyncio.sleep(FAK_INTERVAL_SEC)
    log.warning("[%s] не удалось полностью закрыть аварийно за %d попыток (продано %.4f/%.4f)",
                label, FAK_ATTEMPTS, total_sold, size)
    return total_sold


# ===========================================================================
# МОНИТОР РЫНКА
# ===========================================================================
async def watch_fill_and_confirm(session, client, token_id: str, label: str,
                                 level_price: float, order_id: str, size: float, deadline: float):
    """Ждёт исполнения резервной лимитки, затем 15с на подтверждение
    паттерна (отскок >= DEEP_CONFIRM_RISE), иначе — аварийная продажа."""
    filled_size = 0.0
    fill_ts = None
    while True:
        remaining = deadline - time.time()
        if remaining <= STOP_BEFORE_SEC:
            if filled_size <= 0 and EXECUTION_ENABLED:
                await asyncio.to_thread(cancel_order, client, order_id)
            return
        if EXECUTION_ENABLED:
            status = await asyncio.to_thread(get_order_status, client, order_id)
            if status:
                matched, _ = status
                if matched > 0 and fill_ts is None:
                    filled_size = matched
                    fill_ts = time.time()
                    log.warning("[%s] РЕЗЕРВНАЯ ЛИМИТКА ИСПОЛНЕНА по %.3f: %.4f шт — жду %.0fс на подтверждение паттерна",
                                label, level_price, filled_size, DEEP_CONFIRM_WAIT_SEC)
                    log_signal({"event": "filled", "label": label, "level": level_price,
                                "filled": filled_size, "paper": not EXECUTION_ENABLED})
        else:
            # PAPER: считаем исполненным сразу же условно (нет реального ордера)
            pass

        if fill_ts is not None and time.time() - fill_ts >= DEEP_CONFIRM_WAIT_SEC:
            ask_now = await get_best_ask(session, token_id)
            confirmed = ask_now is not None and ask_now >= level_price + DEEP_CONFIRM_RISE
            if confirmed:
                log.warning("[%s] ПАТТЕРН ПОДТВЕРЖДЁН: ask=%s >= %.3f+%.3f — держим до резолва",
                            label, f"{ask_now:.3f}" if ask_now is not None else "н/д",
                            level_price, DEEP_CONFIRM_RISE)
                log_signal({"event": "confirmed_hold", "label": label, "level": level_price,
                            "ask_after_wait": ask_now})
            else:
                log.warning("[%s] паттерн НЕ подтверждён (ask=%s) за %.0fс — аварийная продажа",
                            label, f"{ask_now:.3f}" if ask_now is not None else "н/д", DEEP_CONFIRM_WAIT_SEC)
                log_signal({"event": "not_confirmed_sell", "label": label, "level": level_price,
                            "ask_after_wait": ask_now})
                if EXECUTION_ENABLED:
                    await fak_sell(session, client, token_id, label, filled_size, deadline)
                else:
                    log.info("PAPER [%s] аварийная продажа (симуляция)", label)
            return
        await asyncio.sleep(ORDER_STATUS_POLL_SEC)


async def monitor_market(session, client, market: dict, asset: str):
    tf, seg, slug = market["tf"], market["seg"], market["slug"]
    token_up, token_down, q = market["token_up"], market["token_down"], market["question"]
    deadline = market["slot"] + seg

    log.info("[%s/%s] МОНИТОРИНГ: %s | резервные лимитки: %s по $%.2f",
             asset.upper(), tf, q, DEEP_BUY_PRICES, DEEP_BUY_USD_PER_LEVEL)

    watchers = []
    for direction_label, token_id in [("UP", token_up), ("DOWN", token_down)]:
        for level_price in DEEP_BUY_PRICES:
            desired_size = DEEP_BUY_USD_PER_LEVEL / level_price
            label = f"{asset.upper()}/{tf} DEEP_{direction_label}_{level_price:.2f}"
            if EXECUTION_ENABLED:
                async with _clob_lock:
                    filled, order_id, ambiguous = await asyncio.to_thread(
                        place_order, client, token_id, level_price, desired_size, label, "BUY", OrderType.GTC)
                if order_id is None:
                    log.error("[%s] не удалось выставить резервную лимитку", label)
                    continue
            else:
                order_id = f"paper-{direction_label}-{level_price}"
                log.info("PAPER [%s] резервная лимитка выставлена: price=%.3f size=%.4f",
                         label, level_price, desired_size)

            watchers.append(asyncio.create_task(
                watch_fill_and_confirm(session, client, token_id, label, level_price,
                                       order_id, desired_size, deadline)
            ))

    if watchers:
        await asyncio.gather(*watchers, return_exceptions=True)

    log.info("[%s/%s] окно закрыто: %s", asset.upper(), tf, slug)


# ===========================================================================
# ПЛАНИРОВЩИК
# ===========================================================================
async def scheduler():
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

    async with aiohttp.ClientSession() as session:
        while True:
            for asset in ASSETS:
                for tf_label, seg in SEGMENTS.items():
                    slot = int(time.time() // seg) * seg
                    key = (asset, tf_label, slot)
                    if key in active_tasks and not active_tasks[key].done():
                        continue
                    market = await get_current_market(session, asset, tf_label, seg)
                    if market is None:
                        continue
                    task = asyncio.create_task(monitor_market(session, client, market, asset))
                    active_tasks[key] = task
                    log.info("[%s/%s] задача мониторинга запущена: %s",
                             asset.upper(), tf_label, market["slug"])

            for k in list(active_tasks):
                if active_tasks[k].done():
                    exc = active_tasks[k].exception()
                    if exc:
                        log.error("Задача %s упала: %s", k, exc)
                    del active_tasks[k]

            await asyncio.sleep(2)


async def main():
    log.info("=== deep_reversal_bot.py СТАРТ ===")
    log.info("Активы: %s | ТФ: %s | paper=%s", ASSETS, list(SEGMENTS.keys()), not EXECUTION_ENABLED)
    log.info("Уровни: %s по $%.2f каждый | подтверждение: рост >=%.3f в течение %.0fс",
             DEEP_BUY_PRICES, DEEP_BUY_USD_PER_LEVEL, DEEP_CONFIRM_RISE, DEEP_CONFIRM_WAIT_SEC)
    if any(a != "btc" for a in ASSETS) or any(tf != "15m" for tf in SEGMENTS):
        log.warning("ВНИМАНИЕ: паттерн валидирован ТОЛЬКО на BTC/15m (14 дней, 13 побед из 686 "
                     "случаев <=0.02). Остальные активы/таймфреймы в конфиге — чистый эксперимент.")
    await scheduler()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено по Ctrl+C")
