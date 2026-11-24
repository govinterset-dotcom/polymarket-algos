#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
skew_momentum_bot.py
----------------------
Реконструкция стратегии трейдера 0xAAAAA (0x251c1a283703beed41590b0875a8dcb8ddd1541f)
по РЕАЛЬНЫМ данным его сделок (575 рынков, официальные redeem-исходы, не догадка):

  ФАКТЫ ИЗ ДАННЫХ (fetch_full_history.py + analyze_strategy.py, 577 рынков,
  12138 покупок, только BTC 5m Up/Down):
  - Сторона с БОЛЬШИМ накопленным объёмом оказывается победителем в 83.8%
    случаев (482/575) — статистически надёжно на такой выборке.
  - Суммарный P&L по этим 575 рынкам: +$12,977.93 (среднее +$22.57/рынок,
    363 рынка в плюсе, 212 в минусе).
  - Медианный перекос (skew) — всего 1.5x (не экстремальный), среднее 1.95x.
  - Медиана 20 клипов на рынок (среднее 21) — непрерывная лесенка, не 1-2 входа.
  - Докупка ТОЙ ЖЕ стороны в 66.8% случаев происходит по цене ВЫШЕ предыдущей
    покупки этой стороны (11015 последовательных пар) — то есть докупает
    В РОСТ цены, не на дипах. Это pyramid/momentum-chase, не mean-reversion.
  - Финальный перекос НЕ решается рано: совпадение текущего перекоса с
    финальным — 62.6% на первых 25% клипов, 70.1% на 50%, 84.5% на 75%,
    100% в конце (тривиально). Значит перекос ДРЕЙФУЕТ весь эпизод —
    механизм реактивный, не "решил в начале и следует плану".
  - Смена стороны клипа: 62.7% клипов подряд на ТОЙ ЖЕ стороне (сериями),
    37.3% — смена. Медианный интервал между клипами — 5 секунд.

  ВЫВОД / РЕКОНСТРУИРОВАННОЕ ПРАВИЛО: каждые несколько секунд трейдер
  докупает клип на той стороне, чей ask только что ВЫРОС за последние
  секунды (момент импульса самой цены Polymarket — Binance не нужен,
  т.к. рост ask_up УЖЕ означает, что BTC только что тикнул вверх).
  Перекос — не целевой параметр, а естественный ПОБОЧНЫЙ ЭФФЕКТ повторной
  покупки текущего краткосрочного момента.

  ВАЖНАЯ ОГОВОРКА: это лучшая реконструкция по факту сделок, а НЕ
  подтверждённая копия внутренней логики трейдера — у нас нет доступа к
  его реальному коду. Проверять строго в paper-режиме перед live.

ЗАПУСК:
  pip install aiohttp python-dotenv
  python3 skew_momentum_bot.py                          # paper
  EXECUTION_ENABLED=true python3 skew_momentum_bot.py   # live
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

# ── WebSocket-мониторинг цены (вместо REST-поллинга) ──
USE_WS_MONITORING     = os.environ.get("USE_WS_MONITORING", "true").lower() == "true"
WS_PING_INTERVAL_SEC  = float(os.environ.get("WS_PING_INTERVAL_SEC",  "5.0"))
WS_SILENT_TIMEOUT_SEC = float(os.environ.get("WS_SILENT_TIMEOUT_SEC", "15.0"))  # нет сообщений столько — считаем связь мёртвой
WS_RECONNECT_BASE_SEC = float(os.environ.get("WS_RECONNECT_BASE_SEC", "1.0"))
WS_RECONNECT_MAX_SEC  = float(os.environ.get("WS_RECONNECT_MAX_SEC",  "20.0"))
WS_STALE_FALLBACK_SEC = float(os.environ.get("WS_STALE_FALLBACK_SEC", "3.0"))  # если данные по токену старше — берём REST

# ── Логирование сырых тиков цены (для будущего бэктеста параметров момента) ──
# Полностью отдельно от торговой логики — по умолчанию ВЫКЛЮЧЕНО, и даже
# если что-то в этом коде сломается, обёрнуто в try/except так, чтобы
# торговля НИКОГДА не пострадала из-за логирования.
LOG_RAW_TICKS      = os.environ.get("LOG_RAW_TICKS", "false").lower() == "true"
RAW_TICKS_LOG_PATH = os.environ.get("RAW_TICKS_LOG_PATH", "raw_ticks.jsonl")

# ВАЛИДИРОВАНО ТОЛЬКО НА BTC/5m (все 577 рынков в датасете — BTC 5m).
ASSETS = [a.strip().lower() for a in os.environ.get("ASSETS", "btc").split(",") if a.strip()]
SEG = 300  # 5 минут

# ── Сердце стратегии: momentum-ladder ──
MOMENTUM_WINDOW_SEC   = float(os.environ.get("MOMENTUM_WINDOW_SEC",   "2.0"))   # окно оценки роста (~медиана интервала клипов)
# Порог скачка задан как ПРОЦЕНТ роста ask за окно (Δp/p_прошлый), ОДИН
# И ТОТ ЖЕ по всему диапазону цен — без ранжирования/множителя по
# удалённости от 0.5 (раньше был абс. шаг цены + edge-мультипликатор,
# растущий к краям 0-1; теперь этого нет — просто факт скачка на N%).
MOMENTUM_MIN_RISE_PCT = float(os.environ.get("MOMENTUM_MIN_RISE_PCT", "0.10"))  # порог роста ask за окно, в долях (0.10 = 10%), без зон по цене
CLIP_SIZE             = float(os.environ.get("CLIP_SIZE",            "100"))   # акций на клип — как у трейдера
# Рост размера клипа по ПОРЯДКУ СРАБАТЫВАНИЯ на рынке — не по стороне и не по
# исходу (мы не знаем исход заранее). 1-й клип на рынке = CLIP_SIZE, 2-й =
# CLIP_SIZE*(1+growth), 3-й = CLIP_SIZE*(1+growth)^2, и так далее, независимо
# от того, на какую сторону приходится каждый клип. Проверено бэктестом на
# 28 реальных рынках: рост суммарного P&L с ростом ставки, без ухудшения
# худшего случая — но выборка ещё небольшая, поэтому по умолчанию выключено.
CLIP_SIZE_GROWTH      = float(os.environ.get("CLIP_SIZE_GROWTH",      "0.0"))   # 0.0 = как раньше, без роста
CLIP_INTERVAL_SEC     = float(os.environ.get("CLIP_INTERVAL_SEC",    "2.0"))   # минимальный интервал между клипами — медиана из данных
MAX_CLIP_PRICE        = float(os.environ.get("MAX_CLIP_PRICE",       "0.93"))  # не докупать выше этой цены (риск при почти решённом исходе)
MIN_REMAINING_FOR_ENTRY_SEC = float(os.environ.get("MIN_REMAINING_FOR_ENTRY_SEC", "1.0"))
MAX_CLIPS_PER_MARKET  = int(os.environ.get("MAX_CLIPS_PER_MARKET",   "40"))    # защитный потолок на рынок
# Потолок на перекос — только страховка от экстремального выброса (нашли
# конкретный случай: 13x, разворот в конце, -$42.70 почти весь убыток
# сессии). У трейдера максимум за 577 рынков — 10x, редкий выброс.
# 8x — с запасом, чтобы не мешать нормальному разгону перекоса.
MAX_SKEW_RATIO        = float(os.environ.get("MAX_SKEW_RATIO",       "8.0"))
# Буфер к ask при покупке (для гарантии FAK-филла). Раньше был захардкожен
# 0.02 — по сравнению с реальным трейдером (наша средняя пара 1.225 против
# его 1.100) это заметная часть переплаты. Теперь настраиваемый.
BUY_PRICE_BUFFER      = float(os.environ.get("BUY_PRICE_BUFFER",     "0.02"))

# ── Торговля "через рынок" (по запросу пользователя) ──
# Бот пока не гасит (redeem) выигранные позиции сам — деньги с выигравшей
# стороны не возвращаются на баланс автоматически. Чтобы капитал не
# запирался в бесконечно растущем хвосте непогашенных рынков подряд,
# после каждого отработанного 5-минутного окна СЛЕДУЮЩЕЕ окно за ним
# сознательно пропускается — даёт время на ручное/внешнее погашение
# предыдущего, прежде чем открывать новую позицию по этому активу.
SKIP_MARKETS_AFTER_TRADE = int(os.environ.get("SKIP_MARKETS_AFTER_TRADE", "1"))

MIN_ORDER_VALUE = float(os.environ.get("MIN_ORDER_VALUE", "1.05"))
STOP_BEFORE_SEC = float(os.environ.get("STOP_BEFORE_SEC", "0.3"))
FAK_ATTEMPTS    = int(os.environ.get("FAK_ATTEMPTS", "3"))
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
log = logging.getLogger("skew_momentum_bot")
log.setLevel(logging.INFO)
log.propagate = False
log.addHandler(_console_handler)
_fh = logging.FileHandler("skew_momentum_bot_trades.log", encoding="utf-8")
_fh.setFormatter(MillisecondFormatter("%(asctime)s | %(levelname)s | %(message)s"))
log.addHandler(_fh)

SIGNAL_LOG_PATH = "skew_momentum_bot_signal_log.jsonl"

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
                return float(p) if p is not None else None
    except Exception as e:
        log.debug("get_best_ask_rest(%s): %s", token_id, e)
    return None


# ===========================================================================
# WEBSOCKET-МОНИТОРИНГ ЦЕНЫ (замена REST-поллинга)
# ---------------------------------------------------------------------------
# ВАЖНО про формат: подписка {"assets_ids": [...], "type": "market"} и поля
# event_type/best_ask/best_bid/ask_prices/bid_prices/pc_price — это
# предположение по стандартному Polymarket CLOB WS market-каналу, тому же,
# что мы видели в наших собственных L2-логах (там ровно эти поля были
# записаны напрямую с этого фида). Если формат в проде окажется другим —
# в логе будет явное предупреждение "не смог разобрать сообщение", а не
# тихий сбой — и бот при этом НЕ ослепнет: см. WS_STALE_FALLBACK_SEC ниже.
# ===========================================================================
_raw_tick_buffer: list[str] = []           # в памяти — append сюда мгновенный, не блокирует WS-цикл
RAW_TICK_FLUSH_INTERVAL_SEC = 1.5           # как часто фоновая задача пишет буфер на диск

def _raw_tick_log(token_id: str, ask: float | None, bid: float | None):
    """Вызывается на КАЖДОЕ обновление книги из WS. Только append в список
    в памяти — никакого диска, никакой блокировки event loop'а. Полностью
    изолировано от торговой логики: любая ошибка тут молча проглатывается."""
    if not LOG_RAW_TICKS:
        return
    try:
        _raw_tick_buffer.append(json.dumps({
            "ts": time.time(), "token_id": token_id, "ask": ask, "bid": bid,
        }))
    except Exception:
        pass  # намеренно тихо — сбор данных не должен мешать торговле


def _raw_tick_write_batch(lines: list[str]):
    """Блокирующая часть (открыть/дописать/закрыть файл) — выполняется в
    отдельном потоке через asyncio.to_thread, никогда в основном event loop'е."""
    try:
        with open(RAW_TICKS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        log.debug("raw_tick_write_batch: %s", e)


async def raw_tick_flusher():
    """Фоновая задача: раз в RAW_TICK_FLUSH_INTERVAL_SEC забирает всё, что
    накопилось в буфере, и пишет одним куском на диск в отдельном потоке.
    Если LOG_RAW_TICKS=false — просто ничего не делает, никаких накладных
    расходов вообще."""
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
    """Живой кэш best_ask/best_bid по token_id, обновляется WS-пушами."""
    def __init__(self):
        self.best_ask: dict[str, float] = {}
        self.best_bid: dict[str, float] = {}
        self.updated_at: dict[str, float] = {}
        self.last_any_message_ts: float = 0.0
        self.connected: bool = False

    def update(self, token_id: str, ask: float | None, bid: float | None):
        now = time.time()
        if ask is not None:
            self.best_ask[token_id] = ask
        if bid is not None:
            self.best_bid[token_id] = bid
        self.updated_at[token_id] = now
        self.last_any_message_ts = now

    def get_ask(self, token_id: str) -> float | None:
        ts = self.updated_at.get(token_id)
        if ts is None or (time.time() - ts) > WS_STALE_FALLBACK_SEC:
            return None  # устарело/нет данных — вызывающий код уйдёт на REST
        return self.best_ask.get(token_id)


book_cache = BookCache()
_ws_wanted_tokens: set[str] = set()   # какие токены сейчас нужны мониторингу
_ws_subscribed_tokens: set[str] = set()  # что реально запрошено у текущего соединения
_ws_resubscribe_event = asyncio.Event()


def ws_want_tokens(token_ids: list[str]):
    """Регистрирует токены как нужные для мониторинга — вызывается из
    monitor_market при старте нового рынка."""
    changed = False
    for t in token_ids:
        if t not in _ws_wanted_tokens:
            _ws_wanted_tokens.add(t)
            changed = True
    if changed:
        _ws_resubscribe_event.set()


def ws_forget_tokens(token_ids: list[str]):
    """Убирает токены из подписки, когда рынок закрылся — иначе список
    подписки бесконечно растёт за долгую сессию (новый рынок каждые 5 мин)."""
    changed = False
    for t in token_ids:
        if t in _ws_wanted_tokens:
            _ws_wanted_tokens.discard(t)
            changed = True
        book_cache.best_ask.pop(t, None)
        book_cache.best_bid.pop(t, None)
        book_cache.updated_at.pop(t, None)
    if changed:
        _ws_resubscribe_event.set()


def _parse_book_message(ev: dict) -> tuple[str | None, float | None, float | None]:
    """Достаёт (token_id, best_ask, best_bid) из одного WS-события, с
    запасными путями на случай разных вариантов структуры полей."""
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
        return
    try:
        await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
        _ws_subscribed_tokens.clear()
        _ws_subscribed_tokens.update(tokens)
        log.info("WS: подписка отправлена на %d токенов", len(tokens))
    except Exception as e:
        log.warning("WS: не удалось отправить подписку: %s", e)


async def _ws_pinger(ws):
    """Некоторые WS-серверы Polymarket требуют периодических пингов, иначе
    рвут соединение — держим keepalive сами, не полагаясь только на
    встроенный ping websockets-библиотеки."""
    try:
        while True:
            await asyncio.sleep(WS_PING_INTERVAL_SEC)
            await ws.send(json.dumps({"type": "ping"}))
    except (asyncio.CancelledError, Exception):
        return


async def ws_market_listener():
    """Фоновая задача на весь процесс: держит одно WS-соединение живым,
    переподписывается на нужные токены по мере смены рынков, переподключается
    с экспоненциальной задержкой при обрыве, и явно детектирует "тихую
    смерть" (соединение формально открыто, но давно нет сообщений) —
    именно такой сценарий мы уже ловили на Binance WS в другом боте."""
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
                log.info("WS подключён (%s)", WS_MARKET_URL)
                backoff = WS_RECONNECT_BASE_SEC
                await _ws_send_subscription(ws)
                ping_task = asyncio.create_task(_ws_pinger(ws))
                _ws_resubscribe_event.clear()

                while True:
                    if _ws_resubscribe_event.is_set():
                        _ws_resubscribe_event.clear()
                        await _ws_send_subscription(ws)

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=WS_SILENT_TIMEOUT_SEC)
                    except asyncio.TimeoutError:
                        log.warning("WS: ни одного сообщения за %.0fс — соединение считаю мёртвым, "
                                    "переподключаюсь", WS_SILENT_TIMEOUT_SEC)
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
                            _raw_tick_log(token_id, ask, bid)
        except Exception as e:
            log.warning("WS соединение упало (%s) — переподключаюсь через %.1fс", e, backoff)
        finally:
            book_cache.connected = False
            if ping_task:
                ping_task.cancel()
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, WS_RECONNECT_MAX_SEC)


async def get_best_ask(session: aiohttp.ClientSession, token_id: str) -> float | None:
    """Единая точка получения ask: сперва живой WS-кэш, при устаревших/
    отсутствующих данных — прозрачный откат на REST. Вызывающему коду
    ничего знать про WS не нужно, интерфейс тот же, что был раньше."""
    if USE_WS_MONITORING and HAVE_WEBSOCKETS:
        cached = book_cache.get_ask(token_id)
        if cached is not None:
            return cached
    return await get_best_ask_rest(session, token_id)


# ===========================================================================
# CLOB — исполнение (проверенный код из предыдущих ботов серии)
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
    """Возвращает (filled, oid, ambiguous)."""
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


async def buy_clip(session, client, token_id: str, label: str, ask_now: float, size: float) -> tuple[float, float]:
    """FAK-покупка клипа с парой попыток на случай проскальзывания.
    При неоднозначном исходе (таймаут/500) проверяет реальный статус
    ордера через get_order_status, прежде чем слать новый — иначе, как
    мы уже находили в трёх других ботах, можно задвоить покупку, если
    первый ордер на самом деле исполнился, просто ответ не дошёл.
    Возвращает (filled, price) — цену тоже, чтобы стоимость считалась по
    РЕАЛЬНОЙ цене исполнения (с буфером), а не по сырому ask на момент
    сигнала — раньше paper-режим полностью игнорировал буфер в учёте."""
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
                    log.warning("[%s] попытка %d неоднозначная и БЕЗ orderID — не могу проверить исход, "
                                "проверьте позицию вручную", label, attempt)
        else:
            log.info("PAPER [%s] клип price=%.3f size=%.1f", label, price, size)
            return size, price
        await asyncio.sleep(FAK_INTERVAL_SEC)
        fresh = await get_best_ask(session, token_id)
        if fresh is not None:
            price = min(fresh + BUY_PRICE_BUFFER, MAX_CLIP_PRICE + BUY_PRICE_BUFFER)
    return 0.0, price


def clip_size_for_trigger(n_prior_clips: int) -> float:
    """Размер клипа с учётом роста по порядку срабатывания (не по стороне,
    не по исходу — n_prior_clips это n_clips ДО текущей покупки, т.е. 0 для
    первого клипа на рынке, 1 для второго, и т.д., независимо от стороны).

    Округление: приводим к ЦЕЛОМУ числу акций сразу здесь — Polymarket не
    принимает дробные акции, и это на всякий случай защищает от плавающих
    артефактов (типа 13.310000000000002) в логах и промежуточных расчётах.
    Дальше place_order() ВСЁ РАВНО сделает свою финальную проверку (округлит
    вверх до целого и досчитает до MIN_ORDER_VALUE, если нужно) — это не
    заменяет ту логику, а просто подаёт на вход уже чистое, целое значение."""
    raw = CLIP_SIZE * ((1.0 + CLIP_SIZE_GROWTH) ** n_prior_clips)
    return float(round(raw))


class AskHistory:
    """Короткая история ask для оценки момента (рост за последние N секунд)."""
    __slots__ = ("samples",)
    def __init__(self):
        self.samples: deque = deque()

    def push(self, ts: float, ask: float):
        self.samples.append((ts, ask))
        cutoff = ts - MOMENTUM_WINDOW_SEC - 2.0
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def _past_ask(self, now: float) -> float | None:
        """ask(now - MOMENTUM_WINDOW_SEC) — базовая точка окна.
        ВАЖНО: если в буфере нет ни одного сэмпла реально старше окна
        (например сторона долго молчала — WS шлёт обновления только на
        реальное изменение цены — и вдруг скакнула), раньше здесь тихо
        подставлялся САМЫЙ СТАРЫЙ сэмпл во всём буфере, каким бы старым
        он ни был, а рост всё равно подписывался как "за WINDOW секунд".
        Это давало ложные, завышенные значения роста (найдено по логу:
        подряд два '+0.27 за 5с' и '+0.28 за 5с', хотя реальный интервал
        между этими точками был намного больше — сторона просто долго не
        обновлялась). Теперь при отсутствии честной точки на границе окна
        возвращаем None — сигнал в этот момент просто не засчитывается,
        вместо того чтобы врать о длине интервала."""
        if len(self.samples) < 2:
            return None
        target_ts = now - MOMENTUM_WINDOW_SEC
        oldest_ts = self.samples[0][0]
        if oldest_ts > target_ts:
            # в буфере вообще нет данных настолько старых — окно ещё не накопилось
            return None
        past_ask = None
        for ts, ask in self.samples:
            if ts <= target_ts:
                past_ask = ask
            else:
                break
        return past_ask

    def rise_pct_over_window(self, now: float) -> float | None:
        """
        Рост ask за окно в ПРОЦЕНТАХ (долях): (ask(now) - ask(now-WINDOW)) / ask(now-WINDOW).
        Порог сравнения — единый MOMENTUM_MIN_RISE_PCT по всему диапазону
        цен, без ранжирования по зонам (раньше была абс. разница + порог,
        растущий к краям 0.5→0/1 через dynamic_min_rise_abs). Вся остальная
        логика поиска базовой точки окна (_past_ask) не изменилась.
        """
        past_ask = self._past_ask(now)
        if past_ask is None or past_ask <= 0:
            return None
        now_ask = self.samples[-1][1]
        return (now_ask - past_ask) / past_ask


# ===========================================================================
# МОНИТОР РЫНКА
# ===========================================================================
async def monitor_market(session, client, market: dict, asset: str):
    token_up, token_down, q = market["token_up"], market["token_down"], market["question"]
    deadline = market["slot"] + SEG

    log.info("[%s/5m] МОНИТОРИНГ: %s", asset.upper(), q)
    if USE_WS_MONITORING:
        ws_want_tokens([token_up, token_down])

    hist_up = AskHistory()
    hist_down = AskHistory()
    q_up = 0.0; q_down = 0.0
    cost_up = 0.0; cost_down = 0.0
    n_clips = 0
    last_clip_ts = 0.0
    last_clip_price = {"UP": None, "DOWN": None}  # цена своей же последней покупки на каждой стороне

    while True:
        remaining = deadline - time.time()
        if remaining <= STOP_BEFORE_SEC:
            break
        # Раньше было 5.0с — блокировало реакцию именно на резкий разворот
        # в последние секунды (нашли конкретный случай: фаворит всё окно был
        # Up, реально победил Down, но бот не мог докупить Down — гейт не
        # пускал). Стратегия держит до резолва (не продаёт заранее), так что
        # причины "нужно время на управление позицией" тут нет — снижено
        # почти до STOP_BEFORE_SEC.
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

        if n_clips < MAX_CLIPS_PER_MARKET and (now - last_clip_ts) >= CLIP_INTERVAL_SEC:
            rise_up = hist_up.rise_pct_over_window(now)
            rise_down = hist_down.rise_pct_over_window(now)

            # Докупка той же стороны разрешена, только если цена ВЫШЕ своей же
            # последней покупки на ней (это и есть реальные 66.8% из данных
            # трейдера) — без этого сигнал может стрелять повторно на плоской
            # цене, пока старая точка сравнения в скользящем окне ещё ниже
            # текущей, хотя движения по факту уже нет (баг, который мы поймали
            # в логе: два клипа BTC UP подряд по одной и той же цене 0.890).
            def _fresh(side, ask_now):
                lp = last_clip_price[side]
                return lp is None or ask_now > lp + 1e-9

            candidate = None
            if (rise_up is not None and ask_up is not None
                    and rise_up >= MOMENTUM_MIN_RISE_PCT
                    and ask_up <= MAX_CLIP_PRICE and _fresh("UP", ask_up)):
                candidate = ("UP", token_up, ask_up, rise_up)
            if (rise_down is not None and ask_down is not None
                    and rise_down >= MOMENTUM_MIN_RISE_PCT
                    and ask_down <= MAX_CLIP_PRICE and _fresh("DOWN", ask_down)):
                if candidate is None or rise_down > candidate[3]:
                    candidate = ("DOWN", token_down, ask_down, rise_down)

            # Потолок на итоговый перекос — единственное, что мы нашли, что
            # реально предотвратило бы конкретную катастрофу (рынок
            # btc-updown-5m-1785572100: перекос 13x, разворот в конце,
            # -$42.70 — почти весь убыток сессии с одного рынка). У
            # трейдера за 577 рынков максимум КОГДА-ЛИБО был 10x, и то это
            # редкий выброс (96.7% рынков ниже 5x). Порог здесь взят с
            # запасом (8x) специально, чтобы НИКОГДА не мешать нормальному
            # разгону перекоса — блокирует только докупку УЖЕ доминирующей
            # стороны, если это превысило бы потолок; докупку слабой
            # стороны (снижающую перекос) не трогает вообще.
            if candidate is not None:
                side_label = candidate[0]
                cur_larger = "UP" if q_up >= q_down else "DOWN"
                if side_label == cur_larger and min(q_up, q_down) > 0:
                    prospective_larger = max(q_up, q_down) + CLIP_SIZE
                    prospective_skew = prospective_larger / min(q_up, q_down)
                    if prospective_skew > MAX_SKEW_RATIO:
                        log.info("[%s/5m MOM_%s] клип пропущен: перекос уже %.1fx, докупка довела бы "
                                 "до %.1fx (> потолка %.1fx)", asset.upper(), side_label,
                                 max(q_up, q_down)/min(q_up, q_down), prospective_skew, MAX_SKEW_RATIO)
                        candidate = None

            if candidate is not None:
                side_label, token_id, ask_now, rise = candidate
                label = f"{asset.upper()}/5m MOM_{side_label}"
                log.warning("[%s] моментум: ask вырос на %.1f%% за %.0fс (ask=%.3f, порог=%.1f%%) — докупаем клип",
                            label, rise * 100.0, MOMENTUM_WINDOW_SEC, ask_now, MOMENTUM_MIN_RISE_PCT * 100.0)
                requested_size = clip_size_for_trigger(n_clips)
                filled, exec_price = await buy_clip(session, client, token_id, label, ask_now, requested_size)
                if filled > 0:
                    n_clips += 1
                    last_clip_ts = now
                    last_clip_price[side_label] = ask_now
                    if side_label == "UP":
                        q_up += filled; cost_up += filled * exec_price
                    else:
                        q_down += filled; cost_down += filled * exec_price
                    log_signal({
                        "event": "clip", "asset": asset.upper(), "slug": market["slug"],
                        "side": side_label, "price": exec_price, "signal_ask": ask_now,
                        "size": filled, "rise": rise,
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
    # держим до резолва — редим не отправляем автоматически (можно сделать
    # отдельным периодическим claim-процессом, как и у трейдера-оригинала)
    if USE_WS_MONITORING:
        ws_forget_tokens([token_up, token_down])


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
    next_eligible_slot: dict[str, int] = {}   # asset -> раньше этого слота не входим
    last_logged_skip: dict[str, int] = {}     # чтобы не спамить лог каждые 2с на одном и том же пропуске

    if USE_WS_MONITORING:
        if HAVE_WEBSOCKETS:
            asyncio.create_task(ws_market_listener())
            log.info("WS-мониторинг включён (USE_WS_MONITORING=true)")
        else:
            log.error("USE_WS_MONITORING=true, но 'websockets' не установлен "
                      "(pip install websockets) — работаю на REST-поллинге")

    if LOG_RAW_TICKS:
        asyncio.create_task(raw_tick_flusher())

    async with aiohttp.ClientSession() as session:
        last_ws_health_log = 0.0
        while True:
            if USE_WS_MONITORING and HAVE_WEBSOCKETS and time.time() - last_ws_health_log >= 30.0:
                last_ws_health_log = time.time()
                silent_for = time.time() - book_cache.last_any_message_ts if book_cache.last_any_message_ts else None
                log.info("WS статус: connected=%s | подписано токенов=%d | тишина=%s",
                         book_cache.connected, len(_ws_subscribed_tokens),
                         f"{silent_for:.1f}с" if silent_for is not None else "н/д")

            for asset in ASSETS:
                slot = int(time.time() // SEG) * SEG
                key = (asset, slot)
                if key in active_tasks and not active_tasks[key].done():
                    continue
                if slot < next_eligible_slot.get(asset, 0):
                    if last_logged_skip.get(asset) != slot:
                        last_logged_skip[asset] = slot
                        log.info("[%s/5m] окно %d сознательно пропущено — даём время на "
                                 "погашение предыдущего рынка перед новым входом", asset.upper(), slot)
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
    log.info("=== skew_momentum_bot.py СТАРТ ===")
    log.info("Активы: %s (только BTC/5m валидировано) | paper=%s", ASSETS, not EXECUTION_ENABLED)
    log.info("Реконструкция 0xAAAAA: клип=%.0f акций каждые >=%.1fс, момент-окно=%.1fс, "
             "мин.рост(%%, без зон по цене)=%.1f%%, потолок цены=%.2f",
             CLIP_SIZE, CLIP_INTERVAL_SEC, MOMENTUM_WINDOW_SEC, MOMENTUM_MIN_RISE_PCT * 100.0, MAX_CLIP_PRICE)
    log.info("Торговля через рынок: после каждого отработанного окна пропускаем %d следующих подряд "
             "(нет автопогашения — даём время на ручной redeem)", SKIP_MARKETS_AFTER_TRADE)
    if any(a != "btc" for a in ASSETS):
        log.warning("ВНИМАНИЕ: паттерн реконструирован ТОЛЬКО по данным BTC/5m трейдера — "
                     "другие активы в конфиге не подтверждены его историей.")
    await scheduler()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено по Ctrl+C")
