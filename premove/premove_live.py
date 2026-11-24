#!/usr/bin/env python3
"""
premove_live.py — боевой «premove»: рывок книги в конце 5m окна → вход в СЛЕДУЮЩЕЕ 5m окно в ту же сторону (Polymarket up/down).
Сделан на базе comeback.py (CLOB-тейкер FAK, исход по книге, компаундинг, журнал) и ядра premove.py (детектор рывка).
Работает ТОЛЬКО по 5m (15m нет). Отсечки по цене входа (ENTRY_MIN_ASK) нет; остальные фильтры ядра (SIDES, JUMP_MAX, CAP_PX) как были.

Сигнал — один в один как в premove.py (live-рывок):
  • окно-сигнал W (5m), конец E; котировка Up пересобирается из уровней WS-книги Up (лучший бид = max, лучший аск = min;
    пустая сторона → None, без «залипших» значений) — как Feed в premove.py;
  • раз в секунду (в +0.01 с после границы секунды, снимок закончившейся секунды) mid = (bid+ask)/2, только при 0 < спред <= MAX_SPREAD;
  • в последние JUMP_LAST_S (25) секунд окна (секунды E−25 … E−1): |mid(t) − mid(t−k)| >= JUMP_MIN (0.4) для k = 3, затем 5, затем 10 с;
  • рывок вверх (Up-mid вырос) → сторона Up, вниз → Down; один сигнал на окно.
Вход:
  • сразу на сигнале покупаем эту сторону в СЛЕДУЮЩЕМ 5m рынке (старт = E), пока тот ещё не стартовал, по его
    предторговой книге (после старта аск стороны уходит к ~0.60 — преимущество именно в цене до старта);
  • тейкер FAK не дороже min(CAP_PX, аск + MAX_SLIP); повтор, пока аск <= CAP_PX, не дольше FILL_TIMEOUT_S и не
    позже ENTRY_AFTER_START_S секунд после старта целевого окна;
  • отсечки «не входить, если аск ниже X» нет; SIDES и JUMP_MAX (по умолчанию выкл) — как в ядре;
  • держим до конца целевого окна (без выхода), исход — по книге (бид победителя >= BOOK_WIN_PX последние
    BOOK_HOLD_S секунд), gamma — фоновая сверка.
Объём: депозит × STAKE_PCT %, до целых $, не ниже минимума площадки (MIN_SHARES по потолку цены и MIN_USDC).
COMPOUND=true — депозит = DEPOSIT + итог закрытых сделок журнала.
Журнал (premove_live.jsonl): jump (каждый рывок) → skip | signal+take+fill | no_fill → resolved → gamma_check,
плюс jump_result на каждый рывок — для сверки с бумагой. PATH_LOG — посекундный путь книги.
Конфиг: premove_live.env рядом со скриптом (или ENV_FILE). PAPER=true по умолчанию.
Нужны: requests, websockets; для боя — py_clob_client_v2.
"""
from __future__ import annotations
BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"

import asyncio
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import websockets

ENV_FILE_USED = ""


def _load_env_file() -> None:
    global ENV_FILE_USED
    explicit = os.environ.get("ENV_FILE", "").strip()
    here = Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] else Path.cwd()
    stem = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else "premove_live"
    cands = [Path(explicit)] if explicit else [here / f"{stem}.env", here / ".env",
                                               Path.cwd() / f"{stem}.env", Path.cwd() / ".env"]
    for p in cands:
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        for raw in p.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.lower().startswith("export "):
                line = line[7:].strip()
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if v[:1] in ("'", '"') and v[-1:] == v[:1] and len(v) >= 2:
                v = v[1:-1]
            else:
                cut = v.find(" #")
                if cut >= 0:
                    v = v[:cut].rstrip()
                if v.startswith("#"):
                    v = ""
            os.environ[k] = v
        ENV_FILE_USED = str(p)
        return
    if explicit:
        raise SystemExit(f"ENV_FILE={explicit} не найден")


_load_env_file()


def _env(k: str, d: str = "") -> str:
    return os.environ.get(k, d).strip()


def _f(k: str, d: float) -> float:
    v = _env(k)
    return float(v) if v else d


def _i(k: str, d: int) -> int:
    v = _env(k)
    return int(float(v)) if v else d


def _b(k: str, d: bool) -> bool:
    v = _env(k).lower()
    return d if not v else v in ("1", "true", "yes", "on")


def _list_f(k: str, d: str) -> List[float]:
    return [float(x) for x in _env(k, d).replace(" ", "").split(",") if x]


class Cfg:
    PAPER = _b("PAPER", True)
    PK = _env("PK")
    FUNDER = _env("FUNDER")
    SIGNATURE_TYPE = _i("SIGNATURE_TYPE", -1)
    API_KEY = _env("CLOB_API_KEY")
    API_SECRET = _env("CLOB_API_SECRET")
    API_PASSPHRASE = _env("CLOB_API_PASSPHRASE")
    HOST = _env("CLOB_HOST", "https://clob.polymarket.com")
    CHAIN_ID = _i("CHAIN_ID", 137)
    GAMMA = _env("GAMMA_HOST", "https://gamma-api.polymarket.com")
    WS_URL = _env("WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market")

    ASSETS = [a.strip().lower() for a in _env("ASSETS", "btc").split(",") if a.strip()]
    SIG_TF = 5                                # окно-сигнал — только 5m
    TGT_TF = 5                                # цель — следующее 5m окно

    # сигнал (как в premove.py)
    JUMP_MIN = _f("JUMP_MIN", 0.4)            # рывок mid Up за k секунд
    JUMP_LAST_S = _f("JUMP_LAST_S", 25.0)     # ищем рывок в последние N секунд окна-сигнала
    JUMP_KS = [int(x) for x in _env("JUMP_KS", "3,5,10").replace(" ", "").split(",") if x]
    MAX_SPREAD = _f("MAX_SPREAD", 0.05)       # mid считается только при спреде <=
    # необязательные фильтры ядра (по умолчанию ВЫКЛ — отбора нет)
    JUMP_MAX = _f("JUMP_MAX", 0.0)            # пропускать рывок с |jump| >= (0 = выкл)
    SIDES = {x.strip().capitalize() for x in _env("SIDES", "Up,Down").split(",") if x.strip()}

    # исполнение
    LEAD_S = _f("LEAD_S", 200.0)              # за сколько секунд до старта следить за рынком (разогрев книги), как в premove.py
    CAP_PX = _f("CAP_PX", 0.80)               # абсолютный потолок цены тейкера
    MAX_SLIP = _f("MAX_SLIP", 0.03)           # потолок FAK = min(CAP_PX, аск + MAX_SLIP)
    FILL_TIMEOUT_S = _f("FILL_TIMEOUT_S", 8.0)
    ENTRY_AFTER_START_S = _f("ENTRY_AFTER_START_S", 3.0)   # дольше после старта целевого окна не пытаемся
    RETRY_S = _f("RETRY_S", 0.4)
    END_GUARD_S = _f("END_GUARD_S", 2.0)

    # объём
    DEPOSIT = _f("DEPOSIT", 100.0)
    STAKE_PCT = _f("STAKE_PCT", 2.0)
    MIN_SHARES = _f("MIN_SHARES", 5.0)
    MIN_USDC = _f("MIN_USDC", 1.0)
    COMPOUND = _b("COMPOUND", True)
    COMPOUND_FROM = _env("COMPOUND_FROM", "")
    FEE_RATE = _f("FEE_RATE", 0.07)           # комиссия тейкера: rate × p × (1−p) на долю

    # исход
    RESOLVE_BY_BOOK = _b("RESOLVE_BY_BOOK", True)
    BOOK_WIN_PX = _f("BOOK_WIN_PX", 0.99)
    BOOK_HOLD_S = _f("BOOK_HOLD_S", 3.0)
    RESOLVE_DELAY_S = _f("RESOLVE_DELAY_S", 60.0)
    RESOLVE_TIMEOUT_S = _f("RESOLVE_TIMEOUT_S", 8 * 3600.0)
    WS_STALE_S = _f("WS_STALE_S", 30.0)

    PATH_LOG = _env("PATH_LOG", "")           # посекундный путь Up-книги окон (для повторного прогона); пусто = выкл
    JOURNAL = _env("JOURNAL", "premove_live.jsonl")
    LOG_LEVEL = _env("LOG_LEVEL", "INFO")


log = logging.getLogger("premove_live")


def _ts_from(s: str) -> float:
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        pass
    import datetime as _dt
    d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.timestamp()


class Equity:
    """Депозит для объёма: DEPOSIT + P&L (с комиссией) закрытых сделок журнала.
    Ключ — (окно, сторона): resolved задаёт итог, resolved_fix (сверка gamma) перезаписывает."""

    EVENTS = ("resolved", "resolved_fix")

    def __init__(self):
        self.pnl: Dict[tuple, float] = {}
        self.since = _ts_from(Cfg.COMPOUND_FROM)

    def on_rec(self, r: dict) -> None:
        if r.get("event") not in self.EVENTS or r.get("pnl") is None:
            return
        if float(r.get("ts") or time.time()) < self.since:
            return
        self.pnl[(r.get("slug"), r.get("side"))] = float(r["pnl"])

    def load(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            with path.open() as f:
                for line in f:
                    if '"resolved' not in line:
                        continue
                    try:
                        self.on_rec(json.loads(line))
                    except Exception:
                        continue
        except Exception as e:
            log.error("депозит: чтение журнала: %s", e)

    @property
    def realized(self) -> float:
        return sum(self.pnl.values()) if Cfg.COMPOUND else 0.0

    @property
    def value(self) -> float:
        return Cfg.DEPOSIT + self.realized


EQUITY: Optional[Equity] = None


def deposit() -> float:
    return EQUITY.value if EQUITY is not None else Cfg.DEPOSIT


def stake_usdc(cap: Optional[float] = None) -> float:
    """Депозит × STAKE_PCT %, до целых (половина — вверх); не ниже минимума площадки (MIN_SHARES по потолку цены)."""
    cap = Cfg.CAP_PX if cap is None else cap
    calc = int(math.floor(max(0.0, deposit()) * Cfg.STAKE_PCT / 100.0 + 0.5))
    floor = int(math.ceil(max(Cfg.MIN_USDC, Cfg.MIN_SHARES * cap) - 1e-9))
    return float(max(calc, floor))


def book_winner_tail(tail, end: float) -> Optional[str]:
    """tail: (t, бид Up, бид Down). Исход, если на всём отрезке [конец − BOOK_HOLD_S, конец] бид победителя >= BOOK_WIN_PX."""
    q = [x for x in tail if x[0] < end + 1.0]
    if not q or end - q[-1][0] > 20:
        return None
    t0 = end - Cfg.BOOK_HOLD_S
    seg = [x for x in q if x[0] > t0]
    before = [x for x in q if x[0] <= t0]
    if before:
        seg = [before[-1]] + seg
    if not seg:
        return None
    thr = Cfg.BOOK_WIN_PX - 1e-9
    if all(ub is not None and ub >= thr for _, ub, _d in seg):
        return "Up"
    if all(db is not None and db >= thr for _, _u, db in seg):
        return "Down"
    return None


def mid_of(b: Optional[float], a: Optional[float]) -> Optional[float]:
    """mid Up по правилу premove.py: только при 0 < спред <= MAX_SPREAD."""
    if b is None or a is None or not (0 < a - b <= Cfg.MAX_SPREAD + 1e-9):
        return None
    return (a + b) / 2


def detect_jump(mids: List[Optional[float]], el: int) -> Optional[Tuple[float, int]]:
    """Рывок на секунде el окна: первый k из JUMP_KS (3, 5, 10), для которого |mid[el] − mid[el−k]| >= JUMP_MIN.
    Возвращает (d = mid[el] − mid[el−k], k). Логика premove.py → App.live(): сравнение >= JUMP_MIN без допусков."""
    if not (0 <= el < len(mids)) or mids[el] is None:
        return None
    for k in Cfg.JUMP_KS:
        if el - k < 0 or mids[el - k] is None:
            continue
        d = mids[el] - mids[el - k]
        if abs(d) >= Cfg.JUMP_MIN:
            return d, k
    return None


# ════════════════════════════════════════════════════════════════ журнал
class Journal:
    def __init__(self, path: str):
        self.path = Path(path)
        self.lock = asyncio.Lock()

    async def write(self, rec: dict) -> None:
        rec = {"ts": round(time.time(), 3), **rec}
        if EQUITY is not None and rec.get("event") in Equity.EVENTS and self.path == Path(Cfg.JOURNAL):
            EQUITY.on_rec(rec)
        line = json.dumps(rec, ensure_ascii=False, default=str)
        async with self.lock:
            await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        with self.path.open("a") as f:
            f.write(line + "\n")


def spawn(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    t.add_done_callback(_done)
    return t


def _done(t: asyncio.Task) -> None:
    if not t.cancelled() and t.exception():
        log.error("фоновая задача упала: %r", t.exception())


# ════════════════════════════════════════════════════════════════ рынки (gamma)
@dataclass
class Market:
    asset: str
    tf: int
    start: int
    slug: str
    tokens: Dict[str, str]
    tick: str

    @property
    def length(self) -> int:
        return self.tf * 60

    @property
    def end(self) -> int:
        return self.start + self.length


class Gamma:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "premove_live/1.0"

    def _rows(self, slug: str, closed: bool = False) -> list:
        p = {"slug": slug}
        if closed:
            p["closed"] = "true"
        r = self.s.get(f"{Cfg.GAMMA}/markets", params=p, timeout=10)
        r.raise_for_status()
        d = r.json()
        return d if isinstance(d, list) else d.get("data", [])

    def market(self, asset: str, tf: int, start: int) -> Optional[Market]:
        slug = f"{asset}-updown-{tf}m-{start}"
        try:
            rows = self._rows(slug)
            if not rows:
                return None
            row = rows[0]
            ids, outs = row.get("clobTokenIds"), row.get("outcomes")
            ids = json.loads(ids) if isinstance(ids, str) else ids
            outs = json.loads(outs) if isinstance(outs, str) else outs
            tokens = {str(o): str(t) for o, t in zip(outs or [], ids or [])}
            if "Up" not in tokens or "Down" not in tokens:
                return None
            return Market(asset, tf, start, slug, tokens, str(row.get("orderPriceMinTickSize") or "0.01"))
        except Exception as e:
            log.warning("gamma %s: %s", slug, e)
            return None

    def winner(self, slug: str) -> Optional[str]:
        try:
            rows = self._rows(slug, closed=True) or self._rows(slug)
            if not rows:
                return None
            outs, px = rows[0].get("outcomes"), rows[0].get("outcomePrices")
            outs = json.loads(outs) if isinstance(outs, str) else outs
            px = [float(x) for x in (json.loads(px) if isinstance(px, str) else px or [])]
            if not outs or not px or max(px) < 0.99:
                return None
            return str(outs[px.index(max(px))])
        except Exception as e:
            log.debug("резолв %s: %s", slug, e)
            return None


async def _pinger(ws, stop: asyncio.Event, every: float, until: float = 1e18) -> None:
    try:
        while not stop.is_set() and time.time() < until:
            await asyncio.sleep(every)
            await ws.send("PING")
    except Exception:
        pass


def _num(d: dict, *keys) -> Optional[float]:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


# ════════════════════════════════════════════════════════════════ CLOB (тейкер FAK)

# ----------------------------------------------------------------------------- builder (gozi)
# Зашито в коде, из .env не читается: параметры менять можно, атрибуцию ордеров — нет.

class Clob:
    """Покупка FAK через py_clob_client_v2. Ничего не отменяет (FAK не висит в книге)."""

    def __init__(self):
        self.enabled = not Cfg.PAPER
        self.client = None
        self.m = None
        if not self.enabled:
            return
        try:
            import py_clob_client_v2 as m
        except ImportError as e:
            raise SystemExit("нужен пакет py_clob_client_v2: %s" % e)
        if not Cfg.PK:
            raise SystemExit("PK не задан, а PAPER=false")
        self.m = m
        kw: Dict[str, Any] = dict(host=Cfg.HOST, chain_id=Cfg.CHAIN_ID, key=Cfg.PK)
        if Cfg.FUNDER:
            kw["funder"] = Cfg.FUNDER
        if Cfg.SIGNATURE_TYPE >= 0:
            kw["signature_type"] = Cfg.SIGNATURE_TYPE
        kw["builder_config"] = m.BuilderConfig(builder_address=BUILDER_ADDRESS, builder_code=BUILDER_CODE)
        self.client = m.ClobClient(**kw)
        if Cfg.API_KEY and Cfg.API_SECRET and Cfg.API_PASSPHRASE:
            creds = m.ApiCreds(api_key=Cfg.API_KEY, api_secret=Cfg.API_SECRET, api_passphrase=Cfg.API_PASSPHRASE)
        else:
            creds = self.client.create_or_derive_api_key()
        self.client.set_api_creds(creds)
        log.info("CLOB подключён — БОЕВОЙ режим")

    def sign(self, token: str, usdc: float, cap: float, tick: str) -> Any:
        m = self.m
        args = m.MarketOrderArgs(token_id=token, amount=round(usdc, 2), side=m.Side.BUY,
                                 order_type=m.OrderType.FAK, price=round(cap, 6))
        return self.client.create_market_order(args, m.PartialCreateOrderOptions(tick_size=tick))

    def buy(self, token: str, usdc: float, cap: float, tick: str) -> dict:
        """FAK BUY на usdc не дороже cap. {'ok','usdc','shares','unknown','err'}."""
        m = self.m
        try:
            signed = self.sign(token, usdc, cap, tick)
        except Exception as e:
            return {"ok": False, "unknown": False, "err": f"сборка: {e}"[:200]}
        try:
            r = self.client.post_order(signed, m.OrderType.FAK)
        except Exception as e:
            code = getattr(e, "status_code", None)
            return {"ok": False, "unknown": not (isinstance(code, int) and 400 <= code < 500), "err": str(e)[:200]}
        if not isinstance(r, dict):
            return {"ok": False, "unknown": True, "err": f"ответ {r!r}"[:200]}
        if r.get("success") is False:
            return {"ok": False, "unknown": False, "err": str(r.get("errorMsg") or r)[:200]}
        u = _num(r, "makingAmount", "making_amount") or 0.0
        sh = _num(r, "takingAmount", "taking_amount") or 0.0
        return {"ok": sh > 0, "usdc": u, "shares": sh, "status": r.get("status"), "unknown": False,
                "id": r.get("orderID") or r.get("orderId")}


# ════════════════════════════════════════════════════════════════ движок окна
@dataclass
class SideState:
    name: str
    usdc: float = 0.0
    shares: float = 0.0
    fee: float = 0.0


ENG: Dict[str, "Engine"] = {}      # slug → движок (окно-сигнал и/или целевое окно одним объектом)


class Engine:
    """Один 5m рынок. Как окно-сигнал — ищет рывок в последние JUMP_LAST_S секунд и покупает следующий
    5m рынок через ENG[...]. Как целевое окно — держит позицию и считает исход по своей книге."""

    def __init__(self, m: Market, clob: Clob, j: Journal, stats: "Stats"):
        self.m, self.clob, self.j, self.stats = m, clob, j, stats
        self.sides = {"Up": SideState("Up"), "Down": SideState("Down")}
        self.mids: List[Optional[float]] = [None] * m.length
        self.path_b: List[Optional[float]] = [None] * m.length
        self.path_a: List[Optional[float]] = [None] * m.length
        self.up_bid: Optional[float] = None
        self.up_ask: Optional[float] = None
        self.tail: List[Tuple[float, Optional[float], Optional[float]]] = []
        self.levels: Dict[str, Dict[str, Dict[float, float]]] = {t: {"bids": {}, "asks": {}} for t in m.tokens.values()}
        self.sig_done = False          # рывок в этом окне уже найден (один сигнал на окно)
        self.entered = False           # в этот рынок уже заходили (страховка от двойного входа)
        self.signals: List[dict] = []  # рывки окна-источника, нацеленные на этот рынок (для jump_result)

    # --- книга
    def _requote(self) -> None:
        """Котировка Up ровно как Feed._best в premove.py: max по бидам, min по аскам; пустая сторона → None."""
        book = self.levels[self.m.tokens["Up"]]
        self.up_bid = max(book["bids"]) if book["bids"] else None
        self.up_ask = min(book["asks"]) if book["asks"] else None

    def on_levels(self, token: str, side: str, price: float, size: float) -> None:
        book = self.levels.get(token)
        if book is None:
            return
        d = book["bids" if side == "BUY" else "asks"]
        if size <= 0:
            d.pop(price, None)
        else:
            d[price] = size
        if token == self.m.tokens["Up"]:
            self._requote()

    def set_book(self, token: str, bids, asks) -> None:
        book = self.levels.get(token)
        if book is None:
            return
        book["bids"] = {float(x["price"]): float(x["size"]) for x in bids or [] if float(x.get("size", 0) or 0) > 0}
        book["asks"] = {float(x["price"]): float(x["size"]) for x in asks or [] if float(x.get("size", 0) or 0) > 0}
        if token == self.m.tokens["Up"]:
            self._requote()

    def best_bid(self, side: str) -> Optional[float]:
        bids = self.levels[self.m.tokens[side]]["bids"]
        return max(bids) if bids else None

    def snap(self, now: float) -> None:
        """Биды обеих книг в последние 60 с окна — для исхода по книге (аски у победителя часто пустые)."""
        if not (self.m.end - 60 <= now < self.m.end + 1):
            return
        ub, db = self.best_bid("Up"), self.best_bid("Down")
        if ub is None and self.up_bid is not None:
            ub = self.up_bid
        if db is None and self.up_ask is not None:
            db = 1 - self.up_ask
        self.tail.append((now, ub, db))

    def mirror_ask(self, side: str) -> Optional[float]:
        """Аск стороны по зеркалу Up-книги (как tgt_ask_side в premove.py)."""
        if self.up_bid is None or self.up_ask is None:
            return None
        return self.up_ask if side == "Up" else round(1 - self.up_bid, 4)

    def real_ask(self, side: str) -> Optional[float]:
        """Лучший аск токена стороны; нет данных — зеркало."""
        asks = self.levels[self.m.tokens[side]]["asks"]
        if asks:
            return min(asks)
        return self.mirror_ask(side)

    def depth(self, side: str, cap: float) -> float:
        tok = self.m.tokens[side]
        return round(sum(s for p, s in self.levels[tok]["asks"].items() if p <= cap + 1e-9), 2)

    # --- посекундный цикл: снимок книги + поиск рывка
    async def sec_loop(self) -> None:
        m = self.m
        while time.time() < m.end + 0.5:
            now = time.time()
            await asyncio.sleep(max(0.0, math.floor(now) + 1 - now) + 0.01)     # как в premove.py
            try:
                self.sample(math.floor(time.time()))
            except Exception as e:
                log.debug("%s: секундный снимок: %s", m.slug, e)

    def sample(self, boundary: int) -> None:
        m = self.m
        el = boundary - 1 - m.start                      # секунда el = последняя котировка до её конца
        if el < 0 or el >= m.length:
            return
        b, a = self.up_bid, self.up_ask
        if b is None or a is None:
            return
        self.path_b[el], self.path_a[el] = b, a
        self.mids[el] = mid_of(b, a)
        if self.sig_done or el < m.length - Cfg.JUMP_LAST_S - 1e-9:
            return
        hit = detect_jump(self.mids, el)
        if not hit:
            return
        self.sig_done = True
        spawn(self.on_jump(hit[0], hit[1], el))

    async def on_jump(self, d: float, k: int, el: int) -> None:
        """Рывок найден в этом окне → решение и вход в следующий 5m рынок."""
        m = self.m
        side = "Up" if d > 0 else "Down"
        tslug = f"{m.asset}-updown-{Cfg.TGT_TF}m-{m.end}"
        tgt = ENG.get(tslug)
        jr = {"event": "jump", "sig_slug": m.slug, "sig_tf": m.tf, "side": side, "jump": round(d, 4), "k": k,
              "el": el, "rem_s": m.length - el, "mid_now": round(self.mids[el], 4), "mid_prev": round(self.mids[el - k], 4),
              "tgt_slug": tslug}
        if tgt is not None:
            jr.update(tgt_up_bid=tgt.up_bid, tgt_up_ask=tgt.up_ask, tgt_ask_side=tgt.mirror_ask(side),
                      tgt_real_ask=tgt.real_ask(side), depth_cap=tgt.depth(side, Cfg.CAP_PX))
            tgt.signals.append({"sig_slug": m.slug, "side": side, "jump": round(d, 4), "k": k})
        log.warning("РЫВОК %s: %+.2f за %d с, %d с до конца → %s; следующий рынок: аск стороны %s",
                    m.slug, d, k, m.length - el, side, jr.get("tgt_real_ask"))
        await self.j.write(jr)

        def skip(reason: str, **kw) -> None:
            spawn(self.j.write({"event": "skip", "sig_slug": m.slug, "slug": tslug, "side": side, "reason": reason,
                                "jump": round(d, 4), "k": k, **kw}))
            log.info("%s: пропуск (%s)", m.slug, reason)

        if tgt is None:
            skip("no_target")
            return
        if side not in Cfg.SIDES:
            skip("side_off")
            return
        if Cfg.JUMP_MAX > 0 and abs(d) >= Cfg.JUMP_MAX - 1e-9:
            skip("jump_max")
            return
        if tgt.entered:
            skip("dup_target")
            return
        await tgt.enter(side, jr)

    # --- вход в целевой рынок (тейкер FAK по предторговой книге)
    async def enter(self, side: str, src: dict) -> None:
        m = self.m
        s = self.sides[side]
        self.entered = True
        t_sig = time.time()
        deadline = min(t_sig + Cfg.FILL_TIMEOUT_S, m.start + Cfg.ENTRY_AFTER_START_S)
        deadline = max(deadline, t_sig + 1.0)
        tries, logged = 0, False
        info: Optional[dict] = None
        while time.time() < deadline and m.end - time.time() > Cfg.END_GUARD_S:
            ask = self.real_ask(side)
            if ask is None or ask > Cfg.CAP_PX + 1e-9:
                await asyncio.sleep(Cfg.RETRY_S / 4)
                continue
            cap = round(min(Cfg.CAP_PX, ask + Cfg.MAX_SLIP), 4)
            usdc = stake_usdc(cap)
            tries += 1
            info = dict(event="signal", slug=m.slug, tf=m.tf, side=side, sig_slug=src["sig_slug"], jump=src["jump"],
                        k=src["k"], rem_before_start=round(m.start - time.time(), 1), ask=round(ask, 4),
                        mirror_ask=self.mirror_ask(side), cap=cap, usdc=usdc, deposit=round(deposit(), 2),
                        depth=self.depth(side, cap), paper=not self.clob.enabled)
            if not self.clob.enabled:
                px = ask
                sh = usdc / px
                s.usdc, s.shares = usdc, sh
                s.fee = Cfg.FEE_RATE * px * (1 - px) * sh
                await self.j.write(info)
                await self.j.write({"event": "fill", "slug": m.slug, "side": side, "usdc": usdc, "shares": round(sh, 4),
                                    "avg_px": round(px, 4), "ask_sig": src.get("tgt_real_ask"),
                                    "slip": round(px - (src.get("tgt_real_ask") or px), 4), "fee": round(s.fee, 4),
                                    "paper": True, "try": tries})
                log.warning("%s %s: ВХОД premove (бумага) — рывок %+.2f, аск %.3f → $%.0f", m.slug, side, src["jump"], px, usdc)
                return
            r = await asyncio.to_thread(self.clob.buy, m.tokens[side], usdc, cap, m.tick)
            lat = round((time.time() - t_sig) * 1000)
            if not logged:
                await self.j.write(info)
                log.warning("%s %s: ВХОД premove — рывок %+.2f, аск %.3f, потолок %.2f → $%.0f",
                            m.slug, side, src["jump"], ask, cap, usdc)
                logged = True
            await self.j.write({"event": "take", "slug": m.slug, "side": side, "try": tries, "latency_ms": lat, **r})
            if r.get("ok"):
                s.usdc, s.shares = r["usdc"], r["shares"]
                px = s.usdc / s.shares if s.shares else cap
                s.fee = Cfg.FEE_RATE * px * (1 - px) * s.shares
                await self.j.write({"event": "fill", "slug": m.slug, "side": side, "usdc": round(s.usdc, 4),
                                    "shares": round(s.shares, 4), "avg_px": round(px, 4), "ask_sig": src.get("tgt_real_ask"),
                                    "slip": round(px - (src.get("tgt_real_ask") or px), 4), "ask_now": self.real_ask(side),
                                    "fee": round(s.fee, 4), "paper": False, "try": tries, "latency_ms": lat})
                log.warning("%s %s: куплено %.2f долей за $%.2f (ср. %.3f), %d мс", m.slug, side, s.shares, s.usdc, px, lat)
                return
            if r.get("unknown"):
                log.error("%s %s: исход ордера неизвестен (%s) — повтор не делаю", m.slug, side, r.get("err"))
                return
            await asyncio.sleep(Cfg.RETRY_S / 4)
        if info is None:
            await self.j.write({"event": "signal", "slug": m.slug, "tf": m.tf, "side": side, "sig_slug": src["sig_slug"],
                                "jump": src["jump"], "k": src["k"], "ask": self.real_ask(side), "usdc": stake_usdc()})
        await self.j.write({"event": "no_fill", "slug": m.slug, "side": side, "tries": tries, "ask_now": self.real_ask(side)})
        log.info("%s %s: не налилось за %.0f с (аск %s, потолок %.2f)", m.slug, side, Cfg.FILL_TIMEOUT_S,
                 self.real_ask(side), Cfg.CAP_PX)

    # --- итог
    def book_winner(self) -> Optional[str]:
        return book_winner_tail(self.tail, self.m.end)

    async def finish(self, gamma: Gamma) -> None:
        m = self.m
        await asyncio.sleep(max(0.0, m.end + 1 - time.time()))
        pos = [s for s in self.sides.values() if s.shares > 0]
        if not pos and not self.signals:
            await self.write_path(self.book_winner(), "book")
            return
        w, src = (self.book_winner() if Cfg.RESOLVE_BY_BOOK else None), "book"
        if not w:
            src = "gamma"
            if Cfg.RESOLVE_BY_BOOK:
                log.info("%s: книга в последние %.0f с не дала бид >= %.2f — исход жду от gamma",
                         m.slug, Cfg.BOOK_HOLD_S, Cfg.BOOK_WIN_PX)
            await asyncio.sleep(Cfg.RESOLVE_DELAY_S)
            deadline = time.time() + Cfg.RESOLVE_TIMEOUT_S
            while time.time() < deadline and not w:
                w = await asyncio.to_thread(gamma.winner, m.slug)
                if not w:
                    await asyncio.sleep(30)
            if not w:
                await self.j.write({"event": "unresolved", "slug": m.slug})
                log.error("%s: исход не получен", m.slug)
                await self.write_path(None, "none")
                return
        for s in pos:
            await self._settle(s, w, src)
        for sg in self.signals:
            await self.j.write({"event": "jump_result", "slug": m.slug, "sig_slug": sg["sig_slug"], "side": sg["side"],
                                "jump": sg["jump"], "k": sg["k"], "winner": w, "source": src, "won": sg["side"] == w,
                                "traded": self.sides[sg["side"]].shares > 0})
        await self.write_path(w, src)
        if src == "book" and pos:
            spawn(self._verify(gamma, w, pos))

    async def write_path(self, w: Optional[str], src: str) -> None:
        if not Cfg.PATH_LOG:
            return
        b, a = list(self.path_b), list(self.path_a)
        for i in range(1, len(b)):
            if b[i] is None and a[i] is None:
                b[i], a[i] = b[i - 1], a[i - 1]
        await _path_journal().write({"slug": self.m.slug, "asset": self.m.asset, "start": self.m.start, "winner": w,
                                     "source": src, "bid": [None if x is None else round(x, 4) for x in b],
                                     "ask": [None if x is None else round(x, 4) for x in a]})

    async def _settle(self, s: SideState, w: str, src: str, fix: bool = False) -> None:
        won = s.name == w
        pnl = (s.shares if won else 0.0) - s.usdc - s.fee
        if not fix:
            self.stats.add(won, s.usdc, pnl)
        await self.j.write({"event": "resolved_fix" if fix else "resolved", "slug": self.m.slug, "side": s.name,
                            "winner": w, "source": src, "won": won, "usdc": round(s.usdc, 4),
                            "shares": round(s.shares, 4), "fee": round(s.fee, 4), "pnl": round(pnl, 4),
                            "paper": not self.clob.enabled})
        log.warning("%s %s %s: победил %s (%s) → %s %+.2f | %s | депозит $%.2f → ставка $%.0f",
                    "ИСПРАВЛЕНИЕ" if fix else "РЕЗОЛВ", self.m.slug, s.name, w, src, "WIN" if won else "loss", pnl,
                    self.stats.line(), deposit(), stake_usdc())

    async def _verify(self, gamma: Gamma, book_w: str, pos) -> None:
        await asyncio.sleep(Cfg.RESOLVE_DELAY_S)
        deadline = time.time() + Cfg.RESOLVE_TIMEOUT_S
        g = None
        while time.time() < deadline and not g:
            g = await asyncio.to_thread(gamma.winner, self.m.slug)
            if not g:
                await asyncio.sleep(60)
        await self.j.write({"event": "gamma_check", "slug": self.m.slug, "book": book_w, "gamma": g,
                            "match": (g == book_w) if g else None})
        if g and g != book_w:
            for s in pos:
                won_old = s.name == book_w
                pnl_old = (s.shares if won_old else 0.0) - s.usdc - s.fee
                pnl_new = (s.shares if s.name == g else 0.0) - s.usdc - s.fee
                self.stats.fix(pnl_new - pnl_old, s.name == g, won_old)
                await self._settle(s, g, "gamma", fix=True)


class Stats:
    def __init__(self):
        self.n = self.w = 0
        self.usd = self.pnl = 0.0

    def add(self, won, usd, pnl):
        self.n += 1
        self.w += int(bool(won))
        self.usd += usd
        self.pnl += pnl

    def fix(self, dpnl, won_new, won_old):
        self.pnl += dpnl
        self.w += int(won_new) - int(won_old)

    def line(self) -> str:
        wr = self.w / self.n if self.n else 0
        return f"сессия: сделок {self.n}, побед {self.w} ({wr:.0%}), вложено ${self.usd:.2f}, P&L ${self.pnl:+.2f}"


# ════════════════════════════════════════════════════════════════ WS
def _fnum(v) -> Optional[float]:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def handle(raw, eng: Engine, now: float) -> None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")
    if not raw or raw.strip() in ("PING", "PONG"):
        return
    try:
        msg = json.loads(raw)
    except Exception:
        return
    for ev in (msg if isinstance(msg, list) else [msg]):
        if not isinstance(ev, dict):
            continue
        et = ev.get("event_type")
        if et == "book":
            eng.set_book(ev.get("asset_id"), ev.get("bids"), ev.get("asks"))
        elif et == "price_change":
            for ch in ev.get("price_changes") or []:
                p, sz = _fnum(ch.get("price")), _fnum(ch.get("size"))
                if p is not None and sz is not None and ch.get("side"):
                    eng.on_levels(ch.get("asset_id"), str(ch.get("side")).upper(), p, sz)
        elif et == "tick_size_change" and ev.get("new_tick_size"):
            eng.m.tick = str(ev["new_tick_size"])
    eng.snap(now)


_PATH_J: Optional[Journal] = None


def _path_journal() -> Journal:
    global _PATH_J
    if _PATH_J is None:
        _PATH_J = Journal(Cfg.PATH_LOG)
    return _PATH_J


async def watch(eng: Engine, gamma: Gamma, stop: asyncio.Event) -> None:
    m = eng.m
    fin = spawn(eng.finish(gamma))
    spawn(eng.sec_loop())
    toks = list(m.tokens.values())
    log.info("слежу за %s до %s", m.slug, time.strftime("%H:%M:%S", time.gmtime(m.end)))
    while not stop.is_set() and time.time() < m.end:
        try:
            async with websockets.connect(Cfg.WS_URL, ping_interval=None, close_timeout=5, max_size=2 ** 23) as ws:
                await ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
                pinger = spawn(_pinger(ws, stop, 10.0, m.end))
                last = time.time()
                try:
                    while not stop.is_set() and time.time() < m.end:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, min(Cfg.WS_STALE_S, m.end - time.time())))
                        except asyncio.TimeoutError:
                            if time.time() - last > Cfg.WS_STALE_S:
                                log.warning("%s: книга молчит, переподключаюсь", m.slug)
                                break
                            continue
                        last = time.time()
                        handle(raw, eng, last)
                finally:
                    pinger.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("%s WS: %s", m.slug, e)
            await asyncio.sleep(1.0)
    try:
        await fin
    except Exception as e:
        log.error("%s завершение: %s", m.slug, e)


# ════════════════════════════════════════════════════════════════ main
async def main() -> None:
    logging.basicConfig(level=getattr(logging, Cfg.LOG_LEVEL.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    log.info("конфиг: %s", ENV_FILE_USED or "файл не найден — умолчания")
    log.warning("РЕЖИМ: %s", "БУМАЖНЫЙ" if Cfg.PAPER else "БОЕВОЙ — ордера уходят на биржу")
    global EQUITY
    EQUITY = Equity()
    if Cfg.COMPOUND:
        EQUITY.load(Path(Cfg.JOURNAL))
    log.info("%s | окна-сигналы %dm → следующее %dm | рывок mid Up >= %.2f за %s с в последние %.0f с окна",
             Cfg.ASSETS, Cfg.SIG_TF, Cfg.TGT_TF, Cfg.JUMP_MIN, "/".join(str(k) for k in Cfg.JUMP_KS), Cfg.JUMP_LAST_S)
    log.info("вход: тейкер FAK по предторговой книге следующего рынка, потолок min(%.2f, аск + %.2f), стороны %s, "
             "макс. рывок %s; отсечки по цене входа нет", Cfg.CAP_PX, Cfg.MAX_SLIP, ",".join(sorted(Cfg.SIDES)),
             f"< {Cfg.JUMP_MAX:.2f}" if Cfg.JUMP_MAX > 0 else "выкл")
    log.warning("ОБЪЁМ: депозит $%.2f (база $%g%s) × %g%% → ставка $%.0f (минимум по потолку %.2f: $%.0f)",
                deposit(), Cfg.DEPOSIT,
                (f" + итог {EQUITY.realized:+.2f} по {len(EQUITY.pnl)} сделкам журнала") if Cfg.COMPOUND else ", без реинвеста",
                Cfg.STAKE_PCT, stake_usdc(), Cfg.CAP_PX, math.ceil(Cfg.MIN_SHARES * Cfg.CAP_PX))
    log.info("исход по книге: %s", f"бид победителя >= {Cfg.BOOK_WIN_PX:g} все последние {Cfg.BOOK_HOLD_S:g} с, иначе gamma; "
             "gamma — сверка" if Cfg.RESOLVE_BY_BOOK else "выкл — только gamma")
    if Cfg.PATH_LOG:
        log.info("путь книги → %s", Cfg.PATH_LOG)
    stop = asyncio.Event()
    try:
        import signal as _sig
        loop = asyncio.get_running_loop()
        for sg in (_sig.SIGINT, _sig.SIGTERM):
            loop.add_signal_handler(sg, stop.set)
    except Exception:
        pass
    clob, gamma, j, stats = Clob(), Gamma(), Journal(Cfg.JOURNAL), Stats()
    tf = Cfg.SIG_TF
    L = tf * 60
    lead = min(Cfg.LEAD_S, L - 5)
    while not stop.is_set():
        now = time.time()
        cur = int(now // L) * L
        for start in (cur, cur + L):
            if start + L <= now or start > now + lead:
                continue
            for a in Cfg.ASSETS:
                slug = f"{a}-updown-{tf}m-{start}"
                if slug in ENG:
                    continue
                mk = await asyncio.to_thread(gamma.market, a, tf, start)
                if mk is None:
                    continue
                eng = Engine(mk, clob, j, stats)
                ENG[slug] = eng
                spawn(watch(eng, gamma, stop))
        for slug in [s for s in ENG if int(s.rsplit("-", 1)[1]) < now - 7200]:
            ENG.pop(slug, None)
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass
    log.info("останавливаюсь. %s", stats.line())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
