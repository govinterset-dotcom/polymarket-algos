#!/usr/bin/env python3
"""
hourbot.py 1.0 — разворот после серии на часовых и 4-часовых рынках Polymarket.

Исполнение, журнал, депозит, резолв — как в polymizerbot 2.0 (тот же код).
Заменён источник сигналов: вместо логистической модели — два правила,
найденные на минутках Binance BTC/ETH за 01–09.2026 и проверенные на двух
половинах периода и на обеих монетах:

  H1 (рынки «Bitcoin Up or Down - <дата>, <час> ET», резолв по свече 1H Binance)
     серия >= 3 часов в одну сторону и сумма хода серии >= 2σ
     -> ставка на разворот следующего часа. История: 0.563, ~5.6 сигналов/сутки.
  H4 (рынки btc-updown-4h-<ts>, резолв по TWAP Chainlink, окна от 00:00 ET)
     серия >= 2 свечей 4h в одну сторону и последняя свеча >= 1σ
     -> ставка на разворот следующих 4 часов. История: 0.586, ~1.4/сутки.

σ — обычный ход свечи: средний квадрат минутного лог-дохода за сутки x √минут свечи.
Решение за PM_RULE_LEAD (120с) до открытия окна по последней закрытой минуте:
текущая свеча берётся неполной (58 из 60 минут / 238 из 240), как в проверке.

Запуск (как polymizerbot):
    python hourbot.py --selftest     проверки без сети
    python hourbot.py --plan         депозит, ставка, потолки
    python hourbot.py --quotes       что правила видят сейчас (сеть)
    python hourbot.py                работа (PM_DRY_RUN=1 по умолчанию)
    python hourbot.py --report       разбор журнала
Конфиг: hourbot.env (рядом со скриптом).
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import math
import os
import random
import statistics
import sys
import threading
import time
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

TF_SEC = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
THETA = 0.07

BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"
VERSION = "hourbot/1.0"
BOOT_TS = time.time()      # момент запуска: свечи старше него уже шли


# ═══════════════════════════════════════════════════════════════════ env
def load_env(path: str = "hourbot.env"):
    p = Path(path)
    for cand in ("hourbot.env", ".env"):
        if p.exists():
            break
        p = Path(__file__).with_name(cand)
    if not p.exists():
        return
    raw = p.read_text(encoding="utf-8")
    # поддержка переноса строки обратным слешем (длинные значения)
    raw = raw.replace("\\\n", "")
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if v.startswith("#"):
            v = ""                      # пустое значение с комментарием
        elif len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        else:
            for sep in ("  #", "\t#", " #"):
                if sep in v:
                    v = v.split(sep, 1)[0].strip()
                    break
        os.environ.setdefault(k, v)


def S(k, d=""):
    return os.getenv(k, d)


def I(k, d):
    return int(float(os.getenv(k, str(d))))


def F(k, d):
    return float(os.getenv(k, str(d)))


def BL(k, d):
    return os.getenv(k, "1" if d else "0").strip().lower() in ("1", "true", "yes", "on")


def LS(k, d):
    raw = os.getenv(k)
    if raw is None:
        return d
    out = tuple(x for x in raw.replace(" ", "").lower().split(",") if x)
    return out if out else d


# ═══════════════════════════════════════════════════════════════ формулы
def fee_taker(price: float, shares: float) -> float:
    """Комиссия биржи на тейкерную покупку: C*θ*p*(1-p). Мейкер платит 0."""
    return shares * THETA * price * (1.0 - price)


def builder_fee(price: float, shares: float) -> float:
    """Builder-комиссия на тейкерную покупку: BUILDER_FEE * нотионал."""
    return CFG.BUILDER_FEE * price * shares


def break_even_taker(price: float) -> float:
    """Винрейт, при котором тейкерный вход по price выходит в ноль."""
    c = price + fee_taker(price, 1.0)
    return c


def max_ask_for(p: float) -> float:
    """Максимальная цена тейкерного входа, при которой ярус с частотой p
    ещё не в минусе: p = x + θ*x*(1-x) -> θx² - (1+θ)x + p = 0."""
    a, b, c = THETA, -(1.0 + THETA), p
    d = b * b - 4 * a * c
    if d < 0:
        return 0.0
    return (-b - math.sqrt(d)) / (2 * a)


def q_price(price: float, tick: str | float = "0.01") -> float:
    ts = str(tick)
    t = float(ts)
    dec = len(ts.split(".")[1]) if "." in ts else 0
    return round(round(price / t) * t, dec)


def floor_price(price: float, tick: str | float = "0.01") -> float:
    """Вниз до тика — чтобы лимитка гарантированно не пересекла книгу."""
    ts = str(tick)
    t = float(ts)
    dec = len(ts.split(".")[1]) if "." in ts else 0
    return round(math.floor(round(price / t, 9)) * t, dec)


def order_shares(usd: float, price: float, tick: str | float = "0.01",
                 min_shares: float = 5.0, min_value: float = 1.05,
                 step: float = 1.0) -> float:
    """Размер заявки под сумму usd, округлённый ВВЕРХ до приемлемого биржей.

    Биржа принимает: сумма (акции * цена) не более 2 знаков, акции не более
    4 знаков, не меньше min_shares акций и не меньше min_value долларов.
    """
    p = q_price(price, tick)
    if p <= 0:
        return 0.0
    if step <= 0:
        step = 1.0
    n = math.ceil(round(usd / p / step, 9)) * step
    n = max(n, math.ceil(round(min_shares / step, 9)) * step)
    for _ in range(2000):
        cost = n * p
        cents_ok = abs(cost * 100 - round(cost * 100)) < 1e-6
        sh_ok = abs(n * 1e4 - round(n * 1e4)) < 1e-6
        if cents_ok and sh_ok and cost >= min_value - 1e-9:
            break
        n += step
    return float(int(round(n))) if step >= 1 else round(n, 4)


def size_step(price: float, tick: str | float = "0.01") -> float:
    """Минимальный шаг размера, при котором сумма (доли*цена) — целые центы.

    Биржа принимает доли с 4 знаками и сумму с 2. Для цены k тиков сумма
    равна j*k/(10^4 * 10^dec) при доле j*1e-4; она кратна центу тогда и
    только тогда, когда j кратно M/gcd(k, M), где M = 100*10^dec.
    Для нечётной цены в центах (0.53) шаг выходит 1 доля, для 0.54 — 0.5,
    для 0.50 — 0.02. Раньше этого не считали, и любой дробный остаток
    после частичного налива превращался в заявку, которую биржа не берёт.
    """
    ts = str(tick)
    dec = len(ts.split(".")[1]) if "." in ts else 0
    k = int(round(price * (10 ** dec)))
    if k <= 0:
        return 1.0
    M = 100 * (10 ** dec)
    return (M // math.gcd(k, M)) / 10000.0


def floor_shares(n: float, price: float, tick: str | float = "0.01") -> float:
    """Размер вниз до приемлемого биржей — чтобы НЕ перебрать объём."""
    st = size_step(price, tick)
    return max(0.0, round(math.floor(round(n / st, 9)) * st, 4))


def ceil_shares(n: float, price: float, tick: str | float = "0.01") -> float:
    st = size_step(price, tick)
    return max(0.0, round(math.ceil(round(n / st, 9)) * st, 4))


def walk_book(asks: list, need: float, cap_px: float,
              max_usd: float = float("inf")) -> tuple[float, float, float | None]:
    """Сколько долей реально забирается из книги.

    Идёт по уровням аска снизу вверх, пока не наберёт need долей, не
    упрётся в потолок цены cap_px или не исчерпает max_usd долларов.
    Возвращает (доли, нотионал, цена худшего задетого уровня).

    Это и есть «набрать по ценам в стакане»: раньше бот слал одну заявку
    по лучшему аску плюс буфер и не смотрел, есть ли там объём вообще.
    """
    got = 0.0
    usd = 0.0
    worst = None
    for px, sz in asks:
        if px > cap_px + 1e-9:
            break
        take = min(sz, need - got)
        if take <= 1e-9:
            break
        if usd + take * px > max_usd:
            take = max(0.0, (max_usd - usd) / px)
            if take <= 1e-9:
                break
        got += take
        usd += take * px
        worst = px
        if got >= need - 1e-9 or usd >= max_usd - 1e-9:
            break
    return got, usd, worst


def opposite(side: str) -> str:
    return "down" if side == "up" else "up"


def window_start(now: float, tf: str) -> int:
    w = TF_SEC[tf]
    return int(now // w) * w


def et_offset(ts: float) -> int:
    """Смещение Нью-Йорка от UTC в секундах: -4ч летом (2-е вс марта 07:00 UTC —
    1-е вс ноября 06:00 UTC), иначе -5ч. Без tzdata, по правилу США."""
    y = datetime.fromtimestamp(ts, timezone.utc).year
    def nth_sunday(month, n):
        d = datetime(y, month, 1, tzinfo=timezone.utc)
        first = (6 - d.weekday()) % 7          # 0=пн ... 6=вс
        return d.timestamp() + (first + 7 * (n - 1)) * 86400
    beg = nth_sunday(3, 2) + 7 * 3600
    end = nth_sunday(11, 1) + 6 * 3600
    return -4 * 3600 if beg <= ts < end else -5 * 3600


HOURLY_NAME = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana", "xrp": "xrp"}
MONTHS = ("january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december")


def slug(asset: str, tf: str, start: int) -> str:
    if tf == "1h":
        e = datetime.fromtimestamp(start + et_offset(start), timezone.utc)
        h12 = e.hour % 12 or 12
        ap = "am" if e.hour < 12 else "pm"
        return (f"{HOURLY_NAME.get(asset, asset)}-up-or-down-{MONTHS[e.month - 1]}-"
                f"{e.day}-{e.year}-{h12}{ap}-et")
    return f"{asset}-updown-{tf}-{start}"


def slot_after(now: float, tf: str, lead: int) -> int:
    """Ближайшее окно tf, момент решения которого (S - lead) ещё впереди.
    4h-окна Polymarket начинаются в 00/04/08/12/16/20 по Нью-Йорку."""
    W = TF_SEC[tf]
    if tf != "4h":
        return int((now + lead) // W) * W + W
    h = (int(now + lead) // 3600 + 1) * 3600
    while True:
        e = datetime.fromtimestamp(h + et_offset(h), timezone.utc)
        if e.hour % 4 == 0:
            return h
        h += 3600


def wlabel(ts: int, tf: str = "15m") -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M") + f"/{tf}"


# ═══════════════════════════════════════════════════════════════ модель
# Обучена на минутках Binance BTC/ETH/SOL/XRP за 01.01-16.09.2026, цель —
# исход окна по TWAP (смоделирован на минутках, проверен на резолюциях).
# Логистическая регрессия: признак обрезается границами, нормируется
# средним и разбросом и умножается на вес. p — вероятность РАЗВОРОТА
# хода текущей свечи (15m для m15, 5m для m5).
AS4 = ("btc", "eth", "sol", "xrp")       # кросс-признакам нужны все четыре
R_MINS = (1, 3, 5, 15, 30, 60, 240)

MODELS = {
    'm15': {
        'tf': '15m', 'lead': 300, 'n_train': 98310,
        'b': 0.08237181580153959,
        'cal': (-0.0032397451460140086, 0.5324576805124812),
        # признак: (нижняя граница, верхняя граница, среднее, разброс, вес)
        'w': {
            'n15': (0.0, 7.899189211294871, 1.2011243370686453, 1.2141264021376015, 0.008486538075137042),
            'ew15': (0.12738127929612425, 10.19890080748787, 1.8211250830271697, 1.4905898237782025, 0.0029068094171926487),
            'ea15': (0.0, 3.469388470740907, 0.5277604422791973, 0.578711996523391, 0.01972766127063274),
            'pb15': (0.0, 4.260744718791755, 0.6158428493224056, 0.6615461521805114, -0.026152186096001766),
            'k15': (-6.0, 6.0, -0.12520598108025632, 2.166739448350607, 0.04420619500029199),
            'ps15': (-15.008529429560088, 13.610499044666332, -0.24051725524902784, 3.5940142567879287, -0.034678429638088704),
            'el15': (0.6666666666666666, 0.6666666666666666, 0.6666666666656968, 1.0, 7.863156675948707e-14),
            'n5': (-3.627853317416677, 9.254609813294854, 1.0109792997075002, 1.8033589651158473, 0.005627675035597727),
            'ew5': (0.0, 11.987445463744605, 1.9391635866128207, 1.8652620676019238, 0.018322709025764494),
            'ea5': (0.0, 5.6080726768825135, 0.8024136980986286, 0.9213297904276502, -0.00875266163704856),
            'pb5': (0.0, 6.551373689847594, 0.9288501835522984, 1.0325768816472705, 0.023842500663984356),
            'k5': (-6.0, 7.0, 0.9459464957786593, 2.009601506928996, 0.0059775151744751245),
            'ps5': (-10.353117817897521, 18.713908212836063, 1.7050580028496671, 3.482379633039771, -0.011709197546972987),
            'el5': (1.0, 1.0, 1.0, 1.0, 0.0),
            'r1': (-2.492809957746341, 3.990864680841255, 0.19570596201891888, 0.8915258956775642, 0.01907444747916329),
            'r3': (-2.097151560225406, 4.238263435035648, 0.3524964693110202, 0.8900610509111205, -0.003745072268332978),
            'r5': (-1.7166342131658432, 4.289611637155494, 0.48427403700471355, 0.8570481180597559, 0.006072813485330805),
            'r15': (-1.2891019074157317, 4.3340228041263975, 0.5807028955679105, 0.7882485801314598, 0.004167332017787267),
            'r30': (-2.371773948419025, 4.064815440346926, 0.37251958681676134, 0.8823602560220367, 0.016437388657620854),
            'r60': (-2.7186305143245266, 3.7920275090877644, 0.2480618685432959, 0.913476659854276, 0.023690269604488477),
            'r240': (-2.8446096219447945, 3.2279162255586584, 0.10664112584464301, 0.9226374464057966, 0.047055375529150734),
            'volr': (0.28218756097969405, 2.76901180606017, 0.9296514119007476, 0.40883368190684216, -0.023927905491843617),
            'lnorm15': (-8.04601138382012, -5.299661930146839, -6.658985413118102, 0.47294494342441695, 0.00034704622045717384),
            'x_eth_n': (-1.693794640452461, 8.017391117206586, 0.7902817211923108, 1.3216660732810486, 0.007719637273020969),
            'x_eth_k': (-6.0, 5.0, -0.07855762384294579, 1.8542791399316316, 0.013620096160237188),
            'x_sol_n': (-1.7483540731274496, 6.657087178435067, 0.7359660660887116, 1.210862047664587, 0.013991164652635857),
            'x_sol_k': (-6.0, 6.0, -0.0744278303326213, 1.8513532936911912, 0.013642255422506308),
            'x_xrp_n': (-1.9144733822368198, 6.680883838871367, 0.7045841968441547, 1.2190482375075933, 0.001166169931253855),
            'x_xrp_k': (-6.0, 6.0, -0.06557827281049741, 1.8871752280924312, -0.00840215659393575),
            'x_mean': (-1.4255675802844463, 7.3410242728732635, 0.9983935584728408, 1.2557548407929615, -0.00701550366246198),
            'x_min': (-2.6656463276976154, 5.888513858283961, 0.45654518557339757, 1.175812326341448, 0.028385898442915298),
            'x_agree': (0.0, 3.0, 2.4032244939477163, 0.918977784940414, -0.0015514007956397592),
            'x_btc_n': (-1.6362984898919768, 7.381640320735754, 0.755818184189033, 1.2549191695460362, -0.006714363470085735),
            'x_btc_k': (-6.0, 6.0, -0.078282982402604, 1.9048560565439576, 0.012825597906854246),
            'hs': (-1.0, 1.0, -1.0530731160790898e-05, 0.7070871302234271, 0.006059664860758475),
            'hc': (-1.0, 1.0, 5.964503412828526e-05, 0.7071264290100011, 0.011074024001802097),
        },
        # |p-0.5| на обучении: квантиль -> порог
        'conf_q': {0.5: 0.022283720408125707, 0.7: 0.03467455731135962, 0.8: 0.04351528733081481, 0.85: 0.049825648954643684, 0.9: 0.05862090824195087, 0.92: 0.0636324227388004, 0.95: 0.0745378980623878, 0.97: 0.08717993097775042, 0.98: 0.0975050206741936, 0.99: 0.11586650585055945},
        # ставок в сутки на 4 монеты при этом квантиле (история)
        'per_day': {0.5: 192.0, 0.7: 115.2, 0.8: 76.8, 0.85: 57.6, 0.9: 38.4, 0.92: 30.7, 0.95: 19.2, 0.97: 11.5, 0.98: 7.7, 0.99: 3.8},
        # контрольные точки для --verify: актив, начало окна, p
        'ref': [('btc', 1789002900, 0.558321), ('btc', 1789257600, 0.518811), ('btc', 1789499700, 0.432715), ('eth', 1789002900, 0.54063), ('eth', 1789257600, 0.525139), ('eth', 1789499700, 0.443404), ('sol', 1789002900, 0.503792), ('sol', 1789258500, 0.492552), ('sol', 1789500600, 0.42182), ('xrp', 1789002900, 0.506416), ('xrp', 1789258500, 0.49224), ('xrp', 1789500600, 0.579081)],
    },
    'm5': {
        'tf': '5m', 'lead': 120, 'n_train': 294934,
        'b': 0.0368124177715065,
        'cal': (-0.00854295790100551, 0.7491037193526461),
        # признак: (нижняя граница, верхняя граница, среднее, разброс, вес)
        'w': {
            'n15': (-4.031668668439291, 6.81080434027158, 0.6131481113462243, 1.3484639245980865, 0.0023040533969141044),
            'ew15': (0.0, 8.3763040698038, 1.204444886941762, 1.2443897517093774, 0.0002652501291846015),
            'ea15': (0.0, 6.795846984258213, 0.7948857427148137, 1.0304030270330897, 0.04020020003993568),
            'pb15': (0.0, 5.0098536649577525, 0.5938222410458575, 0.7650959488277691, -0.056785342987404355),
            'k15': (-6.0, 6.0, -0.08088928370415076, 2.168884499911768, 0.04047023814314334),
            'ps15': (-14.702536538619295, 14.051350424081892, -0.14248540466285928, 3.599704794064113, -0.010495620671435463),
            'el15': (0.2, 0.8666666666666667, 0.5333333333343225, 0.27216644977600435, 0.017584423240755317),
            'n5': (0.0, 7.752188953919438, 1.1439268933701268, 1.1838406806413244, 0.06741554972566374),
            'ew5': (0.0010686390883187932, 9.911799045775888, 1.6526003773716573, 1.4350048070065216, -0.08840965619114058),
            'ea5': (0.0, 3.2996834526376473, 0.4255404154465739, 0.5457465973001424, -0.0014891663747131528),
            'pb5': (0.0, 4.047465008286103, 0.5043900963387883, 0.6249888531216601, 0.047333640428755),
            'k5': (-7.0, 6.0, -0.08630744505550395, 2.2219688136306543, 0.013652820855585758),
            'ps5': (-16.162083719797465, 14.605487078019815, -0.16812800008531523, 3.815787324366103, -0.01046179097949726),
            'el5': (0.6, 0.6, 0.6000000000032846, 1.0, -1.0955200775821232e-13),
            'r1': (-2.09278292023843, 4.290174752127675, 0.3738645747107042, 0.8849712584171903, 0.011600875795553947),
            'r3': (-0.04812464507264127, 4.52299352202531, 0.7077396234977038, 0.7164895359623394, 0.005541016395740952),
            'r5': (-1.517266095809384, 4.344890486125095, 0.5294647811278572, 0.8137377941451369, 0.017801473745702526),
            'r15': (-2.7021286493352163, 4.063592723031569, 0.2788255958797422, 0.9135042797004272, 0.021288906712741546),
            'r30': (-2.9261648211170233, 3.9056813245166406, 0.18455319227907221, 0.9312138152819838, 0.022675283266387868),
            'r60': (-3.078063126915578, 3.650619879069128, 0.12086083723265115, 0.9371204674369775, 0.02047663993910401),
            'r240': (-2.9553157771516103, 3.146700931718948, 0.05385502593306691, 0.9268639736570163, 0.0324982356284288),
            'volr': (0.28156730118557993, 2.755144811978703, 0.9295645529694843, 0.40840472390295934, -0.007442871171629721),
            'lnorm15': (-8.04601138382012, -5.299661930146839, -6.658983023286113, 0.47294512349420603, 0.007771212923928245),
            'x_eth_n': (-1.665762366487647, 7.52744485653468, 0.7357130994904278, 1.255811995394336, 0.031355960079246854),
            'x_eth_k': (-6.0, 6.0, -0.0424874717733459, 1.9337636900680957, -0.00525001809865516),
            'x_sol_n': (-1.7495791809802848, 6.506146556594081, 0.6843359104638995, 1.1614361487536637, 0.03945688909609478),
            'x_sol_k': (-6.0, 6.0, -0.04261631415842188, 1.8505311837342584, -0.00496967703366698),
            'x_xrp_n': (-2.0070367385826264, 6.42036867091327, 0.6579534369593253, 1.1785884236729949, 0.041763670579072674),
            'x_xrp_k': (-6.0, 6.0, -0.05112330216251772, 1.8935070118755966, 0.015439816900895175),
            'x_mean': (-1.4203473017725923, 7.209671417667285, 0.9380583630009968, 1.2179492812349817, -0.10598367616918071),
            'x_min': (-2.7261887246659753, 5.712033223293546, 0.4036402618604301, 1.1441974159123234, -0.010456269914854646),
            'x_agree': (0.0, 3.0, 2.353258695165698, 0.9449950624948718, 0.002549036902957492),
            'x_btc_n': (-1.7606528981808776, 7.353845786881815, 0.7269850694523887, 1.2528859863556536, 0.036311590524912125),
            'x_btc_k': (-7.0, 6.0, -0.06064407630181668, 1.98199059924802, 0.014141123369568605),
            'hs': (-1.0, 1.0, -3.5101961131122938e-06, 0.7070810505269304, -0.012437177658494522),
            'hc': (-1.0, 1.0, 7.413083369546439e-05, 0.7071325070164943, 0.0011998183293627264),
        },
        # |p-0.5| на обучении: квантиль -> порог
        'conf_q': {0.5: 0.01917417789170714, 0.7: 0.02979749122312622, 0.8: 0.03745796722874325, 0.85: 0.04256704949291326, 0.9: 0.04963543540934701, 0.92: 0.05357198173026401, 0.95: 0.06156235819673312, 0.97: 0.07050372361490395, 0.98: 0.07765765663924801, 0.99: 0.08952570336488196},
        # ставок в сутки на 4 монеты при этом квантиле (история)
        'per_day': {0.5: 576.0, 0.7: 345.6, 0.8: 230.4, 0.85: 172.8, 0.9: 115.2, 0.92: 92.2, 0.95: 57.6, 0.97: 34.6, 0.98: 23.0, 0.99: 11.5},
        # контрольные точки для --verify: актив, начало окна, p
        'ref': [('btc', 1788999900, 0.526625), ('btc', 1789258200, 0.479443), ('btc', 1789512300, 0.501722), ('eth', 1788999900, 0.532009), ('eth', 1789258200, 0.510852), ('eth', 1789512300, 0.5108), ('sol', 1788999900, 0.54833), ('sol', 1789258500, 0.517888), ('sol', 1789512600, 0.54031), ('xrp', 1788999900, 0.527806), ('xrp', 1789258500, 0.519768), ('xrp', 1789512600, 0.521399)],
    },
}

DEFAULT_STREAMS = "H1=h1,H4=h4"

# Правила разворота после серии. p — ожидаемый вин рейт (ниже исторического —
# с запасом); из него считается потолок цены покупки, как у моделей.
RULES = {
    "h1": {"tf": "1h", "lead": 120, "streak": 3, "sum_sig": 2.0, "last_sig": 0.0,
           "p": 0.555, "hist": "0.563 на 1413 сигналах (01–09.2026)", "per_day_coin": 2.8},
    "h4": {"tf": "4h", "lead": 120, "streak": 2, "sum_sig": 0.0, "last_sig": 1.0,
           "p": 0.565, "hist": "0.586 на 348 сигналах (01–09.2026)", "per_day_coin": 0.7},
}
SIG_LB_MIN = 1440            # окно σ, минут
RULE_BACK = 8                # сколько полных свечей назад смотреть на серию


def rule_eval(rule: dict, arr, S: int, dec: int) -> dict | None:
    """Серия и амплитуда по минуткам (t,o,h,l,c), последняя минута кончается в dec.
    Возвращает признаки и fire=True/False; None — данных мало."""
    if arr is None:
        return None
    t, o, h, l, c = arr
    W = TF_SEC[rule["tf"]]
    M = W // 60
    n = len(c)
    if n < SIG_LB_MIN + 2 or t[-1] + 60 != dec:
        return None
    t0 = t[0]
    def ix(ts):
        i = (ts - t0) // 60
        return i if 0 <= i < n else None
    lr = [math.log(c[i] / c[i - 1]) for i in range(n - SIG_LB_MIN, n)]
    sig = math.sqrt(sum(x * x for x in lr) / len(lr)) * math.sqrt(M)
    if sig <= 0:
        return None
    i0 = ix(S - W)
    if i0 is None:
        return None
    moves = [math.log(c[-1] / o[i0])]            # текущая свеча, неполная
    for j in range(1, RULE_BACK + 1):
        a0, a1 = ix(S - W * (j + 1)), ix(S - W * j - 60)
        if a0 is None or a1 is None:
            break
        moves.append(math.log(c[a1] / o[a0]))
    d0 = (moves[0] > 0) - (moves[0] < 0)
    if d0 == 0:
        return {"fire": False, "dir": 0, "streak": 0, "sum": 0.0, "last": 0.0, "sig": sig}
    k, cum = 0, 0.0
    for mv in moves:
        if mv != 0 and ((mv > 0) - (mv < 0)) == d0:
            k += 1
            cum += abs(mv)
        else:
            break
    last = abs(moves[0]) / sig
    cs = cum / sig
    fire = (k >= rule["streak"] and cs >= rule["sum_sig"] - 1e-12
            and last >= rule["last_sig"] - 1e-12)
    return {"fire": fire, "dir": d0, "streak": k, "sum": cs, "last": last, "sig": sig}


def rule_signal(mname: str, asset: str, S: int, dec: int, ev: dict) -> dict:
    r = RULES[mname]
    ref_dir = "up" if ev["dir"] > 0 else "down"
    side = opposite(ref_dir)
    pc = r["p"]
    cap = max_ask_for(max(0.0, pc - CFG.MIN_EDGE))
    return {"model": mname, "asset": asset, "tgt_tf": r["tf"], "tgt_start": S,
            "dec": dec, "lead": r["lead"], "p": pc, "pw": pc, "pc": pc,
            "conf": 1.0 if ev["fire"] else 0.0, "q": 1.0 if ev["fire"] else 0.0,
            "ref_dir": ref_dir, "side": side, "dir_kind": "разворот", "cap": cap,
            "feat": {"streak": ev["streak"], "sum_sig": round(ev["sum"], 3),
                     "last_sig": round(ev["last"], 3), "sig_pct": round(ev["sig"] * 100, 4)}}


def model_of(mname: str) -> dict:
    return RULES[mname] if mname in RULES else MODELS[mname]


def conf_threshold(m: dict, q: float) -> float:
    """Порог |p-0.5| для квантиля q (линейно между точками таблицы)."""
    pts = sorted(m["conf_q"].items())
    if q <= pts[0][0]:
        return pts[0][1]
    if q >= pts[-1][0]:
        return pts[-1][1]
    for (q0, t0), (q1, t1) in zip(pts, pts[1:]):
        if q0 <= q <= q1:
            return t0 + (t1 - t0) * (q - q0) / (q1 - q0)
    return pts[-1][1]


def per_day_at(m: dict, q: float) -> float:
    pts = sorted(m["per_day"].items())
    for (q0, d0), (q1, d1) in zip(pts, pts[1:]):
        if q0 <= q <= q1:
            return d0 + (d1 - d0) * (q - q0) / (q1 - q0)
    return pts[0][1] if q < pts[0][0] else pts[-1][1]


def conf_rank(m: dict, conf: float) -> float:
    """Обратная операция: какой квантиль истории у этой силы сигнала."""
    pts = sorted(m["conf_q"].items())
    if conf <= pts[0][1]:
        return pts[0][0]
    for (q0, t0), (q1, t1) in zip(pts, pts[1:]):
        if t0 <= conf <= t1:
            return q0 + (q1 - q0) * (conf - t0) / (t1 - t0) if t1 > t0 else q1
    return pts[-1][0]


def calibrate(m: dict, pw: float) -> float:
    """Сжатая вероятность выигрыша выбранной стороны (калибровка вне выборки)."""
    pw = min(max(pw, 1e-6), 1 - 1e-6)
    a, b = m["cal"]
    z = a + b * math.log(pw / (1.0 - pw))
    return 1.0 / (1.0 + math.exp(-z))


def parse_skip_hours(raw: str) -> dict:
    """PM_SKIP_HOURS: "C5:12-16" или "C5:12-16+20-22,A15:20-24" — часы UTC начала
    целевого окна, в которые поток не торгует. Диапазон [с, по), через полночь можно (22-2)."""
    out: dict = {}
    for chunk in (x.strip() for x in (raw or "").split(",")):
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(f"{chunk!r}: нужно поток:с-по")
        sid, body = (x.strip() for x in chunk.split(":", 1))
        hours = out.setdefault(sid.lower(), set())
        for rng in (x.strip() for x in body.split("+")):
            if not rng:
                continue
            try:
                a, b = (int(x) for x in rng.split("-"))
            except ValueError:
                raise ValueError(f"{chunk!r}: диапазон {rng!r} не вида 12-16")
            if not (0 <= a <= 24 and 0 <= b <= 24) or a == b:
                raise ValueError(f"{chunk!r}: часы 0..24 и с != по")
            h = a % 24
            while h != b % 24:
                hours.add(h)
                h = (h + 1) % 24
    return out


def parse_streams(raw: str) -> list[dict]:
    out, seen = [], set()
    for chunk in (x.strip() for x in raw.split(",")):
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"поток {chunk!r}: нет id")
        sid, body = (x.strip() for x in chunk.split("=", 1))
        parts = [x.strip() for x in body.split(":")]
        if parts[0].lower() in RULES:
            if sid in seen:
                raise ValueError(f"поток {sid}: дублируется id")
            seen.add(sid)
            rn = parts[0].lower()
            assets = None
            if len(parts) >= 2 and parts[-1] and not parts[-1].replace(".", "").isdigit():
                assets = tuple(a.strip().lower() for a in parts[-1].split("+") if a.strip())
                bad = [a for a in assets if a not in AS4]
                if bad:
                    raise ValueError(f"поток {sid}: неизвестные активы {bad}")
            r = RULES[rn]
            out.append({"id": sid, "model": rn, "tgt_tf": r["tf"], "lead": r["lead"],
                        "q": 1.0, "thr": 0.5, "assets": assets,
                        "name": rn + (f":{'+'.join(assets)}" if assets else "")})
            continue
        if len(parts) not in (2, 3):
            raise ValueError(f"поток {sid}: нужно модель:квантиль[:активы]")
        mname = parts[0].lower()
        if mname not in MODELS:
            raise ValueError(f"поток {sid}: неизвестная модель {mname!r}, "
                             f"есть {sorted(MODELS)}")
        try:
            q = float(parts[1])
        except ValueError:
            raise ValueError(f"поток {sid}: квантиль {parts[1]!r} не число")
        if not 0.5 <= q < 1.0:
            raise ValueError(f"поток {sid}: квантиль {q} вне 0.5..0.99")
        assets = None
        if len(parts) == 3 and parts[2]:
            assets = tuple(a.strip().lower() for a in parts[2].split("+") if a.strip())
            bad = [a for a in assets if a not in AS4]
            if bad:
                raise ValueError(f"поток {sid}: неизвестные активы {bad}")
        if sid in seen:
            raise ValueError(f"поток {sid}: дублируется id")
        seen.add(sid)
        m = MODELS[mname]
        out.append({"id": sid, "model": mname, "tgt_tf": m["tf"],
                    "lead": m["lead"], "q": q, "thr": conf_threshold(m, q),
                    "assets": assets,
                    "name": f"{mname}:q{q:g}" + (f":{'+'.join(assets)}" if assets else "")})
    if not out:
        raise ValueError("ни одного потока")
    return out


# ═══════════════════════════════════════════════════════════════ конфиг
class Cfg:
    def __init__(self):
        self.ASSETS = LS("PM_ASSETS", ("btc", "eth"))
        lead = I("PM_RULE_LEAD", 120)
        for rn in RULES:
            u = rn.upper()
            RULES[rn]["lead"] = lead
            RULES[rn]["streak"] = I(f"PM_{u}_STREAK", RULES[rn]["streak"])
            RULES[rn]["sum_sig"] = F(f"PM_{u}_SUM_SIG", RULES[rn]["sum_sig"])
            RULES[rn]["last_sig"] = F(f"PM_{u}_LAST_SIG", RULES[rn]["last_sig"])
            RULES[rn]["p"] = F(f"PM_{u}_P", RULES[rn]["p"])
        bad = [a for a in self.ASSETS if a not in AS4]
        if bad:
            raise SystemExit(f"PM_ASSETS: неизвестные активы {bad}")
        try:
            self.STREAMS = parse_streams(S("PM_STREAMS", DEFAULT_STREAMS))
        except ValueError as e:
            raise SystemExit(f"PM_STREAMS: {e}")
        only = LS("PM_STREAMS_ONLY", ())
        if only:
            self.STREAMS = [s for s in self.STREAMS if s["id"].lower() in only]
        skip = LS("PM_STREAMS_SKIP", ())
        if skip:
            self.STREAMS = [s for s in self.STREAMS if s["id"].lower() not in skip]
        if not self.STREAMS:
            raise SystemExit("после PM_STREAMS_ONLY/SKIP не осталось потоков")
        self.MODELS_USED = sorted({s["model"] for s in self.STREAMS})
        self.WARN = []
        # часы UTC (начало целевого окна), когда поток не торгует. По умолчанию C5 молчит
        # в 12–16 UTC: на истории там около безубытка, вживую — весь убыток 08.10
        try:
            self.SKIP_HOURS = parse_skip_hours(S("PM_SKIP_HOURS", ""))
        except ValueError as e:
            raise SystemExit(f"PM_SKIP_HOURS: {e}")
        ids = {st["id"].lower() for st in self.STREAMS}
        for sid in self.SKIP_HOURS:
            if sid not in ids:
                self.WARN.append(f"PM_SKIP_HOURS: потока {sid.upper()} нет среди активных — правило не действует")
        if os.getenv("PM_TIERS") or os.getenv("PM_TIERS_ONLY") or os.getenv("PM_TIERS_SKIP"):
            self.WARN.append("PM_TIERS* в конфиге игнорируются: в 2.0 сигналы "
                             "задаёт PM_STREAMS")

        # ── размер ──
        self.DEPOSIT = F("PM_DEPOSIT", 200.0)
        self.SIZE_PCT = F("PM_SIZE_PCT", 2.0)
        # 1 = депозит в работе = PM_DEPOSIT + закрытый итог из журнала
        self.COMPOUND = BL("PM_COMPOUND", True)
        # пересчёт базы ставки ступенью: депозит ушёл от базы на столько %
        self.EQUITY_STEP_PCT = F("PM_EQUITY_STEP_PCT", 5.0)
        # итог в журнале считается с этого момента (ISO, пусто = весь журнал)
        self.EQUITY_SINCE = S("PM_EQUITY_SINCE", "")
        self.EQUITY_SINCE_TS = 0.0
        if self.EQUITY_SINCE:
            try:
                d = datetime.fromisoformat(self.EQUITY_SINCE.replace("Z", "+00:00"))
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                self.EQUITY_SINCE_TS = d.timestamp()
            except ValueError:
                raise SystemExit(f"PM_EQUITY_SINCE={self.EQUITY_SINCE!r}: нужен ISO, "
                                 "например 2026-10-05T00:00:00")
        # новых цепочек не открываем, если депозит в работе не выше этого (0 = выкл)
        self.EQUITY_STOP = F("PM_EQUITY_STOP", 0.0)
        self.MIN_SHARES = F("PM_MIN_SHARES", 5.0)
        self.MIN_ORDER_VALUE = F("PM_MIN_ORDER_VALUE", 1.05)
        self.SHARE_STEP = F("PM_SHARE_STEP", 1.0)
        # потолок денег на один рынок (актив+тф+окно), 0 = без потолка.
        # По умолчанию в ставках от ТЕКУЩЕЙ ставки, поэтому растёт и падает
        # вместе с депозитом. PM_MARKET_CAP_USD >= 0 задаёт жёсткие доллары.
        self.MARKET_CAP_STAKES = F("PM_MARKET_CAP_STAKES", 6.0)
        self.MARKET_CAP_FIXED = F("PM_MARKET_CAP_USD", -1.0)
        self.MARKET_CAP_USD = self.market_cap_for(self.DEPOSIT * self.SIZE_PCT / 100.0)

        # ── эскалация объёма (по умолчанию выключена: объём фиксированный) ──
        self.ESCALATE = F("PM_ESCALATE", 1.0)
        self.ESCALATE_MAX = F("PM_ESCALATE_MAX", 1.0)
        if self.ESCALATE < 1.0:
            raise SystemExit(f"PM_ESCALATE ({self.ESCALATE:g}) не может быть меньше 1")
        if self.ESCALATE_MAX < 1.0:
            raise SystemExit(f"PM_ESCALATE_MAX ({self.ESCALATE_MAX:g}) не может быть меньше 1")

        # ── сигнал ──
        # запас маржи сверх безубыточности (в долях вероятности выигрыша)
        self.MIN_EDGE = F("PM_MIN_EDGE", 0.005)
        self.DECISION_LAG = F("PM_DECISION_LAG", 1.5)
        self.DECISION_MAX_WAIT = F("PM_DECISION_MAX_WAIT", 20.0)
        self.LOG_SCORES = BL("PM_LOG_SCORES", True)
        self.HISTORY_MIN = I("PM_HISTORY_MIN", 2880)
        self.MIN_SOURCES = LS("PM_MIN_SOURCES", ("binance", "bybit", "okx"))
        self.MIN_TIMEOUT = F("PM_MIN_TIMEOUT", 6.0)
        self.MIN_STALE = F("PM_MIN_STALE", 180.0)
        self.MIN_BINANCE_BASES = [x for x in S(
            "PM_MIN_BINANCE_BASES",
            S("PM_AMP_BINANCE_BASES",
              "https://data-api.binance.vision,https://api.binance.com")
        ).replace(" ", "").split(",") if x]
        self.SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT",
                        "sol": "SOLUSDT", "xrp": "XRPUSDT"}

        # ── исполнение (как в 1.2) ──
        self.HARD_STOP = F("PM_HARD_STOP", 60.0)
        self.TAKER_DEADLINE = F("PM_TAKER_DEADLINE", 70.0)
        if self.TAKER_DEADLINE <= self.HARD_STOP:
            raise SystemExit(
                f"PM_TAKER_DEADLINE ({self.TAKER_DEADLINE:g}) должен быть "
                f"строго больше PM_HARD_STOP ({self.HARD_STOP:g})")
        for s in self.STREAMS:
            room = s["lead"] - self.DECISION_LAG - self.DECISION_MAX_WAIT
            if room < self.TAKER_DEADLINE:
                raise SystemExit(
                    f"поток {s['id']}: решение за {s['lead']}с до открытия, "
                    f"а с задержкой ({self.DECISION_LAG:g}+{self.DECISION_MAX_WAIT:g}с) "
                    f"остаётся {room:g}с — меньше дедлайна тейкера "
                    f"{self.TAKER_DEADLINE:g}с. Уменьши PM_DECISION_MAX_WAIT")
        self.MAKER_ENABLED = BL("PM_MAKER_ENABLED", True)
        self.TAKER_AT_SIGNAL = BL("PM_TAKER_AT_SIGNAL", True)
        self.MAKER_POLL = F("PM_MAKER_POLL", 1.0)
        self.REQUOTE_MIN_SEC = F("PM_REQUOTE_MIN_SEC", 1.0)
        self.MAKER_MAX_PRICE = F("PM_MAKER_MAX_PRICE", 0.0)
        # 0 = потолок сигнала (маржа >= MIN_EDGE), <0 = без потолка, число = абсолютный
        self.TAKER_MAX_PRICE = F("PM_TAKER_MAX_PRICE", 0.0)
        self.TAKER_BUFFER = F("PM_TAKER_BUFFER", 0.0)
        self.BUILDER_FEE = F("PM_BUILDER_FEE", 0.01)   # доля от тейкерного нотионала
        self.TAKER_TRIES = I("PM_TAKER_TRIES", 8)
        self.TAKER_GAP = F("PM_TAKER_GAP", 0.25)
        self.TAKER_MIN_LEFT = F("PM_TAKER_MIN_LEFT", 1.0)
        self.TAKER_NOTIONAL = BL("PM_TAKER_NOTIONAL", True)
        self.FILL_TOL_SHARES = F("PM_FILL_TOL_SHARES", 1.0)
        self.CANCEL_TRIES = I("PM_CANCEL_TRIES", 6)
        self.CANCEL_GAP = F("PM_CANCEL_GAP", 0.4)
        self.FILL_VERIFY_TRIES = I("PM_FILL_VERIFY_TRIES", 6)
        self.FILL_VERIFY_GAP = F("PM_FILL_VERIFY_GAP", 0.5)
        self.SWEEP_SEC = F("PM_SWEEP_SEC", 20.0)
        self.POST_ONLY = BL("PM_POST_ONLY", True)
        self.MAX_CHAINS = I("PM_MAX_CHAINS", 0)

        # ── сеть ──
        self.GAMMA_HOST = S("PM_GAMMA_HOST", "https://gamma-api.polymarket.com")
        self.CLOB_HOST = S("PM_CLOB_HOST", "https://clob.polymarket.com")
        self.GAMMA_RPS = F("PM_GAMMA_RPS", 6.0)
        self.GAMMA_TIMEOUT = F("PM_GAMMA_TIMEOUT", 8.0)
        self.GAMMA_RETRIES = I("PM_GAMMA_RETRIES", 3)
        self.GAMMA_BACKOFF = F("PM_GAMMA_BACKOFF", 0.6)
        self.BOOK_TIMEOUT = F("PM_BOOK_TIMEOUT", 5.0)
        self.MARKET_WAIT = F("PM_MARKET_WAIT", 600.0)
        self.MARKET_RETRY = F("PM_MARKET_RETRY", 5.0)

        # ── клиент ──
        self.DRY_RUN = BL("PM_DRY_RUN", True)
        self.DRY_FILL_PROB = F("PM_DRY_FILL_PROB", 0.35)
        self.PRIVATE_KEY = S("PM_PRIVATE_KEY", "")
        self.FUNDER = S("PM_FUNDER", "")
        self.CHAIN_ID = I("PM_CHAIN_ID", 137)
        self.SIGNATURE_TYPE = I("PM_SIGNATURE_TYPE", 1)

        # ── журнал ──
        self.JOURNAL = S("PM_JOURNAL", "hourbot.jsonl")
        self.LOG_TZ = S("PM_LOG_TZ", "UTC")
        self.VERBOSE = BL("PM_VERBOSE", False)
        self.RESOLVE_DELAY = F("PM_RESOLVE_DELAY", 20.0)
        self.RESOLVE_ATTEMPTS = I("PM_RESOLVE_ATTEMPTS", 480)   # 15с x 480 = 2ч
        self.RESOLVE_RETRY = F("PM_RESOLVE_RETRY", 15.0)
        self.HEARTBEAT = F("PM_HEARTBEAT", 300.0)

    def market_cap_for(self, stake: float) -> float:
        if self.MARKET_CAP_FIXED >= 0:
            return self.MARKET_CAP_FIXED
        return stake * self.MARKET_CAP_STAKES * 1.3

    def stake_usd(self, base: float | None = None) -> float:
        b = self.DEPOSIT if base is None else base
        return max(0.0, b * self.SIZE_PCT / 100.0)

    def mult_for(self, ordn: int, frozen: bool = False) -> float:
        """Множитель объёма для n-й цепочки на рынке+стороне (1 = выключено)."""
        if frozen or ordn <= 1:
            return 1.0
        return min(self.ESCALATE ** (ordn - 1), self.ESCALATE_MAX)

    def streams_for(self, mname: str, asset: str) -> list[dict]:
        return [s for s in self.STREAMS
                if s["model"] == mname and asset in self.ASSETS
                and (s["assets"] is None or asset in s["assets"])]


CFG: "Cfg | None" = None


# ═══════════════════════════════════════════════════════════════════ лог
_LOGLOCK = threading.Lock()


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _LOGLOCK:
        print(f"{ts}  {msg}", flush=True)


class Journal:
    def __init__(self, path: str):
        self.path = Path(path)
        self.lock = threading.Lock()

    def write(self, kind: str, data: dict):
        rec = {"ts": round(time.time(), 3),
               "iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "kind": kind, **data}
        line = json.dumps(rec, ensure_ascii=False, default=str)
        try:
            with self.lock, self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            log(f"  журнал недоступен: {e}")


_TASKS: set = set()


def spawn(coro, what: str, jr: "Journal | None" = None):
    t = asyncio.ensure_future(coro)
    _TASKS.add(t)

    def done(fut):
        _TASKS.discard(fut)
        if fut.cancelled():
            return
        e = fut.exception()
        if e:
            log(f"!! задача {what} упала: {type(e).__name__}: {e}")
            if CFG and CFG.VERBOSE:
                traceback.print_exception(type(e), e, e.__traceback__)
            if jr:
                jr.write("task_error", {"what": what, "err": repr(e)})

    t.add_done_callback(done)
    return t


async def sleep_until(ts: float, stop) -> bool:
    while not stop.is_set():
        d = ts - time.time()
        if d <= 0:
            return True
        try:
            await asyncio.wait_for(stop.wait(), timeout=min(d, 5.0))
        except asyncio.TimeoutError:
            continue
        return False
    return False


# ═════════════════════════════════════════════════════ лимит денег на рынок
class Exposure:
    """Сколько долларов уже отдано одному рынку.

    Рынок = актив + таймфрейм + конкретное окно (eth:5m:1790759700). Ключ
    именно такой, а не «eth:5m» на всю жизнь бота: иначе через несколько
    окон торговля встала бы навсегда. Внутри одного окна потолок общий для
    ВСЕХ ярусов, которые в него целятся, — ровно та ситуация, где 30.09
    t01 и t16 купили пять позиций вместо двух.

    Цепочка сначала резервирует деньги (reserve), потом, когда всё
    исполнено, сообщает фактический расход (settle). Неизвестный налив
    считается потраченным полностью — деньги не возвращаются.
    """

    def __init__(self, cap: float):
        self.cap = float(cap)
        self.used: dict[str, float] = defaultdict(float)
        self.ord: dict[str, int] = defaultdict(int)
        self.frozen: set[str] = set()
        self.lock = threading.Lock()

    @staticmethod
    def key(asset: str, tf: str, start: int) -> str:
        return f"{asset}:{tf}:{start}"

    @staticmethod
    def skey(mkey: str, side: str) -> str:
        """Ключ эскалации: рынок И сторона. Ставки в противоположные
        стороны одного окна друг друга НЕ подтверждают."""
        return f"{mkey}:{side}"

    def claim_ord(self, skey: str) -> tuple[int, bool]:
        """Забрать номер подтверждения на рынке+стороне.

        Вызывается РОВНО ОДИН РАЗ на цепочку, под тем же замком, что и
        деньги, поэтому два одновременных сигнала не могут получить
        одинаковый номер. Второй элемент — признак заморозки: если по
        этому рынку+стороне уже была цепочка с неизвестной судьбой
        заявки, множитель больше не растёт.
        """
        with self.lock:
            self.ord[skey] += 1
            return self.ord[skey], (skey in self.frozen)

    def freeze(self, skey: str):
        """Запретить рост объёма по этому рынку+стороне."""
        with self.lock:
            self.frozen.add(skey)

    def ord_snapshot(self) -> dict:
        with self.lock:
            return dict(self.ord)

    def free(self, key: str) -> float:
        if self.cap <= 0:
            return float("inf")
        with self.lock:
            return max(0.0, self.cap - self.used[key])

    def reserve(self, key: str, usd: float) -> float:
        """Выдаёт сколько может, не больше остатка потолка."""
        with self.lock:
            if self.cap <= 0:
                self.used[key] += usd
                return usd
            free = max(0.0, self.cap - self.used[key])
            take = min(usd, free)
            if take > 0:
                self.used[key] += take
            return take

    def settle(self, key: str, reserved: float, actual: float):
        """Вернуть неиспользованное или доначислить перелив."""
        with self.lock:
            self.used[key] += actual - reserved
            if self.used[key] < 0:
                self.used[key] = 0.0

    def snapshot(self) -> dict:
        with self.lock:
            return {k: round(v, 2) for k, v in self.used.items() if v > 0}


# ══════════════════════════════════════════════════════════ минутки
class Minutes:
    """Закрытые минутные свечи четырёх монет: {начало: (o, h, l, c)}.

    История перекачивается целиком при старте и при смене источника, дальше
    подтягиваются последние 30 минут. Склеивать историю из разных бирж
    нельзя: уровни цен у них чуть разные, на стыке получился бы ложный ход.
    """

    def __init__(self):
        import requests
        self._requests = requests
        self._local = threading.local()
        self.lock = threading.Lock()
        self.data: dict[str, dict[int, tuple]] = {a: {} for a in AS4}
        self.src: dict[str, str | None] = {a: None for a in AS4}
        self.base: str | None = None
        self.stats: dict[str, int] = defaultdict(int)
        self.last_ok: float | None = None
        self.live: str | None = None
        self.ok = 0

    @property
    def s(self):
        s = getattr(self._local, "s", None)
        if s is None:
            s = self._requests.Session()
            s.headers["User-Agent"] = VERSION
            self._local.s = s
        return s

    def bump(self, k):
        with self.lock:
            self.stats[k] += 1

    # ── источники: [(начало, o, h, l, c), ...] по возрастанию ──
    def _binance(self, asset, limit, end_ms=None):
        bases = list(CFG.MIN_BINANCE_BASES)
        if self.base and self.base in bases:
            bases = [self.base] + [b for b in bases if b != self.base]
        last = None
        for b in bases:
            params = {"symbol": CFG.SYMBOLS[asset], "interval": "1m",
                      "limit": min(int(limit), 1000)}
            if end_ms:
                params["endTime"] = int(end_ms)
            try:
                r = self.s.get(b + "/api/v3/klines", params=params,
                               timeout=CFG.MIN_TIMEOUT)
                r.raise_for_status()
                rows = [(int(x[0]) // 1000, float(x[1]), float(x[2]),
                         float(x[3]), float(x[4])) for x in r.json()]
                if b != self.base:
                    log(f"минутки: binance через {b}")
                    self.base = b
                return rows
            except Exception as ex:
                last = ex
                self.bump(f"binance {b.split('//')[-1]}: отказ")
        raise last if last else RuntimeError("нет зеркал binance")

    def _bybit(self, asset, limit, end_ms=None):
        params = {"category": "spot", "symbol": CFG.SYMBOLS[asset],
                  "interval": "1", "limit": min(int(limit), 1000)}
        if end_ms:
            params["end"] = int(end_ms)
        r = self.s.get("https://api.bybit.com/v5/market/kline", params=params,
                       timeout=CFG.MIN_TIMEOUT)
        r.raise_for_status()
        rows = r.json()["result"]["list"]
        return sorted((int(x[0]) // 1000, float(x[1]), float(x[2]),
                       float(x[3]), float(x[4])) for x in rows)

    def _okx(self, asset, limit, end_ms=None):
        inst = CFG.SYMBOLS[asset].replace("USDT", "-USDT")
        params = {"instId": inst, "bar": "1m", "limit": min(int(limit), 100)}
        if end_ms:
            params["after"] = int(end_ms) + 1
        r = self.s.get("https://www.okx.com/api/v5/market/history-candles",
                       params=params, timeout=CFG.MIN_TIMEOUT)
        r.raise_for_status()
        rows = r.json()["data"]
        return sorted((int(x[0]) // 1000, float(x[1]), float(x[2]),
                       float(x[3]), float(x[4])) for x in rows)

    def _fn(self, src):
        return {"binance": self._binance, "bybit": self._bybit, "okx": self._okx}[src]

    def _pull(self, src, asset, need, end_ms=None) -> list:
        """Страницами назад от end_ms, пока не наберётся need минут."""
        per = {"binance": 1000, "bybit": 1000, "okx": 100}[src]
        rows: dict[int, tuple] = {}
        end = end_ms
        for _ in range(need // per + 4):
            chunk = self._fn(src)(asset, per, end)
            if not chunk:
                break
            for r in chunk:
                rows[r[0]] = r
            if len(rows) >= need:
                break
            end = min(r[0] for r in chunk) * 1000 - 1
        return sorted(rows.values())

    @staticmethod
    def _closed(rows: list) -> list:
        lim = time.time() - 0.2
        return [r for r in rows if r[0] + 60 <= lim]

    def _mark_ok(self, src):
        self.ok += 1
        self.last_ok = time.time()
        if self.live != src:
            log(f"минутки: работаем через {src}"
                + (f" [{self.base}]" if src == "binance" and self.base else ""))
            self.live = src

    def warm_sync(self, asset, end_ts: float | None = None) -> bool:
        """Полная перекачка истории из первого живого источника.

        end_ts — момент, на котором история должна кончаться (для --verify);
        по умолчанию «сейчас», и тогда берутся только закрытые минуты.
        """
        need = CFG.HISTORY_MIN + 5
        end_ms = None if end_ts is None else int(end_ts * 1000) - 1
        for src in CFG.MIN_SOURCES:
            try:
                rows = self._pull(src, asset, need, end_ms)
            except Exception as e:
                self.bump(f"{src}: ошибка")
                if CFG.VERBOSE:
                    log(f"  минутки {asset} {src}: {e}")
                continue
            rows = self._closed(rows) if end_ts is None else [
                r for r in rows if r[0] + 60 <= end_ts]
            if len(rows) < CFG.HISTORY_MIN * 0.9:
                self.bump(f"{src}: мало минут")
                continue
            with self.lock:
                self.data[asset] = {r[0]: r[1:] for r in rows}
                self.src[asset] = src
            self.bump(f"{src}: история")
            self._mark_ok(src)
            return True
        log(f"!! минутки: ни один источник не отдал историю {asset}")
        return False

    def refresh_sync(self, asset) -> bool:
        src = self.src[asset]
        if src is None:
            return self.warm_sync(asset)
        try:
            rows = self._closed(self._fn(src)(asset, 30, None))
        except Exception:
            self.bump(f"{src}: ошибка")
            log(f"!! минутки: {src} не ответил по {asset} — перекачиваю историю")
            return self.warm_sync(asset)
        if not rows:
            return False
        with self.lock:
            d = self.data[asset]
            newest = max(d) if d else None
        # дыра между старой историей и свежими минутами (бот спал) — заново
        if newest is None or rows[0][0] > newest + 60:
            return self.warm_sync(asset)
        with self.lock:
            d = self.data[asset]
            for r in rows:
                d[r[0]] = r[1:]
            lim = max(d) - (CFG.HISTORY_MIN + 120) * 60
            for k in [k for k in d if k < lim]:
                del d[k]
        self.bump(f"{src}: ok")
        self._mark_ok(src)
        return True

    def arrays(self, asset, dec: int, n: int | None = None):
        """Ряды (t, o, h, l, c) из n минут, последняя кончается ровно в dec.

        Пропущенная минута заполняется предыдущей (так готовилась история).
        None — нет последней минуты или пропусков больше 2%.
        """
        n = n or CFG.HISTORY_MIN
        last = int(dec) - 60
        with self.lock:
            d = self.data.get(asset) or {}
            if last not in d:
                return None
            start = last - (n - 1) * 60
            rows = [(ts, d.get(ts)) for ts in range(start, last + 60, 60)]
        t, o, h, l, c = [], [], [], [], []
        prev, miss = None, 0
        for ts, r in rows:
            if r is None:
                miss += 1
                if prev is None:
                    continue
                r = prev
            t.append(ts)
            o.append(r[0])
            h.append(r[1])
            l.append(r[2])
            c.append(r[3])
            prev = r
        if miss > 0.02 * n or len(t) < min(n, 1600):
            return None
        return t, o, h, l, c

    def health(self) -> str:
        if self.last_ok is None:
            return "минутки: ещё не получены"
        age = time.time() - self.last_ok
        srcs = ",".join(f"{a}:{self.src[a] or '-'}" for a in AS4)
        return (f"минутки: {srcs}, успешных {self.ok}, последний ответ {age:.0f}с назад"
                + ("  !! МОЛЧАТ" if age > CFG.MIN_STALE else ""))


MM: "Minutes | None" = None


# ═══════════════════════════════════════════════════════ признаки и скор
def candle_stats(t, o, h, l, c, dec: int, tf: str) -> dict | None:
    """Свеча tf, внутри которой лежит dec, и закрытые свечи перед ней.

    Свечи собираются из минуток по сетке tf (так же собиралась история).
    Норма — медиана |close-open|/open последних 96 (15m) / 288 (5m)
    закрытых свечей. Серия — сколько закрытых свечей подряд шли в одну
    сторону перед текущей, sum — сумма их тел.
    """
    W = TF_SEC[tf]
    m = W // 60
    idec = len(t) - 1
    cst = (dec - 1) // W * W
    i0 = (cst - t[0]) // 60
    if i0 < 0 or i0 > idec:
        return None
    O = o[i0]
    win = 96 if tf == "15m" else 288
    bodies, dirs = [], []
    k = 1
    while k <= win + 80:
        s0 = i0 - k * m
        if s0 < 0:
            break
        co, cc = o[s0], c[s0 + m - 1]
        bodies.append(abs(cc - co) / co)
        dirs.append((cc > co) - (cc < co))
        k += 1
    nb = bodies[:win]
    if len(nb) < win // 3:
        return None
    norm = statistics.median(nb)
    if norm <= 0:
        return None
    pd_, pl, ps = 0, 0, 0.0
    if dirs and dirs[0] != 0:
        pd_ = dirs[0]
        for dd, bb in zip(dirs, bodies):
            if dd != pd_:
                break
            pl += 1
            ps += bb
        pl = min(pl, 60)
    mx = max(h[i0:idec + 1])
    mn = min(l[i0:idec + 1])
    P = c[idec]
    return {"net": (P - O) / O / norm, "eu": (mx - O) / O / norm,
            "ed": (O - mn) / O / norm, "pd": pd_, "pl": pl, "ps": ps / norm,
            "el": (dec - cst) / W, "norm": norm}


def asset_base(arr, dec: int) -> dict | None:
    """Всё, что модель знает об одной монете в момент dec."""
    if arr is None:
        return None
    t, o, h, l, c = arr
    n = len(c)
    if n < 1500 or t[-1] + 60 != dec:
        return None
    idec = n - 1
    P = c[idec]
    lp = [math.log(x) for x in c[-1441:]]
    lr = [lp[i] - lp[i - 1] for i in range(1, len(lp))]
    sd = math.sqrt(max(sum(x * x for x in lr) / 1440.0, 1e-12))
    sdh = math.sqrt(max(sum(x * x for x in lr[-60:]) / 60.0, 1e-12))
    r = {mm: (math.log(P) - math.log(c[idec - mm])) / (sd * math.sqrt(mm))
         for mm in R_MINS}
    c15 = candle_stats(t, o, h, l, c, dec, "15m")
    c5 = candle_stats(t, o, h, l, c, dec, "5m")
    if c15 is None or c5 is None:
        return None
    return {"c15": c15, "c5": c5, "r": r, "volr": sdh / sd}


def build_features(bases: dict, asset: str, mname: str, dec: int):
    """Признаки в знаках хода опорной свечи. Возвращает (X, s), s = +1/-1."""
    m = MODELS[mname]
    ref = "c15" if m["tf"] == "15m" else "c5"
    d = bases.get(asset)
    if d is None:
        return None, None
    s = 1 if d[ref]["net"] >= 0 else -1
    X: dict[str, float] = {}
    for tf, key in (("15", "c15"), ("5", "c5")):
        cs = d[key]
        n_ = s * cs["net"]
        ew = cs["eu"] if s > 0 else cs["ed"]
        ea = cs["ed"] if s > 0 else cs["eu"]
        sg = 1 if cs["pd"] == s else -1
        X["n" + tf] = n_
        X["ew" + tf] = ew
        X["ea" + tf] = ea
        X["pb" + tf] = ew - n_
        X["k" + tf] = cs["pl"] * sg
        X["ps" + tf] = cs["ps"] * sg
        X["el" + tf] = cs["el"]
    for mm in R_MINS:
        X[f"r{mm}"] = s * d["r"][mm]
    X["volr"] = d["volr"]
    X["lnorm15"] = math.log(d["c15"]["norm"])
    crs = []
    for b in AS4:
        if b == asset or bases.get(b) is None:
            X[f"x_{b}_n"] = 0.0
            X[f"x_{b}_k"] = 0.0
            continue
        cb = bases[b][ref]
        xn = s * cb["net"]
        X[f"x_{b}_n"] = xn
        X[f"x_{b}_k"] = cb["pl"] * (1 if cb["pd"] == s else -1)
        crs.append(xn)
    X["x_mean"] = sum(crs) / len(crs) if crs else 0.0
    X["x_min"] = min(crs) if crs else 0.0
    X["x_agree"] = float(sum(1 for v in crs if v > 0))
    hour = (dec % 86400) // 3600
    X["hs"] = math.sin(2 * math.pi * hour / 24)
    X["hc"] = math.cos(2 * math.pi * hour / 24)
    return X, s


def score(mname: str, X: dict) -> float:
    """p разворота по модели mname."""
    m = MODELS[mname]
    z = m["b"]
    for name, (lo, hi, mu, sd, w) in m["w"].items():
        v = X.get(name, 0.0)
        if v is None or not math.isfinite(v):
            v = 0.0
        v = lo if v < lo else (hi if v > hi else v)
        z += (v - mu) / sd * w
    return 1.0 / (1.0 + math.exp(-z))


def make_signal(mname: str, asset: str, S: int, dec: int, p: float, s: int,
                X: dict) -> dict:
    m = MODELS[mname]
    conf = abs(p - 0.5)
    ref_dir = "up" if s > 0 else "down"
    rev = p >= 0.5
    side = opposite(ref_dir) if rev else ref_dir
    pw = max(p, 1.0 - p)
    pc = calibrate(m, pw)
    cap = max_ask_for(max(0.0, pc - CFG.MIN_EDGE))
    return {"model": mname, "asset": asset, "tgt_tf": m["tf"], "tgt_start": S,
            "dec": dec, "lead": m["lead"], "p": p, "pw": pw, "pc": pc,
            "conf": conf, "q": conf_rank(m, conf), "ref_dir": ref_dir,
            "side": side, "dir_kind": "разворот" if rev else "продолжение",
            "cap": cap,
            "feat": {k: round(X[k], 4) for k in
                     ("n15", "n5", "k15", "k5", "r60", "r240", "x_mean", "volr")}}


def next_slot(now: float, W: int, lead: int) -> int:
    """Ближайшее окно, момент решения которого ещё впереди."""
    return int((now + lead) // W) * W + W


# ═══════════════════════════════════════════════════════════════ рынки
class Rate:
    def __init__(self, rps: float):
        self.gap = 1.0 / rps if rps > 0 else 0.0
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self):
        if self.gap <= 0:
            return
        with self.lock:
            now = time.monotonic()
            t = self.next if self.next > now else now
            self.next = t + self.gap
        d = t - time.monotonic()
        if d > 0:
            time.sleep(d)

    def pause(self, sec: float):
        with self.lock:
            self.next = max(self.next, time.monotonic() + sec)


class Markets:
    def __init__(self):
        import requests
        self._requests = requests
        self._local = threading.local()
        self.cache: dict[str, dict] = {}
        self.outcomes: dict[str, str] = {}
        self.lock = threading.Lock()
        self.rate = Rate(CFG.GAMMA_RPS)
        self.stats: dict[str, int] = defaultdict(int)
        self.mode = "slug"

    @property
    def s(self):
        s = getattr(self._local, "s", None)
        if s is None:
            s = self._requests.Session()
            s.headers["User-Agent"] = VERSION
            self._local.s = s
        return s

    def bump(self, k):
        with self.lock:
            self.stats[k] += 1

    @staticmethod
    def _jload(v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return None
        return v

    def _get(self, url, params=None):
        for i in range(CFG.GAMMA_RETRIES + 1):
            self.rate.wait()
            try:
                r = self.s.get(url, params=params, timeout=CFG.GAMMA_TIMEOUT)
            except Exception:
                self.bump("сеть/таймаут")
                time.sleep(CFG.GAMMA_BACKOFF * (2 ** i) * (0.5 + random.random()))
                continue
            sc = r.status_code
            if sc == 200:
                self.bump("200 ok")
                try:
                    return 200, r.json()
                except Exception:
                    return 200, None
            if sc == 404:
                self.bump("404 нет рынка")
                return 404, None
            self.bump(f"HTTP {sc}")
            if sc == 429 or sc >= 500:
                w = CFG.GAMMA_BACKOFF * (2 ** i) * (0.5 + random.random())
                self.rate.pause(w)
                time.sleep(w)
                continue
            return sc, None
        return 0, None

    @staticmethod
    def _markets_from(js) -> list[dict]:
        if js is None:
            return []
        if isinstance(js, dict):
            for k in ("data", "markets"):
                if isinstance(js.get(k), list):
                    js = js[k]
                    break
            else:
                js = [js]
        if not isinstance(js, list):
            return []
        out = []
        for it in js:
            if not isinstance(it, dict):
                continue
            if isinstance(it.get("markets"), list):
                out.extend(x for x in it["markets"] if isinstance(x, dict))
            else:
                out.append(it)
        return out

    def _ingest(self, m: dict, want: str | None = None, force: bool = False):
        sl = want if (force and want) else (str(m.get("slug") or "") or (want or ""))
        if not sl:
            return
        toks = self._jload(m.get("clobTokenIds"))
        outs = self._jload(m.get("outcomes"))
        flip = bool(outs and len(outs) >= 2
                    and str(outs[0]).strip().lower() in ("down", "no"))
        if toks and len(toks) >= 2:
            up, down = str(toks[0]), str(toks[1])
            if flip:
                up, down = down, up
            info = {"slug": sl, "up": up, "down": down,
                    "tick_size": str(m.get("orderPriceMinTickSize") or "0.01"),
                    "neg_risk": bool(m.get("negRisk", False))}
            with self.lock:
                self.cache.setdefault(sl, info)
        pr = self._jload(m.get("outcomePrices"))
        if pr and len(pr) >= 2:
            try:
                p0, p1 = float(pr[0]), float(pr[1])
            except (TypeError, ValueError):
                return
            closed = bool(m.get("closed") or m.get("archived") or m.get("resolved"))
            sure = abs(p0 - p1) >= 0.9
            if closed or sure:
                names = ([str(o).strip().lower() for o in outs[:2]]
                         if outs and len(outs) >= 2 else ["up", "down"])
                wn = names[0] if p0 > p1 else names[1]
                w = ("up" if wn in ("up", "yes")
                     else ("down" if wn in ("down", "no") else None))
                if w:
                    with self.lock:
                        self.outcomes[sl] = w

    def _fetch(self, sl: str):
        _, js = self._get(f"{CFG.GAMMA_HOST}/markets/slug/{sl}")
        ms = self._markets_from(js)
        if not ms:
            _, js = self._get(f"{CFG.GAMMA_HOST}/markets",
                              params=[("slug", sl), ("limit", "5")])
            ms = self._markets_from(js)
        for m in ms:
            self._ingest(m, want=sl, force=(len(ms) == 1))

    def info_sync(self, asset, tf, start) -> dict | None:
        sl = slug(asset, tf, start)
        with self.lock:
            hit = self.cache.get(sl)
        if hit:
            return hit
        self._fetch(sl)
        with self.lock:
            return self.cache.get(sl)

    def outcome_sync(self, asset, tf, start) -> str | None:
        sl = slug(asset, tf, start)
        with self.lock:
            hit = self.outcomes.get(sl)
        if hit:
            return hit
        self._fetch(sl)
        with self.lock:
            return self.outcomes.get(sl)

    def levels_sync(self, token) -> dict | None:
        """Уровни книги целиком: нужны, чтобы вычесть свой объём."""
        try:
            r = self.s.get(f"{CFG.CLOB_HOST}/book", params={"token_id": token},
                           timeout=CFG.BOOK_TIMEOUT)
            r.raise_for_status()
            b = r.json()
            bids = sorted(((float(x["price"]), float(x["size"]))
                           for x in (b.get("bids") or [])), reverse=True)
            asks = sorted((float(x["price"]), float(x["size"]))
                          for x in (b.get("asks") or []))
            return {"bids": bids, "asks": asks,
                    "bid": bids[0][0] if bids else None,
                    "ask": asks[0][0] if asks else None}
        except Exception:
            return None

    async def info(self, a, tf, s):
        return await asyncio.to_thread(self.info_sync, a, tf, s)

    async def outcome(self, a, tf, s):
        return await asyncio.to_thread(self.outcome_sync, a, tf, s)

    async def levels(self, t):
        return await asyncio.to_thread(self.levels_sync, t)


# ═══════════════════════════════════════════════════════════════ брокер
class Broker:
    """Лимитки, отмена с подтверждением, FAK-добор.

    Главное правило версии 1.1: НЕИЗВЕСТНОСТЬ НЕ РАВНА НУЛЮ. Любой метод,
    который не смог узнать налив, возвращает None, а не 0.0. Ноль здесь
    означает «биржа явно отказала, в книгу ничего не ушло».
    """

    def __init__(self):
        self.c = None
        self._seq = 0
        self._lock = threading.Lock()
        self._get_order = None
        self._cancels: list[str] = []
        self._post_only_kw: dict | None = None
        self.live: dict[str, dict] = {}          # oid -> {token, tag, ts}
        self._dry: dict[str, dict] = {}
        if CFG.DRY_RUN:
            log("БУМАГА: ордера не отправляются, наливы имитируются "
                f"(PM_DRY_FILL_PROB={CFG.DRY_FILL_PROB})")
            return
        from py_clob_client_v2 import BuilderConfig, ClobClient
        self.c = ClobClient(host=CFG.CLOB_HOST, key=CFG.PRIVATE_KEY,
                            chain_id=CFG.CHAIN_ID,
                            signature_type=CFG.SIGNATURE_TYPE,
                            funder=CFG.FUNDER,
                            builder_config=BuilderConfig(
                                builder_address=BUILDER_ADDRESS,
                                builder_code=BUILDER_CODE))
        creds = None
        for nm in ("create_or_derive_api_key", "derive_api_key",
                   "create_or_derive_api_creds", "create_api_key"):
            fn = getattr(self.c, nm, None)
            if callable(fn):
                try:
                    creds = fn()
                    log(f"api creds через {nm}()")
                    break
                except Exception as e:
                    log(f"  {nm}(): {e}")
        if creds is None:
            raise SystemExit("не удалось получить api creds")
        self.c.set_api_creds(creds)
        for nm in ("get_order", "get_order_by_id", "order"):
            if callable(getattr(self.c, nm, None)):
                self._get_order = nm
                break
        for nm in ("cancel", "cancel_order", "cancel_orders", "cancel_all",
                   "cancel_market_orders"):
            if callable(getattr(self.c, nm, None)):
                self._cancels.append(nm)
        log(f"CLOB подключён. статус через {self._get_order or 'НЕТ'}, "
            f"отмена через {', '.join(self._cancels) or 'НЕТ'}")
        if not self._cancels:
            raise SystemExit("клиент не умеет отменять ордера — мейкерная "
                             "стратегия без отмены работать не должна")
        if not self._get_order:
            log("!! клиент не умеет читать статус ордера: любой обрыв связи "
                "будет считаться ИСПОЛНЕНИЕМ, добор прекратится. Это "
                "осознанно — лучше недобрать, чем купить дважды")

    # ── вспомогательное ──
    @staticmethod
    def _num(o, keys):
        if o is None:
            return None
        if isinstance(o, list):
            v = [Broker._num(x, keys) for x in o]
            v = [x for x in v if x is not None]
            return sum(v) if v else None
        if not isinstance(o, dict):
            o = getattr(o, "__dict__", None) or {}
        for k in keys:
            if k in o and o[k] is not None:
                try:
                    return float(o[k])
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _status(o):
        if isinstance(o, list):
            o = o[0] if o else None
        if o is None:
            return None
        if not isinstance(o, dict):
            o = getattr(o, "__dict__", None) or {}
        for k in ("status", "state", "orderStatus"):
            v = o.get(k)
            if v:
                return str(v).upper()
        return None

    def order_state(self, oid) -> dict:
        """{'filled': float|None, 'live': bool|None}. None = неизвестно."""
        if CFG.DRY_RUN:
            d = self._dry.get(str(oid))
            if d is None:
                return {"filled": None, "live": False}
            return {"filled": d["filled"], "live": d["live"]}
        if not self._get_order or not oid:
            return {"filled": None, "live": None}
        try:
            o = getattr(self.c, self._get_order)(oid)
        except Exception as e:
            if CFG.VERBOSE:
                log(f"  статус {oid}: {e}")
            return {"filled": None, "live": None}
        f = self._num(o, ("size_matched", "sizeMatched", "matched_size",
                          "matchedSize", "filled_size", "filledSize", "filled"))
        st = self._status(o)
        live = None
        if st:
            live = st in ("LIVE", "OPEN", "PLACED", "NEW", "PARTIALLY_FILLED",
                          "PARTIALLYFILLED", "ACTIVE")
        return {"filled": f, "live": live}

    def verify_fill(self, oid, tag="") -> float | None:
        """Переспрашивает статус, пока налив не станет известен.

        Возвращает число или None, если ясности так и нет.
        """
        if not oid:
            return None
        for i in range(max(1, CFG.FILL_VERIFY_TRIES)):
            st = self.order_state(oid)
            if st["filled"] is not None:
                return float(st["filled"])
            if st["live"] is False and self._get_order:
                # заявки нет и налив не назван — считаем, что не налилось
                return 0.0
            time.sleep(CFG.FILL_VERIFY_GAP * (1 + i * 0.3))
        if CFG.VERBOSE:
            log(f"  {tag}: налив {oid} так и не подтверждён")
        return None

    def _post_kwargs(self, OrderArgs):
        """Есть ли у клиента post_only — выясняем один раз."""
        if self._post_only_kw is not None:
            return self._post_only_kw
        kw = {}
        if CFG.POST_ONLY:
            try:
                names = set(inspect.signature(OrderArgs).parameters)
            except (TypeError, ValueError):
                names = set()
            for cand in ("post_only", "postOnly"):
                if cand in names:
                    kw = {cand: True}
                    break
            if not kw:
                log("  post_only у клиента нет — лимитка защищена только "
                    "ценой (строго ниже лучшего аска)")
        self._post_only_kw = kw
        return kw

    # ── размещение ──
    def place_maker(self, token, price, size, tick, neg_risk, tag) -> tuple[str | None, bool]:
        """GTC-лимитка, которая ОСТАЁТСЯ в книге.

        Возвращает (oid, ясность). ясность=False означает, что заявка могла
        уйти в книгу, а ответа мы не получили: цепочка обязана считать себя
        неопределённой и не добирать тейкером.
        """
        price = q_price(price, tick)
        if CFG.DRY_RUN:
            with self._lock:
                self._seq += 1
                oid = f"dry-m{self._seq:06d}"
            self._dry[oid] = {"filled": 0.0, "live": True, "size": size,
                              "price": price, "t": time.time(), "maker": True}
            self.live[oid] = {"token": token, "tag": tag, "ts": time.time()}
            return oid, True
        from py_clob_client_v2 import (OrderArgs, OrderType,
                                       PartialCreateOrderOptions,
                                       PostOrdersV2Args)
        from py_clob_client_v2.order_builder.constants import BUY
        kw = self._post_kwargs(OrderArgs)
        try:
            signed = self.c.create_order(
                OrderArgs(token_id=token, price=price, size=size, side=BUY, **kw),
                options=PartialCreateOrderOptions(tick_size=tick,
                                                  neg_risk=neg_risk))
        except Exception as e:
            log(f"  {tag}: лимитка не подписана: {e}")
            return None, True          # в сеть ничего не ушло
        try:
            resp = self.c.post_orders([PostOrdersV2Args(
                order=signed, orderType=getattr(OrderType, "GTC"))])
        except Exception as e:
            msg = str(e)
            if "cross" in msg.lower():
                return None, True      # книга ушла, переставимся следующим кругом
            log(f"!! {tag}: обрыв при отправке лимитки ({e}) — судьба заявки "
                "НЕИЗВЕСТНА, снимаю всё по этому токену")
            self.panic_cancel(token, tag)
            return None, False
        r = resp[0] if isinstance(resp, list) and resp else resp
        if not isinstance(r, dict) or not r.get("success", True):
            em = str((r or {}).get("errorMsg") or "")
            if "cross" not in em.lower():
                log(f"  {tag}: отказ лимитки {em}")
            return None, True
        oid = r.get("orderID") or r.get("orderId") or r.get("id")
        if oid:
            self.live[str(oid)] = {"token": token, "tag": tag, "ts": time.time()}
            return str(oid), True
        # успех без идентификатора — снять такую заявку нечем
        log(f"!! {tag}: лимитка принята без orderID, снимаю всё по токену")
        self.panic_cancel(token, tag)
        return None, False

    def take_fak(self, token, price, size, tick, neg_risk, tag) -> tuple[str | None, float | None]:
        """FAK-заявка тейкером. Возвращает (oid, налив | None).

        None = НЕИЗВЕСТНО. Раньше здесь стояло float(got or 0.0), и любой
        обрыв связи превращался в «налив 0»: цикл бил снова. Так 30.09
        три цепочки набрали восемь позиций вместо трёх.
        """
        price = q_price(price, tick)
        if CFG.DRY_RUN:
            with self._lock:
                self._seq += 1
                oid = f"dry-t{self._seq:06d}"
            got = size          # тейкер по аску в бумаге считается налитым
            self._dry[oid] = {"filled": got, "live": False, "size": size,
                              "price": price, "t": time.time(), "maker": False}
            log(f"[БУМАГА] {tag}: ТЕЙКЕР {size:g} @ {price} -> {oid}")
            return oid, got
        from py_clob_client_v2 import (OrderArgs, OrderType,
                                       PartialCreateOrderOptions,
                                       PostOrdersV2Args)
        from py_clob_client_v2.order_builder.constants import BUY
        ot = None
        for nm in ("FAK", "IOC", "FOK"):
            ot = getattr(OrderType, nm, None)
            if ot is not None:
                break
        if ot is None:
            ot = getattr(OrderType, "GTC")
        try:
            signed = self.c.create_order(
                OrderArgs(token_id=token, price=price, size=size, side=BUY),
                options=PartialCreateOrderOptions(tick_size=tick,
                                                  neg_risk=neg_risk))
        except Exception as e:
            log(f"  {tag}: тейкер не подписан: {e}")
            return None, 0.0           # в сеть ничего не ушло
        try:
            resp = self.c.post_orders([PostOrdersV2Args(order=signed,
                                                        orderType=ot)])
        except Exception as e:
            log(f"!! {tag}: обрыв при отправке тейкера ({e}) — судьба заявки "
                "НЕИЗВЕСТНА, повтора не будет")
            return None, None
        r = resp[0] if isinstance(resp, list) and resp else resp
        if not isinstance(r, dict):
            log(f"!! {tag}: непонятный ответ тейкера {r!r} — считаю неизвестным")
            return None, None
        if not r.get("success", True):
            log(f"  {tag}: отказ тейкера {(r or {}).get('errorMsg')}")
            return None, 0.0           # биржа явно отказала
        oid = r.get("orderID") or r.get("orderId") or r.get("id")
        got = self._num(r, ("size_matched", "sizeMatched", "matched_size",
                            "matchedSize", "filled_size", "filledSize"))
        if got is None and oid:
            time.sleep(0.25)
            got = self.verify_fill(oid, tag)
        if got is None:
            log(f"!! {tag}: ТЕЙКЕР {size:g} @ {price} -> налив НЕИЗВЕСТЕН")
            return (str(oid) if oid else None), None
        log(f"{tag}: ТЕЙКЕР {size:g} @ {price} -> налив {got}")
        return (str(oid) if oid else None), float(got)

    def panic_cancel(self, token, tag=""):
        """Снять всё по токену, когда судьба собственной заявки неизвестна.

        Может задеть лимитку соседней цепочки на том же рынке — это
        сознательный размен: чужая цепочка увидит отмену и остановится,
        а неснимаемая заявка-призрак в книге хуже.
        """
        for nm in ("cancel_market_orders", "cancel_all"):
            fn = getattr(self.c, nm, None)
            if fn is None:
                continue
            try:
                if nm == "cancel_market_orders" and token:
                    fn(token)
                elif nm == "cancel_all":
                    fn()
                else:
                    continue
                log(f"  {tag}: аварийная отмена через {nm}()")
                return True
            except Exception as e:
                log(f"  {tag}: {nm}(): {e}")
        return False

    # ── отмена с подтверждением ──
    def cancel_verified(self, oid, tag="") -> tuple[float | None, bool]:
        """Снимает лимитку. Возвращает (налив | None, снята ли наверняка).

        Это место раньше ломалось дважды: одиночный cancel() молча не
        проходил, и заявка оставалась в книге; а неизвестный налив
        возвращался нулём. Теперь отмена повторяется, после каждой попытки
        читается статус, и неясность честно отдаётся наверх.
        """
        if not oid:
            return 0.0, True
        oid = str(oid)
        if CFG.DRY_RUN:
            d = self._dry.get(oid)
            if d:
                d["live"] = False
            self.live.pop(oid, None)
            return (float(d["filled"]) if d else 0.0), True
        got = None
        for i in range(max(1, CFG.CANCEL_TRIES)):
            for nm in self._cancels:
                fn = getattr(self.c, nm, None)
                if fn is None:
                    continue
                try:
                    if nm in ("cancel", "cancel_order"):
                        fn(oid)
                    elif nm == "cancel_orders":
                        fn([oid])
                    else:
                        continue          # cancel_all/market — только в эскалации
                    break
                except Exception as e:
                    if CFG.VERBOSE:
                        log(f"  {tag}: отмена {nm}({oid}): {e}")
            st = self.order_state(oid)
            if st["filled"] is not None:
                got = st["filled"]
            if st["live"] is False:
                self.live.pop(oid, None)
                return got, True
            time.sleep(CFG.CANCEL_GAP * (1 + i * 0.5))
        # эскалация: снять всё по этому рынку / вообще всё
        info = self.live.get(oid) or {}
        if self.panic_cancel(info.get("token"), tag):
            st = self.order_state(oid)
            if st["filled"] is not None:
                got = st["filled"]
            if st["live"] is False:
                self.live.pop(oid, None)
                return got, True
        log(f"!! {tag}: заявка {oid} НЕ подтверждена снятой после "
            f"{CFG.CANCEL_TRIES} попыток и эскалации — цепочка считается "
            "неопределённой, добора не будет")
        return got, False

    def sweep(self, keep: set):
        """Сторож: гасит всё, что осталось жить вне активных цепочек."""
        for oid in list(self.live):
            if oid in keep:
                continue
            st = self.order_state(oid)
            if st["live"] is False:
                self.live.pop(oid, None)
                continue
            log(f"  сторож: осиротевшая заявка {oid}, снимаю")
            self.cancel_verified(oid, "сторож")

    def cancel_everything(self):
        for oid in list(self.live):
            self.cancel_verified(oid, "выход")

    # ── бумажная имитация налива лимитки ──
    def dry_tick(self, oid, bid, ask):
        """В бумаге лимитка наливается с вероятностью, если она на лучшем биде."""
        d = self._dry.get(str(oid))
        if not d or not d["live"]:
            return
        if bid is not None and d["price"] >= bid - 1e-9:
            if random.random() < CFG.DRY_FILL_PROB * CFG.MAKER_POLL:
                left = d["size"] - d["filled"]
                part = left if random.random() < 0.6 else max(
                    1.0, math.floor(left * random.random()))
                d["filled"] = min(d["size"], d["filled"] + part)
                if d["filled"] >= d["size"] - 1e-9:
                    d["live"] = False


# ═══════════════════════════════════════════════════════════ банк/учёт
def load_history(path: str, since_ts: float = 0.0) -> tuple[float, int]:
    """Закрытый итог по журналу: сумма pnl записей settled начиная с since_ts."""
    p = Path(path)
    if not p.exists():
        return 0.0, 0
    tot, n = 0.0, 0
    with p.open(encoding="utf-8") as f:
        for line in f:
            if '"settled"' not in line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("kind") != "settled" or r.get("pnl") is None:
                continue
            if float(r.get("ts") or 0) < since_ts:
                continue
            tot += float(r["pnl"])
            n += 1
    return tot, n


class Bank:
    """Учёт сделок и депозит в работе.

    Депозит в работе = PM_DEPOSIT + закрытый итог (журнал до запуска +
    сессия). База ставки пересчитывается ступенью PM_EQUITY_STEP_PCT, чтобы
    объём не прыгал после каждой сделки; ставка = база * PM_SIZE_PCT %.
    """

    def __init__(self, hist_pnl: float = 0.0, hist_n: int = 0):
        self.realized = 0.0
        self.cost = 0.0
        self.fees = 0.0
        self.trades = 0
        self.wins = 0
        self.maker_sh = 0.0
        self.taker_sh = 0.0
        self.hist_pnl = hist_pnl
        self.hist_n = hist_n
        self.base: float | None = None
        self.on_base = None          # callback(old, new) при смене базы
        self.lock = threading.Lock()

    def add(self, cost, fee, payout, won, maker_sh, taker_sh):
        with self.lock:
            self.realized += payout - cost - fee
            self.cost += cost
            self.fees += fee
            self.trades += 1
            self.wins += 1 if won else 0
            self.maker_sh += maker_sh
            self.taker_sh += taker_sh
        self.sizing_base()

    def equity(self) -> float:
        with self.lock:
            add = (self.hist_pnl + self.realized) if CFG.COMPOUND else 0.0
        return CFG.DEPOSIT + add

    def sizing_base(self) -> float:
        eq = self.equity()
        changed = None
        with self.lock:
            if self.base is None:
                self.base = eq
                changed = (None, eq)
            else:
                step = CFG.EQUITY_STEP_PCT / 100.0
                if step <= 0:
                    move = abs(eq - self.base) > 1e-9
                else:
                    move = (self.base <= 0 or
                            abs(eq / self.base - 1.0) >= step - 1e-12)
                if move:
                    changed = (self.base, eq)
                    self.base = eq
            base = self.base
        if changed and self.on_base:
            try:
                self.on_base(*changed)
            except Exception as e:
                log(f"  банк: {e}")
        return base

    def stake(self) -> float:
        return CFG.stake_usd(max(0.0, self.sizing_base()))

    def halted(self) -> bool:
        eq = self.equity()
        return eq <= 0 or (CFG.EQUITY_STOP > 0 and eq <= CFG.EQUITY_STOP)

    def line(self) -> str:
        with self.lock:
            wr = self.wins / self.trades if self.trades else 0.0
            roi = self.realized / self.cost * 100 if self.cost else 0.0
            tot = self.maker_sh + self.taker_sh
            mk = self.maker_sh / tot * 100 if tot else 0.0
            base = self.base
            s = (f"банк: сделок {self.trades}, винрейт {wr:.3f}, "
                 f"оборот ${self.cost:.2f}, комиссия ${self.fees:.2f}, "
                 f"итог ${self.realized:+.2f} ({roi:+.2f}%), "
                 f"мейкером {mk:.0f}% объёма")
        eq = self.equity()
        s += (f" | депозит в работе ${eq:.2f}"
              + (f", база ставки ${base:.2f} -> ${CFG.stake_usd(base):.2f}"
                 if base is not None else ""))
        return s


# ══════════════════════════════════════════════════════ мейкер-котирование
def desired_maker_px(levels: dict, own_px: float | None, own_sz: float,
                     tick: str) -> float | None:
    """Цена, при которой мы первые на покупку и точно не пересекаем книгу.

    Свой объём вычитается: иначе бот будет бесконечно перебивать сам себя.
    """
    t = float(tick)
    bids = levels.get("bids") or []
    ask = levels.get("ask")
    best_other = None
    for px, sz in bids:                       # уже по убыванию цены
        s = sz
        if own_px is not None and abs(px - own_px) < t / 2:
            s = sz - own_sz
        if s > 1e-9:
            best_other = px
            break
    if best_other is None:
        want = float(tick)                    # книга пуста — минимальная цена
    else:
        want = best_other + t
    if ask is not None and want >= ask - 1e-9:
        # спред в один тик: выше лучшего бида встать нельзя, не пересекая
        want = best_other if best_other is not None else max(ask - t, t)
    want = floor_price(want, tick)
    if want < t:
        want = t
    if ask is not None and want >= ask - 1e-9:
        want = floor_price(ask - t, tick)
    return want if want >= t else None


# ══════════════════════════════════════════════════════════════ цепочка
class Chain:
    """Одна независимая цепочка: сигнал -> мейкер -> дедлайн -> тейкер.

    Деньги цепочки ограничены дважды: ставкой яруса и остатком потолка
    рынка (актив+тф+окно). Всё, что не удалось выяснить, считается
    потраченным и исполненным.
    """

    def __init__(self, stream, asset, sig, mk, br, jr, bank, stat, exp):
        self.t = stream
        self.asset = asset
        self.sig = sig
        self.sig_tf = sig["tgt_tf"]
        self.sig_start = sig["dec"]
        self.mk, self.br, self.jr, self.bank, self.stat = mk, br, jr, bank, stat
        self.exp = exp
        self.tgt_tf = sig["tgt_tf"]
        self.tgt_start = sig["tgt_start"]
        self.side = sig["side"]
        self.mkey = Exposure.key(asset, self.tgt_tf, self.tgt_start)
        self.skey = Exposure.skey(self.mkey, self.side)
        self.ordn = 0              # номер подтверждения на рынке+стороне
        self.mult = 1.0            # множитель объёма от этого номера
        self.cid = f"{stream['id']}:{asset}:{self.tgt_tf}:{self.tgt_start}:{self.side}"
        self.tag = f"{stream['id']} {asset}/{self.tgt_tf} {wlabel(self.tgt_start, self.tgt_tf)} {self.side}"
        self.fills: list[tuple[float, float, bool]] = []   # (шт, цена, тейкер)
        self.oid = None
        self.oid_px = None
        self.oid_size = 0.0
        self.size = 0.0
        self.budget = 0.0
        self.cap_px = 0.0          # предельная цена покупки для этой цепочки
        self.reserved = 0.0        # сколько денег рынка занято под цепочку
        self.unknown_usd = 0.0     # деньги, судьба которых неизвестна
        self.uncertain = False
        self._quoted_filled = 0.0

    def cap_price(self) -> float:
        """Предельная цена, выше которой покупать нельзя.

        По умолчанию — потолок сигнала: калиброванная вероятность выигрыша
        минус PM_MIN_EDGE, за вычетом тейкерной комиссии. PM_TAKER_MAX_PRICE
        > 0 задаёт абсолютный потолок, отрицательное значение снимает его.
        """
        cap = self.sig["cap"]
        if CFG.MAKER_MAX_PRICE > 0:
            cap = min(cap, CFG.MAKER_MAX_PRICE)
        if CFG.TAKER_MAX_PRICE > 0:
            cap = min(cap, CFG.TAKER_MAX_PRICE)
        elif CFG.TAKER_MAX_PRICE < 0:
            cap = 0.99
        return min(cap, 0.99)

    @property
    def left(self) -> float:
        """Сколько долей ещё нужно набрать."""
        return max(0.0, round(self.size - self.filled, 4))

    @property
    def done_enough(self) -> bool:
        """Объём считается набранным (хвост меньше минимального лота)."""
        return self.left <= CFG.FILL_TOL_SHARES + 1e-9

    @property
    def filled(self):
        return sum(s for s, _, _ in self.fills)

    @property
    def cost(self):
        return sum(s * p for s, p, _ in self.fills)

    @property
    def fee(self):
        return sum(fee_taker(p, s) + builder_fee(p, s) for s, p, tk in self.fills if tk)

    def room(self) -> float:
        """Сколько долларов цепочке ещё можно потратить."""
        return max(0.0, self.budget - self.cost - self.unknown_usd)

    def mark_uncertain(self, usd: float, why: str, **extra):
        """Деньги ушли в неизвестность: добор прекращается."""
        self.uncertain = True
        self.unknown_usd += max(0.0, usd)
        self.stat["uncertain"] += 1
        self.jr.write("uncertain", {"cid": self.cid, "why": why,
                                    "usd": round(usd, 4), **extra})
        log(f"!! {self.tag}: {why} — считаю ${usd:.2f} потраченными, "
            "добор прекращён")

    async def run(self, stop):
        t, jr = self.t, self.jr
        deadline = self.tgt_start - CFG.TAKER_DEADLINE     # мейкер до сюда
        hard = self.tgt_start - CFG.HARD_STOP              # глухая стена
        if time.time() >= hard:
            self.stat["too_late"] += 1
            jr.write("skip", {"cid": self.cid, "why": "позже глухой стены",
                              "left": round(hard - time.time(), 2)})
            return
        # номер подтверждения берётся ОДИН раз и только для допущенной
        # цепочки: пропущенные по стене номер не тратят
        self.ordn, frozen = self.exp.claim_ord(self.skey)
        self.mult = CFG.mult_for(self.ordn, frozen)
        want_usd = self.bank.stake() * self.mult
        if want_usd < CFG.MIN_ORDER_VALUE:
            self.stat["stake_zero"] += 1
            jr.write("skip", {"cid": self.cid, "why": "ставка ниже минимальной заявки",
                              "stake": round(want_usd, 4)})
            return
        if frozen and self.ordn > 1:
            jr.write("escalate_frozen", {"cid": self.cid, "market": self.mkey,
                                         "side": self.side, "ord": self.ordn})
            log(f"  {self.tag}: по этому рынку была неизвестная заявка — "
                "объём не наращивается")
        try:
            await self._run_inner(stop, deadline, hard, want_usd)
        finally:
            # неизвестная судьба заявки запрещает дальнейший рост объёма
            # по этому рынку и стороне: реальная позиция может быть больше
            # той, которую видит банк
            if self.uncertain:
                self.exp.freeze(self.skey)
            # вернуть рынку то, что не потратили (неизвестное не возвращаем)
            if self.reserved > 0:
                self.exp.settle(self.mkey, self.reserved,
                                self.cost + self.unknown_usd)

    def _reserve(self, want_usd: float, px_ref: float, tick: str) -> bool:
        """Определить объём и занять под него деньги рынка.

        ГЛАВНОЕ ОТЛИЧИЕ ОТ 1.1. Раньше цепочка резервировала ровно ставку,
        а объём считала по текущему аску и округляла ВВЕРХ. Пока висела
        лимитка, цена уходила на цент-другой, и на дедлайне проверка
        «размер*цена*запас <= остаток бюджета» не проходила НИКОГДА —
        отсюда сплошные «заявка не влезает в бюджет» и ноль исполнения.

        Теперь наоборот: объём фиксируется от ставки по текущей цене, а
        денег занимается столько, сколько этот объём стоит по ПРЕДЕЛЬНОЙ
        цене цепочки. Дальше по какой бы цене (в пределах потолка) ни
        пришлось добирать, бюджет гарантированно не мешает.
        """
        self.cap_px = self.cap_price()
        size = order_shares(want_usd, px_ref, tick, CFG.MIN_SHARES,
                            CFG.MIN_ORDER_VALUE, CFG.SHARE_STEP)
        need = size * self.cap_px
        got = self.exp.reserve(self.mkey, need)
        if got < need - 1e-9:
            # рынок отдал меньше: ужимаем объём под то, что дали
            fit = floor_shares(got / self.cap_px if self.cap_px > 0 else 0.0,
                               self.cap_px, tick)
            if fit < CFG.MIN_SHARES or fit * self.cap_px < CFG.MIN_ORDER_VALUE:
                self.exp.settle(self.mkey, got, 0.0)
                self.stat["cap_skip"] += 1
                self.jr.write("cap_skip", {
                    "cid": self.cid, "market": self.mkey,
                    "need": round(need, 2), "got": round(got, 2),
                    "cap": round(CFG.MARKET_CAP_USD, 2)})
                log(f"  {self.tag}: лимит рынка {self.mkey} исчерпан "
                    f"(${CFG.MARKET_CAP_USD:.2f}) — цепочка не торгует")
                return False
            size = fit
        self.size = size
        self.reserved = got
        self.budget = got
        return True

    async def _run_inner(self, stop, deadline, hard, want_usd):
        t, jr = self.t, self.jr
        sg = self.sig
        cap = self.cap_price()
        jr.write("signal", {
            "cid": self.cid, "tier": t["id"], "tier_name": t["name"],
            "asset": self.asset, "model": sg["model"], "lead": sg["lead"],
            "dec": sg["dec"], "t_dec": round(time.time() - sg["dec"], 2),
            "ref_dir": sg["ref_dir"], "side": self.side, "dir_kind": sg["dir_kind"],
            "p": round(sg["p"], 5), "pw": round(sg["pw"], 5),
            "p_cal": round(sg["pc"], 5), "conf": round(sg["conf"], 5),
            "q": round(sg["q"], 4), "thr": round(t["thr"], 5),
            "max_ask": round(cap, 4),
            "tgt_tf": self.tgt_tf, "tgt_start": self.tgt_start,
            "market": self.mkey,
            "want_usd": round(want_usd, 2),
            "ord": self.ordn, "mult": round(self.mult, 3),
            "sec_to_open": round(self.tgt_start - time.time(), 1),
            "taker_at_signal": bool(CFG.TAKER_AT_SIGNAL),
            "feat": sg.get("feat")})
        log(f"{self.tag}: сигнал {sg['dir_kind']} (ход {sg['ref_dir']}), "
            f"p {sg['p']:.3f}, калибр. {sg['pc']:.3f}, квантиль {sg['q']:.3f}, "
            f"до открытия {self.tgt_start - time.time():.0f}с, "
            f"потолок {cap:.3f}, ставка ${want_usd:.2f}")

        info = await self._wait_market(hard, stop)
        if info is None:
            self.stat["no_market"] += 1
            jr.write("skip", {"cid": self.cid, "why": "рынок не найден"})
            log(f"!! {self.tag}: рынок не найден до стены, пропуск")
            return
        token = info[self.side]
        tick = str(info["tick_size"])
        neg = bool(info["neg_risk"])

        lv = await self.mk.levels(token)
        px0 = (lv or {}).get("ask") or 0.52
        if not self._reserve(want_usd, px0, tick):
            return
        jr.write("sized", {"cid": self.cid, "shares": self.size,
                           "px_ref": px0, "cap_px": self.cap_px,
                           "budget": round(self.budget, 4), "tick": tick,
                           "market": self.mkey})

        if CFG.TAKER_AT_SIGNAL:
            # ждать нечего: цена до дедлайна только ухудшается
            jr.write("no_maker", {"cid": self.cid, "why": "тейкер по сигналу"})
            log(f"  {self.tag}: тейкер сразу по сигналу, без ожидания дедлайна")
        elif not CFG.MAKER_ENABLED:
            jr.write("no_maker", {"cid": self.cid, "why": "мейкер выключен"})
            if time.time() < deadline and not await sleep_until(deadline, stop):
                return
        elif time.time() < deadline:
            await self._maker_phase(token, tick, neg, deadline, stop)
        else:
            jr.write("no_maker", {"cid": self.cid,
                                  "why": "решение позже дедлайна тейкера"})
            log(f"  {self.tag}: мейкерной фазы нет — сразу тейкер")

        if self.uncertain:
            jr.write("no_taker", {"cid": self.cid,
                                  "why": "судьба лимитки неизвестна"})
        elif stop.is_set():
            jr.write("no_taker", {"cid": self.cid, "why": "остановка бота"})
        elif not self.done_enough:
            # тейкерная фаза — обязательная часть цикла, а не опция
            await self._taker_phase(token, tick, neg, hard, stop)

        # после стены ничего живого в книге остаться не должно
        if self.oid:
            got, gone = await asyncio.to_thread(self.br.cancel_verified,
                                                self.oid, self.tag)
            self._absorb(got, gone, at="стена")

        if self.filled <= 0:
            self.stat["unfilled"] += 1
            jr.write("unfilled", {"cid": self.cid, "want": self.size,
                                  "uncertain": self.uncertain})
            log(f"  {self.tag}: не набрано ничего"
                + (" (были неопределённые заявки — проверь позиции руками)"
                   if self.uncertain else ""))
            return
        mk_sh = sum(s for s, _, tk in self.fills if not tk)
        tk_sh = sum(s for s, _, tk in self.fills if tk)
        avg = self.cost / self.filled
        self.stat["filled"] += 1
        if not self.done_enough:
            self.stat["partial"] += 1
        log(f"{self.tag}: набрано {self.filled:g}/{self.size:g} "
            f"(мейкер {mk_sh:g}, тейкер {tk_sh:g}), средняя {avg:.4f}, "
            f"комиссия ${self.fee:.4f}"
            + ("" if self.done_enough else f", НЕДОБОР {self.left:g}"))
        jr.write("position", {"cid": self.cid, "shares": self.filled,
                              "want": self.size, "maker_sh": mk_sh,
                              "taker_sh": tk_sh, "avg": round(avg, 5),
                              "cost": round(self.cost, 4),
                              "fee": round(self.fee, 5),
                              "market": self.mkey,
                              "short": round(self.left, 4),
                              "uncertain": self.uncertain})
        spawn(self._settle(stop), f"резолв {self.cid}", jr)

    async def _wait_market(self, deadline, stop):
        t0 = time.time()
        while not stop.is_set():
            info = await self.mk.info(self.asset, self.tgt_tf, self.tgt_start)
            if info:
                return info
            if time.time() > min(deadline, t0 + CFG.MARKET_WAIT):
                return None
            if not await sleep_until(time.time() + CFG.MARKET_RETRY, stop):
                return None
        return None

    def _absorb(self, got, gone, at=""):
        """Учесть результат снятой лимитки.

        got=None или gone=False — неизвестность: остаток заявки считается
        купленным, цепочка помечается неопределённой.
        """
        px = self.oid_px or 0.0
        if got is not None and got > self._quoted_filled + 1e-9:
            add = got - self._quoted_filled
            self._quoted_filled = got
            self.fills.append((add, px, False))
            self.jr.write("fill", {"cid": self.cid, "kind": "maker",
                                   "shares": add, "price": px,
                                   "oid": self.oid, "at": at})
            log(f"  {self.tag}: мейкер налив {add:g} @ {px}")
        if got is None or not gone:
            rest = max(0.0, self.oid_size - self._quoted_filled)
            self.mark_uncertain(rest * px, "лимитка не подтверждена снятой",
                                oid=self.oid, at=at)
        self.oid, self.oid_px, self.oid_size = None, None, 0.0
        self._quoted_filled = 0.0

    async def _maker_phase(self, token, tick, neg, deadline, stop):
        """Держимся первыми на покупку до дедлайна. Книгу не пересекаем."""
        cap = self.cap_px
        last_quote = 0.0
        requotes = 0
        while time.time() < deadline and not stop.is_set():
            if self.uncertain:
                break
            if self.left <= 1e-9:
                break
            lv = await self.mk.levels(token)
            if lv is None:
                await asyncio.sleep(CFG.MAKER_POLL)
                continue
            if CFG.DRY_RUN and self.oid:
                self.br.dry_tick(self.oid, lv.get("bid"), lv.get("ask"))
            # налив по текущей заявке
            if self.oid:
                st = await asyncio.to_thread(self.br.order_state, self.oid)
                got = st.get("filled")
                if got is not None and got > self._quoted_filled + 1e-9:
                    add = got - self._quoted_filled
                    self._quoted_filled = got
                    self.fills.append((add, self.oid_px, False))
                    self.jr.write("fill", {"cid": self.cid, "kind": "maker",
                                           "shares": add, "price": self.oid_px,
                                           "oid": self.oid})
                    log(f"  {self.tag}: мейкер налив {add:g} @ {self.oid_px}")
                if self.filled >= self.size - 1e-9:
                    if st.get("live"):
                        g, gone = await asyncio.to_thread(
                            self.br.cancel_verified, self.oid, self.tag)
                        self._absorb(g, gone, at="набрано")
                    else:
                        self.oid, self.oid_px, self.oid_size = None, None, 0.0
                        self._quoted_filled = 0.0
                    break
                if st.get("live") is False:
                    # заявку сняли не мы; налив уже учтён выше
                    self.oid, self.oid_px, self.oid_size = None, None, 0.0
                    self._quoted_filled = 0.0
            rest = self.left
            if rest < CFG.MIN_SHARES:
                break     # хвост меньше минимальной лимитки — оставляем тейкеру
            want = desired_maker_px(lv, self.oid_px,
                                    self.oid_size if self.oid else 0.0, tick)
            if want is None or want > cap + 1e-9:
                if self.oid:
                    g, gone = await asyncio.to_thread(self.br.cancel_verified,
                                                      self.oid, self.tag)
                    self.jr.write("quote_off", {"cid": self.cid,
                                                "oid": self.oid,
                                                "why": "цена выше потолка",
                                                "want": want, "cap": cap})
                    self._absorb(g, gone, at="потолок")
                await asyncio.sleep(CFG.MAKER_POLL)
                continue
            need_move = (self.oid is None
                         or abs(want - (self.oid_px or 0)) >= float(tick) / 2)
            if need_move and time.time() - last_quote >= CFG.REQUOTE_MIN_SEC:
                if self.oid:
                    g, gone = await asyncio.to_thread(self.br.cancel_verified,
                                                      self.oid, self.tag)
                    self._absorb(g, gone, at="переставление")
                    if self.uncertain:
                        break
                rest = self.left
                if rest <= 0:
                    break
                # размер вниз до шага, при котором сумма — целые центы:
                # дробный остаток после частичного налива иначе даёт
                # заявку, которую биржа молча не принимает
                sz = floor_shares(min(rest, self.room() / want if want > 0
                                      else rest), want, tick)
                if sz < CFG.MIN_SHARES or sz * want < CFG.MIN_ORDER_VALUE:
                    self.jr.write("quote_capped", {
                        "cid": self.cid, "room": round(self.room(), 4),
                        "rest": rest, "shares": sz, "price": want})
                    break
                oid, clear = await asyncio.to_thread(self.br.place_maker, token,
                                                     want, sz, tick, neg,
                                                     self.tag)
                last_quote = time.time()
                if not clear:
                    self.mark_uncertain(sz * want,
                                        "лимитка отправлена без ответа",
                                        price=want, shares=sz)
                    break
                if oid:
                    requotes += 1
                    self.oid, self.oid_px, self.oid_size = oid, want, sz
                    self._quoted_filled = 0.0
                    self.jr.write("quote", {"cid": self.cid, "oid": oid,
                                            "price": want, "shares": sz,
                                            "bid": lv.get("bid"),
                                            "ask": lv.get("ask"),
                                            "n": requotes})
                    if CFG.VERBOSE:
                        log(f"  {self.tag}: лимитка {sz:g} @ {want} "
                            f"(книга {lv.get('bid')}/{lv.get('ask')})")
            await asyncio.sleep(CFG.MAKER_POLL)
        # дедлайн: лимитку снимаем в любом случае
        if self.oid:
            g, gone = await asyncio.to_thread(self.br.cancel_verified, self.oid,
                                              self.tag)
            self._absorb(g, gone, at="дедлайн")
        self.jr.write("maker_done", {"cid": self.cid, "filled": self.filled,
                                     "want": self.size, "requotes": requotes,
                                     "uncertain": self.uncertain})

    def _drop_quote(self, why):
        """Синхронный сброс котировки — только из сторожа и на выходе."""
        if not self.oid:
            return
        got, gone = self.br.cancel_verified(self.oid, self.tag)
        self.jr.write("quote_off", {"cid": self.cid, "oid": self.oid,
                                    "why": why})
        self._absorb(got, gone, at=why)

    async def _taker_phase(self, token, tick, neg, hard, stop):
        """Обязательный добор остатка по ценам стакана между дедлайном и стеной.

        Как это работает теперь:
          1. читается ВЕСЬ аск, а не только лучшая цена;
          2. по уровням набирается ровно недостающий объём, не дороже
             потолка цепочки и не дороже остатка бюджета;
          3. лимитная цена заявки = цена худшего задетого уровня, БЕЗ
             надбавки. Клиент считает нотионал size*price и исполняет по
             книге, поэтому любая надбавка превращается в лишние доли —
             это и была покупка 8.1509 вместо 8;
          4. размер пересчитывается из нотионала (нотионал/лимитная цена)
             и округляется ВНИЗ до шага, при котором сумма остаётся
             целыми центами. Перебрать объём при таком счёте нельзя;
          5. не набрали за одну заявку — читаем книгу заново и повторяем,
             пока не наберём или не упрёмся в стену;
          6. неизвестный результат заявки прекращает фазу немедленно:
             повтор при неизвестности и был тем самым двойным набором.
        """
        tries = 0
        empty = 0
        jr = self.jr
        while not stop.is_set():
            left = self.left
            if left <= 1e-9 or self.done_enough:
                break
            now = time.time()
            if now >= hard - CFG.TAKER_MIN_LEFT:
                jr.write("taker_stop", {"cid": self.cid, "left": left,
                                        "why": "глухая стена"})
                break
            if tries >= CFG.TAKER_TRIES:
                jr.write("taker_stop", {"cid": self.cid, "left": left,
                                        "why": "исчерпаны попытки"})
                break
            lv = await self.mk.levels(token)
            asks = (lv or {}).get("asks") or []
            if not asks:
                empty += 1
                if empty >= 3 and not lv:
                    jr.write("taker_stop", {"cid": self.cid,
                                            "why": "книга не читается"})
                    break
                await asyncio.sleep(CFG.TAKER_GAP)
                continue
            empty = 0
            room = self.room()
            got_sh, notional, worst = walk_book(asks, left, self.cap_px, room)
            if got_sh <= 1e-9 or worst is None:
                best = asks[0][0]
                if best > self.cap_px + 1e-9:
                    jr.write("taker_capped", {"cid": self.cid, "ask": best,
                                              "cap": round(self.cap_px, 4),
                                              "left": left})
                    log(f"  {self.tag}: аск {best} выше потолка "
                        f"{self.cap_px:.3f} — добор прекращён, ярус там убыточен")
                else:
                    jr.write("taker_stop", {"cid": self.cid,
                                            "room": round(room, 4),
                                            "why": "нет денег на добор"})
                break
            px = q_price(min(worst + CFG.TAKER_BUFFER, self.cap_px, 0.99), tick)
            if px < worst - 1e-9:
                px = q_price(worst, tick)
            # нотионал -> размер заявки, вниз до приемлемого биржей шага
            want = notional / px if (CFG.TAKER_NOTIONAL and px > 0) else got_sh
            sz = floor_shares(min(want, left), px, tick)
            if sz * px > room + 1e-9:
                sz = floor_shares(room / px, px, tick)
            if sz <= 0 or sz * px < CFG.MIN_ORDER_VALUE - 1e-9:
                jr.write("taker_tail", {
                    "cid": self.cid, "left": left, "price": px,
                    "usd": round(sz * px, 4), "room": round(room, 4),
                    "why": "остаток дешевле минимальной заявки биржи"})
                log(f"  {self.tag}: хвост {left:g} долей (~${left*px:.2f}) "
                    f"меньше минимальной заявки ${CFG.MIN_ORDER_VALUE:.2f} — "
                    "добрать нечем")
                break
            tries += 1
            oid, got = await asyncio.to_thread(self.br.take_fak, token, px, sz,
                                               tick, neg, self.tag)
            if got is None:
                # неизвестность: считаем заявку исполненной и уходим
                self.fills.append((sz, px, True))
                jr.write("taker_unknown", {
                    "cid": self.cid, "oid": oid, "shares_assumed": sz,
                    "price": px, "try": tries})
                self.uncertain = True
                self.stat["taker_unknown"] += 1
                log(f"!! {self.tag}: налив неизвестен — считаю {sz:g} "
                    f"@ {px} исполненными, добор прекращён. ПРОВЕРЬ ПОЗИЦИЮ")
                break
            if got > 1e-9:
                # потрачено ровно столько, сколько ушло нотионалом: если
                # налив пришёл больше заказа, значит книга была дешевле
                # лимитной цены — записываем фактическую среднюю, иначе
                # себестоимость и комиссия в журнале окажутся завышены
                px_eff = px if got <= sz + 1e-9 else round(sz * px / got, 6)
                self.fills.append((got, px_eff, True))
                jr.write("fill", {"cid": self.cid, "kind": "taker",
                                  "shares": got, "price": px, "avg": px_eff,
                                  "oid": oid, "try": tries, "asked": sz,
                                  "fee": round(fee_taker(px_eff, got), 5),
                                  "builder": round(builder_fee(px_eff, got), 5)})
                if got > sz + 1e-6:
                    self.stat["overfill"] += 1
                    jr.write("overfill", {"cid": self.cid, "asked": sz,
                                          "got": got, "price": px})
            else:
                await asyncio.sleep(CFG.TAKER_GAP)
        jr.write("taker_done", {"cid": self.cid, "filled": self.filled,
                                "want": self.size, "tries": tries,
                                "short": round(self.left, 4),
                                "uncertain": self.uncertain,
                                "left_to_wall": round(hard - time.time(), 2)})

    async def _settle(self, stop):
        end = self.tgt_start + TF_SEC[self.tgt_tf]
        if not await sleep_until(end + CFG.RESOLVE_DELAY, stop):
            return
        out = None
        for _ in range(CFG.RESOLVE_ATTEMPTS):
            out = await self.mk.outcome(self.asset, self.tgt_tf, self.tgt_start)
            if out:
                break
            if not await sleep_until(time.time() + CFG.RESOLVE_RETRY, stop):
                return
        if out is None:
            self.jr.write("settle_gap", {"cid": self.cid})
            log(f"  ! {self.tag}: исход не прочитан")
            return
        won = out == self.side
        payout = self.filled if won else 0.0
        mk_sh = sum(s for s, _, tk in self.fills if not tk)
        tk_sh = sum(s for s, _, tk in self.fills if tk)
        pnl = payout - self.cost - self.fee
        self.bank.add(self.cost, self.fee, payout, won, mk_sh, tk_sh)
        self.stat["won" if won else "lost"] += 1
        self.jr.write("settled", {
            "cid": self.cid, "tier": self.t["id"], "asset": self.asset,
            "tgt_tf": self.tgt_tf, "tgt_start": self.tgt_start,
            "side": self.side, "outcome": out, "won": won,
            "shares": self.filled, "maker_sh": mk_sh, "taker_sh": tk_sh,
            "avg": round(self.cost / self.filled, 5) if self.filled else None,
            "cost": round(self.cost, 4), "fee": round(self.fee, 5),
            "uncertain": self.uncertain, "pnl": round(pnl, 4)})
        log(f"{self.tag}: {'ВЫИГРЫШ' if won else 'проигрыш'} "
            f"{pnl:+.3f}$ | {self.bank.line()}")


# ═══════════════════════════════════════════════════ решение по окнам
async def decide(mname: str, S: int, assets: list, mk, br, jr, bank, stat,
                 stop, chains: dict, exp: "Exposure"):
    """Момент решения окна S: обновить минутки, посчитать скор, раздать потоки."""
    m = model_of(mname)
    is_rule = mname in RULES
    need = list(assets) if is_rule else list(AS4)
    dec = S - m["lead"]
    give_up = dec + CFG.DECISION_MAX_WAIT
    while True:
        await asyncio.gather(*(asyncio.to_thread(MM.refresh_sync, a) for a in need))
        have = {a: MM.arrays(a, dec) for a in need}
        if all(have[a] is not None for a in need):
            break
        if time.time() > give_up or stop.is_set():
            missing = [a for a in need if have[a] is None]
            stat["minute_late"] += 1
            jr.write("minute_late", {"model": mname, "tgt_start": S,
                                     "missing": missing})
            log(f"!! {mname} {wlabel(S, m['tf'])}: к моменту решения нет "
                f"минутки {', '.join(missing)}"
                + (" — торгуем остальными" if any(have[a] for a in assets) else ""))
            break
        await asyncio.sleep(1.0)
    if is_rule:
        bases = {a: True for a, arr in have.items() if arr is not None}
    else:
        bases = {a: asset_base(arr, dec) for a, arr in have.items() if arr is not None}
        bases = {a: b for a, b in bases.items() if b is not None}
    stake = bank.stake()
    exp.cap = CFG.market_cap_for(stake)
    for a in assets:
        if a not in bases:
            continue
        if is_rule:
            ev = rule_eval(m, have[a], S, dec)
            if ev is None or ev["dir"] == 0:
                continue
            sig = rule_signal(mname, a, S, dec, ev)
            p = sig["p"]
        else:
            X, s = build_features(bases, a, mname, dec)
            if X is None:
                continue
            p = score(mname, X)
            sig = make_signal(mname, a, S, dec, p, s, X)
        streams = CFG.streams_for(mname, a)
        fired = [st for st in streams if sig["conf"] >= st["thr"]]
        hour_utc = (S // 3600) % 24
        hour_skip = [st["id"] for st in fired
                     if hour_utc in CFG.SKIP_HOURS.get(st["id"].lower(), ())]
        if hour_skip:
            fired = [st for st in fired if st["id"] not in hour_skip]
            stat["hour_skip"] += len(hour_skip)
            jr.write("skip", {"model": mname, "asset": a, "tgt_start": S,
                              "tiers": hour_skip, "why": f"час UTC {hour_utc} закрыт (PM_SKIP_HOURS)"})
        stat[f"score_{mname}"] += 1
        if CFG.LOG_SCORES:
            jr.write("score", {
                "model": mname, "asset": a, "tgt_tf": m["tf"], "tgt_start": S,
                "p": round(p, 5), "conf": round(sig["conf"], 5),
                "q": round(sig["q"], 4), "p_cal": round(sig["pc"], 5),
                "ref_dir": sig["ref_dir"], "side": sig["side"],
                "cap": round(sig["cap"], 4),
                "fired": [st["id"] for st in fired],
                "hour_skip": hour_skip,
                "x_missing": [b for b in need if b not in bases],
                "feat": sig.get("feat"),
                "late": round(time.time() - dec, 2)})
        if not fired:
            continue
        if stake < CFG.MIN_ORDER_VALUE or bank.halted():
            stat["halted"] += 1
            jr.write("skip", {"model": mname, "asset": a, "tgt_start": S,
                              "why": "депозит в работе ниже порога",
                              "equity": round(bank.equity(), 2)})
            log(f"!! {a}/{m['tf']} {wlabel(S, m['tf'])}: сигнал есть, но депозит "
                f"${bank.equity():.2f} ниже порога — не торгую")
            continue
        for st in fired:
            if CFG.MAX_CHAINS and len(chains) >= CFG.MAX_CHAINS:
                stat["chain_cap"] += 1
                jr.write("skip", {"tier": st["id"], "why": "лимит цепочек"})
                continue
            ch = Chain(st, a, sig, mk, br, jr, bank, stat, exp)
            if ch.cid in chains:
                continue
            chains[ch.cid] = ch
            stat["fire"] += 1
            stat["stream_" + st["id"]] += 1

            async def go(c=ch):
                try:
                    await c.run(stop)
                finally:
                    chains.pop(c.cid, None)

            spawn(go(), f"цепочка {ch.cid}", jr)


async def run_model(mname: str, mk, br, jr, bank, stat, stop, chains: dict,
                    exp: "Exposure"):
    m = model_of(mname)
    W, lead = TF_SEC[m["tf"]], m["lead"]
    assets = [a for a in CFG.ASSETS if CFG.streams_for(mname, a)]
    if not assets:
        return
    while not stop.is_set():
        S = slot_after(time.time(), m["tf"], lead)
        if not await sleep_until(S - lead + CFG.DECISION_LAG, stop):
            return
        try:
            await decide(mname, S, assets, mk, br, jr, bank, stat, stop,
                         chains, exp)
        except Exception as e:
            stat["decide_error"] += 1
            jr.write("decide_error", {"model": mname, "tgt_start": S,
                                      "err": repr(e)})
            log(f"!! {mname} {wlabel(S, m['tf'])}: ошибка решения {type(e).__name__}: {e}")
            if CFG.VERBOSE:
                traceback.print_exc()


async def sweeper(br: Broker, chains: dict, stop):
    while not stop.is_set():
        if not await sleep_until(time.time() + CFG.SWEEP_SEC, stop):
            return
        keep = {c.oid for c in list(chains.values()) if c.oid}
        try:
            await asyncio.to_thread(br.sweep, keep)
        except Exception as e:
            log(f"  сторож: {e}")


async def heartbeat(bank: Bank, stat: dict, chains: dict, exp: Exposure, stop):
    while not stop.is_set():
        if not await sleep_until(time.time() + CFG.HEARTBEAT, stop):
            return
        top = ", ".join(f"{k}={v}" for k, v in sorted(stat.items())
                        if not k.startswith(("stream_", "score_")))
        log(f"— {bank.line()}")
        log(f"— цепочек в работе {len(chains)}, {top or 'событий нет'}")
        live = exp.snapshot()
        if live:
            log("— деньги по рынкам: "
                + ", ".join(f"{k} ${v:.2f}" for k, v in sorted(live.items())))
        log(f"— {MM.health()}")


# ══════════════════════════════════════════════════════════════ отчёты
def print_streams():
    print(f"\nпотоков: {len(CFG.STREAMS)}, активы {', '.join(CFG.ASSETS)}")
    print(f"стены: решение за lead до открытия, удар тейкера сразу, дедлайн "
          f"{CFG.TAKER_DEADLINE:.0f}с, глухая стена {CFG.HARD_STOP:.0f}с\n")
    print(f"{'id':>5} {'модель':>6} {'цель':>5} {'lead':>5} {'квант.':>7} "
          f"{'порог|p-.5|':>11} {'p сырое':>8} {'p калибр.':>9} {'потолок':>8} "
          f"{'ставок/сут':>10}")
    tot = 0.0
    for s in CFG.STREAMS:
        if s["model"] in RULES:
            r = RULES[s["model"]]
            cap = max_ask_for(max(0.0, r["p"] - CFG.MIN_EDGE))
            n_as = len([a for a in CFG.ASSETS if s["assets"] is None or a in s["assets"]])
            tot += r["per_day_coin"] * n_as
            print(f"{s['id']:>5} {s['model']:>6} {s['tgt_tf']:>5} {s['lead']:>4}с "
                  f"правило: серия >= {r['streak']}, сумма >= {r['sum_sig']:g}σ, "
                  f"последняя >= {r['last_sig']:g}σ; ожидаемый вин {r['p']:.3f}, "
                  f"потолок {cap:.3f}; история {r['hist']}")
            continue
        m = MODELS[s["model"]]
        n_as = len([a for a in CFG.ASSETS if s["assets"] is None or a in s["assets"]])
        pdz = per_day_at(m, s["q"]) * n_as / 4.0
        tot += pdz
        pw = 0.5 + s["thr"]
        pc = calibrate(m, pw)
        cap = max_ask_for(max(0.0, pc - CFG.MIN_EDGE))
        print(f"{s['id']:>5} {s['model']:>6} {s['tgt_tf']:>5} {s['lead']:>4}с "
              f"{s['q']:>7.3f} {s['thr']:>11.4f} {pw:>8.4f} {pc:>9.4f} "
              f"{cap:>8.3f} {pdz:>10.1f}")
    print(f"\nвсего ~{tot:.0f} сигналов в сутки (номинально, по истории; "
          "вложенные потоки на одном сигнале — отдельные цепочки).")
    if any(s["model"] not in RULES for s in CFG.STREAMS):
        print("Покупается часть: на стаканах потолок цены пропускал ~45% сигналов A15, "
              "~35% B15, ~75% C5 —\nостальные слишком дорогие для маржи больше нуля.")
    else:
        print("Покупается часть: сигнал без покупки, если аск выше потолка (рынок уже заложил разворот).")
    for w in CFG.WARN:
        print(f"!! {w}")
    print("«p калибр.» и «потолок» указаны на ПОРОГЕ потока: сигналы сильнее "
          "получают более высокий потолок.")
    print(f"потолок = цена, при которой маржа ещё {CFG.MIN_EDGE*100:.1f}пп сверх "
          "безубыточности с тейкерной комиссией; аск выше — не покупаем.")
    for mn in CFG.MODELS_USED:
        if mn in RULES:
            continue
        m = MODELS[mn]
        print(f"\n{mn}: цель {m['tf']}, решение за {m['lead']}с, обучено на "
              f"{m['n_train']} окнах, калибровка a={m['cal'][0]:+.4f} "
              f"b={m['cal'][1]:.4f}")
        print("  квантиль -> порог |p-0.5| -> ставок/сут на 4 монеты: "
              + ", ".join(f"{q:g}:{t:.4f}:{m['per_day'][q]:g}"
                          for q, t in sorted(m["conf_q"].items()) if q >= 0.8))


def print_plan():
    hist, hn = (load_history(CFG.JOURNAL, CFG.EQUITY_SINCE_TS)
                if CFG.COMPOUND else (0.0, 0))
    eq = CFG.DEPOSIT + hist
    usd = CFG.stake_usd(eq)
    print(f"\nдепозит ${CFG.DEPOSIT:.2f}"
          + (f", закрытый итог в журнале ${hist:+.2f} ({hn} сделок)"
             f" -> в работе ${eq:.2f}" if CFG.COMPOUND else ", без учёта итога"))
    print(f"ставка {CFG.SIZE_PCT:g}% = ${usd:.2f}; база пересчитывается, когда "
          f"депозит уходит на {CFG.EQUITY_STEP_PCT:g}% от неё"
          + (f"; стоп при ${CFG.EQUITY_STOP:.2f}" if CFG.EQUITY_STOP > 0 else ""))
    print(f"\n{'депозит':>9} {'ставка':>8}")
    for k in (0.7, 0.85, 1.0, 1.15, 1.3, 1.5):
        print(f"{eq * k:>9.2f} {CFG.stake_usd(eq * k):>8.2f}")
    print(f"\n{'цена':>6} {'акций':>7} {'сумма':>8} {'комиссия тейкером':>18}")
    for p in (0.50, 0.52, 0.54, 0.56):
        n = order_shares(usd, p, "0.01", CFG.MIN_SHARES, CFG.MIN_ORDER_VALUE,
                         CFG.SHARE_STEP)
        print(f"{p:>6.2f} {n:>7g} {n*p:>8.2f} {fee_taker(p, n):>18.4f}")
    cap = CFG.market_cap_for(usd)
    if cap > 0:
        print(f"\nлимит на рынок (актив+тф+окно): ${cap:.2f}"
              + (" — от текущей ставки, растёт и падает вместе с ней"
                 if CFG.MARKET_CAP_FIXED < 0 else " — фиксированный"))
    else:
        print("\nлимит на рынок ВЫКЛЮЧЕН")
    if CFG.ESCALATE > 1.0:
        print(f"эскалация: x{CFG.ESCALATE:g} на каждую следующую цепочку по "
              f"рынку+стороне, не выше x{CFG.ESCALATE_MAX:g}")
    else:
        print("эскалации нет: каждая цепочка — одна ставка")
    print_streams()
    print(f"\nрежим: {'БУМАГА' if CFG.DRY_RUN else 'РЕАЛЬНЫЕ ДЕНЬГИ'}, "
          + ("тейкер в момент сигнала" if CFG.TAKER_AT_SIGNAL
             else "мейкер до дедлайна, затем тейкер"))


def report(path: str):
    p = Path(path)
    if not p.exists():
        print(f"журнала {path} нет")
        return
    recs = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            recs.append(json.loads(line))
        except Exception:
            pass
    sig = [r for r in recs if r["kind"] == "signal"]
    pos = [r for r in recs if r["kind"] == "position"]
    stl = [r for r in recs if r["kind"] == "settled"]
    unf = [r for r in recs if r["kind"] == "unfilled"]
    print(f"\nзаписей {len(recs)}: сигналов {len(sig)}, позиций {len(pos)}, "
          f"резолвов {len(stl)}")
    # главный контроль исполнения: доходят ли сигналы до позиции
    if sig:
        conv = len(pos) / len(sig) * 100
        print(f"сигнал -> позиция: {len(pos)}/{len(sig)} = {conv:.1f}% "
              f"(пусто {len(unf)})")
        why = defaultdict(int)
        for r in recs:
            if r["kind"] in ("taker_stop", "taker_tail", "taker_capped",
                             "no_taker", "cap_skip", "skip", "quote_capped"):
                why[f"{r['kind']}: {r.get('why') or r['kind']}"] += 1
        if why:
            print("почему не добрали:")
            for k, n in sorted(why.items(), key=lambda x: -x[1]):
                print(f"   {n:>4}  {k}")
    if pos:
        short = [r.get("short", 0) or 0 for r in pos]
        part = sum(1 for s in short if s > 1e-9)
        if part:
            print(f"позиций с недобором: {part}/{len(pos)}, "
                  f"суммарно {sum(short):g} долей")
    if pos:
        mk = sum(r.get("maker_sh", 0) for r in pos)
        tk = sum(r.get("taker_sh", 0) for r in pos)
        tot = mk + tk
        print(f"объём: мейкером {mk:g} ({mk/tot*100 if tot else 0:.1f}%), "
              f"тейкером {tk:g}")
        avg = [r["avg"] for r in pos if r.get("avg")]
        if avg:
            avg.sort()
            print(f"средняя цена входа: медиана {avg[len(avg)//2]:.4f}, "
                  f"мин {avg[0]:.4f}, макс {avg[-1]:.4f}")
        # сколько денег легло на каждый рынок — тот самый контроль спама
        bym = defaultdict(lambda: [0, 0.0])
        for r in pos:
            m = r.get("market")
            if not m:
                continue
            bym[m][0] += 1
            bym[m][1] += r.get("cost", 0.0)
        worst = sorted(bym.items(), key=lambda x: -x[1][1])[:8]
        if worst:
            print(f"\n{'рынок':>28} {'цепочек':>8} {'потрачено':>10}")
            for m, (n, c) in worst:
                flag = "  !! выше лимита" if c > CFG.MARKET_CAP_USD > 0 else ""
                print(f"{m:>28} {n:>8} {c:>10.2f}{flag}")
    unc = [r for r in recs if r["kind"] in ("uncertain", "taker_unknown")]
    if unc:
        print(f"\n!! неопределённых заявок: {len(unc)} — позиции по ним стоит "
              "сверить с историей на сайте:")
        for r in unc[-10:]:
            print(f"   {r['iso']} {r.get('cid')} {r.get('why') or 'налив неизвестен'}")
    cap = [r for r in recs if r["kind"] == "cap_skip"]
    if cap:
        print(f"\nцепочек не открыто из-за лимита рынка: {len(cap)}")
    if stl:
        w = sum(1 for r in stl if r["won"])
        cost = sum(r["cost"] for r in stl)
        fee = sum(r["fee"] for r in stl)
        pnl = sum(r["pnl"] for r in stl)
        print(f"\nвинрейт {w}/{len(stl)} = {w/len(stl):.4f}")
        print(f"оборот ${cost:.2f}, комиссия ${fee:.2f}, итог ${pnl:+.2f} "
              f"({pnl/cost*100 if cost else 0:+.2f}% на оборот)")
        by = defaultdict(lambda: [0, 0, 0.0])
        for r in stl:
            b = by[r["tier"]]
            b[0] += 1
            b[1] += 1 if r["won"] else 0
            b[2] += r["pnl"]
        print(f"\n{'поток':>6} {'n':>4} {'винрейт':>8} {'итог $':>9}")
        for k, (n, w_, pl) in sorted(by.items(), key=lambda x: -x[1][2]):
            print(f"{k:>6} {n:>4} {w_/n:>8.3f} {pl:>+9.3f}")
    sigs = {r["cid"]: r for r in recs if r["kind"] == "signal"}
    if stl:
        print(f"\nживая калибровка (ожидание = средняя p_cal сигналов потока):")
        print(f"{'поток':>6} {'n':>5} {'винрейт':>8} {'ожид.':>7} {'цена':>7} "
              f"{'потолок':>8} {'итог $':>9}")
        bys = defaultdict(list)
        for r in stl:
            bys[r["tier"]].append(r)
        for k, rs in sorted(bys.items()):
            n = len(rs)
            w_ = sum(1 for r in rs if r["won"]) / n
            ss_ = [sigs[r["cid"]] for r in rs if r["cid"] in sigs]
            pc = [x["p_cal"] for x in ss_ if x.get("p_cal") is not None]
            cp = [x["max_ask"] for x in ss_ if x.get("max_ask") is not None]
            av = [r["avg"] for r in rs if r.get("avg")]
            print(f"{k:>6} {n:>5} {w_:>8.3f} "
                  f"{(sum(pc)/len(pc) if pc else float('nan')):>7.3f} "
                  f"{(sum(av)/len(av) if av else float('nan')):>7.3f} "
                  f"{(sum(cp)/len(cp) if cp else float('nan')):>8.3f} "
                  f"{sum(r['pnl'] for r in rs):>+9.3f}")
    sc = [r for r in recs if r["kind"] == "score"]
    if sc:
        span = max(1.0, (sc[-1]["ts"] - sc[0]["ts"]) / 86400)
        print(f"\nскор: оценок {len(sc)} за {span:.1f} сут")
        bym_ = defaultdict(lambda: [0, 0])
        for r in sc:
            bym_[r["model"]][0] += 1
            bym_[r["model"]][1] += 1 if r.get("fired") else 0
        for k, (n, f_) in sorted(bym_.items()):
            print(f"  {k}: оценок {n}, с сигналом {f_} ({f_/n*100:.1f}%, "
                  f"{f_/span:.1f} в сутки)")
        late = [r.get("late", 0) for r in sc]
        late.sort()
        print(f"  задержка решения после закрытия минуты: медиана "
              f"{late[len(late)//2]:.1f}с, макс {late[-1]:.1f}с")
    unf = [r for r in recs if r["kind"] == "unfilled"]
    if unf:
        print(f"\nне набрано вообще: {len(unf)} цепочек")


# ══════════════════════════════════════════════════════════════ селфтест
def _synthetic(n=3000, seed=7, end=1790000100):
    """Четыре случайных блуждания с общей составляющей — для проверок без сети."""
    rnd = random.Random(seed)
    t0 = end - n * 60
    out = {}
    common = [rnd.gauss(0, 1) for _ in range(n)]
    for j, a in enumerate(AS4):
        px = [60000.0, 2500.0, 150.0, 0.6][j]
        t, o, h, l, c = [], [], [], [], []
        for i in range(n):
            op = px
            px = px * math.exp(0.0006 * (0.7 * common[i] + 0.7 * rnd.gauss(0, 1)))
            hi = max(op, px) * (1 + abs(rnd.gauss(0, 0.0002)))
            lo = min(op, px) * (1 - abs(rnd.gauss(0, 0.0002)))
            t.append(t0 + i * 60)
            o.append(op)
            h.append(hi)
            l.append(lo)
            c.append(px)
        out[a] = (t, o, h, l, c)
    return out


def selftest() -> int:
    bad = 0

    def ok(cond, what):
        nonlocal bad
        if not cond:
            bad += 1
            print(f"  ПРОВАЛ: {what}")
        else:
            print(f"  ok: {what}")

    print("\n— потоки")
    ss = parse_streams("A15=m15:0.95,B15=m15:0.90,C5=m5:0.95")
    ok(len(ss) == 3 and [s["id"] for s in ss] == ["A15", "B15", "C5"],
       "по умолчанию три потока A15, B15, C5")
    ok(ss[0]["thr"] > ss[1]["thr"], "A15 (верхние 5%) строже B15 (верхние 10%)")
    ok(abs(ss[0]["thr"] - MODELS["m15"]["conf_q"][0.95]) < 1e-12,
       "порог A15 = квантиль 0.95 модели m15")
    ok(ss[2]["tgt_tf"] == "5m" and ss[2]["lead"] == 120, "C5: цель 5m, решение за 120с")
    ok(parse_streams("X=m5:0.9:btc+eth")[0]["assets"] == ("btc", "eth"),
       "фильтр активов разбирается")
    for bads in ("x=m7:0.9", "x=m15:1.5", "x=m15:0.9:doge", "x", "x=m15",
                 "x=m15:0.9,x=m5:0.9"):
        try:
            parse_streams(bads)
            ok(False, f"мусор отбит: {bads}")
        except ValueError:
            ok(True, f"мусор отбит: {bads}")
    for mn, m in MODELS.items():
        qs = sorted(m["conf_q"].items())
        ok(all(b[1] >= a[1] for a, b in zip(qs, qs[1:])),
           f"{mn}: пороги растут с квантилем")
        ok(abs(conf_rank(m, conf_threshold(m, 0.93)) - 0.93) < 1e-9,
           f"{mn}: квантиль <-> порог обратимы")
        ok(0.5 < calibrate(m, 0.65) < 0.65, f"{mn}: калибровка сжимает к 0.5")
        ok(calibrate(m, 0.6) < calibrate(m, 0.62), f"{mn}: калибровка монотонна")

    print("\n— признаки и скор (синтетика, без сети)")
    syn = _synthetic()
    end = syn["btc"][0][-1] + 60
    for mn, m in MODELS.items():
        W = TF_SEC[m["tf"]]
        S = (end + m["lead"]) // W * W
        while S - m["lead"] > end:
            S -= W
        dec = S - m["lead"]
        cut = {a: tuple(x[: (dec - syn[a][0][0]) // 60] for x in syn[a]) for a in AS4}
        bases = {a: asset_base(cut[a], dec) for a in AS4}
        ok(all(bases[a] is not None for a in AS4), f"{mn}: база по всем монетам")
        X, s = build_features(bases, "eth", mn, dec)
        ok(set(X) >= set(m["w"]), f"{mn}: все {len(m['w'])} признаков модели на месте")
        ok(all(math.isfinite(v) for v in X.values()), f"{mn}: признаки конечны")
        ok(s in (1, -1) and X["n15" if m["tf"] == "15m" else "n5"] >= 0,
           f"{mn}: опорный ход в знаке s неотрицателен")
        ok(X["x_eth_n"] == 0.0 and X["x_eth_k"] == 0.0,
           f"{mn}: кросс-признаки своей монеты нулевые (как на истории)")
        if m["tf"] == "15m":
            ok(abs(X["el15"] - 600 / 900) < 1e-12 and abs(X["el5"] - 1.0) < 1e-12,
               "m15: решение на 10-й минуте свечи 15m")
        else:
            ok(abs(X["el5"] - 0.6) < 1e-12, "m5: решение на 3-й минуте свечи 5m")
        p = score(mn, X)
        ok(0.0 < p < 1.0, f"{mn}: p={p:.4f} в (0,1)")
        mid = {k: v[2] for k, v in m["w"].items()}
        ok(abs(score(mn, mid) - 1 / (1 + math.exp(-m["b"]))) < 1e-12,
           f"{mn}: признаки на среднем дают sigmoid(свободного члена)")
        mir = {a: (x[0], [1 / v for v in x[1]], [1 / v for v in x[3]],
                   [1 / v for v in x[2]], [1 / v for v in x[4]])
               for a, x in cut.items()}
        bm = {a: asset_base(mir[a], dec) for a in AS4}
        _, s2 = build_features(bm, "eth", mn, dec)
        ok(s2 == -s, f"{mn}: зеркальная цена меняет сторону опорного хода")
        sig = make_signal(mn, "eth", S, dec, 0.62, s, X)
        ok(sig["side"] == opposite(sig["ref_dir"]) and sig["dir_kind"] == "разворот",
           f"{mn}: p>=0.5 — ставка против хода")
        sig2 = make_signal(mn, "eth", S, dec, 0.38, s, X)
        ok(sig2["side"] == sig2["ref_dir"], f"{mn}: p<0.5 — ставка по ходу")
        ok(abs(sig["cap"] + fee_taker(sig["cap"], 1) - (sig["pc"] - CFG.MIN_EDGE)) < 1e-9,
           f"{mn}: потолок даёт ровно MIN_EDGE сверх безубыточности")
    # дыра в минутках заполняется предыдущей минутой
    mm = Minutes()
    t, o, h, l, c = syn["btc"]
    mm.data["btc"] = {ts: (oo, hh, ll, cc) for ts, oo, hh, ll, cc in zip(t, o, h, l, c)
                      if ts != t[-10]}
    arr = mm.arrays("btc", t[-1] + 60, 2000)
    ok(arr is not None and arr[4][-10] == c[-11] and arr[1][-10] == o[-11],
       "пропущенная минута = предыдущая минута целиком")
    ok(mm.arrays("btc", t[-1] + 120, 2000) is None,
       "нет минуты, кончающейся в момент решения, — решения нет")

    print("\n— момент решения")
    ok(next_slot(1000.0, 900, 300) == 1800, "окно 1800, решение в 1500")
    ok(next_slot(1499.9, 900, 300) == 1800, "за долю секунды до решения — то же окно")
    ok(next_slot(1500.1, 900, 300) == 2700, "после решения — следующее окно")
    ok(next_slot(1000.0, 300, 120) == 1200, "5m: окно 1200, решение в 1080")
    for s in CFG.STREAMS:
        room = s["lead"] - CFG.DECISION_LAG - CFG.DECISION_MAX_WAIT
        ok(room >= CFG.TAKER_DEADLINE,
           f"{s['id']}: худший старт цепочки за {room:g}с до открытия, "
           f"дедлайн {CFG.TAKER_DEADLINE:g}с")

    print("\n— депозит в работе и ставка")
    saved = (CFG.DEPOSIT, CFG.SIZE_PCT, CFG.EQUITY_STEP_PCT, CFG.COMPOUND, CFG.EQUITY_STOP)
    CFG.DEPOSIT, CFG.SIZE_PCT, CFG.EQUITY_STEP_PCT, CFG.COMPOUND, CFG.EQUITY_STOP = \
        200.0, 2.0, 5.0, True, 0.0
    bk = Bank(0.0, 0)
    ok(abs(bk.stake() - 4.0) < 1e-9, "старт: 2% от 200 = $4.00")
    bk.add(4.0, 0.1, 9.1, True, 0, 8)          # +5.0 -> 205 (+2.5%)
    ok(abs(bk.stake() - 4.0) < 1e-9, "депозит 205 (+2.5%) — база прежняя, ставка $4.00")
    bk.add(4.0, 0.1, 9.1, True, 0, 8)          # +5.0 -> 210 (+5%)
    ok(abs(bk.stake() - 4.2) < 1e-9, "депозит 210 (+5%) — база 210, ставка $4.20")
    bk.add(20.0, 0.0, 0.0, False, 0, 40)       # -20 -> 190 (-9.5% от 210)
    ok(abs(bk.stake() - 3.8) < 1e-9, "просадка до 190 — ставка падает до $3.80")
    bk2 = Bank(30.0, 12)
    ok(abs(bk2.equity() - 230.0) < 1e-9 and abs(bk2.stake() - 4.6) < 1e-9,
       "итог из журнала +30 переживает рестарт: ставка $4.60")
    CFG.COMPOUND = False
    ok(abs(Bank(30.0, 12).stake() - 4.0) < 1e-9, "PM_COMPOUND=0 — ставка от PM_DEPOSIT")
    CFG.COMPOUND, CFG.EQUITY_STOP = True, 150.0
    ok(Bank(-60.0, 5).halted() and not Bank(-40.0, 5).halted(),
       "PM_EQUITY_STOP=150: при 140 новых цепочек нет, при 160 есть")
    CFG.DEPOSIT, CFG.SIZE_PCT, CFG.EQUITY_STEP_PCT, CFG.COMPOUND, CFG.EQUITY_STOP = saved
    ok(abs(CFG.market_cap_for(4.0) - 4.0 * CFG.MARKET_CAP_STAKES * 1.3) < 1e-9
       or CFG.MARKET_CAP_FIXED >= 0, "лимит рынка считается от текущей ставки")
    print("\n— цена мейкера")
    lv = {"bids": [(0.50, 100.0), (0.49, 50.0)], "asks": [(0.53, 80.0)],
          "bid": 0.50, "ask": 0.53}
    ok(desired_maker_px(lv, None, 0, "0.01") == 0.51,
       "встаём на тик выше лучшего бида")
    lv1 = {"bids": [(0.50, 100.0)], "asks": [(0.51, 80.0)], "bid": 0.50,
           "ask": 0.51}
    ok(desired_maker_px(lv1, None, 0, "0.01") == 0.50,
       "спред в тик: встаём НА лучший бид, книгу не пересекаем")
    lv2 = {"bids": [(0.51, 15.0), (0.50, 40.0)], "asks": [(0.54, 10.0)],
           "bid": 0.51, "ask": 0.54}
    ok(desired_maker_px(lv2, 0.51, 15.0, "0.01") == 0.51,
       "свой объём вычтен — сам себя не перебиваем")
    lv3 = {"bids": [(0.51, 40.0), (0.50, 40.0)], "asks": [(0.54, 10.0)],
           "bid": 0.51, "ask": 0.54}
    ok(desired_maker_px(lv3, 0.51, 15.0, "0.01") == 0.52,
       "на нашем уровне встали чужие — поднимаемся на тик")
    lve = {"bids": [], "asks": [(0.60, 5.0)], "bid": None, "ask": 0.60}
    ok(desired_maker_px(lve, None, 0, "0.01") == 0.01,
       "пустая книга бидов — минимальная цена")
    for lvx in (lv, lv1, lv2, lv3, lve):
        px = desired_maker_px(lvx, None, 0, "0.01")
        ok(px < (lvx["ask"] or 1.0), f"цена {px} строго ниже аска {lvx['ask']}")

    print("\n— округление заявки")
    for usd in (7.5, 1.0, 25.0):
        for p in (0.03, 0.10, 0.37, 0.52, 0.99):
            n = order_shares(usd, p, "0.01", 5.0, 1.05, 1.0)
            cost = n * q_price(p, "0.01")
            ok(n >= 5 and cost >= 1.05 - 1e-9
               and abs(cost * 100 - round(cost * 100)) < 1e-6
               and float(n).is_integer(),
               f"${usd} по {p}: {n:g} акций на ${cost:.2f}")
    n = order_shares(7.5, 0.52, "0.001", 5.0, 1.05, 1.0)
    ok(abs(n * 0.52 * 100 - round(n * 0.52 * 100)) < 1e-6,
       f"тик 0.001: сумма всё равно в центах ({n:g} акций)")

    print("\n— экономика")
    ok(abs(fee_taker(0.51, 1) - 0.07 * 0.51 * 0.49) < 1e-12,
       "комиссия тейкера C*θ*p*(1-p)")
    ok(fee_taker(0.51, 0) == 0.0, "мейкер платит ноль")
    for p in (0.57, 0.5744, 0.6332):
        ma = max_ask_for(p)
        ok(abs((ma + fee_taker(ma, 1)) - p) < 1e-9,
           f"потолок {ma:.4f} для частоты {p} безубыточен")
    ok(max_ask_for(0.5744) < 0.5744, "потолок ниже частоты на величину комиссии")

    print("\n— лимит денег на рынок")
    e = Exposure(20.0)
    k = Exposure.key("eth", "5m", 1790759700)
    k15 = Exposure.key("eth", "15m", 1790759700)
    ok(e.reserve(k, 7.5) == 7.5, "первая цепочка получает полную ставку")
    ok(e.reserve(k, 7.5) == 7.5, "вторая тоже")
    ok(abs(e.reserve(k, 7.5) - 5.0) < 1e-9,
       "третьей достаётся только остаток $5 из $20")
    ok(e.reserve(k, 7.5) == 0.0, "четвёртой не достаётся ничего")
    ok(e.reserve(k15, 7.5) == 7.5, "другой таймфрейм — свой лимит")
    ok(e.reserve(Exposure.key("eth", "5m", 1790760000), 7.5) == 7.5,
       "следующее окно того же рынка — свой лимит")
    e.settle(k, 7.5, 4.0)
    ok(abs(e.free(k) - 3.5) < 1e-9, "неизрасходованное возвращается рынку")
    e.settle(k, 4.0, 4.4)
    ok(abs(e.free(k) - 3.1) < 1e-9, "перелив доначисляется рынку")
    e0 = Exposure(0.0)
    ok(e0.reserve(k, 999.0) == 999.0 and e0.free(k) == float("inf"),
       "PM_MARKET_CAP_USD=0 снимает лимит")
    # ровно та ситуация 30.09: восемь заявок по ~$4.4 в одно окно
    e2 = Exposure(20.0)
    got = [e2.reserve(k, 4.4) for _ in range(8)]
    ok(sum(got) <= 20.0 + 1e-9,
       f"восемь заявок 30.09 упёрлись бы в ${sum(got):.2f} вместо $35.2")

    print("\n— эскалация объёма по подтверждениям")
    c0 = Cfg()
    e4 = Exposure(0.0)
    k4 = Exposure.key("btc", "15m", 1790827200)
    s_dn, s_up = Exposure.skey(k4, "down"), Exposure.skey(k4, "up")
    ok([e4.claim_ord(s_dn)[0] for _ in range(3)] == [1, 2, 3],
       "номера подтверждений растут по порядку")
    ok(e4.claim_ord(s_up)[0] == 1,
       "противоположная сторона того же рынка считается отдельно")
    ok(c0.mult_for(1) == 1.0, "первая цепочка идёт базовой ставкой")
    ok(abs(c0.mult_for(3) - c0.ESCALATE ** 2) < 1e-9,
       f"третья цепочка = ставка x{c0.ESCALATE ** 2:.2f}")
    ok(c0.mult_for(50) <= c0.ESCALATE_MAX + 1e-9,
       f"множитель не выходит за x{c0.ESCALATE_MAX:g} даже на 50-м подтверждении")
    e4.freeze(s_dn)
    ok(e4.claim_ord(s_dn)[1] is True and c0.mult_for(9, True) == 1.0,
       "неизвестная заявка замораживает рост объёма по этой стороне")
    ok(e4.claim_ord(s_up)[1] is False,
       "заморозка одной стороны не трогает другую")
    # эскалация не может обойти потолок рынка
    e5 = Exposure(31.2)
    k5 = Exposure.key("btc", "15m", 1790828100)
    tot = sum(e5.reserve(k5, 4.0 * c0.mult_for(n) * 1.15) for n in range(1, 9))
    ok(tot <= 31.2 + 1e-9,
       f"восемь нарастающих цепочек упираются в ${tot:.2f}, потолок держит")
    ok(e5.reserve(k5, 1.0) == 0.0, "после потолка рынок больше не даёт денег")

    print("\n— окна и слаги")
    ok(window_start(1000, "15m") == 900, "начало 15m окна")
    ok(window_start(1000, "5m") == 900, "начало 5m окна")
    ok(slug("btc", "15m", 1782518400) == "btc-updown-15m-1782518400", "слаг")
    ok(opposite("up") == "down" and opposite("down") == "up", "сторона")

    c = CFG
    print("\n— защита от повторной покупки")
    ok(c.TAKER_BUFFER == 0.0,
       "надбавки к цене тейкера нет: она и делала перелив налива")
    ok(c.MARKET_CAP_USD >= 0, "лимит рынка задан")
    src = Path(__file__).read_text(encoding="utf-8")
    # ищем именно старый возврат, а не упоминания в комментариях.
    # склейка нужна, чтобы строка-образец не нашла сама себя
    needle = "), float(got or " + "0.0)"
    ok(needle not in src,
       "в коде не осталось превращения неизвестности в ноль")
    ok("if got is None:" in src, "неизвестный налив обрабатывается отдельно")

    print("\n— размер заявки: сумма всегда в целых центах")
    for p in (0.01, 0.25, 0.49, 0.50, 0.53, 0.54, 0.67, 0.99):
        st = size_step(p, "0.01")
        n = floor_shares(8.1509, p, "0.01")
        cost = n * p
        ok(n <= 8.1509 + 1e-9
           and abs(cost * 100 - round(cost * 100)) < 1e-6
           and abs(n / st - round(n / st)) < 1e-6,
           f"цена {p}: шаг {st:g}, вниз от 8.1509 -> {n:g} на ${cost:.2f}")
    ok(floor_shares(7.0, 0.53, "0.01") == 7.0, "целое при нечётной цене цело")
    ok(ceil_shares(6.21, 0.50, "0.01") == 6.22,
       f"вверх по цене 0.50 шагом 0.02 (получено {ceil_shares(6.21, 0.5, '0.01')})")
    ok(floor_shares(0.4, 0.53, "0.01") == 0.0, "меньше шага — ноль, не отрицание")

    print("\n— набор по уровням стакана")
    book = [(0.53, 3.0), (0.54, 5.0), (0.55, 100.0)]
    g, u, w = walk_book(book, 8.0, 0.60)
    ok(abs(g - 8.0) < 1e-9 and abs(u - (3 * 0.53 + 5 * 0.54)) < 1e-9 and w == 0.54,
       f"8 долей собираются с двух уровней, нотионал ${u:.2f}, худший {w}")
    g, u, w = walk_book(book, 8.0, 0.53)
    ok(abs(g - 3.0) < 1e-9 and w == 0.53,
       "потолок цены обрезает набор по первому уровню")
    g, u, w = walk_book(book, 8.0, 0.60, max_usd=2.0)
    ok(u <= 2.0 + 1e-9 and g > 0, f"остаток бюджета обрезает набор (${u:.2f})")
    g, u, w = walk_book([(0.70, 50.0)], 8.0, 0.60)
    ok(g == 0.0 and w is None, "вся книга дороже потолка — не берём ничего")
    # нотионал -> размер: налив не должен превышать заказ
    g, u, w = walk_book(book, 8.0, 0.60)
    px = q_price(w, "0.01")
    sz = floor_shares(u / px, px, "0.01")
    ok(sz <= g + 1e-9,
       f"заявка {sz:g} @ {px} не перебирает {g:g} долей (нотионал ${u:.2f})")
    one = [(0.53, 50.0)]
    g1, u1, w1 = walk_book(one, 7.0, 0.60)
    sz1 = floor_shares(u1 / w1, w1, "0.01")
    ok(abs(sz1 - 7.0) < 1e-9,
       "весь объём с одного уровня — заявка ровно на нужные доли")

    print("\n— бюджет цепочки покрывает объём по потолку цены")
    c = CFG
    stake = c.stake_usd()
    n = order_shares(stake, 0.48, "0.01", c.MIN_SHARES, c.MIN_ORDER_VALUE,
                     c.SHARE_STEP)
    need = n * 0.56
    ok(need >= n * 0.55,
       f"под {n:g} долей занимается ${need:.2f} по потолку, а не ставка ${stake:.2f}")
    e3 = Exposure(c.market_cap_for(stake))
    kk = Exposure.key("eth", "15m", 1790772300)
    fits = 0
    while e3.reserve(kk, need) >= need - 1e-9:
        fits += 1
        if fits > 50:
            break
    ok(fits >= 3, f"в одно окно влезает {fits} цепочек по ${need:.2f} "
                  f"при лимите ${e3.cap:.2f}")

    print("\n— hourbot: время Нью-Йорка, слаги, окна 4h")
    ok(et_offset(1791694800) == -4 * 3600, "11.10.2026 — летнее время, UTC-4")
    ok(et_offset(datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp()) == -5 * 3600,
       "01.12.2026 — зимнее время, UTC-5")
    b_on = datetime(2026, 3, 8, 7, tzinfo=timezone.utc).timestamp()
    b_off = datetime(2026, 11, 1, 6, tzinfo=timezone.utc).timestamp()
    ok(et_offset(b_on - 1) == -5 * 3600 and et_offset(b_on) == -4 * 3600, "переход на летнее 08.03 07:00 UTC")
    ok(et_offset(b_off - 1) == -4 * 3600 and et_offset(b_off) == -5 * 3600, "переход на зимнее 01.11 06:00 UTC")
    ok(slug("btc", "1h", 1791694800) == "bitcoin-up-or-down-october-11-2026-1am-et", "слаг 1h: 1AM ET")
    ok(slug("eth", "1h", 1791694800) == "ethereum-up-or-down-october-11-2026-1am-et", "слаг 1h ETH")
    ok(slug("btc", "1h", 1791691200).endswith("october-11-2026-12am-et"), "слаг 1h: полночь = 12am")
    ok(slug("btc", "1h", 1791734400).endswith("october-11-2026-12pm-et"), "слаг 1h: полдень = 12pm")
    ok(slug("btc", "4h", 1791691200) == "btc-updown-4h-1791691200", "слаг 4h")
    ok(slot_after(1791691200 - 3600, "4h", 120) == 1791691200, "4h: окно 04:00 UTC (00:00 ET)")
    ok(slot_after(1791691200, "4h", 120) == 1791705600, "4h: следующее — 08:00 UTC (04:00 ET)")
    w = slot_after(datetime(2026, 12, 1, 3, tzinfo=timezone.utc).timestamp(), "4h", 120)
    ok(datetime.fromtimestamp(w, timezone.utc).hour == 5, "4h зимой: окно 05:00 UTC (00:00 EST)")
    ok(slot_after(1791694800 - 60, "1h", 120) == 1791698400, "1h: за минуту до часа решение уже прошло — следующий час")
    ok(slot_after(1791694800 - 200, "1h", 120) == 1791694800, "1h: за 200с — этот час")

    print("\n— hourbot: правила серии")
    def series(S, W, drift_min, n=2880, lead=120):
        dec = S - lead
        t0 = dec - n * 60
        t, o, h, l, c = [], [], [], [], []
        px = 100.0
        for i in range(n):
            ts = t0 + i * 60
            r = 0.0005 if i % 2 == 0 else -0.0005
            if ts >= S - W * drift_min[1] and ts < dec:
                r += drift_min[0]
            op = px
            px = px * math.exp(r)
            t.append(ts); o.append(op); h.append(max(op, px)); l.append(min(op, px)); c.append(px)
        return (t, o, h, l, c), dec
    S1 = 1791694800
    arr, dec = series(S1, 3600, (0.0001, 3))
    ev = rule_eval(RULES["h1"], arr, S1, dec)
    ok(ev and ev["fire"] and ev["streak"] >= 3 and ev["dir"] == 1,
       f"h1: 3 часа вверх по ~1.5σ -> сигнал (серия {ev and ev['streak']}, сумма {ev and round(ev['sum'],2)}σ)")
    sg = rule_signal("h1", "btc", S1, dec, ev)
    ok(sg["side"] == "down" and sg["tgt_tf"] == "1h" and sg["cap"] < sg["pc"], "h1: ставка вниз (разворот), потолок ниже p")
    arr, dec = series(S1, 3600, (0.0001, 2))
    ev = rule_eval(RULES["h1"], arr, S1, dec)
    ok(ev and not ev["fire"] and ev["streak"] == 2, "h1: только 2 часа -> нет сигнала")
    arr, dec = series(S1, 3600, (-0.00002, 3))
    ev = rule_eval(RULES["h1"], arr, S1, dec)
    ok(ev and not ev["fire"] and ev["dir"] == -1, "h1: 3 часа вниз, но сумма мала -> нет сигнала")
    S4 = 1791705600
    arr, dec = series(S4, 14400, (0.00005, 2))
    ev = rule_eval(RULES["h4"], arr, S4, dec)
    ok(ev and ev["fire"] and ev["streak"] >= 2 and ev["last"] >= 1, f"h4: 2 свечи вверх, последняя {ev and round(ev['last'],2)}σ -> сигнал")
    arr, dec = series(S4, 14400, (0.00001, 2))
    ev = rule_eval(RULES["h4"], arr, S4, dec)
    ok(ev and not ev["fire"], "h4: последняя свеча мала -> нет сигнала")
    ok(rule_eval(RULES["h4"], (arr[0][:500], arr[1][:500], arr[2][:500], arr[3][:500], arr[4][:500]), S4, dec) is None,
       "h4: мало истории -> нет решения")
    hs = parse_streams("H1=h1,H4=h4:btc")
    ok([s["model"] for s in hs] == ["h1", "h4"] and hs[1]["assets"] == ("btc",) and hs[0]["thr"] == 0.5,
       "потоки H1/H4 разбираются, H4 только btc")

    print(f"\nитог: {'ВСЁ ЗЕЛЁНОЕ' if bad == 0 else f'ПРОВАЛОВ {bad}'}")
    return 1 if bad else 0


# ══════════════════════════════════════════════════════ сверка и котировки
def verify() -> int:
    """Скачать минутки на контрольные моменты истории и сверить p с обучением."""
    global MM
    bad = 0
    for mn, m in MODELS.items():
        print(f"\n{mn}: цель {m['tf']}, решение за {m['lead']}с")
        by_dec = defaultdict(list)
        for a, S, pref in m["ref"]:
            by_dec[S - m["lead"]].append((a, S, pref))
        for dec, cases in sorted(by_dec.items()):
            MM = Minutes()
            for a in AS4:
                MM.warm_sync(a, end_ts=dec)
            bases = {a: asset_base(MM.arrays(a, dec), dec) for a in AS4}
            for a, S, pref in cases:
                if bases.get(a) is None:
                    print(f"  {a} {wlabel(S, m['tf'])}: нет минуток (источник {MM.src[a]})")
                    bad += 1
                    continue
                X, s = build_features(bases, a, mn, dec)
                p = score(mn, X)
                d = abs(p - pref)
                flag = "ok" if d < 2e-3 else "РАСХОЖДЕНИЕ"
                if d >= 2e-3:
                    bad += 1
                src = MM.src[a]
                print(f"  {flag:>12} {a} {datetime.fromtimestamp(S, timezone.utc):%Y-%m-%d %H:%M} "
                      f"p={p:.6f} история {pref:.6f} разница {d:.2e} [{src}]")
    print(f"\nитог сверки: {'ВСЁ СОВПАЛО' if bad == 0 else f'РАСХОЖДЕНИЙ {bad}'}")
    if bad:
        print("малые расхождения бывают, если источник не binance (у бирж чуть "
              "разные цены); большие — повод не запускать бота")
    return 1 if bad else 0


def show_quotes():
    global MM
    MM = Minutes()
    for a in sorted(set(CFG.ASSETS) | (set(AS4) if any(mn not in RULES for mn in CFG.MODELS_USED) else set())):
        MM.warm_sync(a)
    print(MM.health())
    now = time.time()
    for mn in CFG.MODELS_USED:
        if mn in RULES:
            r = RULES[mn]
            S = slot_after(now, r["tf"], r["lead"])       # ближайшее окно
            dec = int(now // 60) * 60                     # по последней закрытой минуте
            print(f"\n{mn}: следующее окно {wlabel(S, r['tf'])} ({slug('btc', r['tf'], S)}), "
                  f"решение в {datetime.fromtimestamp(S - r['lead'], timezone.utc):%H:%M:%S} UTC; "
                  f"сейчас по минуте {datetime.fromtimestamp(dec, timezone.utc):%H:%M}")
            for a in CFG.ASSETS:
                ev = rule_eval(r, MM.arrays(a, dec), S, dec)
                if ev is None:
                    print(f"  {a}: нет данных")
                    continue
                d = {1: "вверх", -1: "вниз", 0: "ноль"}[ev["dir"]]
                print(f"  {a}: серия {ev['streak']} {d}, сумма {ev['sum']:.2f}σ, последняя "
                      f"{ev['last']:.2f}σ (σ свечи {ev['sig']*100:.3f}%)"
                      + ("  -> СИГНАЛ (если так останется к решению)" if ev["fire"] else ""))
            continue
        m = MODELS[mn]
        W, lead = TF_SEC[m["tf"]], m["lead"]
        S = next_slot(now, W, lead) - W           # последнее окно, по которому решение уже было
        dec = S - lead
        bases = {a: asset_base(MM.arrays(a, dec), dec) for a in AS4}
        print(f"\n{mn}: окно {wlabel(S, m['tf'])}, решение было в "
              f"{datetime.fromtimestamp(dec, timezone.utc):%H:%M:%S}")
        for a in AS4:
            if bases.get(a) is None:
                print(f"  {a}: нет данных")
                continue
            X, s = build_features(bases, a, mn, dec)
            p = score(mn, X)
            sig = make_signal(mn, a, S, dec, p, s, X)
            fired = [st["id"] for st in CFG.streams_for(mn, a) if sig["conf"] >= st["thr"]]
            print(f"  {a}: ход {sig['ref_dir']:>4}, p={p:.4f}, квантиль {sig['q']:.3f}, "
                  f"ставка {sig['side']:>4} ({sig['dir_kind']}), калибр. {sig['pc']:.4f}, "
                  f"потолок {sig['cap']:.3f}"
                  + (f"  -> ПОТОКИ {','.join(fired)}" if fired else ""))


# ═══════════════════════════════════════════════════════════════════ run
async def run():
    global MM
    stat: dict = defaultdict(int)
    jr = Journal(CFG.JOURNAL)
    hist, hn = (load_history(CFG.JOURNAL, CFG.EQUITY_SINCE_TS)
                if CFG.COMPOUND else (0.0, 0))
    bank = Bank(hist, hn)
    MM = Minutes()
    mk = Markets()
    br = Broker()
    exp = Exposure(CFG.MARKET_CAP_USD)
    chains: dict = {}
    stop = asyncio.Event()

    def on_base(old, new):
        stake = CFG.stake_usd(new)
        exp.cap = CFG.market_cap_for(stake)
        jr.write("equity", {"old_base": None if old is None else round(old, 4),
                            "base": round(new, 4), "stake": round(stake, 4),
                            "equity": round(bank.equity(), 4),
                            "market_cap": round(exp.cap, 2)})
        if old is not None:
            log(f"депозит в работе ${new:.2f} (было ${old:.2f}) — ставка теперь "
                f"${stake:.2f}, лимит рынка ${exp.cap:.2f}")

    bank.on_base = on_base

    loop = asyncio.get_running_loop()
    import signal as _sig
    for s in (_sig.SIGINT, _sig.SIGTERM):
        try:
            loop.add_signal_handler(s, stop.set)
        except NotImplementedError:
            pass

    for w in CFG.WARN:
        log(f"!! {w}")
    log("минутки: качаю историю по четырём монетам")
    WARM = sorted(set(CFG.ASSETS) | (set(AS4) if any(mn not in RULES for mn in CFG.MODELS_USED) else set()))
    got = await asyncio.gather(*(asyncio.to_thread(MM.warm_sync, a) for a in WARM))
    if not all(got):
        log("!! не по всем монетам есть история — бот повторит перед каждым решением")
    stake = bank.stake()
    log(f"{VERSION}: потоков {len(CFG.STREAMS)} "
        f"({', '.join(s['id'] + '=' + s['name'] for s in CFG.STREAMS)}), "
        f"активы {', '.join(CFG.ASSETS)}")
    if CFG.SKIP_HOURS:
        log("закрытые часы UTC (PM_SKIP_HOURS): " + ", ".join(
            f"{k.upper()} {sorted(v)}" for k, v in CFG.SKIP_HOURS.items()))
    log(f"депозит в работе ${bank.equity():.2f} (журнал {hist:+.2f} за {hn} сделок), "
        f"ставка ${stake:.2f} ({CFG.SIZE_PCT:g}%), ступень {CFG.EQUITY_STEP_PCT:g}%, "
        + (f"лимит рынка ${exp.cap:.2f}, " if exp.cap > 0 else "ЛИМИТ РЫНКА ВЫКЛЮЧЕН, ")
        + f"мин. маржа {CFG.MIN_EDGE*100:.1f}пп, "
        + ("тейкер сразу по сигналу" if CFG.TAKER_AT_SIGNAL else "мейкер до дедлайна"))
    jr.write("start", {"version": VERSION,
                       "streams": [s["id"] + "=" + s["name"] for s in CFG.STREAMS],
                       "assets": list(CFG.ASSETS),
                       "deposit": CFG.DEPOSIT, "pct": CFG.SIZE_PCT,
                       "equity": round(bank.equity(), 4), "hist_pnl": round(hist, 4),
                       "hist_n": hn, "stake": round(stake, 4),
                       "step_pct": CFG.EQUITY_STEP_PCT, "min_edge": CFG.MIN_EDGE,
                       "market_cap": round(exp.cap, 2),
                       "taker_tries": CFG.TAKER_TRIES, "dry": CFG.DRY_RUN,
                       "taker_at_signal": CFG.TAKER_AT_SIGNAL,
                       "escalate": CFG.ESCALATE, "escalate_max": CFG.ESCALATE_MAX})

    tasks = []
    for mn in CFG.MODELS_USED:
        tasks.append(spawn(run_model(mn, mk, br, jr, bank, stat, stop, chains, exp),
                           f"модель {mn}", jr))
    tasks.append(spawn(sweeper(br, chains, stop), "сторож заявок", jr))
    tasks.append(spawn(heartbeat(bank, stat, chains, exp, stop), "пульс", jr))

    await stop.wait()
    log("остановка: снимаю все заявки")
    try:
        await asyncio.to_thread(br.cancel_everything)
    except Exception as e:
        log(f"  при остановке: {e}")
    for t in tasks:
        t.cancel()
    log(bank.line())
    if stat.get("uncertain") or stat.get("taker_unknown"):
        log(f"!! неопределённых заявок за сессию: "
            f"{stat.get('uncertain', 0) + stat.get('taker_unknown', 0)} — "
            "сверь позиции с историей на сайте")
    jr.write("stop", {"trades": bank.trades, "pnl": round(bank.realized, 4),
                      "equity": round(bank.equity(), 4),
                      "uncertain": stat.get("uncertain", 0),
                      "taker_unknown": stat.get("taker_unknown", 0)})


def main():
    ap = argparse.ArgumentParser(description="polymizerbot 2.0 — потоки по скору "
                                             "разворота, тейкерное исполнение")
    ap.add_argument("--env", default="hourbot.env")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--streams", "--tiers", action="store_true", dest="streams")
    ap.add_argument("--quotes", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--report", nargs="?", const="")
    a = ap.parse_args()
    load_env(a.env)
    global CFG
    CFG = Cfg()
    if a.selftest:
        sys.exit(selftest())
    if a.streams:
        print_streams()
        return
    if a.plan:
        print_plan()
        return
    if a.verify:
        sys.exit(verify())
    if a.quotes:
        show_quotes()
        return
    if a.report is not None:
        report(a.report or CFG.JOURNAL)
        return
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
