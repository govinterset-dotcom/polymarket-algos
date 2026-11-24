#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chainlink_edge.py
------------------
Самостоятельный боевой скрипт. Идея: не смотреть на цену Polymarket как на
источник истины (она отстаёт), а слушать НАПРЯМУЮ тот же самый источник,
что указан в правилах резолва рынка — Chainlink data stream (RTDS от
самого Polymarket, топик crypto_prices_chainlink). Это не приближение
через биржи — это тот же самый поток, что определяет исход.

ЛОГИКА:
  1. Слушаем Chainlink-цену для BTC/ETH/XRP/SOL непрерывно.
  2. В момент открытия каждого 5m/15m окна фиксируем цену как window_open.
  3. Считаем дистанцию: (текущая_цена - open) / open * 100%.
  4. ЖИВАЯ МОДЕЛЬ УВЕРЕННОСТИ (принцип отражения для броуновского
     движения, см. функцию confidence_from_model): confidence = 1 -
     2*Phi(-|D|/(sigma*sqrt(T))), где sigma оценивается динамически по
     последним VOL_LOOKBACK_SEC секундам реальных данных, не из
     статичной таблицы.
  5. КРИТЕРИЙ СРАБАТЫВАНИЯ — не цена сама по себе, а РАЗРЫВ между
     confidence модели и текущей ценой Polymarket для того же
     направления: spread = confidence - цена_рынка. Срабатывает при
     spread >= SPREAD_THRESHOLD (по умолчанию 0.10) и confidence не ниже
     MIN_CONFIDENCE_FLOOR. Цена в стакане используется ТОЛЬКО в момент
     сделки — как потолок для FAK, чтобы забрать лучшее из доступного.
     Работает на всём отрезке рынка, не только в последние секунды —
     несколько быстрых попыток).

ПРЕЖНЯЯ КАЛИБРОВКА (посекундные данные Binance, BTC+ETH, 6 дней, 2016 окон 5м +
672 окна 15м — использовалась для ПРОВЕРКИ модели, не как рабочий порог):
  T-2с, дистанция >= 0.05%: 100.0% совпадения с финалом (5м и 15м, оба актива)
  T-1с, дистанция >= 0.005% (любая измеримая): 100.0% совпадения (все случаи)
  Выбран порог 0.05% с запасом на T-2 и T-1 — самый строгий уровень, где
  на ВСЕХ проверенных комбинациях (актив x таймфрейм x момент) наблюдалось
  ровно 100% (0 разворотов из тысяч наблюдений).

ВАЖНЫЕ ОГОВОРКИ:
  - Калибровка сделана на Binance-данных (BTC/ETH), НЕ на самом Chainlink
    напрямую — Chainlink может вести себя чуть иначе (сглаженнее из-за
    агрегации нескольких источников). Структура правила (время x дистанция
    -> уверенность) должна работать аналогично, но точные числа не
    подтверждены отдельно на самом Chainlink.
  - Для XRP и SOL калибровки вообще не было (данные есть только для
    BTC/ETH) — используется ТА ЖЕ сетка по экстраполяции, не проверено.
  - 100% на исторических данных не означает 100% в будущем — это редкое,
    но не невозможное событие (asymptotic guarantee, не математическая
    гарантия).
  - Формат RTDS WebSocket и субскрипции взят из официальной документации
    Polymarket, но НЕ проверен вживую в этом коде — обязательно
    paper-режим (EXECUTION_ENABLED=false) перед реальными деньгами.
"""

import os, sys, json, time, asyncio, logging, math
import numpy as np
from scipy.stats import norm
from decimal import Decimal, ROUND_HALF_UP
import aiohttp

_dotenv_loaded = False
try:
    from dotenv import load_dotenv
    _dotenv_loaded = load_dotenv()
except ImportError:
    print("ВНИМАНИЕ: python-dotenv не установлен — .env не будет прочитан!", file=sys.stderr)

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
RTDS_WS = "wss://ws-live-data.polymarket.com"

EXECUTION_ENABLED = os.environ.get("EXECUTION_ENABLED", "false").lower() == "true"
PRIVATE_KEY       = os.environ.get("POLY_WALLET_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY", "")
FUNDER            = os.environ.get("POLY_FUNDER_ADDRESS") or os.environ.get("FUNDER", "")
SIGNATURE_TYPE    = int(os.environ.get("POLY_SIGNATURE_TYPE", "3"))
CHAIN_ID          = int(os.environ.get("POLY_CHAIN_ID", "137"))
API_KEY           = os.environ.get("CLOB_API_KEY", "")
API_SECRET        = os.environ.get("CLOB_API_SECRET", "")
API_PASSPHRASE    = os.environ.get("CLOB_API_PASSPHRASE", "")

ASSETS = [a.strip().lower() for a in os.environ.get("ASSETS", "btc,eth,xrp,sol").split(",") if a.strip()]
SEGMENTS = {"5m": 300, "15m": 900}

# Chainlink RTDS символы (slash-separated, как в документации)
CHAINLINK_SYMBOL = {"btc": "btc/usd", "eth": "eth/usd", "xrp": "xrp/usd", "sol": "sol/usd"}

# --- Модель уверенности (принцип отражения, см. докстринг) ---
VOL_LOOKBACK_SEC = float(os.environ.get("VOL_LOOKBACK_SEC", "300"))   # сколько истории используем для оценки волатильности
VOL_REFRESH_SEC = float(os.environ.get("VOL_REFRESH_SEC", "5"))       # как часто пересчитываем волатильность
VOL_MIN_SAMPLES = int(os.environ.get("VOL_MIN_SAMPLES", "30"))        # минимум точек, чтобы доверять оценке σ

# --- Тайминг взведения рынка ---
# ARM_BEFORE_SEC убран — раньше означал "взвести за N секунд ДО закрытия",
# что при работе на всём отрезке рынка означало торговлю фактически
# только в эти последние N секунд. Теперь взводим сразу после открытия
# окна, непрерывно пробуя, пока не получится (см. ниже в scheduler).
# Насколько часто заново подписываемся на RTDS, чтобы форсировать свежий
# снимок цены — подтверждено, что настоящего непрерывного потока может не
# быть, только редкие снимки. Слишком часто — риск упереться в лимиты
# сервера; слишком редко — цена устаревает (было 20с, давало расхождения
# до 0.05% с реальной ценой открытия).
RTDS_RESUB_SEC = float(os.environ.get("RTDS_RESUB_SEC", "30"))
# Watchdog: если от сервера вообще нет НИ ОДНОГО сообщения дольше этого
# времени — соединение технически "живо" (aiohttp не видит обрыва), но
# сервер молчит. Подтверждено на практике: 15+ минут полной тишины без
# единого reconnect-события, chainlink_open замирал на одном значении
# на несколько окон подряд. Форсируем закрытие соединения сами.
RTDS_STALE_TIMEOUT_SEC = float(os.environ.get("RTDS_STALE_TIMEOUT_SEC", "15"))

# --- КРИТЕРИЙ СРАБАТЫВАНИЯ: разрыв между уверенностью модели и ценой
# в стакане Polymarket, а НЕ голая цена и НЕ фиксированное временное окно.
# Идея: цена в стакане сама по себе НЕ критерий — она нужна только в
# момент сделки, чтобы через FAK забрать лучшее из доступного в стакане.
# Настоящий сигнал — это МОМЕНТ, когда модель оценивает вероятность выше,
# чем уже заложено в цену рынка. Работает на ВСЁМ отрезке рынка (нет
# больше отдельного FIRE_WINDOW_SEC) — но порог дистанции в самой модели
# уверенности сам по себе требует непропорционально больших движений
# рано в окне (см. историю чата — расчёт конкретных порогов по времени).
SPREAD_THRESHOLD = float(os.environ.get("SPREAD_THRESHOLD", "0.10"))  # 10% разрыв по умолчанию
MIN_CONFIDENCE_FLOOR = float(os.environ.get("MIN_CONFIDENCE_FLOOR", "0.80"))  # не рассматриваем сигнал вообще ниже этого уровня уверенности
ENTRY_PRICE_CAP = float(os.environ.get("ENTRY_PRICE_CAP", "0.97"))  # абсолютный потолок цены FAK, даже если confidence выше

# --- Многократные срабатывания на ОДНОМ рынке — без ограничения "одна
# сделка за окно". Разрыв может появляться и исчезать несколько раз за
# время жизни рынка. Единственная защита от бесконтрольного наращивания
# позиции на одном и том же сигнале — пауза между срабатываниями.
REFIRE_COOLDOWN_SEC = float(os.environ.get("REFIRE_COOLDOWN_SEC", "5"))

SIZE = float(os.environ.get("SIZE", "10"))
MIN_ORDER_VALUE = float(os.environ.get("MIN_ORDER_VALUE", "1.05"))

# --- Несколько быстрых попыток FAK, как только уверенность достигнута ---
FAK_ATTEMPTS = int(os.environ.get("FAK_ATTEMPTS", "5"))
FAK_INTERVAL_SEC = float(os.environ.get("FAK_INTERVAL_SEC", "0.15"))

MONITOR_POLL_SEC = float(os.environ.get("MONITOR_POLL_SEC", "0.1"))
STOP_BEFORE_SEC = float(os.environ.get("STOP_BEFORE_SEC", "0.3"))

logging.basicConfig(level=logging.INFO)
class MillisecondFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        s = time.strftime("%H:%M:%S", ct)
        return f"{s}.{int(record.msecs):03d}"
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(MillisecondFormatter("%(asctime)s | %(levelname)s | %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_console_handler])
log = logging.getLogger("chainlink_edge")
_fh = logging.FileHandler("chainlink_edge_trades.log", encoding="utf-8")
_fh.setFormatter(MillisecondFormatter("%(asctime)s | %(levelname)s | %(message)s"))
log.addHandler(_fh)

# ===========================================================================
# CHAINLINK RTDS
# ===========================================================================

latest_chainlink_price = {a.upper(): None for a in ASSETS}
_rtds_msg_count = {a.upper(): 0 for a in ASSETS}  # сколько РЕАЛЬНЫХ сообщений (не повторов) пришло с момента старта
_last_rtds_message_time = time.time()  # для watchdog "сервер молчит слишком долго"
latest_chainlink_time = {a.upper(): None for a in ASSETS}
_chainlink_lock = asyncio.Lock()

# История цен (timestamp_ms, value) для оценки волатильности "на лету" —
# не статичная таблица, а живой пересчёт по последним VOL_LOOKBACK_SEC
# секундам данных, обновляемый не чаще чем раз в VOL_REFRESH_SEC.
# Словарь timestamp_ms -> value, а не просто список/очередь — RTDS шлёт
# ПЕРЕКРЫВАЮЩИЕСЯ пакеты (каждое сообщение содержит последние ~30-60с,
# большинство точек повторяются из предыдущих сообщений). Словарь
# естественно устраняет дубли по timestamp и не портит порядок по
# времени, в отличие от простого добавления всего подряд в очередь.
price_history = {a.upper(): {} for a in ASSETS}
PRICE_HISTORY_MAX_AGE_SEC = 600  # держим не больше 10 минут истории на актив
_sigma_cache = {a.upper(): {"sigma_1s_pct": None, "last_calc": 0.0, "n_samples": 0} for a in ASSETS}

def estimate_sigma_1s(asset_upper):
    """Оценка волатильности за 1 секунду (в %) по последним VOL_LOOKBACK_SEC
    секундам истории. Кэшируется на VOL_REFRESH_SEC, чтобы не пересчитывать
    на каждой проверке — волатильность не меняется настолько быстро."""
    cache = _sigma_cache[asset_upper]
    now = time.time()
    if cache["sigma_1s_pct"] is not None and (now - cache["last_calc"]) < VOL_REFRESH_SEC:
        return cache["sigma_1s_pct"], cache["n_samples"]

    hist = price_history[asset_upper]
    if len(hist) < VOL_MIN_SAMPLES + 1:
        return None, len(hist)

    cutoff_ms = (now - VOL_LOOKBACK_SEC) * 1000
    # явная сортировка по timestamp — словарь не гарантирует порядок,
    # а после дедупликации нам критично идти строго по возрастанию времени
    recent_items = sorted((ts, v) for ts, v in hist.items() if ts >= cutoff_ms)
    if len(recent_items) < VOL_MIN_SAMPLES + 1:
        recent_items = sorted(hist.items())[-(VOL_MIN_SAMPLES + 1):]

    values = [v for _, v in recent_items]
    diffs_pct = [ (values[i+1]/values[i] - 1) * 100 for i in range(len(values)-1) if values[i] != 0 ]
    if len(diffs_pct) < VOL_MIN_SAMPLES:
        return None, len(diffs_pct)

    sigma = float(np.std(diffs_pct)) if diffs_pct else None
    cache["sigma_1s_pct"] = sigma
    cache["last_calc"] = now
    cache["n_samples"] = len(diffs_pct)
    return sigma, len(diffs_pct)

def confidence_from_model(distance_pct, seconds_remaining, sigma_1s_pct):
    """Принцип отражения для броуновского движения без сноса: вероятность
    пересечения барьера (разворота) = 2*Phi(-D/(sigma*sqrt(T))).
    confidence = 1 - P(разворот)."""
    if seconds_remaining <= 0 or sigma_1s_pct is None or sigma_1s_pct <= 0:
        return None
    z = -abs(distance_pct) / (sigma_1s_pct * math.sqrt(seconds_remaining))
    p_reversal = 2 * norm.cdf(z)
    return 1 - p_reversal

async def chainlink_rtds_listener():
    """Слушаем RTDS напрямую. Формат подписки и сообщений — из официальной
    документации Polymarket (docs.polymarket.com/market-data/websocket/rtds),
    НЕ проверено вживую в этом коде — сверить в paper-режиме."""
    # ИСПРАВЛЕНО: подписка БЕЗ фильтра по символу (filters="") — найдено в
    # рабочем скрипте (arb_bot.py), который явно ждёт type='update' и
    # получает его именно с пустым фильтром. Наша прежняя фильтрованная
    # по символу подписка давала только одноразовый снимок (type=
    # 'subscribe'), не непрерывный поток. Фильтруем по символу сами,
    # разбирая payload.symbol на своей стороне.
    sub_msg = {"action": "subscribe",
               "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}]}
    while True:
        ping_task = None
        resub_task = None
        watchdog_task = None
        global _last_rtds_message_time
        _last_rtds_message_time = time.time()  # сброс при каждом новом подключении
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(RTDS_WS, heartbeat=5) as ws:
                    await ws.send_json(sub_msg)
                    log.info("RTDS Chainlink WS подключен и подписан: %s", [CHAINLINK_SYMBOL[a] for a in ASSETS])

                    async def _send_pings():
                        # Документация явно требует прикладной PING каждые
                        # 5 секунд для поддержания соединения — полагаться
                        # только на встроенный heartbeat aiohttp недостаточно.
                        while not ws.closed:
                            await asyncio.sleep(5)
                            if not ws.closed:
                                await ws.ping()
                    ping_task = asyncio.create_task(_send_pings())

                    async def _resubscribe_periodically():
                        # ПОДОЗРЕНИЕ: сервер, возможно, шлёт только ОДИН
                        # начальный снимок (type='subscribe') при подписке,
                        # а дальше молчит — реальных потоковых 'update' может
                        # не быть вовсе. Подтверждено на практике: RTDS шлёт
                        # только 1-2 сообщения за 30с — ровно от нашей же
                        # повторной подписки, не от настоящего live-потока.
                        # Раньше было 20с (слишком редко — цена устаревала
                        # до 20с, что и объясняло расхождения с "Price To
                        # Beat"). Сократили до RTDS_RESUB_SEC.
                        while not ws.closed:
                            await asyncio.sleep(RTDS_RESUB_SEC)
                            if not ws.closed:
                                await ws.send_json(sub_msg)
                    resub_task = asyncio.create_task(_resubscribe_periodically())

                    async def _watchdog():
                        # Форсируем закрытие, если от сервера вообще нет
                        # сообщений дольше RTDS_STALE_TIMEOUT_SEC — иначе
                        # соединение может "тихо висеть" сколько угодно.
                        global _last_rtds_message_time
                        while not ws.closed:
                            await asyncio.sleep(3)
                            if time.time() - _last_rtds_message_time > RTDS_STALE_TIMEOUT_SEC:
                                log.warning("RTDS: тишина дольше %.0fс — принудительно закрываем соединение "
                                            "для переподключения", RTDS_STALE_TIMEOUT_SEC)
                                await ws.close()
                                return
                    watchdog_task = asyncio.create_task(_watchdog())

                    async for msg in ws:
                        _last_rtds_message_time = time.time()
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        if not msg.data:
                            # сервер иногда шлёт пустые текстовые сообщения
                            # (не JSON) — пропускаем, не роняя всю сессию
                            continue
                        try:
                            data = json.loads(msg.data)
                        except (json.JSONDecodeError, TypeError) as e:
                            log.debug("RTDS: не удалось разобрать сообщение (%s): %r", e, msg.data[:200])
                            continue
                        # Диагностика на DEBUG (была на INFO, пока искали
                        # причину — реальный формат отличается от документации,
                        # см. комментарий ниже).
                        data_arr = data.get("payload", {}).get("data")
                        last_point = data_arr[-1] if isinstance(data_arr, list) and data_arr else None
                        log.debug("RTDS СЫРОЕ СООБЩЕНИЕ: topic=%r type=%r outer_timestamp=%r "
                                 "payload_keys=%s data_len=%s последняя_точка=%s",
                                 data.get("topic"), data.get("type"), data.get("timestamp"),
                                 list(data.get("payload", {}).keys()),
                                 len(data_arr) if isinstance(data_arr, list) else None,
                                 last_point)
                        # РЕАЛЬНЫЙ формат оказался другим, чем в документации:
                        # topic='crypto_prices' (не 'crypto_prices_chainlink'),
                        # type='subscribe' (стартовый снимок, массив точек в
                        # payload.data), символ — в payload.symbol. Не филь-
                        # труем строго по topic/type, просто пытаемся вытащить
                        # символ+последнюю цену из того, что реально пришло.
                        payload = data.get("payload", {})
                        symbol = payload.get("symbol", "")
                        if not symbol:
                            continue

                        value = None
                        ts_ms = None
                        data_list = payload.get("data")
                        if isinstance(data_list, list) and data_list:
                            # снимок/пакет точек — берём последнюю (самую свежую)
                            last = data_list[-1]
                            value = last.get("value")
                            ts_ms = last.get("timestamp")
                        elif payload.get("value") is not None:
                            # на случай, если ongoing update придёт одной точкой
                            value = payload.get("value")
                            ts_ms = payload.get("timestamp")
                        if value is None:
                            continue

                        asset_upper = None
                        for a, sym in CHAINLINK_SYMBOL.items():
                            if sym == symbol:
                                asset_upper = a.upper()
                                break
                        if asset_upper is None:
                            continue
                        async with _chainlink_lock:
                            latest_chainlink_price[asset_upper] = float(value)
                            # Историю пополняем ВСЕМИ точками снимка сразу
                            # (даёт мгновенную базу для оценки волатильности,
                            # не нужно ждать накопления по одной точке).
                            # По ключу timestamp — если точка уже есть,
                            # просто перезаписывается тем же значением,
                            # дублей не возникает, порядок по времени не
                            # портится перекрывающимися пакетами.
                            hist = price_history[asset_upper]
                            if isinstance(data_list, list) and data_list:
                                for pt in data_list:
                                    pv, pts = pt.get("value"), pt.get("timestamp")
                                    if pv is not None and pts is not None:
                                        hist[pts] = float(pv)
                            else:
                                hist[ts_ms] = float(value)
                            # чистим слишком старые точки, чтобы словарь не рос бесконечно
                            cutoff = (time.time() - PRICE_HISTORY_MAX_AGE_SEC) * 1000
                            stale_keys = [k for k in hist if k < cutoff]
                            for k in stale_keys:
                                del hist[k]
                        latest_chainlink_time[asset_upper] = time.time()
                        _rtds_msg_count[asset_upper] += 1
        except Exception as e:
            log.warning("RTDS Chainlink WS обрыв: %s - переподключение через 3с", e)
            await asyncio.sleep(3)
        finally:
            if ping_task is not None:
                ping_task.cancel()
            if resub_task is not None:
                resub_task.cancel()
            if watchdog_task is not None:
                watchdog_task.cancel()

# ===========================================================================
# POLYMARKET (переиспользуем ту же инфраструктуру, что в closer.py)
# ===========================================================================

_dumped_market_once = False

async def fetch_event_markets(session, slug):
    global _dumped_market_once
    try:
        async with session.get(
            f"{GAMMA_REST}/events", params={"slug": slug},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status == 200:
                data = await r.json()
                if data:
                    ev = data[0] if isinstance(data, list) else data
                    if not _dumped_market_once:
                        # ОДНОКРАТНАЯ диагностика: печатаем ВЕСЬ JSON рынка,
                        # чтобы найти поле с "Price To Beat" — не гадаем
                        # название поля, смотрим на реальные данные, как
                        # раньше делали с форматом RTDS.
                        _dumped_market_once = True
                        log.warning("ДИАГНОСТИКА (один раз): полный JSON рынка %s:\n%s",
                                    slug, json.dumps(ev, indent=2, ensure_ascii=False)[:5000])
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
_clob_call_lock = asyncio.Lock()

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

    t0 = time.time()
    tick = get_tick(client, token_id)
    neg_risk = get_neg_risk(client, token_id)
    t1 = time.time()
    args = OrderArgs(token_id=token_id, price=price, size=size, side=Side.BUY)
    opts = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
    try:
        signed = client.create_order(args, opts)
        t2 = time.time()
        resp = client.post_order(signed, order_type)
        t3 = time.time()
        log.info("  [%s] хронометраж place_order: tick/neg_risk=%.3fс create_order(подпись)=%.3fс post_order(сеть)=%.3fс",
                  label, t1-t0, t2-t1, t3-t2)
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
# СОСТОЯНИЕ ОКОН + УВЕРЕННОСТЬ ПО CHAINLINK
# ===========================================================================

class WindowState:
    __slots__ = ("slot", "market", "armed", "fired", "window_open", "window_open_is_reserve")
    def __init__(self):
        self.slot = None
        self.market = None
        self.armed = False
        self.fired = False
        self.window_open = None
        self.window_open_is_reserve = False  # True, если ориентир захвачен НЕ в момент открытия окна

states = {(a, tf): WindowState() for a in ASSETS for tf in SEGMENTS}

async def fire_now(session, client, token_id, label, tf_label, t_start, deadline, entry_price):
    for attempt in range(1, FAK_ATTEMPTS + 1):
        elapsed = time.time() - t_start
        remaining_to_close = deadline - time.time()
        if remaining_to_close <= 0.05:
            log.warning("[%s] останов: до закрытия %.2fс, новая попытка бессмысленна", label, remaining_to_close)
            return
        if EXECUTION_ENABLED:
            t_before_order = time.time()
            async with _clob_call_lock:
                filled, oid = await asyncio.to_thread(place_order, client, token_id, entry_price, SIZE,
                                                        f"{label} {tf_label} попытка {attempt}/{FAK_ATTEMPTS} t+{elapsed:.3f}с")
            t_after_order = time.time()
            log.info("[%s] попытка %d: сам сетевой вызов place_order занял %.3fс", label, attempt, t_after_order - t_before_order)
            if filled > 0:
                log.info("[%s] исполнено %.4f за %.3fс от сигнала уверенности (реальный момент сигнала)", label, filled, time.time()-t_start)
                return
        else:
            log.info("PAPER [%s попытка %d/%d, t+%.3fс] price=%.2f size=%.2f",
                     label, attempt, FAK_ATTEMPTS, elapsed, entry_price, SIZE)
            return
        if attempt < FAK_ATTEMPTS:
            remaining_to_close = deadline - time.time()
            if remaining_to_close <= FAK_INTERVAL_SEC + 0.05:
                log.warning("[%s] останов: не хватит времени на ещё одну попытку (осталось %.2fс)", label, remaining_to_close)
                return
            await asyncio.sleep(FAK_INTERVAL_SEC)
    log.warning("[%s] не набрано ничего за %d попыток", label, FAK_ATTEMPTS)

async def monitor_and_fire(session, client, asset, tf_label, market, slot, seg, window_open):
    """Непрерывно считаем confidence по формуле принципа отражения для
    броуновского движения (см. докстринг файла): confidence = 1 -
    2*Phi(-|D|/(sigma*sqrt(T))), где D — дистанция от window_open в %,
    T — секунд осталось, sigma — волатильность за 1с, оценённая ЖИВЬЁМ
    по последним VOL_LOOKBACK_SEC секундам, а не из статичной таблицы.
    Срабатываем, когда РАЗРЫВ между confidence и текущей ценой Polymarket
    (для того же направления) достигает SPREAD_THRESHOLD — бьём
    агрессивно по той стороне, забирая лучшее из доступного в стакане.

    БЕЗ ограничения "одна сделка на рынок" — можем сработать МНОГО раз за
    одно окно, если разрыв появляется повторно (открылся, закрылся,
    открылся снова). Единственная защита от бесконтрольного наращивания
    позиции — REFIRE_COOLDOWN_SEC между последовательными срабатываниями:
    после сделки ждём паузу, но мониторинг и вся работа НЕ прекращаются."""
    t_start = time.time()
    token_up = market["token_up"]
    token_down = market["token_down"]
    asset_upper = asset.upper()
    checks = 0
    last_fire_time = None
    fire_count = 0

    while True:
        now = time.time()
        remaining = slot + seg - now
        if remaining <= STOP_BEFORE_SEC:
            break

        checks += 1
        current_price = latest_chainlink_price.get(asset_upper)
        price_age = (now - latest_chainlink_time[asset_upper]) if latest_chainlink_time.get(asset_upper) else None
        price_is_fresh = price_age is not None and price_age <= RTDS_STALE_TIMEOUT_SEC

        if current_price is not None and window_open and price_is_fresh:
            distance_pct = (current_price - window_open) / window_open * 100
            sigma, n_samples = estimate_sigma_1s(asset_upper)
            conf = confidence_from_model(distance_pct, remaining, sigma)

            if conf is not None and conf >= MIN_CONFIDENCE_FLOOR:
                direction = "Up" if distance_pct > 0 else "Down"

                t_before_price_check = time.time()
                ask_up, ask_down = await asyncio.gather(
                    get_best_ask(session, token_up),
                    get_best_ask(session, token_down),
                )
                t_after_price_check = time.time()
                token_id = token_up if direction == "Up" else token_down
                side_price = ask_up if direction == "Up" else ask_down
                label = f"{asset_upper}-{direction}"

                if side_price is not None:
                    spread = conf - side_price
                    log.info("[%s/%s] проверка #%d, осталось %.2fс: confidence=%.4f цена_рынка=%.3f "
                             "разрыв=%.4f (нужно >=%.2f) [запрос цены занял %.3fс]",
                             asset_upper, tf_label, checks, remaining, conf, side_price, spread, SPREAD_THRESHOLD,
                             t_after_price_check - t_before_price_check)

                    in_cooldown = last_fire_time is not None and (time.time() - last_fire_time) < REFIRE_COOLDOWN_SEC
                    if spread >= SPREAD_THRESHOLD and in_cooldown:
                        log.info("[%s/%s] разрыв есть, но пауза после предыдущей сделки ещё не прошла "
                                 "(осталось %.1fс паузы) - пропуск этой проверки",
                                 asset_upper, tf_label, REFIRE_COOLDOWN_SEC - (time.time() - last_fire_time))
                    elif spread >= SPREAD_THRESHOLD:
                        t_gap_detected = time.time()
                        fire_count += 1
                        log.warning("[%s/%s] РАЗРЫВ НАЙДЕН #%d (проверка #%d, осталось %.2fс): open=%s текущая=%s "
                                    "дистанция=%.4f%% sigma_1s=%.5f%% (n=%d) -> confidence=%.4f, рынок=%.3f, "
                                    "разрыв=%.4f, направление=%s -> бьём агрессивно",
                                     asset_upper, tf_label, fire_count, checks, remaining, window_open, current_price,
                                     distance_pct, sigma if sigma else -1, n_samples, conf, side_price, spread, direction)
                        # Потолок FAK — чуть ниже нашей же уверенности (не
                        # выше), чтобы не переплачивать сверх собственной
                        # оценки, но с запасом на случай, если цена чуть
                        # подрастёт за время попыток.
                        entry_ceiling = min(conf, ENTRY_PRICE_CAP)
                        signal_time = time.time()
                        log.info("[%s/%s] ХРОНОМЕТРАЖ: запрос цены=%.3fс, обнаружение->сигнал=%.3fс",
                                 asset_upper, tf_label, t_after_price_check - t_before_price_check,
                                 signal_time - t_gap_detected)
                        deadline = slot + seg
                        await fire_now(session, client, token_id, label, tf_label, signal_time, deadline, entry_ceiling)
                        last_fire_time = time.time()
                        # НЕ return — продолжаем мониторинг и после сделки,
                        # пауза REFIRE_COOLDOWN_SEC защищает от немедленного
                        # повторного срабатывания на том же самом сигнале.
                else:
                    log.info("[%s/%s] уверенность есть, но %s уже дороже потолка (%.3f) - пропуск",
                              asset_upper, tf_label, label, side_price if side_price is not None else -1)
                # НЕ return здесь тоже — раньше это прерывало весь
                # мониторинг после первой же проверки confidence, даже
                # если сделки не произошло вообще.

        await asyncio.sleep(MONITOR_POLL_SEC)

    log.info("[%s/%s] мониторинг рынка закончен (%d проверок, %d сделок)",
              asset_upper, tf_label, checks, fire_count)

async def scheduler(session, client):
    last_stats_log = time.time()
    while True:
        now = time.time()
        if now - last_stats_log >= 30.0:
            log.warning("=== RTDS: сообщений с обновлением цены за последние ~30с: %s ===",
                        {a: _rtds_msg_count[a] for a in _rtds_msg_count})
            for a in _rtds_msg_count:
                _rtds_msg_count[a] = 0
            last_stats_log = now
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
                    # Захватываем цену ИМЕННО в момент начала нового окна —
                    # не в момент взведения (которое теперь тоже происходит
                    # почти сразу после старта, но всё равно чуть позже, из-за
                    # быть на 280+ секунде из 300, то есть почти в конце!
                    # Раньше window_open ловился именно там — это делало
                    # всю калибровку дистанции бессмысленной (сравнивали бы
                    # текущую цену с другой поздней ценой, не с настоящим
                    # открытием). Планировщик опрашивает часто (раз в
                    # MONITOR_POLL_SEC), так что здесь это довольно точное
                    # приближение к истинной границе слота.
                    cur_price = latest_chainlink_price.get(asset.upper())
                    cur_time = latest_chainlink_time.get(asset.upper())
                    price_is_fresh = cur_time is not None and (now - cur_time) <= RTDS_STALE_TIMEOUT_SEC
                    if cur_price is not None and price_is_fresh:
                        st.window_open = cur_price
                        st.window_open_is_reserve = False
                        log.info("[%s/%s] НОВОЕ ОКНО: slot=%d, chainlink_open=%s",
                                  asset.upper(), tf_label, slot, st.window_open)
                    else:
                        # Цена есть, но устарела (RTDS молчит) — не берём
                        # её как ориентир, иначе получаем "замороженный"
                        # window_open на несколько окон подряд подряд.
                        st.window_open = None
                        st.window_open_is_reserve = False
                        log.warning("[%s/%s] НОВОЕ ОКНО: цена устарела или отсутствует (возраст=%s) - "
                                    "ждём резервного захвата свежих данных",
                                    asset.upper(), tf_label,
                                    f"{now-cur_time:.1f}с" if cur_time else "нет данных")

                if not st.armed and remaining > STOP_BEFORE_SEC:
                    # ИСПРАВЛЕНО: раньше взводили по ОСТАВШЕМУСЯ времени
                    # (remaining <= ARM_BEFORE_SEC) — то есть только в
                    # последние 15с ПЕРЕД ЗАКРЫТИЕМ окна. Раз работаем
                    # теперь на всём отрезке рынка, это означало торговлю
                    # фактически только в эти же последние 15с, несмотря
                    # на снятое ограничение FIRE_WINDOW_SEC. Теперь
                    # пробуем взвести НЕПРЕРЫВНО, начиная сразу после
                    # открытия окна, и повторяем на каждом цикле, пока не
                    # получится (а не бросаем попытки после одного провала).
                    mkt = await get_current_market(session, asset, tf_label, seg)
                    if mkt is not None:
                        st.market = mkt
                        st.armed = True
                        log.info("[%s/%s] ВЗВЕДЕНО: осталось %.1fs, рынок=%s",
                                  asset.upper(), tf_label, remaining, mkt["slug"])
                        if EXECUTION_ENABLED:
                            async def _warm_cache(tok):
                                async with _clob_call_lock:
                                    await asyncio.to_thread(get_tick, client, tok)
                                    await asyncio.to_thread(get_neg_risk, client, tok)
                            asyncio.create_task(_warm_cache(mkt["token_up"]))
                            asyncio.create_task(_warm_cache(mkt["token_down"]))
                    else:
                        log.warning("[%s/%s] не удалось взвести (рынок не найден)", asset.upper(), tf_label)

                # РЕЗЕРВНЫЙ захват window_open — на случай, если в момент
                # смены слота (выше) RTDS ещё не успел прислать ни одной
                # цены вообще (например, только что переподключились).
                # Повторяем на каждом цикле, пока не получится хоть какое-то
                # значение — лучше приближение, чем окно, которое навсегда
                # пропускается без единого предупреждения. Если это сработало
                # НЕ в момент смены слота, а позже — точность window_open
                # соответственно ниже (уже не совсем "истинное открытие").
                if st.slot is not None and st.window_open is None:
                    cur = latest_chainlink_price.get(asset.upper())
                    cur_time = latest_chainlink_time.get(asset.upper())
                    if cur is not None and cur_time is not None and (now - cur_time) <= RTDS_STALE_TIMEOUT_SEC:
                        st.window_open = cur
                        st.window_open_is_reserve = True
                        log.warning("[%s/%s] chainlink_open захвачен РЕЗЕРВНО (не в момент открытия окна!): %s "
                                    "(осталось %.1fs) -> ТОРГОВЛЯ ПО ЭТОМУ ОКНУ ПРОПУЩЕНА, ориентир ненадёжен",
                                  asset.upper(), tf_label, cur, remaining)

                if (st.armed and not st.fired and st.window_open is not None
                        and not st.window_open_is_reserve):
                    st.fired = True
                    asyncio.create_task(monitor_and_fire(session, client, asset, tf_label, st.market, st.slot, seg, st.window_open))

        await asyncio.sleep(MONITOR_POLL_SEC)

# ===========================================================================
# MAIN
# ===========================================================================

async def main():
    log.info("=" * 70)
    log.info("  CHAINLINK EDGE BOT - уверенность из прямого потока Chainlink RTDS")
    log.info("  .env найден и прочитан: %s", _dotenv_loaded)
    log.info("  АКТИВЫ: %s", ASSETS)
    log.info("  EXECUTION=%s", EXECUTION_ENABLED)
    log.info("  Критерий: разрыв confidence-цена >= %.2f (мин.confidence=%.2f), "
              "волатильность пересчитывается каждые %.0fс по последним %.0fс данных",
              SPREAD_THRESHOLD, MIN_CONFIDENCE_FLOOR, VOL_REFRESH_SEC, VOL_LOOKBACK_SEC)
    log.info("  Потолок цены входа: %.2f  Размер: %s  Попыток: %d, шаг %.2fс",
              ENTRY_PRICE_CAP, SIZE, FAK_ATTEMPTS, FAK_INTERVAL_SEC)
    log.info("  ВНИМАНИЕ: формат RTDS не проверен вживую — обязательно paper-режим сначала")
    log.info("  ВНИМАНИЕ: калибровка для XRP/SOL не подтверждена (данные были только для BTC/ETH)")
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
        await asyncio.gather(
            chainlink_rtds_listener(),
            scheduler(session, client),
        )

if __name__ == "__main__":
    asyncio.run(main())
