#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
spread_arb_bot.py
------------------
Мониторит спред между ценой "Up" BTC и ценой "Up" ETH на ОДНОМ И ТОМ ЖЕ
(текущем, ещё живом) рынке Polymarket — и на 15m, и на 5m — в реальном
времени.

МЕХАНИКА (v2 — пороговые сигналы + FOK):

  Есть фиксированный набор порогов спреда SPREAD_LEVELS (по умолчанию
  0.5/0.6/0.7/0.8/0.9/0.95/0.98/0.99). КАЖДЫЙ порог — это НЕЗАВИСИМЫЙ сигнал
  с собственным объёмом (через SIZE_BY_LEVEL). На одном рынке каждый порог
  срабатывает СТРОГО ОДИН РАЗ: как только |spread| впервые пересекает этот
  конкретный уровень, сигнал считается "использованным" до конца жизни
  этого рынка, повторно не отрабатывает. Если спред одним скачком проходит
  сразу несколько порогов (например, с 0.3 сразу до 0.85) — сработают ВСЕ
  пересечённые пороги (0.5, 0.6, 0.7, 0.8) в одном цикле, каждый со своим
  объёмом.

  При срабатывании порога — вместо лимитной сетки ставится РЫНОЧНЫЙ ордер
  через FOK (Fill-Or-Kill) на дешёвую сторону (тот Up-токен, что дешевле).
  FOK либо исполняется полностью сразу, либо отменяется целиком — поэтому
  делаем НЕСКОЛЬКО попыток подряд, каждый раз повышая цену на
  FOK_PRICE_STEP, пока либо не наберём нужный объём, либо не исчерпаем
  FOK_MAX_ATTEMPTS попыток.

Проверено на реальных ордербуках (BTC+ETH 15m, 23 июня — 6 июля, 1269
рынков) на уровне механики "макс.спред за окно": >=0.7 дало win_rate=59.3%,
ROI=333.8%; >=0.5 дало win_rate=56.9%, ROI=161.9%. Конкретно ЭТИ 8 порогов
(0.5-0.99) и FOK-механика исполнения — НЕ протестированы отдельно на
реальных деньгах, это прямое развитие найденной закономерности.

⚠️ 5-минутная версия — расширение по аналогии, без отдельной проверки на
реальных данных 5m.
"""

import os, json, time, asyncio, logging
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

EXECUTION_ENABLED = os.environ.get("EXECUTION_ENABLED", "false").lower() == "true"
PRIVATE_KEY       = os.environ.get("POLY_WALLET_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY", "")
FUNDER            = os.environ.get("POLY_FUNDER_ADDRESS") or os.environ.get("FUNDER", "")
SIGNATURE_TYPE    = int(os.environ.get("POLY_SIGNATURE_TYPE", "3"))
CHAIN_ID          = int(os.environ.get("POLY_CHAIN_ID", "137"))
API_KEY           = os.environ.get("CLOB_API_KEY", "")
API_SECRET        = os.environ.get("CLOB_API_SECRET", "")
API_PASSPHRASE    = os.environ.get("CLOB_API_PASSPHRASE", "")

SEG_15M = 900
SEG_5M = 300
ENABLE_15M = os.environ.get("ENABLE_15M", "true").lower() == "true"
ENABLE_5M  = os.environ.get("ENABLE_5M", "true").lower() == "true"

# --- Пороги спреда — независимые сигналы, каждый один раз на рынок ---
SPREAD_LEVELS = [float(x) for x in os.environ.get(
    "SPREAD_LEVELS", "0.1,0.3,0.5,0.7,0.9,0.95,0.99"
).split(",")]
SPREAD_LEVELS = sorted(SPREAD_LEVELS)

# --- Объём на КАЖДЫЙ порог отдельно — НОГА A (дешёвая Up-сторона) ---
_size_env = os.environ.get(
    "SIZE_BY_LEVEL",
    "0.1:10,0.3:15,0.5:20,0.7:30,0.9:40,0.95:50,0.99:60",
)
SIZE_BY_LEVEL = {}
for item in _size_env.split(","):
    lvl, s = item.strip().split(":")
    SIZE_BY_LEVEL[float(lvl)] = int(s)

# --- Объём на КАЖДЫЙ порог — НОГА B (Down дорогой стороны, НОВОЕ) ---
# Включается/выключается отдельно (LEG_B_ENABLED). По умолчанию объём меньше,
# т.к. нога B на тех же порогах обычно ДЕШЕВЛЕ ноги A (см. проверку на реальных
# данных: порог 0.5 -> медиана цены A~0.22, цены B~0.06; порог 0.9 -> A~0.03, B~0.01)
LEG_B_ENABLED = os.environ.get("LEG_B_ENABLED", "true").lower() == "true"
_size_env_b = os.environ.get(
    "SIZE_BY_LEVEL_B",
    "0.1:10,0.3:15,0.5:20,0.7:30,0.9:40,0.95:50,0.99:60",
)
SIZE_BY_LEVEL_B = {}
for item in _size_env_b.split(","):
    lvl, s = item.strip().split(":")
    SIZE_BY_LEVEL_B[float(lvl)] = int(s)

# --- FOK: несколько попыток, цена растёт с каждой попыткой ---
FOK_MAX_ATTEMPTS = int(os.environ.get("FOK_MAX_ATTEMPTS", "5"))
FOK_PRICE_STEP   = float(os.environ.get("FOK_PRICE_STEP", "0.02"))
# Пауза между попытками добора остатка (FAK) — не бьём мгновенно подряд,
# даём стакану время обновиться перед следующей попыткой
FAK_PAUSE_SEC    = float(os.environ.get("FAK_PAUSE_SEC", "1.5"))
# Polymarket требует минимальную СУММУ ордера (price*size) для marketable BUY —
# по факту наблюдалось "min size: 1" ($1). На дешёвых ценах (например $0.01)
# наш size=5 даёт всего $0.05 — ниже минимума и ордер отклоняется с 400.
# Поэтому size на каждой попытке автоматически поднимается, чтобы price*size
# было НЕ МЕНЬШЕ MIN_ORDER_VALUE (с небольшим запасом по умолчанию).
MIN_ORDER_VALUE = float(os.environ.get("MIN_ORDER_VALUE", "1.05"))

POLL_INTERVAL_SEC   = float(os.environ.get("POLL_INTERVAL_SEC", "5"))
STALE_REMAINING_SEC = int(os.environ.get("STALE_REMAINING_SEC", "20"))
# Раздельные пороги "мало времени осталось" по таймфреймам — на коротком 5m
# окне минуты решают намного больше, чем на 15m, поэтому единый порог в 20с
# слишком мал для обоих сразу. Ниже 60с (5m) / 180с (3 мин, 15m) до конца
# слота монитор просто перестаёт запрашивать цены и выставлять ордера на
# этот слот — сама механика сигналов/сеток дальше не тронута.
STALE_REMAINING_SEC_5M  = int(os.environ.get("STALE_REMAINING_SEC_5M", "60"))
STALE_REMAINING_SEC_15M = int(os.environ.get("STALE_REMAINING_SEC_15M", "180"))

ASSETS = ["btc", "eth"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("spread_arb_bot")

_trade_log = logging.getLogger("spread_arb_bot.trades")
_trade_log.propagate = False
_fh = logging.FileHandler("spread_arb_trades.log", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
_trade_log.addHandler(_fh)
_trade_log.setLevel(logging.INFO)

def tlog(msg: str):
    _trade_log.info(msg)

# ===========================================================================
# POLYMARKET — поиск ТЕКУЩЕГО (живого) рынка + получение цены
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
    """Текущая лучшая цена ask (продажи) для токена — то, по чему МЫ купили бы."""
    try:
        async with session.get(
            f"{CLOB_REST}/price",
            params={"token_id": token_id, "side": "sell"},
            timeout=aiohttp.ClientTimeout(total=6),
        ) as r:
            if r.status == 200:
                data = await r.json()
                price = data.get("price")
                return float(price) if price is not None else None
    except Exception as e:
        log.debug("get_best_ask(%s): %s", token_id, e)
    return None

# ===========================================================================
# CLOB — исполнение
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

def place_fak_order(client, token_id, price, size, label):
    """Одна попытка FAK-ордера (Fill-And-Kill / IOC): исполняется частично
    (сколько есть ликвидности по этой цене), остаток отменяется — в отличие
    от FOK, который требует исполнения ВСЕГО объёма сразу или отменяется
    целиком. Возвращает (filled_size, order_id) — filled_size может быть
    меньше запрошенного size, это нормально."""
    # Polymarket отклоняет marketable BUY дешевле MIN_ORDER_VALUE по сумме —
    # поднимаем size, если price*size вышел меньше минимума (см. MIN_ORDER_VALUE)
    if price > 0:
        min_size_for_value = MIN_ORDER_VALUE / price
        if size < min_size_for_value:
            size = round(min_size_for_value + 0.5, 2)  # с небольшим запасом
    tick = get_tick(client, token_id)
    neg_risk = get_neg_risk(client, token_id)
    args = OrderArgs(token_id=token_id, price=price, size=size, side=Side.BUY)
    opts = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
    try:
        resp = client.create_and_post_order(args, opts, OrderType.FAK)
        resp = resp or {}
        oid = resp.get("orderID") or resp.get("id") or "?"
        # ищем размер исполненной части под разными возможными названиями поля
        filled = None
        for key in ("size_matched", "sizeMatched", "matchedSize", "filledSize", "filled_size"):
            if key in resp:
                try:
                    filled = float(resp[key])
                except (TypeError, ValueError):
                    pass
                break
        status = str(resp.get("status", "")).lower()
        if filled is None:
            # поле с точным объёмом не нашлось — ориентируемся по статусу:
            # если статус говорит "полностью исполнен", считаем что набрали весь size
            filled = size if status in ("matched", "filled") else 0.0
        log.info("  %s [%s] price=%.2f запрошено=%d исполнено=%.2f → %s %s",
                  "✅" if filled >= size else ("🟡" if filled > 0 else "⚠️ "),
                  label, price, size, filled, oid, status)
        return filled, oid
    except Exception as e:
        log.error("  ❌ [%s] price=%.2f size=%d → %s", label, price, size, e)
        return 0.0, None

async def buy_fak_with_retries(session, client, token_id, base_price, size, label):
    """Набирает size через FAK с 'добором' остатка: каждая попытка исполняется
    частично или полностью, остаток допокупается на следующей попытке, с
    паузой FAK_PAUSE_SEC между попытками (не сразу — даём стакану время
    обновиться), до FOK_MAX_ATTEMPTS раз.

    ВАЖНО: перед КАЖДОЙ попыткой (кроме первой) заново запрашиваем текущую
    живую цену ask и берём МАКСИМУМ из (живая цена, предыдущая цена+шаг) —
    иначе можно много раз подряд целиться в диапазон, где в стакане просто
    нет предложений (см. ошибку 'no orders found to match'), даже наращивая
    цену на фиксированный шаг от устаревшего значения."""
    price = base_price
    remaining = size
    total_filled = 0.0
    for attempt in range(1, FOK_MAX_ATTEMPTS + 1):
        if attempt > 1:
            live_ask = await get_best_ask(session, token_id)
            stepped_price = price + FOK_PRICE_STEP
            if live_ask is not None:
                price = max(live_ask, stepped_price)
            else:
                price = stepped_price
        price = min(round(price, 2), 0.99)
        attempt_label = f"{label} попытка {attempt}/{FOK_MAX_ATTEMPTS} (осталось набрать {remaining:.2f})"
        if EXECUTION_ENABLED and client:
            filled, oid = place_fak_order(client, token_id, price, remaining, attempt_label)
            tlog(f"  FAK    | {attempt_label} | price={price:.2f} filled={filled:.2f} | id={oid}")
            total_filled += filled
            remaining -= filled
            if remaining <= 0.01:  # набрали всё (с учётом округлений)
                log.info("  ✅ [%s] набрано полностью: %.2f/%d", label, total_filled, size)
                return True
        else:
            log.info("  📝 [%s] price=%.2f size=%.2f [PAPER, считаем исполненным]", attempt_label, price, remaining)
            tlog(f"  FAK    | {attempt_label} | price={price:.2f} size={remaining:.2f} | PAPER")
            return True
        await asyncio.sleep(FAK_PAUSE_SEC)
    if total_filled > 0:
        log.warning("  🟡 [%s] набрано частично: %.2f/%d за %d попыток", label, total_filled, size, FOK_MAX_ATTEMPTS)
    else:
        log.warning("  ⚠️  [%s] не набрано ничего за %d попыток", label, FOK_MAX_ATTEMPTS)
    return total_filled > 0

# ===========================================================================
# ОСНОВНАЯ ЛОГИКА
# ===========================================================================
async def check_and_trade_timeframe(session, client, tf_label, seg, current_markets, fired_levels):
    """fired_levels: dict[slot] -> set(levels, уже отработавших на этом рынке)."""
    now = int(time.time())
    slot = (now // seg) * seg
    remaining = seg - (now - slot)

    for asset in ASSETS:
        mkt = current_markets.get(asset)
        if mkt is None or mkt["slot"] != slot:
            new_mkt = await get_current_market(session, asset, tf_label, seg)
            if new_mkt:
                current_markets[asset] = new_mkt
                log.info("[%s] %s: новый рынок %s (%s)", tf_label, asset.upper(), new_mkt["slug"], new_mkt["question"])
            else:
                log.warning("[%s] %s: не удалось найти текущий рынок для слота %d", tf_label, asset.upper(), slot)

    if "btc" not in current_markets or "eth" not in current_markets:
        return

    btc_mkt = current_markets["btc"]
    eth_mkt = current_markets["eth"]
    if not (btc_mkt["slot"] == slot == eth_mkt["slot"]):
        return
    if remaining < (STALE_REMAINING_SEC_15M if seg == SEG_15M else STALE_REMAINING_SEC_5M):
        return

    btc_up_ask = await get_best_ask(session, btc_mkt["token_up"])
    eth_up_ask = await get_best_ask(session, eth_mkt["token_up"])
    if btc_up_ask is None or eth_up_ask is None:
        return

    spread = btc_up_ask - eth_up_ask
    abs_spread = abs(spread)
    log.info("[%s] BTC-Up=%.3f  ETH-Up=%.3f  spread=%+.3f  (осталось %ds)",
             tf_label, btc_up_ask, eth_up_ask, spread, remaining)

    if spread > 0:
        cheap_asset, cheap_token, cheap_ask = "ETH", eth_mkt["token_up"], eth_up_ask
        expensive_asset, expensive_mkt, expensive_up_ask = "BTC", btc_mkt, btc_up_ask
    else:
        cheap_asset, cheap_token, cheap_ask = "BTC", btc_mkt["token_up"], btc_up_ask
        expensive_asset, expensive_mkt, expensive_up_ask = "ETH", eth_mkt, eth_up_ask

    done = fired_levels.setdefault(slot, set())

    # находим уровни, которые ПРЕДСТОИТ обработать в этом цикле
    pending_levels = [lvl for lvl in SPREAD_LEVELS if abs_spread >= lvl and lvl not in done]
    if not pending_levels:
        return

    expensive_down_token = expensive_mkt["token_down"]
    # Раньше цена Down дорогой стороны бралась приближением 1-Up (без лишнего
    # запроса к API) — но реальный ask на Down часто отличается от этого
    # приближения (свой спред стакана), из-за чего нога B систематически
    # исполнялась дороже ноги A. Теперь берём живую цену тем же способом, что
    # и для Up-сторон; приближение остаётся только запасным вариантом, если
    # живой запрос не удался (сеть/таймаут).
    if LEG_B_ENABLED:
        expensive_down_ask = await get_best_ask(session, expensive_down_token)
        if expensive_down_ask is None:
            expensive_down_ask = round(1 - expensive_up_ask, 4)
            log.debug("[%s] live-цена ноги B недоступна, используем приближение 1-Up=%.4f", tf_label, expensive_down_ask)
    else:
        expensive_down_ask = None

    # Проверка на настоящий рассинхрон: обе ноги должны быть дешёвыми (<0.5).
    # Если expensive_up_ask <= 0.5, то дорогая сторона сама по себе не дорогая —
    # это просто разная скорость одного и того же движения, а не расхождение
    # исходов, и нога B (expensive_down_ask) окажется дороже 0.5. В этом случае
    # пропускаем весь цикл по этому слоту, ничего не покупаем.
    if LEG_B_ENABLED and (cheap_ask >= 0.5 or (expensive_down_ask is not None and expensive_down_ask >= 0.5)):
        log.info("[%s] пропуск: нет настоящего рассинхрона (cheap_ask=%.3f expensive_down_ask=%s >= 0.5)",
                  tf_label, cheap_ask, expensive_down_ask)
        return

    for level in pending_levels:
        size_a = SIZE_BY_LEVEL.get(level, 0)
        size_b = SIZE_BY_LEVEL_B.get(level, 0) if LEG_B_ENABLED else 0

        info = (f"[{tf_label}] level={level} spread={spread:+.3f} BTC-Up={btc_up_ask:.3f} ETH-Up={eth_up_ask:.3f} "
                f"нога_A={cheap_asset}-Up(size={size_a}) нога_B={expensive_asset}-Down(size={size_b})")
        log.warning("🔥 [%s] ПОРОГ %.2f пересечён (|spread|=%.3f) | A=%s-Up size=%d | B=%s-Down size=%d",
                    tf_label, level, abs_spread, cheap_asset, size_a, expensive_asset, size_b)
        tlog(f"TRIGGER | tf={tf_label} slot={slot} level={level} | {info}")

        done.add(level)  # помечаем СРАЗУ (строго один раз), независимо от результата попыток

        # Раньше нога B ждала полного завершения ноги A (включая все её ретраи,
        # это могло занимать секунды и делало цену ноги B устаревшей к моменту
        # покупки). Теперь обе ноги стартуют одновременно.
        async def _buy_leg_a():
            if size_a > 0:
                label_a = f"{cheap_asset}-Up {tf_label} spread>={level}"
                try:
                    await buy_fak_with_retries(session, client, cheap_token, cheap_ask, size_a, label_a)
                except Exception as e:
                    log.error("buy_fak_with_retries (нога A): %s", e)

        async def _buy_leg_b():
            if LEG_B_ENABLED and size_b > 0 and expensive_down_ask is not None:
                label_b = f"{expensive_asset}-Down {tf_label} spread>={level}"
                try:
                    await buy_fak_with_retries(session, client, expensive_down_token, expensive_down_ask, size_b, label_b)
                except Exception as e:
                    log.error("buy_fak_with_retries (нога B): %s", e)

        await asyncio.gather(_buy_leg_a(), _buy_leg_b())


async def main():
    log.info("=" * 65)
    log.info("  SPREAD ARBITRAGE BOT v2 (пороговые сигналы + FOK) | BTC-Up vs ETH-Up")
    log.info("  EXECUTION=%s", EXECUTION_ENABLED)
    log.info("  Пороги спреда (каждый — независимый сигнал, 1 раз/рынок): %s", SPREAD_LEVELS)
    log.info("  Нога A (дешёвая Up) — объём по порогам: %s", SIZE_BY_LEVEL)
    log.info("  Нога B (Down дорогой стороны): %s, объём по порогам: %s", "ВКЛЮЧЕНА" if LEG_B_ENABLED else "выключена", SIZE_BY_LEVEL_B if LEG_B_ENABLED else "-")
    log.info("  FAK (частичное исполнение + добор): макс. попыток=%d, шаг цены=%.2f, пауза=%.1fs", FOK_MAX_ATTEMPTS, FOK_PRICE_STEP, FAK_PAUSE_SEC)
    log.info("  Таймфреймы: 15m=%s  5m=%s", ENABLE_15M, ENABLE_5M)
    log.info("  Опрос: каждые %.1fs", POLL_INTERVAL_SEC)
    log.info("  ⚠️  Механика проверена как явление ('спред схлопывается') на 15m реальных данных;")
    log.info("     конкретно ЭТИ 8 порогов и FOK-исполнение — не тестировались отдельно")
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

    state_15m = {"current_markets": {}, "fired_levels": {}}
    state_5m  = {"current_markets": {}, "fired_levels": {}}

    async with aiohttp.ClientSession() as session:
        while True:
            cycle_start = time.time()

            if ENABLE_15M:
                try:
                    await check_and_trade_timeframe(session, client, "15m", SEG_15M,
                                                     state_15m["current_markets"], state_15m["fired_levels"])
                except Exception as e:
                    log.error("15m check: %s", e)
            if ENABLE_5M:
                try:
                    await check_and_trade_timeframe(session, client, "5m", SEG_5M,
                                                     state_5m["current_markets"], state_5m["fired_levels"])
                except Exception as e:
                    log.error("5m check: %s", e)

            now = int(time.time())
            state_15m["fired_levels"] = {s: lv for s, lv in state_15m["fired_levels"].items() if s >= now - SEG_15M*4}
            state_5m["fired_levels"] = {s: lv for s, lv in state_5m["fired_levels"].items() if s >= now - SEG_5M*4}

            elapsed = time.time() - cycle_start
            wait = max(0, POLL_INTERVAL_SEC - elapsed)
            await asyncio.sleep(wait)

if __name__ == "__main__":
    asyncio.run(main())
