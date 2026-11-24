#!/usr/bin/env python3
"""
comeback.py — «поздний камбэк» лидера в up/down рынках Polymarket (BTC 5m + 15m), тейкер.

Сигнал (книги BTC 13–27.10 + июль 15m):
  • сторона в этом окне уже стоила <= MIN_LOW (0.10) — была почти проигравшей;
  • её аск ВПЕРВЫЕ за окно попадает в коридор [ENTRY_LO, ENTRY_HI] (0.85–0.88);
  • и это первое попадание случилось после FRAC_MIN (80%) окна.
  → покупаем эту сторону тейкером (FAK) не дороже ENTRY_HI.
  Бэктест: 213 сигналов (~15 в сутки), WR 94%, ROI +8.6%; июль 15m: 36, WR 94%, ROI +8.9%.
  Дневной итог против мягкого rozi5: корреляция −0.54.

Объём: депозит × STAKE_PCT %, округление до целых $. Если меньше минимума площадки
(5 долей по потолку и $1) — берём минимум, сигнал не пропускаем.
COMPOUND=true: депозит = DEPOSIT + итог (с комиссией) закрытых сделок журнала — растёт с прибылью,
падает с убытком, но ставка не ниже минимума площадки.
Итог окна — сразу по книге: все последние BOOK_HOLD_S с бид победителя >= BOOK_WIN_PX
(бид Up — Up, бид Down — Down), иначе ждём gamma; gamma — фоновая сверка (resolved_fix).
Конфиг: comeback.env рядом со скриптом (или ENV_FILE). PAPER=true по умолчанию.
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
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import websockets

ENV_FILE_USED = ""


def _load_env_file() -> None:
    global ENV_FILE_USED
    explicit = os.environ.get("ENV_FILE", "").strip()
    here = Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] else Path.cwd()
    stem = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else "comeback"
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
    TFS = [int(x) for x in _env("TFS", "5,15").split(",") if x.strip()]

    # сигнал
    MIN_LOW = _f("MIN_LOW", 0.10)          # сторона в окне была <= (mid)
    ENTRY_LO = _f("ENTRY_LO", 0.85)        # коридор аска
    ENTRY_HI = _f("ENTRY_HI", 0.88)        # он же потолок цены тейкера
    FRAC_MIN = _f("FRAC_MIN", 0.80)        # первое попадание в коридор не раньше этой доли окна
    ENTRY_MIN_ASK = _f("ENTRY_MIN_ASK", 0.0)  # отсечка: аск при первом попадании в коридор не ниже (0 = выкл)
    MAX_SPREAD = _f("MAX_SPREAD", 0.05)    # mid считается только при спреде <= (как в анализе)
    JOIN_MAX_S = _f("JOIN_MAX_S", 15.0)    # бот подключился к окну позже — окно не торгуем (минимум неизвестен)
    END_GUARD_S = _f("END_GUARD_S", 2.0)   # не входить позже чем за столько секунд до конца

    # объём
    DEPOSIT = _f("DEPOSIT", 100.0)
    STAKE_PCT = _f("STAKE_PCT", 4.0)
    MIN_SHARES = _f("MIN_SHARES", 5.0)     # минимум площадки
    MIN_USDC = _f("MIN_USDC", 1.0)
    COMPOUND = _b("COMPOUND", True)        # депозит = DEPOSIT + итог закрытых сделок журнала
    COMPOUND_FROM = _env("COMPOUND_FROM", "")  # unix или 2026-10-29T12:00 (UTC); пусто = весь журнал
    FEE_RATE = _f("FEE_RATE", 0.07)        # комиссия тейкера: rate × p × (1−p) на долю

    # исполнение
    CAP_PX = _f("CAP_PX", 0.90)            # потолок цены тейкера (сигнал — по коридору ENTRY_LO–ENTRY_HI)
    PRESIGN = _b("PRESIGN", True)          # заранее подписать ордер, когда сторона подходит к сигналу
    PRESIGN_ASK = _f("PRESIGN_ASK", 0.70)  # подписывать, когда аск стороны >= и минимум уже был <= MIN_LOW
    PRESIGN_FRAC = _f("PRESIGN_FRAC", 0.70)  # и прошло не меньше этой доли окна
    FILL_TIMEOUT_S = _f("FILL_TIMEOUT_S", 6.0)   # повторять FAK, пока аск <= CAP_PX
    # подтверждение входа: после сигнала ждать CONFIRM_DELAY_S с и входить, только если бид стороны на этот момент
    # >= CONFIRM_MIN_BID и аск <= CONFIRM_MAX_ASK; иначе skip (no_confirm_bid / no_confirm_ask). 0 = выкл
    CONFIRM_DELAY_S = _f("CONFIRM_DELAY_S", 0.0)
    CONFIRM_MIN_BID = _f("CONFIRM_MIN_BID", 0.84)
    CONFIRM_MAX_ASK = _f("CONFIRM_MAX_ASK", 0.95)
    RETRY_S = _f("RETRY_S", 0.4)

    RESOLVE_BY_BOOK = _b("RESOLVE_BY_BOOK", True)
    BOOK_WIN_PX = _f("BOOK_WIN_PX", 0.99)  # бид победителя >= ... все последние BOOK_HOLD_S секунд окна
    BOOK_HOLD_S = _f("BOOK_HOLD_S", 3.0)
    RESOLVE_DELAY_S = _f("RESOLVE_DELAY_S", 60.0)
    RESOLVE_TIMEOUT_S = _f("RESOLVE_TIMEOUT_S", 8 * 3600.0)
    WS_STALE_S = _f("WS_STALE_S", 30.0)
    # фильтр чистки (проходит, если выполнено ХОТЯ БЫ ОДНО; 0 = условие выкл; оба 0 = фильтра нет):
    #  • |BTC − открытие окна| > MIN_DIST_BP (Binance; DIST_SRC=minute — закрытие последней закрытой минуты,
    #    как в анализе; live — последняя сделка)
    #  • отскок: от последнего mid стороны <= MIN_LOW до входа <= FAST_REC_S секунд
    # история 5m 13–27.10: 80 сигналов, WR 98.8% (без фильтра 96%), ROI +13.6%
    MIN_DIST_BP = _f("MIN_DIST_BP", 0.0)
    FAST_REC_S = _f("FAST_REC_S", 0.0)
    DIST_SRC = _env("DIST_SRC", "minute").lower()
    FILTER_LOG_ONLY = _b("FILTER_LOG_ONLY", False)   # true — брать все сигналы, фильтр только пишется в журнал
    BINANCE_WS = _env("BINANCE_WS", "wss://stream.binance.com:9443/ws")
    BINANCE_REST = _env("BINANCE_REST", "https://api.binance.com")
    # докупки лимитками: после основного налива — GTD BUY той же стороны по каждому уровню ADD_LEVELS
    # на ADD_SIZE × (USDC основного налива); снимаются по id за ADD_CANCEL_BEFORE_S до конца окна
    # (после конца налилась бы только проигравшая сторона). Пусто = выкл.
    # история окт (5m+15m): аск ≤0.70 — 56 раз, 75% побед; ≤0.50 — 33 раза, 58% побед
    # рывок: mid стороны за JUMP_LOOK_S секунд до первого попадания в коридор должен быть < JUMP_MAX_MID
    # (0 = выкл). Бот всё окно ведёт посекундную историю mid, поэтому в момент сигнала рывок уже известен.
    JUMP_LOOK_S = _f("JUMP_LOOK_S", 3.0)
    JUMP_MAX_MID = _f("JUMP_MAX_MID", 0.0)
    # режим сигнала: false — по каждому изменению книги (как раньше); true — раз в секунду по последней котировке
    # (как в бэктесте). SHADOW=true — второй режим считается параллельно и только пишется в журнал
    # (shadow_signal / shadow_skip / shadow_result), без сделок. PATH_LOG — посекундный путь книги окон BTC
    # (последняя котировка Up за секунду) для повторного прогона; пусто = выкл
    SIGNAL_1S = _b("SIGNAL_1S", False)
    SHADOW = _b("SHADOW", True)
    PATH_LOG = _env("PATH_LOG", "")
    ADD_LEVELS = [float(x) for x in _env("ADD_LEVELS", "").replace(" ", "").split(",") if x]
    ADD_SIZE = _f("ADD_SIZE", 1.0)
    ADD_CANCEL_BEFORE_S = _f("ADD_CANCEL_BEFORE_S", 1.0)
    # страховочные лимитки: сразу после налива основного входа — GTD BUY ОБРАТНОГО исхода по каждой цене
    # HEDGE_LEVELS на HEDGE_USDC $ каждая (фиксированная сумма, без % депозита и компаундинга; в депозит не входят).
    # Если основной вход не налился — не ставятся. HEDGE_CANCEL_AT_END=true — снять по id сразу после конца окна
    # (после конца на 0.01–0.05 налились бы только доли уже проигравшего исхода); false — живут до срока GTD.
    HEDGE_LEVELS = [float(x) for x in _env("HEDGE_LEVELS", "").replace(" ", "").split(",") if x]
    HEDGE_USDC = _f("HEDGE_USDC", 1.0)
    HEDGE_CANCEL_AT_END = _b("HEDGE_CANCEL_AT_END", True)
    GTD_PAD_S = _i("GTD_PAD_S", 30)
    GTD_MIN_S = _i("GTD_MIN_S", 200)         # биржа: срок GTD >= 180 с от «сейчас»
    JOURNAL = _env("JOURNAL", "comeback.jsonl")
    LOG_LEVEL = _env("LOG_LEVEL", "INFO")


def tfp(tf: int) -> dict:
    """Параметры сигнала для таймфрейма: NAME_5 / NAME_15 в env перекрывают общий NAME."""
    def g(name: str, base: float) -> float:
        v = _env(f"{name}_{tf}")
        return float(v) if v else base
    return {"lo": g("ENTRY_LO", Cfg.ENTRY_LO), "hi": g("ENTRY_HI", Cfg.ENTRY_HI), "fr": g("FRAC_MIN", Cfg.FRAC_MIN),
            "ml": g("MIN_LOW", Cfg.MIN_LOW), "jl": g("JUMP_LOOK_S", Cfg.JUMP_LOOK_S), "jm": g("JUMP_MAX_MID", Cfg.JUMP_MAX_MID),
            "ma": g("ENTRY_MIN_ASK", Cfg.ENTRY_MIN_ASK)}


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


EQUITY = None   # type: Optional[Equity]


def stake_floor() -> int:
    """Минимум площадки: MIN_SHARES долей по потолку и MIN_USDC, вверх до целых $."""
    hi = max([Cfg.ENTRY_HI, Cfg.CAP_PX] + [tfp(tf)["hi"] for tf in Cfg.TFS])
    return int(math.ceil(max(Cfg.MIN_USDC, Cfg.MIN_SHARES * hi) - 1e-9))


def deposit() -> float:
    return EQUITY.value if EQUITY is not None else Cfg.DEPOSIT


def stake_usdc() -> float:
    """Депозит × STAKE_PCT %, до целых (половина — вверх); меньше минимума площадки — минимум."""
    calc = int(math.floor(max(0.0, deposit()) * Cfg.STAKE_PCT / 100.0 + 0.5))
    return float(max(calc, stake_floor()))


def book_winner_tail(tail, end: float) -> Optional[str]:
    """tail: (t, бид Up, бид Down). Исход, если на всём отрезке [конец − BOOK_HOLD_S, конец] (включая
    котировку, действовавшую на его начало) бид победителя >= BOOK_WIN_PX. Сверка с gamma, 6193 окна
    (0.99 / 3 с): решено 99.66%, расхождений 3 — поздний переворот по TWAP при книге 0.999 (их исправит сверка)."""
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

log = logging.getLogger("comeback")


class Binance:
    """Минутные бары BTC (open/close) из потока aggTrade + сверка REST-свечами раз в 30 с."""

    def __init__(self, sym: str = "BTCUSDT"):
        self.sym = sym
        self.bars: Dict[int, List[float]] = {}     # начало минуты -> [open, close]
        self.price: Optional[float] = None
        self.price_t = 0.0
        self.rest_ok: set = set()                  # минуты, подтверждённые REST (закрытые)

    def _trade(self, px: float, t: float) -> None:
        self.price, self.price_t = px, t
        mnt = int(t // 60) * 60
        b = self.bars.get(mnt)
        if b is None:
            self.bars[mnt] = [px, px]
        elif mnt not in self.rest_ok:
            b[1] = px

    def _rest(self) -> None:
        r = requests.get(f"{Cfg.BINANCE_REST}/api/v3/klines",
                         params={"symbol": self.sym, "interval": "1m", "limit": 30}, timeout=5)
        r.raise_for_status()
        now = time.time()
        for k in r.json():
            mnt = int(k[0]) // 1000
            if mnt + 60 <= now:                         # закрытая минута — точные open/close
                self.bars[mnt] = [float(k[1]), float(k[4])]
                self.rest_ok.add(mnt)
            elif mnt not in self.bars:
                self.bars[mnt] = [float(k[1]), float(k[4])]
        cut = now - 4 * 3600
        for mnt in [x for x in self.bars if x < cut]:
            self.bars.pop(mnt, None)
            self.rest_ok.discard(mnt)

    async def run(self, stop: asyncio.Event) -> None:
        async def rest_loop():
            while not stop.is_set():
                try:
                    await asyncio.to_thread(self._rest)
                except Exception as e:
                    log.warning("Binance REST: %s", e)
                delay = (61.0 - time.time() % 60) % 30 or 30     # сразу после закрытия минуты и в середине
                try:
                    await asyncio.wait_for(stop.wait(), timeout=max(1.0, delay))
                except asyncio.TimeoutError:
                    pass
        spawn(rest_loop())
        url = f"{Cfg.BINANCE_WS}/{self.sym.lower()}@aggTrade"
        while not stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, close_timeout=5) as ws:
                    log.info("Binance поток подключён: %s", self.sym)
                    while not stop.is_set():
                        d = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                        if "p" in d:
                            self._trade(float(d["p"]), d["T"] / 1000.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Binance поток: %s — переподключаюсь", e)
                await asyncio.sleep(1.0)

    def dist_bp(self, start: int, t: float, side: str) -> Tuple[Optional[float], Optional[float]]:
        """(расстояние по последней закрытой минуте, по последней сделке) от открытия окна, bp, в пользу стороны."""
        b0 = self.bars.get(int(start // 60) * 60)
        if not b0:
            return None, None
        o = b0[0]
        sg = 1 if side == "Up" else -1
        last = self.bars.get(int(t // 60) * 60 - 60)
        dm = sg * math.log(last[1] / o) * 1e4 if last else None
        dl = sg * math.log(self.price / o) * 1e4 if self.price and t - self.price_t < 10 else None
        return dm, dl


BN: Optional[Binance] = None
BN_SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}


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
    tokens: Dict[str, str]       # 'Up'/'Down' -> token_id
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
        self.s.headers["User-Agent"] = "comeback/1.0"

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

    def buy(self, token: str, usdc: float, cap: float, tick: str, signed: Any = None) -> dict:
        """FAK BUY на usdc не дороже cap (signed — заранее подписанный). {'ok','usdc','shares','unknown','err'}."""
        m = self.m
        if signed is None:
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

    def place(self, token: str, price: float, shares: float, tick: str, expiration: int) -> dict:
        """GTD BUY лимитка. {'ok','id','unknown','err','status'}."""
        m = self.m
        try:
            args = m.OrderArgsV2(token_id=token, price=round(price, 6), size=round(shares, 2),
                                 side=m.Side.BUY, expiration=int(expiration))
            signed = self.client.create_order(args, m.PartialCreateOrderOptions(tick_size=tick))
        except Exception as e:
            return {"ok": False, "unknown": False, "err": f"сборка: {e}"[:200]}
        try:
            r = self.client.post_order(signed, m.OrderType.GTD)
        except Exception as e:
            code = getattr(e, "status_code", None)
            return {"ok": False, "unknown": not (isinstance(code, int) and 400 <= code < 500), "err": str(e)[:200]}
        if not isinstance(r, dict):
            return {"ok": False, "unknown": True, "err": f"ответ {r!r}"[:200]}
        oid = r.get("orderID") or r.get("orderId") or r.get("id")
        if r.get("success") is False or not oid:
            return {"ok": False, "unknown": False, "err": str(r.get("errorMsg") or r)[:200]}
        return {"ok": True, "id": oid, "status": r.get("status"), "unknown": False}

    def order(self, oid: str) -> Optional[dict]:
        try:
            r = self.client.get_order(oid)
            return r if isinstance(r, dict) else None
        except Exception as e:
            log.warning("ордер %s: %s", oid[:12], e)
            return None

    def cancel(self, oid: str) -> bool:
        """Снятие только по id (на кошельке работают другие боты)."""
        try:
            r = self.client.cancel_orders([oid])
            r = r if isinstance(r, dict) else {}
            return oid in set(r.get("canceled") or [])
        except Exception as e:
            log.warning("отмена %s: %s", oid[:12], e)
            return False



def _num(d: dict, *keys) -> Optional[float]:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


# ════════════════════════════════════════════════════════════════ движок окна
@dataclass
class SideState:
    name: str
    low: float = 9.0              # минимум mid стороны в окне
    band_seen: bool = False       # аск уже бывал в коридоре (с валидным mid)
    fired: bool = False
    usdc: float = 0.0
    shares: float = 0.0
    fee: float = 0.0
    presigned: Any = None
    presign_key: Optional[tuple] = None
    presigning: bool = False
    last_low_t: Optional[float] = None   # последний момент, когда mid стороны был <= MIN_LOW
    hist: List[Tuple[float, float]] = field(default_factory=list)   # (t, mid) — по одной точке на секунду
    adds: List[dict] = field(default_factory=list)   # лимитки-докупки: px, shares, oid, matched, paper_filled
    add_usdc: float = 0.0
    add_shares: float = 0.0
    hedges: List[dict] = field(default_factory=list)   # лимитки на обратный исход: px, shares, oid, matched
    # посекундный автомат (SIGNAL_1S / тень)
    low1: float = 9.0
    band1: bool = False
    hist1: List[Tuple[float, float]] = field(default_factory=list)


class Engine:
    def __init__(self, m: Market, clob: Clob, j: Journal, stats: "Stats"):
        self.m, self.clob, self.j, self.stats = m, clob, j, stats
        self.sides = {"Up": SideState("Up"), "Down": SideState("Down")}
        self.p = tfp(m.tf)
        self.shadow: List[dict] = []                     # сигналы теневого режима (для shadow_result)
        self.path_b: List[Optional[float]] = [None] * m.length
        self.path_a: List[Optional[float]] = [None] * m.length
        self.adds_task: Optional[asyncio.Task] = None
        self.up_bid: Optional[float] = None
        self.up_ask: Optional[float] = None
        self.up_last: Optional[Tuple[float, Optional[float], Optional[float]]] = None
        self.tail: List[Tuple[float, Optional[float], Optional[float]]] = []   # (t, бид Up, бид Down) в конце окна
        self.first_quote: Optional[float] = None
        self.levels: Dict[str, Dict[str, Dict[float, float]]] = {t: {"bids": {}, "asks": {}} for t in m.tokens.values()}
        self.busy = False

    def on_levels(self, token: str, side: str, price: float, size: float) -> None:
        book = self.levels.get(token)
        if book is None:
            return
        d = book["bids" if side == "BUY" else "asks"]
        if size <= 0:
            d.pop(price, None)
        else:
            d[price] = size

    def set_book(self, token: str, bids, asks) -> None:
        book = self.levels.get(token)
        if book is None:
            return
        book["bids"] = {float(x["price"]): float(x["size"]) for x in bids or [] if float(x.get("size", 0) or 0) > 0}
        book["asks"] = {float(x["price"]): float(x["size"]) for x in asks or [] if float(x.get("size", 0) or 0) > 0}

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
            db = 1 - self.up_ask                       # бид Down = 1 − аск Up (зеркало)
        self.tail.append((now, ub, db))

    def depth(self, side: str, cap: float) -> float:
        tok = self.m.tokens[side]
        return round(sum(s for p, s in self.levels[tok]["asks"].items() if p <= cap + 1e-9), 2)

    def side_ask(self, side: str) -> Optional[float]:
        if self.up_bid is None or self.up_ask is None:
            return None
        return self.up_ask if side == "Up" else 1 - self.up_bid

    def side_bid(self, side: str) -> Optional[float]:
        """Бид стороны по зеркалу книги Up (как в анализе): Up — бид Up, Down — 1 − аск Up."""
        if self.up_bid is None or self.up_ask is None:
            return None
        return self.up_bid if side == "Up" else 1 - self.up_ask

    def on_quote(self, token: str, bid: Optional[float], ask: Optional[float], now: float) -> None:
        if token != self.m.tokens["Up"]:
            return                                       # сигнал по книге Up (как в анализе)
        if bid is not None:
            self.up_bid = bid
        if ask is not None:
            self.up_ask = ask
        b, a = self.up_bid, self.up_ask
        if b is None or a is None:
            return
        if not self.clob.enabled and now < self.m.end:
            for s in self.sides.values():
                if not s.hedges:
                    continue
                o_ask = (1 - b) if s.name == "Up" else a          # аск обратного исхода
                for hd in s.hedges:
                    if not hd.get("paper_filled") and hd.get("oid") and now > hd["t"] and o_ask < hd["px"] - 1e-9:
                        hd["paper_filled"] = True
                        log.warning("%s: страховка %s по %.2f налилась (бумага), аск %.3f", self.m.slug, hd["side"], hd["px"], o_ask)
        if not self.clob.enabled and now < self.m.end - Cfg.ADD_CANCEL_BEFORE_S:
            for s in self.sides.values():
                s_ask = a if s.name == "Up" else 1 - b
                for ad in s.adds:
                    if not ad.get("paper_filled") and ad.get("oid") and now > ad["t"] and s_ask <= ad["px"] + 1e-9:
                        ad["paper_filled"] = True
                        log.warning("%s %s: докупка по %.2f налилась (бумага), аск %.3f", self.m.slug, s.name, ad["px"], s_ask)
        el = now - self.m.start
        if el < 0 or now > self.m.end:
            return
        if self.first_quote is None:
            self.first_quote = el
        self.up_last = (now, b, a)
        ok = 0 < a - b <= Cfg.MAX_SPREAD + 1e-9
        for s in self.sides.values():
            mid = ((a + b) / 2 if s.name == "Up" else 1 - (a + b) / 2) if ok else None
            s_ask = a if s.name == "Up" else 1 - b
            if mid is not None and mid < s.low:
                s.low = mid
            if mid is not None and mid <= self.p["ml"] + 1e-9:
                s.last_low_t = now
            if mid is not None:                          # посекундная история mid (последняя точка секунды)
                if s.hist and int(s.hist[-1][0]) == int(now):
                    s.hist[-1] = (now, mid)
                else:
                    s.hist.append((now, mid))
                    if len(s.hist) > 120:
                        del s.hist[:len(s.hist) - 120]
            if (Cfg.PRESIGN and self.clob.enabled and not s.band_seen and not s.presigning and s.presigned is None
                    and s.low <= self.p["ml"] + 1e-9 and s_ask >= Cfg.PRESIGN_ASK and el / self.m.length >= Cfg.PRESIGN_FRAC):
                s.presigning = True
                spawn(self.presign(s))
            in_band = mid is not None and el >= 5 and self.p["lo"] - 1e-9 <= s_ask <= self.p["hi"] + 1e-9
            if not in_band or s.band_seen:
                continue
            s.band_seen = True                           # первое попадание в коридор — дальше не повторяется
            frac = el / self.m.length
            if frac < self.p["fr"] or s.low > self.p["ml"] + 1e-9:
                continue
            if self.p["ma"] > 0 and s_ask < self.p["ma"] - 1e-9:
                spawn(self.j.write({"event": "skip", "slug": self.m.slug, "side": s.name, "mode": "tick", "reason": "low_ask",
                                    "frac": round(frac, 3), "ask": round(s_ask, 4), "low": round(s.low, 4),
                                    "min_ask": self.p["ma"]}))
                log.info("%s %s: камбэк по %.3f < отсечки %.2f, пропуск", self.m.slug, s.name, s_ask, self.p["ma"])
                continue
            if self.first_quote > Cfg.JOIN_MAX_S:
                spawn(self.j.write({"event": "skip", "slug": self.m.slug, "side": s.name, "mode": "tick", "reason": "late_join",
                                    "join_s": round(self.first_quote, 1)}))
                continue
            if self.m.end - now < Cfg.END_GUARD_S:
                continue
            flt = self.filter_info(s, now)
            jb = self.mid_back(s, now)
            flt["mid_back"] = None if jb is None else round(jb, 4)
            flt["jump_look_s"] = self.p["jl"]
            flt["jump_ok"] = self.p["jm"] <= 0 or jb is None or jb < self.p["jm"] - 1e-9
            if not flt["jump_ok"] and not Cfg.FILTER_LOG_ONLY:
                spawn(self.j.write({"event": "skip", "slug": self.m.slug, "side": s.name, "mode": "tick", "reason": "slow_jump",
                                    "frac": round(frac, 3), "ask": round(s_ask, 4), "low": round(s.low, 4), **flt}))
                log.info("%s %s: камбэк без рывка — mid %.0f с назад %.3f >= %.2f, пропуск", self.m.slug, s.name,
                         self.p["jl"], jb, self.p["jm"])
                continue
            if not flt["pass"] and not Cfg.FILTER_LOG_ONLY:
                spawn(self.j.write({"event": "skip", "slug": self.m.slug, "side": s.name, "mode": "tick", "reason": "filter",
                                    "frac": round(frac, 3), "ask": round(s_ask, 4), "low": round(s.low, 4), **flt}))
                log.info("%s %s: камбэк отсеян фильтром (отскок %s с, BTC %s bp)", self.m.slug, s.name,
                         flt["rec_s"], flt["dist_bp"])
                continue
            if Cfg.SIGNAL_1S:                            # торгует посекундный режим — этот только в журнал
                if Cfg.SHADOW:
                    self.shadow_hit(s, "tick", now, s_ask, frac, flt)
                continue
            s.fired = True
            spawn(self.enter(s, now, s_ask, frac, flt, mode="tick"))

    # --- посекундный режим: раз в секунду, по последней котировке Up этой секунды (как в бэктесте)
    async def sec_loop(self) -> None:
        m = self.m
        while time.time() < m.end + 0.5:
            now = time.time()
            await asyncio.sleep(max(0.0, math.floor(now) + 1 - now) + 0.003)
            try:
                self.sample(math.floor(time.time()))
            except Exception as e:
                log.debug("%s: секундный снимок: %s", m.slug, e)

    def sample(self, boundary: int) -> None:
        m = self.m
        el = boundary - 1 - m.start                     # снимок секунды el = последняя котировка до её конца
        if el < 0 or el >= m.length:
            return
        b, a = self.up_bid, self.up_ask
        if b is None or a is None:
            return
        self.path_b[el], self.path_a[el] = b, a
        if not (Cfg.SIGNAL_1S or Cfg.SHADOW):
            return
        ok = 0 < a - b <= Cfg.MAX_SPREAD + 1e-9
        t = float(m.start + el)
        for s in self.sides.values():
            mid = ((a + b) / 2 if s.name == "Up" else 1 - (a + b) / 2) if ok else None
            s_ask = a if s.name == "Up" else 1 - b
            if mid is None:
                continue
            s.low1 = min(s.low1, mid)
            s.hist1.append((t, mid))
            if len(s.hist1) > 120:
                del s.hist1[:len(s.hist1) - 120]
            if s.band1 or el < 5 or not (self.p["lo"] - 1e-9 <= s_ask <= self.p["hi"] + 1e-9):
                continue
            s.band1 = True
            frac = el / m.length
            if frac < self.p["fr"] or s.low1 > self.p["ml"] + 1e-9:
                continue
            mode = "1s"
            info = {"slug": m.slug, "tf": m.tf, "side": s.name, "mode": mode, "el": el, "frac": round(frac, 3),
                    "ask": round(s_ask, 4), "low": round(s.low1, 4)}
            if self.p["ma"] > 0 and s_ask < self.p["ma"] - 1e-9:
                self._1s_skip({**info, "min_ask": self.p["ma"]}, "low_ask")
                continue
            if self.first_quote is None or self.first_quote > Cfg.JOIN_MAX_S:
                self._1s_skip(info, "late_join")
                continue
            if m.end - time.time() < Cfg.END_GUARD_S:
                continue
            jb = None
            for ht, hm in reversed(s.hist1):
                if ht <= t - self.p["jl"] + 1e-9:
                    jb = hm
                    break
            jump_ok = self.p["jm"] <= 0 or jb is None or jb < self.p["jm"] - 1e-9
            info.update(mid_back=None if jb is None else round(jb, 4), jump_look_s=self.p["jl"], jump_ok=jump_ok)
            if not jump_ok and not Cfg.FILTER_LOG_ONLY:
                self._1s_skip(info, "slow_jump")
                continue
            flt = self.filter_info(s, time.time())
            flt.update(mid_back=info["mid_back"], jump_look_s=self.p["jl"], jump_ok=jump_ok)
            if not flt["pass"] and not Cfg.FILTER_LOG_ONLY:
                self._1s_skip(info, "filter")
                continue
            live_ask = self.side_ask(s.name)
            if Cfg.SIGNAL_1S:
                if s.fired:
                    continue
                s.fired = True
                spawn(self.enter(s, time.time(), live_ask if live_ask is not None else s_ask, frac, flt, mode="1s"))
            else:
                self.shadow_hit(s, "1s", time.time(), live_ask if live_ask is not None else s_ask, frac, flt, snap_ask=s_ask)

    def _1s_skip(self, info: dict, reason: str) -> None:
        ev = "skip" if Cfg.SIGNAL_1S else "shadow_skip"
        spawn(self.j.write({"event": ev, **info, "reason": reason}))

    def shadow_hit(self, s: SideState, mode: str, now: float, ask: float, frac: float, flt: dict,
                   snap_ask: Optional[float] = None) -> None:
        rec = {"event": "shadow_signal", "slug": self.m.slug, "tf": self.m.tf, "side": s.name, "mode": mode,
               "el": round(now - self.m.start, 1), "frac": round(frac, 3), "rem_s": round(self.m.end - now, 1),
               "ask": round(ask, 4), "low": round(s.low1 if mode == "1s" else s.low, 4),
               "mid_back": flt.get("mid_back"), "traded_too": s.fired}
        if snap_ask is not None:
            rec["snap_ask"] = round(snap_ask, 4)
        self.shadow.append(rec)
        spawn(self.j.write(rec))
        log.info("%s %s: ТЕНЬ (%s) — сигнал камбэка, аск %.3f на %.0f%% окна%s", self.m.slug, s.name, mode, ask,
                 frac * 100, " (основной режим тоже вошёл)" if s.fired else "")

    def mid_back(self, s: SideState, now: float) -> Optional[float]:
        """mid стороны JUMP_LOOK_S секунд назад (последняя точка не позже now − look)."""
        t = now - self.p["jl"]
        for ht, hm in reversed(s.hist):
            if ht <= t + 1e-9:
                return hm
        return None

    def filter_info(self, s: SideState, now: float) -> dict:
        rec = round(now - s.last_low_t, 1) if s.last_low_t is not None else None
        dm, dl = BN.dist_bp(self.m.start, now, s.name) if BN is not None else (None, None)
        d = dl if Cfg.DIST_SRC == "live" else dm
        on_d, on_r = Cfg.MIN_DIST_BP > 0, Cfg.FAST_REC_S > 0
        ok_d = on_d and d is not None and abs(d) > Cfg.MIN_DIST_BP
        ok_r = on_r and rec is not None and rec <= Cfg.FAST_REC_S
        return {"pass": (ok_d or ok_r) if (on_d or on_r) else True, "rec_s": rec,
                "dist_bp": None if d is None else round(d, 2),
                "dist_min_bp": None if dm is None else round(dm, 2), "dist_live_bp": None if dl is None else round(dl, 2),
                "by_dist": ok_d, "by_rec": ok_r}

    async def presign(self, s: SideState) -> None:
        key = (stake_usdc(), Cfg.CAP_PX, self.m.tick)
        try:
            s.presigned = await asyncio.to_thread(self.clob.sign, self.m.tokens[s.name], key[0], key[1], key[2])
            s.presign_key = key
        except Exception as e:
            log.warning("%s %s: предподпись: %s", self.m.slug, s.name, e)
        finally:
            s.presigning = False

    async def enter(self, s: SideState, now: float, s_ask: float, frac: float, flt: Optional[dict] = None,
                    mode: str = "tick") -> None:
        m = self.m
        usdc = stake_usdc()
        info = dict(event="signal", mode=mode, slug=m.slug, tf=m.tf, side=s.name, el=round(now - m.start, 1), frac=round(frac, 3),
                    rem_s=round(m.end - now, 1), low=round(s.low, 4), ask=round(s_ask, 4), usdc=usdc,
                    deposit=round(deposit(), 2),
                    depth=self.depth(s.name, self.p["hi"]), depth_cap=self.depth(s.name, Cfg.CAP_PX), **(flt or {}))
        if Cfg.CONFIRM_DELAY_S > 0:                 # подтверждение: движение должно удержаться CONFIRM_DELAY_S секунд
            await asyncio.sleep(Cfg.CONFIRM_DELAY_S)
            cb, ca = self.side_bid(s.name), self.side_ask(s.name)
            why = None
            if cb is None or cb < Cfg.CONFIRM_MIN_BID - 1e-9:
                why = "no_confirm_bid"
            elif ca is None or ca > Cfg.CONFIRM_MAX_ASK + 1e-9:
                why = "no_confirm_ask"
            if why:
                await self.j.write({"event": "skip", "mode": mode, "slug": m.slug, "tf": m.tf, "side": s.name, "reason": why,
                                    "bid_c": None if cb is None else round(cb, 4), "ask_c": None if ca is None else round(ca, 4),
                                    "ask_sig": round(s_ask, 4), "delay_s": Cfg.CONFIRM_DELAY_S})
                log.info("%s %s: нет подтверждения через %.1fс (бид %s, аск %s; нужно бид >= %.2f, аск <= %.2f) — пропуск",
                         m.slug, s.name, Cfg.CONFIRM_DELAY_S, None if cb is None else round(cb, 3),
                         None if ca is None else round(ca, 3), Cfg.CONFIRM_MIN_BID, Cfg.CONFIRM_MAX_ASK)
                return
            info.update(confirm_bid=round(cb, 4), confirm_ask=round(ca, 4), confirm_delay_s=Cfg.CONFIRM_DELAY_S)
        t_sig = time.time()
        deadline = t_sig + Cfg.FILL_TIMEOUT_S
        tries = 0
        logged = False
        while time.time() < deadline and m.end - time.time() > Cfg.END_GUARD_S / 2:
            ask = self.side_ask(s.name)
            if tries > 0 and (ask is None or ask > Cfg.CAP_PX + 1e-9):
                await asyncio.sleep(Cfg.RETRY_S / 4)
                continue
            tries += 1
            if not self.clob.enabled:
                px = ask if ask is not None else s_ask
                sh = usdc / px
                s.usdc, s.shares = usdc, sh
                s.fee = Cfg.FEE_RATE * px * (1 - px) * sh
                if Cfg.HEDGE_LEVELS:
                    spawn(self.place_hedges(s))
                await self.j.write(info)
                await self.j.write({"event": "fill", "slug": m.slug, "side": s.name, "usdc": usdc, "shares": round(sh, 4),
                                    "avg_px": round(px, 4), "ask_sig": round(s_ask, 4), "slip": round(px - s_ask, 4),
                                    "fee": round(s.fee, 4), "paper": True, "try": tries})
                log.warning("%s %s: СИГНАЛ камбэк — была %.3f, аск %.3f на %.0f%% окна → куплено (бумага) $%.0f",
                            m.slug, s.name, s.low, px, frac * 100, usdc)
                if Cfg.ADD_LEVELS:
                    spawn(self.place_adds(s))
                return
            signed = None
            if tries == 1 and s.presigned is not None and s.presign_key == (usdc, Cfg.CAP_PX, m.tick):
                signed, s.presigned = s.presigned, None
            r = await asyncio.to_thread(self.clob.buy, m.tokens[s.name], usdc, Cfg.CAP_PX, m.tick, signed)
            lat = round((time.time() - t_sig) * 1000)
            if not logged:                          # журнал — после первой отправки, чтобы не терять время
                await self.j.write(info)
                log.warning("%s %s: СИГНАЛ камбэк — была %.3f, аск %.3f на %.0f%% окна (%.0fс до конца) → $%.0f",
                            m.slug, s.name, s.low, s_ask, frac * 100, m.end - now, usdc)
                logged = True
            await self.j.write({"event": "take", "slug": m.slug, "side": s.name, "try": tries, "presigned": signed is not None,
                                "latency_ms": lat, **r})
            if r.get("ok"):
                s.usdc, s.shares = r["usdc"], r["shares"]
                if Cfg.HEDGE_LEVELS:                 # сразу, до записи в журнал
                    spawn(self.place_hedges(s))
                px = s.usdc / s.shares if s.shares else Cfg.CAP_PX
                s.fee = Cfg.FEE_RATE * px * (1 - px) * s.shares
                await self.j.write({"event": "fill", "slug": m.slug, "side": s.name, "usdc": round(s.usdc, 4),
                                    "shares": round(s.shares, 4), "avg_px": round(px, 4), "ask_sig": round(s_ask, 4),
                                    "slip": round(px - s_ask, 4), "ask_now": self.side_ask(s.name), "fee": round(s.fee, 4),
                                    "paper": False, "try": tries, "latency_ms": lat})
                log.warning("%s %s: куплено %.2f долей за $%.2f (ср. %.3f), %d мс", m.slug, s.name, s.shares, s.usdc, px, lat)
                if Cfg.ADD_LEVELS:
                    spawn(self.place_adds(s))
                return
            if r.get("unknown"):
                log.error("%s %s: исход ордера неизвестен (%s) — повтор не делаю", m.slug, s.name, r.get("err"))
                return
            await asyncio.sleep(Cfg.RETRY_S / 4)
        if not logged:
            await self.j.write(info)
        await self.j.write({"event": "no_fill", "slug": m.slug, "side": s.name, "tries": tries,
                            "ask_now": self.side_ask(s.name)})
        log.info("%s %s: не налилось за %.0fс (аск выше %.2f)", m.slug, s.name, Cfg.FILL_TIMEOUT_S, Cfg.CAP_PX)

    def book_winner(self) -> Optional[str]:
        return book_winner_tail(self.tail, self.m.end)

    async def place_adds(self, s: SideState) -> None:
        """Лимитки-докупки по уровням ADD_LEVELS на ADD_SIZE × USDC основного налива."""
        m = self.m
        if m.end - time.time() <= Cfg.ADD_CANCEL_BEFORE_S + 0.5:
            return
        exp = int(max(m.end + Cfg.GTD_PAD_S, time.time() + Cfg.GTD_MIN_S))
        base = s.usdc * Cfg.ADD_SIZE
        for lv in Cfg.ADD_LEVELS:
            px = round(math.floor(lv / float(m.tick) + 1e-9) * float(m.tick), 4)
            sh = math.floor(base / px * 100) / 100
            if sh < Cfg.MIN_SHARES or sh * px < Cfg.MIN_USDC:
                sh = max(Cfg.MIN_SHARES, math.ceil(Cfg.MIN_USDC / px * 100) / 100)
            ad = {"px": px, "shares": sh, "oid": None, "matched": 0.0, "t": time.time()}
            s.adds.append(ad)
            if not self.clob.enabled:
                ad["oid"] = f"paper-{m.slug}-{s.name}-{px}"
                await self.j.write({"event": "add_order", "slug": m.slug, "side": s.name, "px": px, "shares": sh,
                                    "usdc": round(sh * px, 4), "paper": True, "expiration": exp})
                continue
            r = await asyncio.to_thread(self.clob.place, m.tokens[s.name], px, sh, m.tick, exp)
            await self.j.write({"event": "add_order", "slug": m.slug, "side": s.name, "px": px, "shares": sh,
                                "usdc": round(sh * px, 4), "paper": False, "expiration": exp, **r})
            if r.get("ok"):
                ad["oid"] = r["id"]
                log.warning("%s %s: докупка-лимитка %.2f × %.2f поставлена, id %s", m.slug, s.name, px, sh, r["id"][:14])
            else:
                log.error("%s %s: докупка %.2f не встала%s: %s", m.slug, s.name, px,
                          " (исход неизвестен!)" if r.get("unknown") else "", r.get("err"))
        if self.adds_task is None:
            self.adds_task = spawn(self.close_adds())

    async def place_hedges(self, s: SideState) -> None:
        """Сразу после налива: GTD BUY обратного исхода по всем HEDGE_LEVELS одновременно, по HEDGE_USDC $."""
        m = self.m
        if m.end - time.time() <= 0.3:
            return
        opp = "Down" if s.name == "Up" else "Up"
        exp = int(max(m.end + Cfg.GTD_PAD_S, time.time() + Cfg.GTD_MIN_S))
        tick = float(m.tick)
        hs = []
        for lv in Cfg.HEDGE_LEVELS:
            px = round(max(tick, math.floor(lv / tick + 1e-9) * tick), 4)
            sh = math.floor(Cfg.HEDGE_USDC / px * 100) / 100
            if sh < Cfg.MIN_SHARES or sh * px < Cfg.MIN_USDC - 1e-9:
                sh = max(Cfg.MIN_SHARES, math.ceil(Cfg.MIN_USDC / px * 100) / 100)
            hd = {"side": opp, "px": px, "shares": sh, "oid": None, "matched": 0.0, "t": time.time()}
            s.hedges.append(hd)
            hs.append(hd)
        if not self.clob.enabled:
            for hd in hs:
                hd["oid"] = f"paper-{m.slug}-{opp}-{hd['px']}"
            for hd in hs:
                await self.j.write({"event": "hedge_order", "slug": m.slug, "main_side": s.name, "side": opp,
                                    "px": hd["px"], "shares": hd["shares"], "usdc": round(hd["shares"] * hd["px"], 4),
                                    "paper": True, "expiration": exp})
            log.warning("%s: страховочные лимитки %s по %s поставлены (бумага)", m.slug, opp,
                        ", ".join(f"{hd['px']:g}" for hd in hs))
            return
        tok = m.tokens[opp]
        rs = await asyncio.gather(*[asyncio.to_thread(self.clob.place, tok, hd["px"], hd["shares"], m.tick, exp) for hd in hs],
                                  return_exceptions=True)
        for hd, r in zip(hs, rs):
            if isinstance(r, Exception):
                r = {"ok": False, "unknown": True, "err": str(r)[:200]}
            if r.get("ok"):
                hd["oid"] = r["id"]
            await self.j.write({"event": "hedge_order", "slug": m.slug, "main_side": s.name, "side": opp, "px": hd["px"],
                                "shares": hd["shares"], "usdc": round(hd["shares"] * hd["px"], 4), "paper": False,
                                "expiration": exp, "lag_ms": round((time.time() - hd["t"]) * 1000),
                                **{k: v for k, v in r.items() if k != "resp"}})
            if r.get("ok"):
                log.warning("%s: страховка %s %.2f × %.2f поставлена, id %s", m.slug, opp, hd["px"], hd["shares"], r["id"][:14])
            else:
                log.error("%s: страховка %s %.2f не встала%s: %s", m.slug, opp, hd["px"],
                          " (исход неизвестен!)" if r.get("unknown") else "", r.get("err"))

    async def close_hedges(self) -> None:
        """После конца окна: снять страховки по id (HEDGE_CANCEL_AT_END) и узнать, сколько налилось."""
        m = self.m
        for s in self.sides.values():
            for hd in s.hedges:
                if not hd.get("oid"):
                    continue
                if not self.clob.enabled:
                    hd["matched"] = hd["shares"] if hd.get("paper_filled") else 0.0
                else:
                    ok = None
                    if Cfg.HEDGE_CANCEL_AT_END:
                        ok = await asyncio.to_thread(self.clob.cancel, hd["oid"])
                    o = await asyncio.to_thread(self.clob.order, hd["oid"]) or {}
                    hd["matched"] = _num(o, "size_matched", "sizeMatched") or 0.0
                    if Cfg.HEDGE_CANCEL_AT_END and not ok and str(o.get("status") or "").upper() in ("LIVE", "OPEN"):
                        ok = await asyncio.to_thread(self.clob.cancel, hd["oid"])
                    await self.j.write({"event": "hedge_cancel" if Cfg.HEDGE_CANCEL_AT_END else "hedge_check", "slug": m.slug,
                                        "side": hd["side"], "px": hd["px"], "id": hd["oid"], "ok": ok, "status": o.get("status")})
                await self.j.write({"event": "hedge_fill" if hd["matched"] > 0 else "hedge_no_fill", "slug": m.slug,
                                    "side": hd["side"], "px": hd["px"], "shares": round(hd["matched"], 4),
                                    "usdc": round(hd["matched"] * hd["px"], 4), "paper": not self.clob.enabled})

    async def settle_hedges(self, w: str, src: str, fix: bool = False) -> None:
        """Итог страховок отдельно от основы (в депозит/компаундинг не входит)."""
        for s in self.sides.values():
            for hd in s.hedges:
                if hd["matched"] <= 0:
                    continue
                won = hd["side"] == w
                pnl = (hd["matched"] if won else 0.0) - hd["matched"] * hd["px"]
                await self.j.write({"event": "hedge_resolved_fix" if fix else "hedge_resolved", "slug": self.m.slug,
                                    "main_side": s.name, "side": hd["side"], "px": hd["px"], "shares": round(hd["matched"], 4),
                                    "usdc": round(hd["matched"] * hd["px"], 4), "winner": w, "source": src, "won": won,
                                    "pnl": round(pnl, 4), "paper": not self.clob.enabled})
                log.warning("%s страховка %s %.2f: победил %s → %+.2f", self.m.slug, hd["side"], hd["px"], w, pnl)

    async def close_adds(self) -> None:
        """За ADD_CANCEL_BEFORE_S до конца: снять лимитки по id и учесть наливы в позиции."""
        m = self.m
        await asyncio.sleep(max(0.0, m.end - Cfg.ADD_CANCEL_BEFORE_S - time.time()))
        for s in self.sides.values():
            for ad in s.adds:
                if not ad.get("oid"):
                    continue
                if not self.clob.enabled:
                    ad["matched"] = ad["shares"] if ad.get("paper_filled") else 0.0
                else:
                    ok = await asyncio.to_thread(self.clob.cancel, ad["oid"])
                    o = await asyncio.to_thread(self.clob.order, ad["oid"]) or {}
                    ad["matched"] = _num(o, "size_matched", "sizeMatched") or 0.0
                    if not ok and str(o.get("status") or "").upper() in ("LIVE", "OPEN"):
                        ok = await asyncio.to_thread(self.clob.cancel, ad["oid"])
                        log.error("%s %s: докупка %s — повторная отмена: %s", m.slug, s.name, ad["oid"][:12], ok)
                    await self.j.write({"event": "add_cancel", "slug": m.slug, "side": s.name, "id": ad["oid"], "ok": ok,
                                        "status": o.get("status")})
                if ad["matched"] > 0:
                    u = ad["matched"] * ad["px"]
                    s.add_usdc += u
                    s.add_shares += ad["matched"]
                    s.usdc += u                       # мейкер — без комиссии
                    s.shares += ad["matched"]
                await self.j.write({"event": "add_fill" if ad["matched"] > 0 else "add_no_fill", "slug": m.slug,
                                    "side": s.name, "px": ad["px"], "shares": round(ad["matched"], 4),
                                    "usdc": round(ad["matched"] * ad["px"], 4), "paper": not self.clob.enabled})

    async def finish(self, gamma: Gamma) -> None:
        m = self.m
        await asyncio.sleep(max(0.0, m.end + 1 - time.time()))
        if self.adds_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self.adds_task), timeout=60)
            except Exception as e:
                log.error("%s: докупки — %s", m.slug, e)
        pos = [s for s in self.sides.values() if s.shares > 0]
        if not pos and not self.shadow:
            await self.write_path(self.book_winner(), "book")
            return
        if any(s.hedges for s in pos):
            try:
                await self.close_hedges()
            except Exception as e:
                log.error("%s: страховки — %s", m.slug, e)
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
        await self.settle_hedges(w, src)
        for r in self.shadow:
            won = r["side"] == w
            px = min(max(r["ask"], 0.01), 0.99)
            await self.j.write({"event": "shadow_result", "slug": m.slug, "tf": m.tf, "side": r["side"], "mode": r["mode"],
                                "ask": r["ask"], "winner": w, "source": src, "won": won,
                                "roi": round((1 / px if won else 0) - 1 - Cfg.FEE_RATE * (1 - px), 4),
                                "traded_too": r["traded_too"]})
        await self.write_path(w, src)
        if src == "book":
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
                            "add_usdc": round(s.add_usdc, 4), "add_shares": round(s.add_shares, 4),
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
            await self.settle_hedges(g, "gamma", fix=True)


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



def _fnum(v) -> Optional[float]:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _best(levels, pick) -> Optional[float]:
    best = None
    for lv in levels or []:
        try:
            p, sz = float(lv["price"]), float(lv.get("size", 0) or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if sz > 0:
            best = p if best is None else pick(best, p)
    return best


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
            eng.on_quote(ev.get("asset_id"), _best(ev.get("bids"), max), _best(ev.get("asks"), min), now)
        elif et == "price_change":
            for ch in ev.get("price_changes") or []:
                p, sz = _fnum(ch.get("price")), _fnum(ch.get("size"))
                if p is not None and sz is not None and ch.get("side"):
                    eng.on_levels(ch.get("asset_id"), str(ch.get("side")).upper(), p, sz)
                if "best_bid" in ch or "best_ask" in ch:
                    eng.on_quote(ch.get("asset_id"), _fnum(ch.get("best_bid")), _fnum(ch.get("best_ask")), now)
        elif et == "best_bid_ask":
            eng.on_quote(ev.get("asset_id"), _fnum(ev.get("best_bid")), _fnum(ev.get("best_ask")), now)
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
    calc = deposit() * Cfg.STAKE_PCT / 100
    for tf in Cfg.TFS:
        p = tfp(tf)
        log.info("%s %dm | сигнал: сторона была <= %.2f, аск впервые в %.2f–%.2f после %.0f%% окна, отсечка аска %s, рывок: %s | потолок тейкера %.2f",
                 Cfg.ASSETS, tf, p["ml"], p["lo"], p["hi"], p["fr"] * 100, f">= {p['ma']:.2f}" if p["ma"] > 0 else "выкл",
                 f"mid {p['jl']:g} с назад < {p['jm']:.2f}" if p["jm"] > 0 else "выкл", Cfg.CAP_PX)
    log.warning("ОБЪЁМ: депозит $%.2f (база $%g%s) × %g%% = $%.2f → ставка $%.0f (минимум площадки: "
                "%g долей × %.2f, $%g → $%d)", deposit(), Cfg.DEPOSIT,
                (f" + итог {EQUITY.realized:+.2f} по {len(EQUITY.pnl)} сделкам журнала"
                 + (f" с {Cfg.COMPOUND_FROM}" if Cfg.COMPOUND_FROM else "")) if Cfg.COMPOUND else ", без реинвеста",
                Cfg.STAKE_PCT, calc, stake_usdc(), Cfg.MIN_SHARES, max([Cfg.ENTRY_HI, Cfg.CAP_PX] + [tfp(t)["hi"] for t in Cfg.TFS]), Cfg.MIN_USDC,
                stake_floor())
    log.info("исход по книге: %s", f"бид победителя >= {Cfg.BOOK_WIN_PX:g} все последние {Cfg.BOOK_HOLD_S:g} с, "
             "иначе gamma; gamma — сверка" if Cfg.RESOLVE_BY_BOOK else "выкл — только gamma")
    stop = asyncio.Event()
    try:
        import signal as _sig
        loop = asyncio.get_running_loop()
        for sg in (_sig.SIGINT, _sig.SIGTERM):
            loop.add_signal_handler(sg, stop.set)
    except Exception:
        pass
    log.warning("сигнал: %s%s%s", "раз в секунду (последняя котировка секунды)" if Cfg.SIGNAL_1S else "по каждому изменению книги",
                f" | тень: {'по каждому изменению' if Cfg.SIGNAL_1S else 'раз в секунду'} — только журнал" if Cfg.SHADOW else "",
                f" | путь книги → {Cfg.PATH_LOG}" if Cfg.PATH_LOG else "")
    log.info("страховка: %s", (f"BUY обратного исхода по {', '.join(f'{x:g}' for x in Cfg.HEDGE_LEVELS)} на ${Cfg.HEDGE_USDC:g} "
             f"каждая сразу после налива, " + ("снятие по id после конца окна" if Cfg.HEDGE_CANCEL_AT_END else "живут до срока GTD"))
             if Cfg.HEDGE_LEVELS else "выкл")
    log.info("докупки: %s", (", ".join(f"{x:g}" for x in Cfg.ADD_LEVELS) + f" × {Cfg.ADD_SIZE:g} основного налива, "
             f"снятие за {Cfg.ADD_CANCEL_BEFORE_S:g} с до конца") if Cfg.ADD_LEVELS else "выкл")
    log.info("фильтр: %s", ("только запись в журнал; " if Cfg.FILTER_LOG_ONLY else "") + (
        " или ".join(x for x in (f"|BTC − открытие| > {Cfg.MIN_DIST_BP:g} bp ({Cfg.DIST_SRC})" if Cfg.MIN_DIST_BP > 0 else "",
                                 f"отскок от <= {Cfg.MIN_LOW:g} за <= {Cfg.FAST_REC_S:g} с" if Cfg.FAST_REC_S > 0 else "") if x)
        or "выкл"))
    global BN
    if Cfg.MIN_DIST_BP > 0 or Cfg.FAST_REC_S > 0 or Cfg.FILTER_LOG_ONLY:
        BN = Binance(BN_SYM.get(Cfg.ASSETS[0], "BTCUSDT"))
        spawn(BN.run(stop))
        if len(Cfg.ASSETS) > 1:
            log.warning("фильтр по Binance считается по %s — ставь ASSETS=btc", BN.sym)
    clob, gamma, j, stats = Clob(), Gamma(), Journal(Cfg.JOURNAL), Stats()
    seen: Dict[str, asyncio.Task] = {}
    while not stop.is_set():
        now = time.time()
        for tf in Cfg.TFS:
            L = tf * 60
            cur = int(now // L) * L
            for start in (cur, cur + L):
                if start + L <= now or start > now + 60:
                    continue
                for a in Cfg.ASSETS:
                    slug = f"{a}-updown-{tf}m-{start}"
                    if slug in seen:
                        continue
                    mk = await asyncio.to_thread(gamma.market, a, tf, start)
                    if mk is None:
                        continue
                    seen[slug] = spawn(watch(Engine(mk, clob, j, stats), gamma, stop))
        for slug in [s for s, t in seen.items() if t.done() and int(s.rsplit("-", 1)[1]) < now - 7200]:
            seen.pop(slug, None)
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
    log.info("останавливаюсь. %s", stats.line())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
