#!/usr/bin/env python3
"""
autotaker — автопокупка стороны будущего спорщика по сильным сигналам фидов (без ручной проверки).

Как работает:
  * слушает новые предложения исхода UMA (ProposePrice) по рынкам Polymarket (Polygon);
  * теннис (ITF/ATP/WTA), футбол и киберспорт (Dota 2, LoL, Valorant, CS2) считает по фидам сам:
      теннис — Flashscore (живая лента), tennisexplorer, ESPN; футбол — ESPN + API-Football; киберспорт — OpenDota, Leaguepedia, bo3.gg;
  * если фид посчитал рынок и исход ≠ предложению, ни один фид не согласен с предложением, а тип рынка в «белом списке»
    (по бэктестам — без ложных) -> покупает тейкером (FAK) исход фида с потолком цены MAX_PRICE на TRADE_USD;
  * недолитое докупает каждые RETRY_SEC, пока есть аски <= потолка (не дольше RETRY_MIN);
  * в Telegram — только важное: покупки, сбои ордеров (исход неизвестен), «позиция есть, а спора нет, окно заканчивается»,
    спор по нашей позиции, итог рынка с PnL, дневная сводка, лимиты.

Бэктесты (споры за ~год; W = спорщик выиграл, L = проиграл, C = обычные предложения без спора):
  теннис «любой фид ≠, кроме 50-50»: W 133/141, L 0/339, C 0/1461
  футбол по белому списку типов: ~36/91 выигранных, ложных 0 на L 97 и C 1500
  киберспорт: Dota/LoL/Valorant без ложных, CS2 — только типы без ложных

Команды:
  python autotaker.py            — работа (в screen)
  python autotaker.py test       — проверка RPC/Gamma/Telegram/CLOB (в боевом режиме — подключение к CLOB)
  python autotaker.py check itf-epps1-laskev1-2026-10-12-match-total-21pt5 Over
                                 — что фиды скажут по рынку и купил бы бот (без покупки)
  python autotaker.py report     — сделки и PnL

pip install requests pycryptodome py_clob_client_v2
"""
import html
import json
import logging
import math
import os
import re
import signal
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from Crypto.Hash import keccak


# ----------------------------------------------------------------------------- конфиг
def _load_env_file() -> str:
    explicit = os.environ.get("ENV_FILE", "").strip()
    here = Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] else Path.cwd()
    for p in ([Path(explicit)] if explicit else [here / "autotaker.env", Path.cwd() / "autotaker.env"]):
        if not p.is_file():
            continue
        for raw in p.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = (x.strip() for x in line.split("=", 1))
            if v[:1] in ("'", '"') and v[-1:] == v[:1] and len(v) >= 2:
                v = v[1:-1]
            elif " #" in v:
                v = v[:v.find(" #")].rstrip()
            os.environ.setdefault(k, v)
        return str(p)
    if explicit:
        raise SystemExit(f"ENV_FILE={explicit} не найден")
    return ""


ENV_USED = _load_env_file()
def _env(k, d=""): return os.environ.get(k, d).strip()
def _f(k, d): v = _env(k); return float(v) if v else d
def _i(k, d): v = _env(k); return int(v) if v else d
def _b(k, d): v = _env(k).lower(); return d if not v else v in ("1", "true", "yes", "on")
def _set(k, d): return {c.strip() for c in _env(k, d).split(",") if c.strip()}


BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"


class Cfg:
    TG_TOKEN = _env("TG_TOKEN")
    TG_CHAT = _env("TG_CHAT_ID")
    GAMMA = _env("GAMMA_HOST", "https://gamma-api.polymarket.com")
    CLOB = _env("CLOB_HOST", "https://clob.polymarket.com")
    HOST = CLOB
    RPC = _env("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")
    LOG_RANGE = _i("LOG_MAX_RANGE", 2000)
    OO = [a.strip().lower() for a in _env("OO_ADDRESSES", "0x2c0367a9db231ddebd88a94b4f6461a6e47c58b1,"
                                          "0xee3afe347d5c74317041e2618c49534daf887c24").split(",") if a.strip()]
    ADAPTERS = [a.strip().lower() for a in _env("ADAPTERS", "0x2f5e3684cb1f318ec51b00edba38d79ac2c0aa9d,"
                "0x65070be91477460d8a7aeeb94ef92fe056c2f2a7,0x69c47de9d4d3dad79590d61b9e05918e03775f24,"
                "0x6a9d222616c90fca5754cd1333cfd9b7fb6a4f74,0x157ce2d672854c848c9b79c49a8cc6cc89176a49").split(",") if a.strip()]
    TZ_H = _f("TZ_OFFSET_H", 5)
    BACKFILL_MIN = _f("BACKFILL_MIN", 30)
    POLL_S = _f("POLL_SEC", 3)
    DB = _env("DB", "autotaker.sqlite")
    LOG_LEVEL = _env("LOG_LEVEL", "INFO")
    # --- торговля
    PAPER = _b("PAPER", True)
    CHAIN_ID = _i("CHAIN_ID", 137)
    PK = _env("PK")
    FUNDER = _env("FUNDER")
    SIGNATURE_TYPE = _i("SIGNATURE_TYPE", -1)
    BUILDER_FEE = _f("BUILDER_FEE", 0.01)      # доля от потраченного USDC (для PnL)
    API_KEY, API_SECRET, API_PASSPHRASE = _env("CLOB_API_KEY"), _env("CLOB_API_SECRET"), _env("CLOB_API_PASSPHRASE")
    TRADE_USD = _f("TRADE_USD", 20)                  # на один рынок
    MAX_PRICE = _f("MAX_PRICE", 0.90)                # потолок цены покупки
    MIN_ORDER = _f("MIN_ORDER_USD", 1)
    DAILY_USD = _f("DAILY_MAX_USD", 300)             # всего за сутки (UTC)
    EVENT_USD = _f("EVENT_MAX_USD", 100)             # на один матч (рынки одного матча ошибаются вместе)
    RETRY_S = _f("RETRY_SEC", 15)
    RETRY_MAX_S = _f("RETRY_MIN", 120) * 60
    STOP_BEFORE_S = _f("STOP_BEFORE_EXP_MIN", 5) * 60     # без спора не докупаем за N мин до конца оспаривания
    WARN_S = _f("NO_DISPUTE_WARN_MIN", 20) * 60          # позиция есть, спора нет, до конца окна < N мин -> сообщение
    FIRST_ONLY = _b("FIRST_PROPOSAL_ONLY", True)         # только первое предложение по рынку (бэктесты — по спору №1)
    TRADE_TENNIS = _b("TRADE_TENNIS", True)
    TRADE_FOOTBALL = _b("TRADE_FOOTBALL", True)
    TRADE_ESPORTS = _b("TRADE_ESPORTS", True)
    TN_CONFIRM_S = _f("TENNIS_TE_CONFIRM_MIN", 2) * 60   # только tennisexplorer (без явного статуса матча) — повтор через N мин
    TN_RECHECK_S = _f("TENNIS_RECHECK_SEC", 30)
    ES_RECHECK_S = _f("ESPORTS_RECHECK_SEC", 60)
    FOOT_CONFIRM_S = _f("FOOT_CONFIRM_MIN", 3) * 60      # футбол, один фид против: тот же исход через N мин -> покупка
    NOTIFY_SIGNAL_NOFILL = _b("NOTIFY_SIGNAL_NOFILL", True)   # сигнал был, но ни одной акции не купили — короткое сообщение
    DATA = _env("DATA_HOST", "https://data-api.polymarket.com")
    # сетка лимиток на сторону фида, если тейкер не налил (мёртвый стакан): «цена:$» через запятую, ниже потолка
    LADDER = [(float(x.split(":")[0]), float(x.split(":")[1])) for x in _env("LADDER", "0.30:3,0.15:3,0.08:3,0.04:3,0.02:3,0.01:3,0.005:2,0.002:2,0.001:2").split(",") if ":" in x]
    LADDER_ON = _b("LADDER_ON", True)
    LADDER_TTL_S = _f("LADDER_TTL_H", 72) * 3600          # лимитки живут N ч (GTD), дальше биржа снимает сама
    LADDER_DAILY_USD = _f("LADDER_DAILY_MAX_USD", 150)     # сколько $ в сутки можно поставить в сетки (резерв под лимитки)
    LADDER_CHECK_S = _f("LADDER_CHECK_SEC", 30)
    MIN_SHARES = _f("MIN_SHARES", 5)                       # минимальный ордер биржи в акциях


log = logging.getLogger("autotaker")
logging.basicConfig(level=getattr(logging, Cfg.LOG_LEVEL.upper(), logging.INFO),
                    format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%m-%d %H:%M:%S")
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("py_clob_client_v2").setLevel(logging.CRITICAL)


def hm(t: float, local=True) -> str:
    return time.strftime("%H:%M", time.gmtime(t + (Cfg.TZ_H * 3600 if local else 0)))
def jl(x, d=None):
    if x is None: return d
    if isinstance(x, (list, dict)): return x
    try: return json.loads(x)
    except Exception: return d
def esc(s) -> str: return html.escape(str(s or ""), quote=False)


# ----------------------------------------------------------------------------- БД
db = sqlite3.connect(Cfg.DB)
db.row_factory = sqlite3.Row
db.executescript("""
create table if not exists kv(k text primary key, v text);
create table if not exists props(tx text, li int, t int, block int, cid text, qid text, proposer text, kind text,
   proposed_out text, exp int, slug text, cat text, question text, url text, alerted int default 0, msg_id int,
   verdict text, verdict_t int, disputed_t int, winner text, wrong int, primary key(tx, li));
create index if not exists props_cid on props(cid);
create index if not exists props_proposer on props(proposer);
""")
db.commit()
def kv_get(k, d=None):
    r = db.execute("select v from kv where k=?", (k,)).fetchone(); return json.loads(r[0]) if r else d
def kv_set(k, v):
    db.execute("insert or replace into kv values(?,?)", (k, json.dumps(v))); db.commit()


# ----------------------------------------------------------------------------- HTTP / RPC
S = requests.Session()


def http_get(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=15)
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            return r.json() if r.status_code < 400 else None
        except Exception as e:
            if i == tries - 1:
                log.debug("GET %s: %s", url, e); return None
            time.sleep(0.7 * (i + 1))


def rpc(method, params, tries=4):
    for i in range(tries):
        try:
            j = S.post(Cfg.RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30).json()
            if "error" in j:
                raise RuntimeError(str(j["error"])[:200])
            return j["result"]
        except Exception:
            if i == tries - 1: raise
            time.sleep(1.5 * (i + 1))


def kec(b: bytes) -> bytes:
    h = keccak.new(digest_bits=256); h.update(b); return h.digest()


SIG_PROPOSE = "0x" + kec(b"ProposePrice(address,address,bytes32,uint256,bytes,int256,uint256,address)").hex()
SIG_DISPUTE = "0x" + kec(b"DisputePrice(address,address,address,bytes32,uint256,bytes,int256)").hex()
ONE, HALF, TOO_EARLY = 10 ** 18, 5 * 10 ** 17, -2 ** 255
_bts: Dict[int, int] = {}


def block_ts(n: int) -> int:
    if n not in _bts:
        if len(_bts) > 5000: _bts.clear()
        b = None
        for i in range(4):                       # свежий блок RPC иногда ещё не отдаёт (null) — ждём, иначе берём текущее время
            b = rpc("eth_getBlockByNumber", [hex(n), False])
            if b: break
            time.sleep(1.5)
        if not b: return int(time.time())
        _bts[n] = int(b["timestamp"], 16)
    return _bts[n]


def pad(a: str) -> str: return "0x" + "0" * 24 + a.lower()[2:]
def cid_of(adapter: str, qid: str) -> str:
    return "0x" + kec(bytes.fromhex(adapter[2:]) + bytes.fromhex(qid[2:]) + (2).to_bytes(32, "big")).hex()


def get_logs(a: int, b: int) -> list:
    """Кусок блоков подбирается сам и запоминается (бесплатные RPC режут диапазон)."""
    out, x = [], a
    step = int(kv_get("log_step") or Cfg.LOG_RANGE)
    topics = [[SIG_PROPOSE, SIG_DISPUTE], [pad(z) for z in Cfg.ADAPTERS]]
    while x <= b:
        y = min(x + step - 1, b)
        try:
            out += rpc("eth_getLogs", [{"fromBlock": hex(x), "toBlock": hex(y), "topics": topics, "address": Cfg.OO}],
                       tries=1 if step > 50 else 3)
            x = y + 1
        except Exception as e:
            if step <= 20: raise
            step = max(20, step // 2); kv_set("log_step", step)
            log.info("getLogs: RPC не принял диапазон, кусок -> %d блоков (%s)", step, str(e)[:80])
    return out


def _words(data: str) -> Tuple[bytes, List[bytes]]:
    b = bytes.fromhex(data[2:]); return b, [b[i:i + 32] for i in range(0, len(b), 32)]
def _int(x: bytes, s=False) -> int: return int.from_bytes(x, "big", signed=s)
def _dyn(b: bytes, off: int) -> bytes:
    n = _int(b[off:off + 32]); return b[off + 32: off + 32 + n]


def decode(lg: dict) -> dict:
    b, w = _words(lg["data"])
    base = dict(tx=lg["transactionHash"], li=int(lg["logIndex"], 16), block=int(lg["blockNumber"], 16),
                requester="0x" + lg["topics"][1][-40:])
    if lg["topics"][0] == SIG_PROPOSE:
        # data: identifier, timestamp, ancillaryData(off), proposedPrice, expirationTimestamp, currency
        anc = _dyn(b, _int(w[2]))
        base.update(ev="propose", proposer="0x" + lg["topics"][2][-40:], proposed=_int(w[3], True),
                    exp=_int(w[4]), anc=anc)
    else:
        # data: identifier, timestamp, ancillaryData(off), proposedPrice
        anc = _dyn(b, _int(w[2]))
        base.update(ev="dispute", proposed=_int(w[3], True), anc=anc)
    base["qid"] = "0x" + kec(base["anc"]).hex()
    base["cid"] = cid_of(base["requester"], base["qid"])
    return base


def kind_of(p: int) -> str:
    return "yes" if p == ONE else "no" if p == 0 else "50/50" if p == HALF else "too early" if p == TOO_EARLY else "other"


def anc_title(anc: bytes) -> str:
    m = re.search(rb"title:\s*(.+?)(?:,\s*description:|$)", anc, re.S)
    return (m.group(1) if m else anc[:150]).decode("utf-8", "replace").strip()


# ----------------------------------------------------------------------------- Gamma / стакан
def _norm(t: str) -> str: return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def market_for(cid: str, qid: str, title: str) -> Optional[dict]:
    for p in ({"condition_ids": cid}, {"condition_ids": cid, "closed": "true"}):
        for m in http_get(Cfg.GAMMA + "/markets", p) or []:
            if str(m.get("conditionId", "")).lower() == cid.lower(): return m
    for extra in (({}, {"closed": "true"}) if qid else ()):
        for x in http_get(Cfg.GAMMA + "/markets", dict(question_ids=qid, **extra)) or []:
            if str(x.get("questionID", "")).lower() == qid.lower(): return x
    if not title: return None
    j = http_get(Cfg.GAMMA + "/public-search", {"q": title[:120], "limit_per_type": 20, "events_status": "all"}) or {}
    for ev in j.get("events", []) or []:
        for x in ev.get("markets", []) or []:
            if _norm(x.get("question")) == _norm(title):
                for extra in ({}, {"closed": "true"}):
                    r = http_get(Cfg.GAMMA + "/markets", dict(slug=x.get("slug"), **extra))
                    if r:
                        full = r[0]; full.setdefault("events", [{"slug": ev.get("slug"), "id": ev.get("id")}]); return full
                x.setdefault("events", [{"slug": ev.get("slug"), "id": ev.get("id")}]); return x
    return None


def book_line(token: str) -> str:
    j = http_get(Cfg.CLOB + "/book", {"token_id": token}) or {}
    asks = sorted((float(a["price"]), float(a["size"])) for a in j.get("asks", []) if float(a.get("size") or 0) > 0)
    bids = sorted(((float(a["price"]), float(a["size"])) for a in j.get("bids", []) if float(a.get("size") or 0) > 0), reverse=True)
    b = f"{bids[0][0]:g}×{bids[0][1]:.0f}" if bids else "—"
    a = f"{asks[0][0]:g}×{asks[0][1]:.0f}" if asks else "—"
    return f"бид {b} | аск {a}"

# ----------------------------------------------------------------------------- Telegram
def tg(method: str, **payload) -> Optional[dict]:
    if not Cfg.TG_TOKEN: return None
    for i in range(3):
        try:
            r = S.post(f"https://api.telegram.org/bot{Cfg.TG_TOKEN}/{method}", json=payload, timeout=35)
            j = r.json()
            if j.get("ok"): return j.get("result")
            if r.status_code == 429:
                time.sleep(float((j.get("parameters") or {}).get("retry_after", 3))); continue
            log.warning("telegram %s: %s", method, str(j)[:200]); return None
        except Exception as e:
            if i == 2: log.warning("telegram %s: %s", method, e)
            time.sleep(1.5)
    return None


# ----------------------------------------------------------------------------- сверка с фидами (футбол)
# Футбольные предложения не шлются сразу: каждые RECHECK_MIN мин считаем рынок по ESPN и API-Football.
# Алерт — только если фид против предложения или фиды расходятся. Оба согласны — тишина.
# Фиды не смогли посчитать до конца окна — алерт «не проверено» только для рискованных рынков (NOFEED_ALERT).
import unicodedata
from datetime import datetime, timedelta, timezone


class FC:
    ON = _env("FEED_CHECK", "1") == "1"
    ESPN = _env("ESPN_HOST", "https://site.api.espn.com/apis/site/v2/sports/soccer")
    ESPN_MAP = dict(x.split(":", 1) for x in _env("ESPN_LEAGUES", "").split(",") if ":" in x)
    FOOT = set(ESPN_MAP) | {x.strip() for x in _env("EXTRA_FOOTBALL_PREFIXES").split(",") if x.strip()}
    AF_KEY = _env("API_FOOTBALL_KEY")
    AF_HOST = _env("API_FOOTBALL_HOST", "https://v3.football.api-sports.io")
    AF_DAILY = _i("API_FOOTBALL_DAILY_LIMIT", 7000)
    AF_DATE_TTL = _f("API_FOOTBALL_DATE_REFRESH_MIN", 360) * 60
    AF_INTERVAL = _f("API_FOOTBALL_MIN_INTERVAL_MIN", 0) * 60
    AF_ALWAYS = _env("API_FOOTBALL_ALWAYS", "1") == "1"
    FIRST = _f("FIRST_CHECK_MIN", 2) * 60
    RECHECK = _f("RECHECK_MIN", 8) * 60
    FINAL = _f("FINAL_BEFORE_MIN", 4) * 60
    NOFEED = _env("NOFEED_ALERT", "risk")            # risk | all | none
    RISK_RX = re.compile(_env("RISK_RX", r"^(efa|swe2|kor2|uwcl|chi|grc|itc|clf|sclc|lec|ukr1|rou1)-"), re.I)
    NOFEED_TRIES = _i("NOFEED_AFTER_CHECKS", 2)      # рискованный рынок без данных: алерт после N неудачных проверок (не ждём конца окна)
    AF_MIN_MINUTES = _i("API_FOOTBALL_MIN_MINUTES", 88)   # меньше — статистика ещё не досчитана
    # футбол из лиг вне ESPN_LEAGUES узнаём по типу рынка (пропы игроков с id, составы, угловые) -> сверка только API-Football
    FOOT_SUF_RX = re.compile(r"^(goals-plus-assists|assists|goals|shots-on-target|shots|saves|ags)-[a-z-]+-\d{6,}|^starting-eleven-|"
                             r"^corners-|^first-corner")
    NOFEED_UNKNOWN = _env("NOFEED_UNKNOWN_LEAGUE", "1") == "1"   # лига не в ESPN_LEAGUES и фиды не посчитали -> «не проверено»
    # американский спорт через ESPN: префикс -> путь ESPN (по очкам по четвертям/таймам)
    US_MAP = dict(x.split(":", 1) for x in _env("US_LEAGUES", "cfb:football/college-football,nfl:football/nfl").split(",") if ":" in x)
    US_GROUPS = {"cfb": ["80", "81"]}                     # FBS + FCS (без groups ESPN отдаёт только топ-матчи)
    ESPN_ROOT = _env("ESPN_ROOT", "https://site.api.espn.com/apis/site/v2/sports")
    CONFIRM = _f("CONFIRM_MIN", 6) * 60          # расхождение от одного фида подтверждаем повторной проверкой (фиды досчитывают стату)
    NOFEED_ONCE = _env("NOFEED_ONCE_PER_MATCH", "1") == "1"   # «не проверено» — одно сообщение на матч


class ES:
    ON = _env("ESPORTS_CHECK", "1") == "1"
    GAMES = {x.strip() for x in _env("ESPORTS_GAMES", "dota2,cs2,val,lol").split(",") if x.strip()}
    BO3_HOST = _env("BO3_HOST", "https://api.bo3.gg/api/v1")
    USE_BO3 = _env("USE_BO3", "1") == "1"
    LP_HOST = _env("LEAGUEPEDIA_HOST", "https://lol.fandom.com/api.php")
    LP_USER, LP_PASS = _env("LEAGUEPEDIA_USER"), _env("LEAGUEPEDIA_PASS")
    OD_HOST = _env("OPENDOTA_HOST", "https://api.opendota.com/api")
    OD_KEY = _env("OPENDOTA_KEY")
    OD_DAILY = _i("OPENDOTA_DAILY_LIMIT", 1900)
    NOFEED = _env("ESPORTS_NOFEED_ALERT", "1") == "1"      # матч не найден в OpenDota -> «не проверено» (одно на матч)


db.executescript("""
create table if not exists fq(tx text, li int, slug text, ev text, title text, outcomes text, proposed text, exp int,
   next_t int, state text, espn text, af text, espn_why text, af_why text, msg_id int, primary key(tx, li));
create index if not exists fq_state on fq(state, next_t);
""")
for _sql in ("alter table fq add column tries int default 0", "alter table props add column src text", "alter table fq add column conf int default 0"):
    try: db.execute(_sql)
    except sqlite3.OperationalError: pass
db.commit()

EV_RX = re.compile(r"^([a-z0-9]{2,8})-([a-z0-9]{2,12})-([a-z0-9]{2,12})-(20\d\d-\d\d-\d\d)-?(.*)$")
PROP_RX = re.compile(r"^(goals-plus-assists|assists|goals|shots-on-target|shots|saves|ags)-(.+?)(?:-(\d{3,}))?(?:-gte(\d+))?$")
XI_RX = re.compile(r"^starting-eleven-(home|away)-(.+?)(?:-(\d{3,}))?$")
SHOTS_RX = re.compile(r"-shots-(?!on-target)")
STOPW = {"al", "el", "saudi", "fc", "cf", "sc", "afc", "ac", "fk", "the", "club", "de", "cd", "sk", "if", "bk", "calcio", "football", "soccer", "sv", "vfl", "vfb"}


def is_football(slug):
    m = EV_RX.match(slug or "")
    if not m or m.group(1) in FC.US_MAP: return False
    return m.group(1) in FC.FOOT or bool(FC.FOOT_SUF_RX.search(m.group(5) or ""))


def is_es(slug):
    m = ES_SL_RX.match(slug or ""); return bool(ES.ON and m and m.group(1) in ES.GAMES)


def is_us(slug):
    m = EV_RX.match(slug or ""); return bool(m and m.group(1) in FC.US_MAP)


def nrm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def team_score(a, b):
    ta = {t for t in nrm(a).split() if t not in STOPW and len(t) > 1}; tb = {t for t in nrm(b).split() if t not in STOPW and len(t) > 1}
    if not ta or not tb: return 0
    pre = sum(1 for x in ta for y in tb if x != y and len(x) >= 4 and len(y) >= 4 and (x.startswith(y) or y.startswith(x)))
    return (len(ta & tb) + 0.5 * pre) / max(len(ta), len(tb))


def best_event(cands, home, away):
    best = None
    for hn, an, o in cands:
        sh, sa = team_score(home, hn), team_score(away, an); sw = team_score(home, an) + team_score(away, hn)
        sc = max(sh + sa, sw)
        if (min(sh, sa) > 0 or sw > 0.8) and (not best or sc > best[0]): best = (sc, o, sw > sh + sa)
    return best


def player_match(slug_name, players):
    st = [t for t in nrm(slug_name.replace("-", " ")).split() if t]
    best, bs, tie = None, 0, False
    for p in players:
        pt_ = nrm(p["name"]).split()
        joins = set(pt_) | {pt_[i] + pt_[i + 1] for i in range(len(pt_) - 1)} | {"".join(pt_)}
        sc = sum(1 for t in st if len(t) > 1 and t in joins)
        if sc == 0: continue
        if len(st) > 1 and sc < 2 and st[-1] not in joins: continue      # совпало только имя, фамилия другая
        if any(len(x) == 1 and any(t.startswith(x) for t in st) for x in pt_): sc += 0.5
        if len(st) > 1 and sc >= len(st): sc += 0.25
        if sc > bs: best, bs, tie = p, sc, False
        elif sc == bs: tie = True
    if not best: return None, "игрок не найден"
    if tie: return None, "несколько игроков с похожим именем"
    return best, ""


def pick_rel(outcomes, target, other):
    """исход, который ближе к target, чем к other (target/other — списки названий одной команды из разных источников)"""
    best, bs = None, 0.0
    for o in outcomes or []:
        sc = max((team_score(o, n) for n in target if n), default=0) - max((team_score(o, n) for n in other if n), default=0)
        if sc > bs: best, bs = o, sc
    return best


def side_names(D, sd):
    """названия стороны sd (1 = хозяева по Gamma) из фида и из заголовка Gamma"""
    return ([D.get("home"), D.get("g_home")], [D.get("away"), D.get("g_away")]) if sd == 1 else \
           ([D.get("away"), D.get("g_away")], [D.get("home"), D.get("g_home")])


def which_rel(name, D):
    h = max(team_score(name, n) for n in (D.get("home"), D.get("g_home")) if n)
    a = max(team_score(name, n) for n in (D.get("away"), D.get("g_away")) if n)
    return None if h == a == 0 else (1 if h > a else 2 if a > h else None)


def ival(x):
    try: return int(float(x))
    except Exception: return None


def swap(D):
    D["home"], D["away"] = D["away"], D["home"]
    for k in ("ft", "ht", "corners", "corners_1h"):
        if D.get(k): D[k] = tuple(D[k])[::-1]
    for p in D["players"]: p["side"] = {"h": "a", "a": "h"}[p["side"]]
    return D


def days3(date):
    d = datetime.fromisoformat(date)
    return [date, (d - timedelta(days=1)).strftime("%Y-%m-%d"), (d + timedelta(days=1)).strftime("%Y-%m-%d")]


_mc: Dict[str, Tuple[float, object]] = {}
def mcache(key, ttl, fn):
    v = _mc.get(key)
    if v and time.time() - v[0] < (v[2] if len(v) > 2 else ttl): return v[1]
    r = fn()
    _mc[key] = (time.time(), r) if r is not None else (time.time(), None, 60)   # отказ помним минуту — не долбим API
    if len(_mc) > 3000:
        for k in sorted(_mc, key=lambda z: _mc[z][0])[:1000]: _mc.pop(k, None)
    return r


# --- ESPN
def espn_data(lg, date, home, away):
    code = FC.ESPN_MAP.get(lg)
    if not code: return None, f"лиги {lg} нет в ESPN"
    cands = []
    for dd in days3(date):
        j = mcache(f"esb|{code}|{dd}", 300, lambda: http_get(f"{FC.ESPN}/{code}/scoreboard", {"dates": dd.replace("-", "")})) or {}
        for ev in j.get("events", []):
            cs = {c.get("homeAway"): c for c in ((ev.get("competitions") or [{}])[0]).get("competitors", [])}
            if "home" in cs and "away" in cs:
                cands.append((cs["home"]["team"].get("displayName", ""), cs["away"]["team"].get("displayName", ""), ev))
        b = best_event(cands, home, away)
        if b and b[0] >= 1.2: break
    b = best_event(cands, home, away)
    if not b: return None, "матч не найден"
    ev, swapped = b[1], b[2]
    if not ((ev.get("status") or {}).get("type") or {}).get("completed"): return None, "матч не завершён"
    sm = mcache(f"ess|{code}|{ev['id']}", 300, lambda: http_get(f"{FC.ESPN}/{code}/summary", {"event": ev["id"]})) or {}
    cs = {c.get("homeAway"): c for c in (((sm.get("header") or {}).get("competitions") or [{}])[0]).get("competitors", [])}
    if "home" not in cs: return None, "нет данных summary"
    lines = lambda c: [ival(x.get("displayValue", x.get("value"))) for x in c.get("linescores", [])]
    lh, la = lines(cs["home"]), lines(cs["away"])
    D = dict(src="ESPN", home=cs["home"]["team"].get("displayName"), away=cs["away"]["team"].get("displayName"),
             ft=(ival(cs["home"].get("score")), ival(cs["away"].get("score"))), ht=(lh[0] if lh else None, la[0] if la else None),
             players=[], corners=None, corners_1h=None, stale=False)
    if len(lh) >= 2 and len(la) >= 2 and None not in lh[:2] + la[:2]: D["ft"] = (lh[0] + lh[1], la[0] + la[1])
    side_of = {cs["home"]["team"].get("id"): "h", cs["away"]["team"].get("id"): "a"}
    team = {}
    for t in (sm.get("boxscore") or {}).get("teams", []):
        side = side_of.get((t.get("team") or {}).get("id")) or ("h" if t.get("homeAway") == "home" else "a")
        team[side] = {x.get("name"): ival(x.get("displayValue", x.get("value"))) for x in t.get("statistics", [])}
    ch, ca = (team.get("h") or {}).get("wonCorners"), (team.get("a") or {}).get("wonCorners")
    if ch is not None and ca is not None and (ch, ca) != (0, 0): D["corners"] = (ch, ca)   # 0:0 у ESPN = «нет данных»
    for r in sm.get("rosters", []):
        side = side_of.get((r.get("team") or {}).get("id")) or ("h" if r.get("homeAway") == "home" else "a")
        for p in r.get("roster", []):
            stt = {x.get("name"): ival(x.get("value", x.get("displayValue"))) or 0 for x in p.get("stats", [])}
            a = p.get("athlete") or {}
            D["players"].append(dict(name=a.get("displayName") or a.get("fullName") or "", side=side, starter=bool(p.get("starter")),
                                     goals=stt.get("totalGoals", 0), assists=stt.get("goalAssists", 0), saves=stt.get("saves", 0),
                                     shots=stt.get("totalShots", 0), sot=stt.get("shotsOnTarget", 0)))
    return (swap(D) if swapped else D), ""


# --- API-Football (бесплатный тариф: вчера/сегодня/завтра, ~100 запросов в день; детали пачкой до 20 матчей)
AF_ERR = [""]
def af_req(path, params):
    if not FC.AF_KEY: return None
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    used = kv_get("af_used_" + day, 0)
    if used >= FC.AF_DAILY: AF_ERR[0] = f"дневной лимит {FC.AF_DAILY}"; return None
    try:
        r = S.get(FC.AF_HOST + path, params=params, headers={"x-apisports-key": FC.AF_KEY}, timeout=30); j = r.json()
    except Exception as e:
        log.warning("API-Football %s: %s", path, e); return None
    kv_set("af_used_" + day, used + 1)
    if j.get("errors") and not j.get("response"):
        AF_ERR[0] = str(j.get("errors"))[:80]; log.warning("API-Football %s: %s", path, j.get("errors")); return None
    AF_ERR[0] = ""
    return j


def af_fixture(date, home, away):
    cands, answered = [], False
    for dd in days3(date):
        j = mcache(f"afd|{dd}", FC.AF_DATE_TTL, lambda: af_req("/fixtures", {"date": dd}))
        if j is not None: answered = True
        for f in (j or {}).get("response", []):
            cands.append((f["teams"]["home"]["name"], f["teams"]["away"]["name"], f))
        b = best_event(cands, home, away)
        if b and b[0] >= 1.2: break
    b = best_event(cands, home, away)
    if b: return b[1]["fixture"]["id"], b[2], ""
    return None, False, ("матч не найден" if answered else "нет ответа API-Football (лимит/ошибка)")


def af_details(fids):
    """{fid: fixture с events/statistics/players} — пачками по 20 (1 запрос на 20 матчей), кэш 6 мин"""
    out, need = {}, []
    for f in fids:
        v = _mc.get(f"afx|{f}")
        if v and time.time() - v[0] < 360: out[f] = v[1]
        else: need.append(f)
    for i in range(0, len(need), 20):
        ch = need[i:i + 20]
        j = af_req("/fixtures", {"ids": "-".join(str(x) for x in ch)}) or {}
        for f in j.get("response", []):
            fid = f["fixture"]["id"]; _mc[f"afx|{fid}"] = (time.time(), f); out[fid] = f
    return out


def af_parse(f, swapped):
    if f["fixture"]["status"]["short"] not in ("FT", "AET", "PEN"): return None, f"статус {f['fixture']['status']['short']}"
    hid = f["teams"]["home"]["id"]
    sc = f.get("score") or {}; ft = sc.get("fulltime") or f.get("goals") or {}; ht = sc.get("halftime") or {}
    D = dict(src="API-Football", home=f["teams"]["home"]["name"], away=f["teams"]["away"]["name"],
             ft=(ft.get("home"), ft.get("away")), ht=(ht.get("home"), ht.get("away")), players=[], corners=None, corners_1h=None)
    team = {}
    for t in f.get("statistics") or []:
        team["h" if t["team"]["id"] == hid else "a"] = {x["type"]: x["value"] for x in t.get("statistics", [])}
    ch, ca = (team.get("h") or {}).get("Corner Kicks"), (team.get("a") or {}).get("Corner Kicks")
    if ch is not None and ca is not None: D["corners"] = (ival(ch), ival(ca))
    xi = {}                                                           # старт берём из составов: флаг substitute в players врёт
    for L in f.get("lineups") or []:
        for q in L.get("startXI") or []:
            pl = q.get("player") or {}
            if pl.get("id"): xi[pl["id"]] = True
    mx = 0
    for t in f.get("players") or []:
        side = "h" if t["team"]["id"] == hid else "a"
        for p in t.get("players", []):
            s = (p.get("statistics") or [{}])[0]; g = s.get("goals") or {}; sh = s.get("shots") or {}; gm = s.get("games") or {}
            mx = max(mx, gm.get("minutes") or 0)
            D["players"].append(dict(name=p["player"]["name"], side=side, starter=(p["player"].get("id") in xi) if xi else None,
                                     goals=g.get("total") or 0, assists=g.get("assists") or 0, saves=g.get("saves") or 0,
                                     shots=sh.get("total") or 0, sot=sh.get("on") or 0))
    D["stale"] = bool(D["players"]) and mx < FC.AF_MIN_MINUTES        # статистика ещё не досчитана (бывает «застывание» на 84')
    return (swap(D) if swapped else D), ""


# --- ESPN: американский спорт (очки по периодам)
def espn_us(lg, date, home, away):
    path = FC.US_MAP[lg]
    cands = []
    for dd in days3(date):
        for gr in FC.US_GROUPS.get(lg, [""]):
            prm = {"dates": dd.replace("-", ""), "limit": 500}
            if gr: prm["groups"] = gr
            j = mcache(f"esu|{path}|{dd}|{gr}", 240, lambda: http_get(f"{FC.ESPN_ROOT}/{path}/scoreboard", prm)) or {}
            for ev in j.get("events", []):
                comp = (ev.get("competitions") or [{}])[0]
                cs = {c.get("homeAway"): c for c in comp.get("competitors", [])}
                if "home" in cs and "away" in cs:
                    nm = lambda c: (c.get("team") or {}).get("location") or (c.get("team") or {}).get("displayName", "")
                    cands.append((nm(cs["home"]), nm(cs["away"]), (ev, comp, cs)))
        b = best_event(cands, home, away)
        if b and b[0] >= 1.6: break
    b = best_event(cands, home, away)
    if not b: return None, "матч не найден"
    (ev, comp, cs), swapped = b[1], b[2]
    lines = lambda c: [ival(x.get("value", x.get("displayValue"))) for x in c.get("linescores", [])]
    st = (comp.get("status") or ev.get("status") or {})
    D = dict(src="ESPN", home=(cs["home"]["team"] or {}).get("displayName"), away=(cs["away"]["team"] or {}).get("displayName"),
             lh=lines(cs["home"]), la=lines(cs["away"]), period=st.get("period") or 0,
             state=(st.get("type") or {}).get("name", ""), done=bool((st.get("type") or {}).get("completed")),
             ft=(ival(cs["home"].get("score")), ival(cs["away"].get("score"))), players=[], corners=None, corners_1h=None)
    if swapped:
        D["home"], D["away"], D["lh"], D["la"], D["ft"] = D["away"], D["home"], D["la"], D["lh"], D["ft"][::-1]
    return D, ""


US_RX = re.compile(r"^(?:(1q|2q|3q|4q|1h|2h)-)?(total|spread-(?:home|away)|team-total-[a-z0-9]+|moneyline|btts-points)(?:-(\d+pt\d+))?$")
US_PER = {"1q": [0], "2q": [1], "3q": [2], "4q": [3], "1h": [0, 1], "2h": "2h", "": "all"}


def evaluate_us(slug, outcomes, D, question=""):
    m = EV_RX.match(slug); suf = m.group(5) or ""
    yes, no = (outcomes + ["", ""])[:2]
    x = US_RX.match(suf) if suf else None
    if suf and not x: return None, "тип рынка не разобран"
    per, kind, line = (x.group(1) or "", x.group(2), x.group(3)) if x else ("", "moneyline", None)
    lh, la = D["lh"], D["la"]
    idx = US_PER[per]
    if idx == "all": idx = list(range(max(len(lh), len(la))))
    elif idx == "2h": idx = list(range(2, max(len(lh), len(la), 4)))
    last = max(idx) if idx else 0
    finished = D["done"] or D["period"] > last + 1 or (D["state"] in ("STATUS_HALFTIME",) and last <= 1) or \
        (D["state"] == "STATUS_END_PERIOD" and D["period"] == last + 1)
    if per in ("", "2h") and not D["done"]: finished = False
    if not finished: return None, f"период ещё идёт ({D['state']}, период {D['period']})"
    if len(lh) <= last or len(la) <= last or None in [lh[i] for i in idx] + [la[i] for i in idx]: return None, "нет очков по периодам"
    p1, p2 = sum(lh[i] for i in idx), sum(la[i] for i in idx)
    ot = " (с ОТ)" if per in ("", "2h") and len(lh) > 4 else ""
    why = f"{per or 'матч'} {p1}:{p2}{ot}"
    if kind == "btts-points": return (yes if p1 > 0 and p2 > 0 else no), why
    if kind == "total": return ou(outcomes, p1 + p2 > ptv(line)), why
    if kind.startswith("team-total"):
        tq = re.split(r"\s+(?:1Q|2Q|3Q|4Q|1H|2H)?\s*Team Total", question or "", flags=re.I)[0].strip()
        sd = which_rel(tq, D) if tq else None
        if not sd: return None, "командный тотал: не сопоставил команду"
        return ou(outcomes, (p1 if sd == 1 else p2) > ptv(line)), why
    def team_out(sd):
        return pick_rel(outcomes, *side_names(D, sd))
    if kind == "moneyline":
        if p1 == p2: return (pick(outcomes, "draw") if any(nrm(o).startswith(("draw", "tie")) for o in outcomes) else None), why + " ничья"
        return team_out(1 if p1 > p2 else 2) or None, why
    if kind.startswith("spread"):
        fm = re.search(r":\s*(.+?)\s*\(-([\d.]+)\)", question or "")
        if not fm: return None, "фора: не разобрал вопрос"
        sd = which_rel(fm.group(1), D)
        if not sd: return None, "фора: не сопоставил команду"
        cov = ((p1 - p2) if sd == 1 else (p2 - p1)) > float(fm.group(2))
        return team_out(sd if cov else 3 - sd), why
    return None, "тип рынка не разобран"


# --- киберспорт: OpenDota (перенесено из esportscheck.py)
ES_SL_RX = re.compile(r"^(cs2|dota2|lol|val)-(.+)-(20\d\d-\d\d-\d\d)(?:-(.+))?$")
def es_norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


ES_STOP = {"team", "esports", "esport", "gaming", "the", "club", "academy", "gg", "e", "sports", "org"}
def es_toks(s): return {t for t in es_norm(s).split() if t not in ES_STOP and len(t) > 1} or set(es_norm(s).split())
def es_tscore(a, b):
    """похожесть названий команд 0..1 (учитывает теги: 'VP' ~ 'Virtus.pro' через склейку/префиксы)"""
    if not a or not b: return 0
    na, nb = es_norm(a).replace(" ", ""), es_norm(b).replace(" ", "")
    if na and na == nb: return 1.0
    ta, tb = es_toks(a), es_toks(b)
    inter = len(ta & tb)
    pre = sum(1 for x in ta for y in tb if x != y and min(len(x), len(y)) >= 3 and (x.startswith(y) or y.startswith(x)))
    sc = (inter + 0.6 * pre) / max(len(ta), len(tb), 1)
    if len(na) >= 3 and len(nb) >= 3 and (na in nb or nb in na): sc = max(sc, 0.6)
    for x, y in ((a, b), (b, a)):                                  # аббревиатура: NIP ~ Ninjas in Pyjamas, TL ~ Team Liquid
        ws = es_norm(y).split()
        if 2 <= len(es_norm(x).replace(" ", "")) <= 5 and " " not in es_norm(x) and len(ws) >= 2 and es_norm(x) == "".join(w[0] for w in ws):
            sc = max(sc, 0.9)
    return sc


def es_orient(a, b, x, y):
    """команды события (a,b) и фида (x,y) -> (score, swapped)"""
    st, sw = es_tscore(a, x) + es_tscore(b, y), es_tscore(a, y) + es_tscore(b, x)
    return (sw, True) if sw > st else (st, False)


# ----------------------------------------------------------------------------- рынки Polymarket
def es_teams_of(title):
    """'Counter-Strike: Vitality vs MOUZ (BO3) - IEM …' -> ('Vitality', 'MOUZ', 3)"""
    t = re.sub(r"^[^:]{2,25}:\s*", "", title or "")
    bo = re.search(r"\(BO(\d)\)", t, re.I)
    t = re.split(r"\s*\(BO\d\)", t, flags=re.I)[0]
    t = re.split(r"\s+-\s+", t)[0]
    x = re.split(r"\s+vs\.?\s+", t, maxsplit=1, flags=re.I)
    return (x[0].strip(), x[1].strip(), int(bo.group(1)) if bo else None) if len(x) == 2 else ("", "", None)


def es_side_swap(Sr):
    f = lambda v: tuple(v)[::-1] if isinstance(v, (list, tuple)) else ({1: 2, 2: 1}.get(v, v))
    Sr["t1"], Sr["t2"] = Sr["t2"], Sr["t1"]
    for mp in Sr["maps"]:
        for k in ("win", "kills", "rounds", "fb", "baron", "dragon", "inhib", "tower", "roshan", "rax"):
            if mp.get(k) is not None: mp[k] = f(mp[k])
    return Sr


def es_pick_series(rows, a, b, date, key_fn, names_fn, t_fn):
    """rows фида за окно дат -> карты нужной серии (сгруппированы key_fn), ориентированные под (a,b)"""
    groups = defaultdict(list)
    for r in rows:
        x, y = names_fn(r)
        sc, sw = es_orient(a, b, x, y)
        if sc >= 0.9 and min(max(es_tscore(a, x), es_tscore(a, y)), max(es_tscore(b, x), es_tscore(b, y))) > 0: groups[key_fn(r)].append((sc, sw, r))
    if not groups: return None
    d0 = datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp() + 43200
    best = min(groups.values(), key=lambda g: (-max(s for s, _, _ in g), abs(min(t_fn(r) for _, _, r in g) - d0)))
    return sorted(best, key=lambda z: t_fn(z[2]))


# ----------------------------------------------------------------------------- OpenDota
_od_last = [0.0]
def es_od_get(path, params=None, ttl=300):
    """OpenDota с кэшем в памяти и суточным лимитом (без ключа ~2000/сутки)"""
    key = "od|" + path + "?" + json.dumps(params or {}, sort_keys=True)
    v = _mc.get(key)
    if v and time.time() - v[0] < (v[2] if len(v) > 2 else ttl): return v[1]
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    used = kv_get("od_used_" + day, 0)
    if used >= ES.OD_DAILY: return None
    time.sleep(max(0.0, 1.1 - (time.time() - _od_last[0]))); _od_last[0] = time.time()
    p = dict(params or {})
    if ES.OD_KEY: p["api_key"] = ES.OD_KEY
    j = http_get(ES.OD_HOST + path, p)
    kv_set("od_used_" + day, used + 1)
    if j is None or (isinstance(j, dict) and j.get("error")):
        if isinstance(j, dict): log.warning("OpenDota %s: %s", path, str(j.get("error"))[:120])
        _mc[key] = (time.time(), None, 60)                  # отказ помним минуту, чтобы не долбить
        return None
    _mc[key] = (time.time(), j, ttl)
    return j


def es_od_day(date):
    """матчи за окно дат: explorer (история) + proMatches (последние минуты, explorer может отставать)"""
    d = datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
    t0, t1 = int((d - timedelta(days=2)).timestamp()), int((d + timedelta(days=3)).timestamp())
    sql = ("SELECT m.match_id, m.start_time, m.duration, m.radiant_win, m.radiant_score, m.dire_score, m.series_id, m.series_type, "
           "m.radiant_team_id, m.dire_team_id, rt.name AS rname, rt.tag AS rtag, dt.name AS dname, dt.tag AS dtag "
           "FROM matches m LEFT JOIN teams rt ON rt.team_id = m.radiant_team_id LEFT JOIN teams dt ON dt.team_id = m.dire_team_id "
           f"WHERE m.start_time >= {t0} AND m.start_time < {t1} ORDER BY m.start_time")
    rows = {r["match_id"]: r for r in ((es_od_get("/explorer", {"sql": sql}, ttl=600) or {}).get("rows") or [])}
    for r in es_od_get("/proMatches", ttl=120) or []:
        if t0 <= (r.get("start_time") or 0) < t1 and r.get("match_id") not in rows:
            rows[r["match_id"]] = dict(r, rname=r.get("radiant_name"), dname=r.get("dire_name"))
    return sorted(rows.values(), key=lambda r: r.get("start_time") or 0)


def es_od_series(date, a, b):
    rows = es_od_day(date)
    if not rows: return None, "OpenDota: нет матчей за даты (или ошибка explorer)"
    nm = lambda r: (r.get("rname") or r.get("rtag") or "", r.get("dname") or r.get("dtag") or "")
    def key(r):
        if r.get("series_id"): return ("s", r["series_id"])
        x, y = sorted(es_norm(v) for v in nm(r)); return ("t", x, y, int(r["start_time"] // 21600))
    g = es_pick_series(rows, a, b, date, key, nm, lambda r: r["start_time"])
    if not g:
        # вторая попытка: имена в одной строке 'tag' (короткие) — score может быть мал; ослабляем порог
        return None, "матч не найден"
    Sr = dict(src="OpenDota", t1=a, t2=b, maps=[], note="")
    for n, (sc, sw, r) in enumerate(g, 1):
        md = es_od_get(f"/matches/{r['match_id']}", ttl=21600) or {}
        if md and md.get("objectives") is None:                 # ещё не разобран: перечитывать через 5 мин и попросить разбор
            _mc["od|/matches/%s?{}" % r["match_id"]] = (time.time(), md, 300)
            if not kv_get(f"od_parse_{r['match_id']}"):
                try: S.post(f"{ES.OD_HOST}/request/{r['match_id']}", timeout=20); kv_set(f"od_parse_{r['match_id']}", 1)
                except Exception: pass
        rad_is_a = not sw                                     # radiant = команда a ?
        side = (lambda radiant: 1 if radiant == rad_is_a else 2)
        mp = dict(n=n, id=r["match_id"], dur=md.get("duration", r.get("duration")))
        mp["win"] = side(True) if (md.get("radiant_win", r.get("radiant_win"))) else side(False)
        rk, dk = md.get("radiant_score", r.get("radiant_score")), md.get("dire_score", r.get("dire_score"))
        mp["kills"] = ((rk, dk) if rad_is_a else (dk, rk)) if rk is not None and dk is not None else None
        objs = md.get("objectives")
        if objs is not None:
            fb = next((o for o in objs if o.get("type") == "CHAT_MESSAGE_FIRSTBLOOD"), None)
            if fb is not None and fb.get("player_slot") is not None: mp["fb"] = side(fb["player_slot"] < 128)
            rosh = [o.get("team") for o in objs if o.get("type") == "CHAT_MESSAGE_ROSHAN_KILL"]
            rr, dr = sum(1 for t in rosh if t == 2), sum(1 for t in rosh if t == 3)
            mp["roshan"] = (rr, dr) if rad_is_a else (dr, rr)
        br, bd = md.get("barracks_status_radiant"), md.get("barracks_status_dire")
        if br is not None and bd is not None:
            rad_killed, dire_killed = bd != 63, br != 63          # radiant снёс бараки dire, и наоборот
            mp["rax"] = (rad_killed, dire_killed) if rad_is_a else (dire_killed, rad_killed)
        pls = md.get("players") or []
        if pls and any(p.get("multi_kills") is not None for p in pls):
            mk = Counter()
            for p in pls:
                for k, v in (p.get("multi_kills") or {}).items(): mk[int(k)] += v or 0
            mp["multi"] = {"ultra": sum(v for k, v in mk.items() if k >= 4), "rampage": sum(v for k, v in mk.items() if k >= 5)}
        if objs is None: mp["note"] = "матч не разобран (нет replay-парсинга)"
        Sr["maps"].append(mp)
    return Sr, ""




def es_pick_team(outcomes, name, alt=""):
    best = max(outcomes, key=lambda o: max(es_tscore(o, name), es_tscore(o, alt) if alt else 0)) if outcomes else None
    return best if best and max(es_tscore(best, name), es_tscore(best, alt) if alt else 0) > 0 else None


def es_pick_word(outcomes, word):
    for o in outcomes:
        if es_norm(o).startswith(word): return o
    return None


def es_which_side(name, a, b):
    sa, sb = es_tscore(name, a), es_tscore(name, b)
    return None if sa == sb == 0 else (1 if sa >= sb else 2)


def es_handicap_parse(q):
    """'Map 1 Rounds Handicap: Brute (-3.5) vs Inner Circle (+3.5)' -> ('Brute', 3.5)"""
    rest = q.split(":", 1)[1] if ":" in q else q
    m = re.match(r"\s*(.+?)\s*\(-([\d.]+)\)", rest)
    if not m: return None, None
    fav = re.sub(r"\s+(drake|kill|tower|map|game|rounds?|inhibitor|nashor|baron)\s+handicap$", "", m.group(1), flags=re.I).strip()
    return fav, float(m.group(2))


def es_fav_side(q, suf, a, b):
    """сторона с минусовой форой: из вопроса, иначе из slug (away = первая команда в заголовке, home = вторая)"""
    fav, line = es_handicap_parse(q)
    s = es_which_side(fav or "", a, b) if fav else None
    hm = re.search(r"-(home|away)-(\d+)pt(\d+)$", suf)
    if line is None and hm: line = float(f"{hm.group(2)}.{hm.group(3)}")
    if not s and hm: s = 1 if hm.group(1) == "away" else 2
    return s, line


def es_evaluate(slug, g, Sr):
    m = ES_SL_RX.match(slug)
    suf = m.group(4) or ""
    outs = g.get("outcomes") or []; q = g.get("question") or ""
    a, b = Sr["t1"], Sr["t2"]; maps = Sr["maps"]
    yes, no = (es_pick_word(outs, "yes") or (outs + ["", ""])[0]), (es_pick_word(outs, "no") or (outs + ["", ""])[1])
    team_out = lambda s: es_pick_team(outs, a if s == 1 else b)
    w1, w2 = Sr.get("score") or (sum(1 for mp in maps if mp.get("win") == 1), sum(1 for mp in maps if mp.get("win") == 2))
    if not maps: return None, "нет карт"
    mg = re.match(r"^game(\d)(?:-(.+))?$", suf)
    mp = None
    if mg:
        n = int(mg.group(1))
        if n > len(maps): return None, f"у фида {len(maps)} карт, нужна {n}"
        mp = maps[n - 1]; suf2 = mg.group(2) or ""
        if not suf2:
            return (team_out(mp["win"]), f"карту {n} выиграла сторона {mp['win']}") if mp.get("win") else (None, "нет победителя карты")
        def need(k):
            v = mp.get(k)
            if v is None: raise LookupError(f"нет данных: {k}" + (f" ({mp['note']})" if mp.get("note") else ""))
            return v
        try:
            x = re.match(r"^kill-over-(\d+pt5)$", suf2)
            if x:
                k = sum(need("kills")); return es_pick_word(outs, "over" if k > float(x.group(1).replace("pt", ".")) else "under"), f"киллов {k}"
            if suf2 == "odd-even-total-kills":
                k = sum(need("kills")); return es_pick_word(outs, "odd" if k % 2 else "even"), f"киллов {k}"
            if suf2 == "odd-even-total-rounds":
                k = sum(need("rounds")); return es_pick_word(outs, "odd" if k % 2 else "even"), f"раундов {k}"
            x = re.match(r"^round-total-(\d+pt5)$", suf2)
            if x:
                k = sum(need("rounds")); return es_pick_word(outs, "over" if k > float(x.group(1).replace("pt", ".")) else "under"), f"раундов {k}"
            if suf2.startswith("round-handicap"):
                r = need("rounds"); fav, line = es_handicap_parse(q); s = es_which_side(fav or "", a, b)
                if not s:
                    ab = re.match(r"round-handicap-([a-z0-9]+)-\d+pt\d+$", suf2); sl = ES_SL_RX.match(slug).group(2).split("-")
                    if ab and ab.group(1) in sl: s = 1 if sl.index(ab.group(1)) == 0 else 2
                if not s: return None, "фора: не сопоставил команду"
                cov = (r[s - 1] - r[2 - s]) > line
                return team_out(s if cov else 3 - s), f"раунды {r[0]}:{r[1]}"
            if suf2 == "first-blood":
                s = need("fb"); return team_out(s), f"первая кровь: сторона {s}"
            x = re.match(r"^both-teams-(beat-roshan|destroy-barracks|destroy-inhibitors|slay-baron|slay-dragon)$", suf2)
            if x:
                k = {"beat-roshan": "roshan", "destroy-barracks": "rax", "destroy-inhibitors": "inhib", "slay-baron": "baron",
                     "slay-dragon": "dragon"}[x.group(1)]
                v = need(k); both = bool(v[0]) and bool(v[1]); return (yes if both else no), f"{k} {v[0]}:{v[1]}"
            x = re.match(r"^any-player-(rampage|ultra-kill|quadra-kill|penta-kill)$", suf2)
            if x:
                mk = need("multi"); k = {"rampage": "rampage", "ultra-kill": "ultra", "quadra-kill": "quadra", "penta-kill": "penta"}[x.group(1)]
                if k not in mk: return None, f"нет данных: {k}"
                return (yes if mk[k] > 0 else no), f"{k}: {mk[k]}"
            if suf2 == "ends-in-daytime":
                d = need("dur"); day = int(d // 300) % 2 == 0
                return (yes if day else no), f"длительность {int(d // 60)}:{int(d % 60):02d} -> {'день' if day else 'ночь'}"
        except LookupError as e:
            return None, str(e)
        return None, "тип рынка не разобран"
    # --- по серии: нужна полная серия (у фида бывают не все карты)
    bo = g.get("bo")
    if bo:
        need_w = bo // 2 + 1
        if max(w1, w2) < need_w and len(maps) < bo:
            return None, f"у фида серия неполная: {len(maps)} карт, счёт {w1}:{w2} (BO{bo})"
    if not suf:
        if w1 == w2: return None, f"счёт серии {w1}:{w2}"
        return team_out(1 if w1 > w2 else 2), f"серия {w1}:{w2}"
    x = re.match(r"^total-games-(\d+pt5)$", suf)
    if x: return es_pick_word(outs, "over" if w1 + w2 > float(x.group(1).replace("pt", ".")) else "under"), f"карт {w1 + w2}"
    if re.match(r"^(map|game)-handicap", suf):
        s, line = es_fav_side(q, suf, a, b)
        if not s or line is None: return None, "фора: не сопоставил команду"
        sc = (w1, w2); cov = (sc[s - 1] - sc[2 - s]) > line
        return team_out(s if cov else 3 - s), f"серия {w1}:{w2}"
    x = re.match(r"^(kill|tower|drake)-handicap-(home|away)-(\d+pt5)$", suf)
    if x:
        k = {"kill": "kills", "tower": "tower", "drake": "dragon"}[x.group(1)]
        if any(mp.get(k) is None for mp in maps): return None, f"нет данных: {k}"
        t = (sum(mp[k][0] for mp in maps), sum(mp[k][1] for mp in maps))
        fav, line = es_handicap_parse(q); s = es_which_side(fav or "", a, b)
        if not s: return None, "фора: не сопоставил команду"
        cov = (t[s - 1] - t[2 - s]) > line
        return team_out(s if cov else 3 - s), f"{k} за серию {t[0]}:{t[1]}"
    x = re.match(r"^most-(kill|drake|inhibitor|nashor|tower)$", suf)
    if x:
        k = {"kill": "kills", "drake": "dragon", "inhibitor": "inhib", "nashor": "baron", "tower": "tower"}[x.group(1)]
        if any(mp.get(k) is None for mp in maps): return None, f"нет данных: {k}"
        t = (sum(mp[k][0] for mp in maps), sum(mp[k][1] for mp in maps))
        if t[0] == t[1]: return (es_pick_word(outs, "draw") or es_pick_word(outs, "tie")), f"{k} поровну {t[0]}:{t[1]}"
        return team_out(1 if t[0] > t[1] else 2), f"{k} за серию {t[0]}:{t[1]}"
    x = re.match(r"^(.+)-to-win-(\d)-games?$", suf)
    if x:
        qq = re.match(r"^(.+?) to win (\d) games?", q, re.I)
        s = es_which_side(qq.group(1) if qq else x.group(1).replace("-", " "), a, b)
        if not s: return None, "N карт: не сопоставил команду"
        won = (w1, w2)[s - 1]; return (yes if won == int(x.group(2)) else no), f"серия {w1}:{w2}"
    x = re.match(r"^(.+?) to win 2-0\??$", q, re.I)
    if x:
        s = es_which_side(x.group(1), a, b)
        if not s: return None, "2-0: не сопоставил команду"
        ok = (w1, w2)[s - 1] == 2 and (w1, w2)[2 - s] == 0
        return (yes if ok else no), f"серия {w1}:{w2}"
    return None, "тип рынка не разобран"


# ----------------------------------------------------------------------------- выборка

ES_BO3_DISC = {"cs2": 1, "val": 2, "lol": 3, "dota2": 4}


def es_bo3_day(disc, date):
    """завершённые матчи дисциплины bo3.gg за окно дат (кэш 4 мин — свежие матчи появляются быстро)"""
    d = datetime.fromisoformat(date)
    d0, d1 = (d - timedelta(days=2)).strftime("%Y-%m-%d"), (d + timedelta(days=3)).strftime("%Y-%m-%d")
    def load():
        rows, off = [], 0
        while off < 3000:
            j = es_bo3_get("/matches", {"scope": "widget-matches", "page[offset]": off, "page[limit]": 100, "sort": "start_date",
                                        "filter[matches.discipline_id][eq]": disc, "filter[matches.status][in]": "finished",
                                        "filter[matches.start_date][gt]": f"{d0} 00:00", "filter[matches.start_date][lt]": f"{d1} 00:00",
                                        "with": "teams,games"})
            if j is None: return None
            part = j.get("results") or []
            rows += part
            if len(part) < 100: break
            off += 100
        return rows
    return mcache(f"bo3|{disc}|{d0}|{d1}", 240, load)


ES_BO3_H = {"accept": "application/json, text/plain, */*", "origin": "https://bo3.gg", "referer": "https://bo3.gg/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}
_bo3_last = [0.0]
def es_bo3_get(path, params=None):
    time.sleep(max(0.0, 1.0 - (time.time() - _bo3_last[0]))); _bo3_last[0] = time.time()
    try:
        r = S.get(ES.BO3_HOST + path, params=params, headers=ES_BO3_H, timeout=30)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        log.debug("bo3 %s: %s", path, e); return None


def es_bo3_series(game, date, a, b):
    rows = es_bo3_day(ES_BO3_DISC[game], date)
    if rows is None: return None, "bo3.gg не ответил"
    best = None
    for m in rows:
        x, y = (m.get("team1") or {}).get("name", ""), (m.get("team2") or {}).get("name", "")
        sc, sw = es_orient(a, b, x, y)
        if sc >= 0.9 and min(max(es_tscore(a, x), es_tscore(a, y)), max(es_tscore(b, x), es_tscore(b, y))) > 0:
            d0 = datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp() + 43200
            t = datetime.fromisoformat(m["start_date"].replace("Z", "+00:00")).timestamp()
            k = (-sc, abs(t - d0))
            if not best or k < best[0]: best = (k, m, sw)
    if not best: return None, "матч не найден"
    m, sw = best[1], best[2]
    t1id = m.get("team1_id")
    s1, s2 = m.get("team1_score"), m.get("team2_score")
    if s1 is None or s2 is None: return None, "нет счёта серии"
    n = s1 + s2
    games = {g.get("number"): g for g in (m.get("games") or []) if g.get("number")}
    if any(not (games.get(k) or {}).get("winner_clan_score") for k in range(1, s1 + s2 + 1)) and not m.get("maps_score"):
        md = mcache(f"bo3m|{m.get('slug')}", 600, lambda: es_bo3_get(f"/matches/{m.get('slug')}", {"scope": "show-match", "with": "games,teams"})) or {}
        m = dict(m, maps_score=md.get("maps_score"))
        for g in md.get("games") or []:
            if g.get("number") and g.get("winner_clan_score") is not None: games[g["number"]] = g
    ms = m.get("maps_score") or []
    ms_ok = len(ms) >= n and sum(1 for v in ms[:n] if v) == s1          # maps_score надёжен, только если сходится со счётом
    Sr = dict(src="bo3.gg", t1=a, t2=b, maps=[], score=(s1, s2), note="")
    for k in range(1, n + 1):
        mp = dict(n=k, win=None, rounds=None)
        g = games.get(k)
        wc = (g or {}).get("winner_team_clan") or {}
        wt = wc.get("team_id") or (wc.get("team") or {}).get("id")
        if g and g.get("winner_clan_score") is not None and wt is not None:
            w1 = wt == t1id
            mp["win"] = 1 if w1 else 2
            ws, ls = g["winner_clan_score"], g["loser_clan_score"]
            mp["rounds"] = (ws, ls) if w1 else (ls, ws)
        elif ms_ok:
            mp["win"] = 1 if ms[k - 1] else 2
        Sr["maps"].append(mp)
    if sw:
        es_side_swap(Sr); Sr["t1"], Sr["t2"] = a, b; Sr["score"] = (s2, s1)
    return Sr, ""


ES_LP_FIELDS = ("Team1,Team2,WinTeam,Team1Kills,Team2Kills,Team1Barons,Team2Barons,Team1Dragons,Team2Dragons,"
                "Team1Inhibitors,Team2Inhibitors,Team1Towers,Team2Towers,Gamelength_Number,DateTime_UTC,N_GameInMatch,MatchId,Tournament")
_lp_state = {"logged": False, "last": 0.0}


def es_lp_login():
    """вход бот-паролем Leaguepedia: без него cargo-запросы упираются в лимит"""
    if _lp_state["logged"] or not ES.LP_USER: return
    _lp_state["logged"] = True
    try:
        h = {"User-Agent": "propwatch/1.0 (personal research)"}
        t = S.get(ES.LP_HOST, params={"action": "query", "meta": "tokens", "type": "login", "format": "json"}, headers=h, timeout=30
                  ).json()["query"]["tokens"]["logintoken"]
        r = S.post(ES.LP_HOST, data={"action": "login", "lgname": ES.LP_USER, "lgpassword": ES.LP_PASS, "lgtoken": t, "format": "json"},
                   headers=h, timeout=30).json()
        log.info("Leaguepedia вход: %s", r.get("login", {}).get("result"))
    except Exception as e:
        log.warning("Leaguepedia вход не удался: %s", e)


def es_lp_day(date):
    """игры LoL за окно дат (кэш 10 мин: вики заполняют вручную, данные появляются с задержкой)"""
    d = datetime.fromisoformat(date)
    d0, d1 = (d - timedelta(days=1)).strftime("%Y-%m-%d"), (d + timedelta(days=2)).strftime("%Y-%m-%d")
    def load():
        es_lp_login()
        time.sleep(max(0.0, 2.0 - (time.time() - _lp_state["last"]))); _lp_state["last"] = time.time()
        try:
            j = S.get(ES.LP_HOST, params={"action": "cargoquery", "format": "json", "tables": "ScoreboardGames", "fields": ES_LP_FIELDS,
                                          "where": f"DateTime_UTC >= '{d0} 00:00:00' AND DateTime_UTC < '{d1} 00:00:00'",
                                          "order_by": "DateTime_UTC", "limit": 500},
                      headers={"User-Agent": "propwatch/1.0 (personal research)"}, timeout=40).json()
        except Exception as e:
            log.debug("Leaguepedia: %s", e); return None
        if "cargoquery" not in j:
            log.warning("Leaguepedia: %s", str(j.get("error"))[:120]); return None
        return [x["title"] for x in j["cargoquery"]]
    return mcache(f"lp|{d0}", 600, load)


def es_lp_series(date, a, b):
    rows = es_lp_day(date)
    if rows is None: return None, "Leaguepedia не ответил"
    ts = lambda r: datetime.fromisoformat((r.get("DateTime UTC") or r.get("DateTime_UTC") or "2000-01-01 00:00:00").replace(" ", "T")
                                          ).replace(tzinfo=timezone.utc).timestamp()
    g = es_pick_series(rows, a, b, date, lambda r: r.get("MatchId") or (r["Team1"], r["Team2"]), lambda r: (r.get("Team1", ""), r.get("Team2", "")), ts)
    if not g: return None, "матч не найден"
    g = sorted(g, key=lambda z: (int(z[2].get("N GameInMatch") or z[2].get("N_GameInMatch") or 0) or ts(z[2])))
    iv = lambda v: int(float(v)) if v not in (None, "") else None
    Sr = dict(src="Leaguepedia", t1=a, t2=b, maps=[], note="")
    for n, (sc, sw, r) in enumerate(g, 1):
        pair = lambda f: (iv(r.get(f"Team1{f}")), iv(r.get(f"Team2{f}")))
        mp = dict(n=n, kills=pair("Kills"), baron=pair("Barons"), dragon=pair("Dragons"), inhib=pair("Inhibitors"), tower=pair("Towers"),
                  dur=(float(r["Gamelength Number"]) * 60 if r.get("Gamelength Number") else None))
        for k in ("kills", "baron", "dragon", "inhib", "tower"):
            if None in mp[k]: mp[k] = None
        w = r.get("WinTeam") or ""
        mp["win"] = 1 if w and es_tscore(w, r["Team1"]) >= es_tscore(w, r["Team2"]) else (2 if w else None)
        if sw:
            for k in ("kills", "baron", "dragon", "inhib", "tower"):
                if mp[k] is not None: mp[k] = mp[k][::-1]
            mp["win"] = {1: 2, 2: 1}.get(mp["win"])
        Sr["maps"].append(mp)
    return Sr, ""


# --- исход рынка по данным фида
def ou(outcomes, over): return pick(outcomes, "over" if over else "under")
def pick(outcomes, word):
    for o in outcomes:
        if nrm(o).startswith(word): return o
    return word.capitalize()
def ptv(x): return float(x.replace("pt", "."))


def evaluate(slug, outcomes, D):
    m = EV_RX.match(slug)
    if not m: return None, "slug не матч"
    lg, ha, aa, date, suf = m.groups()
    yes, no = (outcomes + ["", ""])[:2]
    x = XI_RX.match(suf)
    if x:
        p, why = player_match(x.group(2), [q for q in D["players"] if q["side"] == x.group(1)[0]])
        if p and p["starter"] is None: return None, "нет стартовых составов"
        return ((yes if p["starter"] else no), f"{p['name']} в старте: {'да' if p['starter'] else 'нет'}") if p else (None, why)
    pm = PROP_RX.match(suf)
    if pm:
        stat, name, _, th = pm.groups(); th = int(th) if th else 1
        if not D["players"]: return None, "нет статистики игроков"
        if D.get("stale"): return None, "статистика не досчитана"
        p, why = player_match(name, D["players"])
        if not p: return None, why
        v = {"goals": p["goals"], "ags": p["goals"], "assists": p["assists"], "goals-plus-assists": p["goals"] + p["assists"],
             "shots": p["shots"], "shots-on-target": p["sot"], "saves": p["saves"]}[stat]
        return (yes if v >= th else no), f"{p['name']} = {v}"
    if suf.startswith("corners"):
        if D.get("stale"): return None, "статистика не досчитана"
        c = D.get("corners")
        x = re.match(r"^corners-(first|second)-half-total-(\d+pt5)$", suf)
        if x:
            c1 = D.get("corners_1h")
            if not c1 or not c or None in tuple(c1) + tuple(c): return None, "нет угловых по таймам"
            v = sum(c1) if x.group(1) == "first" else sum(c) - sum(c1)
            return ou(outcomes, v > ptv(x.group(2))), f"угловых за тайм {v}"
        if not c or None in c: return None, "нет угловых"
        x = re.match(r"^corners-total-(\d+pt5)$", suf)
        if x: return ou(outcomes, sum(c) > ptv(x.group(1))), f"угловые {c[0]}–{c[1]}"
        x = re.match(r"^corners-team-(home|away)-(\d+pt5)$", suf)
        if x:
            v = c[0] if x.group(1) == "home" else c[1]; return ou(outcomes, v > ptv(x.group(2))), f"угловые {c[0]}–{c[1]}"
        if suf == "corners-odd-even": return pick(outcomes, "odd" if sum(c) % 2 else "even"), f"угловые {c[0]}–{c[1]}"
        return None, "тип угловых не разобран"
    ft = D.get("ft") or (None, None); ht = D.get("ht") or (None, None)
    if None in ft: return None, "нет счёта"
    sh = (ft[0] - ht[0], ft[1] - ht[1]) if None not in ht else (None, None)
    per = {"": ft, "first-half-": ht, "second-half-": sh}
    sc = f"счёт {ft[0]}:{ft[1]}" + (f" (1-й тайм {ht[0]}:{ht[1]})" if None not in ht else "")
    if suf == "draw": return (yes if ft[0] == ft[1] else no), sc
    if suf == ha: return (yes if ft[0] > ft[1] else no), sc
    if suf == aa: return (yes if ft[1] > ft[0] else no), sc
    x = re.match(r"^btts(?:-(first|second)-half)?$", suf)
    if x:
        s = per[(x.group(1) + "-half-") if x.group(1) else ""]
        return ((yes if s[0] > 0 and s[1] > 0 else no), sc) if None not in s else (None, "нет счёта по таймам")
    x = re.match(r"^(first-half-|second-half-)?total-(\d+pt5)$", suf)
    if x:
        s = per[x.group(1) or ""]
        return (ou(outcomes, sum(s) > ptv(x.group(2))), sc) if None not in s else (None, "нет счёта по таймам")
    x = re.match(r"^(first-half-|second-half-)?team-total-(home|away)-(\d+pt5)$", suf)
    if x:
        s = per[x.group(1) or ""]
        if None in s: return None, "нет счёта по таймам"
        return ou(outcomes, (s[0] if x.group(2) == "home" else s[1]) > ptv(x.group(3))), sc
    x = re.match(r"^spread-(home|away)-(\d+pt5)$", suf)
    if x:
        line = ptv(x.group(2)); margin = (ft[0] - ft[1]) if x.group(1) == "home" else (ft[1] - ft[0])
        sd = 1 if (margin > line) == (x.group(1) == "home") else 2
        o = pick_rel(outcomes, *side_names(D, sd))
        return (o, sc) if o else (None, "фора: не сопоставил команду")
    return None, "тип рынка не разобран"


def teams_of(title, slug):
    x = re.split(r"\s+(?:vs\.?|v\.?|@)\s+", title or "", maxsplit=1)
    if len(x) == 2: return x[0].strip(), re.sub(r"\s+[-:–].*$", "", x[1]).strip()
    m = EV_RX.match(slug); return (m.group(2), m.group(3)) if m else ("", "")


# ----------------------------------------------------------------------------- теннис (из tennischeck, живой режим)
# M = {src, p1, p2, sets: [[g1, g2, kind]], status: fin|ret|wo|canc|live|sched|none, t, tour, dbl, men}
# kind: '' | 'tb' (7-6) | 'stb' (супер-тай-брейк вместо сета: хранится как 1-0 — это 1 гейм)
class TN:
    FS_FEED = _env("FS_FEED_HOST", "https://global.flashscore.ninja/2/x/feed")
    FS_SIGN = _env("FS_SIGN", "SW9D1eZo")
    USE_TE = _b("USE_TE", True)
    USE_FS = _b("USE_FS", True)
    USE_ESPN = _b("USE_ESPN_TENNIS", True)


TN_SL_RX = re.compile(r"^(itf|atp|wta)-(.+?)-(20\d\d-\d\d-\d\d)(?:-(.+))?$")
TN_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
         "Accept-Language": "en-US,en;q=0.9"}
TN_FS_H = {**TN_UA, "x-fsign": TN.FS_SIGN, "Referer": "https://www.flashscore.com/", "Origin": "https://www.flashscore.com"}
try:
    from zoneinfo import ZoneInfo
    TE_TZ = ZoneInfo("Europe/Prague")
except Exception:
    TE_TZ = timezone(timedelta(hours=1))


def is_tn(slug): return bool(TN_SL_RX.match(slug or ""))


def tn_get(url, params=None, headers=None, as_json=True):
    for i in range(2):
        try:
            r = S.get(url, params=params, headers=headers or TN_UA, timeout=20)
            if r.status_code >= 400: return None
            return r.json() if as_json else r.text
        except Exception as e:
            if i == 1: log.debug("теннис GET %s: %s", url[:80], e)
            time.sleep(1)
    return None


TN_PART = {"de", "da", "di", "del", "della", "la", "le", "las", "los", "van", "von", "der", "den", "dos", "du", "el", "al", "y", "e"}


def tn_norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9/&]+", " ", s).strip()


def name_parts(s, feed=True):
    s = re.sub(r"\(\s*\d+\s*\)|\b(WC|Q|LL|PR|SE|ALT)\b(?!\.)", " ", s or "")
    full, ini = set(), set()
    for raw in re.split(r"[\s/&]+", s):
        dot = raw.endswith(".")
        for t in tn_norm(raw).split():
            if t in TN_PART or t.isdigit() or not t: continue
            if feed and (dot or len(t) == 1): ini.add(t)
            else: full.add(t)
    return full or ini, (ini if full else set())


def pscore(g, f):
    if not g or not f: return 0.0
    gt, _ = name_parts(g, feed=False); ft, fi = name_parts(f)
    m = 0; used = set()
    for x in ft:
        for y in gt:
            if y in used: continue
            if x == y or (min(len(x), len(y)) >= 4 and (x.startswith(y) or y.startswith(x))):
                m += 1; used.add(y); break
    if not m: return 0.0
    sc = m / max(1, min(len(ft), len(gt)))
    rest = gt - used
    if fi and rest:
        if any(any(t.startswith(i) for i in fi) for t in rest):
            if len(used) < len(gt): sc = max(sc, min(1.0, (m + 1) / max(1, min(len(ft) + 1, len(gt)))))
        else: sc *= 0.55
    return min(1.0, sc)


def tn_orient(a, b, x, y):
    st, sw = min(pscore(a, x), pscore(b, y)), min(pscore(a, y), pscore(b, x))
    return (sw, True) if sw > st else (st, False)


def players_of(*texts):
    for t in texts:
        if not t: continue
        parts = [p.strip() for p in re.split(r":\s+", t)]
        for i in range(len(parts) - 1, -1, -1):
            p = re.sub(r"\s*\([+-]?[\d.]+\)", "", parts[i])
            x = re.split(r"\s+vs\.?\s+", p, maxsplit=1, flags=re.I)
            if len(x) == 2 and x[0] and x[1]:
                return x[0].strip(), re.split(r"\s+-\s+", x[1])[0].strip()
    return "", ""


def tn_mtype(slug):
    m = TN_SL_RX.match(slug); suf = (m.group(4) or "") if m else ""
    if not suf: return "победа в матче"
    if suf.startswith("first-set-winner"): return "first-set-winner"
    mm = re.match(r"set-(\d)-winner", suf)
    if mm: return f"set-{mm.group(1)}-winner"
    return re.sub(r"-(home|away)-\d+pt\d+$|-\d+pt\d+$", "", suf)


def tn_set_k(t):
    if t in ("first-set-total", "first-set-winner"): return 1
    m = re.fullmatch(r"set-(\d)-(total|winner)", t)
    return int(m.group(1)) if m else None


def line_of(slug, q):
    m = re.search(r"-(\d+)pt(\d+)$", slug)
    if m: return float(f"{m.group(1)}.{m.group(2)}")
    m = re.search(r"O/U\s*([\d.]+)", q or "")
    return float(m.group(1)) if m else None


def set_done(g1, g2, kind):
    if kind in ("tb", "stb"): return True
    hi, lo = max(g1, g2), min(g1, g2)
    return (hi == 6 and hi - lo >= 2) or (hi == 7 and lo in (5, 6))


def complete(M):
    w = [0, 0]; n = len(M["sets"])
    for i, (g1, g2, k) in enumerate(M["sets"]):
        if not set_done(g1, g2, k): return False
        w[0 if g1 > g2 else 1] += 1
        if i == n - 1: return max(w) in (2, 3) and min(w) < max(w)
        if max(w) == 3: return False
    return False


def fix_stb(sets):
    out = []
    for g1, g2, k in sets:
        if max(g1, g2) >= 8 and abs(g1 - g2) >= 2: out.append([1 if g1 > g2 else 0, 1 if g2 > g1 else 0, "stb"])
        else: out.append([g1, g2, k])
    return out


def tn_swap(M):
    M = dict(M); M["p1"], M["p2"] = M["p2"], M["p1"]
    M["sets"] = [[g2, g1, k] for g1, g2, k in M["sets"]]
    return M


def pick_match(rows, a, b, t0):
    best = None
    want_dbl = "/" in a or "/" in b
    for M in rows:
        if M.get("dbl") is not None and bool(M["dbl"]) != want_dbl: continue
        sc, sw = tn_orient(a, b, M["p1"], M["p2"])
        if sc < 0.6: continue
        dt = abs((M.get("t") or t0) - t0) / 3600
        key = (-round(sc, 1), dt)
        if best is None or key < best[0]: best = (key, tn_swap(M) if sw else M)
    return best[1] if best else None


# --- tennisexplorer: дневная страница результатов (явного статуса нет: недоигранный = «идёт», снятие так не ловим)
def _cell_int(s):
    s = re.sub(r"<sup>.*?</sup>", "", s or "").replace("&nbsp;", "").strip()
    return int(s) if s.isdigit() else None


def te_parse(html_, d):
    rows, tour, href, pend = [], "", "", None
    for attrs, body in re.findall(r"<tr([^>]*)>(.*?)</tr>", html_, re.S):
        if 'class="head flags"' in attrs:
            mm = re.search(r'<a href="([^"]+)">(.*?)</a>', body, re.S)
            if mm: href, tour = mm.group(1), re.sub(r"<[^>]+>|&nbsp;", "", mm.group(2)).strip()
            continue
        mid = re.search(r'id="(r\d+)(b?)"', attrs)
        if not mid: continue
        nm = re.search(r'<td class="t-name">(.*?)</td>', body, re.S)
        name = re.sub(r"<[^>]+>", "", nm.group(1)).replace("&nbsp;", " ").strip() if nm else ""
        sc = re.findall(r'<td class="score">(.*?)</td>', body, re.S)[:5]
        g = [_cell_int(x) for x in sc]; tb = ["<sup>" in x for x in sc]
        if not mid.group(2):
            tm = re.search(r'<td class="first time"[^>]*>(\d\d:\d\d)</td>', body)
            pend = dict(id=mid.group(1), p1=name, g1=g, tb1=tb, tm=tm.group(1) if tm else None)
        elif pend and pend["id"] == mid.group(1):
            sets = []
            for i in range(5):
                x, y = pend["g1"][i] if i < len(pend["g1"]) else None, g[i] if i < len(g) else None
                if x is None or y is None: break
                sets.append([x, y, "tb" if (pend["tb1"][i] if i < len(pend["tb1"]) else False) or (tb[i] if i < len(tb) else False) else ""])
            t = datetime.fromisoformat(f"{d}T{pend['tm']}").replace(tzinfo=TE_TZ).timestamp() if pend["tm"] else None
            M = dict(src="TE", p1=pend["p1"], p2=name, sets=fix_stb(sets), t=t, tour=tour, dbl="double" in href, men="atp-men" in href)
            M["status"] = "none" if not sets else ("fin" if complete(M) else "live")
            rows.append(M); pend = None
    return rows


def te_day(d):
    if d > datetime.now(timezone.utc).date().isoformat(): return []
    old = d < (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
    y, m, dd = d.split("-")
    def load():
        h = tn_get("https://www.tennisexplorer.com/results/", {"type": "all", "year": y, "month": m, "day": dd}, as_json=False)
        return te_parse(h, d) if h else None
    return mcache(f"te|{d}", 3600 if old else 60, load) or []


# --- ESPN (ATP/WTA тур)
ES_ST = {"STATUS_FINAL": "fin", "STATUS_RETIRED": "ret", "STATUS_WALKOVER": "wo", "STATUS_CANCELED": "canc",
         "STATUS_SCHEDULED": "sched", "STATUS_IN_PROGRESS": "live", "STATUS_POSTPONED": "canc", "STATUS_DEFAULTED": "ret"}


def tn_es_name(cp):
    a = cp.get("athlete") or {}
    if a.get("displayName"): return a["displayName"]
    ros = cp.get("roster") or {}
    ath = ros.get("athletes") or []
    if ath: return " / ".join(x.get("displayName") or x.get("shortName") or "" for x in ath)
    return ros.get("displayName") or ros.get("shortDisplayName") or (cp.get("team") or {}).get("displayName") or ""


def tn_espn_day(tour, d):
    def load():
        j = tn_get(f"https://site.api.espn.com/apis/site/v2/sports/tennis/{tour}/scoreboard", {"dates": d.replace("-", "")})
        if j is None: return None
        rows = []
        for ev in j.get("events") or []:
            for gr in ev.get("groupings") or []:
                slug = (gr.get("grouping") or {}).get("slug", "")
                for cmp in gr.get("competitions") or []:
                    cps = sorted(cmp.get("competitors") or [], key=lambda x: x.get("order", 0))
                    if len(cps) != 2: continue
                    sets = []
                    for s1, s2 in zip(cps[0].get("linescores") or [], cps[1].get("linescores") or []):
                        x, y = s1.get("value"), s2.get("value")
                        if x is None or y is None: break
                        x, y = int(x), int(y)
                        if "tiebreak" in s1 or "tiebreak" in s2:
                            kind = "stb" if max(x, y) <= 1 else "tb"
                            if kind == "stb" and x == y:
                                x, y = (1, 0) if (s1.get("tiebreak") or 0) > (s2.get("tiebreak") or 0) else (0, 1)
                        else: kind = ""
                        sets.append([x, y, kind])
                    t = datetime.fromisoformat(cmp["date"].replace("Z", "+00:00")).timestamp() if cmp.get("date") else None
                    st = ES_ST.get(((cmp.get("status") or {}).get("type") or {}).get("name", ""), "none")
                    M = dict(src="ESPN", p1=tn_es_name(cps[0]), p2=tn_es_name(cps[1]), sets=fix_stb(sets), t=t, tour=ev.get("name", ""),
                             dbl="doubles" in slug, men="mens" in slug and "womens" not in slug, status=st)
                    if st == "fin" and not complete(M): M["status"] = "ret"      # «финал» с неполным счётом = снятие
                    rows.append(M)
        return rows
    return mcache(f"tnes|{tour}|{d}", 60, load) or []


# --- Flashscore: дневная лента (все уровни, явный статус: 3 доигран, 8 снятие, 9 отказ, 5 отмена)
FS_AC = {"3": "fin", "8": "ret", "9": "wo", "5": "canc", "10": "ret", "11": "ret"}
FS_SETS = [("BA", "BB", "DA", "DB"), ("BC", "BD", "DC", "DD"), ("BE", "BF", "DE", "DF"), ("BG", "BH", "DG", "DH"), ("BI", "BJ", "DI", "DJ")]


def fs_parse(text):
    rows, head = [], {}
    for rec in (text or "").split("~"):
        d = {}
        for f in rec.split("¬"):
            if "÷" in f:
                k, v = f.split("÷", 1); d[k] = v
        if "ZA" in d: head = d; continue
        if "AA" not in d or "AE" not in d: continue
        za = head.get("ZA", "")
        if "SINGLES" not in za.upper() and "DOUBLES" not in za.upper(): continue
        sets = []
        for a, b, ta, tb in FS_SETS:
            if a not in d or b not in d: break
            try: x, y = int(d[a]), int(d[b])
            except ValueError: break
            sets.append([x, y, "tb" if (ta in d or tb in d) else ""])
        ab, ac = d.get("AB"), d.get("AC")
        st = (FS_AC.get(ac) if ab == "3" else ("live" if ab == "2" else "sched")) or "none"
        M = dict(src="FS", p1=d["AE"], p2=d["AF"], sets=fix_stb(sets), t=float(d["AD"]) if d.get("AD", "").isdigit() else None,
                 tour=za, dbl="DOUBLES" in za.upper(), men=bool(re.match(r"(ATP|CHALLENGER MEN|ITF MEN)", za.upper())), status=st)
        if st == "fin" and not complete(M): M["status"] = "ret"
        rows.append(M)
    return rows


def fs_day(d):
    off = (datetime.fromisoformat(d).date() - datetime.now(timezone.utc).date()).days
    if off < -7 or off > 1: return []
    return mcache(f"fs|{off}", 20, lambda: (lambda t: fs_parse(t) if t else None)(
        tn_get(f"{TN.FS_FEED}/f_2_{off}_3_en_1", headers=TN_FS_H, as_json=False))) or []


def around(d, k=1):
    x = datetime.fromisoformat(d)
    return [(x + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(-k, k + 1)]


def tn_feeds(tour, d, a, b, t0):
    out = []
    if TN.USE_FS:
        M = pick_match([x for dd in around(d) for x in fs_day(dd)], a, b, t0)
        out.append(("FS", M, None if M else "Flashscore: матч не найден"))
    if TN.USE_TE:
        M = pick_match([x for dd in around(d) for x in te_day(dd)], a, b, t0)
        if M and M["src"] == "TE" and M["sets"]: M = dict(M, status="fin" if complete(M) else "live")
        out.append(("TE", M, None if M else "tennisexplorer: матч не найден"))
    if TN.USE_ESPN and tour in ("atp", "wta"):
        M = pick_match([x for tt in ("atp", "wta") for dd in around(d) for x in tn_espn_day(tt, dd)], a, b, t0)
        out.append(("ESPN", M, None if M else "ESPN: матч не найден"))
    return out


def tn_pick_out(outs, name):
    if not outs or not name: return None
    best = max(outs, key=lambda o: pscore(o, name))
    return best if pscore(best, name) > 0 else None


def tn_side_of(name, M):
    s1, s2 = pscore(name, M["p1"]), pscore(name, M["p2"])
    return None if s1 == s2 == 0 else (1 if s1 >= s2 else 2)


def tn_evaluate(slug, outs, q, M):
    """-> (исход | '50-50' | None, пояснение, надёжно: явный статус матча или доигранный сет при начатом следующем)"""
    t = tn_mtype(slug); st = M["status"]; sets = M["sets"]; k = tn_set_k(t)
    over = next((o for o in outs if tn_norm(o).startswith("over")), None)
    under = next((o for o in outs if tn_norm(o).startswith("under")), None)
    yes = next((o for o in outs if tn_norm(o) == "yes"), None); no = next((o for o in outs if tn_norm(o) == "no"), None)
    scs = " ".join(f"{x}-{y}" + ("(tb)" if kk == "tb" else "(stb)" if kk == "stb" else "") for x, y, kk in sets) or "—"
    info = f"{M['src']}: {M['p1']} — {M['p2']} {scs} [{st}]"
    explicit = M["src"] in ("FS", "ESPN")
    if st == "live":
        if k and len(sets) > k and set_done(*sets[k - 1]): explicit, st = True, "set_done"    # сет k доигран, идёт следующий
        else: return None, info + " — матч идёт", False
    if st in ("sched", "none"): return None, info + " — нет счёта", False
    if st in ("wo", "canc"):
        if t == "completed-match" and no: return no, info + " — не сыгран", explicit
        return "50-50", info + " — отказ/отмена", explicit
    if t == "completed-match":
        if not (yes and no): return None, info + " — исходы не Yes/No", False
        if st == "set_done": return None, info + " — матч идёт", False
        return (yes if st == "fin" else no), info, explicit
    if st == "ret" and t in ("победа в матче", "match-total", "set-totals", "set-handicap", "game-handicap"):
        return ("50-50" if t != "победа в матче" else None), info + " — снятие", explicit
    line = line_of(slug, q)
    if k:
        if k > len(sets): return ("50-50" if st == "ret" else None), info + f" — сет {k} не сыгран", explicit
        x, y, kind = sets[k - 1]
        if not set_done(x, y, kind): return ("50-50" if st == "ret" else None), info + f" — сет {k} не доигран", explicit
        if t.endswith("winner"):
            w = M["p1"] if x > y else M["p2"]
            return tn_pick_out(outs, w), info + f" -> сет {k}: {w}", explicit
        if not (over and under) or line is None: return None, info + " — нет Over/Under", False
        v = x + y
        return (over if v > line else under), info + f" -> {v} vs {line}", explicit
    if st == "set_done": return None, info + " — матч идёт", False
    w1, w2 = sum(1 for x, y, _ in sets if x > y), sum(1 for x, y, _ in sets if y > x)
    if t in ("match-total", "set-totals"):
        if not (over and under) or line is None: return None, info + " — нет Over/Under", False
        v = sum(x + y for x, y, _ in sets) if t == "match-total" else len(sets)
        return (over if v > line else under), info + f" -> {v} vs {line}", explicit
    if t == "победа в матче":
        w = M["p1"] if w1 > w2 else M["p2"]
        return tn_pick_out(outs, w), info + f" -> {w}", explicit
    if t in ("set-handicap", "game-handicap"):
        rest = q.split(":", 1)[1] if ":" in q else q
        mm = re.match(r"\s*(.+?)\s*\(-([\d.]+)\)", rest)
        if not mm: return None, info + " — фора не разобрана", False
        fav, ln = mm.group(1), float(mm.group(2))
        fsd = tn_side_of(fav, M)
        if not fsd: return None, info + " — не понял, чья фора", False
        if t == "set-handicap": diff = (w1 - w2) if fsd == 1 else (w2 - w1)
        else:
            g1 = sum(x for x, _, _ in sets); g2 = sum(y for _, y, _ in sets); diff = (g1 - g2) if fsd == 1 else (g2 - g1)
        favn = M["p1"] if fsd == 1 else M["p2"]
        fo = tn_pick_out(outs, fav) or tn_pick_out(outs, favn)
        do = next((o for o in outs if o != fo), None)
        if not fo or not do: return None, info + " — исходы форы не разобраны", False
        return (fo if diff > ln else do), info + f" -> фора {favn} -{ln}: {diff:+d}", explicit
    return None, info + f" — тип рынка не разобран ({t})", False


# ----------------------------------------------------------------------------- белые списки (по бэктестам — без ложных)
def _rules(k, d):
    out = {}
    for x in _env(k, d).split(","):
        if ":" in x:
            a, b = x.split(":", 1); out[a.strip()] = b.strip()
    return out


# футбол: тип -> any (хватит одного фида против, второй не за предложение) | both (оба фида против) ; нет в списке = не торгуем
FOOT_RULES = _rules("FOOT_RULES", "win:any,draw:any,corners:any,total:any,half_total:any,btts:any,spread:any,prop_saves:any,"
                                  "prop_goals-plus-assists:any,prop_assists:any,prop_ags:any,prop_shots-on-target:any,"
                                  "team_total:both,prop_goals:both")
# киберспорт: игра -> * (все разобранные типы) | список типов через |; ES_EXCLUDE — исключения
# MLS считает «вторые передачи» (secondary assists): ESPN и API-Football их не дают — передач у фида МЕНЬШЕ официальных.
# Поэтому по этим рынкам фиду верим только в сторону «да» (у фида уже хватает — официально тоже хватит), «нет» не торгуем
FOOT_YES_ONLY_RX = re.compile(_env("FOOT_YES_ONLY_RX", r"^mls-.*-(assists|goals-plus-assists)-"), re.I)
ES_RULES = _rules("ESPORTS_RULES", "dota2:*,lol:*,val:*,cs2:total-games|map-handicap|odd-even-total-rounds")
ES_EXCLUDE = _rules("ESPORTS_EXCLUDE", "lol:both-teams-slay-dragon")


def foot_type(slug):
    m = EV_RX.match(slug or "")
    if not m: return ""
    _, ha, aa, _, suf = m.groups()
    if suf in (ha, aa): return "win"
    if suf == "draw": return "draw"
    if re.match(r"^corners-(first|second)-half", suf): return "corners_half"
    if suf.startswith("corners") or suf.startswith("first-corner"): return "corners"
    if re.match(r"^(first-half-|second-half-)total-", suf): return "half_total"
    if re.match(r"^total-\d+pt5$", suf): return "total"
    if "team-total" in suf: return "team_total"
    if suf.startswith("btts"): return "btts"
    if suf.startswith("spread"): return "spread"
    if suf.startswith("starting-eleven"): return "xi"
    pm = PROP_RX.match(suf)
    if pm: return "prop_" + pm.group(1)
    return "other"


def es_mtype(slug):
    m = ES_SL_RX.match(slug); suf = (m.group(4) or "") if m else ""
    if not suf: return "победа в матче"
    if re.fullmatch(r"game\d", suf): return "победа в карте"
    s = re.sub(r"^game\d-", "", suf)
    s = re.sub(r"-(home|away)-\d+pt\d+$|-\d+pt\d+$", "", s)
    s = re.sub(r"round-handicap-.*", "round-handicap", s)
    if re.fullmatch(r"[a-z0-9]{1,8}", s): return "победа 2-0"
    return s


def es_allowed(slug):
    m = ES_SL_RX.match(slug); g = m.group(1); t = es_mtype(slug)
    rule = ES_RULES.get(g, "")
    if not rule: return False, t
    if t in set(ES_EXCLUDE.get(g, "").split("|")): return False, t
    return (rule == "*" or t in set(rule.split("|"))), t


# ----------------------------------------------------------------------------- БД торговли
db.executescript("""
create table if not exists fq(tx text, li int, slug text, ev text, title text, outcomes text, proposed text, exp int,
   next_t int, state text, espn text, af text, espn_why text, af_why text, msg_id int, primary key(tx, li));
create table if not exists trades(id integer primary key autoincrement, tx text, li int, t int, slug text, cat text, token text,
   usd_req real, cap real, status text, usdc real, shares real, paper int, resp text);
create table if not exists lorders(oid text primary key, tx text, li int, cid text, token text, other text, level real, shares real,
   matched real default 0, state text, placed_t int, exp_t int, paper int);
create table if not exists paper_cursor(cid text primary key, t int);
""")
for _sql in ("alter table fq add column tries int default 0", "alter table fq add column conf int default 0",
             "alter table fq add column cat text", "alter table fq add column cid text", "alter table fq add column tokens text",
             "alter table fq add column tick real", "alter table fq add column start text", "alter table fq add column question text",
             "alter table fq add column url text", "alter table fq add column val text", "alter table fq add column reason text",
             "alter table fq add column conf_val text", "alter table fq add column conf_t int", "alter table fq add column token text",
             "alter table fq add column spent real default 0", "alter table fq add column shares real default 0",
             "alter table fq add column first_try int", "alter table fq add column disputed int default 0",
             "alter table fq add column warned int default 0", "alter table fq add column result text",
             "alter table fq add column pnl real", "alter table fq add column note text",
             "alter table fq add column ladder int default 0", "alter table fq add column book text", "alter table props add column src text"):
    try: db.execute(_sql)
    except sqlite3.OperationalError: pass
db.commit()


# ----------------------------------------------------------------------------- CLOB (из dispute_ladder: FAK BUY с потолком)
def tick_floor(price: float, tick: float) -> float:
    return round(math.floor(price / tick + 1e-9) * tick, 6)


def get_book(token: str) -> dict:
    j = http_get(Cfg.CLOB + "/book", {"token_id": token}) or {}
    asks = sorted((float(a["price"]), float(a["size"])) for a in j.get("asks", []) if float(a.get("size") or 0) > 0)
    bids = sorted(((float(a["price"]), float(a["size"])) for a in j.get("bids", []) if float(a.get("size") or 0) > 0), reverse=True)
    return dict(ok=bool(j), asks=asks, bids=bids, tick=float(j.get("tick_size") or 0) or None,
                min_size=float(j.get("min_order_size") or Cfg.MIN_SHARES))


def book_text(bk: dict, cap: float) -> str:
    """«аск 0.40×20 0.43×100 | бид 0.30×50 | до потолка: 120 акц. на $49»"""
    a = " ".join(f"{p:g}×{s:.0f}" for p, s in bk["asks"][:3]) or "—"
    b = " ".join(f"{p:g}×{s:.0f}" for p, s in bk["bids"][:2]) or "—"
    sh = sum(s for p, s in bk["asks"] if p <= cap + 1e-12); usd = sum(p * s for p, s in bk["asks"] if p <= cap + 1e-12)
    return f"аск {a} | бид {b} | до потолка {cap:g}: {sh:.0f} акц. на ${usd:.0f}"


def tick_ok(level: float, tick: float) -> bool:
    return level >= tick - 1e-12 and abs(round(level / tick) * tick - level) < 1e-9 and level <= 1 - tick


class Clob:
    _MIN_TICK_RX = re.compile(r"minimum for the market is ([0-9.]+)")
    enabled = True

    def __init__(self):
        try:
            import py_clob_client_v2 as m
        except ImportError as e:
            raise SystemExit("нужен пакет py_clob_client_v2: %s" % e)
        self.m = m
        if not Cfg.PK:
            raise SystemExit("PK не задан, а PAPER=0")
        kw: Dict[str, Any] = dict(host=Cfg.CLOB, chain_id=Cfg.CHAIN_ID, key=Cfg.PK)
        if Cfg.FUNDER: kw["funder"] = Cfg.FUNDER
        if Cfg.SIGNATURE_TYPE >= 0: kw["signature_type"] = Cfg.SIGNATURE_TYPE
        kw["builder_config"] = m.BuilderConfig(builder_address=BUILDER_ADDRESS, builder_code=BUILDER_CODE)
        self.c = m.ClobClient(**kw)
        if Cfg.API_KEY and Cfg.API_SECRET and Cfg.API_PASSPHRASE:
            creds = m.ApiCreds(api_key=Cfg.API_KEY, api_secret=Cfg.API_SECRET, api_passphrase=Cfg.API_PASSPHRASE)
        else:
            creds = self.c.create_or_derive_api_key()
        self.c.set_api_creds(creds)
        log.warning("CLOB v2 подключён — БОЕВОЙ режим")

    def _opts(self, tick: float):
        return self.m.PartialCreateOrderOptions(tick_size=("%g" % tick))

    def take(self, token: str, usdc: float, cap: float, tick: float) -> Optional[dict]:
        """FAK BUY на usdc с потолком cap. None — исход неизвестен."""
        m = self.m
        args = m.MarketOrderArgs(token_id=token, amount=round(usdc, 2), side=m.Side.BUY,
                                 order_type=m.OrderType.FAK, price=round(cap, 6))
        try:
            try:
                signed = self.c.create_market_order(args, self._opts(tick))
            except Exception as e:
                mt = self._MIN_TICK_RX.search(str(e))
                if not mt:
                    return {"success": False, "status": "rejected", "errorMsg": f"сборка: {e}"[:200]}
                t2 = float(mt.group(1))
                args.price = max(tick_floor(cap, t2), t2)
                signed = self.c.create_market_order(args, self._opts(t2))
        except Exception as e:
            return {"success": False, "status": "rejected", "errorMsg": f"сборка: {e}"[:200]}
        try:
            return self.c.post_order(signed, m.OrderType.FAK)
        except Exception as e:
            code = getattr(e, "status_code", None)
            if isinstance(code, int) and 400 <= code < 500:
                return {"success": False, "status": "rejected", "errorMsg": str(e)[:200]}
            log.error("FAK: исход неизвестен: %s", e)
            return None

    def place_limit(self, token: str, price: float, shares: float, tick: float, expiration: int) -> dict:
        """GTD post-only BUY. {"ok","id","unknown","err"}"""
        m = self.m
        try:
            args = m.OrderArgsV2(token_id=token, price=round(price, 6), size=round(shares, 2), side=m.Side.BUY, expiration=int(expiration))
            signed = self.c.create_order(args, self._opts(tick))
        except Exception as e:
            return {"ok": False, "unknown": False, "err": f"сборка: {e}"[:200]}
        try:
            r = self.c.post_order(signed, m.OrderType.GTD, post_only=True)
        except Exception as e:
            code = getattr(e, "status_code", None)
            return {"ok": False, "unknown": not (isinstance(code, int) and 400 <= code < 500), "err": str(e)[:200]}
        oid = (r or {}).get("orderID") or (r or {}).get("orderId") or (r or {}).get("id") if isinstance(r, dict) else None
        if not oid or (isinstance(r, dict) and r.get("success") is False):
            return {"ok": False, "unknown": False, "err": str((r or {}).get("errorMsg") if isinstance(r, dict) else r)[:200]}
        return {"ok": True, "id": oid}

    def matched(self, oid: str) -> Optional[Tuple[float, str]]:
        """(налито акций, статус) или None — нет ответа"""
        try:
            o = self.c.get_order(oid)
        except Exception as e:
            log.debug("ордер %s: %s", oid[:12], e); return None
        if not isinstance(o, dict): return None
        try: mt = float(o.get("size_matched") or o.get("sizeMatched") or 0)
        except (TypeError, ValueError): mt = 0.0
        return mt, str(o.get("status") or "").upper()

    @staticmethod
    def take_matched(resp: Any) -> Tuple[float, float]:
        """(USDC, шары): при BUY makingAmount = USDC, takingAmount = шары."""
        if not isinstance(resp, dict) or resp.get("success") is False:
            return 0.0, 0.0
        def num(*ks):
            for k in ks:
                v = resp.get(k)
                if v not in (None, ""):
                    try: return float(v)
                    except (TypeError, ValueError): pass
            return None
        u, s = num("making_amount", "makingAmount"), num("taking_amount", "takingAmount")
        return (max(0.0, u), max(0.0, s)) if u is not None and s is not None else (0.0, 0.0)


class PaperClob:
    """покупка «по стакану»: забираем аски <= потолка из текущего /book (биржа не трогается)"""
    enabled = False

    def __init__(self):
        log.warning("PAPER=1 — бумажный режим: покупки считаются по текущему стакану, ордера не отправляются")

    def take(self, token, usdc, cap, tick):
        bk = get_book(token); spent = sh = 0.0
        for p, s in bk["asks"]:
            if p > cap + 1e-12 or spent >= usdc - 1e-9: break
            q = min(s, (usdc - spent) / p); spent += q * p; sh += q
        if sh <= 0:
            best = bk["asks"][0][0] if bk["asks"] else None
            return {"success": False, "status": "rejected", "errorMsg": f"нет асков <= {cap:g} (лучший аск {best})"}
        return {"success": True, "status": "matched", "makingAmount": spent, "takingAmount": sh}

    take_matched = staticmethod(Clob.take_matched)

    def place_limit(self, token, price, shares, tick, expiration):
        bk = get_book(token)
        if bk["asks"] and bk["asks"][0][0] <= price + 1e-12:
            return {"ok": False, "unknown": False, "err": "post-only: пересекает стакан"}
        return {"ok": True, "id": f"paper-{token[-6:]}-{price:g}-{int(time.time() * 1000)}"}

    def matched(self, oid):
        return None                                   # бумажный налив считает paper_fills() по сделкам рынка


CLOB = None


def tg_send(text: str, **kw) -> Optional[dict]:
    return tg("sendMessage", chat_id=Cfg.TG_CHAT, text=text[:4000], parse_mode="HTML", disable_web_page_preview=True, **kw)


# ----------------------------------------------------------------------------- решения
def set_state(r, now, **upd):
    db.execute("update fq set %s where tx=? and li=?" % ", ".join(f"{k}=?" for k in upd), (*upd.values(), r["tx"], r["li"]))


def want_buy(r, val, reason, now):
    outs = json.loads(r["outcomes"]); toks = json.loads(r["tokens"] or "[]")
    if val not in outs or len(toks) != 2:
        set_state(r, now, state="skip", note=f"исход фида «{val}» не найден среди исходов {outs}"); return
    tok = str(toks[outs.index(val)])
    set_state(r, now, state="buy", val=val, token=tok, reason=reason[:400], next_t=now)
    log.warning("СИГНАЛ | %s | %s | предложено «%s», фид «%s» | %s", r["cat"], r["slug"][:70], r["proposed"], val, reason[:160])


def retry_or_end(r, now, step, why):
    if now >= r["exp"] - 60:
        set_state(r, now, state="nofeed", note=why[:300])
    else:
        set_state(r, now, state="wait", next_t=now + int(step), tries=(r["tries"] or 0) + 1, note=why[:300])


def decide_generic(r, res, now, step, confirm_s, strong_fn):
    """res = [(фид, исход|None, пояснение, надёжно)]. Покупка, если: есть фид против (не 50-50), никто не за предложение,
    фиды против согласны между собой; ненадёжный одиночный фид — повтор через confirm_s с тем же исходом."""
    prop = r["proposed"]
    vals = [(n, v, w, s) for n, v, w, s in res if v]
    why = " | ".join(f"{n}={v or '—'} ({w})" for n, v, w, s in res)[:600]
    if any(v == prop for _, v, _, _ in vals):
        set_state(r, now, state="ok", note=why); return
    against = [(n, v, w, s) for n, v, w, s in vals if v != "50-50"]
    if not against:
        if vals: set_state(r, now, state="skip", note="только 50-50 (снятие/отмена) — не торгуем: " + why); return
        retry_or_end(r, now, step, why); return
    if len({v for _, v, _, _ in against}) > 1:
        set_state(r, now, state="skip", note="фиды против, но расходятся между собой: " + why); return
    val = against[0][1]
    if strong_fn(against):
        want_buy(r, val, why, now); return
    # одиночный ненадёжный — подтверждение тем же исходом через confirm_s
    if r["conf_val"] == val and r["conf_t"] and now - r["conf_t"] >= confirm_s:
        want_buy(r, val, why + " (подтверждено повторной проверкой)", now); return
    if r["conf_val"] != val:
        set_state(r, now, conf_val=val, conf_t=now)
    if now >= r["exp"] - Cfg.STOP_BEFORE_S:
        set_state(r, now, state="skip", note="не успели подтвердить до конца окна: " + why); return
    set_state(r, now, state="wait", next_t=now + int(min(step, confirm_s)), note="ждём подтверждения: " + why)


# ----------------------------------------------------------------------------- воркеры по видам спорта
def tn_worker(ev, rs, now):
    m = TN_SL_RX.match(ev); tour, d = m.group(1), m.group(3)
    a, b = players_of(rs[0]["title"], rs[0]["question"])
    if not a:
        for r in rs: set_state(r, now, state="skip", note="не разобрал игроков")
        return
    t0 = None
    try: t0 = datetime.fromisoformat((rs[0]["start"] or "").replace("Z", "+00:00")).timestamp() if rs[0]["start"] else None
    except ValueError: pass
    t0 = t0 or datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp() + 43200
    feeds = tn_feeds(tour, d, a, b, t0)
    for r in rs:
        outs = json.loads(r["outcomes"])
        res = []
        for nm, M, why in feeds:
            if M: res.append((nm, *tn_evaluate(r["slug"], outs, r["question"] or "", M)))
            else: res.append((nm, None, why, False))
        decide_generic(r, res, now, Cfg.TN_RECHECK_S, Cfg.TN_CONFIRM_S, strong_fn=lambda ag: any(s for _, _, _, s in ag))
    db.commit()


def es_worker2(ev, rs, now):
    em = ES_SL_RX.match(ev); game, date = em.group(1), em.group(3)
    a, b, bo = es_teams_of(rs[0]["title"])
    feeds = []
    if not a:
        feeds.append(("—", None, "не разобрал команды"))
    else:
        if game == "dota2": feeds.append(("OpenDota", *es_od_series(date, a, b)))
        if game == "lol": feeds.append(("Leaguepedia", *es_lp_series(date, a, b)))
        if ES.USE_BO3 and game in ES_BO3_DISC: feeds.append(("bo3.gg", *es_bo3_series(game, date, a, b)))
    for r in rs:
        ok, t = es_allowed(r["slug"])
        if not ok:
            set_state(r, now, state="skip", note=f"тип {t} не в белом списке"); continue
        g = dict(outcomes=json.loads(r["outcomes"]), question=r["question"] or "", bo=bo)
        res = []
        for nm, Sr, why in feeds:
            v, w = es_evaluate(r["slug"], g, Sr) if Sr else (None, why)
            res.append((nm, v, w, True))                        # фиды отдают только завершённые карты/серии
        decide_generic(r, res, now, Cfg.ES_RECHECK_S, 0, strong_fn=lambda ag: True)
    db.commit()


def foot_worker(byev, now):
    info = {}
    for ev, rs in byev.items():
        lg, ha, aa, date = EV_RX.match(ev).groups()[:4]; home, away = teams_of(rs[0]["title"], rs[0]["slug"])
        try: De, we = espn_data(lg, date, home, away)
        except Exception as e: De, we = None, f"ошибка {str(e)[:40]}"
        if De: De["g_home"], De["g_away"] = home, away
        info[ev] = dict(date=date, home=home, away=away, De=De, we=we)
    afmap, det, afwhy = {}, {}, {}
    if FC.AF_KEY:
        for ev in info:
            fid, sw, wh = af_fixture(info[ev]["date"], info[ev]["home"], info[ev]["away"])
            if fid: afmap[ev] = (fid, sw)
            else: afwhy[ev] = wh
        if afmap: det = af_details([f for f, _ in afmap.values()])
    for ev, rs in byev.items():
        x = info[ev]; De = x["De"]
        Da, wa = None, ("нет ключа" if not FC.AF_KEY else afwhy.get(ev, "матч не найден"))
        if ev in afmap:
            f = det.get(afmap[ev][0]); Da, wa = af_parse(f, afmap[ev][1]) if f else (None, f"нет деталей ({AF_ERR[0] or 'лимит/ошибка'})")
            if Da: Da["g_home"], Da["g_away"] = x["home"], x["away"]
        for r in rs:
            outs = json.loads(r["outcomes"])
            ft = foot_type(r["slug"]); rule = FOOT_RULES.get(ft)
            if not rule:
                set_state(r, now, state="skip", note=f"тип {ft} не в белом списке"); continue
            e, ew = evaluate(r["slug"], outs, De) if De else (None, x["we"])
            a, aw = evaluate(r["slug"], outs, Da) if Da else (None, wa)
            if FOOT_YES_ONLY_RX.search(r["slug"]) and any(v and tn_norm(v) == "no" for v in (e, a)):
                set_state(r, now, state="skip", note=f"MLS передачи: фид «нет» ненадёжен (вторые передачи не считает): ESPN={e} API-F={a}")
                continue
            res = [("ESPN", e, ew, False), ("API-F", a, aw, False)]
            if rule == "both":
                prop = r["proposed"]
                if e == prop or a == prop: set_state(r, now, state="ok", note=f"фид за предложение: ESPN={e} API-F={a}"); continue
                if e and a and e == a:
                    want_buy(r, e, f"оба фида: ESPN={e} ({ew}) | API-F={a} ({aw})", now); continue
                retry_or_end(r, now, FC.RECHECK, f"нужны оба фида против: ESPN={e} ({ew}) | API-F={a} ({aw})"); continue
            # any: оба против и согласны — сразу; один — повтор через FOOT_CONFIRM (стата досчитывается)
            decide_generic(r, res, now, FC.RECHECK, Cfg.FOOT_CONFIRM_S,
                           strong_fn=lambda ag: len(ag) >= 2 and len({v for _, v, _, _ in ag}) == 1)
        db.commit()


def feed_worker() -> None:
    now = int(time.time())
    rows = db.execute("select * from fq where state='wait' and next_t<=? order by exp limit 300", (now,)).fetchall()
    if not rows: return
    byev: Dict[str, list] = {}
    for r in rows: byev.setdefault((r["cat"], r["ev"]), []).append(r)
    foot = {}
    for (cat, ev), rs in byev.items():
        try:
            if cat == "tennis": tn_worker(ev, rs, now)
            elif cat == "esports": es_worker2(ev, rs, now)
            elif cat == "football": foot[ev] = rs
        except Exception as e:
            log.error("%s %s: %s", cat, ev, e, exc_info=True)
            for r in rs: set_state(r, now, next_t=now + 60)
            db.commit()
    if foot:
        try: foot_worker(foot, now)
        except Exception as e:
            log.error("футбол: %s", e, exc_info=True)
            for rs in foot.values():
                for r in rs: set_state(r, now, next_t=now + 60)
            db.commit()


# ----------------------------------------------------------------------------- исполнение
def day_key(t=None): return time.strftime("%Y-%m-%d", time.gmtime(t or time.time()))
def spent_today() -> float:
    t0 = int(time.time()) - int(time.time()) % 86400
    return db.execute("select coalesce(sum(usdc),0) from trades where t>=?", (t0,)).fetchone()[0]
def spent_event(cat, ev) -> float:
    return db.execute("select coalesce(sum(spent),0) from fq where cat=? and ev=?", (cat, ev)).fetchone()[0]


def trader() -> None:
    now = int(time.time())
    rows = db.execute("select * from fq where state in ('buy','buying') and next_t<=? order by exp", (now,)).fetchall()
    for r in rows:
        first = r["first_try"] or now
        if r["first_try"] is None: set_state(r, now, first_try=now)
        left_usd = Cfg.TRADE_USD - (r["spent"] or 0)
        stop = None
        if left_usd < Cfg.MIN_ORDER: stop = "куплено на всю сумму"
        elif not r["disputed"] and now >= r["exp"] - Cfg.STOP_BEFORE_S: stop = "окно оспаривания заканчивается без спора"
        elif now - first > Cfg.RETRY_MAX_S: stop = f"докупка дольше {Cfg.RETRY_MAX_S / 60:.0f} мин"
        if stop:
            set_state(r, now, state="done", note=stop); db.commit()
            if (r["spent"] or 0) <= 0 and Cfg.NOTIFY_SIGNAL_NOFILL:
                tg_send(f"➖ Сигнал без покупки: {esc(stop)}\n<b>{esc(r['question'] or r['slug'])}</b>\n"
                        f"предложено {esc(r['proposed'])}, фид: <b>{esc(r['val'])}</b>, потолок {Cfg.MAX_PRICE:g}\n{esc(r['url'] or '')}")
            continue
        room_day = Cfg.DAILY_USD - spent_today()
        room_ev = Cfg.EVENT_USD - spent_event(r["cat"], r["ev"])
        usd = min(left_usd, room_day, room_ev)
        if usd < Cfg.MIN_ORDER:
            why = "дневной лимит" if room_day < Cfg.MIN_ORDER else "лимит на матч"
            set_state(r, now, state="done", note=why); db.commit()
            k = f"limit_msg|{why}|{day_key()}"
            if not kv_get(k):
                kv_set(k, 1); tg_send(f"⛔ {why} исчерпан (DAILY_MAX_USD={Cfg.DAILY_USD:g}, EVENT_MAX_USD={Cfg.EVENT_USD:g}) — "
                                      f"сигнал пропущен: {esc(r['slug'])}")
            continue
        bk = get_book(r["token"])
        btxt = book_text(bk, Cfg.MAX_PRICE) if bk["ok"] else "стакан не получен"
        if r["book"] is None:
            set_state(r, now, book=btxt); log.info("СТАКАН | %s | «%s»: %s", r["slug"][:60], r["val"], btxt)
        if bk["ok"] and not any(p <= Cfg.MAX_PRICE + 1e-12 for p, _ in bk["asks"]):
            if Cfg.LADDER_ON and not r["ladder"]: place_ladder(r, bk, now)
            set_state(r, now, state="buying", next_t=now + int(Cfg.RETRY_S), note=f"нет асков <= {Cfg.MAX_PRICE:g}: {btxt}")
            db.commit(); continue
        resp = CLOB.take(r["token"], usd, Cfg.MAX_PRICE, r["tick"] or 0.01)
        status = str(resp.get("status") or "").lower() if isinstance(resp, dict) else ""
        u, sh = CLOB.take_matched(resp)
        db.execute("insert into trades(tx, li, t, slug, cat, token, usd_req, cap, status, usdc, shares, paper, resp) values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (r["tx"], r["li"], now, r["slug"], r["cat"], r["token"], usd, Cfg.MAX_PRICE, status, u, sh,
                    0 if CLOB.enabled else 1, str(resp)[:300]))
        if resp is None or status == "delayed" or (status == "matched" and u <= 0):
            set_state(r, now, state="unknown", note=f"FAK: исход неизвестен ({status or 'нет ответа'})"); db.commit()
            tg_send(f"❗ Ордер: исход неизвестен ({esc(status or 'нет ответа')}) — проверь позицию вручную\n"
                    f"<b>{esc(r['question'] or r['slug'])}</b>\nпокупка «{esc(r['val'])}» на ${usd:.2f} ≤ {Cfg.MAX_PRICE:g}\n{esc(r['url'] or '')}")
            log.error("FAK исход неизвестен | %s", r["slug"][:70]); continue
        if u > 0 and sh > 0:
            sp, shs = (r["spent"] or 0) + u, (r["shares"] or 0) + sh
            more = Cfg.TRADE_USD - sp >= Cfg.MIN_ORDER
            set_state(r, now, spent=sp, shares=shs, state="buying" if more else "done", next_t=now + int(Cfg.RETRY_S),
                      note="докупка" if more else "куплено на всю сумму")
            db.commit()
            mode = "🧾 БУМАГА" if not CLOB.enabled else "✅ КУПЛЕНО"
            tg_send(f"{mode}: {sh:.1f} акц. «<b>{esc(r['val'])}</b>» по ср. {u / sh:.3f} на ${u:.2f}"
                    f"{' (всего ' + format(shs, '.1f') + ' за $' + format(sp, '.2f') + ')' if shs > sh else ''}\n"
                    f"<b>{esc(r['question'] or r['slug'])}</b>\nпредложено: {esc(r['proposed'])} · {esc(r['cat'])}\n"
                    f"фиды: {esc((r['reason'] or '')[:500])}\n"
                    f"стакан «{esc(r['val'])}» до покупки: {esc(btxt)}\n"
                    f"конец оспаривания {hm(r['exp'])} (через {(r['exp'] - now) / 60:.0f} мин){' · уже оспорено' if r['disputed'] else ''}\n{esc(r['url'] or '')}")
            log.warning("КУПЛЕНО | %s | %.1f «%s» по %.3f на $%.2f%s", r["slug"][:60], sh, r["val"], u / sh, u, "" if CLOB.enabled else " (бумага)")
        else:
            if Cfg.LADDER_ON and not r["ladder"]: place_ladder(r, bk, now)
            set_state(r, now, state="buying", next_t=now + int(Cfg.RETRY_S),
                      note="не налито: " + str((resp or {}).get("errorMsg") or status)[:200])
            db.commit()


# ----------------------------------------------------------------------------- сетка лимиток (мёртвый стакан)
def ladder_reserved_today() -> float:
    t0 = int(time.time()) - int(time.time()) % 86400
    return db.execute("select coalesce(sum(level*shares),0) from lorders where placed_t>=?", (t0,)).fetchone()[0]


def place_ladder(r, bk, now) -> None:
    """лимитки BUY стороны фида ниже потолка; биржа снимет их сама через LADDER_TTL_H"""
    set_state(r, now, ladder=1)
    tick = bk.get("tick") or r["tick"] or 0.01
    best_ask = bk["asks"][0][0] if bk["asks"] else 1.0
    toks = json.loads(r["tokens"]); other = toks[1] if toks[0] == r["token"] else toks[0]
    exp_t = now + int(Cfg.LADDER_TTL_S)
    placed, skipped = [], []
    for lvl, usd in sorted(Cfg.LADDER, reverse=True):
        if lvl >= Cfg.MAX_PRICE or lvl >= best_ask or not tick_ok(lvl, tick):
            skipped.append(f"{lvl:g}"); continue
        shares = max(bk.get("min_size") or Cfg.MIN_SHARES, usd / lvl)
        if ladder_reserved_today() + lvl * shares > Cfg.LADDER_DAILY_USD:
            skipped.append(f"{lvl:g}(лимит)"); continue
        res = CLOB.place_limit(r["token"], lvl, shares, tick, exp_t + 60)
        if res.get("ok"):
            db.execute("insert or replace into lorders values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (res["id"], r["tx"], r["li"], r["cid"], r["token"], other, lvl, round(shares, 2), 0, "resting", now, exp_t,
                        0 if CLOB.enabled else 1))
            placed.append(f"{lvl:g}×{shares:.0f}")
        else:
            skipped.append(f"{lvl:g}({res.get('err', '')[:40]})")
            if res.get("unknown"):
                tg_send(f"❗ Лимитка {lvl:g}: исход неизвестен — проверь ордера вручную\n{esc(r['slug'])}")
    db.commit()
    log.warning("СЕТКА | %s | «%s» | %s | пропущено: %s", r["slug"][:60], r["val"], " ".join(placed) or "—", " ".join(skipped) or "—")
    if placed:
        tg_send(f"🪜 {'БУМАГА: ' if not CLOB.enabled else ''}сетка лимиток «<b>{esc(r['val'])}</b>» (тейкер не налил, асков ≤ {Cfg.MAX_PRICE:g} нет): "
                f"{esc(' '.join(placed))} — на {sum(float(x.split('×')[0]) * float(x.split('×')[1]) for x in placed):.2f}$, живут {Cfg.LADDER_TTL_S / 3600:.0f} ч\n"
                f"<b>{esc(r['question'] or r['slug'])}</b>\nпредложено {esc(r['proposed'])} · стакан: {esc(book_text(bk, Cfg.MAX_PRICE))}\n{esc(r['url'] or '')}")


def paper_fills(now) -> None:
    """бумага: лимитка налита, если после постановки прошла сделка стороны фида по цене <= уровня (или противоположной >= 1-уровня)"""
    for cid, in db.execute("select distinct cid from lorders where state='resting' and paper=1").fetchall():
        cur = db.execute("select t from paper_cursor where cid=?", (cid,)).fetchone()
        last = cur[0] if cur else now - 5
        tr = http_get(Cfg.DATA + "/trades", {"market": cid, "limit": 500, "takerOnly": "true"}) or []
        mx = last
        for x in sorted(tr, key=lambda z: int(z.get("timestamp") or 0)):
            t = int(x.get("timestamp") or 0)
            if t <= last: continue
            mx = max(mx, t)
            p, a, sz = float(x.get("price") or 0), str(x.get("asset")), float(x.get("size") or 0)
            for o in db.execute("select * from lorders where cid=? and state='resting' order by level desc", (cid,)).fetchall():
                pu = p if a == o["token"] else (1 - p) if a == o["other"] else None
                if pu is None or pu > o["level"] + 1e-9 or t < o["placed_t"] or sz <= 0: continue
                q = min(sz, o["shares"] - o["matched"]); sz -= q
                if q > 0: ladder_fill(o, o["matched"] + q, now)
        db.execute("insert or replace into paper_cursor values(?,?)", (cid, mx)); db.commit()


def ladder_fill(o, new_matched, now) -> None:
    add = new_matched - (o["matched"] or 0)
    if add <= 1e-9: return
    state = "filled" if new_matched >= o["shares"] - 1e-6 else "resting"
    db.execute("update lorders set matched=?, state=? where oid=?", (new_matched, state, o["oid"]))
    db.execute("update fq set spent=coalesce(spent,0)+?, shares=coalesce(shares,0)+? where tx=? and li=?",
               (add * o["level"], add, o["tx"], o["li"]))
    db.commit()
    log.warning("ЛИМИТКА | %s | +%.1f по %g", o["cid"][:12], add, o["level"])
    _FILLS.setdefault((o["tx"], o["li"], o["paper"]), []).append((add, o["level"]))


_FILLS: Dict[tuple, list] = {}
def flush_fills() -> None:
    """одно сообщение на рынок за проверку: сколько налилось по каким уровням"""
    for (tx, li, paper), xs in list(_FILLS.items()):
        q = db.execute("select * from fq where tx=? and li=?", (tx, li)).fetchone()
        sh = sum(a for a, _ in xs); usd = sum(a * l for a, l in xs)
        lv = " ".join(f"{l:g}×{a:.0f}" for a, l in xs)
        tg_send(f"🪜 {'БУМАГА: ' if paper else ''}налиты лимитки «{esc(q['val'] if q else '')}»: +{sh:.1f} акц. на ${usd:.2f} ({esc(lv)}); "
                f"всего {q['shares']:.1f} акц. за ${q['spent']:.2f}\n{esc((q['question'] or q['slug']) if q else '')}")
    _FILLS.clear()


def ladder_worker() -> None:
    now = int(time.time())
    if now - kv_get("ladder_t", 0) < Cfg.LADDER_CHECK_S: return
    kv_set("ladder_t", now)
    db.execute("update lorders set state='expired' where state='resting' and exp_t<?", (now,)); db.commit()
    if not CLOB.enabled:
        paper_fills(now); flush_fills(); return
    for o in db.execute("select * from lorders where state='resting' and paper=0").fetchall():
        res = CLOB.matched(o["oid"])
        if res is None: continue
        mt, st = res
        if mt > (o["matched"] or 0): ladder_fill(o, mt, now)
        if st in ("CANCELED", "CANCELLED", "EXPIRED", "CANCELED_MARKET_RESOLVED", "INVALID"):
            db.execute("update lorders set state=? where oid=?", (st.lower(), o["oid"])); db.commit()
    flush_fills()


def watchdog() -> None:
    """позиция есть, спора нет, окно заканчивается -> предупреждение (рынок уйдёт по неверному предложению)"""
    now = int(time.time())
    for r in db.execute("select * from fq where spent>0 and disputed=0 and warned=0 and exp-?<?", (now, Cfg.WARN_S)).fetchall():
        set_state(r, now, warned=1); db.commit()
        tg_send(f"⚠️ <b>Спора нет</b>, до конца оспаривания {(r['exp'] - now) / 60:.0f} мин — позиция уйдёт в ноль, если предложение примут\n"
                f"<b>{esc(r['question'] or r['slug'])}</b>\nпредложено {esc(r['proposed'])}, у нас «{esc(r['val'])}»: "
                f"{r['shares']:.1f} акц. за ${r['spent']:.2f}\n{esc(r['url'] or '')}")


def results() -> None:
    """итог рынков с позицией: PnL в Telegram"""
    now = int(time.time())
    if now - kv_get("results_t", 0) < 600: return
    kv_set("results_t", now)
    for r in db.execute("select * from fq where spent>0 and result is null and exp<?", (now - 1800,)).fetchall():
        m = market_for(r["cid"], "", "")
        if not m or not m.get("closed"): continue
        px = [float(x) for x in jl(m.get("outcomePrices"), []) or []]; outs = json.loads(r["outcomes"])
        if len(px) != 2: continue
        pay = px[outs.index(r["val"])] if r["val"] in outs else 0.0
        if 0.02 < pay < 0.98 and abs(pay - 0.5) > 0.02: continue      # ещё не финальные цены
        pnl = r["shares"] * pay - r["spent"] * (1 + Cfg.BUILDER_FEE)
        set_state(r, now, result=f"{pay:g}", pnl=pnl); db.commit()
        tg_send(f"{'💰' if pnl > 0 else '🔻'} Итог: «{esc(r['val'])}» -> {pay:g} · PnL <b>{pnl:+.2f}$</b> "
                f"({r['shares']:.1f} акц. за ${r['spent']:.2f})\n{esc(r['question'] or r['slug'])}")


def daily() -> None:
    d = time.strftime("%Y-%m-%d", time.gmtime(time.time() + Cfg.TZ_H * 3600))
    last = kv_get("daily_d")
    if last == d: return
    kv_set("daily_d", d)
    if not last: return
    tg_send("<pre>" + esc(report_text(days=1)) + "</pre>")


def report_text(days=None) -> str:
    t0 = int(time.time() - days * 86400) if days else 0
    rows = db.execute("select * from fq where first_try is not null and first_try>=?", (t0,)).fetchall()
    by = defaultdict(lambda: [0, 0, 0.0, 0.0, 0, 0.0])          # сигналов, с покупкой, $, pnl закрытых, закрыто, акций
    for r in rows:
        x = by[r["cat"]]; x[0] += 1
        if (r["spent"] or 0) > 0:
            x[1] += 1; x[2] += r["spent"]; x[5] += r["shares"] or 0
            if r["pnl"] is not None: x[3] += r["pnl"]; x[4] += 1
    out = [f"{'за сутки' if days else 'всего'} ({'бумага' if Cfg.PAPER else 'боевой'}):",
           f"{'вид':<10}{'сигн':>6}{'купл':>6}{'$':>9}{'закр':>6}{'PnL$':>9}"]
    for k, x in sorted(by.items()):
        out.append(f"{k:<10}{x[0]:>6}{x[1]:>6}{x[2]:>9.2f}{x[4]:>6}{x[3]:>9.2f}")
    lo = db.execute("select count(*), coalesce(sum(matched),0), coalesce(sum(matched*level),0) from lorders where placed_t>=?", (t0,)).fetchone()
    out.append(f"лимитки: поставлено {lo[0]}, налито {lo[1]:.0f} акц. на ${lo[2]:.2f}")
    st = db.execute("select state, count(*) from fq where next_t>=? group by state", (t0,)).fetchall()
    out.append("состояния: " + ", ".join(f"{a} {b}" for a, b in st))
    return "\n".join(out)


# ----------------------------------------------------------------------------- события UMA
def cat_of(slug):
    if is_tn(slug): return "tennis" if Cfg.TRADE_TENNIS else None
    if is_es(slug): return "esports" if Cfg.TRADE_ESPORTS else None
    if is_football(slug): return "football" if Cfg.TRADE_FOOTBALL else None
    return None


def on_propose(d: dict) -> None:
    if db.execute("select 1 from props where tx=? and li=?", (d["tx"], d["li"])).fetchone():
        return
    k = kind_of(d["proposed"])
    if k not in ("yes", "no"): return
    title = anc_title(d["anc"])
    m = market_for(d["cid"], d["qid"], title)
    slug = (m or {}).get("slug") or ""
    cat = cat_of(slug) if m else None
    if not cat: return
    outs = jl((m or {}).get("outcomes"), []) or []
    toks = jl((m or {}).get("clobTokenIds"), []) or []
    if len(outs) != 2 or len(toks) != 2: return
    cid = str(m.get("conditionId") or d["cid"]).lower()
    repeat = db.execute("select count(*) from props where cid=?", (cid,)).fetchone()[0]
    pout = outs[0] if k == "yes" else outs[1]
    ev0 = (m.get("events") or [{}])[0]
    url = f"https://polymarket.com/event/{ev0.get('slug')}/{slug}" if ev0.get("slug") else f"https://polymarket.com/market/{slug}"
    t = block_ts(d["block"])
    row = dict(tx=d["tx"], li=d["li"], t=t, block=d["block"], cid=cid, qid=d["qid"], proposer=d["proposer"].lower(), kind=k,
               proposed_out=pout, exp=d["exp"], slug=slug, cat=cat, question=(m.get("question") or title)[:300], url=url)
    db.execute("insert or replace into props(%s) values(%s)" % (",".join(row), ",".join("?" * len(row))), list(row.values()))
    n_disp = [str(x).lower() for x in (jl(m.get("umaResolutionStatuses"), []) or [])].count("disputed")
    if Cfg.FIRST_ONLY and (repeat or n_disp):
        db.commit(); log.info("повторное предложение (споров было %d) — пропуск | %s", n_disp, slug[:70]); return
    if m.get("closed"):
        db.commit(); return
    if cat == "esports":
        em = ES_SL_RX.match(slug); evk = f"{em.group(1)}-{em.group(2)}-{em.group(3)}"
    elif cat == "tennis":
        tm = TN_SL_RX.match(slug); evk = f"{tm.group(1)}-{tm.group(2)}-{tm.group(3)}"
    else:
        evk = "-".join(EV_RX.match(slug).groups()[:4])
    delay = FC.FIRST if cat == "football" else 0
    tick = float(m.get("orderPriceMinTickSize") or 0.01)
    db.execute("""insert or ignore into fq(tx, li, slug, ev, title, outcomes, proposed, exp, next_t, state, cat, cid, tokens, tick, start,
                  question, url) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (d["tx"], d["li"], slug, evk, ev0.get("title") or "", json.dumps(outs), pout, d["exp"], int(time.time() + delay), "wait",
                cat, cid, json.dumps([str(x) for x in toks]), tick, m.get("gameStartTime") or ev0.get("startTime") or "",
                row["question"], url))
    db.commit()
    log.info("ПРЕДЛОЖЕНИЕ | %s | %s | «%s» | до конца %.0f мин", cat, slug[:70], pout, (d["exp"] - time.time()) / 60)


def on_dispute(d: dict) -> None:
    r = db.execute("select * from props where cid=? and disputed_t is null order by t desc limit 1", (d["cid"],)).fetchone()
    if not r: return
    t = block_ts(d["block"])
    db.execute("update props set disputed_t=? where tx=? and li=?", (t, r["tx"], r["li"]))
    db.execute("update fq set disputed=1 where tx=? and li=?", (r["tx"], r["li"])); db.commit()
    q = db.execute("select * from fq where tx=? and li=?", (r["tx"], r["li"])).fetchone()
    log.warning("ОСПОРЕНО | %s | через %.0f мин", r["slug"][:60], (t - r["t"]) / 60)
    if q and (q["spent"] or 0) > 0:
        tg_send(f"⚔️ Оспорено через {(t - r['t']) / 60:.0f} мин после предложения — по нашей позиции «{esc(q['val'])}» "
                f"({q['shares']:.1f} акц. за ${q['spent']:.2f})\n{esc(q['question'] or q['slug'])}")


def poll() -> None:
    lb = int(rpc("eth_blockNumber", []), 16)
    last = kv_get("last_block") or (lb - int(Cfg.BACKFILL_MIN * 60 / 2))
    if lb <= last: return
    logs = sorted(get_logs(last + 1, lb), key=lambda g: (int(g["blockNumber"], 16), int(g["logIndex"], 16)))
    for lg in logs:
        try:
            d = decode(lg)
            on_propose(d) if d["ev"] == "propose" else on_dispute(d)
        except Exception as e:
            log.error("разбор события %s: %s", lg.get("transactionHash", "")[:12], e, exc_info=True)
    kv_set("last_block", lb)


# ----------------------------------------------------------------------------- запуск
STOP = False
def _stop(*_):
    global STOP; STOP = True


def run() -> None:
    global CLOB
    if not Cfg.TG_TOKEN or not Cfg.TG_CHAT: raise SystemExit("нет TG_TOKEN / TG_CHAT_ID в autotaker.env")
    CLOB = PaperClob() if Cfg.PAPER else Clob()
    signal.signal(signal.SIGINT, _stop); signal.signal(signal.SIGTERM, _stop)
    kinds = [k for k, on in (("теннис", Cfg.TRADE_TENNIS), ("футбол", Cfg.TRADE_FOOTBALL), ("киберспорт", Cfg.TRADE_ESPORTS)) if on]
    msg = (f"autotaker запущен ({'БУМАГА' if Cfg.PAPER else 'БОЕВОЙ'}): {', '.join(kinds)}; ${Cfg.TRADE_USD:g} на рынок, потолок {Cfg.MAX_PRICE:g}, "
           f"в сутки до ${Cfg.DAILY_USD:g}, на матч до ${Cfg.EVENT_USD:g}")
    log.info("%s | env=%s | RPC %s", msg, ENV_USED or "нет", Cfg.RPC)
    tg_send(msg)
    errs = 0
    while not STOP:
        try:
            poll(); feed_worker(); trader(); ladder_worker(); watchdog(); results(); daily(); errs = 0
        except Exception as e:
            errs += 1
            log.error("цикл: %s", str(e)[:200], exc_info=errs == 1)
            if errs == 30: tg_send(f"autotaker: 30 ошибок подряд, последняя: {esc(str(e)[:200])}")
        for _ in range(int(Cfg.POLL_S * 2)):
            if STOP: break
            time.sleep(0.5)
    log.info("остановлен")


def test() -> None:
    print("env:", ENV_USED or "не найден")
    print("RPC блок:", int(rpc("eth_blockNumber", []), 16))
    print("Gamma:", "ок" if http_get(Cfg.GAMMA + "/markets", {"limit": 1}) else "нет ответа")
    fs = fs_day(datetime.now(timezone.utc).date().isoformat())
    print("Flashscore сегодня: матчей", len(fs), "| tennisexplorer сегодня:", len(te_day(datetime.now(timezone.utc).date().isoformat())))
    print("Telegram:", "ок" if tg_send("autotaker: тест связи ✅") else "ошибка — проверь TG_TOKEN/TG_CHAT_ID")
    if not Cfg.PAPER:
        c = Clob(); print("CLOB: подключение ок (баланс смотри на сайте/кошельке)")


def check(slug, prop) -> None:
    """прогон одного рынка через фиды и правила (без покупки)"""
    mk = http_get(Cfg.GAMMA + "/markets", {"slug": slug}) or http_get(Cfg.GAMMA + "/markets", {"slug": slug, "closed": "true"}) or [{}]
    m = mk[0]; outs = jl(m.get("outcomes"), []) or []; ev0 = (m.get("events") or [{}])[0]
    cat = "tennis" if is_tn(slug) else "esports" if is_es(slug) else "football" if is_football(slug) else None
    print("рынок:", m.get("question"), outs, "| событие:", ev0.get("title"), "| вид:", cat)
    if not cat or len(outs) != 2: return
    if cat == "tennis":
        tm = TN_SL_RX.match(slug); a, b = players_of(ev0.get("title", ""), m.get("question", ""))
        print("игроки:", a, "|", b)
        t0 = datetime.fromisoformat(tm.group(3)).replace(tzinfo=timezone.utc).timestamp() + 43200
        for nm, M, why in tn_feeds(tm.group(1), tm.group(3), a, b, t0):
            print(f"  {nm}:", tn_evaluate(slug, outs, m.get("question", ""), M) if M else why)
    elif cat == "esports":
        ok, t = es_allowed(slug); print("тип:", t, "| в белом списке:", ok)
        em = ES_SL_RX.match(slug); a, b, bo = es_teams_of(ev0.get("title", ""))
        g = dict(outcomes=outs, question=m.get("question", ""), bo=bo)
        for nm, fn in (("OpenDota", es_od_series), ("Leaguepedia", es_lp_series)):
            if (nm == "OpenDota" and em.group(1) == "dota2") or (nm == "Leaguepedia" and em.group(1) == "lol"):
                Sr, why = fn(em.group(3), a, b); print(f"  {nm}:", es_evaluate(slug, g, Sr) if Sr else why)
        Sr, why = es_bo3_series(em.group(1), em.group(3), a, b); print("  bo3.gg:", es_evaluate(slug, g, Sr) if Sr else why)
    else:
        ft = foot_type(slug); print("тип:", ft, "| правило:", FOOT_RULES.get(ft, "не торгуем"))
        lg, ha, aa, date = EV_RX.match(slug).groups()[:4]; home, away = teams_of(ev0.get("title", ""), slug)
        De, we = espn_data(lg, date, home, away); print("  ESPN:", evaluate(slug, outs, De) if De else we)
        fid, sw, _w = af_fixture(date, home, away)
        f = af_details([fid]).get(fid) if fid else None
        Da, wa = af_parse(f, sw) if f else (None, "матч не найден / лимит")
        print("  API-Football:", evaluate(slug, outs, Da) if Da else wa)
    if prop: print("предложено:", prop)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run": run()
    elif cmd == "test": test()
    elif cmd == "report": print(report_text())
    elif cmd == "check": check(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
    else: print(__doc__)
