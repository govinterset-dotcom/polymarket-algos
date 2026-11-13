#!/usr/bin/env python3
"""
rozi — ловля резкого хода на Polymarket up/down (15m): тейкер или лимитка-ловушка. Сигнал — по стакану.

Сигнал (по каждой стороне рынка, внутри открытого окна):
  * цена стороны = mid её стакана Polymarket (WS), только при живой книге
    (есть бид и аск, не скрещена, спред <= MAX_SPREAD);
  * ступень = уровень : референс : потолок : объём  (например 0.20:0.50:0.21:2);
  * ПЕРВОЕ касание mid <= уровня — сигнал, если одновременно
        - за последние FAST_S секунд mid этой стороны был >= референса (резкий ход),
        - прошло не больше (1 − REM_MIN_FRAC) окна,
        - объём Binance за 60с >= VOL_MULT × средний минутный объём,
        - час открытия окна входит в HOURS_UTC (пусто = все часы);
    медленное первое касание делает ступень мёртвой до конца окна.
  Binance нужен для фильтра объёма, записи открытия/закрытия и дополнительных фильтров.

Исполнение — погоня до полного объёма ступени:
  * первый выстрел на сигнале — FAK по потолку ступени (подписан заранее);
  * дальше повторы, пока не набран объём или не вышло FILL_TIMEOUT_S: стреляем, когда
    аск <= текущего потолка (или книга молчит > BOOK_STALE_S); пауза RETRY_PAUSE_S,
    после отказов растёт до RETRY_PAUSE_MAX_S;
  * потолок растёт на ESC_STEP каждые ESC_EVERY_S, не выше потолка ступени + ESC_MAX;
  * без дублирования объёма: на стороне один ордер в полёте, повтор берёт только
    недобор, неизвестный исход ордера замораживает ступень.
Допуск входа: потолок ступени = уровень + 0.02 (без эскалации).
Докупка на подтверждении (ADD_*): бот помнит МАКСИМАЛЬНУЮ цену покупки на стороне
(вход или докупка; более глубокая ступень ядра или налив ниже якоря его не опускают,
лесенка не повторяется). Когда mid поднимается до якорь + ADD_STEP, докупает ADD_USDC
тейкером (FAK) с потолком предыдущая + ADD_STEP + ADD_TOL (и не выше ADD_MAX_PX).
Аск выше допуска или не выше предыдущей покупки — не докупает, ждёт. После налива
порог переносится от новой цены. Отказ FAK — пауза ADD_PAUSE_S, после ADD_MAX_REJECTS
отказов подряд — пауза RETRY_PAUSE_MAX_S. Один ордер на сторону в полёте.
Вход ловушкой (ENTRY_MODE=trap, мейкер — без тейкерской комиссии):
  * по каждой ступени — лимитка BUY GTD post-only по P = уровень − TRAP_OFFSET;
  * стоит ТОЛЬКО пока выполняется условие сигнала: max mid стороны за FAST_S >= P + 0.30
    (ref − level) и прошло не больше (1 − REM_MIN_FRAC) окна; иначе — снятие;
  * одна лимитка на ступень; новая — только после подтверждённого снятия старой;
    ступень наливается один раз; налив и судьба ордера — по данным биржи (опрос
    открытых ордеров раз в TRAP_POLL_S, size_matched), частичный налив учитывается;
  * снятие: пакетный DELETE по id -> все ордера токена -> cancel_all
    (TRAP_CANCEL_ALL_FALLBACK); после каждого шага сверка статуса ордера на бирже;
    не подтвердилось — повтор в фоне; GTD-срок — страховка на стороне биржи;
  * медленное/позднее первое касание уровня убивает ступень (TRAP_KILL_SLOW), как в
    исходном сигнале: ловушка снимается и до конца окна не ставится;
  * статус ордера «не найден» выводится только из ответа биржи на отмену по id, не из
    пустого списка открытых (сбой API давал «потерянные» живые ордера);
  * старт окна — очистка ордеров его токенов; остановка бота — снятие всех лимиток
    и проверка, что ни одной не осталось; докупки в этом режиме не работают.
  Реплей по стаканам до TWAP (исход по TWAP): тейкер с живыми ценами и комиссией +14%,
  ловушка на 0.02 ниже уровня +24% на $, прибыли в сумме в 1.5 раза больше.
Фильтры входа (0 = выкл): PRE_MAX_MIN — сторона в окне до обвала уже была >= порога
(свой порог ступени — PRE_MAX_TIERS=уровень:порог,...);
BN_MAX_ADVERSE_BP — Binance от открытия окна ушёл против стороны не больше N bp. Отсеянное
касание пишется как skip с reason=pre_max / binance (и win_max, bn_adv_bp — для разбора).
Журнал для разбора: по каждому окну событие winner (исход gamma, TWAP), посекундная
книга Up всех окон — в PATH_LOG (rozi_path.jsonl); отчёт — rozimon.py.
DEPTH_LOG=true — в trigger/skip/book_after пишется объём книги стороны (dep_*), только для разбора.
Выхода нет — держим до резолва. В конце окна по каждой сработавшей ступени пишется
shadow — результат при полном наливе по аску на сигнале.

Симуляция по стаканам (23.06–07.07 и 07.07–19.07 вне выборки, BTC+ETH 15m):
  ядро — 5 уровней 0.30..0.10, падение mid >= 0.30 за 60с, $3 на сигнал, потолок
  уровень+0.01..0.02: $133/день в выборке, $64/день вне;
  докупка $1 строго выше прошлой покупки (порог +0.02, допуск 0.01, до 0.50):
  реплей бота по стаканам — ядро $122/$63 в день, докупка $13/$16 (ROI 7% / 38%).
  Падение < 0.3 за 60с эффекта не даёт; докупка шагом 0.1 — около нуля;
  сигнал на второй стороне того же окна — в плюсе, поэтому ONE_SIDE_PER_WINDOW=false.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import math
import os
import re
import signal as _signal
import sys
import time
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import requests
import websockets

# ----------------------------------------------------------------------------- конфиг

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()

def _f(key: str, default: float) -> float:
    v = _env(key)
    return float(v) if v else default

def _i(key: str, default: int) -> int:
    v = _env(key)
    return int(v) if v else default

def _b(key: str, default: bool) -> bool:
    v = _env(key).lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")

def _list(key: str, default: str) -> List[str]:
    raw = _env(key, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


ENV_FILE_USED = ""


def _load_env_file() -> None:
    """rozi.py -> rozi.env рядом со скриптом (или ENV_FILE). Значения из файла
    перекрывают окружение, чтобы старый export не менял режим молча."""
    global ENV_FILE_USED
    explicit = os.environ.get("ENV_FILE", "").strip()
    here = Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] else Path.cwd()
    stem = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else "rozi"
    candidates = [Path(explicit)] if explicit else [
        here / f"{stem}.env", here / ".env", Path.cwd() / f"{stem}.env", Path.cwd() / ".env",
    ]
    for p in candidates:
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        for raw in p.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if not k:
                continue
            if v[:1] in ("'", '"') and v[-1:] == v[:1] and len(v) >= 2:
                v = v[1:-1]
            elif v.startswith("#"):
                v = ""
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


class Cfg:
    HOST = _env("CLOB_HOST", "https://clob.polymarket.com")
    GAMMA = _env("GAMMA_HOST", "https://gamma-api.polymarket.com")
    WS_URL = _env("WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market")
    BINANCE_WS = _env("BINANCE_WS", "wss://stream.binance.com:9443/stream")
    BINANCE_REST = _env("BINANCE_REST", "https://api.binance.com")
    CHAIN_ID = _i("CHAIN_ID", 137)
    PK = _env("PK")
    FUNDER = _env("FUNDER")
    SIGNATURE_TYPE = _i("SIGNATURE_TYPE", -1)
    API_KEY = _env("CLOB_API_KEY")
    API_SECRET = _env("CLOB_API_SECRET")
    API_PASSPHRASE = _env("CLOB_API_PASSPHRASE")

    PAPER = _b("PAPER", True)
    ASSETS = [a.lower() for a in _list("ASSETS", "btc,eth,sol,xrp")]
    TFS = [int(x) for x in _list("TFS", "15")]
    HOURS = {int(x) for x in _list("HOURS_UTC", "")}          # пусто = все часы

    TIERS: List["Tier"] = []
    FAST_S = _f("FAST_S", 60.0)
    REM_MIN_FRAC = _f("REM_MIN_FRAC", 0.33)
    VOL_MULT = _f("VOL_MULT", 0.0)
    # фильтры входа (по разбору проигрышей): 0 / <=0 = выкл
    # PRE_MAX_MIN: в этом окне mid стороны ДО обвала уже был >= порога (сторона лидировала)
    PRE_MAX_MIN = _f("PRE_MAX_MIN", 0.0)
    # к каким ступеням применять фильтр: «уровень» (порог PRE_MAX_MIN) или «уровень:порог» (свой порог ступени),
    # через запятую, напр. 0.15:0.70,0.10:0.55; пусто = ко всем с PRE_MAX_MIN; ступень не в списке — без фильтра
    PRE_MAX_TIERS = {round(float(a), 4): (float(b) if b else None)
                     for a, _, b in (x.partition(":") for x in
                                     (_env("PRE_MAX_TIERS") or "").replace(" ", "").split(",") if x)}
    # BN_MAX_ADVERSE_BP: Binance от открытия окна ушёл ПРОТИВ стороны не больше чем на N bp
    # (стакан обвалился сильнее, чем оправдывает цена) — нет данных Binance: не мешает
    BN_MAX_ADVERSE_BP = _f("BN_MAX_ADVERSE_BP", 0.0)
    SIGMA_WIN_MIN = _i("SIGMA_WIN_MIN", 60)     # окно среднего минутного объёма
    SIGMA_MIN_BP = _f("SIGMA_MIN_BP", 0.5)
    MAX_SPREAD = _f("MAX_SPREAD", 0.05)          # шире — mid не считаем
    # погоня за полным исполнением
    FILL_TIMEOUT_S = _f("FILL_TIMEOUT_S", 20.0)
    RETRY_PAUSE_S = _f("RETRY_PAUSE_S", 1.0)
    RETRY_PAUSE_MAX_S = _f("RETRY_PAUSE_MAX_S", 3.0)
    BOOK_STALE_S = _f("BOOK_STALE_S", 2.0)
    ESC_STEP = _f("ESC_STEP", 0.01)
    ESC_EVERY_S = _f("ESC_EVERY_S", 3.0)
    ESC_MAX = _f("ESC_MAX", 0.0)
    END_GUARD_S = _f("END_GUARD_S", 30.0)
    MAX_TAKES = _i("MAX_TAKES", 40)
    ONE_SIDE_PER_WINDOW = _b("ONE_SIDE_PER_WINDOW", False)
    # докупка на подтверждении: $ADD_USDC тейкером, когда mid стороны поднялся на
    # ADD_STEP выше цены ПРЕДЫДУЩЕЙ покупки на этой стороне; цена докупки строго выше
    # предыдущей и не выше предыдущая + ADD_STEP + ADD_TOL, иначе не докупаем
    ADD_USDC = _f("ADD_USDC", 0.0)              # 0 = выкл (по умолчанию выкл)
    ADD_STEP = _f("ADD_STEP", 0.02)
    ADD_TOL = _f("ADD_TOL", 0.01)               # допуск цены докупки над порогом
    ADD_MAX_PX = _f("ADD_MAX_PX", 0.50)
    ADD_WINDOW_S = _f("ADD_WINDOW_S", 0.0)      # 0 = до конца окна (минус END_GUARD_S)
    ADD_PAUSE_S = _f("ADD_PAUSE_S", 1.0)        # пауза после отказа FAK
    ADD_MAX_REJECTS = _i("ADD_MAX_REJECTS", 3)
    ADD_MAX_N = _i("ADD_MAX_N", 0)              # докупок на сторону в окне, 0 = без лимита  # отказов подряд на одном пороге — ждём новой цены
    PRESIGN = _b("PRESIGN", True)

    MIN_NOTIONAL = _f("MIN_NOTIONAL_USDC", 1.0)
    MAX_PER_WINDOW = _f("MAX_PER_WINDOW_USDC", 60.0)
    MAX_PER_HOUR = _f("MAX_PER_HOUR_USDC", 400.0)         # 0 = выкл
    # объём ступени = депозит × STAKE_PCT %, до целых $ (STAKE_PCT=0 — $ из TIERS, как раньше).
    # Меньше минимума площадки (MIN_SHARES долей по потолку ступени и MIN_NOTIONAL_USDC) — берём минимум.
    # COMPOUND=true: депозит = DEPOSIT + итог закрытых позиций в журнале (с COMPOUND_FROM)
    DEPOSIT = _f("DEPOSIT", 100.0)
    STAKE_PCT = _f("STAKE_PCT", 0.0)
    MIN_SHARES = _f("MIN_SHARES", 5.0)
    COMPOUND = _b("COMPOUND", True)
    FEE_RATE = _f("FEE_RATE", 0.07)     # комиссия тейкера rate × p × (1−p) на долю, сверх ставки — вычитается из депозита
    COMPOUND_FROM = os.getenv("COMPOUND_FROM", "").strip()   # unix или 2026-10-29T12:00; пусто = весь журнал

    PREROLL_S = _f("PREROLL_S", 45.0)
    DISCOVERY_EVERY_S = _f("DISCOVERY_EVERY_S", 15.0)
    WS_STALE_S = _f("WS_STALE_S", 25.0)
    RESOLVE_DELAY_S = _f("RESOLVE_DELAY_S", 90.0)
    RESOLVE_TIMEOUT_S = _f("RESOLVE_TIMEOUT_S", 7200.0)   # gamma иногда разрешает > 30 мин
    RESOLVE_MAX_S = _f("RESOLVE_MAX_S", 6 * 3600.0)   # сколько ещё ждать gamma по позициям
    RESOLVE_BY_BOOK = _b("RESOLVE_BY_BOOK", True)   # итог сразу по книге в конце окна
    # книга решает, если ВСЕ последние BOOK_HOLD_S секунд окна бид победителя >= BOOK_WIN_PX
    # (Up: бид Up; Down: бид Down = 1 − аск Up). Иначе — ждём gamma
    # фильтр подтверждения по Binance: в момент сигнала строим «книгу по котировкам» — справедливую цену
    # стороны по секундам Binance с механикой TWAP (страйк = среднее 60 с до старта окна, исход — среднее
    # последних 60 с; p = Φ((x + B) / (K·σ·√var))). Если по ней сторона ТОЖЕ обвалилась (за последние
    # BN_CONFIRM_LOOK_S с была <= уровня + TOL, а за FAST_S + LOOK_S с — >= референса − TOL) — это настоящий
    # ход цены, а не провал книги: на истории такие сигналы в минусе (окт 15m: 26 сигн., WR 12%, ROI −23%),
    # остальные — WR 23%, ROI +51% (лидер >= 0.70: 31 сигн., WR 26%, ROI +68%).
    # off — не считать; log — считать и писать в журнал (входы как раньше); on — пропускать (reason=bn_confirm)
    BN_CONFIRM = _env("BN_CONFIRM", "off").lower()
    BN_CONFIRM_ASSETS = {a.strip().lower() for a in _env("BN_CONFIRM_ASSETS", "btc").split(",") if a.strip()}
    BN_CONFIRM_B = _f("BN_CONFIRM_B", 0.04)      # сдвиг, bp
    BN_CONFIRM_K = _f("BN_CONFIRM_K", 1.37)      # множитель σ
    BN_CONFIRM_TOL = _f("BN_CONFIRM_TOL", 0.0)
    BN_CONFIRM_LOOK_S = _i("BN_CONFIRM_LOOK_S", 10)
    BN_CONFIRM_SIG_S = _i("BN_CONFIRM_SIG_S", 600)       # σ посекундных доходностей за столько секунд
    BN_CONFIRM_MIN_S = _i("BN_CONFIRM_MIN_S", 300)       # меньше истории — фильтр не решает (вход как раньше)
    # фильтр локального экстремума BTC (15m): off / log / on
    # on — оставлять только «плохой» экстремум: Up только на пике, Down только на дне
    # за последние BTC_EXTREME_N свечей ТФ BTC_EXTREME_TF минут (по закрытым + текущей)
    BTC_EXTREME = _env("BTC_EXTREME", "off").lower()
    BTC_EXTREME_N = _i("BTC_EXTREME_N", 6)
    BTC_EXTREME_TF = _i("BTC_EXTREME_TF", 15)          # минуты (15 или 5)
    BTC_EXTREME_ASSETS = {a.strip().lower() for a in _env("BTC_EXTREME_ASSETS", "btc").split(",") if a.strip()}
    # нет данных для фильтра (дыра в котировках, поток Binance молчит): pass — торгуем без фильтра, skip — пропускаем сигнал
    BTC_EXTREME_ON_ERROR = _env("BTC_EXTREME_ON_ERROR", "pass").lower()
    # поток Binance считается устаревшим, если сделок не было дольше стольких секунд
    BTC_EXTREME_MAX_LAG_S = _f("BTC_EXTREME_MAX_LAG_S", 10.0)
    # фильтр «амплитудная свеча ДО окна»: off / log / on. Свеча считается амплитудной, если её диапазон (high−low)
    # >= BTC_AMP_MULT × медианы диапазонов BTC_AMP_REF_N предыдущих свечей того же ТФ. Смотрим BTC_AMP_LOOKBACK
    # последних ЗАКРЫТЫХ свечей перед окном (любая из них). Направление свечи не учитывается (на истории не влияло).
    BTC_AMP = _env("BTC_AMP", "off").lower()
    BTC_AMP_TF = _i("BTC_AMP_TF", 15)                  # минуты свечи (15 или 5)
    BTC_AMP_LOOKBACK = _i("BTC_AMP_LOOKBACK", 2)
    BTC_AMP_MULT = _f("BTC_AMP_MULT", 1.5)
    BTC_AMP_REF_N = _i("BTC_AMP_REF_N", 96)
    BTC_AMP_ASSETS = {a.strip().lower() for a in _env("BTC_AMP_ASSETS", "btc").split(",") if a.strip()}
    BTC_AMP_ON_ERROR = _env("BTC_AMP_ON_ERROR", "pass").lower()      # pass — нет данных: торгуем; skip — пропускаем
    # как сочетать фильтры экстремума и амплитуды, когда оба в on: or — пропуск, только если НЕ прошёл НИ ОДИН; and — нужны оба
    BTC_FILTERS_MODE = _env("BTC_FILTERS_MODE", "or").lower()
    # Новый фильтр серий 5m: требуем N последних закрытых свечей в сторону ставки.
    # Источник — Binance 5m klines (те же REST credentials/endpoints, без нового API).
    STREAK_FILTER = _env("STREAK_FILTER", "off").lower()
    STREAK_5M_MIN = _i("STREAK_5M_MIN", 3)
    STREAK_ASSETS = {a.strip().lower() for a in _env("STREAK_ASSETS", "btc").split(",") if a.strip()}
    STREAK_ON_ERROR = _env("STREAK_ON_ERROR", "pass").lower()
    STREAK_LOOKBACK = _i("STREAK_LOOKBACK", 100)
    # поток Binance без сообщений дольше стольких секунд = завис, переподключаемся (по BTC сделки идут каждую секунду)
    BN_STALL_S = _f("BN_STALL_S", 10.0)
    BOOK_WIN_PX = _f("BOOK_WIN_PX", 0.99)
    BOOK_HOLD_S = _f("BOOK_HOLD_S", 3.0)
    CANCEL_ALL_ON_START = _b("CANCEL_ALL_ON_START", False)
    # --- вход: taker (FAK при касании уровня) | trap (лимитка-ловушка мейкером) ---
    ENTRY_MODE = _env("ENTRY_MODE", "taker").lower()
    TRAP_OFFSET = _f("TRAP_OFFSET", 0.02)          # цена лимитки = уровень − смещение
    TRAP_POLL_S = _f("TRAP_POLL_S", 1.0)           # опрос открытых ордеров (наливы)
    TRAP_REPLACE_S = _f("TRAP_REPLACE_S", 2.0)     # пауза перед повторной постановкой
    TRAP_GTD_PAD_S = _f("TRAP_GTD_PAD_S", 60.0)    # GTD: срок = отсечка входа + запас
    TRAP_GTD_MIN_S = _f("TRAP_GTD_MIN_S", 200.0)   # биржа: срок GTD >= 180 с от «сейчас»
    TRAP_BACKOFF_MAX_S = _f("TRAP_BACKOFF_MAX_S", 30.0)  # отказы постановки: пауза до
    TRAP_CANCEL_TRIES = _i("TRAP_CANCEL_TRIES", 6)
    TRAP_CANCEL_ALL_FALLBACK = _b("TRAP_CANCEL_ALL_FALLBACK", True)
    TRAP_MIN_SHARES = _f("TRAP_MIN_SHARES", 5.0)   # минимальный размер лимитки на бирже
    CLOB_MAX_CONC = _i("CLOB_MAX_CONC", 4)          # одновременных REST-запросов к CLOB
    # медленное (или позднее) первое касание уровня убивает ступень, как в исходном сигнале:
    # ловушка снимается и до конца окна не ставится
    TRAP_KILL_SLOW = _b("TRAP_KILL_SLOW", True)
    TRAP_COND_REF = _b("TRAP_COND_REF", True)
    # Binance-фильтр: лимитка стоит, только если вероятность стороны по цене Binance
    # (от открытия окна, с минутной σ за FAIR_SIGMA_MIN и оставшимся временем) не ниже
    # цены лимитки + TRAP_FAIR_MIN. Отсекает падения стакана, оправданные ходом актива
    # (там доходность ~+10% против +50…190%). История: прибыль −7%, просадка −44%.
    # <= -1 = выключено. Нет данных Binance — фильтр не мешает (как раньше)
    TRAP_FAIR_MIN = _f("TRAP_FAIR_MIN", -0.05)
    FAIR_SIGMA_MIN = _i("FAIR_SIGMA_MIN", 240)     # порог «быстроты» от уровня, а не от цены лимитки
    # «мёртвая рука» биржи: пока бот жив, раз в HEARTBEAT_S шлёт heartbeat; если биржа
    # не получила его 10 с (бот завис, комп уснул, пропала сеть) — она САМА снимает все
    # открытые ордера этих API-ключей. Защита от наливов вслепую. 0 = выключено.
    HEARTBEAT_S = _f("HEARTBEAT_S", 4.0)
    STALL_LOG_S = _f("STALL_LOG_S", 5.0)            # пауза цикла дольше — событие stall
    JOURNAL = _env("JOURNAL", "rozi.jsonl")
    # посекундная книга Up (bid/ask) каждого окна + исход gamma — данные для пересчёта
    # статистики после перехода на TWAP-резолв; пусто = не писать
    PATH_LOG = _env("PATH_LOG", "rozi_path.jsonl")
    # объём книги стороны в момент сигнала (trigger/skip/book_after): только запись, на торговлю не влияет
    DEPTH_LOG = _b("DEPTH_LOG", True)
    LOG_LEVEL = _env("LOG_LEVEL", "INFO")
    TICK_DEFAULT = _env("TICK_DEFAULT", "0.01")


@dataclass(frozen=True)
class Tier:
    level: float      # уровень mid стакана стороны
    ref: float        # с какой цены должна упасть за FAST_S
    cap: float        # потолок цены тейкера в книге
    size: float       # USDC
    label: str = ""   # имя вместо уровня (докупка — "add")

    @property
    def name(self) -> str:
        return self.label or f"{self.level:g}"


def _pre_min(tier: "Tier") -> float:
    """Порог фильтра «сторона до обвала уже была >= X» для ступени; 0 = фильтр не действует."""
    if tier.label:
        return 0.0
    if not Cfg.PRE_MAX_TIERS:
        return max(Cfg.PRE_MAX_MIN, 0.0)
    key = round(tier.level, 4)
    if key not in Cfg.PRE_MAX_TIERS:
        return 0.0
    v = Cfg.PRE_MAX_TIERS[key]
    return max(Cfg.PRE_MAX_MIN if v is None else v, 0.0)


def _pre_on(tier: "Tier") -> bool:
    """Фильтр PRE_MAX действует на эту ступень."""
    return _pre_min(tier) > 0


def _parse_tiers() -> None:
    raw = _env("TIERS", "0.30:0.60:0.32:3,0.25:0.55:0.27:3,0.20:0.50:0.22:3,"
                        "0.15:0.45:0.17:3,0.10:0.40:0.12:3")
    out = []
    for chunk in raw.split(","):
        parts = [p.strip() for p in chunk.split(":")]
        if len(parts) != 4:
            raise SystemExit(f"TIERS: {chunk!r} — нужно уровень:референс:потолок:объём")
        lv, ref, cap, size = (float(x) for x in parts)
        if not (0 < lv < ref < 1):
            raise SystemExit(f"TIERS: нужно 0 < уровень < референс < 1 в {chunk!r}")
        if cap < lv:
            raise SystemExit(f"TIERS: потолок ниже уровня в {chunk!r}")
        if Cfg.STAKE_PCT <= 0 and size < Cfg.MIN_NOTIONAL:
            raise SystemExit(f"TIERS: объём {size} меньше MIN_NOTIONAL_USDC в {chunk!r}")
        out.append(Tier(lv, ref, cap, size))
    out.sort(key=lambda t: -t.level)
    Cfg.TIERS = out


log = logging.getLogger("rozi")


# ----------------------------------------------------------------------------- объём от депозита

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
    """Депозит для расчёта объёма: DEPOSIT + P&L закрытых позиций (COMPOUND).
    P&L по ключу позиции: resolved/resolved_lock задают, resolved_fix (сверка gamma) перезаписывает."""

    EVENTS = ("resolved", "resolved_fix", "resolved_lock")

    def __init__(self):
        self.pnl: Dict[tuple, float] = {}
        self.since = _ts_from(Cfg.COMPOUND_FROM)

    @staticmethod
    def _key(r: Dict[str, Any]) -> tuple:
        lock = r.get("event") == "resolved_lock"
        return (r.get("slug"), r.get("side"), r.get("tier_name") if lock else r.get("tier"), lock)

    def on_rec(self, r: Dict[str, Any]) -> None:
        if r.get("event") not in self.EVENTS or r.get("pnl") is None:
            return
        if float(r.get("ts") or time.time()) < self.since:
            return
        pnl = float(r["pnl"])
        if r.get("event") != "resolved_lock":          # захлоп — мейкер, без комиссии
            u, sh = float(r.get("usdc") or 0), float(r.get("shares") or 0)
            if sh > 0:
                p = u / sh
                pnl -= Cfg.FEE_RATE * p * (1 - p) * sh
        self.pnl[self._key(r)] = pnl

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


def stake_floor(cap: float) -> int:
    """Минимум площадки: MIN_SHARES долей по потолку ступени и MIN_NOTIONAL_USDC, вверх до целых $."""
    return int(math.ceil(max(Cfg.MIN_NOTIONAL, Cfg.MIN_SHARES * cap) - 1e-9))


def stake_usdc(cap: float) -> float:
    """Депозит × STAKE_PCT %, до целых $; меньше минимума — минимум (сигнал не пропускаем)."""
    dep = EQUITY.value if EQUITY is not None else Cfg.DEPOSIT
    calc = int(math.floor(max(0.0, dep) * Cfg.STAKE_PCT / 100.0 + 0.5))
    return float(max(calc, stake_floor(cap)))


def book_winner_tail(tail, end: float) -> Optional[str]:
    """Исход по книге Up в последние BOOK_HOLD_S с окна: все котировки (и действовавшая на начало
    отрезка) — бид Up >= BOOK_WIN_PX → Up, либо аск Up <= 1 − BOOK_WIN_PX (бид Down >= порога) → Down.
    Сверка с gamma по 6193 окнам (0.99 / 3 с): решено 99.66%, расхождений 3 — поздний переворот по TWAP
    при книге 0.999, их исправит фоновая сверка gamma."""
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
    if all(b is not None and b >= thr for _, b, _a in seg):
        return "Up"
    if all(a is not None and a <= 1 - thr for _, _b, a in seg):
        return "Down"
    return None


def setup_logging() -> None:
    logging.basicConfig(level=getattr(logging, Cfg.LOG_LEVEL.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # клиент CLOB сам пишет ERROR на каждый 4xx; отказы бот логирует сам, без спама
    logging.getLogger("py_clob_client_v2").setLevel(logging.CRITICAL)


def tick_floor(price: float, tick: str) -> float:
    t = float(tick)
    return round(math.floor(price / t + 1e-9) * t, 6)


def _short(resp: Any) -> Any:
    if isinstance(resp, dict):
        return {k: resp[k] for k in list(resp)[:8]}
    return str(resp)[:200]


# ----------------------------------------------------------------------------- журнал

class Journal:
    def __init__(self, path: str):
        self.path = Path(path)
        self._lock = asyncio.Lock()

    async def write(self, rec: Dict[str, Any]) -> None:
        rec = {"ts": round(time.time(), 3), **rec}
        if EQUITY is not None and rec.get("event") in Equity.EVENTS and Path(self.path) == Path(Cfg.JOURNAL):
            EQUITY.on_rec(rec)
        async with self._lock:
            try:
                with self.path.open("a") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception as e:
                log.error("журнал: %s", e)

    def load_recent(self) -> Tuple[Dict[tuple, Tuple[float, float]], set]:
        """За 2 часа: (slug, side, tier) -> (USDC, шары) и ступени, которые уже
        сработали/отсеяны/заморожены, — чтобы рестарт не купил второй раз."""
        filled: Dict[tuple, Tuple[float, float]] = {}
        done: set = set()
        if not self.path.exists():
            return filled, done
        cutoff = time.time() - 7200
        try:
            with self.path.open() as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("ts", 0) < cutoff:
                        continue
                    k = (r.get("slug"), r.get("side"), r.get("tier"))
                    ev = r.get("event")
                    if ev == "fill":
                        u, s = filled.get(k, (0.0, 0.0))
                        filled[k] = (u + float(r.get("usdc") or 0),
                                     s + float(r.get("shares") or 0))
                    elif ev in ("trigger", "skip", "freeze"):
                        done.add(k)
        except Exception as e:
            log.error("чтение журнала: %s", e)
        return filled, done


# ----------------------------------------------------------------------------- builder (gozi)
# Зашито в коде, из .env не читается: параметры менять можно, атрибуцию ордеров — нет.
BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"


# ----------------------------------------------------------------------------- CLOB

class Clob:
    """Обёртка над py_clob_client_v2: FAK-тейкер, первый выстрел — предподписанный."""

    def __init__(self):
        self.client = None
        self.enabled = not Cfg.PAPER
        self._mod = None
        self.can_presign = False
        self.can_build = False
        if not self.enabled:
            log.warning("PAPER=true — ордера не отправляются, налив моделируется по книге")
            return
        try:
            import py_clob_client_v2 as m
        except ImportError as e:
            raise SystemExit("нужен пакет py_clob_client_v2: %s" % e)
        self._mod = m
        if not Cfg.PK:
            raise SystemExit("PK не задан, а PAPER=false")
        kw: Dict[str, Any] = dict(host=Cfg.HOST, chain_id=Cfg.CHAIN_ID, key=Cfg.PK)
        if Cfg.FUNDER:
            kw["funder"] = Cfg.FUNDER
        if Cfg.SIGNATURE_TYPE >= 0:
            kw["signature_type"] = Cfg.SIGNATURE_TYPE
        kw["builder_config"] = m.BuilderConfig(builder_address=BUILDER_ADDRESS, builder_code=BUILDER_CODE)
        self.client = m.ClobClient(**kw)
        if Cfg.API_KEY and Cfg.API_SECRET and Cfg.API_PASSPHRASE:
            creds = m.ApiCreds(api_key=Cfg.API_KEY, api_secret=Cfg.API_SECRET,
                               api_passphrase=Cfg.API_PASSPHRASE)
        else:
            creds = self.client.create_or_derive_api_key()
        self.client.set_api_creds(creds)
        if not hasattr(self.client, "create_and_post_market_order"):
            raise SystemExit("в клиенте нет create_and_post_market_order — нужен py_clob_client_v2")
        self.can_build = (hasattr(self.client, "create_market_order")
                          and hasattr(self.client, "post_order"))
        self.can_presign = Cfg.PRESIGN and self.can_build
        log.info("CLOB подключён, боевой режим%s",
                 ", предподпись ордеров" if self.can_presign else "")

    def _opts(self, tick: str):
        return self._mod.PartialCreateOrderOptions(tick_size=tick)

    def _args(self, token: str, usdc: float, cap: float):
        m = self._mod
        return m.MarketOrderArgs(token_id=token, amount=round(usdc, 2), side=m.Side.BUY,
                                 order_type=m.OrderType.FAK, price=round(cap, 6))

    @staticmethod
    def _classify(e: Exception, what: str) -> Optional[dict]:
        code = getattr(e, "status_code", None)
        if isinstance(code, int) and 400 <= code < 500:
            log.info("%s отклонён (%s): %s", what, code, str(e)[:160])
            return {"success": False, "status": "rejected", "errorMsg": str(e)[:200]}
        log.error("%s: исход неизвестен: %s", what, e)
        return None                     # неизвестно — ордер мог исполниться

    _MIN_TICK_RX = __import__("re").compile(r"minimum for the market is ([0-9.]+)")

    def build(self, token: str, usdc: float, cap: float, tick: str) -> Any:
        """Локальная сборка и подпись ордера — на биржу ничего не уходит.
        Если клиент держит в кэше более крупный тик (событие смены тика до него не
        дошло), пересобираем по его минимуму: цена по крупному тику валидна всегда."""
        try:
            return self.client.create_market_order(self._args(token, usdc, cap), self._opts(tick))
        except Exception as e:
            mt = self._MIN_TICK_RX.search(str(e))
            if not mt:
                raise
            t2 = mt.group(1)
            return self.client.create_market_order(
                self._args(token, usdc, max(tick_floor(cap, t2), float(t2))), self._opts(t2))

    def presign(self, token: str, usdc: float, cap: float, tick: str) -> Any:
        if not self.can_presign:
            return None
        try:
            return self.build(token, usdc, cap, tick)
        except Exception as e:
            log.warning("предподпись %s: %s", token[:10], e)
            return None

    def post_signed(self, signed: Any) -> Optional[dict]:
        try:
            return self.client.post_order(signed, self._mod.OrderType.FAK)
        except Exception as e:
            return self._classify(e, "тейкер (предподписанный)")

    def take(self, token: str, usdc: float, cap: float, tick: str) -> Optional[dict]:
        if self.can_build:
            try:
                signed = self.build(token, usdc, cap, tick)
            except Exception as e:
                # упало ДО отправки — ордера на бирже точно нет, повтор допустим
                log.warning("сборка ордера %s: %s", token[:10], e)
                return {"success": False, "status": "rejected", "errorMsg": str(e)[:200]}
            return self.post_signed(signed)
        try:
            return self.client.create_and_post_market_order(
                order_args=self._args(token, usdc, cap), options=self._opts(tick),
                order_type=self._mod.OrderType.FAK)
        except Exception as e:
            return self._classify(e, f"тейкер {token[:10]} до {cap:.3f}")

    def cancel_all(self) -> bool:
        if not self.enabled:
            return True
        try:
            self.client.cancel_all()
            return True
        except Exception as e:
            log.error("cancel_all: %s", e)
            return False

    # --- лимитки (режим trap) ------------------------------------------------------
    # все REST-вызовы лимиток идут через семафор: в начале окна ловушки 4 активов ставятся
    # разом, и пачка одновременных запросов давала "Request exception"
    _sem = __import__("threading").BoundedSemaphore(max(1, Cfg.CLOB_MAX_CONC))

    def place_limit(self, token: str, price: float, shares: float, tick: str,
                    expiration: int) -> dict:
        """GTD post-only BUY. Ответ: {"ok", "id", "unknown", "err"}.
        unknown=True — сеть/5xx: ордер МОГ встать, нужна сверка."""
        m = self._mod
        with self._sem:
            for attempt in range(2):
                try:
                    args = m.OrderArgsV2(token_id=token, price=round(price, 6),
                                         size=round(shares, 2), side=m.Side.BUY,
                                         expiration=int(expiration))
                    signed = self.client.create_order(args, self._opts(tick))
                    break
                except Exception as e:
                    mt = self._MIN_TICK_RX.search(str(e))
                    if mt and attempt == 0:        # у клиента в кэше другой тик рынка
                        tick = mt.group(1)
                        price = max(tick_floor(price, tick), float(tick))
                        continue
                    return {"ok": False, "unknown": False, "err": f"сборка: {e}"[:200]}
            r = self._post_limit(signed)
            r["px"] = price
            return r

    def _post_limit(self, signed: Any) -> dict:
        m = self._mod
        try:
            r = self.client.post_order(signed, m.OrderType.GTD, post_only=True)
        except Exception as e:
            code = getattr(e, "status_code", None)
            unknown = not (isinstance(code, int) and 400 <= code < 500)
            return {"ok": False, "unknown": unknown, "err": str(e)[:200]}
        if not isinstance(r, dict):
            return {"ok": False, "unknown": True, "err": f"ответ {r!r}"[:200]}
        oid = r.get("orderID") or r.get("orderId") or r.get("id")
        if r.get("success") is False or not oid:
            return {"ok": False, "unknown": False, "err": str(r.get("errorMsg") or r)[:200]}
        return {"ok": True, "id": oid, "status": r.get("status"), "unknown": False}

    def cancel_ids(self, ids: List[str]) -> Tuple[set, Dict[str, str]]:
        """Отмена по id через пакетный DELETE /orders (одиночный DELETE /order у v2 требует
        OrderPayload — строкой даёт ошибку). Исключение — исход неизвестен."""
        ids = [i for i in ids if i]
        if not ids:
            return set(), {}
        with self._sem:
            r = self.client.cancel_orders(list(ids))
        r = r if isinstance(r, dict) else {}
        canceled = set(r.get("canceled") or [])
        nc = r.get("not_canceled") or r.get("notCanceled") or {}
        return canceled, {k: str(v) for k, v in nc.items()} if isinstance(nc, dict) else {}

    def cancel_asset(self, token: str) -> bool:
        try:
            with self._sem:
                self.client.cancel_market_orders(self._mod.OrderMarketCancelParams(asset_id=token))
            return True
        except Exception as e:
            log.error("отмена по токену %s: %s", token[:10], e)
            return False

    def heartbeat(self, hid: str) -> Tuple[bool, str, str]:
        """POST /v1/heartbeats. Ответ: (ok, id для следующего, ошибка).
        Не через семафор: heartbeat не должен ждать очередь медленных запросов."""
        try:
            r = self.client.post_heartbeat(hid)
        except Exception as e:
            msg = getattr(e, "error_msg", None) or str(e)
            return False, _hb_id(msg) or "", str(msg)[:200]
        if isinstance(r, dict) and r.get("success") is False:
            return False, _hb_id(r) or "", str(r)[:200]
        return True, _hb_id(r) or hid, ""

    def open_orders(self, token: Optional[str] = None) -> Optional[List[dict]]:
        try:
            p = self._mod.OpenOrderParams(asset_id=token) if token else None
            with self._sem:
                return list(self.client.get_open_orders(p))
        except Exception as e:
            log.warning("открытые ордера: %s", e)
            return None

    def order(self, oid: str) -> Optional[dict]:
        try:
            with self._sem:
                r = self.client.get_order(oid)
            return r if isinstance(r, dict) else None
        except Exception as e:
            log.warning("ордер %s: %s", oid[:12], e)
            return None

    @staticmethod
    def matched(resp: Any) -> Tuple[float, float]:
        """(USDC, шары) по ответу. При BUY makingAmount = USDC, takingAmount = шары."""
        if not isinstance(resp, dict) or resp.get("success") is False:
            return 0.0, 0.0

        def num(*keys):
            for k in keys:
                v = resp.get(k)
                if v not in (None, ""):
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
            return None
        usdc = num("making_amount", "makingAmount")
        shares = num("taking_amount", "takingAmount")
        if usdc is None or shares is None:
            return 0.0, 0.0
        return max(0.0, usdc), max(0.0, shares)


# ----------------------------------------------------------------------------- рынки

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


class Discovery:
    def __init__(self):
        self.sess = requests.Session()
        self.sess.headers["User-Agent"] = "rozi/2.0"
        self.cache: Dict[str, Market] = {}

    @staticmethod
    def slug(asset: str, tf: int, start: int) -> str:
        return f"{asset}-updown-{tf}m-{start}"

    def _rows(self, slug: str, closed: bool = False) -> list:
        params = {"slug": slug}
        if closed:
            params["closed"] = "true"      # без него gamma не отдаёт закрытые рынки (пустой список)
        r = self.sess.get(f"{Cfg.GAMMA}/markets", params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("data", [])

    def fetch(self, asset: str, tf: int, start: int) -> Optional[Market]:
        slug = self.slug(asset, tf, start)
        if slug in self.cache:
            return self.cache[slug]
        try:
            rows = self._rows(slug)
            m = self._parse(rows[0], asset, tf, start, slug) if rows else None
        except Exception as e:
            log.warning("gamma %s: %s", slug, e)
            return None
        if m is not None:
            self.cache[slug] = m
            log.info("рынок найден %s tick=%s", slug, m.tick)
        return m

    def resolution(self, slug: str) -> Optional[str]:
        """'Up' / 'Down', когда рынок разрешён, иначе None."""
        try:
            rows = self._rows(slug, closed=True) or self._rows(slug)
            if not rows:
                return None
            outs, prices = rows[0].get("outcomes"), rows[0].get("outcomePrices")
            if isinstance(outs, str):
                outs = json.loads(outs)
            if isinstance(prices, str):
                prices = json.loads(prices)
            if not outs or not prices:
                return None
            px = [float(p) for p in prices]
            if max(px) < 0.99:
                return None
            return str(outs[px.index(max(px))])
        except Exception as e:
            log.debug("резолв %s: %s", slug, e)
            return None

    @staticmethod
    def _parse(row: dict, asset: str, tf: int, start: int, slug: str) -> Optional[Market]:
        try:
            ids, outs = row.get("clobTokenIds"), row.get("outcomes")
            if isinstance(ids, str):
                ids = json.loads(ids)
            if isinstance(outs, str):
                outs = json.loads(outs)
            if not ids or not outs or len(ids) != len(outs):
                return None
            tokens = {str(o): str(t) for o, t in zip(outs, ids)}
            if "Up" not in tokens or "Down" not in tokens:
                return None
            tick = row.get("orderPriceMinTickSize") or Cfg.TICK_DEFAULT
            return Market(asset, tf, start, slug, tokens, str(tick))
        except Exception as e:
            log.warning("разбор рынка %s: %s", slug, e)
            return None

    def prune(self) -> None:
        now = time.time()
        for slug in list(self.cache):
            try:
                if int(slug.rsplit("-", 1)[1]) < now - 7200:
                    self.cache.pop(slug, None)
            except (IndexError, ValueError):
                pass


# ----------------------------------------------------------------------------- Binance

class Binance:
    """Поток aggTrade по всем активам + минутная статистика (σ, средний объём)."""

    def __init__(self, assets: List[str]):
        self.assets = assets
        self.sym = {a: f"{a.upper()}USDT" for a in assets}
        self.price: Dict[str, float] = {}
        self.vol60: Dict[str, Deque[Tuple[float, float]]] = {a: collections.deque() for a in assets}
        self.vol60_sum: Dict[str, float] = {a: 0.0 for a in assets}
        self.stats: Dict[str, Tuple[float, float]] = {}      # σ за минуту, ср. объём за минуту
        self.sig_fair: Dict[str, float] = {}                 # σ минуты за FAIR_SIGMA_MIN (Binance-фильтр)
        self.subs: Dict[str, List["Engine"]] = {a: [] for a in assets}
        self.lag_ms: Dict[str, float] = {}
        self.sess = requests.Session()
        self.sec: Dict[str, Dict[int, float]] = {a: {} for a in assets}   # секунда -> последняя цена
        self.sec_first: Dict[str, int] = {}
        # фильтр экстремума BTC: минутные high/low из REST (раз в минуту, в фоне) и из потока сделок (вживую);
        # на сигнале — только чтение памяти, без сетевых запросов
        self.ext_rest: Dict[str, Dict[int, Tuple[float, float]]] = {a: {} for a in assets}
        self.ext_live: Dict[str, Dict[int, List[float]]] = {a: {} for a in assets}
        self.amp_rest: Dict[str, Dict[int, Tuple[float, float]]] = {a: {} for a in assets}   # старт свечи -> (high, low), закрытые
        self.streak_5m: Dict[str, List[Tuple[int, Optional[str]]]] = {a: [] for a in assets}  # (open_time, Up/Down/None), только закрытые
        self.last_trade_t: Dict[str, float] = {}
        self.resync = False            # поток переподключён — пора быстро обновить REST-кэш

    # --- посекундная история для фильтра подтверждения ----------------------------------
    def _sec_series(self, asset: str, s0: int, s1: int) -> Optional[List[float]]:
        d = self.sec.get(asset) or {}
        last = None
        for k in range(s0 - 1, s0 - 121, -1):           # цена, действовавшая на начало
            if k in d:
                last = d[k]
                break
        out = []
        for k in range(s0, s1 + 1):
            if k in d:
                last = d[k]
            out.append(last)
        if any(x is None for x in out):
            return None
        return out

    def confirm_path(self, asset: str, start: float, end: float, side: str, s_from: int, s_to: int) -> Optional[dict]:
        """Справедливая цена стороны по Binance (TWAP-модель) на секундах [s_from, s_to]."""
        first = self.sec_first.get(asset)
        if first is None or s_to - first < Cfg.BN_CONFIRM_MIN_S or first > int(start) - 61:
            return None
        n_sig = Cfg.BN_CONFIRM_SIG_S
        hist = self._sec_series(asset, max(first, s_to - n_sig), s_to)
        pre = self._sec_series(asset, int(start) - 60, int(start) - 1)
        if not hist or not pre or len(hist) < 60:
            return None
        rets = [math.log(hist[i] / hist[i - 1]) for i in range(1, len(hist)) if hist[i - 1] > 0]
        if len(rets) < 30:
            return None
        mu = sum(rets) / len(rets)
        sig = math.sqrt(sum((x - mu) ** 2 for x in rets) / max(1, len(rets) - 1)) * 1e4
        if sig <= 0:
            return None
        strike = sum(pre) / len(pre)
        seg = self._sec_series(asset, s_from, s_to)
        if not seg:
            return None
        end_i = int(end)
        fair = []
        for k, px in zip(range(s_from, s_to + 1), seg):
            tau = end_i - k
            if tau >= 60:
                ehat, var = px, tau - 40.0
            else:
                kn = 60 - max(tau, 0)
                known = self._sec_series(asset, end_i - 60, k) or [px]
                ehat = (sum(known) / len(known) * kn + px * max(tau, 0)) / 60.0
                var = max(tau, 0) ** 3 / (3 * 3600.0)
            x = math.log(ehat / strike) * 1e4
            z = (x + Cfg.BN_CONFIRM_B) / (Cfg.BN_CONFIRM_K * sig * math.sqrt(max(var, 0.5)))
            up = 0.5 * (1 + math.erf(z / math.sqrt(2)))
            fair.append(up if side == "Up" else 1 - up)
        return {"fair": fair, "sig_bp": sig, "strike": strike}

    def _klines(self, asset: str, limit: int, start_ms: Optional[int] = None) -> list:
        params: Dict[str, Any] = {"symbol": self.sym[asset], "interval": "1m", "limit": limit}
        if start_ms is not None:
            params["startTime"] = start_ms
        r = self.sess.get(f"{Cfg.BINANCE_REST}/api/v3/klines", params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def refresh_stats(self, asset: str) -> None:
        n = Cfg.SIGMA_WIN_MIN
        nf = Cfg.FAIR_SIGMA_MIN if Cfg.TRAP_FAIR_MIN > -1 else 0
        allrows = self._klines(asset, min(1000, max(n, nf) + 3))
        cur_min_ms = int(time.time() // 60) * 60_000
        allrows = [k for k in allrows if int(k[0]) < cur_min_ms]          # только закрытые
        if nf:
            fc = [float(k[4]) for k in allrows[-(nf + 1):]]
            if len(fc) >= max(30, nf // 2):
                fr = [math.log(fc[i] / fc[i - 1]) for i in range(1, len(fc))]
                fm = sum(fr) / len(fr)
                fs = math.sqrt(sum((x - fm) ** 2 for x in fr) / max(1, len(fr) - 1))
                self.sig_fair[asset] = max(fs, Cfg.SIGMA_MIN_BP * 1e-4)
        rows = allrows[-(n + 1):]
        if len(rows) < max(10, n // 2):
            return
        closes = [float(k[4]) for k in rows]
        vols = [float(k[5]) for k in rows[1:]]
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        mu = sum(rets) / len(rets)
        sig = math.sqrt(sum((x - mu) ** 2 for x in rets) / max(1, len(rets) - 1))
        self.stats[asset] = (max(sig, Cfg.SIGMA_MIN_BP * 1e-4), sum(vols) / len(vols))

    def open_at(self, asset: str, start: int) -> Optional[float]:
        """Открытие минуты start по REST — если бот подключился уже внутри окна."""
        try:
            rows = self._klines(asset, 1, start * 1000)
            if rows and int(rows[0][0]) == start * 1000:
                return float(rows[0][1])
        except Exception as e:
            log.warning("binance open %s: %s", asset, e)
        return None

    def refresh_extreme(self, asset: str) -> None:
        """Фон (поток): минутные свечи Binance REST для фильтра экстремума. Словарь подменяется целиком."""
        n = max(2, Cfg.BTC_EXTREME_N)
        tf_min = max(1, Cfg.BTC_EXTREME_TF)
        rows = self._klines(asset, min(1000, n * tf_min + 2 * tf_min + 10))
        self.ext_rest[asset] = {int(k[0]) // 1000: (float(k[2]), float(k[3])) for k in rows}

    def refresh_amp(self, asset: str) -> None:
        """Фон: закрытые свечи ТФ BTC_AMP_TF для фильтра амплитуды. Запрос — только когда в кэше нет последней закрытой свечи."""
        tf_min = max(1, Cfg.BTC_AMP_TF)
        tf_s = tf_min * 60
        need = int(time.time()) // tf_s * tf_s - tf_s
        have = self.amp_rest.get(asset) or {}
        if need in have and not self.resync:
            return
        limit = min(1000, Cfg.BTC_AMP_REF_N + max(1, Cfg.BTC_AMP_LOOKBACK) + 4)
        r = self.sess.get(f"{Cfg.BINANCE_REST}/api/v3/klines",
                          params={"symbol": self.sym[asset], "interval": f"{tf_min}m", "limit": limit}, timeout=10)
        r.raise_for_status()
        now = time.time()
        self.amp_rest[asset] = {int(k[0]) // 1000: (float(k[2]), float(k[3])) for k in r.json()
                                if int(k[0]) // 1000 + tf_s <= now}

    def amp_ok(self, asset: str, now_ts: float) -> Optional[dict]:
        """Была ли амплитудная свеча в последних BTC_AMP_LOOKBACK закрытых свечах перед текущим окном. Только память."""
        if Cfg.BTC_AMP == "off" or asset not in Cfg.BTC_AMP_ASSETS:
            return None
        tf_min = max(1, Cfg.BTC_AMP_TF)
        tf_s = tf_min * 60
        nref = max(4, Cfg.BTC_AMP_REF_N)
        lb = max(1, Cfg.BTC_AMP_LOOKBACK)

        def fail(why: str) -> dict:
            log.warning("btc_amp %s: %s (политика %s)", asset, why, Cfg.BTC_AMP_ON_ERROR)
            return dict(btc_amp_ok=(False if Cfg.BTC_AMP_ON_ERROR == "skip" else None), btc_amp_err=why,
                        btc_amp_tf=tf_min, btc_amp_n=lb)

        rest = self.amp_rest.get(asset) or {}
        cur = int(now_ts) // tf_s * tf_s
        xs: List[Optional[float]] = []
        ref0 = None
        for j in range(1, lb + 1):
            cs = cur - j * tf_s
            c = rest.get(cs)
            if c is None:
                xs.append(None)
                continue
            rs = [rest[k][0] - rest[k][1] for k in (cs - i * tf_s for i in range(1, nref + 1)) if k in rest]
            if len(rs) < nref // 2:
                xs.append(None)
                continue
            rs.sort()
            m = len(rs)
            med = rs[m // 2] if m % 2 else 0.5 * (rs[m // 2 - 1] + rs[m // 2])
            if med <= 0:
                xs.append(None)
                continue
            xs.append((c[0] - c[1]) / med)
            if j == 1:
                ref0 = med
        got = [x for x in xs if x is not None]
        if not got:
            return fail("нет свечей/истории для опорного диапазона")
        best = max(got)
        d = dict(btc_amp_ok=best >= Cfg.BTC_AMP_MULT - 1e-9, btc_amp_x=round(best, 3), btc_amp_tf=tf_min,
                 btc_amp_n=lb, btc_amp_mult=Cfg.BTC_AMP_MULT, btc_amp_ref=round(ref0, 2) if ref0 else None)
        for j, x in enumerate(xs, 1):
            d[f"btc_amp_x{j}"] = None if x is None else round(x, 3)
        return d

    def extreme_ok(self, asset: str, side: str, now_ts: float) -> Optional[dict]:
        """Локальный экстремум BTC за BTC_EXTREME_N свечей ТФ BTC_EXTREME_TF — только по данным в памяти.
        Минутные high/low = объединение REST (история) и потока сделок (текущая минута, дыры REST).
        ok=True означает «плохой» экстремум: Up на пике или Down на дне. При нехватке данных —
        btc_ext_err и политика BTC_EXTREME_ON_ERROR (pass: ok=None, сигнал идёт; skip: ok=False)."""
        if Cfg.BTC_EXTREME == "off" or asset not in Cfg.BTC_EXTREME_ASSETS:
            return None
        n = max(2, Cfg.BTC_EXTREME_N)
        tf_min = max(1, Cfg.BTC_EXTREME_TF)
        tf_s = tf_min * 60

        def fail(why: str) -> dict:
            log.warning("btc_extreme %s: %s (политика %s)", asset, why, Cfg.BTC_EXTREME_ON_ERROR)
            return dict(btc_ext_ok=(False if Cfg.BTC_EXTREME_ON_ERROR == "skip" else None),
                        btc_ext_err=why, btc_ext_n=n, btc_ext_tf=tf_min)

        lt = self.last_trade_t.get(asset)
        px = self.price.get(asset)
        if lt is None or px is None or time.time() - lt > Cfg.BTC_EXTREME_MAX_LAG_S:
            return fail("поток Binance устарел")
        rest = self.ext_rest.get(asset) or {}
        live = self.ext_live.get(asset) or {}
        cur_min = int(now_ts) // 60 * 60
        cur_start = int(now_ts) // tf_s * tf_s
        first_start = cur_start - (n - 1) * tf_s
        highs: List[float] = []
        lows: List[float] = []
        for b in range(n):
            bs = first_start + b * tf_s
            last_min = cur_min if b == n - 1 else bs + tf_s - 60
            exp = (last_min - bs) // 60 + 1
            h, l, cov = -math.inf, math.inf, 0
            for m in range(bs, last_min + 1, 60):
                got = False
                for src in (rest.get(m), live.get(m)):
                    if src:
                        h, l, got = max(h, src[0]), min(l, src[1]), True
                cov += got
            if b == n - 1:                      # текущая свеча: последняя цена потока тоже учтена
                h, l, cov = max(h, px), min(l, px), max(cov, 1)
            if cov < exp - 1 or h == -math.inf:
                return fail(f"нет минут в свече {b + 1}/{n}: {cov}/{exp}")
            highs.append(h)
            lows.append(l)
        is_peak = highs[-1] >= max(highs) - 1e-9
        is_bot = lows[-1] <= min(lows) + 1e-9
        ok = (side == "Up" and is_peak) or (side == "Down" and is_bot)
        return dict(btc_ext_peak=is_peak, btc_ext_bot=is_bot, btc_ext_ok=ok,
                    btc_ext_n=n, btc_ext_tf=tf_min,
                    btc_ext_chi=highs[-1], btc_ext_clo=lows[-1],
                    btc_ext_phi=max(highs[:-1]), btc_ext_plo=min(lows[:-1]))

    def refresh_streak(self, asset: str) -> None:
        """Обновить историю закрытых 5m свечей Binance для streak-фильтра."""
        limit = max(10, min(1000, Cfg.STREAK_LOOKBACK))
        r = self.sess.get(
            f"{Cfg.BINANCE_REST}/api/v3/klines",
            params={"symbol": self.sym[asset], "interval": "5m", "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        now_ms = int(time.time() * 1000)
        rows = []
        for k in r.json():
            try:
                open_ms, close_ms = int(k[0]), int(k[6])
                if close_ms >= now_ms:       # незакрытую свечу никогда не используем
                    continue
                op, cl = float(k[1]), float(k[4])
                direction = "Up" if cl > op else ("Down" if cl < op else None)
                rows.append((open_ms // 1000, direction))
            except (IndexError, TypeError, ValueError):
                continue
        rows.sort(key=lambda x: x[0])

        # Если в основном журнале уже есть реальные резолвы Polymarket 5m,
        # они приоритетнее направления Binance-свечи. Binance закрывает только пробелы.
        resolved_5m: Dict[int, str] = {}
        try:
            jp = Path(Cfg.JOURNAL)
            if jp.is_file():
                slug_prefix = f"{asset}-updown-5m-"
                with jp.open() as jf:
                    for line in jf:
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        if rec.get("event") != "winner":
                            continue
                        slug = str(rec.get("slug") or "")
                        if not slug.startswith(slug_prefix):
                            continue
                        try:
                            start_ts = int(slug.rsplit("-", 1)[1])
                        except (ValueError, IndexError):
                            continue
                        winner = rec.get("winner")
                        if winner in ("Up", "Down"):
                            resolved_5m[start_ts] = winner
        except Exception as e:
            log.warning("streak journal %s: %s", asset, e)

        if resolved_5m:
            rows = [(ts, resolved_5m.get(ts, direction)) for ts, direction in rows]
        self.streak_5m[asset] = rows[-limit:]

    def streak_check(self, asset: str, side: str, now_ts: float) -> dict:
        """True только при STREAK_5M_MIN подряд закрытых 5m свечах в сторону ставки.
        При отсутствии/устаревании данных применяется STREAK_ON_ERROR."""
        rows = self.streak_5m.get(asset) or []
        min_n = max(1, Cfg.STREAK_5M_MIN)

        def fail(why: str) -> dict:
            passed = Cfg.STREAK_ON_ERROR != "skip"
            return {
                "streak_5": None, "streak_side": None, "streak_align": None,
                "streak_pass": passed, "streak_err": why,
                "streak_required": min_n,
            }

        if len(rows) < min_n:
            return fail(f"недостаточно 5m свечей: {len(rows)}/{min_n}")

        # Последняя закрытая свеча должна быть свежей. Нормальный возраст — до 5 минут;
        # 10 минут допускаем как запас на публикацию/REST, но не используем старую историю.
        last_open, last_dir = rows[-1]
        last_close = last_open + 300
        age = now_ts - last_close
        if age < -2 or age > 600:
            return fail(f"последняя закрытая 5m свеча устарела: age={age:.0f}s")

        # Проверяем непрерывность последовательности: пропуск свечи обрывает серию.
        run = 0
        run_side = None
        expected_open = last_open
        for open_ts, direction in reversed(rows):
            if expected_open - open_ts not in (0, 300):
                break
            if direction not in ("Up", "Down"):
                break
            if run_side is None:
                run_side = direction
            if direction != run_side:
                break
            run += 1
            expected_open = open_ts
        align = run_side == side
        passed = bool(align and run >= min_n)
        return {
            "streak_5": run, "streak_side": run_side,
            "streak_align": align, "streak_pass": passed,
            "streak_err": None, "streak_required": min_n,
            "streak_last_open": last_open,
        }

    async def stats_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.resync = False
            failed = False
            for a in self.assets:
                try:
                    await asyncio.to_thread(self.refresh_stats, a)
                except Exception as e:
                    failed = True
                    log.warning("binance klines %s: %s", a, e)
                if Cfg.STREAK_FILTER == "on" and a in Cfg.STREAK_ASSETS:
                    try:
                        await asyncio.to_thread(self.refresh_streak, a)
                    except Exception as e:
                        failed = True
                        log.warning("streak klines %s: %s", a, e)
                if Cfg.BTC_EXTREME != "off" and a in Cfg.BTC_EXTREME_ASSETS:
                    try:
                        await asyncio.to_thread(self.refresh_extreme, a)
                    except Exception as e:
                        failed = True
                        log.warning("btc_extreme klines %s: %s", a, e)
                if Cfg.BTC_AMP != "off" and a in Cfg.BTC_AMP_ASSETS:
                    try:
                        await asyncio.to_thread(self.refresh_amp, a)
                    except Exception as e:
                        failed = True
                        log.warning("btc_amp klines %s: %s", a, e)
            delay = (61.5 - time.time() % 60) % 60          # через 1.5с после закрытия минуты
            if failed or self.resync:                       # сбой REST или переподключение потока — не ждём минуту
                delay = min(delay, 5.0)
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(0.5, delay))
            except asyncio.TimeoutError:
                pass

    def vol_ratio(self, asset: str) -> Optional[float]:
        st = self.stats.get(asset)
        if not st or st[1] <= 0:
            return None
        return self.vol60_sum[asset] / st[1]

    def on_trade(self, asset: str, px: float, qty: float, t: float) -> None:
        self.price[asset] = px
        self.last_trade_t[asset] = time.time()
        if Cfg.BTC_EXTREME != "off" and asset in Cfg.BTC_EXTREME_ASSETS:
            mn = int(t) // 60 * 60
            ld = self.ext_live[asset]
            r = ld.get(mn)
            if r is None:
                ld[mn] = [px, px]
                keep = (max(2, Cfg.BTC_EXTREME_N) + 2) * max(1, Cfg.BTC_EXTREME_TF) * 60 + 600
                for k in [k for k in ld if k < mn - keep]:
                    del ld[k]
            else:
                if px > r[0]:
                    r[0] = px
                if px < r[1]:
                    r[1] = px
        if Cfg.BN_CONFIRM != "off":
            sec = int(t)
            d = self.sec[asset]
            if not d:
                self.sec_first[asset] = sec
            d[sec] = px
            if sec % 60 == 0 and len(d) > 1800:
                for k in [k for k in d if k < sec - 1500]:
                    del d[k]
        dq = self.vol60[asset]
        dq.append((t, qty))
        self.vol60_sum[asset] += qty
        while dq and dq[0][0] < t - 60.0:
            self.vol60_sum[asset] -= dq.popleft()[1]
        for eng in list(self.subs[asset]):
            try:
                eng.on_price(px, t)
            except Exception as e:
                log.error("on_price %s: %s", eng.m.slug, e)

    async def ws_loop(self, stop: asyncio.Event) -> None:
        streams = "/".join(f"{s.lower()}@aggTrade" for s in self.sym.values())
        url = f"{Cfg.BINANCE_WS}?streams={streams}"
        back = {s: a for a, s in self.sym.items()}
        while not stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, close_timeout=5) as ws:
                    log.info("Binance поток подключён: %s", ", ".join(self.sym.values()))
                    self.resync = True
                    while not stop.is_set():
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(3.0, Cfg.BN_STALL_S)))
                        d = msg.get("data") or msg
                        a = back.get(d.get("s"))
                        if a is None:
                            continue
                        t = d["T"] / 1000.0
                        self.lag_ms[a] = (time.time() - t) * 1000
                        self.on_trade(a, float(d["p"]), float(d["q"]), t)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Binance поток: %s — переподключаюсь", e)
                await asyncio.sleep(1.0)


# ----------------------------------------------------------------------------- бюджет

class Budget:
    """Потрачено + в полёте за текущий час UTC по всем рынкам."""

    def __init__(self):
        self.spent: Dict[int, float] = collections.defaultdict(float)
        self.pending = 0.0

    def add(self, usdc: float) -> None:
        self.spent[int(time.time() // 3600)] += usdc

    def room(self) -> float:
        if Cfg.MAX_PER_HOUR <= 0:
            return float("inf")
        return max(0.0, Cfg.MAX_PER_HOUR - self.spent[int(time.time() // 3600)] - self.pending)


class Stats:
    def __init__(self):
        self.n = self.wins = 0
        self.spent = self.pnl = 0.0
        self.sh_n = self.sh_wins = 0
        self.sh_size = self.sh_pnl = 0.0

    def add(self, usdc: float, pnl: float, won: bool) -> None:
        self.n += 1
        self.wins += int(won)
        self.spent += usdc
        self.pnl += pnl

    def add_shadow(self, size: float, pnl: Optional[float], won: bool) -> None:
        if pnl is None:
            return
        self.sh_n += 1
        self.sh_wins += int(won)
        self.sh_size += size
        self.sh_pnl += pnl

    def line(self) -> str:
        return (f"сессия: позиций {self.n}, побед {self.wins}, вложено ${self.spent:.2f}, "
                f"P&L ${self.pnl:+.2f} | полный налив по аску: сигналов {self.sh_n}, "
                f"побед {self.sh_wins}, ${self.sh_size:.2f}, P&L ${self.sh_pnl:+.2f}")


# ----------------------------------------------------------------------------- ядро

TRAP_TERMINAL = {"MATCHED", "CANCELED", "CANCELLED", "CANCELED_MARKET_RESOLVED", "EXPIRED",
                 "INVALID", "UNMATCHED"}
TRAPS: Dict[str, Tuple[Any, Any, Any]] = {}      # id лимитки -> (Engine, Side, TierState)


def _bg_done(t: "asyncio.Task") -> None:
    """Ошибка фоновой задачи — в лог с трассой, без «Task exception was never retrieved»."""
    if t.cancelled():
        return
    e = t.exception()
    if e is not None:
        log.error("фоновая задача %s упала: %r", t.get_coro().__qualname__ if t.get_coro() else "?", e,
                  exc_info=(type(e), e, e.__traceback__))


def _oid(o: dict) -> Optional[str]:
    return o.get("id") or o.get("orderID") or o.get("order_id")


def _matched(o: dict) -> float:
    try:
        return float(o.get("size_matched") or o.get("sizeMatched") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _terminal(o: dict) -> bool:
    st = str(o.get("status") or "").upper()
    if st in TRAP_TERMINAL:
        return True
    try:
        orig = float(o.get("original_size") or o.get("originalSize") or 0.0)
    except (TypeError, ValueError):
        orig = 0.0
    return orig > 0 and _matched(o) >= orig - 1e-9


@dataclass
class TierState:
    tier: Tier
    state: str = "idle"            # idle -> armed -> done | dead | frozen
    t_arm: float = 0.0
    takes: int = 0
    last_take: float = 0.0
    inflight: bool = False
    spent: float = 0.0
    shares: float = 0.0
    signed: Any = None             # предподписанный первый выстрел
    signed_key: Tuple = ()
    rejects: int = 0               # отказов подряд — для паузы
    ask0: Optional[float] = None   # аск книги на сигнале — для shadow
    triggered: bool = False
    # режим trap: лимитка-ловушка
    trap_state: str = "idle"       # idle | placing | resting | cancelling | done | frozen
    oid: Optional[str] = None      # id лимитки на бирже
    trap_p: float = 0.0            # цена лимитки
    trap_size: float = 0.0         # шаров в лимитке
    trap_matched: float = 0.0      # налито по этой лимитке (шары, по данным биржи)
    trap_res: float = 0.0          # зарезервировано USDC под лимитку
    trap_off_t: float = 0.0        # когда лимитка снята — пауза перед новой
    trap_busy: bool = False        # идёт процедура снятия
    trap_stuck: bool = False       # снятие не подтверждено — поллер повторит
    trap_miss: int = 0             # раз подряд ордера нет ни среди открытых, ни по id
    trap_rejects: int = 0          # отказов постановки подряд
    trap_next_t: float = 0.0       # раньше этого времени не ставить (пауза после отказа)
    trap_last_err: str = ""
    trap_seen: Dict[str, Tuple[float, float]] = field(default_factory=dict)  # id -> (цена, налито)
    trap_dead: bool = False        # первое касание уровня было медленным — ступень мертва
    fair_block: bool = False       # Binance-фильтр сейчас не пускает ступень


@dataclass
class Side:
    name: str
    token: str
    ask: Optional[float] = None
    ask_t: float = 0.0
    bid: Optional[float] = None
    mid: Optional[float] = None    # mid стакана этой стороны — по нему сигнал
    busy: bool = False             # на стороне летит ордер — второго не шлём
    hist: Deque[Tuple[float, float]] = field(default_factory=collections.deque)  # (t, mid)
    win_max: float = 0.0           # максимум mid стороны с начала окна (фильтр PRE_MAX_MIN)
    tiers: List[TierState] = field(default_factory=list)
    add: Optional[TierState] = None  # докупки (state = "add"), тоже лежит в tiers
    anchor: Optional[float] = None   # максимальная цена покупки на стороне (вход или докупка)
    anchor_t: float = 0.0            # время первого входа
    add_n: int = 0                   # сколько докупок сделано
    add_blocked: bool = False        # порог пройден, но аск вне допуска — уже записали
    # уровни книги токена стороны (DEPTH_LOG): цена -> объём долей; lv_ok — был снимок book
    asks: Dict[float, float] = field(default_factory=dict)
    bids: Dict[float, float] = field(default_factory=dict)
    lv_ok: bool = False

    @property
    def spent(self) -> float:
        return sum(t.spent for t in self.tiers)


class Engine:
    def __init__(self, mkt: Market, clob: Clob, journal: Journal, bn: Binance, budget: Budget,
                 filled: Dict[tuple, Tuple[float, float]], done: set):
        self.m = mkt
        self.clob = clob
        self.j = journal
        self.bn = bn
        self.budget = budget
        self.sides: Dict[str, Side] = {}
        self.by_token: Dict[str, Side] = {}
        # объём ступеней фиксируется на окно при создании движка (за PREROLL_S до старта)
        self.tiers_sized = [dataclasses.replace(t, size=stake_usdc(t.cap)) if Cfg.STAKE_PCT > 0 else t
                            for t in Cfg.TIERS]
        self.deposit = EQUITY.value if EQUITY is not None else Cfg.DEPOSIT
        for nm in ("Up", "Down"):
            s = Side(nm, mkt.tokens[nm])
            for t in self.tiers_sized:
                ts = TierState(t)
                k = (mkt.slug, nm, t.name)
                if k in filled:
                    ts.spent, ts.shares = filled[k]
                if k in done or ts.spent > 0:
                    ts.state = "done"              # рестарт: ступень уже отработана
                    ts.trap_state = "done"
                s.tiers.append(ts)
            add = TierState(Tier(0.0, 0.0, Cfg.ADD_MAX_PX, Cfg.ADD_USDC, label="add"),
                            state="add")
            ka = (mkt.slug, nm, "add")
            if ka in filled:
                add.spent, add.shares = filled[ka]
            s.add = add
            s.tiers.append(add)
            # рестарт: цена последней покупки неизвестна (anchor=None) — докупок по стороне нет
            self.sides[nm] = s
            self.by_token[s.token] = s
        self.pre_px: Optional[float] = None
        self.open: Optional[float] = None
        self.close: Optional[float] = None
        self.logged_open = False
        self.hour_ok = not Cfg.HOURS or (mkt.start // 3600) % 24 in Cfg.HOURS
        self.max_elapsed = (1 - Cfg.REM_MIN_FRAC) * mkt.length
        self.reserved = 0.0                        # USDC в летящих ордерах
        self._open_pending = False
        n = int(mkt.length)
        self.path_b: List[Optional[float]] = [None] * n   # bid/ask токена Up по секундам окна
        self.path_a: List[Optional[float]] = [None] * n
        self.up_last: Optional[Tuple[float, Optional[float], Optional[float]]] = None  # (t, bid, ask)
        self.up_tail: Deque[Tuple[float, Optional[float], Optional[float]]] = collections.deque(maxlen=5000)

    # --- служебное --------------------------------------------------------------
    @staticmethod
    def _bg(coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        t.add_done_callback(_bg_done)

    async def _rec(self, event: str, side: Optional[Side] = None,
                   ts: Optional[TierState] = None, **kw) -> None:
        rec: Dict[str, Any] = {"event": event, "slug": self.m.slug}
        if side is not None:
            rec["side"] = side.name
        if ts is not None:
            rec["tier"] = ts.tier.name
        rec.update(kw)
        await self.j.write(rec)

    def window_spent(self) -> float:
        return sum(s.spent for s in self.sides.values()) + self.reserved

    def cap_of(self, ts: TierState) -> float:
        return max(tick_floor(ts.tier.cap, self.m.tick), float(self.m.tick))

    def ceil_of(self, ts: TierState, now: Optional[float] = None) -> float:
        """Текущий потолок погони: базовый + ESC_STEP за каждые ESC_EVERY_S, не выше
        базового + ESC_MAX и не выше 1 − тик."""
        base = self.cap_of(ts)
        if ts.t_arm <= 0 or Cfg.ESC_STEP <= 0 or Cfg.ESC_EVERY_S <= 0:
            return base
        now = time.time() if now is None else now
        steps = int(max(0.0, now - ts.t_arm) // Cfg.ESC_EVERY_S)
        raw = min(ts.tier.cap + steps * Cfg.ESC_STEP, ts.tier.cap + Cfg.ESC_MAX)
        top = 1.0 - float(self.m.tick)
        return round(min(max(tick_floor(raw, self.m.tick), base), top), 6)

    def chase_over(self, ts: TierState, now: float) -> Optional[str]:
        """Причина конца погони или None."""
        left = ts.tier.size - ts.spent
        if left < Cfg.MIN_NOTIONAL:
            # остаток меньше минимального ордера: добирать нельзя без перебора объёма
            return "filled" if left < 0.01 else "left_below_min"
        if now - ts.t_arm > Cfg.FILL_TIMEOUT_S:
            return "timeout"
        if now > self.m.end - Cfg.END_GUARD_S:
            return "window_end"
        if Cfg.MAX_TAKES > 0 and ts.takes >= Cfg.MAX_TAKES:
            return "max_takes"
        return None

    async def _end_chase(self, s: Side, ts: TierState, why: str) -> None:
        if ts.state != "armed":
            return
        ts.state = "done"
        await self._rec("chase_end", s, ts, why=why, takes=ts.takes, spent=round(ts.spent, 4),
                        shares=round(ts.shares, 4), need=ts.tier.size,
                        fill_pct=round(100 * ts.spent / ts.tier.size, 1))
        lvl = logging.INFO if why == "filled" else logging.WARNING
        log.log(lvl, "погоня %s %s %s: %s, налито $%.2f из $%.2f за %d попыток",
                self.m.slug, s.name, ts.tier.name, why, ts.spent, ts.tier.size, ts.takes)

    @staticmethod
    def side_p(s: Side) -> Optional[float]:
        return round(s.mid, 4) if s.mid is not None else None

    async def presign_all(self) -> None:
        """Первый выстрел каждой ступени подписан заранее — на сигнале только POST.
        Переподписываем при смене тика."""
        if not self.clob.can_presign or Cfg.ENTRY_MODE == "trap":
            return
        for s in self.sides.values():
            for ts in s.tiers:
                if ts.state != "idle":
                    continue
                key = (self.m.tick, ts.tier.size)
                if ts.signed is not None and ts.signed_key == key:
                    continue
                if ts.signed is None and ts.signed_key == key:
                    continue                       # уже пробовали с этим тиком
                ts.signed_key = key
                ts.signed = await asyncio.to_thread(self.clob.presign, s.token, ts.tier.size,
                                                    self.cap_of(ts), self.m.tick)

    # --- Binance: только открытие/закрытие окна (журнал) и объём (фильтр) -------
    def on_price(self, px: float, t: float) -> None:
        m = self.m
        if t < m.start:
            self.pre_px = px
            return
        if t >= m.end:
            if self.close is None:
                self.close = px
            return
        if self.open is None:
            if self.pre_px is not None:
                self.open = self.pre_px
            elif not self._open_pending:
                self._open_pending = True
                self._bg(self._fetch_open())
        if self.open is not None and not self.logged_open:
            self.logged_open = True
            st = self.bn.stats.get(m.asset)
            self._bg(self._rec("window_open", open=self.open,
                               avg_vol=round(st[1], 4) if st else None, hour_ok=self.hour_ok))

    async def _fetch_open(self) -> None:
        o = await asyncio.to_thread(self.bn.open_at, self.m.asset, self.m.start)
        if o is not None:
            self.open = o
            await self._rec("open_from_rest", open=o)
        else:
            self._open_pending = False

    def bn_adverse_bp(self, s: Side) -> Optional[float]:
        """На сколько bp Binance ушёл от открытия окна ПРОТИВ стороны (>0 = против)."""
        px = self.bn.price.get(self.m.asset)
        if not px or not self.open or px <= 0 or self.open <= 0:
            return None
        sg = 1 if s.name == "Up" else -1
        return round(-sg * math.log(px / self.open) * 1e4, 2)

    def bn_ok(self, s: Side) -> bool:
        if Cfg.BN_MAX_ADVERSE_BP <= 0:
            return True
        adv = self.bn_adverse_bp(s)
        return adv is None or adv <= Cfg.BN_MAX_ADVERSE_BP + 1e-9

    def _first_touch(self, s: Side, ts: TierState, p: float, t: float, elapsed: float) -> None:
        tier = ts.tier
        pmax = max((x[1] for x in s.hist if x[0] >= t - Cfg.FAST_S), default=p)
        vr = self.bn.vol_ratio(self.m.asset)
        reason = None
        if not self.hour_ok:
            reason = "hour"
        elif elapsed > self.max_elapsed:
            reason = "late"
        elif pmax < tier.ref:
            reason = "slow"
        elif _pre_on(tier) and s.win_max < _pre_min(tier) - 1e-9:
            reason = "pre_max"
        elif not self.bn_ok(s):
            reason = "binance"
        elif Cfg.VOL_MULT > 0 and (vr is None or vr < Cfg.VOL_MULT):
            reason = "volume"
        elif Cfg.ONE_SIDE_PER_WINDOW and any(
                o.spent > 0 for o in self.sides.values() if o is not s):
            reason = "other_side"
        elif self.window_spent() + Cfg.MIN_NOTIONAL > Cfg.MAX_PER_WINDOW:
            reason = "window_budget"
        elif self.budget.room() < Cfg.MIN_NOTIONAL:
            reason = "hour_budget"
        streak = None
        if Cfg.STREAK_FILTER == "on" and self.m.asset in Cfg.STREAK_ASSETS:
            try:
                streak = self.bn.streak_check(self.m.asset, s.name, t)
            except Exception as e:
                streak = {
                    "streak_5": None, "streak_side": None, "streak_align": None,
                    "streak_pass": Cfg.STREAK_ON_ERROR != "skip",
                    "streak_err": str(e)[:120], "streak_required": Cfg.STREAK_5M_MIN,
                }
            if reason is None and not streak.get("streak_pass", True):
                reason = "streak"
        conf = None
        if Cfg.BN_CONFIRM in ("log", "on"):
            try:
                st = int(t)
                cp = self.bn.confirm_path(self.m.asset, self.m.start, self.m.end, s.name,
                                          st - int(Cfg.FAST_S) - Cfg.BN_CONFIRM_LOOK_S, st)
                if cp:
                    f = cp["fair"]
                    mn = min(f[-(Cfg.BN_CONFIRM_LOOK_S + 1):]); mx = max(f)
                    crash = mn <= tier.level + Cfg.BN_CONFIRM_TOL + 1e-9 and mx >= tier.ref - Cfg.BN_CONFIRM_TOL - 1e-9
                    conf = dict(bn_fair=round(f[-1], 4), bn_min=round(mn, 4), bn_max=round(mx, 4),
                                bn_sig_bp=round(cp["sig_bp"], 3), bn_crash=crash)
                else:
                    conf = dict(bn_crash=None)
            except Exception as e:
                conf = dict(bn_crash=None, bn_err=str(e)[:100])
            if (reason is None and Cfg.BN_CONFIRM == "on" and conf.get("bn_crash")
                    and self.m.asset in Cfg.BN_CONFIRM_ASSETS):
                reason = "bn_confirm"
        # фильтр локального экстремума BTC (Up только на пике, Down только на дне)
        ext = None
        if Cfg.BTC_EXTREME in ("log", "on") and self.m.asset in Cfg.BTC_EXTREME_ASSETS:
            try:
                ext = self.bn.extreme_ok(self.m.asset, s.name, t)
            except Exception as e:
                ext = dict(btc_ext_ok=None, btc_ext_err=str(e)[:100])
        # фильтр амплитудной свечи до окна
        amp = None
        if Cfg.BTC_AMP in ("log", "on") and self.m.asset in Cfg.BTC_AMP_ASSETS:
            try:
                amp = self.bn.amp_ok(self.m.asset, t)
            except Exception as e:
                amp = dict(btc_amp_ok=None, btc_amp_err=str(e)[:100])
        # решение по фильтрам в режиме on: or — пропуск, только если НИ ОДИН не прошёл; and — если не прошёл хоть один.
        # None (нет данных при политике pass) не блокирует
        act = []
        if Cfg.BTC_EXTREME == "on" and ext and self.m.asset in Cfg.BTC_EXTREME_ASSETS:
            act.append(("btc_extreme", ext.get("btc_ext_ok")))
        if Cfg.BTC_AMP == "on" and amp and self.m.asset in Cfg.BTC_AMP_ASSETS:
            act.append(("btc_amp", amp.get("btc_amp_ok")))
        pass_by = None
        if act:
            oks = [v for _, v in act]
            pass_by = "+".join(n[4:] for n, v in act if v) or None
            if reason is None:
                block = all(v is False for v in oks) if Cfg.BTC_FILTERS_MODE == "or" else any(v is False for v in oks)
                if block:
                    reason = act[0][0] if len(act) == 1 else "btc_filters"
        info = dict(p=round(p, 4), pmax=round(pmax, 4), elapsed=round(elapsed, 1),
                    win_max=round(s.win_max, 4), bn_adv_bp=self.bn_adverse_bp(s),
                    vol_ratio=round(vr, 2) if vr is not None else None,
                    book_bid=s.bid, book_ask=s.ask, px=self.bn.price.get(self.m.asset),
                    open=self.open, bn_lag_ms=round(self.bn.lag_ms.get(self.m.asset, 0.0)),
                    **(conf or {}), **(ext or {}), **(amp or {}), **(streak or {}),
                    btc_pass_by=pass_by, **self.depth_info(s, tier))
        if reason and Cfg.ENTRY_MODE != "trap":
            ts.state = "dead"
            self._bg(self._rec("skip", s, ts, reason=reason, **info))
            return
        if Cfg.ENTRY_MODE == "trap":
            # вход делает лимитка-ловушка; касание пишем для статистики. Медленное/позднее
            # первое касание (reason) убивает ступень, как в исходном сигнале
            if reason:
                ts.state = "dead"
                self._bg(self._rec("skip", s, ts, reason=reason, mode="trap",
                                   trap_state=ts.trap_state, **info))
                if Cfg.TRAP_KILL_SLOW and ts.spent <= 0 and reason in (
                        "slow", "late", "hour", "volume", "other_side", "pre_max", "binance",
                        "bn_confirm", "btc_extreme", "btc_amp", "btc_filters", "streak"):
                    ts.trap_dead = True
                    if ts.trap_state == "resting":
                        self._trap_cancel_bg(s, ts, f"первое касание: {reason}")
                    elif ts.trap_state == "idle":
                        ts.trap_state = "done"
                return
            ts.state = "touched"
            self._bg(self._rec("trigger", s, ts, mode="trap", trap_state=ts.trap_state, **info))
            return
        ts.state = "armed"
        ts.triggered = True
        ts.ask0 = s.ask
        ts.t_arm = time.time()                 # погоня — по локальным часам
        log.warning("СИГНАЛ %s %s ступень %s: mid=%.3f (макс %.2f за %.0fс), прошло %.0fс, "
                    "объём ×%.1f, книга %s/%s", self.m.slug, s.name, tier.name, p, pmax,
                    Cfg.FAST_S, elapsed, vr or 0.0, s.bid, s.ask)
        if Cfg.STAKE_PCT > 0:
            log.warning("  объём $%g (депозит $%.2f × %g%%)", tier.size, self.deposit, Cfg.STAKE_PCT)
        self._bg(self._rec("trigger", s, ts, stake=tier.size, deposit=round(self.deposit, 2), **info))
        self._kick(s, ts, "signal")
        self._bg(self._chase(s, ts))
        self._bg(self._book_after(s, ts))

    # --- уровни книги (только для записи объёма) -------------------------------------
    def set_levels(self, token: str, bids: Any, asks: Any) -> None:
        s = self.by_token.get(token)
        if s is None:
            return
        def conv(levels):
            out = {}
            for lv in levels or []:
                try:
                    p, z = float(lv["price"]), float(lv.get("size", 0) or 0)
                except (KeyError, TypeError, ValueError):
                    continue
                if z > 0:
                    out[p] = z
            return out
        s.bids, s.asks, s.lv_ok = conv(bids), conv(asks), True

    def on_level(self, token: str, side: Any, price: Any, size: Any) -> None:
        s = self.by_token.get(token)
        if s is None or not s.lv_ok:
            return
        p, z = _fnum(price), _fnum(size)
        if p is None or z is None:
            return
        book = s.bids if str(side).upper() == "BUY" else s.asks
        if z <= 0:
            book.pop(p, None)
        else:
            book[p] = z

    def depth_info(self, s: Side, tier: Tier) -> Dict[str, Any]:
        """Объём книги стороны: аски по <= уровня / потолка / потолка+0.05, биды в 0.02 и 0.05 ниже уровня."""
        if not Cfg.DEPTH_LOG:
            return {}
        if not s.lv_ok:
            return {"dep_ok": False}
        a = lambda px: round(sum(z for p, z in s.asks.items() if p <= px + 1e-9), 2)
        b = lambda px: round(sum(z for p, z in s.bids.items() if p >= px - 1e-9), 2)
        lv, cap = tier.level, tier.cap
        return {"dep_ok": True, "dep_a_lv": a(lv), "dep_a_cap": a(cap), "dep_a_cap5": a(cap + 0.05),
                "dep_b_2": b(lv - 0.02), "dep_b_5": b(lv - 0.05),
                "dep_n_a": sum(1 for p in s.asks if p <= cap + 0.05 + 1e-9)}

    # --- книга Polymarket: сигнал и добор -----------------------------------------
    def on_quote(self, token: str, bid: Optional[float], ask: Optional[float], now: float,
                 has_bid: bool = True, has_ask: bool = True) -> None:
        """Обновление лучших цен стороны. has_* = False — поле в сообщении не пришло,
        прежнее значение сохраняется; None при has_* = True — уровень опустел."""
        s = self.by_token.get(token)
        if s is None:
            return
        if has_bid:
            s.bid = bid
        if has_ask:
            s.ask, s.ask_t = ask, now
        if s.name == "Up" and now < self.m.end + 2:
            self.up_last = (now, s.bid, s.ask)
            if now >= self.m.end - 60:
                self.up_tail.append((now, s.bid, s.ask))
        if s.name == "Up" and Cfg.PATH_LOG:
            k = int(now - self.m.start)
            if 0 <= k < len(self.path_b):
                self.path_b[k], self.path_a[k] = s.bid, s.ask
        # mid только по живой книге: обе стороны есть, не скрещена, спред не шире MAX_SPREAD
        if s.bid is not None and s.ask is not None and 0 < s.ask - s.bid <= Cfg.MAX_SPREAD:
            s.mid = (s.bid + s.ask) / 2
        else:
            s.mid = None
        m = self.m
        if s.mid is not None and m.start <= now < m.end:
            self._on_mid(s, s.mid, now)
        if Cfg.ENTRY_MODE == "trap":
            self._trap_check(s, now)
            self._check_add(s, now)        # докупка тейкером после налива ловушки
            return
        if s.ask is None:
            return
        for ts in s.tiers:
            if ts.state == "armed" and s.ask <= self.ceil_of(ts, now) \
                    and now - ts.last_take >= self.pause_of(ts):
                self._kick(s, ts, "book")
        self._check_add(s, now)

    # --- докупка на подтверждении ---------------------------------------------------
    def add_band(self, s: Side) -> Tuple[float, float]:
        """(порог mid, потолок цены) следующей докупки от цены последней покупки."""
        thr = s.anchor + Cfg.ADD_STEP
        ceil = min(Cfg.ADD_MAX_PX, tick_floor(thr + Cfg.ADD_TOL + 1e-9, self.m.tick))
        return thr, ceil

    def _check_add(self, s: Side, now: float) -> None:
        a = s.add
        if (a is None or Cfg.ADD_USDC <= 0 or s.anchor is None or s.busy or a.inflight
                or a.state != "add" or s.mid is None or s.ask is None):
            return
        m = self.m
        if now > m.end - Cfg.END_GUARD_S or now < m.start:
            return
        if Cfg.ADD_WINDOW_S > 0 and now - s.anchor_t > Cfg.ADD_WINDOW_S:
            return
        if Cfg.ADD_MAX_N > 0 and s.add_n >= Cfg.ADD_MAX_N:
            return
        thr, ceil = self.add_band(s)
        if s.mid < thr - 1e-9:
            s.add_blocked = False
            return
        if s.ask > ceil + 1e-9 or s.ask <= s.anchor + 1e-9:
            # цена ушла выше допуска (или не выше прошлой покупки) — не докупаем, ждём
            if not s.add_blocked:
                s.add_blocked = True
                self._bg(self._rec("add_skip", s, a, last_px=round(s.anchor, 4), thr=round(thr, 4),
                                   ceil=ceil, mid=round(s.mid, 4), ask=s.ask))
            return
        if a.rejects >= Cfg.ADD_MAX_REJECTS and now - a.last_take < Cfg.RETRY_PAUSE_MAX_S:
            return
        if a.rejects > 0 and now - a.last_take < Cfg.ADD_PAUSE_S:
            return
        a.inflight = True
        s.busy = True
        a.takes += 1
        a.last_take = now
        self._bg(self._add_take(s, a, thr, ceil))

    async def _add_take(self, s: Side, a: TierState, thr: float, ceil: float) -> None:
        need = 0.0
        try:
            need = round(min(Cfg.ADD_USDC, Cfg.MAX_PER_WINDOW - self.window_spent(),
                             self.budget.room()), 2)
            if need < Cfg.MIN_NOTIONAL:
                need = 0.0
                a.state = "done"
                await self._rec("add_stop", s, a, reason="budget", spent=round(a.spent, 4))
                return
            self.reserved += need
            self.budget.pending += need
            ask_seen = s.ask
            cap = ceil                      # строго не дороже предыдущая + шаг + допуск
            prev_px = s.anchor
            t0 = time.time()
            if not self.clob.enabled:
                if s.ask is not None and s.ask <= cap:
                    resp: Any = {"paper": True, "status": "matched", "success": True,
                                 "makingAmount": need, "takingAmount": need / s.ask}
                else:
                    resp = {"paper": True, "status": "rejected", "success": False}
            else:
                resp = await asyncio.to_thread(self.clob.take, s.token, need, cap, self.m.tick)
            lat = round((time.time() - t0) * 1000)
            status = str(resp.get("status") or "").lower() if isinstance(resp, dict) else ""
            usdc, shares = self.clob.matched(resp)
            await self._rec("add_take", s, a, n=s.add_n + 1, last_px=round(prev_px, 4),
                            thr=round(thr, 4), mid=s.mid, need=need,
                            cap=cap, ask_seen=ask_seen, latency_ms=lat, status=status,
                            resp=_short(resp) if resp is not None else None)
            if resp is None or status == "delayed" or (status == "matched" and usdc <= 0):
                a.state = "frozen"
                await self._rec("freeze", s, a, note=f"докупка: исход неизвестен ({status or 'нет ответа'})")
                log.error("ЗАМОРОЗКА докупок %s %s: исход неизвестен", self.m.slug, s.name)
                return
            if usdc > 0:
                usdc = min(usdc, need)
                a.spent += usdc
                a.shares += shares
                a.rejects = 0
                self.budget.add(usdc)
                px = usdc / shares if shares else cap
                s.add_n += 1
                # следующая докупка — строго выше максимальной покупки; FAK мог налиться ниже
                # якоря (цена откатила за время ордера) — такой налив якорь не опускает
                s.anchor = max(s.anchor, px)
                s.add_blocked = False
                await self._rec("fill", s, a, usdc=round(usdc, 4), shares=round(shares, 4),
                                avg_px=round(px, 4), total=round(a.spent, 4), n=s.add_n,
                                last_px=round(prev_px, 4), cap=cap, latency_ms=lat,
                                paper=not self.clob.enabled)
                log.warning("ДОКУПКА %s %s #%d: $%.2f по %.4f (прошлая %.4f, порог mid %.3f, "
                            "потолок %.3f), докуплено $%.2f", self.m.slug, s.name, s.add_n,
                            usdc, px, prev_px, thr, cap, a.spent)
            else:
                a.rejects += 1
        except Exception as e:
            log.error("докупка %s %s: %s", self.m.slug, s.name, e)
            a.state = "frozen"
        finally:
            self.reserved = max(0.0, self.reserved - need)
            self.budget.pending = max(0.0, self.budget.pending - need)
            a.inflight = False
            s.busy = False

    def _on_mid(self, s: Side, p: float, t: float) -> None:
        if p > s.win_max:
            s.win_max = p
        h = s.hist
        if not h or t - h[-1][0] >= 0.2 or abs(p - h[-1][1]) >= 0.005:
            h.append((t, p))
            while len(h) > 1 and h[0][0] < t - Cfg.FAST_S - 1:
                h.popleft()
        elapsed = t - self.m.start
        for ts in s.tiers:
            if ts.state == "idle" and p <= ts.tier.level:
                self._first_touch(s, ts, p, t, elapsed)

    async def _book_after(self, s: Side, ts: TierState) -> None:
        """Аск и mid книги через 1/5/15 с после сигнала — для разбора."""
        prev = 0.0
        for d in (1.0, 5.0, 15.0):
            await asyncio.sleep(d - prev)
            prev = d
            await self._rec("book_after", s, ts, after_s=d, book_ask=s.ask, p=self.side_p(s),
                            **self.depth_info(s, ts.tier))

    # --- исполнение ---------------------------------------------------------------
    def pause_of(self, ts: TierState) -> float:
        """Пауза между попытками: RETRY_PAUSE_S, после отказов подряд ×1.5, до максимума."""
        if ts.takes == 0:
            return 0.0
        return min(Cfg.RETRY_PAUSE_S * (1.5 ** max(0, ts.rejects - 1)), Cfg.RETRY_PAUSE_MAX_S)

    def _kick(self, s: Side, ts: TierState, why: str) -> None:
        now = time.time()
        if ts.state != "armed" or ts.inflight or s.busy:
            return
        over = self.chase_over(ts, now)
        if over:
            self._bg(self._end_chase(s, ts, over))
            return
        if ts.takes > 0 and now - ts.last_take < self.pause_of(ts):
            return
        ts.inflight = True
        s.busy = True                      # один ордер на сторону — объём не задваивается
        ts.takes += 1
        ts.last_take = now
        self._bg(self._take(s, ts, why))

    async def _chase(self, s: Side, ts: TierState) -> None:
        """Повторы до полного объёма: стреляем, когда аск <= потолка или книга молчит."""
        while ts.state == "armed":
            await asyncio.sleep(0.2)
            if ts.state != "armed":
                break
            now = time.time()
            over = self.chase_over(ts, now)
            if over:
                if not ts.inflight:
                    await self._end_chase(s, ts, over)
                    break
                continue
            if ts.inflight or s.busy or now - ts.last_take < self.pause_of(ts):
                continue
            stale = (s.ask_t <= 0) or (now - s.ask_t > Cfg.BOOK_STALE_S)
            if stale or s.ask is None or s.ask <= self.ceil_of(ts, now):
                self._kick(s, ts, "retry" if not stale else "retry_stale")

    async def _take(self, s: Side, ts: TierState, why: str) -> None:
        need = 0.0
        try:
            need = round(min(ts.tier.size - ts.spent,
                             Cfg.MAX_PER_WINDOW - self.window_spent(),
                             self.budget.room()), 2)
            if need < Cfg.MIN_NOTIONAL:
                need = 0.0
                await self._end_chase(s, ts, self.chase_over(ts, time.time()) or "budget")
                return
            self.reserved += need
            self.budget.pending += need
            cap = self.ceil_of(ts)
            ask_seen = s.ask
            t0 = time.time()
            presigned = False
            if not self.clob.enabled:
                # бумага: налив по последнему аску книги, если он не выше потолка
                if s.ask is not None and s.ask <= cap:
                    resp: Any = {"paper": True, "status": "matched", "success": True,
                                 "makingAmount": need, "takingAmount": need / s.ask}
                else:
                    resp = {"paper": True, "status": "rejected", "success": False}
            else:
                presigned = (ts.takes == 1 and ts.signed is not None
                             and ts.signed_key == (self.m.tick, ts.tier.size)
                             and abs(need - ts.tier.size) < 0.005
                             and abs(cap - self.cap_of(ts)) < 1e-9)
                if presigned:
                    resp = await asyncio.to_thread(self.clob.post_signed, ts.signed)
                else:
                    resp = await asyncio.to_thread(self.clob.take, s.token, need, cap, self.m.tick)
                ts.signed = None
            lat = round((time.time() - t0) * 1000)
            status = str(resp.get("status") or "").lower() if isinstance(resp, dict) else ""
            usdc, shares = self.clob.matched(resp)
            await self._rec("take", s, ts, why=why, try_n=ts.takes, need=need, cap=cap,
                            ask_seen=ask_seen, presigned=presigned, latency_ms=lat,
                            status=status, resp=_short(resp) if resp is not None else None)
            if resp is None or status == "delayed" or (status == "matched" and usdc <= 0):
                # исход неизвестен: ордер мог налиться — повтор задвоил бы объём
                ts.state = "frozen"
                await self._rec("freeze", s, ts, note=f"исход неизвестен ({status or 'нет ответа'})")
                log.error("ЗАМОРОЗКА %s %s %s: исход тейкера неизвестен",
                          self.m.slug, s.name, ts.tier.name)
                return
            if usdc > 0:
                usdc = min(usdc, need)
                ts.spent += usdc
                ts.shares += shares
                ts.rejects = 0
                self.budget.add(usdc)
                if shares > 0:
                    if s.anchor is None:
                        s.anchor_t = time.time()
                    # якорь докупок = максимальная цена покупки на стороне: более глубокая
                    # ступень ядра его не опускает, иначе лесенка докупок повторяется заново
                    s.anchor = max(s.anchor or 0.0, usdc / shares)
                    s.add_blocked = False
                await self._rec("fill", s, ts, usdc=round(usdc, 4), shares=round(shares, 4),
                                avg_px=round(usdc / shares, 4) if shares else None,
                                total=round(ts.spent, 4), try_n=ts.takes, cap=cap,
                                latency_ms=lat, paper=not self.clob.enabled)
                log.warning("НАЛИВ %s %s ступень %s: $%.2f, %.1f шар по %.4f, всего $%.2f/$%.2f "
                            "(попытка %d, потолок %.3f, %d мс)", self.m.slug, s.name,
                            ts.tier.name, usdc, shares, usdc / shares if shares else 0.0,
                            ts.spent, ts.tier.size, ts.takes, cap, lat)
            else:
                ts.rejects += 1
            over = self.chase_over(ts, time.time())
            if over in ("filled", "left_below_min"):
                await self._end_chase(s, ts, over)
        except Exception as e:
            log.error("тейкер %s %s: %s", self.m.slug, s.name, e)
            ts.state = "frozen"
        finally:
            self.reserved = max(0.0, self.reserved - need)
            self.budget.pending = max(0.0, self.budget.pending - need)
            ts.inflight = False
            s.busy = False
            # освободили сторону — дать шанс другим ступеням, ждущим книгу
            for o in s.tiers:
                if o is not ts and o.state == "armed" and s.ask is not None \
                        and s.ask <= self.ceil_of(o):
                    self._kick(s, o, "book")
                    break

    # --- режим trap: лимитка-ловушка ------------------------------------------------
    # Лимитка BUY по цене P = уровень − TRAP_OFFSET стоит ТОЛЬКО пока выполняется условие
    # сигнала: max mid стороны за FAST_S >= P + (ref − level), т.е. падение до P было бы
    # быстрым, и прошло не больше (1 − REM_MIN_FRAC) окна. Условие пропало — снимаем.
    # Одна лимитка на ступень; новая — только после подтверждённого снятия старой;
    # налив по данным биржи (size_matched), ступень наливается один раз.
    def trap_px(self, ts: TierState) -> float:
        return max(tick_floor(ts.tier.level - Cfg.TRAP_OFFSET, self.m.tick), float(self.m.tick))

    def fair_prob(self, s: Side, now: float) -> Optional[float]:
        """Вероятность стороны по Binance: Φ(±ln(цена/открытие) / (σ·√минут до конца))."""
        px = getattr(self.bn, "price", {}).get(self.m.asset)
        sig = getattr(self.bn, "sig_fair", {}).get(self.m.asset)
        o = self.open
        if not px or not sig or not o or px <= 0 or o <= 0:
            return None
        left = max(1.0, (self.m.end - now) / 60.0)
        z = (1 if s.name == "Up" else -1) * math.log(px / o) / (sig * math.sqrt(left))
        return 0.5 * math.erfc(-z / math.sqrt(2))

    def fair_ok(self, s: Side, ts: TierState, now: float) -> bool:
        if Cfg.TRAP_FAIR_MIN <= -1:
            return True
        f = self.fair_prob(s, now)
        if f is None:
            return True                                   # нет данных — не мешаем
        P = self.trap_px(ts)
        ok = f - P >= Cfg.TRAP_FAIR_MIN - 1e-9
        if ok != (not ts.fair_block):
            ts.fair_block = not ok
            self._bg(self._rec("fair_block" if not ok else "fair_ok", s, ts,
                               fair=round(f, 4), px_trap=P, bn=self.bn.price.get(self.m.asset),
                               open=self.open, elapsed=round(now - self.m.start, 1)))
        return ok

    def trap_cond(self, s: Side, ts: TierState, now: float) -> bool:
        el = now - self.m.start
        if el < 0 or el > self.max_elapsed or not self.hour_ok:
            return False
        pm = max((x[1] for x in s.hist if x[0] >= now - Cfg.FAST_S), default=None)
        if s.mid is not None:
            pm = s.mid if pm is None else max(pm, s.mid)
        # TRAP_COND_REF: лимитка стоит, только пока касание самого УРОВНЯ было бы быстрым
        # (max mid за FAST_S >= референс). Иначе (порог от цены лимитки) она ловила наливы при
        # медленном касании уровня: цена проскакивала уровень и лимитку за одну секунду,
        # и снятие по «медленному касанию» опаздывало (5 из 5 таких наливов 08–09.10 — минус)
        thr = ts.tier.ref if Cfg.TRAP_COND_REF else \
            self.trap_px(ts) + (ts.tier.ref - ts.tier.level)
        if not (pm is not None and pm >= thr - 1e-9):
            return False
        if _pre_on(ts.tier) and s.win_max < _pre_min(ts.tier) - 1e-9:
            return False
        if not self.bn_ok(s):
            return False
        return self.fair_ok(s, ts, now)

    def _trap_release(self, ts: TierState) -> None:
        self.reserved = max(0.0, self.reserved - ts.trap_res)
        ts.trap_res = 0.0

    def _trap_check(self, s: Side, now: float) -> None:
        for ts in s.tiers:
            if ts.tier.label:
                continue
            st = ts.trap_state
            if st == "resting":
                if not self.trap_cond(s, ts, now):
                    self._trap_cancel_bg(s, ts, "время" if now - self.m.start > self.max_elapsed
                                         else ("Binance: падение оправдано" if ts.fair_block
                                               else "скорость"))
                elif not self.clob.enabled:
                    self._paper_fill(s, ts)
                continue
            if st != "idle" or now - ts.trap_off_t < Cfg.TRAP_REPLACE_S or now < ts.trap_next_t:
                continue
            if not self.trap_cond(s, ts, now):
                continue
            P = self.trap_px(ts)
            rem = round(ts.tier.size - ts.spent, 4)
            if rem < Cfg.MIN_NOTIONAL or rem / P < Cfg.TRAP_MIN_SHARES - 1e-9:
                ts.trap_state = "done"
                continue
            if s.ask is not None and s.ask <= P + 1e-9:
                continue                           # книга уже ниже: post-only не встанет
            if Cfg.ONE_SIDE_PER_WINDOW and any(o.spent > 0 for o in self.sides.values()
                                               if o is not s):
                continue
            if self.window_spent() + rem > Cfg.MAX_PER_WINDOW + 1e-9 or self.budget.room() < rem:
                continue
            ts.trap_state = "placing"
            ts.trap_res = rem
            self.reserved += rem
            if self.clob.enabled:
                self._bg(self._trap_place(s, ts, P, rem))
            else:
                ts.oid, ts.trap_p, ts.trap_matched = f"paper-{self.m.slug}-{s.name}-{ts.tier.name}-{now:.0f}", P, 0.0
                ts.trap_size = math.floor(rem / P * 100) / 100
                ts.trap_seen[ts.oid] = (P, 0.0)
                ts.trap_state = "resting"
                self._bg(self._rec("trap_place", s, ts, oid=ts.oid, px=P, shares=ts.trap_size,
                                   elapsed=round(now - self.m.start, 1), paper=True))
                self._paper_fill(s, ts)

    def _paper_fill(self, s: Side, ts: TierState) -> None:
        """Бумага: стоящая лимитка налита, если продажи прошли сквозь её цену."""
        P = ts.trap_p
        if (s.bid is not None and s.bid < P - 1e-9) or (s.ask is not None and s.ask <= P + 1e-9):
            self._trap_sync(s, ts, ts.trap_size)
            self._trap_closed(s, ts, "MATCHED")

    def _trap_sync(self, s: Side, ts: TierState, matched: float,
                   oid: Optional[str] = None) -> None:
        """Налив по данным биржи: size_matched ордера только растёт, учитываем прирост.
        Учёт по id ордера — поздняя сверка уже закрытого ордера не путается с новым."""
        oid = oid or ts.oid
        if not oid:
            return
        px, prev = ts.trap_seen.get(oid, (ts.trap_p, 0.0))
        delta = round(matched - prev, 6)
        if delta <= 1e-6:
            return
        ts.trap_seen[oid] = (px, matched)
        if oid == ts.oid:
            ts.trap_matched = matched
        usdc = round(delta * px, 6)
        ts.spent += usdc
        ts.shares += delta
        self.budget.add(usdc)
        if delta > 0:
            # якорь докупок = максимальная цена покупки на стороне; время — первого налива
            if s.anchor is None:
                s.anchor_t = time.time()
            s.anchor = max(s.anchor or 0.0, px)
            s.add_blocked = False
        rel = min(ts.trap_res, usdc)
        ts.trap_res -= rel
        self.reserved = max(0.0, self.reserved - rel)
        if not ts.triggered:
            ts.triggered = True
            ts.ask0 = ts.trap_p
            ts.t_arm = time.time()
        log.warning("НАЛИВ ЛОВУШКИ %s %s ступень %s: %.2f шар по %.3f ($%.2f), всего $%.2f/$%.2f",
                    self.m.slug, s.name, ts.tier.name, delta, px, usdc, ts.spent,
                    ts.tier.size)
        if ts.spent >= ts.tier.size - Cfg.MIN_NOTIONAL + 1e-9 and ts.trap_state == "resting" \
                and oid == ts.oid and matched < ts.trap_size - 1e-6 and self.clob.enabled:
            self._trap_cancel_bg(s, ts, "ступень набрана")   # остаток частично налитой
        self._bg(self._rec("fill", s, ts, usdc=round(usdc, 4), shares=round(delta, 4),
                           avg_px=px, total=round(ts.spent, 4), maker=True, oid=oid,
                           elapsed=round(time.time() - self.m.start, 1),
                           paper=not self.clob.enabled))

    def _trap_closed(self, s: Side, ts: TierState, status: str) -> None:
        """Лимитка закончилась (налита/снята/истекла) — подтверждено биржей."""
        if ts.oid:
            TRAPS.pop(ts.oid, None)
        self._trap_release(ts)
        oid, ts.oid = ts.oid, None
        ts.trap_busy = ts.trap_stuck = False
        ts.trap_miss = 0
        ts.trap_off_t = time.time()
        rem = ts.tier.size - ts.spent
        ts.trap_state = "done" if (ts.spent > 0 or rem < Cfg.MIN_NOTIONAL or ts.trap_dead) \
            else "idle"
        log.info("лимитка %s %s ступень %s закрыта: статус %s, налито %.2f шар ($%.2f)%s",
                 self.m.slug, s.name, ts.tier.name, status or "?", ts.trap_matched, ts.spent,
                 "" if ts.trap_matched > 0 else " — покупки не было")
        self._bg(self._rec("trap_closed", s, ts, oid=oid, status=status,
                           matched=round(ts.trap_matched, 4), spent=round(ts.spent, 4),
                           next=ts.trap_state))

    async def _trap_place(self, s: Side, ts: TierState, P: float, rem: float) -> None:
        shares = math.floor(rem / P * 100) / 100
        exp = int(max(self.m.start + self.max_elapsed + Cfg.TRAP_GTD_PAD_S,
                      time.time() + Cfg.TRAP_GTD_MIN_S))
        # защита от дублей: на этой цене токена не должно стоять наших ордеров вне учёта
        lst = await asyncio.to_thread(self.clob.open_orders, s.token)
        if lst is None:
            self._trap_release(ts)
            ts.trap_state, ts.trap_off_t = "idle", time.time()
            return
        orphans = []
        for o in lst:
            try:
                if str(o.get("side", "")).upper() == "BUY" and abs(float(o.get("price")) - P) < 1e-9 \
                        and _oid(o) not in TRAPS:
                    orphans.append(_oid(o))
            except (TypeError, ValueError):
                continue
        if orphans:
            log.warning("ловушка %s %s %s: на %.3f уже стоят ордера вне учёта %s — снимаю, новую не "
                        "ставлю", self.m.slug, s.name, ts.tier.name, P, [o[:12] for o in orphans])
            try:
                await asyncio.to_thread(self.clob.cancel_ids, orphans)
            except Exception as e:
                log.warning("снятие сирот: %s", e)
                await asyncio.to_thread(self.clob.cancel_asset, s.token)
            await self._rec("trap_orphans", s, ts, px=P, ids=orphans)
            self._trap_release(ts)
            ts.trap_state, ts.trap_off_t = "idle", time.time()
            return
        try:
            r = await asyncio.to_thread(self.clob.place_limit, s.token, P, shares, self.m.tick, exp)
        except Exception as e:
            r = {"ok": False, "unknown": True, "err": str(e)[:200]}
        now = time.time()
        if r.get("ok"):
            P = float(r.get("px") or P)
            ts.trap_rejects, ts.trap_last_err = 0, ""
            ts.oid, ts.trap_p, ts.trap_size, ts.trap_matched = r["id"], P, shares, 0.0
            ts.trap_seen[ts.oid] = (P, 0.0)
            ts.trap_state = "resting"
            TRAPS[ts.oid] = (self, s, ts)
            log.info("лимитка выставлена (это ещё не покупка) %s %s ступень %s: %.2f шар по %.3f, "
                     "срок до %s", self.m.slug, s.name, ts.tier.name, shares, P,
                     time.strftime("%H:%M:%S", time.gmtime(exp)))
            await self._rec("trap_place", s, ts, oid=ts.oid, px=P, shares=shares, expiration=exp,
                            elapsed=round(now - self.m.start, 1), mid=s.mid, bid=s.bid, ask=s.ask)
            if not self.trap_cond(s, ts, now):
                self._trap_cancel_bg(s, ts, "условие пропало при постановке")
            return
        if not r.get("unknown"):
            self._trap_release(ts)
            ts.trap_state = "idle"
            ts.trap_off_t = now
            ts.trap_rejects += 1
            back = min(Cfg.TRAP_BACKOFF_MAX_S, Cfg.TRAP_REPLACE_S * 2 ** (ts.trap_rejects - 1))
            ts.trap_next_t = now + back
            err = str(r.get("err") or "")
            if err != ts.trap_last_err or ts.trap_rejects in (1, 5, 20):
                log.warning("лимитка %s %s ступень %s отклонена (%d подряд): %s — следующая "
                            "попытка через %.0f с", self.m.slug, s.name, ts.tier.name,
                            ts.trap_rejects, err[:160], back)
            ts.trap_last_err = err
            await self._rec("trap_reject", s, ts, px=P, shares=shares, expiration=exp, err=err,
                            n=ts.trap_rejects)
            return
        # исход постановки неизвестен: ищем ордер на бирже; не нашли — снимаем всё по токену
        log.error("ловушка %s %s %s: исход постановки неизвестен (%s) — сверка",
                  self.m.slug, s.name, ts.tier.name, r.get("err"))
        ts.trap_state = "cancelling"
        found = None
        for _ in range(3):
            await asyncio.sleep(1.0)
            lst = await asyncio.to_thread(self.clob.open_orders, s.token)
            for o in lst or []:
                try:
                    if abs(float(o.get("price")) - P) < 1e-9 and str(o.get("side", "")).upper() == "BUY" \
                            and abs(float(o.get("original_size") or 0) - shares) < 1e-6 \
                            and _oid(o) not in TRAPS:
                        found = o
                except (TypeError, ValueError):
                    continue
            if found or lst is not None:
                break
        if found:
            ts.oid, ts.trap_p, ts.trap_size, ts.trap_matched = _oid(found), P, shares, 0.0
            ts.trap_seen[ts.oid] = (P, 0.0)
            TRAPS[ts.oid] = (self, s, ts)
            self._trap_sync(s, ts, _matched(found))
            await self._rec("trap_place", s, ts, oid=ts.oid, px=P, shares=shares, expiration=exp,
                            recovered=True)
            ts.trap_state = "resting"
            if not self.trap_cond(s, ts, time.time()):
                self._trap_cancel_bg(s, ts, "условие пропало при сверке")
            return
        self._trap_release(ts)
        ts.trap_state = "frozen"
        await self._rec("freeze", s, ts, note="ловушка: исход постановки неизвестен, ордер не "
                                               "найден — ступень заморожена, цена под надзором")
        self._bg(self._trap_sweep(s, ts, P))

    async def _trap_sweep(self, s: Side, ts: TierState, P: float) -> None:
        """После неизвестного исхода постановки: если ордер всё-таки встал (а список открытых
        его не показал), он всплывёт позже — снимаем ордера на этой цене вне учёта.
        Не удалось получить список — снимаем все ордера токена (соседи переставятся)."""
        for d in (5.0, 15.0, 45.0):
            await asyncio.sleep(d)
            lst = await asyncio.to_thread(self.clob.open_orders, s.token)
            if lst is None:
                if d >= 15.0:
                    await asyncio.to_thread(self.clob.cancel_asset, s.token)
                continue
            ids = []
            for o in lst:
                try:
                    if str(o.get("side", "")).upper() == "BUY" and abs(float(o.get("price")) - P) < 1e-9 \
                            and _oid(o) not in TRAPS:
                        ids.append(_oid(o))
                except (TypeError, ValueError):
                    continue
            if ids:
                log.warning("%s %s %s: всплыл ордер после неизвестной постановки %s — снимаю",
                            self.m.slug, s.name, ts.tier.name, [i[:12] for i in ids])
                try:
                    await asyncio.to_thread(self.clob.cancel_ids, ids)
                except Exception as e:
                    log.warning("снятие всплывшего: %s", e)
                    await asyncio.to_thread(self.clob.cancel_asset, s.token)
                await self._rec("trap_orphans", s, ts, px=P, ids=ids, after_unknown=True)

    def _trap_cancel_bg(self, s: Side, ts: TierState, why: str) -> None:
        if ts.trap_state != "resting" or ts.trap_busy:
            return
        if not ts.oid:
            return
        ts.trap_state = "cancelling"
        if not self.clob.enabled:
            self._bg(self._rec("trap_cancel", s, ts, oid=ts.oid, why=why, ok=True, tries=0))
            self._trap_closed(s, ts, "CANCELED")
            return
        self._bg(self._trap_cancel(s, ts, why, ts.oid))

    async def _trap_cancel(self, s: Side, ts: TierState, why: str,
                           oid: Optional[str] = None) -> bool:
        """Надёжное снятие без лишних жертв.
        1) отмена по id; ответ биржи «снят» или «не найден/налит» — ордер больше не стоит;
        2) статус читаем с терпением (у биржи бывает задержка), налив учитываем;
        3) только если ордер по-прежнему виден среди открытых — повтор по id, с 3-й попытки
           все ордера токена, с 5-й — cancel_all (TRAP_CANCEL_ALL_FALLBACK).
        Раньше эскалация шла сразу и сносила соседние ловушки и чужие ордера."""
        oid = oid or ts.oid
        if not oid or ts.oid != oid:
            return True                            # ордер уже закрыт (налит/снят) — снимать нечего
        if ts.trap_busy:
            return False
        ts.trap_busy = True
        ok, status, tries, gone = False, "", 0, 0
        try:
            for i in range(Cfg.TRAP_CANCEL_TRIES):
                if ts.oid != oid:                  # поллер уже закрыл ордер по данным биржи
                    ok, status = True, "CLOSED"
                    break
                tries = i + 1
                confirmed = False
                try:
                    canceled, nc = await asyncio.to_thread(self.clob.cancel_ids, [oid])
                    reason = str(nc.get(oid) or "")
                    if oid in canceled:
                        confirmed = True
                    elif reason:
                        r = reason.lower()
                        confirmed = any(x in r for x in ("not found", "can't be found",
                                                         "cannot be found", "matched", "cancel"))
                        log.info("снятие %s: биржа ответила «%s»", oid[:12], reason)
                except Exception as e:
                    log.warning("снятие %s по id: %s", oid[:12], e)
                # статус с терпением: биржа подтверждает отмену с задержкой
                for d in ((0.0, 0.4, 0.8) if confirmed else (0.0, 0.4)):
                    if d:
                        await asyncio.sleep(d)
                    o = await asyncio.to_thread(self.clob.order, oid)
                    if o is not None:
                        self._trap_sync(s, ts, _matched(o), oid)
                        if _terminal(o):
                            ok, status = True, str(o.get("status") or "")
                            break
                if ok:
                    break
                lst = await asyncio.to_thread(self.clob.open_orders, s.token)
                still = lst is None or any(_oid(x) == oid for x in lst)
                if not still:
                    if confirmed:
                        ok, status = True, "CANCELED"   # биржа подтвердила, среди открытых нет
                        break
                    # не подтверждено и в списке нет — список мог быть неполным; повторяем
                    # отмену по id, пока биржа не ответит явно
                elif lst is not None:
                    for x in lst:                        # налив, пока висит
                        if _oid(x) == oid:
                            self._trap_sync(s, ts, _matched(x), oid)
                    if i >= 2:
                        log.warning("снятие %s: всё ещё стоит — снимаю все ордера токена", oid[:12])
                        await asyncio.to_thread(self.clob.cancel_asset, s.token)
                    if i >= 4 and Cfg.TRAP_CANCEL_ALL_FALLBACK:
                        log.error("снятие %s: всё ещё стоит после снятия по токену — cancel_all",
                                  oid[:12])
                        await asyncio.to_thread(self.clob.cancel_all)
                await asyncio.sleep(min(2.0, 0.25 * (2 ** i)))
        finally:
            ts.trap_busy = False
        await self._rec("trap_cancel", s, ts, oid=oid, why=why, ok=ok, status=status, tries=tries,
                        matched=round(ts.trap_seen.get(oid, (0.0, 0.0))[1], 4))
        if ok:
            if ts.oid == oid:                      # не закрываем дважды
                self._trap_closed(s, ts, status)
            self._bg(self._trap_late_sync(s, ts, oid))
        else:
            ts.trap_stuck = True                   # поллер будет повторять
            log.error("ЛОВУШКА НЕ СНЯТА %s %s ступень %s (%s) — повторяю в фоне",
                      self.m.slug, s.name, ts.tier.name, oid)
        return ok

    async def _trap_late_sync(self, s: Side, ts: TierState, oid: str) -> None:
        """Через несколько секунд после снятия ещё раз читаем налив ордера: последний
        матч мог прийти в момент отмены и отразиться в статусе с задержкой."""
        await asyncio.sleep(3.0)
        o = await asyncio.to_thread(self.clob.order, oid)
        if o is not None:
            self._trap_sync(s, ts, _matched(o), oid)

    async def trap_close_all(self) -> None:
        """Конец окна: снять всё, что ещё стоит, и дождаться подтверждения."""
        for s in self.sides.values():
            for ts in s.tiers:
                if ts.trap_state == "resting":
                    self._trap_cancel_bg(s, ts, "конец окна")
        t_end = time.time() + 30
        while time.time() < t_end and any(ts.trap_state in ("placing", "cancelling")
                                          for s in self.sides.values() for ts in s.tiers):
            await asyncio.sleep(0.5)
            for s in self.sides.values():
                for ts in s.tiers:
                    if ts.trap_state == "cancelling" and ts.trap_stuck and not ts.trap_busy \
                            and ts.oid:
                        await self._trap_cancel(s, ts, "конец окна, повтор", ts.oid)
        left = [(s.name, ts.tier.name, ts.oid) for s in self.sides.values() for ts in s.tiers
                if ts.oid]
        if left:
            log.error("%s: после конца окна не сняты ловушки %s", self.m.slug, left)

    # --- итог окна ------------------------------------------------------------------
    def positions(self) -> List[Tuple[Side, TierState]]:
        return [(s, ts) for s in self.sides.values() for ts in s.tiers if ts.spent > 0]

    async def _write_path(self, winner: Optional[str], src: str) -> None:
        if not Cfg.PATH_LOG:
            return
        b, a = list(self.path_b), list(self.path_a)
        for i in range(1, len(b)):                 # секунда без сообщений — прошлая котировка
            if b[i] is None and a[i] is None:
                b[i], a[i] = b[i - 1], a[i - 1]
        await _path_journal().write({"slug": self.m.slug, "asset": self.m.asset,
                                     "start": self.m.start, "winner": winner, "source": src,
                                     "open": self.open, "close": self.close,
                                     "bid": [None if x is None else round(x, 4) for x in b],
                                     "ask": [None if x is None else round(x, 4) for x in a]})

    def book_winner(self) -> Optional[str]:
        return book_winner_tail(self.up_tail, self.m.end)

    async def _verify_gamma(self, disc: Discovery, book_w: str, pos) -> None:
        """Сверка исхода по книге с gamma (фон). Расхождение — запись resolved_fix в журнал."""
        await asyncio.sleep(Cfg.RESOLVE_DELAY_S)
        deadline = time.time() + Cfg.RESOLVE_TIMEOUT_S + Cfg.RESOLVE_MAX_S
        g = None
        while time.time() < deadline:
            g = await asyncio.to_thread(disc.resolution, self.m.slug)
            if g:
                break
            await asyncio.sleep(60)
        await self._rec("gamma_check", book=book_w, gamma=g, match=(g == book_w) if g else None)
        if g and g != book_w:
            log.error("%s: исход по книге %s, а gamma %s — ИСПРАВЛЯЮ итог", self.m.slug, book_w, g)
            for s, ts in pos:
                won = s.name == g
                pnl = (ts.shares if won else 0.0) - ts.spent
                await self._rec("resolved_fix", s, ts, winner=g, source="gamma", won=won,
                                usdc=round(ts.spent, 4), shares=round(ts.shares, 4), pnl=round(pnl, 4))

    async def finish(self, disc: Discovery, stats: Stats) -> None:
        if Cfg.ENTRY_MODE == "trap":
            await self.trap_close_all()
        pos = self.positions()
        bin_win = None
        if self.open is not None and self.close is not None:
            bin_win = "Up" if self.close >= self.open else "Down"
        await self._rec("window_end", open=self.open, close=self.close, binance_winner=bin_win,
                        positions=[dict(side=s.name, tier=ts.tier.name, usdc=round(ts.spent, 4),
                                        shares=round(ts.shares, 4)) for s, ts in pos])
        trig = [(s, ts) for s in self.sides.values() for ts in s.tiers if ts.triggered]
        # исход сразу по книге в последние секунды окна (сверка с gamma 5677 окон: книга однозначна
        # в 99.8% окон и верна в 99.95%); gamma потом проверяется в фоне. Книга неоднозначна — ждём gamma
        winner, src = None, "gamma"
        bw = self.book_winner() if Cfg.RESOLVE_BY_BOOK else None
        if bw:
            winner, src = bw, "book"
            self._bg(self._verify_gamma(disc, bw, pos))
        else:
            if pos and Cfg.RESOLVE_BY_BOOK:
                log.info("%s: книга в последние %.0f с не дала бид >= %.2f — исход жду от gamma",
                         self.m.slug, Cfg.BOOK_HOLD_S, Cfg.BOOK_WIN_PX)
            await asyncio.sleep(Cfg.RESOLVE_DELAY_S)
            deadline = time.time() + Cfg.RESOLVE_TIMEOUT_S
            while time.time() < deadline:
                winner = await asyncio.to_thread(disc.resolution, self.m.slug)
                if winner:
                    break
                await asyncio.sleep(30 if time.time() - deadline + Cfg.RESOLVE_TIMEOUT_S < 1800 else 120)
        if not winner:
            if pos or trig:
                # по позициям исход по Binance НЕ берём: при TWAP он расходится с реальным ~12%
                # окон, именно в спорных (почти ничьих) окнах. Ждём gamma дальше, до RESOLVE_MAX_S
                log.warning("%s: gamma не дала исход за %.0f с — жду дальше (позиции не закрываю)",
                            self.m.slug, Cfg.RESOLVE_TIMEOUT_S)
                deadline2 = time.time() + Cfg.RESOLVE_MAX_S
                while time.time() < deadline2:
                    await asyncio.sleep(300)
                    winner = await asyncio.to_thread(disc.resolution, self.m.slug)
                    if winner:
                        break
                if not winner:
                    await self._rec("winner", winner=None, source="unresolved", binance_winner=bin_win)
                    await self._write_path(bin_win, "binance")
                    log.error("%s: исход так и не получен — позиции без итога, разрешит бэклог при "
                              "следующем запуске", self.m.slug)
                    return
            else:
                winner, src = bin_win, "binance"
        await self._rec("winner", winner=winner, source=src, binance_winner=bin_win)
        await self._write_path(winner, src)
        if not pos and not trig:
            return
        for s, ts in pos:
            won = s.name == winner
            pnl = (ts.shares if won else 0.0) - ts.spent
            stats.add(ts.spent, pnl, won)
            await self._rec("resolved", s, ts, winner=winner, source=src, won=won,
                            usdc=round(ts.spent, 4), shares=round(ts.shares, 4), pnl=round(pnl, 4))
        # shadow: что было бы при полном наливе ступени по аску на сигнале
        for s, ts in trig:
            won = s.name == winner
            px = ts.ask0
            pnl_ask = None
            if px and 0 < px < 1:
                pnl_ask = round((ts.tier.size / px if won else 0.0) - ts.tier.size, 4)
            stats.add_shadow(ts.tier.size, pnl_ask, won)
            await self._rec("shadow", s, ts, winner=winner, source=src, won=won,
                            size=ts.tier.size, ask0=px, pnl_full_ask0=pnl_ask,
                            fill_pct=round(100 * ts.spent / ts.tier.size, 1))
        log.warning("РЕЗОЛВ %s: победил %s (%s). %s%s", self.m.slug, winner, src, stats.line(),
                    f" | депозит ${EQUITY.value:.2f}" if EQUITY is not None else "")


_PATHJ: Optional[Journal] = None


def _path_journal() -> Journal:
    global _PATHJ
    if _PATHJ is None:
        _PATHJ = Journal(Cfg.PATH_LOG)
    return _PATHJ


# ----------------------------------------------------------------------------- вотчер

async def watch_market(eng: Engine, stop: asyncio.Event) -> None:
    mkt = eng.m
    eng.bn.subs[mkt.asset].append(eng)
    try:
        await eng.presign_all()
    except Exception as e:
        log.warning("%s предподпись: %s", mkt.slug, e)
    assets = list(mkt.tokens.values())
    if Cfg.ENTRY_MODE == "trap" and eng.clob.enabled:
        for tok in assets:                      # хвосты прошлого запуска по этим токенам
            await asyncio.to_thread(eng.clob.cancel_asset, tok)
    log.info("слежу за %s до %s", mkt.slug, time.strftime("%H:%M:%S", time.gmtime(mkt.end)))
    try:
        while not stop.is_set() and time.time() < mkt.end:
            try:
                async with websockets.connect(Cfg.WS_URL, ping_interval=None,
                                              close_timeout=5) as ws:
                    await ws.send(json.dumps({"assets_ids": assets, "type": "market"}))
                    last = time.time()
                    pinger = asyncio.create_task(_ping(ws, stop, mkt.end))
                    try:
                        while not stop.is_set() and time.time() < mkt.end:
                            timeout = max(1.0, min(Cfg.WS_STALE_S, mkt.end - time.time()))
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                            except asyncio.TimeoutError:
                                if time.time() - last > Cfg.WS_STALE_S:
                                    log.warning("%s: нет данных %.0fс, переподключаюсь",
                                                mkt.slug, time.time() - last)
                                    break
                                continue
                            last = time.time()
                            if handle_book(raw, eng, last):
                                eng._bg(eng.presign_all())          # сменился тик
                    finally:
                        pinger.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("%s ws: %s", mkt.slug, e)
                await asyncio.sleep(1.0)
        await asyncio.sleep(max(0.0, min(3.0, mkt.end + 2 - time.time())))   # цена закрытия
    finally:
        try:
            eng.bn.subs[mkt.asset].remove(eng)
        except ValueError:
            pass
        log.info("окно %s закрыто", mkt.slug)


_HB_KEYS = ("heartbeat_id", "heartbeatId", "expected_heartbeat_id", "expected", "id")
_HB_RX = __import__("re").compile(r'heartbeat_?id"?\s*[:=]\s*"?([A-Za-z0-9_-]{6,})', 2)
_UUID_RX = __import__("re").compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")


def _hb_id(x: Any) -> Optional[str]:
    """Достать id цепочки heartbeat из ответа/ошибки (формат у биржи описан скупо)."""
    if isinstance(x, str):
        try:
            x = json.loads(x)
        except Exception:
            m = _HB_RX.search(x) or _UUID_RX.search(x)
            return m.group(1) if m else None
    if isinstance(x, dict):
        for k in _HB_KEYS:
            v = x.get(k)
            if isinstance(v, str) and v:
                return v
        for v in x.values():
            r = _hb_id(v) if isinstance(v, (dict, str)) else None
            if r:
                return r
    return None


async def heartbeat_loop(clob: "Clob", stop: asyncio.Event) -> None:
    """Heartbeat из event loop: если loop завис или процесс уснул — heartbeat'ов нет,
    и через ~10–15 с биржа снимает наши лимитки сама. Свой поток, без общего семафора."""
    from concurrent.futures import ThreadPoolExecutor
    ex = ThreadPoolExecutor(1, thread_name_prefix="hb")
    loop = asyncio.get_running_loop()
    hid, alive, fails, last_warn = "", False, 0, 0.0
    try:
        while not stop.is_set():
            t0 = time.time()
            try:
                ok, nid, err = await asyncio.wait_for(
                    loop.run_in_executor(ex, clob.heartbeat, hid), timeout=8)
            except asyncio.TimeoutError:
                ok, nid, err = False, "", "таймаут"
            if ok:
                hid, fails = nid, 0
                if not alive:
                    alive = True
                    log.warning("heartbeat включён: при зависании бота/сети биржа сама "
                                "снимет лимитки через ~10 с")
            else:
                fails += 1
                if nid and nid != hid:
                    hid = nid                      # биржа подсказала ожидаемый id
                    continue                       # сразу повтор с ним
                if fails >= 3:
                    hid = ""                       # начать цепочку заново
                if time.time() - last_warn > 30:
                    last_warn = time.time()
                    log.warning("heartbeat не принят (%d подряд): %s%s", fails, err,
                                "" if alive else " — защита пока НЕ активна")
            try:
                await asyncio.wait_for(stop.wait(),
                                       timeout=max(0.2, Cfg.HEARTBEAT_S - (time.time() - t0)))
            except asyncio.TimeoutError:
                pass
    finally:
        ex.shutdown(wait=False)


async def stall_watch(journal: "Journal", stop: asyncio.Event) -> None:
    """Пишет в журнал паузы цикла (сон компа, обрыв, блокировка) — чтобы было видно,
    когда бот был «слеп», а его ордера стояли на бирже."""
    last = time.time()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        now = time.time()
        gap = now - last - 1.0
        if gap > Cfg.STALL_LOG_S:
            log.error("бот стоял %.0f с (сон/сеть/блокировка); состояние лимиток сверит опрос", gap)
            await journal.write({"event": "stall", "gap_s": round(gap, 1),
                                 "from": round(last, 3)})
        last = now


async def trap_poller(clob: "Clob", stop: asyncio.Event) -> None:
    """Наливы и судьба стоящих лимиток: один запрос открытых ордеров на всех."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=Cfg.TRAP_POLL_S)
            break
        except asyncio.TimeoutError:
            pass
        if not TRAPS:
            continue
        lst = await asyncio.to_thread(clob.open_orders)
        if lst is None:
            continue
        live = {_oid(o): o for o in lst}
        for oid, (eng, s, ts) in list(TRAPS.items()):
            if ts.oid != oid:
                TRAPS.pop(oid, None)
                continue
            o = live.get(oid)
            if o is not None:
                ts.trap_miss = 0
                eng._trap_sync(s, ts, _matched(o), oid)
                if ts.trap_stuck and not ts.trap_busy:
                    eng._bg(eng._trap_cancel(s, ts, "повтор снятия", oid))
                continue
            o = await asyncio.to_thread(clob.order, oid)       # нет среди открытых
            if o is None:
                # статус недоступен: список открытых мог прийти неполным (сбой API). Не гадаем —
                # отправляем отмену по id: биржа либо снимет живой ордер, либо ответит
                # «не найден», и это подтверждение закроет ступень. Так не будет «потерянных»
                # живых ордеров (раньше они всплывали сиротами).
                ts.trap_miss += 1
                if ts.trap_miss >= 2 and not ts.trap_busy and ts.trap_state == "resting":
                    eng._trap_cancel_bg(s, ts, "нет среди открытых, статус недоступен")
                continue
            if not _terminal(o):
                continue
            eng._trap_sync(s, ts, _matched(o), oid)
            if ts.trap_busy:
                continue                                       # снятие само закроет
            eng._trap_closed(s, ts, str(o.get("status") or ""))


async def trap_shutdown(clob: "Clob", tokens: List[str]) -> None:
    """Остановка бота: снять все лимитки и убедиться, что ни одна не осталась."""
    ids = list(TRAPS.keys())
    if not clob.enabled or (not ids and not tokens):
        return
    try:
        if ids:
            await asyncio.to_thread(clob.cancel_ids, ids)
    except Exception as e:
        log.warning("остановка: снятие по id: %s", e)
    for t in tokens:
        await asyncio.to_thread(clob.cancel_asset, t)
    lst = await asyncio.to_thread(clob.open_orders)
    left = [o for o in (lst or []) if _oid(o) in TRAPS or o.get("asset_id") in tokens]
    if lst is None or left:
        if Cfg.TRAP_CANCEL_ALL_FALLBACK:
            log.error("остановка: остались лимитки (%d) — cancel_all", len(left))
            await asyncio.to_thread(clob.cancel_all)
        else:
            log.error("остановка: остались лимитки: %s", [_oid(o) for o in left])


async def _ping(ws, stop: asyncio.Event, until: float) -> None:
    try:
        while not stop.is_set() and time.time() < until:
            await asyncio.sleep(10)
            await ws.send("PING")
    except Exception:
        pass


def _fnum(v: Any) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _best(levels: Any, pick) -> Optional[float]:
    best = None
    for lv in levels or []:
        try:
            p, sz = float(lv["price"]), float(lv.get("size", 0) or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if sz > 0:
            best = p if best is None else pick(best, p)
    return best


def handle_book(raw: Any, eng: Engine, now: float) -> bool:
    """Разбор сообщения книги Polymarket. True — сменился тик."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")
    if not raw or raw.strip() in ("PONG", "PING"):
        return False
    try:
        msg = json.loads(raw)
    except Exception:
        return False
    tick_changed = False
    for ev in (msg if isinstance(msg, list) else [msg]):
        if not isinstance(ev, dict):
            continue
        et = ev.get("event_type")
        if et == "book":
            if Cfg.DEPTH_LOG:
                eng.set_levels(ev.get("asset_id"), ev.get("bids"), ev.get("asks"))
            eng.on_quote(ev.get("asset_id"), _best(ev.get("bids"), max),
                         _best(ev.get("asks"), min), now)
        elif et == "price_change":
            chs = ev.get("price_changes") or []
            if Cfg.DEPTH_LOG:                      # уровни всего сообщения — до котировок
                for ch in chs:
                    if "price" in ch and "size" in ch and ch.get("side"):
                        eng.on_level(ch.get("asset_id"), ch.get("side"), ch.get("price"), ch.get("size"))
            for ch in chs:
                hb, ha = "best_bid" in ch, "best_ask" in ch
                if hb or ha:
                    eng.on_quote(ch.get("asset_id"), _fnum(ch.get("best_bid")),
                                 _fnum(ch.get("best_ask")), now, has_bid=hb, has_ask=ha)
        elif et == "best_bid_ask":
            hb, ha = "best_bid" in ev, "best_ask" in ev
            eng.on_quote(ev.get("asset_id"), _fnum(ev.get("best_bid")),
                         _fnum(ev.get("best_ask")), now, has_bid=hb, has_ask=ha)
        elif et == "tick_size_change":
            new = ev.get("new_tick_size")
            if new and str(new) != eng.m.tick:
                eng.m.tick = str(new)
                tick_changed = True
                log.info("%s тик -> %s", eng.m.slug, new)
    return tick_changed


# ----------------------------------------------------------------------------- рантайм

def window_starts(tf: int, now: float) -> List[int]:
    L = tf * 60
    cur = int(now // L) * L
    return [cur, cur + L]



def _slug_end(slug: str) -> Optional[float]:
    m = re.match(r"^[a-z0-9]+-updown-(\d+)m-(\d+)$", slug or "")
    return float(int(m.group(2)) + int(m.group(1)) * 60) if m else None


async def resolve_backlog(journal: "Journal", disc: "Discovery", stats: "Stats",
                          stop: asyncio.Event, hours: Optional[float] = None) -> None:
    """Старт: налитые позиции (и захлопы) завершённых окон за `hours` ч без записи 'resolved'
    — разрешить по gamma (closed=true) и дописать 'resolved' с source=gamma_backlog."""
    if not journal.path.exists():
        return
    if hours is None:
        hours = float(os.getenv("BACKLOG_H", "96") or 96)   # глубина поиска в журнале, ч
    now0 = time.time()
    cutoff = now0 - hours * 3600
    fills: Dict[tuple, List[float]] = {}
    locks: Dict[tuple, Dict[str, float]] = {}
    resolved: set = set()
    resolved_lk: set = set()
    try:
        with journal.path.open() as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("ts", 0) < cutoff:
                    continue
                ev, slug = r.get("event"), r.get("slug")
                k = (slug, r.get("side"), r.get("tier") or r.get("tier_name"))
                if ev == "fill":
                    a = fills.setdefault(k, [0.0, 0.0])
                    a[0] += float(r.get("usdc") or 0)
                    a[1] += float(r.get("shares") or 0)
                elif ev == "resolved":
                    resolved.add(k)
                elif ev == "lock_fill":
                    d = locks.setdefault(k, {"sh": 0.0, "tot": 0.0, "px": 0.0})
                    if r.get("total") is not None:
                        d["tot"] = max(d["tot"], float(r["total"]))
                    else:
                        d["sh"] += float(r.get("shares") or 0)
                    d["px"] = float(r.get("px") or d["px"] or 0)
                elif ev == "resolved_lock":
                    resolved_lk.add(k)
    except Exception as e:
        log.error("бэклог резолвов: чтение журнала: %s", e)
        return
    todo: Dict[str, list] = {}
    for k, (u, sh) in fills.items():
        end = _slug_end(k[0])
        if k in resolved or sh <= 0 or end is None or end > now0:
            continue                       # окно ещё идёт — его разрешит движок
        todo.setdefault(k[0], []).append(("fill", k, u, sh))
    for k, d in locks.items():
        end = _slug_end(k[0])
        sh = max(d["tot"], d["sh"])
        if k in resolved_lk or sh <= 0 or end is None or end > now0:
            continue
        todo.setdefault(k[0], []).append(("lock", k, round(sh * d["px"], 4), sh))
    if not todo:
        return
    log.warning("бэклог резолвов: %d окон с позициями без итога: %s", len(todo), ", ".join(sorted(todo)))
    pending = dict(todo)
    deadline = time.time() + Cfg.RESOLVE_TIMEOUT_S
    while pending and not stop.is_set() and time.time() < deadline:
        for slug in list(pending):
            winner = await asyncio.to_thread(disc.resolution, slug)
            if not winner:
                continue
            for kind, k, u, sh in pending.pop(slug):
                rec = {"event": "resolved" if kind == "fill" else "resolved_lock", "slug": slug,
                       "side": k[1], "winner": winner, "source": "gamma_backlog"}
                if kind == "fill":
                    won = k[1] == winner
                    pnl = (sh if won else 0.0) - u
                    stats.add(u, pnl, won)
                    rec["tier"] = k[2]
                else:
                    won = k[1] != winner      # захлоп = токен против своей стороны
                    pnl = (sh if won else 0.0) - u
                    stats.add_lock(u, pnl) if hasattr(stats, "add_lock") else None
                    rec["tier_name"] = k[2]
                rec.update(won=won, usdc=round(u, 4), shares=round(sh, 4), pnl=round(pnl, 4))
                await journal.write(rec)
                log.warning("БЭКЛОГ %s %s %s ступень %s: победил %s → %s %+.2f",
                            slug, kind, k[1], k[2], winner, "WIN" if won else "LOSS", pnl)
        if pending:
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
    if pending:
        log.error("бэклог резолвов: не разрешены %s", ", ".join(sorted(pending)))


async def main() -> None:
    _parse_tiers()
    setup_logging()
    log.info("конфиг: %s", ENV_FILE_USED or "файл не найден — окружение и умолчания")
    log.warning("РЕЖИМ: %s", "БУМАЖНЫЙ" if Cfg.PAPER else "БОЕВОЙ — ордера уходят на биржу")
    log.info("ступени: %s", ", ".join(
        f"{t.level:g} (с {t.ref:g} за {Cfg.FAST_S:.0f}с, потолок {t.cap:g}, "
        + (f"мин. ${stake_floor(t.cap)})" if Cfg.STAKE_PCT > 0 else f"${t.size:g})")
        for t in Cfg.TIERS))
    log.info("фильтр серий 5m: %s", (
        f"ON — минимум {Cfg.STREAK_5M_MIN} подряд закрытых свечей в сторону ставки "
        f"(активы {','.join(sorted(Cfg.STREAK_ASSETS))}; нет данных → {Cfg.STREAK_ON_ERROR})"
        if Cfg.STREAK_FILTER == "on" else "выкл"))
    log.info("фильтр подтверждения Binance: %s", "выкл" if Cfg.BN_CONFIRM == "off" else (
        ("только запись в журнал" if Cfg.BN_CONFIRM == "log" else "ПРОПУСК, если цена тоже обвалилась")
        + f" (активы {','.join(sorted(Cfg.BN_CONFIRM_ASSETS))}; остальные — только запись), "
          f"B={Cfg.BN_CONFIRM_B:g} K={Cfg.BN_CONFIRM_K:g} допуск {Cfg.BN_CONFIRM_TOL:g}, окно {Cfg.BN_CONFIRM_LOOK_S} с; "
          f"первые {Cfg.BN_CONFIRM_MIN_S} с после запуска фильтр не решает"))
    log.info("фильтр экстремума BTC: %s", "выкл" if Cfg.BTC_EXTREME == "off" else (
        ("только запись" if Cfg.BTC_EXTREME == "log" else "ПРОПУСК, если не «плохой» экстремум")
        + f" (активы {','.join(sorted(Cfg.BTC_EXTREME_ASSETS))}; "
          f"{Cfg.BTC_EXTREME_TF}m × {Cfg.BTC_EXTREME_N}: Up только на пике, Down только на дне; "
          f"нет данных → {Cfg.BTC_EXTREME_ON_ERROR})"))
    log.info("фильтр амплитудной свечи: %s", "выкл" if Cfg.BTC_AMP == "off" else (
        ("только запись" if Cfg.BTC_AMP == "log" else "ПРОПУСК, если нет амплитудной свечи (при режиме ИЛИ — если не прошёл и экстремум)")
        + f" (активы {','.join(sorted(Cfg.BTC_AMP_ASSETS))}; {Cfg.BTC_AMP_TF}m, последние {Cfg.BTC_AMP_LOOKBACK} закрытых, "
          f"диапазон >= {Cfg.BTC_AMP_MULT:g}× медианы {Cfg.BTC_AMP_REF_N} свечей; нет данных → {Cfg.BTC_AMP_ON_ERROR}); "
          f"сочетание с экстремумом: {'ИЛИ' if Cfg.BTC_FILTERS_MODE == 'or' else 'И'}"))
    log.info("исход по книге: %s", f"бид победителя >= {Cfg.BOOK_WIN_PX:g} все последние {Cfg.BOOK_HOLD_S:g} с, "
             "иначе gamma; gamma — сверка" if Cfg.RESOLVE_BY_BOOK else "выкл — только gamma")
    log.info("фильтры входа: сторона до обвала была >= [%s], Binance против <= %s bp",
             ", ".join(f"{t.name}: {_pre_min(t):g}" if _pre_on(t) else f"{t.name}: выкл" for t in Cfg.TIERS),
             Cfg.BN_MAX_ADVERSE_BP if Cfg.BN_MAX_ADVERSE_BP > 0 else "выкл")
    log.info("активы %s | ТФ %s | остаток >= %.0f%% | объём ×%g | часы %s | "
             "лимит окна $%g, часа $%g", Cfg.ASSETS, Cfg.TFS, Cfg.REM_MIN_FRAC * 100,
             Cfg.VOL_MULT, sorted(Cfg.HOURS) or "все", Cfg.MAX_PER_WINDOW, Cfg.MAX_PER_HOUR)
    log.info("докупка: $%g при mid >= прошлая покупка +%.3f, цена <= прошлая +%.3f, до %.2f, %s",
             Cfg.ADD_USDC, Cfg.ADD_STEP, Cfg.ADD_STEP + Cfg.ADD_TOL, Cfg.ADD_MAX_PX,
             "до конца окна" if Cfg.ADD_WINDOW_S <= 0 else f"{Cfg.ADD_WINDOW_S:.0f}с после входа")
    log.info("погоня: до %.0fс, пауза %.1f→%.1fс, потолок +%.2f каждые %.0fс (макс +%.2f), "
             "не позже %.0fс до конца окна, попыток до %d", Cfg.FILL_TIMEOUT_S,
             Cfg.RETRY_PAUSE_S, Cfg.RETRY_PAUSE_MAX_S, Cfg.ESC_STEP, Cfg.ESC_EVERY_S,
             Cfg.ESC_MAX, Cfg.END_GUARD_S, Cfg.MAX_TAKES)

    if Cfg.ENTRY_MODE == "trap":
        log.warning("ВХОД: ловушка-лимитка по уровень − %.2f (GTD post-only), стоит пока "
                    "падение до неё было бы быстрым; снятие id -> токен -> %s; опрос %.1fс",
                    Cfg.TRAP_OFFSET, "cancel_all" if Cfg.TRAP_CANCEL_ALL_FALLBACK else "стоп",
                    Cfg.TRAP_POLL_S)
    elif Cfg.ENTRY_MODE != "taker":
        raise SystemExit(f"ENTRY_MODE={Cfg.ENTRY_MODE!r}: нужно taker или trap")
    clob = Clob()
    if clob.enabled and Cfg.CANCEL_ALL_ON_START:
        await asyncio.to_thread(clob.cancel_all)
    global EQUITY
    journal = Journal(Cfg.JOURNAL)
    if Cfg.STAKE_PCT > 0:
        EQUITY = Equity()
        if Cfg.COMPOUND:
            EQUITY.load(journal.path)
        caps = sorted({t.cap for t in Cfg.TIERS})
        log.warning("ОБЪЁМ: %g%% от депозита $%.2f (база $%g%s) = $%s за ступень; минимум площадки "
                    "%g долей и $%g → $%s",
                    Cfg.STAKE_PCT, EQUITY.value, Cfg.DEPOSIT,
                    (f" + итог с комиссией {EQUITY.realized:+.2f} по {len(EQUITY.pnl)} позициям журнала"
                     + (f" с {Cfg.COMPOUND_FROM}" if Cfg.COMPOUND_FROM else "")) if Cfg.COMPOUND else ", без реинвеста",
                    "/".join(f"{stake_usdc(c):g}" for c in caps), Cfg.MIN_SHARES, Cfg.MIN_NOTIONAL,
                    "/".join(str(stake_floor(c)) for c in caps))
    filled, done = journal.load_recent()
    budget = Budget()
    for (u, _s) in filled.values():
        budget.add(u)                    # грубо: всё налитое за 2 часа — в текущий час
    stats = Stats()
    bn = Binance(Cfg.ASSETS)
    disc = Discovery()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sg in (_signal.SIGINT, _signal.SIGTERM):
        try:
            loop.add_signal_handler(sg, stop.set)
        except NotImplementedError:
            pass

    bg = [asyncio.create_task(bn.stats_loop(stop)), asyncio.create_task(bn.ws_loop(stop))]
    if Cfg.ENTRY_MODE == "trap" and clob.enabled:
        bg.append(asyncio.create_task(trap_poller(clob, stop)))
        if Cfg.HEARTBEAT_S > 0:
            bg.append(asyncio.create_task(heartbeat_loop(clob, stop)))
    bg.append(asyncio.create_task(stall_watch(journal, stop)))
    _bt = asyncio.create_task(resolve_backlog(journal, disc, stats, stop))
    _bt.add_done_callback(_bg_done)
    bg.append(_bt)
    t0 = time.time()
    while not stop.is_set() and time.time() - t0 < 30 and \
            not all(a in bn.stats for a in Cfg.ASSETS):
        await asyncio.sleep(0.5)
    missing = [a for a in Cfg.ASSETS if a not in bn.stats]
    if missing:
        log.error("нет минутной статистики Binance по %s — проверь BINANCE_REST", missing)

    tasks: Dict[str, asyncio.Task] = {}
    engines: Dict[str, Engine] = {}
    finals: List[asyncio.Task] = []
    try:
        while not stop.is_set():
            now = time.time()
            for asset in Cfg.ASSETS:
                for tf in Cfg.TFS:
                    for start in window_starts(tf, now):
                        if start + tf * 60 <= now or start - now > Cfg.PREROLL_S:
                            continue
                        slug = Discovery.slug(asset, tf, start)
                        if slug in tasks:
                            continue
                        mkt = await asyncio.to_thread(disc.fetch, asset, tf, start)
                        if mkt is None:
                            continue
                        eng = Engine(mkt, clob, journal, bn, budget, filled, done)
                        engines[slug] = eng
                        tasks[slug] = asyncio.create_task(watch_market(eng, stop))
            for slug in [s for s, t in tasks.items() if t.done()]:
                t = tasks.pop(slug)
                if not t.cancelled() and t.exception():
                    log.error("задача %s упала: %r", slug, t.exception())
                eng = engines.pop(slug, None)
                if eng is not None:
                    ft = asyncio.create_task(eng.finish(disc, stats))
                    ft.add_done_callback(_bg_done)
                    finals.append(ft)
            finals = [f for f in finals if not f.done()]
            disc.prune()
            try:
                await asyncio.wait_for(stop.wait(), timeout=Cfg.DISCOVERY_EVERY_S)
            except asyncio.TimeoutError:
                pass
    finally:
        log.info("останавливаюсь. %s", stats.line())
        if Cfg.ENTRY_MODE == "trap":
            toks = [tk for e in engines.values() for tk in e.m.tokens.values()]
            try:
                await asyncio.wait_for(trap_shutdown(clob, toks), timeout=30)
            except Exception as e:
                log.error("остановка ловушек: %s", e)
        for t in list(tasks.values()) + bg + finals:
            t.cancel()
        await asyncio.gather(*tasks.values(), *bg, *finals, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
