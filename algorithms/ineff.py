#!/usr/bin/env python3
"""
ineff.py — торговля неэффективности прайсинга Polymarket BTC up/down 15m.

Логики (по бэктесту path + секундный BTC, 3 временных фолда):
  1) EDGE residual: fair(spot) − ask_side > EDGE_MIN
     fair = Φ( ln(S/K) / (σ √τ) ), K = цена BTC на открытии окна, σ — 1m realized.
  2) SPOT_LEAD (опционально): за SPOT_LOOK_S спот уже ушёл в сторону ставки
     на SPOT_MOVE_MIN (доля), и edge всё ещё > EDGE_MIN.
  3) FALSE_DUMP / TRUE_DUMP:
     — разрешать «дешёвую» сторону при mid экстремуме только если fair НЕ подтверждает
       обвал (false dump); true dump (mid и fair оба в зоне) — skip.
  4) MODE:
     — residual  : только edge
     — lead      : edge + spot lead
     — false_dump: только ложный обвал книги vs fair
     — all       : lead ИЛИ (edge≥EDGE_STRONG) ИЛИ false_dump; true dump всегда режет
  5) ONE_PER_REASON=true (по умолчанию): в одном окне каждый reason может сработать
     не больше одного раза. Разные reason не блокируют друг друга.
  6) Текущий боевой срез (по path): lead + false_dump вместо residual edge.
     ENABLE_EDGE/ENABLE_EDGE_STRONG=false — edge/edge_strong выкл.
     lead: ask < LEAD_MAX_ASK (дефолт 0.60); false_dump без верхнего коридора (входы обычно <0.30).

Вход: тейкер FAK по ask (потолок ask+SLIP). Без сетки лимиток.
Объём: депозит × STAKE_PCT %, до центов, не ниже минимума площадки: max(MIN_USDC, MIN_SHARES × потолок цены ЭТОГО входа).
Каждый reason (lead / false_dump / edge_strong / edge) — изолированный счёт:
  DEPOSIT_<REASON>, STAKE_PCT_<REASON>, COMPOUND_<REASON> (fallback на DEPOSIT / STAKE_PCT / COMPOUND).
  Компаундинг и ставка считаются только по закрытым сделкам этого reason.
Конфиг: ineff.env | ENV_FILE=...  PAPER=true по умолчанию.
БОЕВОЙ режим (PAPER=false) — адаптер как в crossreb: проверка L2-авторизации на старте (нет авторизации → выход,
а не тихий бумажный режим), цена лимита по тику, сумма вниз до центов, реальные суммы fill из ответа биржи (ничего
не выдумывается), builder-комиссия в P&L, лимит неудач на окно (MAX_FAILS), блокировка reason при неизвестном исходе.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import signal as _signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import requests
import websockets

# ───────────────────────────────────────────────────────────── env

ENV_FILE_USED = ""


def _load_env() -> None:
    global ENV_FILE_USED
    explicit = os.environ.get("ENV_FILE", "").strip()
    here = Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] else Path.cwd()
    stem = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else "ineff"
    cands = [Path(explicit)] if explicit else [
        here / f"{stem}.env", here / ".env", Path.cwd() / f"{stem}.env", Path.cwd() / ".env",
    ]
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


_load_env()


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
    BINANCE_REST = _env("BINANCE_REST", "https://api.binance.com")
    BINANCE_WS = _env("BINANCE_WS", "wss://stream.binance.com:9443/stream")

    ASSETS = [a.strip().lower() for a in _env("ASSETS", "btc").split(",") if a.strip()]
    TFS = [int(x) for x in _env("TFS", "15").replace(" ", "").split(",") if x]

    # --- inefficiency logic ---
    # residual | lead | false_dump | all
    MODE = _env("MODE", "all").lower()
    EDGE_MIN = _f("EDGE_MIN", 0.05)          # fair − ask
    EDGE_STRONG = _f("EDGE_STRONG", 0.10)    # для MODE=all без обязательного lead
    # live-фильтр цены входа (ask): вне диапазона — не входим (edge / edge_strong)
    MIN_ASK_PX = _f("MIN_ASK_PX", 0.30)      # для edge/edge_strong если включены
    MAX_ASK_PX = _f("MAX_ASK_PX", 0.45)
    ENABLE_LEAD = _b("ENABLE_LEAD", True)
    ENABLE_FALSE_DUMP = _b("ENABLE_FALSE_DUMP", True)
    ENABLE_EDGE = _b("ENABLE_EDGE", False)          # residual edge выкл в текущем срезе
    ENABLE_EDGE_STRONG = _b("ENABLE_EDGE_STRONG", False)
    LEAD_MAX_ASK = _f("LEAD_MAX_ASK", 0.60)         # lead: не брать дороже
    LEAD_MIN_ASK = _f("LEAD_MIN_ASK", 0.0)
    SPOT_LOOK_S = _i("SPOT_LOOK_S", 300)     # окно хода спота (5м)
    SPOT_MOVE_MIN = _f("SPOT_MOVE_MIN", 0.002)  # 0.2%
    REQUIRE_LEAD = _b("REQUIRE_LEAD", False)  # если true — только lead-сигналы
    # false dump: mid экстремум, fair не подтверждает
    MID_DUMP = _f("MID_DUMP", 0.25)
    FAIR_ALIVE = _f("FAIR_ALIVE", 0.45)      # false up-dump: mid<MID_DUMP & fair>FAIR_ALIVE
    MID_RALLY = _f("MID_RALLY", 0.75)
    FAIR_WEAK = _f("FAIR_WEAK", 0.55)        # false down-side: mid>MID_RALLY & fair<FAIR_WEAK
    SKIP_TRUE_DUMP = _b("SKIP_TRUE_DUMP", True)
    TRUE_DUMP_FAIR = _f("TRUE_DUMP_FAIR", 0.30)  # mid<MID_DUMP & fair<TRUE_DUMP_FAIR
    TRUE_RALLY_FAIR = _f("TRUE_RALLY_FAIR", 0.70)

    # fair model
    SIG_LOOKBACK_MIN = _i("SIG_LOOKBACK_MIN", 60)  # минут для σ
    MIN_TAU_S = _f("MIN_TAU_S", 30.0)
    MAX_SPREAD = _f("MAX_SPREAD", 0.08)
    REM_MIN_FRAC = _f("REM_MIN_FRAC", 0.15)  # не входить в последние 15% окна
    ONE_SIDE_PER_WINDOW = _b("ONE_SIDE_PER_WINDOW", False)
    # true: каждый reason раз на окно; lead/false_dump/edge не душат друг друга
    ONE_PER_REASON = _b("ONE_PER_REASON", True)
    COOLDOWN_S = _f("COOLDOWN_S", 5.0)       # пауза между входами (разные reason)

    # size / exec
    # глобальные дефолты; per-reason: DEPOSIT_LEAD, STAKE_PCT_FALSE_DUMP, COMPOUND_EDGE, ...
    DEPOSIT = _f("DEPOSIT", 100.0)
    STAKE_PCT = _f("STAKE_PCT", 5.0)
    COMPOUND = _b("COMPOUND", True)
    COMPOUND_FROM = _env("COMPOUND_FROM", "")  # unix или 2026-10-29T12:00 (UTC); пусто = весь журнал
    REASONS = ("lead", "false_dump", "edge_strong", "edge")
    SLIP = _f("SLIP", 0.02)
    CAP_PX = _f("CAP_PX", 0.97)
    FEE_RATE = _f("FEE_RATE", 0.07)
    BUILDER_FEE = _f("BUILDER_FEE", 0.01)     # builder-комиссия: доля от суммы покупки (сверх комиссии биржи); 0 = выкл
    MAX_FAILS = int(_f("MAX_FAILS", 2))       # неудач FAK на (сторона, reason) в окне, потом блок
    FAIL_BACKOFF_S = _f("FAIL_BACKOFF_S", 2.0)  # пауза после неудачи
    # --- FAK: ретраи при "no orders found to match" (можно переопределить в env, но не обязательно) ---
    FAK_RETRIES = int(_f("FAK_RETRIES", 3))             # доп. попытки в рамках одного входа
    FAK_RETRY_DELAY_S = _f("FAK_RETRY_DELAY_S", 0.10)   # пауза перед ретраем
    NOMATCH_ROUNDS = int(_f("NOMATCH_ROUNDS", 12))      # раундов no-match до блокировки reason в окне
    NOMATCH_BACKOFF_S = _f("NOMATCH_BACKOFF_S", 0.4)    # пауза между раундами
    FILL_TIMEOUT_S = _f("FILL_TIMEOUT_S", 12.0)
    END_GUARD_S = _f("END_GUARD_S", 20.0)
    MIN_SHARES = _f("MIN_SHARES", 5.0)
    MIN_USDC = _f("MIN_USDC", 1.05)      # marketable BUY: минимум $1 на бирже, +5¢ запаса на округление

    RESOLVE_BY_BOOK = _b("RESOLVE_BY_BOOK", True)
    BOOK_WIN_PX = _f("BOOK_WIN_PX", 0.99)
    BOOK_HOLD_S = _f("BOOK_HOLD_S", 3.0)
    RESOLVE_DELAY_S = _f("RESOLVE_DELAY_S", 60.0)
    RESOLVE_TIMEOUT_S = _f("RESOLVE_TIMEOUT_S", 7200.0)
    WS_STALE_S = _f("WS_STALE_S", 25.0)
    PREROLL_S = _f("PREROLL_S", 20.0)
    DISCOVERY_EVERY_S = _f("DISCOVERY_EVERY_S", 10.0)
    EVAL_EVERY_S = _f("EVAL_EVERY_S", 1.0)

    JOURNAL = _env("JOURNAL", "ineff.jsonl")
    LOG_LEVEL = _env("LOG_LEVEL", "INFO")
    BN_STALL_S = _f("BN_STALL_S", 15.0)


log = logging.getLogger("ineff")


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _fnum(v: Any) -> Optional[float]:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


# ───────────────────────────────────────────────────────────── journal / stats

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


def _reason_key(reason: str) -> str:
    return (reason or "").strip().lower().replace("-", "_").replace(" ", "_")


def _deposit_for(reason: str) -> float:
    """DEPOSIT_<REASON> из env, иначе DEPOSIT."""
    k = "DEPOSIT_" + _reason_key(reason).upper()
    v = _env(k)
    return float(v) if v else Cfg.DEPOSIT


def _stake_pct_for(reason: str) -> float:
    k = "STAKE_PCT_" + _reason_key(reason).upper()
    v = _env(k)
    return float(v) if v else Cfg.STAKE_PCT


def _compound_for(reason: str) -> bool:
    k = "COMPOUND_" + _reason_key(reason).upper()
    v = _env(k).lower()
    if not v:
        return Cfg.COMPOUND
    return v in ("1", "true", "yes", "on")


class Equity:
    """Изолированный депозит/P&L по каждому reason.
    Ключ записи — (reason, slug, side): resolved задаёт итог, resolved_fix перезаписывает.
    Ставка reason = DEPOSIT_<REASON> + sum(pnl этого reason) при COMPOUND_<REASON>."""

    EVENTS = ("resolved", "resolved_fix")

    def __init__(self):
        # reason -> {(slug, side): pnl}
        self.by_reason: Dict[str, Dict[tuple, float]] = {r: {} for r in Cfg.REASONS}
        self.since = _ts_from(Cfg.COMPOUND_FROM)

    def on_rec(self, r: dict) -> None:
        if r.get("event") not in self.EVENTS or r.get("pnl") is None:
            return
        if float(r.get("ts") or time.time()) < self.since:
            return
        reason = _reason_key(str(r.get("reason") or ""))
        if not reason:
            return
        if reason not in self.by_reason:
            self.by_reason[reason] = {}
        self.by_reason[reason][(r.get("slug"), r.get("side"))] = float(r["pnl"])

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

    def realized(self, reason: str) -> float:
        reason = _reason_key(reason)
        if not _compound_for(reason):
            return 0.0
        return sum(self.by_reason.get(reason, {}).values())

    def value(self, reason: str) -> float:
        return _deposit_for(reason) + self.realized(reason)

    def n_trades(self, reason: str) -> int:
        return len(self.by_reason.get(_reason_key(reason), {}))


EQUITY: Optional[Equity] = None


def deposit(reason: str = "") -> float:
    if EQUITY is not None and reason:
        return EQUITY.value(reason)
    return Cfg.DEPOSIT


def stake_usdc(reason: str = "", cap: Optional[float] = None) -> float:
    """Депозит reason × STAKE_PCT_reason %, но не ниже минимума площадки — всё с точностью до цента.
    Минимум = max(MIN_USDC, MIN_SHARES × cap): при цене до cap это даёт ≥ MIN_SHARES долей и ≥ MIN_USDC в долларах.
    cap — реальный потолок цены ЭТОГО входа (ask+SLIP); без него — худший случай CAP_PX."""
    cap = Cfg.CAP_PX if cap is None else cap
    dep = deposit(reason) if reason else Cfg.DEPOSIT
    pct = _stake_pct_for(reason) if reason else Cfg.STAKE_PCT
    calc = math.floor(max(0.0, dep) * pct + 1e-9) / 100.0                 # проценты → центы, вниз
    floor = math.ceil(max(Cfg.MIN_USDC, Cfg.MIN_SHARES * cap) * 100 - 1e-9) / 100.0   # вверх до цента
    return round(max(calc, floor), 2)


def reason_cap_max(reason: str) -> float:
    """Максимальный cap, до которого реально доходит вход этого reason (для расчёта минимальной ставки в логе старта)."""
    r = _reason_key(reason)
    if r == "lead":
        c = Cfg.LEAD_MAX_ASK + Cfg.SLIP
    elif r == "false_dump":
        c = Cfg.MID_DUMP + Cfg.MAX_SPREAD / 2.0 + Cfg.SLIP     # mid < MID_DUMP, ask ≤ mid + spread/2
    else:
        c = Cfg.MAX_ASK_PX + Cfg.SLIP
    return min(Cfg.CAP_PX, c)


class Stats:
    def __init__(self):
        self.n = 0
        self.wins = 0
        self.usd = 0.0
        self.pnl = 0.0

    def add(self, usd: float, pnl: float, won: bool) -> None:
        self.n += 1
        self.usd += usd
        self.pnl += pnl
        if won:
            self.wins += 1

    def line(self) -> str:
        wr = (self.wins / self.n * 100) if self.n else 0.0
        return f"сделок {self.n}, WR {wr:.0f}%, вложено ${self.usd:.2f}, P&L ${self.pnl:+.2f}"


def spawn(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    t.add_done_callback(_done)
    return t


def _done(t: asyncio.Task) -> None:
    if not t.cancelled() and t.exception():
        log.error("bg: %r", t.exception())


# ───────────────────────────────────────────────────────────── markets

@dataclass
class Market:
    asset: str
    tf: int
    start: int
    slug: str
    tokens: Dict[str, str]
    tick: str
    cond: str = ""

    @property
    def length(self) -> int:
        return self.tf * 60

    @property
    def end(self) -> int:
        return self.start + self.length


class Gamma:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "ineff/1.0"

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
            ids, outs = row.get("clobTokenIds"), row.get("outcomes")
            ids = json.loads(ids) if isinstance(ids, str) else ids
            outs = json.loads(outs) if isinstance(outs, str) else outs
            tokens = {str(o): str(t) for o, t in zip(outs or [], ids or [])}
            if "Up" not in tokens or "Down" not in tokens:
                return None
            return Market(
                asset, tf, start, slug, tokens,
                str(row.get("orderPriceMinTickSize") or "0.01"),
                str(row.get("conditionId") or ""),
            )
        except Exception as e:
            log.warning("gamma %s: %s", slug, e)
            return None

    def winner(self, slug: str) -> Optional[str]:
        try:
            for closed in (True, False):
                p = {"slug": slug}
                if closed:
                    p["closed"] = "true"
                r = self.s.get(f"{Cfg.GAMMA}/markets", params=p, timeout=10)
                r.raise_for_status()
                rows = r.json()
                rows = rows if isinstance(rows, list) else rows.get("data", [])
                if not rows:
                    continue
                outs, px = rows[0].get("outcomes"), rows[0].get("outcomePrices")
                outs = json.loads(outs) if isinstance(outs, str) else outs
                px = [float(x) for x in (json.loads(px) if isinstance(px, str) else px or [])]
                if outs and px and max(px) >= 0.99:
                    return str(outs[px.index(max(px))])
        except Exception as e:
            log.debug("resolve %s: %s", slug, e)
        return None


# ───────────────────────────────────────────────────────────── Binance spot

class Spot:
    """Секундные/минутные цены BTC + realized σ для fair."""

    SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}

    def __init__(self, assets: List[str]):
        self.assets = assets
        self.sess = requests.Session()
        # asset -> deque of (ts, price) last ~2h seconds from trades agg
        self.ticks: Dict[str, Deque[Tuple[float, float]]] = {a: deque(maxlen=7200) for a in assets}
        self.minute: Dict[str, Deque[Tuple[int, float]]] = {a: deque(maxlen=500) for a in assets}
        self.last_px: Dict[str, float] = {}
        self.last_ts: Dict[str, float] = {}
        self.lock = asyncio.Lock()

    def _sym(self, asset: str) -> str:
        return self.SYM.get(asset, f"{asset.upper()}USDT")

    def bootstrap(self, asset: str) -> None:
        """1m klines for sigma + last price."""
        try:
            r = self.sess.get(
                f"{Cfg.BINANCE_REST}/api/v3/klines",
                params={"symbol": self._sym(asset), "interval": "1m", "limit": max(120, Cfg.SIG_LOOKBACK_MIN + 10)},
                timeout=10,
            )
            r.raise_for_status()
            rows = []
            for k in r.json():
                open_ms, close = int(k[0]), float(k[4])
                rows.append((open_ms // 1000, close))
            self.minute[asset] = deque(rows, maxlen=500)
            if rows:
                self.last_px[asset] = rows[-1][1]
                self.last_ts[asset] = float(rows[-1][0] + 60)
            log.info("spot bootstrap %s: %d min bars, last=%.2f", asset, len(rows), self.last_px.get(asset, 0))
        except Exception as e:
            log.error("spot bootstrap %s: %s", asset, e)

    def on_trade(self, asset: str, ts: float, px: float) -> None:
        self.ticks[asset].append((ts, px))
        self.last_px[asset] = px
        self.last_ts[asset] = ts
        m = int(ts) // 60 * 60
        mq = self.minute[asset]
        if mq and mq[-1][0] == m:
            mq[-1] = (m, px)
        else:
            mq.append((m, px))

    def price(self, asset: str) -> Optional[float]:
        return self.last_px.get(asset)

    def stale(self, asset: str, now: float) -> bool:
        t = self.last_ts.get(asset)
        return t is None or (now - t) > Cfg.BN_STALL_S

    def sigma_1m(self, asset: str) -> Optional[float]:
        mq = list(self.minute[asset])
        need = Cfg.SIG_LOOKBACK_MIN
        if len(mq) < max(20, need // 2):
            return None
        closes = [c for _, c in mq[-need:]]
        if len(closes) < 15:
            return None
        if len(closes) < 16:
            return None
        logs = [math.log(c) for c in closes if c > 0]
        if len(logs) < 16:
            return None
        rets = [logs[i] - logs[i - 1] for i in range(1, len(logs))]
        if len(rets) < 10:
            return None
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / len(rets)
        s = math.sqrt(var)
        return s if s > 1e-8 else None

    def ret(self, asset: str, look_s: float, now: float) -> Optional[float]:
        """Доходность спота за look_s секунд назад → now."""
        px = self.last_px.get(asset)
        if px is None:
            return None
        ticks = self.ticks[asset]
        if len(ticks) < 2:
            # fallback minutes
            mq = list(self.minute[asset])
            if len(mq) < 2:
                return None
            target = now - look_s
            past = None
            for ts, c in mq:
                if ts <= target:
                    past = c
            if past is None or past <= 0:
                return None
            return px / past - 1.0
        target = now - look_s
        past = None
        for ts, c in ticks:
            if ts <= target:
                past = c
        if past is None:
            past = ticks[0][1]
        if past <= 0:
            return None
        return px / past - 1.0

    def strike_at(self, asset: str, start: int) -> Optional[float]:
        """Цена BTC ровно на старте окна (раньше брался close свечи m0 = цена через 60с → K сдвинут).
        1) последний тик ≤ start, если тики покрывают старт; 2) close минутной свечи m0−60;
        3) нет данных → None (сигнал пропускается, а не S==K)."""
        ticks = self.ticks[asset]
        if ticks and ticks[0][0] <= start:
            past = None
            for ts, c in ticks:
                if ts <= start:
                    past = c
                else:
                    break
            if past:
                return past
        m_prev = start // 60 * 60 - 60
        for ts, c in self.minute[asset]:
            if ts == m_prev:
                return c
        return None

    def fair_up(self, asset: str, start: int, now: float, end: float) -> Optional[dict]:
        S = self.price(asset)
        K = self.strike_at(asset, start)
        sig = self.sigma_1m(asset)
        if S is None or K is None or K <= 0 or sig is None:
            return None
        tau = max(Cfg.MIN_TAU_S, end - now)
        sig_path = sig * math.sqrt(tau / 60.0)
        if sig_path < 1e-12:
            return None
        d = math.log(S / K) / sig_path
        fair = _norm_cdf(d)
        return {"fair": fair, "S": S, "K": K, "sig_1m": sig, "tau": tau, "d": d}

    async def ws_loop(self, stop: asyncio.Event) -> None:
        streams = "/".join(f"{self._sym(a).lower()}@aggTrade" for a in self.assets)
        url = f"{Cfg.BINANCE_WS}?streams={streams}"
        while not stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, close_timeout=5, max_size=2**22) as ws:
                    log.info("Binance WS подключён")
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=Cfg.BN_STALL_S)
                        except asyncio.TimeoutError:
                            log.warning("Binance WS stall — reconnect")
                            break
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        data = msg.get("data") or msg
                        if data.get("e") != "aggTrade":
                            continue
                        sym = str(data.get("s") or "")
                        asset = next((a for a in self.assets if self._sym(a) == sym), None)
                        if not asset:
                            continue
                        px = _fnum(data.get("p"))
                        ts = (_fnum(data.get("T")) or time.time() * 1000) / 1000.0
                        if px:
                            self.on_trade(asset, ts, px)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Binance WS: %s", e)
                await asyncio.sleep(1.5)

    async def rest_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            for a in self.assets:
                try:
                    await asyncio.to_thread(self.bootstrap, a)
                except Exception as e:
                    log.warning("spot rest %s: %s", a, e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass


# ───────────────────────────────────────────────────────────── CLOB

BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"


class Clob:
    """CLOB V2 адаптер (боевая версия, как в crossreb). Логика сигналов не затрагивается."""

    def __init__(self):
        self.enabled = False
        self.client = None
        self._v2 = None
        if Cfg.PAPER:
            log.info("CLOB: бумажный режим")
            return
        if not Cfg.PK:
            raise SystemExit("PK не задан, а PAPER=false")
        try:
            from py_clob_client_v2 import (
                ApiCreds,
                BuilderConfig,
                ClobClient,
                MarketOrderArgs,
                OrderType,
                PartialCreateOrderOptions,
                Side,
            )

            self._v2 = {
                "MarketOrderArgs": MarketOrderArgs,
                "OrderType": OrderType,
                "PartialCreateOrderOptions": PartialCreateOrderOptions,
                "Side": Side,
            }
            kwargs: Dict[str, Any] = {"host": Cfg.HOST, "key": Cfg.PK, "chain_id": Cfg.CHAIN_ID}
            if Cfg.FUNDER:
                kwargs["funder"] = Cfg.FUNDER
            if Cfg.SIGNATURE_TYPE >= 0:
                kwargs["signature_type"] = Cfg.SIGNATURE_TYPE
            kwargs["builder_config"] = BuilderConfig(builder_address=BUILDER_ADDRESS, builder_code=BUILDER_CODE)

            supplied = (Cfg.API_KEY, Cfg.API_SECRET, Cfg.API_PASSPHRASE)
            if any(supplied) and not all(supplied):
                raise RuntimeError("заданы не все CLOB_API_KEY/CLOB_API_SECRET/CLOB_API_PASSPHRASE")
            if all(supplied):
                creds = ApiCreds(api_key=Cfg.API_KEY, api_secret=Cfg.API_SECRET, api_passphrase=Cfg.API_PASSPHRASE)
            else:
                creds = ClobClient(**kwargs).create_or_derive_api_key()
            self.client = ClobClient(**kwargs, creds=creds)
            # упасть на старте, а не на первом боевом сигнале, если L2-авторизация не установилась
            self.client.assert_level_2_auth()
            self.enabled = True
            log.warning("CLOB V2: авторизация готова, БОЕВОЙ адаптер включён")
        except Exception as e:
            self.client = None
            self.enabled = False
            raise SystemExit(f"CLOB V2 init: {e} — боевой режим невозможен (PAPER=false), запуск остановлен")

    @staticmethod
    def _amount(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def prewarm(self, tokens) -> None:
        """Прогрев кэша клиента (tick/fee/neg_risk): первый ордер не теряет 100-300 мс на служебных REST."""
        if not self.enabled or self.client is None:
            return
        for t in tokens:
            try:
                self.client.get_tick_size(str(t))
                self.client.get_fee_rate_bps(str(t))
            except Exception as e:
                log.debug("prewarm %s: %s", str(t)[:10], e)

    def rest_best_ask(self, token: str) -> Optional[float]:
        """Свежий лучший ask по REST (локальный WS-стакан может отставать)."""
        try:
            book = self.client.get_order_book(str(token))
            asks = book.get("asks") if isinstance(book, dict) else getattr(book, "asks", None)
            best = None
            for x in asks or []:
                if isinstance(x, dict):
                    p, sz = float(x.get("price")), float(x.get("size") or 0)
                else:
                    p, sz = float(x.price), float(x.size or 0)
                if sz > 0 and (best is None or p < best):
                    best = p
            return best
        except Exception as e:
            log.debug("rest_best_ask: %s", e)
            return None

    def buy_fak(self, token: str, usdc: float, cap: float, tick: str,
                hard_cap: Optional[float] = None) -> dict:
        """FAK BUY с ретраями при no-match: свежий ask по REST → лимит ask+SLIP, но не выше hard_cap
        (граница стратегии). Выше hard_cap не гонимся. При unknown/других ошибках — сразу возврат."""
        hard = min(float(hard_cap if hard_cap is not None else cap), Cfg.CAP_PX)
        cur, tick_cur, attempts = float(cap), str(tick), 0
        while True:
            attempts += 1
            r = self._buy_fak_once(token, usdc, cur, tick_cur)
            r["attempts"] = attempts
            r["cap"] = cur
            if r.get("tick"):
                tick_cur = str(r["tick"])
            if r.get("ok") or not r.get("nomatch") or attempts > Cfg.FAK_RETRIES:
                return r
            time.sleep(Cfg.FAK_RETRY_DELAY_S)
            best = self.rest_best_ask(token)
            if best is None:
                continue
            if best > hard + 1e-9:
                r["err"] = f"nomatch_ask_above_cap:{best:.3f}>{hard:.3f}"
                return r
            cur = min(hard, best + Cfg.SLIP)

    def _buy_fak_once(self, token: str, usdc: float, cap: float, tick: str) -> dict:
        """Одна FAK-попытка. {'ok','usdc','shares','status','unknown','nomatch','err','tick'}."""
        if not self.enabled or self.client is None or self._v2 is None:
            return {"ok": False, "unknown": False, "err": "clob_v2_not_ready"}
        try:
            MarketOrderArgs = self._v2["MarketOrderArgs"]
            OrderType = self._v2["OrderType"]
            PartialCreateOrderOptions = self._v2["PartialCreateOrderOptions"]
            Side = self._v2["Side"]

            # сумма: максимум 2 знака, вниз (никогда не больше ставки)
            amt = math.floor(float(usdc) * 100 + 1e-9) / 100
            if amt < Cfg.MIN_USDC:
                return {"ok": False, "unknown": False, "err": f"amount_below_min:{amt}"}

            tick_cur = str(tick)
            signed = None
            for attempt in (0, 1):
                tick_f = float(tick_cur)
                if tick_f <= 0:
                    return {"ok": False, "unknown": False, "err": f"invalid_tick_size:{tick_cur}"}
                px = min(float(cap), Cfg.CAP_PX)
                px = round(math.floor((px + 1e-12) / tick_f) * tick_f, 8)
                if px <= 0:
                    return {"ok": False, "unknown": False, "err": f"invalid_limit_price:{px}"}
                args = MarketOrderArgs(
                    token_id=str(token), amount=amt, side=Side.BUY,
                    order_type=OrderType.FAK, price=round(px, 6),
                )
                try:
                    signed = self.client.create_market_order(args, PartialCreateOrderOptions(tick_size=tick_cur))
                    break
                except Exception as e:
                    # gamma иногда отдаёт тик мельче, чем разрешает CLOB: берём минимум из ошибки и пробуем ещё раз
                    # (сборка локальная, ордер ещё не отправлен — повтор безопасен)
                    mt = re.search(r"minimum for the market is ([0-9]*\.?[0-9]+)", str(e))
                    if attempt == 0 and mt and float(mt.group(1)) > 0:
                        log.warning("тик %s не подходит, минимум рынка %s — пересборка", tick_cur, mt.group(1))
                        tick_cur = mt.group(1)
                        continue
                    return {"ok": False, "unknown": False, "err": f"build: {e}"[:200], "tick": tick_cur}

            try:
                resp = self.client.post_order(signed, OrderType.FAK)
            except Exception as e:
                code = getattr(e, "status_code", None)
                msg = str(e)
                if "no orders found to match" in msg or "no match" in msg.lower():
                    # ничего не куплено — безопасно повторять
                    return {"ok": False, "unknown": False, "nomatch": True, "err": "nomatch", "tick": tick_cur}
                definite = isinstance(code, int) and 400 <= code < 500
                return {"ok": False, "unknown": not definite, "err": msg[:200], "tick": tick_cur}

            if not isinstance(resp, dict):
                return {"ok": False, "unknown": True, "err": f"unexpected_clob_response:{type(resp).__name__}", "tick": tick_cur}
            if resp.get("success") is False:
                emsg = str(resp.get("errorMsg") or resp.get("error") or "order_rejected")
                return {"ok": False, "unknown": False, "nomatch": "no orders found to match" in emsg,
                        "err": emsg[:200], "tick": tick_cur}

            status = str(resp.get("status") or "").lower()
            if status in ("unmatched", "failed", "rejected", "cancelled", "canceled"):
                return {"ok": False, "unknown": False, "nomatch": status == "unmatched",
                        "err": f"order_not_filled:{status}", "tick": tick_cur}

            # BUY: makingAmount — потрачено USDC, takingAmount — получено долей. Fill не выдумываем.
            filled_usdc = self._amount(resp.get("makingAmount") or resp.get("making_amount"))
            filled_shares = self._amount(resp.get("takingAmount") or resp.get("taking_amount"))
            if filled_usdc <= 0 or filled_shares <= 0:
                return {"ok": False, "unknown": False, "err": "fill_amounts_missing_or_zero", "status": status, "tick": tick_cur}

            return {
                "ok": True, "usdc": filled_usdc, "shares": filled_shares,
                "status": status or "accepted_with_fill",
                "order_id": resp.get("orderID") or resp.get("orderId"), "tick": tick_cur,
            }
        except Exception as e:
            return {"ok": False, "unknown": True, "err": str(e)[:200]}


# ───────────────────────────────────────────────────────────── signal logic

def eval_signal(
    side: str,
    bid: float,
    ask: float,
    fair_up: float,
    spot_ret: Optional[float],
) -> Optional[dict]:
    """
    Возвращает dict сигнала или None.
    side: 'Up' | 'Down'
    """
    mid = 0.5 * (bid + ask)
    spread = ask - bid
    if spread < 0 or spread > Cfg.MAX_SPREAD:
        return None

    if side == "Up":
        edge = fair_up - ask
        fair_side = fair_up
        # true dump: mid низкий и fair тоже
        true_dump = mid < Cfg.MID_DUMP and fair_up < Cfg.TRUE_DUMP_FAIR
        false_dump = mid < Cfg.MID_DUMP and fair_up > Cfg.FAIR_ALIVE
        lead_ok = spot_ret is not None and spot_ret >= Cfg.SPOT_MOVE_MIN
    else:
        fair_dn = 1.0 - fair_up
        ask_dn = 1.0 - bid  # Down ask ≈ 1 − Up bid
        edge = fair_dn - ask_dn
        fair_side = fair_dn
        mid_dn = 1.0 - mid
        # для Down «dump» = перекупленность Up (mid высокий)
        true_dump = mid > Cfg.MID_RALLY and fair_up > Cfg.TRUE_RALLY_FAIR
        false_dump = mid > Cfg.MID_RALLY and fair_up < Cfg.FAIR_WEAK
        lead_ok = spot_ret is not None and spot_ret <= -Cfg.SPOT_MOVE_MIN

    if Cfg.SKIP_TRUE_DUMP and true_dump:
        return None

    mode = Cfg.MODE
    reason = None

    if mode == "residual":
        if Cfg.ENABLE_EDGE and edge >= Cfg.EDGE_MIN:
            reason = "edge"
    elif mode == "lead":
        if Cfg.ENABLE_LEAD and edge >= Cfg.EDGE_MIN and lead_ok:
            reason = "lead"
    elif mode == "false_dump":
        if Cfg.ENABLE_FALSE_DUMP and false_dump and edge >= 0:
            reason = "false_dump"
    else:  # all — приоритет: lead → false_dump → edge_strong → edge (если включены)
        if Cfg.REQUIRE_LEAD:
            if Cfg.ENABLE_LEAD and edge >= Cfg.EDGE_MIN and lead_ok:
                reason = "lead"
        else:
            if Cfg.ENABLE_LEAD and edge >= Cfg.EDGE_MIN and lead_ok:
                reason = "lead"
            elif Cfg.ENABLE_FALSE_DUMP and false_dump and edge >= Cfg.EDGE_MIN * 0.5:
                reason = "false_dump"
            elif Cfg.ENABLE_EDGE_STRONG and edge >= Cfg.EDGE_STRONG:
                reason = "edge_strong"
            elif Cfg.ENABLE_EDGE and edge >= Cfg.EDGE_MIN:
                reason = "edge"

    if not reason:
        return None

    return {
        "side": side,
        "reason": reason,
        "edge": edge,
        "fair": fair_side,
        "fair_up": fair_up,
        "mid": mid,
        "bid": bid,
        "ask": ask if side == "Up" else (1.0 - bid),
        "spread": spread,
        "lead_ok": lead_ok,
        "false_dump": false_dump,
        "true_dump": true_dump,
        "spot_ret": spot_ret,
    }


# ───────────────────────────────────────────────────────────── engine

class Engine:
    def __init__(self, m: Market, clob: Clob, spot: Spot, journal: Journal, stats: Stats):
        self.m = m
        self.clob = clob
        self.spot = spot
        self.j = journal
        self.stats = stats
        self.books: Dict[str, Dict[str, Dict[float, float]]] = {
            t: {"bids": {}, "asks": {}} for t in m.tokens.values()
        }
        # изолированные позиции по reason: {reason: {side, usdc, shares, fee}}
        self.pos: Dict[str, Dict[str, Any]] = {}
        self.tail: List[Tuple[float, Optional[float], Optional[float]]] = []
        self.entered_side: Optional[str] = None
        self.entered_reasons: set = set()   # reason уже взятые в этом окне
        self.last_entry_ts = 0.0
        self.busy = False
        self.fails: Dict[Tuple[str, str], int] = {}   # неудачи FAK по (сторона, reason)
        self.nomatch: Dict[Tuple[str, str], int] = {} # раунды no-match (не фейлы)
        self.blocked: set = set()                     # reason, заблокированные в этом окне после неудач

    def set_book(self, token: str, bids, asks) -> None:
        book = self.books.get(token)
        if book is None:
            return
        book["bids"] = {float(x["price"]): float(x["size"]) for x in (bids or []) if float(x.get("size") or 0) > 0}
        book["asks"] = {float(x["price"]): float(x["size"]) for x in (asks or []) if float(x.get("size") or 0) > 0}

    def on_level(self, token: str, side: str, price: float, size: float) -> None:
        book = self.books.get(token)
        if book is None:
            return
        d = book["bids" if side == "BUY" else "asks"]
        if size <= 0:
            d.pop(price, None)
        else:
            d[price] = size

    def best(self, side: str) -> Tuple[Optional[float], Optional[float]]:
        """bid, ask for Up token side label."""
        tok = self.m.tokens["Up"]
        bids, asks = self.books[tok]["bids"], self.books[tok]["asks"]
        b = max(bids) if bids else None
        a = min(asks) if asks else None
        return b, a

    def snap(self, now: float) -> None:
        b, a = self.best("Up")
        db = (1.0 - a) if a is not None else None
        self.tail.append((now, b, db))
        if len(self.tail) > 30:
            self.tail = self.tail[-30:]

    async def evaluate(self, now: float) -> None:
        m = self.m
        if self.busy:
            return
        if now < m.start or now > m.end - Cfg.END_GUARD_S:
            return
        if (m.end - now) < Cfg.REM_MIN_FRAC * m.length:
            return
        if self.spot.stale(m.asset, now):
            return
        # лимит «одна сторона на окно» — только если не режим per-reason
        if not Cfg.ONE_PER_REASON and Cfg.ONE_SIDE_PER_WINDOW and self.entered_side:
            return
        if now - self.last_entry_ts < Cfg.COOLDOWN_S:
            return

        b, a = self.best("Up")
        if b is None or a is None:
            return

        fd = self.spot.fair_up(m.asset, m.start, now, float(m.end))
        if not fd:
            return
        fair = fd["fair"]
        spot_ret = self.spot.ret(m.asset, float(Cfg.SPOT_LOOK_S), now)

        cands = []
        for side in ("Up", "Down"):
            sig = eval_signal(side, b, a, fair, spot_ret)
            if not sig:
                continue
            if sig["reason"] in self.blocked:
                continue  # заблокирован в окне после неудач / неизвестного исхода
            if Cfg.ONE_PER_REASON and sig["reason"] in self.entered_reasons:
                continue  # этот тип уже брали в окне
            if not Cfg.ONE_PER_REASON and Cfg.ONE_SIDE_PER_WINDOW and self.entered_side:
                continue
            cands.append(sig)
        if not cands:
            return
        # приоритет reason: lead > false_dump > edge_strong > edge, затем больший edge
        prio = {"lead": 0, "false_dump": 1, "edge_strong": 2, "edge": 3}
        cands.sort(key=lambda x: (prio.get(x["reason"], 9), -x["edge"]))
        sig = cands[0]
        if sig["edge"] < Cfg.EDGE_MIN and sig["reason"] != "false_dump":
            return
        ask = float(sig["ask"])
        # ценовой коридор
        if sig["reason"] in ("edge", "edge_strong"):
            if ask < Cfg.MIN_ASK_PX or ask >= Cfg.MAX_ASK_PX:
                return
        if sig["reason"] == "lead":
            if ask < Cfg.LEAD_MIN_ASK or ask >= Cfg.LEAD_MAX_ASK:
                return

        await self.enter(sig, fd, now)

    async def enter(self, sig: dict, fd: dict, now: float) -> None:
        self.busy = True
        try:
            await self._enter(sig, fd, now)
        except Exception as e:
            log.error("%s enter: %r", self.m.slug, e)
            self.last_entry_ts = now + Cfg.FAIL_BACKOFF_S - Cfg.COOLDOWN_S
        finally:
            self.busy = False       # busy не залипает ни при каком исключении

    async def _enter(self, sig: dict, fd: dict, now: float) -> None:
        m = self.m
        side = sig["side"]
        reason = sig["reason"]
        ask = sig["ask"]
        cap = min(Cfg.CAP_PX, ask + Cfg.SLIP)
        usdc = stake_usdc(reason, cap)
        token = m.tokens[side]
        dep = deposit(reason)

        await self.j.write({
            "event": "signal",
            "slug": m.slug,
            "side": side,
            "reason": reason,
            "edge": round(sig["edge"], 4),
            "fair": round(sig["fair"], 4),
            "fair_up": round(sig["fair_up"], 4),
            "mid": round(sig["mid"], 4),
            "ask": round(ask, 4),
            "spot_ret": None if sig["spot_ret"] is None else round(sig["spot_ret"], 5),
            "S": round(fd["S"], 2),
            "K": round(fd["K"], 2),
            "tau": round(fd["tau"], 1),
            "lead_ok": sig["lead_ok"],
            "false_dump": sig["false_dump"],
            "deposit": round(dep, 2),
            "stake": round(usdc, 2),
        })
        log.warning(
            "%s SIGNAL %s %s edge=%.3f fair=%.3f ask=%.3f mid=%.3f ret5=%.3f%% dep=$%.0f stake=$%.0f",
            m.slug, side, reason, sig["edge"], sig["fair"], ask, sig["mid"],
            (sig["spot_ret"] or 0) * 100, dep, usdc,
        )

        got_u = got_s = 0.0
        if not self.clob.enabled:
            # paper: fill at ask
            got_u = usdc
            got_s = usdc / ask if ask > 0 else 0.0
            r = {"ok": True, "usdc": got_u, "shares": got_s, "paper": True}
        else:
            r = await asyncio.to_thread(self.clob.buy_fak, token, usdc, cap, m.tick, reason_cap_max(reason))
            if r.get("tick") and str(r["tick"]) != m.tick:
                m.tick = str(r["tick"])      # запомнить тик, который принял CLOB
            if not r.get("ok"):
                key = (side, reason)
                nomatch = bool(r.get("nomatch"))
                if nomatch:
                    self.nomatch[key] = self.nomatch.get(key, 0) + 1
                else:
                    self.fails[key] = self.fails.get(key, 0) + 1
                await self.j.write({"event": "take_fail", "slug": m.slug, "side": side, "reason": reason,
                                    "err": r.get("err"), "unknown": bool(r.get("unknown")),
                                    "fails": self.fails.get(key, 0), "nomatch": self.nomatch.get(key, 0),
                                    "attempts": r.get("attempts", 1),
                                    "stake": round(usdc, 2), "cap": round(float(r.get("cap") or cap), 4)})
                if nomatch:
                    # ничего не куплено → не фейл и не блок; следующий раунд по свежему сигналу
                    log.warning("%s %s/%s FAK no-match: %d поп., cap=%.3f, ask_ws=%.3f (раунд %d/%d) %s",
                                m.slug, side, reason, r.get("attempts", 1), float(r.get("cap") or cap), ask,
                                self.nomatch[key], Cfg.NOMATCH_ROUNDS, r.get("err") or "")
                    if self.nomatch[key] < Cfg.NOMATCH_ROUNDS:
                        self.last_entry_ts = now + Cfg.NOMATCH_BACKOFF_S - Cfg.COOLDOWN_S
                        return
                    self.blocked.add(reason)
                    log.error("%s %s/%s заблокирован в окне (no-match %d раундов подряд)",
                              m.slug, side, reason, self.nomatch[key])
                    self.last_entry_ts = now + Cfg.FAIL_BACKOFF_S - Cfg.COOLDOWN_S
                    return
                log.error("%s take fail: %s (unknown=%s, fails=%d)", m.slug, r.get("err"), r.get("unknown"), self.fails[key])
                if r.get("unknown"):
                    # исход неизвестен (таймаут/5xx): ордер мог исполниться — повтор НЕ делаем, сверить вручную
                    await self.j.write({"event": "take_unknown", "slug": m.slug, "side": side, "reason": reason,
                                        "stake": round(usdc, 2), "token": token})
                    log.error("%s %s/%s: ИСХОД НЕИЗВЕСТЕН — проверь позицию вручную", m.slug, side, reason)
                if r.get("unknown") or self.fails[key] >= Cfg.MAX_FAILS:
                    self.blocked.add(reason)
                    log.error("%s %s/%s заблокирован в окне (unknown=%s, fails=%d)",
                              m.slug, side, reason, r.get("unknown"), self.fails[key])
                self.last_entry_ts = now + Cfg.FAIL_BACKOFF_S - Cfg.COOLDOWN_S
                return
            got_u = float(r["usdc"])
            got_s = float(r["shares"])

        px = got_u / got_s if got_s else ask
        fee = Cfg.FEE_RATE * px * (1 - px) * got_s
        builder = Cfg.BUILDER_FEE * got_u
        # изолированная позиция по reason (ONE_PER_REASON → один раз на reason); fee включает builder-комиссию
        self.pos[reason] = {"side": side, "usdc": got_u, "shares": got_s, "fee": fee + builder}
        self.entered_side = side
        self.entered_reasons.add(reason)
        self.last_entry_ts = now
        await self.j.write({
            "event": "fill",
            "slug": m.slug,
            "side": side,
            "usdc": round(got_u, 4),
            "shares": round(got_s, 4),
            "px": round(px, 4),
            "fee": round(fee, 4),
            "builder": round(builder, 4),
            "reason": reason,
            "edge": round(sig["edge"], 4),
            "deposit": round(dep, 2),
            "reasons_used": sorted(self.entered_reasons),
            "paper": not self.clob.enabled,
        })
        log.warning("%s FILL %s $%.2f @ %.3f (%s) used=%s",
                    m.slug, side, got_u, px, reason, sorted(self.entered_reasons))
        self.busy = False

    def book_winner(self) -> Optional[str]:
        end = float(self.m.end)
        q = [x for x in self.tail if x[0] < end + 1]
        if not q:
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
        if all(db is not None and db >= thr for _, _u, db in seg):
            return "Down"
        return None

    async def finish(self, gamma: Gamma) -> None:
        m = self.m
        await asyncio.sleep(max(0.0, m.end + 1 - time.time()))
        traded = any(float(p.get("shares") or 0) > 0 for p in self.pos.values())
        if not traded:
            await self.j.write({"event": "flat", "slug": m.slug})
            return
        w = self.book_winner() if Cfg.RESOLVE_BY_BOOK else None
        src = "book"
        if not w:
            src = "gamma"
            await asyncio.sleep(Cfg.RESOLVE_DELAY_S)
            deadline = time.time() + Cfg.RESOLVE_TIMEOUT_S
            while time.time() < deadline and not w:
                w = await asyncio.to_thread(gamma.winner, m.slug)
                if not w:
                    await asyncio.sleep(20)
        if not w:
            await self.j.write({"event": "unresolved", "slug": m.slug})
            return
        # каждый reason — отдельный финрез и запись в журнал/equity
        for reason, p in self.pos.items():
            side = str(p["side"])
            usd = float(p["usdc"])
            shares = float(p["shares"])
            fee = float(p["fee"])
            if shares <= 0:
                continue
            if side == w:
                pnl = shares - usd - fee
            else:
                pnl = -usd - fee
            won = pnl > 0
            self.stats.add(usd, pnl, won)
            await self.j.write({
                "event": "resolved",
                "slug": m.slug,
                "winner": w,
                "source": src,
                "usdc": round(usd, 4),
                "fee": round(fee, 4),
                "pnl": round(pnl, 4),
                "won": won,
                "side": side,
                "reason": reason,
            })
            log.warning(
                "%s RESOLVE %s [%s] %s → %s %+.2f | %s",
                m.slug, reason, side, w, "WIN" if won else "LOSS", pnl, self.stats.line(),
            )


# ───────────────────────────────────────────────────────────── WS market

def handle_book(raw: Any, eng: Engine, now: float) -> None:
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
                    eng.on_level(ch.get("asset_id"), str(ch.get("side")).upper(), p, sz)
        elif et == "best_bid_ask":
            pass
        elif et == "tick_size_change" and ev.get("new_tick_size"):
            eng.m.tick = str(ev["new_tick_size"])
    eng.snap(now)


async def _ping(ws, stop: asyncio.Event, until: float) -> None:
    try:
        while not stop.is_set() and time.time() < until:
            await asyncio.sleep(10)
            await ws.send("PING")
    except Exception:
        pass


async def watch_market(eng: Engine, stop: asyncio.Event) -> None:
    m = eng.m
    toks = list(m.tokens.values())
    while not stop.is_set() and time.time() < m.end:
        try:
            async with websockets.connect(Cfg.WS_URL, ping_interval=None, close_timeout=5, max_size=2**23) as ws:
                await ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
                pinger = spawn(_ping(ws, stop, m.end))
                last = time.time()
                try:
                    while not stop.is_set() and time.time() < m.end:
                        timeout = max(1.0, min(Cfg.WS_STALE_S, m.end - time.time()))
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                        except asyncio.TimeoutError:
                            if time.time() - last > Cfg.WS_STALE_S:
                                break
                            await eng.evaluate(time.time())
                            continue
                        last = time.time()
                        handle_book(raw, eng, last)
                        await eng.evaluate(last)
                finally:
                    pinger.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("%s WS: %s", m.slug, e)
            await asyncio.sleep(1.0)


async def run_engine(eng: Engine, gamma: Gamma, stop: asyncio.Event) -> None:
    m = eng.m
    log.info("окно %s до %s", m.slug, time.strftime("%H:%M:%S", time.gmtime(m.end)))
    fin = spawn(eng.finish(gamma))
    spawn(watch_market(eng, stop))
    if eng.clob.enabled:
        spawn(asyncio.to_thread(eng.clob.prewarm, list(m.tokens.values())))
    # periodic eval even if book quiet
    while not stop.is_set() and time.time() < m.end:
        await eng.evaluate(time.time())
        try:
            await asyncio.wait_for(stop.wait(), timeout=Cfg.EVAL_EVERY_S)
        except asyncio.TimeoutError:
            pass
    try:
        await fin
    except Exception as e:
        log.error("%s finish: %s", m.slug, e)


# ───────────────────────────────────────────────────────────── main

async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, Cfg.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    log.info("конфиг: %s", ENV_FILE_USED or "defaults")
    log.warning("РЕЖИМ: %s", "БУМАЖНЫЙ" if Cfg.PAPER else "БОЕВОЙ")
    log.info(
        "MODE=%s EDGE_MIN=%.3f EDGE_STRONG=%.3f SPOT_LOOK=%ds MOVE=%.3f%% SKIP_TRUE_DUMP=%s",
        Cfg.MODE, Cfg.EDGE_MIN, Cfg.EDGE_STRONG, Cfg.SPOT_LOOK_S, Cfg.SPOT_MOVE_MIN * 100, Cfg.SKIP_TRUE_DUMP,
    )
    log.info(
        "срез: LEAD=%s (ask∈[%.2f,%.2f)) FALSE_DUMP=%s | EDGE=%s EDGE_STRONG=%s (ask∈[%.2f,%.2f))",
        Cfg.ENABLE_LEAD, Cfg.LEAD_MIN_ASK, Cfg.LEAD_MAX_ASK,
        Cfg.ENABLE_FALSE_DUMP, Cfg.ENABLE_EDGE, Cfg.ENABLE_EDGE_STRONG,
        Cfg.MIN_ASK_PX, Cfg.MAX_ASK_PX,
    )
    log.info(
        "входы: ONE_PER_REASON=%s (разные reason в одном окне, без повтора) | "
        "ONE_SIDE_PER_WINDOW=%s | COOLDOWN=%.0fs",
        Cfg.ONE_PER_REASON, Cfg.ONE_SIDE_PER_WINDOW, Cfg.COOLDOWN_S,
    )
    if not Cfg.PAPER and not Cfg.ONE_PER_REASON:
        # повторный вход в тот же reason перезаписал бы учёт первой позиции (деньги ушли, а в журнале её нет)
        log.warning("БОЕВОЙ режим: ONE_PER_REASON=false не поддерживается учётом позиций — принудительно true")
        Cfg.ONE_PER_REASON = True
    log.info("комиссии: FEE_RATE=%.3f BUILDER_FEE=%.3f | MAX_FAILS=%d FAIL_BACKOFF=%.1fs | SLIP=%.3f CAP_PX=%.2f",
             Cfg.FEE_RATE, Cfg.BUILDER_FEE, Cfg.MAX_FAILS, Cfg.FAIL_BACKOFF_S, Cfg.SLIP, Cfg.CAP_PX)
    global EQUITY
    EQUITY = Equity()
    EQUITY.load(Path(Cfg.JOURNAL))
    log.info("assets %s tf %s", Cfg.ASSETS, Cfg.TFS)
    for reason in Cfg.REASONS:
        dep0 = _deposit_for(reason)
        pct = _stake_pct_for(reason)
        comp = _compound_for(reason)
        real = EQUITY.realized(reason)
        n = EQUITY.n_trades(reason)
        dep = EQUITY.value(reason)
        cmax = reason_cap_max(reason)
        st = stake_usdc(reason, cmax)          # худший случай по цене входа этого reason
        st_lo = stake_usdc(reason, 0.0)        # дешёвый вход: упирается в MIN_USDC / проценты
        extra = (f" + итог {real:+.2f} по {n} сделкам") if comp else ", без реинвеста"
        log.warning(
            "ОБЪЁМ [%s]: депозит $%.2f (база $%g%s) × %g%% → ставка $%.2f…$%.2f (cap до %.2f, мин. %g долей и $%.2f) | COMPOUND=%s",
            reason, dep, dep0, extra, pct, st_lo, st, cmax, Cfg.MIN_SHARES, Cfg.MIN_USDC, comp,
        )

    stop = asyncio.Event()
    try:
        loop = asyncio.get_running_loop()
        for sg in (_signal.SIGINT, _signal.SIGTERM):
            loop.add_signal_handler(sg, stop.set)
    except NotImplementedError:
        pass

    clob = Clob()
    gamma = Gamma()
    journal = Journal(Cfg.JOURNAL)
    stats = Stats()
    spot = Spot(Cfg.ASSETS)
    for a in Cfg.ASSETS:
        await asyncio.to_thread(spot.bootstrap, a)

    bg = [
        asyncio.create_task(spot.ws_loop(stop)),
        asyncio.create_task(spot.rest_loop(stop)),
    ]
    engines: Dict[str, Engine] = {}
    tasks: Dict[str, asyncio.Task] = {}

    try:
        while not stop.is_set():
            now = time.time()
            for asset in Cfg.ASSETS:
                for tf in Cfg.TFS:
                    L = tf * 60
                    cur = int(now // L) * L
                    for start in (cur, cur + L):
                        if start + L <= now or start - now > Cfg.PREROLL_S:
                            continue
                        slug = f"{asset}-updown-{tf}m-{start}"
                        if slug in tasks:
                            continue
                        mk = await asyncio.to_thread(gamma.market, asset, tf, start)
                        if mk is None:
                            continue
                        eng = Engine(mk, clob, spot, journal, stats)
                        engines[slug] = eng
                        tasks[slug] = asyncio.create_task(run_engine(eng, gamma, stop))
            for slug in [s for s, t in tasks.items() if t.done()]:
                t = tasks.pop(slug)
                engines.pop(slug, None)
                if not t.cancelled() and t.exception():
                    log.error("task %s: %r", slug, t.exception())
            try:
                await asyncio.wait_for(stop.wait(), timeout=Cfg.DISCOVERY_EVERY_S)
            except asyncio.TimeoutError:
                pass
    finally:
        log.info("стоп. %s", stats.line())
        for t in list(tasks.values()) + bg:
            t.cancel()
        await asyncio.gather(*tasks.values(), *bg, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
