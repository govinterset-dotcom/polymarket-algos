#!/usr/bin/env python3
"""
path_z.py — BTC 5m/15m maker/taker signals by role + underlying residual (z).

Ядро (robust across z-thresholds 0.4–1.0):
  5m  Leader  | vs_leader   at frac 0.25 / 0.30 / 0.35 / 0.40 / 0.45
  15m Underdog| fav_leader  at frac 0.20 / 0.25 / 0.40 / 0.45 / 0.50
  15m Leader  | vs_leader   at frac 0.20 / 0.25

z = log(close_now / open_window) / (σ_1s * sqrt(remaining_seconds))
state:
  fav  = z >  +Z_THR
  vs   = z <  -Z_THR

PAPER=true по умолчанию. Исполнение — FAK (как в comeback), можно переключить на short GTD.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import websockets

ENV_FILE_USED = ""


def _load_env_file() -> None:
    global ENV_FILE_USED
    explicit = os.environ.get("ENV_FILE", "").strip()
    here = Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] else Path.cwd()
    stem = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else "path_z"
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

    # --- path_z core ---
    Z_THR = _f("Z_THR", 0.55)                 # порог residual
    SIGNAL_MAX_PX = _f("SIGNAL_MAX_PX", 0.95)  # не брать слишком дорогих Leader
    END_GUARD_S = _f("END_GUARD_S", 3.0)
    JOIN_MAX_S = _f("JOIN_MAX_S", 20.0)
    # как в бэктесте: ровно один сигнал на окно; после первого — тишина
    ONE_SIGNAL_PER_WINDOW = _b("ONE_SIGNAL_PER_WINDOW", True)
    # запрет второй стороны в том же окне (даже если ONE_SIGNAL выкл)
    ONE_SIDE_ONLY = _b("ONE_SIDE_ONLY", True)

    DEPOSIT = _f("DEPOSIT", 100.0)
    STAKE_PCT = _f("STAKE_PCT", 3.0)
    MIN_SHARES = _f("MIN_SHARES", 5.0)
    MIN_USDC = _f("MIN_USDC", 1.0)
    COMPOUND = _b("COMPOUND", True)
    COMPOUND_FROM = _env("COMPOUND_FROM", "")
    FEE_RATE = _f("FEE_RATE", 0.07)
    BUILDER_FEE = _f("BUILDER_FEE", 0.01)

    FILL_TIMEOUT_S = _f("FILL_TIMEOUT_S", 5.0)
    RETRY_S = _f("RETRY_S", 0.35)

    RESOLVE_BY_BOOK = _b("RESOLVE_BY_BOOK", True)
    BOOK_WIN_PX = _f("BOOK_WIN_PX", 0.99)
    BOOK_HOLD_S = _f("BOOK_HOLD_S", 3.0)
    RESOLVE_DELAY_S = _f("RESOLVE_DELAY_S", 45.0)
    RESOLVE_TIMEOUT_S = _f("RESOLVE_TIMEOUT_S", 6 * 3600.0)
    WS_STALE_S = _f("WS_STALE_S", 25.0)

    BINANCE_WS = _env("BINANCE_WS", "wss://stream.binance.com:9443/ws")
    BINANCE_REST = _env("BINANCE_REST", "https://api.binance.com")

    JOURNAL = _env("JOURNAL", "path_z.jsonl")
    LOG_LEVEL = _env("LOG_LEVEL", "INFO")
    PATH_LOG = _env("PATH_LOG", "")


# ───────────── robust signals (role + state + frac) ─────────────
@dataclass(frozen=True)
class ZSignal:
    name: str
    tf: int                 # 5 or 15
    kind: str               # "leader" | "underdog"
    state: str              # "vs" | "fav"
    fraction: float
    # optional price pocket (0 = any)
    price_lo: float = 0.0
    price_hi: float = 1.0


_ALL_SIGNALS: tuple = (
    # 5m Leader when BTC already against the current Leader
    ZSignal("5m_L_vs_25", 5, "leader", "vs", 0.25),
    ZSignal("5m_L_vs_30", 5, "leader", "vs", 0.30),
    ZSignal("5m_L_vs_35", 5, "leader", "vs", 0.35),
    ZSignal("5m_L_vs_40", 5, "leader", "vs", 0.40),
    ZSignal("5m_L_vs_45", 5, "leader", "vs", 0.45),
    # 15m Underdog when BTC already favours Leader
    ZSignal("15m_U_fav_20", 15, "underdog", "fav", 0.20),
    ZSignal("15m_U_fav_25", 15, "underdog", "fav", 0.25),
    ZSignal("15m_U_fav_40", 15, "underdog", "fav", 0.40),
    ZSignal("15m_U_fav_45", 15, "underdog", "fav", 0.45),
    ZSignal("15m_U_fav_50", 15, "underdog", "fav", 0.50),
    # 15m Leader | vs
    ZSignal("15m_L_vs_20", 15, "leader", "vs", 0.20),
    ZSignal("15m_L_vs_25", 15, "leader", "vs", 0.25),
)

# DISABLE_SIGNALS=name1,name2  — выключить слабые без правки кода
# Пример: DISABLE_SIGNALS=5m_L_vs_45,15m_U_fav_45,15m_L_vs_25
_disabled = {x.strip() for x in _env("DISABLE_SIGNALS", "").replace(" ", "").split(",") if x.strip()}
SIGNALS: tuple = tuple(s for s in _ALL_SIGNALS if s.name not in _disabled)


log = logging.getLogger("path_z")


# ───────────── Binance live residual ─────────────
class Binance:
    """Live price + open-of-window for residual z."""

    def __init__(self, sym: str = "BTCUSDT"):
        self.sym = sym
        self.price: Optional[float] = None
        self.price_t = 0.0
        self.bars: Dict[int, List[float]] = {}   # minute -> [open, close]
        self.rest_ok: set = set()
        self._rets: List[float] = []             # recent 1s log-returns for σ

    def _trade(self, px: float, t: float) -> None:
        if self.price is not None and self.price > 0 and px > 0:
            self._rets.append(math.log(px / self.price))
            if len(self._rets) > 1200:
                self._rets = self._rets[-900:]
        self.price, self.price_t = px, t
        mnt = int(t // 60) * 60
        b = self.bars.get(mnt)
        if b is None:
            self.bars[mnt] = [px, px]
        elif mnt not in self.rest_ok:
            b[1] = px

    def _rest(self) -> None:
        r = requests.get(f"{Cfg.BINANCE_REST}/api/v3/klines",
                         params={"symbol": self.sym, "interval": "1m", "limit": 40}, timeout=5)
        r.raise_for_status()
        now = time.time()
        for k in r.json():
            mnt = int(k[0]) // 1000
            if mnt + 60 <= now:
                self.bars[mnt] = [float(k[1]), float(k[4])]
                self.rest_ok.add(mnt)
            elif mnt not in self.bars:
                self.bars[mnt] = [float(k[1]), float(k[4])]
        cut = now - 5 * 3600
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
                try:
                    await asyncio.wait_for(stop.wait(), timeout=25)
                except asyncio.TimeoutError:
                    pass
        spawn(rest_loop())
        url = f"{Cfg.BINANCE_WS}/{self.sym.lower()}@aggTrade"
        while not stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, close_timeout=5) as ws:
                    log.info("Binance stream: %s", self.sym)
                    while not stop.is_set():
                        d = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                        if "p" in d:
                            self._trade(float(d["p"]), d["T"] / 1000.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Binance: %s — reconnect", e)
                await asyncio.sleep(1.0)

    def window_open(self, start: int) -> Optional[float]:
        """Open of the minute that contains the window start (or previous closed)."""
        mnt = int(start // 60) * 60
        b = self.bars.get(mnt) or self.bars.get(mnt - 60)
        return b[0] if b else None

    def sigma(self) -> float:
        if len(self._rets) < 60:
            return 1e-4
        return max(float(np_std(self._rets[-900:])), 1e-5)

    def residual_z(self, start: int, now: float, remaining_s: float) -> Optional[float]:
        """z = log(px/open) / (σ * sqrt(remaining))."""
        if self.price is None or now - self.price_t > 8:
            return None
        o = self.window_open(start)
        if not o or o <= 0:
            return None
        r = math.log(self.price / o)
        sig = self.sigma()
        return r / (sig * math.sqrt(max(remaining_s, 1.0)))


def np_std(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


BN: Optional[Binance] = None
BN_SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}


# ───────────── Equity / Journal ─────────────
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
            log.error("equity load: %s", e)

    @property
    def realized(self) -> float:
        return sum(self.pnl.values()) if Cfg.COMPOUND else 0.0

    @property
    def value(self) -> float:
        return Cfg.DEPOSIT + self.realized


EQUITY: Optional[Equity] = None


def stake_floor() -> int:
    hi = min(0.99, Cfg.SIGNAL_MAX_PX)
    return int(math.ceil(max(Cfg.MIN_USDC, Cfg.MIN_SHARES * hi) - 1e-9))


def deposit() -> float:
    return EQUITY.value if EQUITY is not None else Cfg.DEPOSIT


def stake_usdc() -> float:
    calc = int(math.floor(max(0.0, deposit()) * Cfg.STAKE_PCT / 100.0 + 0.5))
    return float(max(calc, stake_floor()))


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
        log.error("bg task: %r", t.exception())


# ───────────── Gamma / Market ─────────────
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
        self.s.headers["User-Agent"] = "path_z/1.0"

    def market(self, asset: str, tf: int, start: int) -> Optional[Market]:
        slug = f"{asset}-updown-{tf}m-{start}"
        try:
            r = self.s.get(f"{Cfg.GAMMA}/markets", params={"slug": slug}, timeout=10)
            r.raise_for_status()
            rows = r.json()
            rows = rows if isinstance(rows, list) else rows.get("data", [])
            if not rows:
                return None
            row = rows[0]
            ids = row.get("clobTokenIds")
            outs = row.get("outcomes")
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
            r = self.s.get(f"{Cfg.GAMMA}/markets", params={"slug": slug, "closed": "true"}, timeout=10)
            rows = r.json()
            rows = rows if isinstance(rows, list) else rows.get("data", [])
            if not rows:
                return None
            outs = rows[0].get("outcomes")
            px = rows[0].get("outcomePrices")
            outs = json.loads(outs) if isinstance(outs, str) else outs
            px = [float(x) for x in (json.loads(px) if isinstance(px, str) else px or [])]
            if not outs or not px or max(px) < 0.99:
                return None
            return str(outs[px.index(max(px))])
        except Exception:
            return None


# ───────────── CLOB ─────────────
BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"


class Clob:
    def __init__(self):
        self.enabled = not Cfg.PAPER
        self.client = None
        self.m = None
        if not self.enabled:
            return
        try:
            import py_clob_client_v2 as m
        except ImportError as e:
            raise SystemExit("нужен py_clob_client_v2: %s" % e)
        if not Cfg.PK:
            raise SystemExit("PK не задан, PAPER=false")
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
        log.info("CLOB LIVE")

    def buy_fak(self, token: str, usdc: float, cap: float, tick: str) -> dict:
        m = self.m
        try:
            args = m.MarketOrderArgs(token_id=token, amount=round(usdc, 2), side=m.Side.BUY,
                                     order_type=m.OrderType.FAK, price=round(cap, 6))
            signed = self.client.create_market_order(args, m.PartialCreateOrderOptions(tick_size=tick))
            r = self.client.post_order(signed, m.OrderType.FAK)
            ok = bool(r and (r.get("success") or r.get("orderID") or r.get("id") or r.get("takingAmount")))
            # extract fill if present
            usdc_f = float(r.get("makingAmount") or r.get("usdc") or 0) if r else 0
            sh_f = float(r.get("takingAmount") or r.get("shares") or 0) if r else 0
            if usdc_f == 0 and sh_f == 0 and ok:
                # fallback: assume full
                usdc_f, sh_f = usdc, usdc / max(cap, 1e-6)
            return {"ok": ok, "usdc": usdc_f, "shares": sh_f, "resp": r}
        except Exception as e:
            return {"ok": False, "err": str(e)[:250]}


# ───────────── Engine ─────────────
@dataclass
class SideState:
    name: str
    usdc: float = 0.0
    shares: float = 0.0
    fee: float = 0.0
    fired: set = field(default_factory=set)   # signal names already used this window


def book_winner_tail(tail, end: float) -> Optional[str]:
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
    if all(ub is not None and ub >= thr for _, ub, _ in seg):
        return "Up"
    if all(db is not None and db >= thr for _, _, db in seg):
        return "Down"
    return None


class Engine:
    def __init__(self, m: Market, clob: Clob, j: Journal, stats: "Stats"):
        self.m = m
        self.clob = clob
        self.j = j
        self.stats = stats
        self.sides = {"Up": SideState("Up"), "Down": SideState("Down")}
        self.best_bid_up: Optional[float] = None
        self.best_ask_up: Optional[float] = None
        self.tail: List[Tuple[float, Optional[float], Optional[float]]] = []
        self.path_b: List[Optional[float]] = [None] * m.length
        self.path_a: List[Optional[float]] = [None] * m.length
        self.joined = time.time()
        self.window_fired = False          # ONE_SIGNAL_PER_WINDOW
        self.locked_side: Optional[str] = None  # ONE_SIDE_ONLY

    def on_quote(self, asset_id: str, bid: Optional[float], ask: Optional[float], now: float) -> None:
        if asset_id == self.m.tokens.get("Up"):
            if bid is not None:
                self.best_bid_up = bid
            if ask is not None:
                self.best_ask_up = ask
            # synthetic Down
            bid_d = (1.0 - self.best_ask_up) if self.best_ask_up is not None else None
            ask_d = (1.0 - self.best_bid_up) if self.best_bid_up is not None else None
            self.tail.append((now, self.best_bid_up, bid_d))
            if len(self.tail) > 400:
                self.tail = self.tail[-300:]
            el = int(now - self.m.start)
            if 0 <= el < self.m.length:
                self.path_b[el] = self.best_bid_up
                self.path_a[el] = self.best_ask_up

    def mid_up(self) -> Optional[float]:
        if self.best_bid_up is None or self.best_ask_up is None:
            return None
        return (self.best_bid_up + self.best_ask_up) / 2.0

    def role_and_asks(self) -> Optional[Tuple[str, float, str, float]]:
        """(leader_side, leader_ask, under_side, under_ask)"""
        mid = self.mid_up()
        if mid is None or abs(mid - 0.5) < 1e-9:
            return None
        if mid > 0.5:
            return "Up", self.best_ask_up, "Down", 1.0 - self.best_bid_up
        return "Down", 1.0 - self.best_bid_up, "Up", self.best_ask_up

    async def maybe_signal(self, now: float) -> None:
        if Cfg.ONE_SIGNAL_PER_WINDOW and self.window_fired:
            return
        if now >= self.m.end - Cfg.END_GUARD_S:
            return
        el = now - self.m.start
        frac = el / self.m.length
        remaining = self.m.end - now

        if BN is None:
            return
        z = BN.residual_z(self.m.start, now, remaining)
        if z is None:
            return
        if z > Cfg.Z_THR:
            state = "fav"
        elif z < -Cfg.Z_THR:
            state = "vs"
        else:
            return  # neutral — no trade

        ra = self.role_and_asks()
        if ra is None:
            return
        L_side, L_ask, U_side, U_ask = ra

        for rule in SIGNALS:
            if rule.tf != self.m.tf:
                continue
            if rule.state != state:
                continue
            # time window ±1.8 s around target
            target_t = self.m.start + rule.fraction * self.m.length
            if abs(now - target_t) > 1.8:
                continue
            if rule.name in self.sides["Up"].fired or rule.name in self.sides["Down"].fired:
                continue

            if rule.kind == "leader":
                side, ask = L_side, L_ask
            else:
                side, ask = U_side, U_ask

            if Cfg.ONE_SIDE_ONLY and self.locked_side is not None and side != self.locked_side:
                continue
            if ask is None or ask > Cfg.SIGNAL_MAX_PX + 1e-9:
                continue
            if ask < rule.price_lo - 1e-9 or ask >= rule.price_hi - 1e-9:
                continue

            # fire (and lock window / side like backtest)
            self.sides[side].fired.add(rule.name)
            self.window_fired = True
            self.locked_side = side
            await self.enter(side, ask, rule, z, frac, now)
            return  # one shot per call; ONE_SIGNAL blocks further calls

    async def enter(self, side: str, ask: float, rule: ZSignal, z: float, frac: float, now: float) -> None:
        m = self.m
        s = self.sides[side]
        usdc = stake_usdc()
        cap = min(ask + 0.01, Cfg.SIGNAL_MAX_PX, 0.99)  # small slippage allowance for FAK
        info = dict(event="signal", signal=rule.name, kind=rule.kind, state=rule.state,
                    slug=m.slug, tf=m.tf, side=side, frac=round(frac, 3),
                    ask=round(ask, 4), z=round(z, 3), usdc=usdc, deposit=round(deposit(), 2))
        await self.j.write(info)

        if not self.clob.enabled:
            px = ask
            sh = usdc / px
            fee = Cfg.FEE_RATE * px * (1 - px) * sh + Cfg.BUILDER_FEE * usdc
            s.usdc += usdc
            s.shares += sh
            s.fee += fee
            await self.j.write({"event": "fill", "slug": m.slug, "side": side, "usdc": usdc,
                                "shares": round(sh, 4), "avg_px": round(px, 4), "fee": round(fee, 4),
                                "signal": rule.name, "paper": True})
            log.warning("%s %s %s z=%.2f ask=%.3f → paper $%.0f", m.slug, rule.name, side, z, ask, usdc)
            return

        # live FAK with short retry
        deadline = time.time() + Cfg.FILL_TIMEOUT_S
        tries = 0
        while time.time() < deadline and m.end - time.time() > Cfg.END_GUARD_S / 2:
            tries += 1
            r = await asyncio.to_thread(self.clob.buy_fak, m.tokens[side], usdc, cap, m.tick)
            await self.j.write({"event": "take", "slug": m.slug, "side": side, "try": tries,
                                "signal": rule.name, **{k: v for k, v in r.items() if k != "resp"}})
            if r.get("ok") and r.get("shares", 0) > 0:
                fill_usdc = float(r["usdc"]) or usdc
                fill_sh = float(r["shares"])
                px = fill_usdc / fill_sh if fill_sh else cap
                fee = Cfg.FEE_RATE * px * (1 - px) * fill_sh + Cfg.BUILDER_FEE * fill_usdc
                s.usdc += fill_usdc
                s.shares += fill_sh
                s.fee += fee
                await self.j.write({"event": "fill", "slug": m.slug, "side": side,
                                    "usdc": round(fill_usdc, 4), "shares": round(fill_sh, 4),
                                    "avg_px": round(px, 4), "fee": round(fee, 4),
                                    "signal": rule.name, "paper": False, "try": tries})
                log.warning("%s %s %s z=%.2f → filled %.2f @ %.3f", m.slug, rule.name, side, z, fill_sh, px)
                return
            await asyncio.sleep(Cfg.RETRY_S)
        await self.j.write({"event": "no_fill", "slug": m.slug, "side": side, "signal": rule.name, "tries": tries})
        log.info("%s %s no fill", m.slug, rule.name)

    def book_winner(self) -> Optional[str]:
        return book_winner_tail(self.tail, self.m.end)

    async def finish(self, gamma: Gamma) -> None:
        m = self.m
        await asyncio.sleep(max(0.0, m.end + 1 - time.time()))
        pos = [s for s in self.sides.values() if s.shares > 0]
        if not pos:
            return
        w = self.book_winner() if Cfg.RESOLVE_BY_BOOK else None
        src = "book" if w else "gamma"
        if not w:
            await asyncio.sleep(Cfg.RESOLVE_DELAY_S)
            deadline = time.time() + Cfg.RESOLVE_TIMEOUT_S
            while time.time() < deadline and not w:
                w = await asyncio.to_thread(gamma.winner, m.slug)
                if not w:
                    await asyncio.sleep(25)
            if not w:
                await self.j.write({"event": "unresolved", "slug": m.slug})
                return
        for s in pos:
            won = s.name == w
            pnl = (s.shares if won else 0.0) - s.usdc - s.fee
            self.stats.add(won, s.usdc, pnl)
            await self.j.write({"event": "resolved", "slug": m.slug, "side": s.name, "winner": w,
                                "source": src, "won": won, "usdc": round(s.usdc, 4),
                                "shares": round(s.shares, 4), "fee": round(s.fee, 4), "pnl": round(pnl, 4),
                                "paper": not self.clob.enabled})
            log.warning("РЕЗОЛВ %s %s → %s %+.2f | %s", m.slug, s.name, "WIN" if won else "loss", pnl, self.stats.line())


class Stats:
    def __init__(self):
        self.n = self.w = 0
        self.usd = self.pnl = 0.0

    def add(self, won, usd, pnl):
        self.n += 1
        self.w += int(bool(won))
        self.usd += usd
        self.pnl += pnl

    def line(self) -> str:
        wr = self.w / self.n if self.n else 0
        return f"n={self.n} wr={wr:.0%} pnl=${self.pnl:+.2f}"


# ───────────── WS handlers ─────────────
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
            eng.on_quote(ev.get("asset_id"), _best(ev.get("bids"), max), _best(ev.get("asks"), min), now)
        elif et == "price_change":
            for ch in ev.get("price_changes") or []:
                if "best_bid" in ch or "best_ask" in ch:
                    eng.on_quote(ch.get("asset_id"), _fnum(ch.get("best_bid")), _fnum(ch.get("best_ask")), now)
        elif et == "best_bid_ask":
            eng.on_quote(ev.get("asset_id"), _fnum(ev.get("best_bid")), _fnum(ev.get("best_ask")), now)
    # after every update try signals
    asyncio.create_task(eng.maybe_signal(now))


async def _pinger(ws, stop: asyncio.Event, every: float, until: float) -> None:
    try:
        while not stop.is_set() and time.time() < until:
            await asyncio.sleep(every)
            await ws.send("PING")
    except Exception:
        pass


async def watch(eng: Engine, gamma: Gamma, stop: asyncio.Event) -> None:
    m = eng.m
    fin = spawn(eng.finish(gamma))
    toks = list(m.tokens.values())
    log.info("watch %s → %s", m.slug, time.strftime("%H:%M:%S", time.gmtime(m.end)))
    while not stop.is_set() and time.time() < m.end:
        try:
            async with websockets.connect(Cfg.WS_URL, ping_interval=None, close_timeout=5, max_size=2**23) as ws:
                await ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
                pinger = spawn(_pinger(ws, stop, 10.0, m.end))
                last = time.time()
                try:
                    while not stop.is_set() and time.time() < m.end:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, min(Cfg.WS_STALE_S, m.end - time.time())))
                        except asyncio.TimeoutError:
                            if time.time() - last > Cfg.WS_STALE_S:
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
        log.error("%s finish: %s", m.slug, e)


# ───────────── main ─────────────
async def main() -> None:
    logging.basicConfig(level=getattr(logging, Cfg.LOG_LEVEL.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    log.info("config: %s", ENV_FILE_USED or "defaults")
    log.warning("MODE: %s", "PAPER" if Cfg.PAPER else "LIVE")
    log.info("Z_THR=%.2f  active signals=%d  ONE_SIGNAL=%s  ONE_SIDE=%s",
             Cfg.Z_THR, len(SIGNALS), Cfg.ONE_SIGNAL_PER_WINDOW, Cfg.ONE_SIDE_ONLY)
    if _disabled:
        log.warning("DISABLED (%d): %s", len(_disabled), ", ".join(sorted(_disabled)))
    else:
        log.warning("DISABLE_SIGNALS empty — все %d правил активны", len(_ALL_SIGNALS))
    for s in SIGNALS:
        log.info("  ON  %s  %dm  %s|%s  frac=%.2f", s.name, s.tf, s.kind, s.state, s.fraction)

    global EQUITY, BN
    EQUITY = Equity()
    if Cfg.COMPOUND:
        EQUITY.load(Path(Cfg.JOURNAL))
    log.warning("deposit $%.2f  stake $%.0f (%.1f%%)", deposit(), stake_usdc(), Cfg.STAKE_PCT)

    stop = asyncio.Event()
    try:
        import signal as _sig
        loop = asyncio.get_running_loop()
        for sg in (_sig.SIGINT, _sig.SIGTERM):
            loop.add_signal_handler(sg, stop.set)
    except Exception:
        pass

    BN = Binance(BN_SYM.get(Cfg.ASSETS[0], "BTCUSDT"))
    spawn(BN.run(stop))

    clob = Clob()
    gamma, j, stats = Gamma(), Journal(Cfg.JOURNAL), Stats()
    seen: Dict[str, asyncio.Task] = {}

    while not stop.is_set():
        now = time.time()
        for tf in Cfg.TFS:
            L = tf * 60
            cur = int(now // L) * L
            for start in (cur, cur + L):
                if start + L <= now or start > now + 90:
                    continue
                for a in Cfg.ASSETS:
                    slug = f"{a}-updown-{tf}m-{start}"
                    if slug in seen:
                        continue
                    mk = await asyncio.to_thread(gamma.market, a, tf, start)
                    if mk is None:
                        continue
                    eng = Engine(mk, clob, j, stats)
                    seen[slug] = spawn(watch(eng, gamma, stop))
        for slug in [s for s, t in seen.items() if t.done() and int(s.rsplit("-", 1)[1]) < now - 7200]:
            seen.pop(slug, None)
        try:
            await asyncio.wait_for(stop.wait(), timeout=4)
        except asyncio.TimeoutError:
            pass
    log.info("stop. %s", stats.line())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
