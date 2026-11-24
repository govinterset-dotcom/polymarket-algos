#!/usr/bin/env python3
"""
lotto01.py — лимитка BUY по 0.01 на «поздний переворот лидера» в up/down рынках Polymarket.

Сигнал (по разбору октябрьских книг, подтип B):
  • сторона в этом окне уже была лидером: mid стороны >= LEAD_MIN (0.90);
  • после FRAC_MIN (70%) окна аск стороны ВПЕРВЫЕ за окно падает до <= TRIGGER_ASK (0.01);
  • падение резкое: mid стороны был >= DROP_FROM (0.50) не раньше чем за DROP_S (15 с) до этого;
  • Binance: прошлая закрытая 1m свеча ушла В ПОЛЬЗУ стороны на >= PREV_Z_MIN (0.8) σ,
    σ — ст. отклонение 1m доходностей за час до старта окна.
  Бэктест 07–27.10 (5m+15m, BTC/ETH/SOL/XRP): 21 сигнал, 4 победы (19%), +$379 на $1.
  Побед мало — это исследование, а не проверенная стратегия.

Действие: сразу лимитка BUY по ORDER_PX (0.01) на SHARES долей, GTD до конца окна
(+запас). После конца окна бот снимает её по id, если она ещё висит. cancel_all не используется.

Журнал (JOURNAL): candidate — каждое первое касание 0.01 после FRAC_MIN у бывшего лидера
(со всеми признаками и решением), order / fill / resolved — по сделкам, result — исход всех
кандидатов (для разбора фильтров). Дополнительно пишется Chainlink TWAP из RTDS и объём на 0.01.

Исследовательская запись (PROBE=true, отдельный журнал PROBE_JOURNAL) — без входа, по PROBE_ASSETS × PROBE_TFS
(можно шире, чем торговые ASSETS/TFS): на ПЕРВОМ за окно касании аска стороны каждого уровня PROBE_LEVELS
(0.03/0.02/0.01/0.005/0.001) после PROBE_FRAC_MIN окна пишется event=probe:
  • окно: el, rem, frac, lead, core (лидер >= LEAD_MIN и после FRAC_MIN), drop50/drop90 (с последнего mid >= 0.5/0.9),
    held (доля секунд стороны лидером), mid_5/10/30/60 (mid стороны N секунд назад), upd10/upd30 (обновлений книги);
  • book — все уровни книги стороны с ценой <= PROBE_DEPTH (биды и аски);
  • dep — объёмы стороны: b_sub/b01/b02/b03 (биды по < 0.01 / <= 0.01 / <= 0.02 / <= 0.03), a_sub/a01/a02/a03 (аски);
    у каждого v (сейчас), m60 (медиана за 60 с), l1/l5/l10/l30 (значение 1/5/10/30 с назад);
  • bn — Binance по активу: dist_bp (до TWAP-страйка, в пользу стороны), fair (TWAP-модель), z, r15_bp/r60_bp, sig_bp;
  • cl_lead_bp — Chainlink TWAP.
После резолва окна — event=probe_result (winner). Торговая логика от probe не зависит.

Режим тейкера (ENTRY_MODE=sweep): на сигнале сразу FAK BUY — забрать всё, что продают по <= SWEEP_PX (0.03),
не больше SWEEP_USDC $. Ничего не висит в книге (налив только того, что стоит сейчас — без доналива
в проигрышных окнах). Доп. фильтры: DEPTH_MAX — на дне (<= DEPTH_PX) в момент касания не больше N долей
(тонкая книга: обвалилась книга, а не цена); CL_SIG_MIN — Chainlink TWAP от старта окна в пользу стороны / σ;
DROP_S=0 и пустой PREV_Z_MIN выключают старые фильтры. Журнал: event=sweep (доли, $, ср. цена, оценка комиссии).

Конфиг: lotto01.env рядом со скриптом (или ENV_FILE). PAPER=true по умолчанию.
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
    stem = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else "lotto01"
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


def _fo(k: str, d: Optional[float]) -> Optional[float]:
    """Число или None: пустое значение в env = граница выключена (если ключа нет — значение по умолчанию)."""
    if k in os.environ:
        v = _env(k)
        return float(v) if v else None
    return d


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
    RTDS_URL = _env("RTDS_URL", "wss://ws-live-data.polymarket.com")
    TWAP_TOPIC = _env("TWAP_TOPIC", "crypto_prices_twap_sixty")
    TWAP_LOG = _b("TWAP_LOG", True)

    ASSETS = [a.strip().lower() for a in _env("ASSETS", "btc,eth,sol,xrp").split(",") if a.strip()]
    TFS = [int(x) for x in _env("TFS", "5,15").split(",") if x.strip()]

    # сигнал
    LEAD_MIN = _f("LEAD_MIN", 0.90)        # сторона в окне уже была >= (mid)
    FRAC_MIN = _f("FRAC_MIN", 0.70)        # касание не раньше этой доли окна
    # окно касания по времени до конца, отдельно для 5m и 15m (0 = выкл): не ближе MIN_REM_S_x и не дальше MAX_REM_S_x
    MIN_REM_S = {5: _f("MIN_REM_S_5", 0.0), 15: _f("MIN_REM_S_15", 0.0)}
    MAX_REM_S = {5: _f("MAX_REM_S_5", 0.0), 15: _f("MAX_REM_S_15", 0.0)}
    TRIGGER_ASK = _f("TRIGGER_ASK", 0.0105)  # аск стороны <= — касание
    DROP_FROM = _f("DROP_FROM", 0.50)      # mid стороны был >= ...
    DROP_S = _f("DROP_S", 15.0)            # ... не раньше чем за столько секунд до касания (0 = фильтр выкл)
    # прошлая 1m свеча Binance в пользу стороны, в σ: z >= PREV_Z_MIN и z <= PREV_Z_MAX (пусто = границы нет)
    PREV_Z_MIN = _fo("PREV_Z_MIN", 0.8)
    PREV_Z_MAX = _fo("PREV_Z_MAX", None)
    # объём аска стороны по <= DEPTH_PX в момент касания <= DEPTH_MAX долей (0 = выкл): тонкая книга на дне
    DEPTH_PX = _f("DEPTH_PX", 0.01)
    DEPTH_MAX = _f("DEPTH_MAX", 0.0)
    # Chainlink TWAP от старта окна в пользу стороны, делённый на σ (1m, bp): >= CL_SIG_MIN (пусто = выкл).
    # Нет данных Chainlink/σ: CL_FAIL_OPEN=true — входить, false — пропуск (reason=cl_na)
    CL_SIG_MIN = _fo("CL_SIG_MIN", None)
    CL_FAIL_OPEN = _b("CL_FAIL_OPEN", False)
    SIGMA_MIN = _i("SIGMA_MIN", 60)        # минут истории для σ (до старта окна)
    MAX_SPREAD = _f("MAX_SPREAD", 0.05)    # mid считается только при спреде <= (как в анализе)
    BN_FAIL_OPEN = _b("BN_FAIL_OPEN", False)  # нет данных Binance: true — входить, false — нет

    # вход: limit — лимитка ORDER_PX × SHARES (GTD, висит до конца окна);
    #       sweep — сразу тейкер FAK: забрать всё, что продают по <= SWEEP_PX, но не больше SWEEP_USDC $
    ENTRY_MODE = _env("ENTRY_MODE", "limit").lower()
    SWEEP_PX = _f("SWEEP_PX", 0.03)
    SWEEP_USDC = _f("SWEEP_USDC", 10.0)
    FEE_RATE = _f("FEE_RATE", 0.07)        # оценка комиссии тейкера: FEE_RATE × p × (1 − p) × доли
    # ордер
    ORDER_PX = _f("ORDER_PX", 0.01)
    SHARES = _f("SHARES", 100.0)           # 100 долей по 0.01 = $1
    GTD_PAD_S = _i("GTD_PAD_S", 30)        # срок жизни лимитки после конца окна
    GTD_MIN_S = _i("GTD_MIN_S", 200)       # но не меньше чем через столько секунд от постановки (биржа: >= 180)
    MAX_ORDERS_PER_HOUR = _i("MAX_ORDERS_PER_HOUR", 0)   # 0 = без лимита
    PAPER_LAT_S = _f("PAPER_LAT_S", 0.3)
    # мониторинг после касания (только запись): через эти секунды после каждого кандидата пишется
    # объём на дне (<= DEPTH_PX) и до SWEEP_PX, аск/бид/mid стороны — event=cand_after; пусто = выкл
    AFTER_S = sorted({float(x) for x in _env("AFTER_S", "0.5,1,2,5").split(",") if x.strip()})

    # исследовательская запись (probe): книга и Binance на каждом первом касании уровней, без входа
    PROBE = _b("PROBE", True)
    PROBE_ASSETS = [a.strip().lower() for a in _env("PROBE_ASSETS", "btc,eth,sol,xrp").split(",") if a.strip()]
    PROBE_TFS = [int(x) for x in _env("PROBE_TFS", "5,15").split(",") if x.strip()]
    PROBE_LEVELS = sorted({float(x) for x in _env("PROBE_LEVELS", "0.03,0.02,0.01,0.005,0.001").split(",") if x.strip()},
                          reverse=True)
    PROBE_DEPTH = _f("PROBE_DEPTH", 0.03)          # уровни книги в снимке: цены <= этого
    PROBE_FRAC_MIN = _f("PROBE_FRAC_MIN", 0.5)     # касания раньше этой доли окна не пишем
    PROBE_JOURNAL = _env("PROBE_JOURNAL", "lotto01_probe.jsonl")
    PROBE_BN = _b("PROBE_BN", True)                # посекундный Binance (aggTrade) для признаков
    BINANCE_WS = _env("BINANCE_WS", "wss://stream.binance.com:9443/stream")
    FAIR_B = _f("FAIR_B", 0.04)                    # TWAP-модель: Φ((x+B)/(K·σ·√var)), подгонка BTC 15m октябрь
    FAIR_K = _f("FAIR_K", 1.37)

    RESOLVE_TIMEOUT_S = _i("RESOLVE_TIMEOUT_S", 3600)
    WS_STALE_S = _f("WS_STALE_S", 30.0)
    JOURNAL = _env("JOURNAL", "lotto01.jsonl")
    LOG_LEVEL = _env("LOG_LEVEL", "INFO")


log = logging.getLogger("lotto01")
BN_SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}


# ════════════════════════════════════════════════════════════════ журнал
class Journal:
    def __init__(self, path: str):
        self.path = Path(path)
        self.lock = asyncio.Lock()

    async def write(self, rec: dict) -> None:
        rec = {"ts": round(time.time(), 3), **rec}
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
        self.s.headers["User-Agent"] = "lotto01/1.0"

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


# ════════════════════════════════════════════════════════════════ Binance (REST)
class Binance:
    def __init__(self):
        self.s = requests.Session()

    def klines(self, asset: str, end_ms: int, limit: int) -> list:
        r = self.s.get(f"{Cfg.BINANCE_REST}/api/v3/klines",
                       params={"symbol": BN_SYM[asset], "interval": "1m", "limit": limit, "endTime": end_ms},
                       timeout=8)
        r.raise_for_status()
        return r.json()

    def sigma(self, asset: str, start: int) -> Optional[float]:
        """σ 1m доходностей (bp) за SIGMA_MIN минут до старта окна."""
        rows = [k for k in self.klines(asset, start * 1000 - 1, Cfg.SIGMA_MIN) if int(k[0]) + 60_000 <= start * 1000]
        rets = [math.log(float(k[4]) / float(k[1])) * 1e4 for k in rows if float(k[1]) > 0]
        if len(rets) < max(20, Cfg.SIGMA_MIN // 2):
            return None
        mu = sum(rets) / len(rets)
        return math.sqrt(sum((x - mu) ** 2 for x in rets) / (len(rets) - 1))

    def minute(self, asset: str, m_open: int) -> Optional[Tuple[float, float]]:
        """(open, close) закрытой 1m свечи, начавшейся в m_open (сек)."""
        rows = self.klines(asset, (m_open + 60) * 1000 - 1, 3)
        for k in rows:
            if int(k[0]) == m_open * 1000 and int(k[6]) < time.time() * 1000 + 1:
                return float(k[1]), float(k[4])
        return None


# ════════════════════════════════════════════════════════════════ Chainlink TWAP (RTDS) — только запись
class Twap:
    def __init__(self):
        self.obs: Dict[str, Dict[int, float]] = {}
        self.last: Dict[str, Tuple[int, float]] = {}
        self.up = False

    @staticmethod
    def _val(p: dict) -> Optional[float]:
        fav = p.get("full_accuracy_value")
        if fav not in (None, ""):
            try:
                return float(Decimal(str(fav)) / Decimal(10) ** 18)
            except Exception:
                pass
        try:
            return float(p.get("value"))
        except (TypeError, ValueError):
            return None

    def _put(self, sym, p) -> None:
        if not sym or not isinstance(p, dict):
            return
        t, v = p.get("timestamp"), self._val(p)
        if t is None or v is None:
            return
        sec = int(float(t) // 1000) if float(t) > 1e11 else int(float(t))
        sym = str(sym).lower()
        d = self.obs.setdefault(sym, {})
        d[sec] = v
        self.last[sym] = (sec, v)
        if len(d) > 7200:
            for k in sorted(d)[:3600]:
                d.pop(k, None)

    def ingest(self, raw) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "ignore")
        if not raw or raw.strip() in ("PING", "PONG"):
            return
        try:
            msg = json.loads(raw)
        except Exception:
            return
        for m in (msg if isinstance(msg, list) else [msg]):
            if not isinstance(m, dict) or m.get("topic") not in (None, Cfg.TWAP_TOPIC):
                continue
            pl = m.get("payload")
            if isinstance(pl, dict) and isinstance(pl.get("data"), list):
                for d in pl["data"]:
                    self._put(pl.get("symbol") or (d or {}).get("symbol"), d)
            elif isinstance(pl, dict):
                self._put(pl.get("symbol"), pl)
            elif isinstance(pl, list):
                for d in pl:
                    self._put((d or {}).get("symbol"), d)

    def at(self, asset: str, sec: int) -> Optional[float]:
        return (self.obs.get(f"{asset}/usd") or {}).get(int(sec))

    def now(self, asset: str) -> Optional[Tuple[int, float]]:
        return self.last.get(f"{asset}/usd")

    async def run(self, stop: asyncio.Event) -> None:
        sub = {"action": "subscribe", "subscriptions": [{"topic": Cfg.TWAP_TOPIC, "type": "update"}]}
        while not stop.is_set():
            try:
                async with websockets.connect(Cfg.RTDS_URL, ping_interval=None, close_timeout=5,
                                              max_size=2 ** 22) as ws:
                    await ws.send(json.dumps(sub))
                    self.up = True
                    last = time.time()
                    pinger = spawn(_pinger(ws, stop, 5.0))
                    try:
                        while not stop.is_set():
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                            except asyncio.TimeoutError:
                                if time.time() - last > 20:
                                    break
                                continue
                            last = time.time()
                            self.ingest(raw)
                    finally:
                        pinger.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("RTDS: %s", e)
            self.up = False
            await asyncio.sleep(2.0)


# ════════════════════════════════════════════════════════════════ Binance посекундно (для probe)
class BnLive:
    """aggTrade по активам → последняя цена каждой секунды; при старте — догрузка 1s свечей по REST."""
    KEEP_S = 1900

    def __init__(self, assets: List[str]):
        self.assets = [a for a in assets if a in BN_SYM]
        self.sec: Dict[str, Dict[int, float]] = {a: {} for a in self.assets}
        self.rev = {BN_SYM[a]: a for a in self.assets}
        self.s = requests.Session()

    def backfill(self) -> None:
        for a in self.assets:
            try:
                r = self.s.get(f"{Cfg.BINANCE_REST}/api/v3/klines",
                               params={"symbol": BN_SYM[a], "interval": "1s", "limit": 1000}, timeout=10)
                r.raise_for_status()
                d = self.sec[a]
                for k in r.json():
                    d.setdefault(int(k[0]) // 1000, float(k[4]))
            except Exception as e:
                log.warning("Binance 1s догрузка %s: %s", a, e)

    async def run(self, stop: asyncio.Event) -> None:
        await asyncio.to_thread(self.backfill)
        url = f"{Cfg.BINANCE_WS}?streams=" + "/".join(f"{BN_SYM[a].lower()}@aggTrade" for a in self.assets)
        while not stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, close_timeout=5) as ws:
                    log.info("Binance поток подключён (%s)", ",".join(self.assets))
                    n = 0
                    while not stop.is_set():
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                        d = msg.get("data") or msg
                        a = self.rev.get(str(d.get("s", "")).upper())
                        if a is None:
                            continue
                        sec = int(d["T"]) // 1000
                        self.sec[a][sec] = float(d["p"])
                        n += 1
                        if n % 5000 == 0:
                            self.prune()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Binance поток: %s", e)
                await asyncio.sleep(2.0)

    def prune(self) -> None:
        lim = int(time.time()) - self.KEEP_S
        for d in self.sec.values():
            for k in [k for k in d if k < lim]:
                d.pop(k, None)

    def series(self, a: str, s0: int, s1: int) -> Optional[List[float]]:
        d = self.sec.get(a) or {}
        last = None
        for k in range(s0 - 1, s0 - 121, -1):
            if k in d:
                last = d[k]
                break
        out = []
        for k in range(s0, s1 + 1):
            if k in d:
                last = d[k]
            out.append(last)
        return None if any(x is None for x in out) else out

    def features(self, a: str, start: int, end: int, now: float, side: str) -> Optional[dict]:
        """Признаки стороны: расстояние до TWAP-страйка, модель TWAP, ход за 15/60 с, σ за 600 с."""
        s1 = int(now)
        hist = self.series(a, s1 - 600, s1)
        if not hist:
            hist = self.series(a, s1 - 300, s1)
        if not hist or len(hist) < 120:
            return None
        sg = 1 if side == "Up" else -1
        rets = [math.log(hist[i] / hist[i - 1]) for i in range(1, len(hist)) if hist[i - 1] > 0]
        mu = sum(rets) / len(rets)
        sig = math.sqrt(sum((x - mu) ** 2 for x in rets) / max(1, len(rets) - 1)) * 1e4
        px = hist[-1]
        out = {"px": px, "sig_bp": round(sig, 4),
               "r15_bp": round(math.log(px / hist[-16]) * 1e4 * sg, 3),
               "r60_bp": round(math.log(px / hist[-61]) * 1e4 * sg, 3)}
        pre = self.series(a, start - 60, start - 1)
        if pre and sig > 0:
            strike = sum(pre) / len(pre)
            tau = end - now
            if tau >= 60:
                ehat, var = px, tau - 40.0
            else:
                known = self.series(a, end - 60, s1) or [px]
                kn = 60 - max(tau, 0)
                ehat = (sum(known) / len(known) * kn + px * max(tau, 0)) / 60.0
                var = max(tau, 0) ** 3 / (3 * 3600.0)
            x = math.log(ehat / strike) * 1e4
            z = (x + Cfg.FAIR_B) / (Cfg.FAIR_K * sig * math.sqrt(max(var, 0.5)))
            up = 0.5 * (1 + math.erf(z / math.sqrt(2)))
            out.update(strike=strike, dist_bp=round(x * sg, 3), z=round(z * sg, 3),
                       fair=round(up if side == "Up" else 1 - up, 5))
        return out


async def _pinger(ws, stop: asyncio.Event, every: float, until: float = 1e18) -> None:
    try:
        while not stop.is_set() and time.time() < until:
            await asyncio.sleep(every)
            await ws.send("PING")
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════ CLOB
class Clob:
    """Лимитки через py_clob_client_v2. Только по id, без cancel_all."""
    _sem = threading.BoundedSemaphore(4)

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
        self.client = m.ClobClient(**kw)
        if Cfg.API_KEY and Cfg.API_SECRET and Cfg.API_PASSPHRASE:
            creds = m.ApiCreds(api_key=Cfg.API_KEY, api_secret=Cfg.API_SECRET, api_passphrase=Cfg.API_PASSPHRASE)
        else:
            creds = self.client.create_or_derive_api_key()
        self.client.set_api_creds(creds)
        log.info("CLOB подключён — БОЕВОЙ режим")

    def place(self, token: str, price: float, shares: float, tick: str, expiration: int) -> dict:
        """GTD BUY. {'ok', 'id', 'unknown', 'err', 'status', 'resp'}; unknown — исход неизвестен."""
        m = self.m
        with self._sem:
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
            return {"ok": False, "unknown": False, "err": str(r.get("errorMsg") or r)[:200], "resp": r}
        return {"ok": True, "id": oid, "status": r.get("status"), "unknown": False, "resp": r}

    def sweep(self, token: str, usdc: float, cap: float, tick: str) -> dict:
        """FAK BUY на usdc не дороже cap: забрать из книги всё по <= cap. {'ok','usdc','shares','unknown','err'}."""
        m = self.m
        with self._sem:
            try:
                args = m.MarketOrderArgs(token_id=token, amount=round(usdc, 2), side=m.Side.BUY,
                                         order_type=m.OrderType.FAK, price=round(cap, 6))
                signed = self.client.create_market_order(args, m.PartialCreateOrderOptions(tick_size=tick))
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

    def order(self, oid: str) -> Optional[dict]:
        try:
            with self._sem:
                r = self.client.get_order(oid)
            return r if isinstance(r, dict) else None
        except Exception as e:
            log.warning("ордер %s: %s", oid[:12], e)
            return None

    def cancel(self, oid: str) -> bool:
        try:
            with self._sem:
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
    lead: float = 0.0                    # максимум mid стороны в окне
    last_hi: float = -1e9                # последний момент mid >= DROP_FROM
    touched: bool = False                # первое касание 0.01 уже было
    # заявка
    oid: Optional[str] = None
    placed_at: float = 0.0
    paper_filled: bool = False
    paper_fill_t: Optional[float] = None
    order_shares: float = 0.0
    cand: Optional[dict] = None
    sweep: Optional[Tuple[float, float, float]] = None   # налив тейкером: (доли, $, оценка комиссии $)
    # probe
    last_hi90: float = -1e9              # последний момент mid >= 0.90
    probed: set = field(default_factory=set)
    hist: list = field(default_factory=list)   # посекундные снимки: (t, mid, агрегаты, обновлений всего)
    n_s: int = 0                         # секунд с mid
    n_lead: int = 0                      # из них mid >= 0.5


AGG_KEYS = ("b_sub", "b01", "b02", "b03", "a_sub", "a01", "a02", "a03")


class Engine:
    def __init__(self, m: Market, clob: Clob, bn: Binance, tw: Optional[Twap], j: Journal, stats: "Stats",
                 rate: "Rate", trade: bool = True, bnl: Optional[BnLive] = None, pj: Optional[Journal] = None):
        self.m, self.clob, self.bn, self.tw, self.j, self.stats, self.rate = m, clob, bn, tw, j, stats, rate
        self.trade = trade                   # False — окно только для записи probe
        self.bnl, self.pj = bnl, pj
        self.probe_on = pj is not None and m.asset in Cfg.PROBE_ASSETS and m.tf in Cfg.PROBE_TFS
        self.upd: Dict[str, int] = {t: 0 for t in m.tokens.values()}
        self.n_probes = 0
        self.sides = {"Up": SideState("Up"), "Down": SideState("Down")}
        self.up_bid: Optional[float] = None
        self.up_ask: Optional[float] = None
        # книги уровней (для объёма на 0.01): token -> {'bids':{px:sz}, 'asks':{px:sz}}
        self.levels: Dict[str, Dict[str, Dict[float, float]]] = {t: {"bids": {}, "asks": {}} for t in m.tokens.values()}
        self.sigma: Optional[float] = None
        self.sigma_done = False
        self.open_px: Optional[float] = None
        self.tasks: List[asyncio.Task] = []

    # --- данные
    async def load_sigma(self) -> None:
        for attempt in range(4):
            try:
                self.sigma = await asyncio.to_thread(self.bn.sigma, self.m.asset, self.m.start)
                break
            except Exception as e:
                log.debug("%s σ: %s", self.m.slug, e)
                await asyncio.sleep(1 + attempt)
        self.sigma_done = True

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

    def depth_at(self, side: str, px: float) -> dict:
        """Объём (доли), который можно купить по <= px. Книга токена в WS уже включает зеркальные
        заявки другого исхода, поэтому складывать их не нужно."""
        tok = self.m.tokens[side]
        return {"shares": round(sum(s for p, s in self.levels[tok]["asks"].items() if p <= px + 1e-9), 2)}

    # --- probe: агрегаты малой глубины стороны
    def agg(self, side: str) -> Dict[str, float]:
        bk = self.levels[self.m.tokens[side]]
        b, a = bk["bids"], bk["asks"]
        o = {"b_sub": sum(s for p, s in b.items() if p < 0.01 - 1e-9),
             "a_sub": sum(s for p, s in a.items() if p < 0.01 - 1e-9)}
        for lv, k in ((0.01, "01"), (0.02, "02"), (0.03, "03")):
            o["b" + k] = sum(s for p, s in b.items() if p <= lv + 1e-9)
            o["a" + k] = sum(s for p, s in a.items() if p <= lv + 1e-9)
        return {k: round(v, 2) for k, v in o.items()}

    def side_mid(self, side: str) -> Optional[float]:
        b, a = self.up_bid, self.up_ask
        if b is None or a is None or not (0 < a - b <= Cfg.MAX_SPREAD + 1e-9):
            return None
        return (a + b) / 2 if side == "Up" else 1 - (a + b) / 2

    async def sampler(self) -> None:
        """Раз в секунду: mid и агрегаты книги каждой стороны (история 90 с) — для динамики в probe."""
        m = self.m
        while time.time() < m.end + 1:
            now = time.time()
            if now >= m.start:
                for s in self.sides.values():
                    mid = self.side_mid(s.name)
                    if mid is not None:
                        s.n_s += 1
                        s.n_lead += mid >= 0.5
                    s.hist.append((now, mid, self.agg(s.name), self.upd[m.tokens[s.name]]))
                    if len(s.hist) > 95:
                        del s.hist[:len(s.hist) - 95]
            await asyncio.sleep(1.0 - (time.time() % 1.0) + 0.02)

    @staticmethod
    def _at(hist: list, t: float):
        """Последний снимок не позже t."""
        for h in reversed(hist):
            if h[0] <= t:
                return h
        return None

    def probe(self, s: SideState, lvl: float, now: float, s_ask: float) -> None:
        """Снимок считается синхронно в момент касания (до следующих обновлений книги), запись — фоном."""
        m = self.m
        tok = m.tokens[s.name]
        bk = self.levels[tok]
        cur = self.agg(s.name)
        hist = list(s.hist)
        dep: Dict[str, dict] = {}
        for k in AGG_KEYS:
            past = [h[2][k] for h in hist if h[0] >= now - 60]
            d = {"v": cur[k], "m60": round(sorted(past)[len(past) // 2], 2) if past else None}
            for lag in (1, 5, 10, 30):
                h = self._at(hist, now - lag)
                d[f"l{lag}"] = h[2][k] if h else None
            dep[k] = d
        mids = {}
        for lag in (5, 10, 30, 60):
            h = self._at(hist, now - lag)
            mids[f"mid_{lag}"] = round(h[1], 4) if h and h[1] is not None else None
        upd_now = self.upd[tok]
        h10, h30 = self._at(hist, now - 10), self._at(hist, now - 30)
        drop50 = now - s.last_hi
        drop90 = now - s.last_hi90
        frac = (now - m.start) / m.length
        rec = dict(event="probe", slug=m.slug, asset=m.asset, tf=m.tf, side=s.name, lvl=lvl, ask=s_ask,
                   el=round(now - m.start, 2), rem=round(m.end - now, 2), frac=round(frac, 4), lead=round(s.lead, 4),
                   core=bool(s.lead >= Cfg.LEAD_MIN - 1e-9 and frac >= Cfg.FRAC_MIN),
                   drop50=round(drop50, 1) if drop50 < 1e8 else None, drop90=round(drop90, 1) if drop90 < 1e8 else None,
                   held=round(s.n_lead / s.n_s, 3) if s.n_s else None,
                   bid=self.up_bid if s.name == "Up" else (round(1 - self.up_ask, 4) if self.up_ask is not None else None),
                   **mids,
                   upd10=upd_now - h10[3] if h10 else None, upd30=upd_now - h30[3] if h30 else None,
                   book={"bids": sorted([[p, round(z, 2)] for p, z in bk["bids"].items() if p <= Cfg.PROBE_DEPTH + 1e-9]),
                         "asks": sorted([[p, round(z, 2)] for p, z in bk["asks"].items() if p <= Cfg.PROBE_DEPTH + 1e-9])},
                   dep=dep)
        if self.bnl is not None:
            try:
                f = self.bnl.features(m.asset, m.start, m.end, now, s.name)
                if f:
                    rec["bn"] = f
            except Exception as e:
                rec["bn_err"] = str(e)[:100]
        if self.tw is not None:
            t0, c = self.tw.at(m.asset, m.start), self.tw.now(m.asset)
            if t0 and c:
                rec["cl_lead_bp"] = round((c[1] - t0) / t0 * 1e4 * (1 if s.name == "Up" else -1), 2)
                rec["cl_age_s"] = round(now - c[0], 1)
        self.n_probes += 1
        spawn(self.pj.write(rec))

    def on_quote(self, token: str, bid: Optional[float], ask: Optional[float], now: float) -> None:
        if token != self.m.tokens["Up"]:
            return                                   # сигнал считаем по книге Up (как в анализе)
        if bid is not None:
            self.up_bid = bid
        if ask is not None:
            self.up_ask = ask
        b, a = self.up_bid, self.up_ask
        if b is None or a is None:
            return
        el = now - self.m.start
        if el < 0 or now > self.m.end:
            return
        spread_ok = 0 < a - b <= Cfg.MAX_SPREAD + 1e-9
        for s in self.sides.values():
            if s.name == "Up":
                mid = (a + b) / 2 if spread_ok else None
                s_ask = a
            else:
                mid = 1 - (a + b) / 2 if spread_ok else None
                s_ask = 1 - b
            if mid is not None:
                if mid > s.lead:
                    s.lead = mid
                if mid >= Cfg.DROP_FROM - 1e-9:
                    s.last_hi = now
                if mid >= 0.90 - 1e-9:
                    s.last_hi90 = now
            # probe: первое касание каждого уровня за окно (после PROBE_FRAC_MIN и не в первые 5 с)
            if self.probe_on and s_ask > 0 and el >= 5 and el >= Cfg.PROBE_FRAC_MIN * self.m.length:
                for lvl in Cfg.PROBE_LEVELS:
                    if lvl not in s.probed and s_ask <= lvl + 1e-9:
                        s.probed.add(lvl)
                        try:
                            self.probe(s, lvl, now, round(s_ask, 4))
                        except Exception as e:
                            log.debug("probe %s: %s", self.m.slug, e)
            elif self.probe_on and s_ask > 0:
                # касания до PROBE_FRAC_MIN помечаем, чтобы позже писать только ПЕРВОЕ касание за окно
                for lvl in Cfg.PROBE_LEVELS:
                    if s_ask <= lvl + 1e-9:
                        s.probed.add(lvl)
            # бумажный налив поставленной лимитки
            if s.oid and Cfg.PAPER and not s.paper_filled and now >= s.placed_at + Cfg.PAPER_LAT_S \
                    and s_ask <= Cfg.ORDER_PX + 1e-9:
                s.paper_filled, s.paper_fill_t = True, now
                spawn(self.j.write({"event": "paper_fill", "slug": self.m.slug, "side": s.name,
                                    "el": round(now - self.m.start, 1), "ask": s_ask,
                                    "depth": self.depth_at(s.name, Cfg.ORDER_PX)}))
            if self.trade and not s.touched and s_ask <= Cfg.TRIGGER_ASK + 1e-9 and s_ask > 0:
                s.touched = True
                spawn(self.on_touch(s, now, s_ask))

    # --- сигнал
    async def on_touch(self, s: SideState, now: float, s_ask: float) -> None:
        m = self.m
        frac = (now - m.start) / m.length
        drop_s = now - s.last_hi
        dep = self.depth_at(s.name, Cfg.DEPTH_PX)
        info = dict(event="candidate", slug=m.slug, asset=m.asset, tf=m.tf, side=s.name,
                    el=round(now - m.start, 1), frac=round(frac, 3), lead=round(s.lead, 4),
                    drop_s=round(drop_s, 1) if drop_s < 1e8 else None, ask=s_ask,
                    depth=dep, depth03=self.depth_at(s.name, Cfg.SWEEP_PX)["shares"])
        sg = 1 if s.name == "Up" else -1
        # Chainlink TWAP (в памяти, без сети)
        cl = None
        if self.tw is not None:
            t0 = self.tw.at(m.asset, m.start)
            cur = self.tw.now(m.asset)
            if t0 and cur:
                cl = (cur[1] - t0) / t0 * 1e4 * sg
                info.update(cl_start=t0, cl_now=cur[1], cl_age_s=round(now - cur[0], 1), cl_lead_bp=round(cl, 2))
        cl_sig = cl / self.sigma if (cl is not None and self.sigma) else None
        if cl_sig is not None:
            info["cl_sig"] = round(cl_sig, 3)
        reason = None
        if frac < Cfg.FRAC_MIN:
            reason = "early"
        elif s.lead < Cfg.LEAD_MIN - 1e-9:
            reason = "no_lead"
        elif Cfg.MIN_REM_S.get(m.tf, 0) > 0 and m.end - now < Cfg.MIN_REM_S[m.tf]:
            reason = "late"
        elif Cfg.MAX_REM_S.get(m.tf, 0) > 0 and m.end - now > Cfg.MAX_REM_S[m.tf]:
            reason = "too_early"
        elif Cfg.DROP_S > 0 and drop_s > Cfg.DROP_S:
            reason = "slow"
        elif Cfg.DEPTH_MAX > 0 and dep["shares"] > Cfg.DEPTH_MAX + 1e-9:
            reason = "depth"
        elif Cfg.CL_SIG_MIN is not None:
            if cl_sig is None:
                reason = None if Cfg.CL_FAIL_OPEN else "cl_na"
            elif cl_sig < Cfg.CL_SIG_MIN - 1e-9:
                reason = "chainlink"
        if reason in ("early", "no_lead"):
            return                                   # не наш подтип — не пишем, чтобы не засорять журнал
        need_z = Cfg.PREV_Z_MIN is not None or Cfg.PREV_Z_MAX is not None
        if need_z:
            prev_z = await self._prev_z(s, now, info)
            if reason is None:
                if prev_z is None:
                    reason = None if Cfg.BN_FAIL_OPEN else "bn_na"
                elif Cfg.PREV_Z_MIN is not None and prev_z < Cfg.PREV_Z_MIN - 1e-9:
                    reason = "prev_z"
                elif Cfg.PREV_Z_MAX is not None and prev_z > Cfg.PREV_Z_MAX + 1e-9:
                    reason = "prev_z_max"
        if reason is None and Cfg.MAX_ORDERS_PER_HOUR > 0 and not self.rate.ok():
            reason = "rate"
        info["decision"] = ("sweep" if Cfg.ENTRY_MODE == "sweep" else "order") if reason is None else "skip"
        info["reason"] = reason
        s.cand = info
        sw = None
        if reason is None and Cfg.ENTRY_MODE == "sweep":
            sw = spawn(self.sweep(s))                # сразу, до записи журнала и запроса Binance
        if not need_z:
            await self._prev_z(s, now, info)         # только для журнала
        await self.j.write(info)
        if Cfg.AFTER_S:
            spawn(self.after_watch(s, now))
        log.warning("%s %s %s: касание %.3f на %.0f%% окна, лидер %.2f, обвал %.1fс, на дне %.0f долей, CL/σ %s → %s",
                    m.slug, s.name, "СИГНАЛ" if reason is None else "кандидат", s_ask, frac * 100, s.lead,
                    drop_s, dep["shares"], f"{cl_sig:+.2f}" if cl_sig is not None else "—",
                    ("ТЕЙКЕР до %.2f" % Cfg.SWEEP_PX if Cfg.ENTRY_MODE == "sweep" else "ЛИМИТКА 0.01")
                    if reason is None else reason)
        if reason is None:
            if sw is not None:
                await sw
            else:
                await self.place(s)

    async def after_watch(self, s: SideState, t0: float) -> None:
        """Книга стороны через AFTER_S секунд после касания — для разбора (торговлю не меняет)."""
        m = self.m
        pts = []
        for dt in Cfg.AFTER_S:
            await asyncio.sleep(max(0.0, t0 + dt - time.time()))
            b, a = self.up_bid, self.up_ask
            if s.name == "Up":
                sb, sa = b, a
            else:
                sb = round(1 - a, 4) if a is not None else None
                sa = round(1 - b, 4) if b is not None else None
            pts.append({"dt": dt, "dep01": self.depth_at(s.name, Cfg.DEPTH_PX)["shares"],
                        "dep03": self.depth_at(s.name, Cfg.SWEEP_PX)["shares"],
                        "ask": sa, "bid": sb, "mid": self.side_mid(s.name)})
        await self.j.write({"event": "cand_after", "slug": m.slug, "side": s.name, "tf": m.tf,
                            "decision": (s.cand or {}).get("decision"), "points": pts})

    async def _prev_z(self, s: SideState, now: float, info: dict) -> Optional[float]:
        """Binance: прошлая закрытая минута в пользу стороны, в σ (пишется в info)."""
        m = self.m
        try:
            if not self.sigma_done:
                await self.load_sigma()
            m0 = int(now // 60 * 60)
            pm = await asyncio.to_thread(self.bn.minute, m.asset, m0 - 60)
            if pm and self.sigma:
                sg = 1 if s.name == "Up" else -1
                move = (pm[1] - pm[0]) / pm[0] * 1e4 * sg
                prev_z = move / self.sigma
                info.update(prev_move_bp=round(move, 2), sigma_bp=round(self.sigma, 2), prev_z=round(prev_z, 2))
                return prev_z
        except Exception as e:
            info["bn_err"] = str(e)[:120]
        return None

    async def sweep(self, s: SideState) -> None:
        """Тейкер FAK: всё, что продают по <= SWEEP_PX, не больше SWEEP_USDC $. Бумага — по книге через PAPER_LAT_S."""
        m = self.m
        self.rate.add()
        tok = m.tokens[s.name]
        if not self.clob.enabled:
            await asyncio.sleep(Cfg.PAPER_LAT_S)
            left, sh, usd, fee = Cfg.SWEEP_USDC, 0.0, 0.0, 0.0
            for p, z in sorted(self.levels[tok]["asks"].items()):
                if p > Cfg.SWEEP_PX + 1e-9 or left <= 1e-9:
                    break
                take = min(z, left / p)
                sh, usd, left = sh + take, usd + take * p, left - take * p
                fee += Cfg.FEE_RATE * p * (1 - p) * take
            r = {"ok": sh > 0, "shares": round(sh, 2), "usdc": round(usd, 4), "paper": True}
        else:
            r = await asyncio.to_thread(self.clob.sweep, tok, Cfg.SWEEP_USDC, Cfg.SWEEP_PX, m.tick)
            sh, usd = float(r.get("shares") or 0), float(r.get("usdc") or 0)
            px = usd / sh if sh > 0 else 0.0
            fee = Cfg.FEE_RATE * px * (1 - px) * sh
            r["paper"] = False
        if sh > 0:
            s.sweep = (sh, usd, fee)
        await self.j.write({"event": "sweep", "slug": m.slug, "side": s.name, "cap": Cfg.SWEEP_PX,
                            "usdc_max": Cfg.SWEEP_USDC, "avg_px": round(usd / sh, 4) if sh > 0 else None,
                            "fee_est": round(fee, 4), **r})
        if sh > 0:
            log.warning("%s %s: тейкер — %.1f долей на $%.2f (ср. %.4f)", m.slug, s.name, sh, usd, usd / sh)
        else:
            log.warning("%s %s: тейкер — ничего не налилось%s %s", m.slug, s.name,
                        " (исход неизвестен!)" if r.get("unknown") else "", r.get("err") or "")

    async def place(self, s: SideState) -> None:
        m = self.m
        # биржа: срок GTD не меньше 180 с от «сейчас»; после конца окна лимитку снимаем по id сами
        exp = int(max(m.end + Cfg.GTD_PAD_S, time.time() + Cfg.GTD_MIN_S))
        self.rate.add()
        s.order_shares = Cfg.SHARES
        if not self.clob.enabled:
            s.oid, s.placed_at = f"paper-{m.slug}-{s.name}", time.time()
            await self.j.write({"event": "order", "slug": m.slug, "side": s.name, "px": Cfg.ORDER_PX,
                                "shares": Cfg.SHARES, "paper": True, "expiration": exp})
            return
        r = await asyncio.to_thread(self.clob.place, m.tokens[s.name], Cfg.ORDER_PX, Cfg.SHARES, m.tick, exp)
        await self.j.write({"event": "order", "slug": m.slug, "side": s.name, "px": Cfg.ORDER_PX,
                            "shares": Cfg.SHARES, "paper": False, "expiration": exp,
                            **{k: v for k, v in r.items() if k != "resp"},
                            "resp": {k: r.get("resp", {}).get(k) for k in ("status", "makingAmount", "takingAmount")}
                            if isinstance(r.get("resp"), dict) else None})
        if r.get("ok"):
            s.oid, s.placed_at = r["id"], time.time()
            log.warning("%s %s: лимитка 0.01 × %.0f поставлена, id %s, статус %s", m.slug, s.name,
                        Cfg.SHARES, r["id"][:14], r.get("status"))
        else:
            log.error("%s %s: лимитка не встала%s: %s", m.slug, s.name, " (исход неизвестен!)" if r.get("unknown") else "",
                      r.get("err"))

    # --- конец окна: снять лимитки, узнать налив, дождаться исхода
    async def finish(self, gamma: Gamma) -> None:
        m = self.m
        await asyncio.sleep(max(0.0, m.end + 3 - time.time()))
        fills: Dict[str, Tuple[float, float]] = {}
        for s in self.sides.values():
            if s.sweep:
                fills[s.name] = (s.sweep[0], s.sweep[1] + s.sweep[2])     # $ с оценкой комиссии
            if not s.oid:
                continue
            if not self.clob.enabled:
                if s.paper_filled:
                    fills[s.name] = (Cfg.SHARES, Cfg.SHARES * Cfg.ORDER_PX)
                continue
            o = await asyncio.to_thread(self.clob.order, s.oid) or {}
            matched = _num(o, "size_matched", "sizeMatched") or 0.0
            status = str(o.get("status") or "")
            if status.upper() in ("LIVE", "OPEN") or (matched < s.order_shares - 1e-9 and status.upper() not in ("CANCELED", "EXPIRED", "MATCHED")):
                ok = await asyncio.to_thread(self.clob.cancel, s.oid)
                await self.j.write({"event": "cancel", "slug": m.slug, "side": s.name, "id": s.oid, "ok": ok})
            px = _num(o, "price") or Cfg.ORDER_PX
            if matched > 0:
                fills[s.name] = (matched, matched * px)
            await self.j.write({"event": "fill" if matched > 0 else "no_fill", "slug": m.slug, "side": s.name,
                                "id": s.oid, "shares": matched, "usdc": round(matched * px, 4), "status": status})
        cands = {n: s.cand for n, s in self.sides.items() if s.cand}
        if not cands and not self.n_probes:
            return
        deadline = time.time() + Cfg.RESOLVE_TIMEOUT_S
        w = None
        while time.time() < deadline:
            w = await asyncio.to_thread(gamma.winner, m.slug)
            if w:
                break
            await asyncio.sleep(20)
        if self.n_probes:
            last = self.sides["Up"].hist[-1] if self.sides["Up"].hist else None
            await self.pj.write({"event": "probe_result", "slug": m.slug, "asset": m.asset, "tf": m.tf,
                                 "winner": w, "probes": self.n_probes,
                                 "up_last_mid": round(last[1], 4) if last and last[1] is not None else None})
        for name, c in cands.items():
            won = (w == name) if w else None
            rec = {"event": "result", "slug": m.slug, "side": name, "winner": w, "won": won,
                   "decision": c.get("decision"), "reason": c.get("reason"), "prev_z": c.get("prev_z"),
                   "drop_s": c.get("drop_s"), "frac": c.get("frac"), "lead": c.get("lead"),
                   "depth": (c.get("depth") or {}).get("shares"), "depth03": c.get("depth03"), "cl_sig": c.get("cl_sig")}
            await self.j.write(rec)
            if name in fills and w:
                sh, usd = fills[name]
                pnl = (sh if won else 0.0) - usd
                self.stats.add(won, usd, pnl)
                await self.j.write({"event": "resolved", "slug": m.slug, "side": name, "winner": w, "won": won,
                                    "shares": sh, "usdc": round(usd, 4), "pnl": round(pnl, 4),
                                    "paper": not self.clob.enabled})
                log.warning("РЕЗОЛВ %s %s: победил %s → %s %+.2f | %s", m.slug, name, w,
                            "WIN" if won else "loss", pnl, self.stats.line())
            elif c.get("decision") == "order" and name not in fills:
                log.info("%s %s: лимитка не налилась, исход %s", m.slug, name, w)


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
        return f"сессия: наливов {self.n}, побед {self.w}, вложено ${self.usd:.2f}, P&L ${self.pnl:+.2f}"


class Rate:
    def __init__(self):
        self.t: List[float] = []

    def ok(self) -> bool:
        now = time.time()
        self.t = [x for x in self.t if x > now - 3600]
        return len(self.t) < Cfg.MAX_ORDERS_PER_HOUR

    def add(self):
        self.t.append(time.time())


# ════════════════════════════════════════════════════════════════ книга по WS
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
                tk = ch.get("asset_id")
                if tk in eng.upd:
                    eng.upd[tk] += 1
                p, sz = _fnum(ch.get("price")), _fnum(ch.get("size"))
                if p is not None and sz is not None and ch.get("side"):
                    eng.on_levels(ch.get("asset_id"), str(ch.get("side")).upper(), p, sz)
                if "best_bid" in ch or "best_ask" in ch:
                    eng.on_quote(ch.get("asset_id"), _fnum(ch.get("best_bid")), _fnum(ch.get("best_ask")), now)
        elif et == "best_bid_ask":
            eng.on_quote(ev.get("asset_id"), _fnum(ev.get("best_bid")), _fnum(ev.get("best_ask")), now)
        elif et == "tick_size_change" and ev.get("new_tick_size"):
            eng.m.tick = str(ev["new_tick_size"])


async def watch(eng: Engine, gamma: Gamma, stop: asyncio.Event) -> None:
    m = eng.m
    if eng.trade:
        spawn(eng.load_sigma())
    if eng.probe_on:
        spawn(eng.sampler())
    fin = spawn(eng.finish(gamma))
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
    g = lambda v, f="{:g}": "выкл" if v is None else f.format(v)
    log.info("активы %s | ТФ %s | сигнал: лидер >= %.2f, касание аска <= %.3f после %.0f%% окна | обвал с %.2f за <= %s | "
             "prev_z [%s, %s] | на дне (<= %.2f) <= %s долей | Chainlink/σ >= %s%s",
             Cfg.ASSETS, Cfg.TFS, Cfg.LEAD_MIN, Cfg.TRIGGER_ASK, Cfg.FRAC_MIN * 100, Cfg.DROP_FROM,
             f"{Cfg.DROP_S:.0f}с" if Cfg.DROP_S > 0 else "выкл", g(Cfg.PREV_Z_MIN), g(Cfg.PREV_Z_MAX),
             Cfg.DEPTH_PX, f"{Cfg.DEPTH_MAX:g}" if Cfg.DEPTH_MAX > 0 else "выкл", g(Cfg.CL_SIG_MIN),
             " (нет данных — вход)" if Cfg.CL_FAIL_OPEN else "")
    if Cfg.ENTRY_MODE == "sweep":
        log.info("вход: тейкер FAK — всё по <= %.2f, не больше $%g за сигнал", Cfg.SWEEP_PX, Cfg.SWEEP_USDC)
    else:
        log.info("вход: лимитка %.2f × %.0f", Cfg.ORDER_PX, Cfg.SHARES)
    for tf in (5, 15):
        lo, hi = Cfg.MIN_REM_S[tf], Cfg.MAX_REM_S[tf]
        log.info("%dm: касание за %s до конца окна", tf,
                 (f"{lo:.0f}" if lo > 0 else "0") + "–" + (f"{hi:.0f} с" if hi > 0 else "любое время с") if (lo > 0 or hi > 0) else "любое время")
    stop = asyncio.Event()
    try:
        import signal as _sig
        loop = asyncio.get_running_loop()
        for sg in (_sig.SIGINT, _sig.SIGTERM):
            loop.add_signal_handler(sg, stop.set)
    except Exception:
        pass
    clob = Clob()
    gamma, bn, j, stats, rate = Gamma(), Binance(), Journal(Cfg.JOURNAL), Stats(), Rate()
    tw = Twap() if Cfg.TWAP_LOG else None
    if tw:
        spawn(tw.run(stop))
    pj = Journal(Cfg.PROBE_JOURNAL) if Cfg.PROBE else None
    bnl = None
    if Cfg.PROBE and Cfg.PROBE_BN:
        bnl = BnLive(Cfg.PROBE_ASSETS)
        spawn(bnl.run(stop))
    if Cfg.PROBE:
        log.info("probe: %s | ТФ %s | уровни %s | глубина <= %.3f | после %.0f%% окна → %s",
                 Cfg.PROBE_ASSETS, Cfg.PROBE_TFS, Cfg.PROBE_LEVELS, Cfg.PROBE_DEPTH, Cfg.PROBE_FRAC_MIN * 100,
                 Cfg.PROBE_JOURNAL)
    tfs = sorted(set(Cfg.TFS) | (set(Cfg.PROBE_TFS) if Cfg.PROBE else set()))
    assets = list(dict.fromkeys(Cfg.ASSETS + (Cfg.PROBE_ASSETS if Cfg.PROBE else [])))
    seen: Dict[str, asyncio.Task] = {}
    while not stop.is_set():
        now = time.time()
        for tf in tfs:
            L = tf * 60
            cur = int(now // L) * L
            for start in (cur, cur + L):
                if start + L <= now:
                    continue
                for a in assets:
                    slug = f"{a}-updown-{tf}m-{start}"
                    if slug in seen or (start > now + 90):
                        continue
                    trade = tf in Cfg.TFS and a in Cfg.ASSETS
                    if not trade and not (Cfg.PROBE and tf in Cfg.PROBE_TFS and a in Cfg.PROBE_ASSETS):
                        continue
                    mk = await asyncio.to_thread(gamma.market, a, tf, start)
                    if mk is None:
                        continue
                    seen[slug] = spawn(watch(Engine(mk, clob, bn, tw, j, stats, rate, trade, bnl, pj), gamma, stop))
        for slug in [s for s, t in seen.items() if t.done() and int(s.rsplit("-", 1)[1]) < now - 7200]:
            seen.pop(slug, None)
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
    log.info("останавливаюсь. %s", stats.line())
    pend = [t for t in seen.values() if not t.done()]
    if pend:
        log.info("жду завершения %d окон (снятие лимиток)…", len(pend))
        await asyncio.wait(pend, timeout=20)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
