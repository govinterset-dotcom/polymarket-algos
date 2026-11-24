"""
Polymarket BTC Up/Down Arbitrage Bot
=====================================
Стратегия:
  1. Мониторинг: находим 15min рынок в 3-й пятиминутке + соответствующий 5min рынок
  2. Проверка: |price_to_beat_15m - price_to_beat_5m| <= PRICE_BEAT_TOLERANCE (5$)
  3. Сканирование стакана: если спред между одним исходом > ARB_SPREAD_ENTRY (3¢)
  4. Исполнение: покупаем дешёвый исход в одном рынке + хеджируем в другом (FOK→FOK→FAK)
  5. Докупка: при расширении спреда каждые ARB_SPREAD_STEP (3¢) — до MAX_LEVELS (4) уровней
  6. Позиции уходят в резолв автоматически, бот не закрывает их вручную

Математика хеджа:
  Позиция A: купили Up_5m за price_A → получим size_A/price_A контрактов
  Позиция B: купили Down_15m за price_B → нужно size_B = size_A * price_B / price_A
  При любом исходе: payout = size_A/price_A * 1.0 = size_B/price_B * 1.0
  Прибыль = size_A/price_A - size_A - size_B = size_A * (1 - price_A - price_B) / price_A

Установка:
  pip install requests websocket-client python-dotenv py-clob-client

.env:
  PRIVATE_KEY=0x...
  FUNDER=0x...
  SIGNATURE_TYPE=1
"""

import os
import time
import json
import threading
import math
from dataclasses import dataclass, field
from typing import Optional, Tuple, List, Dict
from collections import deque
from enum import Enum

import requests
import websocket
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderType
from py_clob_client.order_builder.constants import BUY
from py_clob_client.constants import POLYGON

try:
    from py_clob_client.clob_types import OrderArgs
except ImportError:
    try:
        from py_clob_client.clob_types import LimitOrderArgs as OrderArgs
    except ImportError:
        from dataclasses import dataclass as _dc
        @_dc
        class OrderArgs:
            token_id: str
            price: float
            size: float
            side: str

load_dotenv()

# ========================= CONFIG =========================

HOST           = os.getenv("HOST", "https://clob.polymarket.com").strip()
GAMMA_BASE     = os.getenv("GAMMA_BASE", "https://gamma-api.polymarket.com").strip()
PRIVATE_KEY    = os.getenv("PRIVATE_KEY", "").strip()
FUNDER         = os.getenv("FUNDER", "").strip()
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))

# Порог разницы "price to beat" между 15min и 5min рынком (в долларах BTC)
PRICE_BEAT_TOLERANCE = float(os.getenv("PRICE_BEAT_TOLERANCE", "5.0"))

# Минимальный арбитражный спред для входа (в центах, как доля: 0.03 = 3¢)
ARB_SPREAD_ENTRY = float(os.getenv("ARB_SPREAD_ENTRY", "0.03"))

# Шаг докупки (расширение спреда на X¢ → добавляем уровень)
ARB_SPREAD_STEP  = float(os.getenv("ARB_SPREAD_STEP", "0.03"))

# Максимум уровней докупки
MAX_LEVELS = int(os.getenv("MAX_LEVELS", "4"))

# Лимит USDC на одно окно (все уровни вместе)
WINDOW_BUDGET_USD = float(os.getenv("WINDOW_BUDGET_USD", "100.0"))

# Базовый размер одного уровня = WINDOW_BUDGET_USD / MAX_LEVELS
# Можно переопределить через env (тогда лимит будет BASE_LEVEL_USD * MAX_LEVELS)
BASE_LEVEL_USD = float(os.getenv(
    "BASE_LEVEL_USD",
    str(round(WINDOW_BUDGET_USD / MAX_LEVELS, 2))
))

# Попытки FOK перед переходом к FAK
FOK_ATTEMPTS = int(os.getenv("FOK_ATTEMPTS", "2"))

# Агрессивный сдвиг цены для FAK (добавляем к ask, чтобы гарантировать исполнение)
FAK_PRICE_SLIPPAGE = float(os.getenv("FAK_PRICE_SLIPPAGE", "0.02"))

# Таймаут ожидания FAK (секунды)
FAK_WAIT_SEC = float(os.getenv("FAK_WAIT_SEC", "8.0"))

# Максимально допустимая открытая направленная позиция (USDC)
# Если recovery не сработал и осталась незахеджированная нога — бот стопается
MAX_NAKED_USD = float(os.getenv("MAX_NAKED_USD", "30.0"))

# Интервал поллинга стакана (секунды)
SCAN_INTERVAL_SEC = float(os.getenv("SCAN_INTERVAL_SEC", "2.0"))

# Пауза между HTTP запросами к API
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.3"))

# Stale-порог Chainlink (секунды)
CHAINLINK_STALE_SEC = float(os.getenv("CHAINLINK_STALE_SEC", "6.0"))

# Debug
DEBUG = os.getenv("DEBUG", "false").strip().lower() == "true"

# ========================= VALIDATION =========================

if not PRIVATE_KEY:
    raise RuntimeError("PRIVATE_KEY is empty")
if not FUNDER:
    raise RuntimeError("FUNDER is empty")

print("=" * 60)
print("Polymarket BTC Arbitrage Bot")
print("=" * 60)
print(f"HOST={HOST}")
print(f"PRICE_BEAT_TOLERANCE=${PRICE_BEAT_TOLERANCE}")
print(f"ARB_SPREAD_ENTRY={ARB_SPREAD_ENTRY*100:.1f}¢")
print(f"ARB_SPREAD_STEP={ARB_SPREAD_STEP*100:.1f}¢")
print(f"MAX_LEVELS={MAX_LEVELS}")
print(f"WINDOW_BUDGET_USD=${WINDOW_BUDGET_USD}")
print(f"BASE_LEVEL_USD=${BASE_LEVEL_USD}")
print(f"MAX_NAKED_USD=${MAX_NAKED_USD}")
print(f"FOK_ATTEMPTS={FOK_ATTEMPTS}")
print("=" * 60)

# ========================= HTTP + CLIENT =========================

http = requests.Session()
http.headers.update({"User-Agent": "polymarket-arb-bot/2.0"})

client = ClobClient(
    host=HOST,
    key=PRIVATE_KEY,
    chain_id=POLYGON,
    signature_type=SIGNATURE_TYPE,
    funder=FUNDER,
)
api_creds = client.create_or_derive_api_creds()
client.set_api_creds(api_creds)
print("✅ API creds installed")

# ========================= CHAINLINK FEED =========================

chainlink_lock     = threading.Lock()
chainlink_price: Optional[float] = None
chainlink_price_ts: float = 0.0
price_buffer_lock  = threading.Lock()
price_buffer       = deque(maxlen=10000)


def normalize_ts(raw) -> float:
    try:
        ts = float(raw)
        return ts / 1000.0 if ts > 1e12 else ts
    except Exception:
        return time.time()


def set_chainlink_price(price: float, raw_ts=None):
    global chainlink_price, chainlink_price_ts
    src_ts = normalize_ts(raw_ts)
    with chainlink_lock:
        chainlink_price    = price
        chainlink_price_ts = time.time()
    with price_buffer_lock:
        price_buffer.append((src_ts, price))
    if DEBUG:
        print(f"🔗 CL BTC/USD={price:.2f}")


def get_chainlink_price() -> Optional[float]:
    with chainlink_lock:
        p, ts = chainlink_price, chainlink_price_ts
    if p is None or time.time() - ts > CHAINLINK_STALE_SEC:
        return None
    return p


def chainlink_ws_loop():
    endpoint = "wss://ws-live-data.polymarket.com"

    def on_open(ws):
        print("🔗 Chainlink WS connected")
        ws.send(json.dumps({
            "action": "subscribe",
            "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}],
        }))

    def on_message(ws, message):
        try:
            if not message or str(message).strip().lower() in ("ping", "pong"):
                return
            data = json.loads(message)
            if data.get("topic") != "crypto_prices_chainlink":
                return
            if data.get("type") != "update":
                return
            payload = data.get("payload", {})
            if str(payload.get("symbol", "")).lower() != "btc/usd":
                return
            val = payload.get("value")
            if val is None:
                return
            set_chainlink_price(float(val), payload.get("timestamp"))
        except Exception as e:
            if DEBUG:
                print(f"❌ WS parse: {e}")

    def on_error(ws, err):
        print(f"❌ WS error: {err}")

    def on_close(ws, code, msg):
        print(f"🔌 WS closed: {code} {msg}")

    while True:
        try:
            ws = websocket.WebSocketApp(
                endpoint,
                on_open=on_open, on_message=on_message,
                on_error=on_error, on_close=on_close,
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"❌ WS reconnect: {e}")
        time.sleep(3)

# ========================= HELPERS =========================

def now_ts() -> int:
    return int(time.time())

def floor_15m(ts: int) -> int:
    return (ts // 900) * 900

def floor_5m(ts: int) -> int:
    return (ts // 300) * 300

def clean_token(t) -> str:
    if isinstance(t, dict):
        t = t.get("clobTokenId") or t.get("tokenId") or t.get("id") or str(t)
    return str(t).strip().replace('"', '').replace("'", '').replace('[','').replace(']','').strip()

def _parse_array(raw) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw]
    s = str(raw).strip()
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed]
    except Exception:
        pass
    if ',' in s:
        return [x.strip().strip('"').strip("'") for x in s.split(',')]
    return [s.strip().strip('"').strip("'")]

# ========================= MARKET FETCHING =========================

def get_event(slug: str) -> Optional[dict]:
    """Получить событие по slug через /events."""
    try:
        r = http.get(f"{GAMMA_BASE}/events", params={"slug": slug}, timeout=12)
        if r.status_code != 200:
            return None
        data = r.json()
        if isinstance(data, list) and data:
            return data[0]
    except Exception as e:
        if DEBUG:
            print(f"❌ get_event({slug}): {e}")
    return None


def get_market_from_event(event: dict) -> Optional[dict]:
    markets = event.get("markets", [])
    return markets[0] if markets else None


def extract_tokens(market: dict) -> Tuple[Optional[str], Optional[str]]:
    """Извлечь (up_token, down_token) из рынка."""
    outcomes   = _parse_array(market.get("outcomes"))
    token_ids  = [clean_token(x) for x in _parse_array(market.get("clobTokenIds")) if str(x).strip()]

    if len(outcomes) < 2 or len(token_ids) < 2:
        return None, None

    up_token = down_token = None
    for label, tid in zip(outcomes, token_ids):
        lbl = str(label).strip().lower()
        if lbl == "up":
            up_token = tid
        elif lbl == "down":
            down_token = tid
    return up_token, down_token


def get_orderbook(token_id: str) -> Optional[dict]:
    """Получить текущий стакан по token_id."""
    try:
        time.sleep(REQUEST_DELAY)
        r = http.get(f"{HOST}/book", params={"token_id": token_id}, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        if DEBUG:
            print(f"❌ get_orderbook({token_id[:12]}...): {e}")
    return None


def best_bid_ask(book: Optional[dict]) -> Tuple[Optional[float], Optional[float]]:
    """Вернуть (best_bid, best_ask) из стакана."""
    if not book:
        return None, None
    bids = [float(b["price"]) for b in book.get("bids", []) if b.get("price")]
    asks = [float(a["price"]) for a in book.get("asks", []) if a.get("price")]
    bid = max(bids) if bids else None
    ask = min(asks) if asks else None
    return bid, ask


def available_at_ask(book: Optional[dict], ask_price: float) -> float:
    """Объём доступный на лучшем ask в стакане (в шерах)."""
    if not book:
        return 0.0
    for level in book.get("asks", []):
        try:
            if abs(float(level["price"]) - ask_price) < 0.001:
                return float(level.get("size", 0) or level.get("amount", 0))
        except Exception:
            pass
    return 0.0


def get_start_price(event: dict) -> Optional[float]:
    """
    Извлечь 'price to beat' — стартовую цену BTC для рынка.
    Polymarket хранит её в description: 'greater than X.XX at the end...'
    Если не нашли — используем текущую цену Chainlink.
    """
    import re
    market = get_market_from_event(event)
    if market:
        desc = market.get("description", "") or ""
        # Ищем паттерн вроде "greater than 94500.00" или "above $94,500"
        for pattern in [
            r"greater than\s*\$?([\d,]+(?:\.\d+)?)",
            r"above\s*\$?([\d,]+(?:\.\d+)?)",
            r"exceed\s*\$?([\d,]+(?:\.\d+)?)",
        ]:
            m = re.search(pattern, desc, re.IGNORECASE)
            if m:
                try:
                    price = float(m.group(1).replace(",", ""))
                    if 1000 < price < 10_000_000:
                        return price
                except Exception:
                    pass

    # Fallback: Chainlink
    return get_chainlink_price()

# ========================= ORDER EXECUTION =========================

def _place_order(token_id: str, price: float, size_shares: float,
                 order_type: OrderType, label: str) -> Tuple[bool, float, str]:
    """
    Разместить один ордер.
    Returns: (success, filled_shares, order_id)
    """
    token_id = clean_token(token_id)
    try:
        args   = OrderArgs(token_id=token_id, price=float(price), size=float(size_shares), side=BUY)
        signed = client.create_order(args)
        result = client.post_order(signed, order_type)
        if DEBUG:
            print(f"  📨 {label}: raw result={result}")

        if isinstance(result, dict) and result.get("success"):
            order_id = (result.get("orderID") or result.get("order_id") or
                        result.get("id") or "")
            # Для FOK успех = сразу заполнен
            if order_type == OrderType.FOK:
                print(f"  ✅ {label}: FOK filled | price={price:.3f} | size={size_shares:.4f}")
                return True, size_shares, order_id
            else:
                # FAK — ждём частичного заполнения
                return True, size_shares, order_id
        else:
            reason = result.get("errorMsg", str(result)) if isinstance(result, dict) else str(result)
            print(f"  ⚠️ {label}: order rejected | reason={reason[:120]}")
            return False, 0.0, ""
    except Exception as e:
        print(f"  ❌ {label}: exception: {e}")
        return False, 0.0, ""


def _wait_fak_fill(order_id: str, label: str,
                   wait_sec: float = FAK_WAIT_SEC) -> float:
    """Ждёт заполнения FAK-ордера, возвращает filled_shares."""
    if not order_id:
        return 0.0
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        time.sleep(1.5)
        for method_name in ("get_order", "get_order_by_id"):
            method = getattr(client, method_name, None)
            if not method:
                continue
            try:
                info = method(order_id)
                if not info:
                    continue
                status = str(info.get("status", "")).lower()
                size_matched = float(info.get("size_matched", 0) or 0)
                print(f"  ⏳ {label} FAK status={status} filled={size_matched:.4f}")
                if status in ("matched", "filled", "cancelled", "canceled"):
                    return size_matched
            except Exception:
                pass
    return 0.0


def _cancel_order(order_id: str, label: str):
    for method_name in ("cancel", "cancel_order"):
        method = getattr(client, method_name, None)
        if not method:
            continue
        try:
            method(order_id)
            print(f"  🚫 {label}: cancelled {order_id}")
            return
        except Exception as e:
            if DEBUG:
                print(f"  ⚠️ cancel {method_name}: {e}")


def execute_pair(
    token_cheap:  str,   # токен дешёвого исхода (покупаем)
    price_cheap:  float, # ask цена дешёвого
    token_hedge:  str,   # токен хеджа (противоположный исход, другой рынок)
    price_hedge:  float, # ask цена хеджа
    usd_a:        float, # USDC на ногу A (дешёвый)
    label:        str,
) -> Tuple[float, float, float]:
    """
    Исполнить арбитражную пару: купить дешёвый + захеджировать.

    Математика объёма хеджа:
      shares_A = usd_a / price_cheap
      usd_b    = usd_a * price_hedge / price_cheap
      shares_B = usd_b / price_hedge = usd_a / price_cheap   ← одинаково!

    Оба токена дадут одинаковый payout при победе → позиция нейтральна.

    Returns: (filled_shares_a, filled_shares_b, realized_profit_usdc)
    """
    shares_a = usd_a / price_cheap
    usd_b    = usd_a * price_hedge / price_cheap
    shares_b = usd_b / price_hedge

    print(f"\n🎯 {label}: EXECUTE PAIR")
    print(f"   Leg A (cheap): price={price_cheap:.3f} | shares={shares_a:.4f} | cost=${usd_a:.2f}")
    print(f"   Leg B (hedge): price={price_hedge:.3f} | shares={shares_b:.4f} | cost=${usd_b:.2f}")
    arb_profit = shares_a * 1.0 - usd_a - usd_b
    print(f"   Expected profit: ${arb_profit:.4f} ({arb_profit/(usd_a+usd_b)*100:.2f}%)")

    # ── Шаг 1: Два FOK одновременно (через threads) ──────────────────
    results = {}

    def place_a():
        results["a"] = _place_order(
            token_cheap, price_cheap, shares_a, OrderType.FOK, f"{label}:A(cheap)"
        )

    def place_b():
        results["b"] = _place_order(
            token_hedge, price_hedge, shares_b, OrderType.FOK, f"{label}:B(hedge)"
        )

    for attempt in range(FOK_ATTEMPTS):
        results.clear()
        t_a = threading.Thread(target=place_a)
        t_b = threading.Thread(target=place_b)
        t_a.start(); t_b.start()
        t_a.join();  t_b.join()

        ok_a = results.get("a", (False, 0.0, ""))[0]
        ok_b = results.get("b", (False, 0.0, ""))[0]

        if ok_a and ok_b:
            fa = results["a"][1]
            fb = results["b"][1]
            print(f"✅ {label}: Both FOK filled | A={fa:.4f} B={fb:.4f}")
            return fa, fb, arb_profit

        if not ok_a and not ok_b:
            print(f"⚠️ {label}: Both FOK failed (attempt {attempt+1}/{FOK_ATTEMPTS}) | retry")
            time.sleep(0.5)
            continue

        # Одна нога не прошла — переходим к recovery немедленно
        print(f"⚠️ {label}: FOK partial (A={ok_a} B={ok_b}) → RECOVERY")
        break

    # ── Шаг 2: Recovery — FAK на недостающую сторону ─────────────────
    print(f"🔧 {label}: RECOVERY with FAK")

    ok_a = results.get("a", (False, 0.0, ""))[0]
    ok_b = results.get("b", (False, 0.0, ""))[0]

    # Определяем что нужно добрать
    need_a = not ok_a
    need_b = not ok_b

    fa = results.get("a", (False, 0.0, ""))[1] if ok_a else 0.0
    fb = results.get("b", (False, 0.0, ""))[1] if ok_b else 0.0

    for fak_attempt in range(2):
        if need_a:
            agg_price_a = min(price_cheap + FAK_PRICE_SLIPPAGE, 0.99)
            succ, _, oid = _place_order(
                token_cheap, agg_price_a, shares_a - fa,
                OrderType.GTC, f"{label}:A_FAK_{fak_attempt+1}"
            )
            if succ and oid:
                filled = _wait_fak_fill(oid, f"{label}:A_FAK")
                fa += filled
                if filled >= (shares_a - fa) * 0.95:
                    need_a = False
                else:
                    _cancel_order(oid, f"{label}:A_FAK")

        if need_b:
            agg_price_b = min(price_hedge + FAK_PRICE_SLIPPAGE, 0.99)
            succ, _, oid = _place_order(
                token_hedge, agg_price_b, shares_b - fb,
                OrderType.GTC, f"{label}:B_FAK_{fak_attempt+1}"
            )
            if succ and oid:
                filled = _wait_fak_fill(oid, f"{label}:B_FAK")
                fb += filled
                if filled >= (shares_b - fb) * 0.95:
                    need_b = False
                else:
                    _cancel_order(oid, f"{label}:B_FAK")

        if not need_a and not need_b:
            print(f"✅ {label}: RECOVERY success | A={fa:.4f} B={fb:.4f}")
            return fa, fb, arb_profit

    # ── Осталась незахеджированная нога ──────────────────────────────
    naked_usd = (fa * price_cheap) if need_b else (fb * price_hedge)
    print(f"🚨 {label}: RECOVERY FAILED | naked_usd=${naked_usd:.2f} | need_a={need_a} need_b={need_b}")
    return fa, fb, 0.0

# ========================= STATE =========================

class WindowPhase(Enum):
    IDLE       = "idle"       # ждём нужного момента
    SCANNING   = "scanning"   # мониторим спред
    TRADING    = "trading"    # входим/добираем позиции
    DONE       = "done"       # окно закрыто, ждём следующего

@dataclass
class LevelRecord:
    """Запись об одном уровне докупки."""
    level:          int
    spread_at_entry: float
    shares_a:       float
    shares_b:       float
    usd_spent:      float
    entry_time:     float = field(default_factory=time.time)


@dataclass
class WindowState:
    """Состояние одного арбитражного окна (одна 3-я пятиминутка)."""
    win15_ts:       int           # начало 15min окна
    seg3_ts:        int           # начало 3-й пятиминутки
    seg3_end_ts:    int           # конец 3-й пятиминутки

    slug_15m:       str
    slug_5m:        str

    token_up_15m:   str
    token_dn_15m:   str
    token_up_5m:    str
    token_dn_5m:    str

    price_beat_15m: Optional[float]
    price_beat_5m:  Optional[float]

    phase:          WindowPhase = WindowPhase.SCANNING
    levels:         List[LevelRecord] = field(default_factory=list)
    total_usd_spent: float = 0.0
    naked_usd:      float = 0.0  # незахеджированная позиция (после failed recovery)

    # Направление арба: "up_cheap_5m" или "dn_cheap_5m"
    arb_direction:  Optional[str] = None

    def levels_entered(self) -> int:
        return len(self.levels)

    def next_spread_threshold(self) -> float:
        """Порог спреда для следующего уровня."""
        return ARB_SPREAD_ENTRY + self.levels_entered() * ARB_SPREAD_STEP

    def budget_remaining(self) -> float:
        return max(0.0, WINDOW_BUDGET_USD - self.total_usd_spent)


bot_lock   = threading.Lock()
current_window: Optional[WindowState] = None
naked_total: float = 0.0  # суммарная незахеджированная позиция


# ========================= WINDOW DETECTION =========================

def find_active_arb_window() -> Optional[WindowState]:
    """
    Ищем 15min рынок у которого сейчас идёт 3-я пятиминутка,
    и соответствующий 5min рынок с тем же стартом.
    """
    ts = now_ts()

    # 3-я пятиминутка 15min рынка начинается в момент win15_ts + 600
    # и длится до win15_ts + 900
    # Значит сейчас идёт 3-я пятиминутка если:
    #   win15_ts + 600 <= ts < win15_ts + 900
    # => win15_ts = (ts - 600) // 900 * 900  (ближайший)

    # Пробуем текущий и предыдущий 15min слот
    for offset in [0, -900]:
        candidate_win15 = floor_15m(ts - 600 + offset)
        seg3_start = candidate_win15 + 600
        seg3_end   = candidate_win15 + 900

        if not (seg3_start <= ts < seg3_end):
            continue

        slug15 = f"btc-updown-15m-{candidate_win15}"
        slug5  = f"btc-updown-5m-{seg3_start}"

        time.sleep(REQUEST_DELAY)
        ev15 = get_event(slug15)
        if not ev15:
            if DEBUG:
                print(f"  15m event not found: {slug15}")
            continue

        time.sleep(REQUEST_DELAY)
        ev5 = get_event(slug5)
        if not ev5:
            if DEBUG:
                print(f"  5m event not found: {slug5}")
            continue

        m15 = get_market_from_event(ev15)
        m5  = get_market_from_event(ev5)
        if not m15 or not m5:
            continue

        up15, dn15 = extract_tokens(m15)
        up5,  dn5  = extract_tokens(m5)

        if not all([up15, dn15, up5, dn5]):
            print(f"⚠️ Could not extract tokens for {slug15} or {slug5}")
            continue

        pb15 = get_start_price(ev15)
        pb5  = get_start_price(ev5)

        print(f"🔍 Candidate: {slug15} ↔ {slug5}")
        print(f"   price_beat_15m={pb15} | price_beat_5m={pb5}")

        if pb15 and pb5:
            diff = abs(pb15 - pb5)
            if diff > PRICE_BEAT_TOLERANCE:
                print(f"   ❌ price_beat diff={diff:.2f} > {PRICE_BEAT_TOLERANCE} → skip")
                continue
            print(f"   ✅ price_beat diff={diff:.2f} ≤ {PRICE_BEAT_TOLERANCE} → MATCH")

        return WindowState(
            win15_ts      = candidate_win15,
            seg3_ts       = seg3_start,
            seg3_end_ts   = seg3_end,
            slug_15m      = slug15,
            slug_5m       = slug5,
            token_up_15m  = up15,
            token_dn_15m  = dn15,
            token_up_5m   = up5,
            token_dn_5m   = dn5,
            price_beat_15m = pb15,
            price_beat_5m  = pb5,
        )

    return None

# ========================= SPREAD SCANNER =========================

def scan_spread(win: WindowState) -> Optional[Tuple[str, float, float, float, float, float]]:
    """
    Снимает стаканы всех 4 токенов и ищет арбитражный спред.

    Возвращает (direction, price_cheap, price_hedge, token_cheap, token_hedge, spread)
    или None если спред недостаточен.

    Логика:
      Вариант 1: Up_15m дороже Up_5m
        → купить Up_5m (cheap) + купить Down_15m (hedge)
        spread = bid_up15 - ask_up5

      Вариант 2: Down_15m дороже Down_5m
        → купить Down_5m (cheap) + купить Up_15m (hedge)
        spread = bid_dn15 - ask_dn5
    """
    book_up15 = get_orderbook(win.token_up_15m)
    book_dn15 = get_orderbook(win.token_dn_15m)
    book_up5  = get_orderbook(win.token_up_5m)
    book_dn5  = get_orderbook(win.token_dn_5m)

    bid_up15, ask_up15 = best_bid_ask(book_up15)
    bid_dn15, ask_dn15 = best_bid_ask(book_dn15)
    bid_up5,  ask_up5  = best_bid_ask(book_up5)
    bid_dn5,  ask_dn5  = best_bid_ask(book_dn5)

    if DEBUG:
        print(f"   Books: up15=({bid_up15:.3f}/{ask_up15:.3f}) "
              f"dn15=({bid_dn15:.3f}/{ask_dn15:.3f}) "
              f"up5=({bid_up5:.3f}/{ask_up5:.3f}) "
              f"dn5=({bid_dn5:.3f}/{ask_dn5:.3f})" if all(
                  x is not None for x in [bid_up15, ask_up15, bid_dn15, ask_dn15,
                                           bid_up5, ask_up5, bid_dn5, ask_dn5]
              ) else "   Books: некоторые значения None")

    threshold = win.next_spread_threshold()

    # Вариант 1: Up_15m дороже → Up_5m дёшев
    if all(x is not None for x in [bid_up15, ask_up5, ask_dn15]):
        spread1 = bid_up15 - ask_up5
        if spread1 >= threshold:
            # Направление арба фиксируем при первом входе
            if win.arb_direction is None or win.arb_direction == "up_cheap_5m":
                return ("up_cheap_5m", ask_up5, ask_dn15,
                        win.token_up_5m, win.token_dn_15m, spread1)

    # Вариант 2: Down_15m дороже → Down_5m дёшев
    if all(x is not None for x in [bid_dn15, ask_dn5, ask_up15]):
        spread2 = bid_dn15 - ask_dn5
        if spread2 >= threshold:
            if win.arb_direction is None or win.arb_direction == "dn_cheap_5m":
                return ("dn_cheap_5m", ask_dn5, ask_up15,
                        win.token_dn_5m, win.token_up_15m, spread2)

    return None


# ========================= SAFETY CHECK =========================

def check_naked_limit(win: WindowState) -> bool:
    """Проверяет не превышен ли лимит незахеджированной позиции. True = безопасно."""
    global naked_total
    with bot_lock:
        total = naked_total + win.naked_usd
    if total > MAX_NAKED_USD:
        print(f"🚨 NAKED LIMIT EXCEEDED: ${total:.2f} > ${MAX_NAKED_USD:.2f} | BOT STOPPED")
        return False
    return True

# ========================= MAIN LOOP =========================

def arb_loop():
    global current_window, naked_total

    print("🚀 Arbitrage loop started")

    while True:
        try:
            ts = now_ts()

            with bot_lock:
                win = current_window

            # ── Нет активного окна — ищем ────────────────────────────
            if win is None:
                win = find_active_arb_window()
                if win:
                    with bot_lock:
                        current_window = win
                    print(f"\n✅ New arb window: {win.slug_15m} ↔ {win.slug_5m}")
                    print(f"   Окно 3-й пятиминутки: "
                          f"{time.strftime('%H:%M:%S', time.gmtime(win.seg3_ts))} UTC → "
                          f"{time.strftime('%H:%M:%S', time.gmtime(win.seg3_end_ts))} UTC")
                else:
                    if DEBUG:
                        print(f"  No arb window at {ts}")
                    time.sleep(SCAN_INTERVAL_SEC)
                    continue

            # ── Окно закончилось ─────────────────────────────────────
            if ts >= win.seg3_end_ts:
                print(f"\n⏰ Window expired: {win.slug_15m}")
                print(f"   Levels entered: {win.levels_entered()} | "
                      f"Total spent: ${win.total_usd_spent:.2f}")
                for i, lvl in enumerate(win.levels):
                    print(f"   Level {i+1}: spread={lvl.spread_at_entry*100:.1f}¢ | "
                          f"A={lvl.shares_a:.4f} B={lvl.shares_b:.4f} | ${lvl.usd_spent:.2f}")
                if win.naked_usd > 0:
                    with bot_lock:
                        naked_total += win.naked_usd
                    print(f"   🚨 Naked exposure: ${win.naked_usd:.2f} → total naked=${naked_total:.2f}")
                with bot_lock:
                    current_window = None
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            # ── Проверка лимитов ──────────────────────────────────────
            if not check_naked_limit(win):
                # Критическая ошибка — выходим из бота
                import sys
                sys.exit(1)

            if win.levels_entered() >= MAX_LEVELS:
                if DEBUG:
                    print(f"  Max levels reached ({MAX_LEVELS}) | waiting for window to close")
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            if win.budget_remaining() < BASE_LEVEL_USD * 0.9:
                if DEBUG:
                    print(f"  Budget exhausted: ${win.total_usd_spent:.2f}/${WINDOW_BUDGET_USD}")
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            # ── Сканируем стакан ─────────────────────────────────────
            result = scan_spread(win)

            if result is None:
                if DEBUG:
                    print(f"  Spread below threshold "
                          f"({win.next_spread_threshold()*100:.1f}¢) | scanning...")
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            direction, price_cheap, price_hedge, token_cheap, token_hedge, spread = result

            level_num = win.levels_entered() + 1
            print(f"\n📊 SPREAD DETECTED | level={level_num}/{MAX_LEVELS} | "
                  f"spread={spread*100:.1f}¢ | dir={direction}")

            # Фиксируем направление при первом входе
            if win.arb_direction is None:
                win.arb_direction = direction

            # Исполняем
            usd_a = min(BASE_LEVEL_USD, win.budget_remaining())
            label = f"L{level_num}"

            fa, fb, profit = execute_pair(
                token_cheap=token_cheap,
                price_cheap=price_cheap,
                token_hedge=token_hedge,
                price_hedge=price_hedge,
                usd_a=usd_a,
                label=label,
            )

            usd_a_spent = fa * price_cheap
            usd_b_spent = fb * price_hedge
            total_spent = usd_a_spent + usd_b_spent

            # Проверка незахеджированной позиции
            hedge_ratio = fb / (usd_a / price_cheap) if fa > 0 else 0.0
            if hedge_ratio < 0.8 and fa > 0:
                naked = usd_a_spent * (1 - hedge_ratio)
                win.naked_usd += naked
                print(f"⚠️ Partial hedge: ratio={hedge_ratio:.2f} | naked=${naked:.2f}")

            rec = LevelRecord(
                level=level_num,
                spread_at_entry=spread,
                shares_a=fa,
                shares_b=fb,
                usd_spent=total_spent,
            )
            win.levels.append(rec)
            win.total_usd_spent += total_spent

            print(f"📝 Level {level_num} recorded | "
                  f"A={fa:.4f} B={fb:.4f} | "
                  f"spent=${total_spent:.2f} | "
                  f"window_total=${win.total_usd_spent:.2f}/${WINDOW_BUDGET_USD}")

            # После входа — небольшая пауза перед следующим сканом
            # (ждём расширения спреда на ARB_SPREAD_STEP)
            time.sleep(SCAN_INTERVAL_SEC * 2)

        except Exception as e:
            print(f"❌ arb_loop error: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(5)


# ========================= STATUS PRINTER =========================

def status_loop():
    """Периодически печатает статус бота."""
    while True:
        time.sleep(30)
        try:
            with bot_lock:
                win = current_window
            cl_price = get_chainlink_price()
            ts = now_ts()

            print(f"\n── STATUS {time.strftime('%H:%M:%S', time.gmtime(ts))} UTC ──")
            print(f"   BTC/USD (Chainlink): {cl_price:.2f}" if cl_price else "   BTC/USD: N/A")
            print(f"   Naked total: ${naked_total:.2f}")

            if win:
                remaining = win.seg3_end_ts - ts
                print(f"   Active window: {win.slug_15m}")
                print(f"   Window remaining: {remaining}s")
                print(f"   Levels: {win.levels_entered()}/{MAX_LEVELS} | "
                      f"Spent: ${win.total_usd_spent:.2f}/${WINDOW_BUDGET_USD}")
                print(f"   Direction: {win.arb_direction or 'not set yet'}")
            else:
                print("   No active window")
        except Exception as e:
            print(f"❌ status_loop: {e}")


# ========================= ENTRY POINT =========================

if __name__ == "__main__":
    threads = [
        threading.Thread(target=chainlink_ws_loop, daemon=True, name="chainlink-ws"),
        threading.Thread(target=arb_loop,          daemon=True, name="arb-loop"),
        threading.Thread(target=status_loop,       daemon=True, name="status"),
    ]
    for t in threads:
        t.start()

    print("\n✅ Bot started | waiting for Chainlink feed...")
    print(f"   Budget per window: ${WINDOW_BUDGET_USD} | Levels: {MAX_LEVELS} | "
          f"Base per level: ${BASE_LEVEL_USD}")
    print(f"   Entry spread: {ARB_SPREAD_ENTRY*100:.1f}¢ | "
          f"Step: {ARB_SPREAD_STEP*100:.1f}¢ | "
          f"Max naked: ${MAX_NAKED_USD}")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("\nStopped by user")
