#!/usr/bin/env python3
"""
slotbot.py — отработка стабильных часовых слотов на Polymarket up/down.
Один файл + .env рядом. Плумбинг (резолв исхода, ранняя проверка в хвосте
окна, брокер, гигиена ордеров) взят из rozi.py без изменений; заменено
только ЯДРО СИГНАЛА.

    pip install requests py-clob-client-v2
    python slotbot.py --selftest    # проверки логики без сети
    python slotbot.py --slots       # печать таблицы слотов и расписания
    python slotbot.py               # работа (PM_DRY_RUN=1 по умолчанию)


ЧТО ЭТО ЗА СИГНАЛ
=================
На 117 578 разрешённых рынках (BTC/ETH/SOL/XRP, май–сентябрь 2026) для
каждого слота стенных часов ET измерена доля РАЗВОРОТОВ — случаев, когда
исход рынка противоположен исходу предыдущего рынка того же актива и тф.

  15m: 96 слотов, средняя 0.5142 без условий и 0.5300 при k>=2
  5m: 288 слотов, средняя 0.5049 без условий и 0.5092 при k>=2

`k` — длина серии одинаковых исходов, ЗАКОНЧИВШЕЙСЯ на предыдущем рынке.
k>=2 означает ровно одно: исход предыдущего рынка равен исходу
пред-предыдущего.

Отбор слотов в таблицу ниже — по устойчивости, а не по одному z. Данные
разбиты на 5 независимых блоков (P0 = только BTC 7 мая – 8 июл, затем
четыре куска по ~15 дней на всех четырёх активах). Слот попадает в таблицу,
если ВСЕ ПЯТЬ блоков лежат по одну сторону от 0.5 и общий перевес не меньше
PM_MIN_EDGE (по умолчанию 5 пп). Слоты с частотой выше 0.5 отрабатываются
ставкой на РАЗВОРОТ, слоты с частотой ниже 0.5 — ставкой на ПРОДОЛЖЕНИЕ.


ЧЕСТНО О КАЧЕСТВЕ СИГНАЛА — ЧИТАТЬ ДО PM_DRY_RUN=0
==================================================
1. Класс «часовые слоты» УЖЕ ПРОВАЛИЛ out-of-sample один раз. Отбор слотов
   по |z|>1.96 на первой половине истории и проверка на второй: +13.7 пп
   в выборке -> −2.45 пп вне её, знак сохранён в 44% слотов, корреляция
   edge(train) с edge(test) по всем 96 слотам 15m = −0.048.

2. Устойчивость по 5 блокам сужает круг кандидатов, но НЕ является
   проверкой вне выборки: все пять блоков — из того же файла, из которого
   слоты и отбирались.

3. На 15m стабильных слотов вдвое больше случайного (12 из 96 без условия
   и 9 из 96 при k>=2 против ожидаемых ~6). На 5m — 13 из 288 и 17 из 288
   против ожидаемых ~18, то есть НА УРОВНЕ ШУМА, несмотря на то что по |z|
   «значимых» слотов на 5m больше. Поэтому 5m по умолчанию выключен
   (PM_TFS=15m) и включается осознанно.

4. Активы сильно коррелированы: в 65% дней все четыре разрешаются
   одинаково. Значит n в каждой ячейке завышен примерно вдвое, и все z в
   этом семействе завышены соответственно.

Практический вывод, зашитый в дефолты: маленький размер, PM_DRY_RUN=1,
и журнал, по которому через 4–6 недель можно посчитать честный винрейт по
каждому слоту отдельно (kind="settled").


ЭКОНОМИКА ВХОДА
===============
Комиссия Polymarket: мейкер 0, тейкер `θ·p·(1−p)`, θ=0.07 для крипты.
На цене 0.51 это 1.75 цента за акцию.

    исполнение          полная цена     безубыточный винрейт
    мейкером (стоял)      0.5100              51.00%
    тейкером (пересёк)    0.5275              52.75%

ЗАПРЕТ НА ДОРОГОГО ТЕЙКЕРА (PM_AVOID_TAKER=1, по умолчанию включён)
Мейкер или тейкер решается в момент ПРИХОДА ордера: заявка, не пересёкшая
книгу при постановке, остаётся мейкером до конца окна, что бы дальше ни
произошло с аском. Поэтому правило чисто локальное — читаем аск перед каждой
попыткой и выбираем цену:

    аск <= 0.50            -> ставим 0.50, пересекаем книгу, платим аск (<=0.50)
    0.50 < аск <= 0.51     -> 0.51 пересёк бы книгу: ставим 0.50, встаём мейкером
    аск >= 0.52            -> ставим 0.51, висит мейкером весь остаток окна
    книги ещё нет          -> ставим 0.51, пересекать нечего

PM_NO_CROSS_MARGIN — запас на гонку между чтением книги и приходом ордера
(аск успевает опуститься на тик). При значении 0.01 заявка по 0.51 ставится
только когда аск не ниже 0.52.

Размен, который этим делается, стоит понимать: комиссия исчезает, но
появляется отбор по исполнению — мейкерская заявка наполняется охотнее там,
где рынок идёт против неё. На родственном сигнале тейкерский путь по 0.50
(полная цена ~0.5175) оказывался ВЫГОДНЕЕ мейкерского именно поэтому.
Величина отбора на этой сетке не измерена — её покажет поле "mode" в
журнале: винрейт строк mode="maker" против mode="taker".

Отменяем только после закрытия рынка: короткий срок жизни заявки режет край.


ЛЕСЕНКА РАЗМЕРА (PM_ESCALATE, по умолчанию ВЫКЛЮЧЕНА)
После каждой выигранной ИСПОЛНЕННОЙ сделки размер следующей умножается на 2,
максимум 5 сделок в цепочке (1x 2x 4x 8x 16x), пятая победа замыкает цикл и
возвращает к базе. Проигрыш сбрасывает сразу. Неисполненная лимитка не
считается вовсе — цепочка стоит на месте.

Исполнение проверяется по факту, а не по факту постановки: после закрытия
рынка спрашиваем у биржи налитый объём и только потом двигаем цепочку. Без
такой проверки бот считал бы победами неисполненные заявки. Если у клиента
нет метода для этого запроса, бот в боевом режиме не стартует.

Результаты цепочки применяются строго по времени ЗАКРЫТИЯ рынка, при
совпадении — по имени актива, чтобы последовательность была воспроизводимой.

Что показала история на расписании (XRP, 1494 сделки за 63 дня):

    плоский размер   P&L +121 ед.  оборот  762  край/оборот +0.1589  просадка  8x
    лесенка x2 до 5  P&L +329 ед.  оборот 2451  край/оборот +0.1340  просадка 69x

Край на единицу риска НЕ вырос: перестановочный тест того же ряда даёт
медиану +0.1582, наблюдённое сидит на 20-м перцентиле — шум. Выросла только
дисперсия: просадка впятеро, а ситуация «дошли до 16x и проиграли» случалась
81 раз за 63 дня. Лесенка не улучшает стратегию, она её масштабирует.

Слоты в таблице дают 55.6–68.0% в выборке. Запас над 52.75% есть, но он
целиком внутривыборочный — см. блок выше.


ТАЙМИНГ
=======
Решение принимается за PM_CHECK_LEAD секунд до открытия целевого окна.
К этому моменту победитель текущего окна уже стоит на бидe ~0.99, поэтому
ранняя проверка даёт тот же ответ, что и резолв после закрытия, но заявка
успевает встать к самому открытию. Проверено на данных в rozi.py.

Отличие от rozi.py: состояние серий между раундами НЕ хранится. Бот
просыпается только к тем окнам, чей СЛЕДУЮЩИЙ рынок попадает в таблицу, и
там читает ровно то, что нужно:
  - исход текущего окна — ранней проверкой на T-3с (нужен всегда: и
    разворот, и продолжение считаются от него);
  - исход предыдущего окна — из gamma outcomePrices (нужен только для
    условия k>=2).
Никакого дрейфа счётчиков и никакого прогрева.


ЧАСОВОЙ ПОЯС
============
Слоты привязаны к стенным часам ET. Вся история (май–сентябрь 2026) целиком
в EDT = UTC−4, поэтому данные НЕ различают «стенные часы Нью-Йорка» и
«фиксированный UTC−4». Разойдутся они 1 ноября 2026, когда в США кончится
летнее время. PM_SLOT_TZ по умолчанию America/New_York (гипотеза «сессия
США»); альтернатива PM_SLOT_TZ=fixed:-4 (гипотеза «просто UTC-сетка»).
Перед 1 ноября выбрать осознанно.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


# ═══════════════════════════════════════════════════ таблица слотов
# (слот ET, наблюдённая доля разворотов, направление ставки)
#   "rev"  — ставим на исход, ПРОТИВОПОЛОЖНЫЙ предыдущему
#   "cont" — ставим на ТОТ ЖЕ исход, что предыдущий
# Комментарий: n — размер выборки, z — против 0.5, «блок» — худший из пяти
# блоков, приведённый к стороне сигнала (для "cont" зеркально).

SLOT_TABLE: dict[tuple[str, str], tuple[tuple[str, float, str], ...]] = {

    # В таблице лежат ВСЕ слоты, прошедшие проверку устойчивости (5 блоков из
    # 5 по одну сторону). Часть из них имеет перевес меньше 5 пп и по
    # умолчанию отсекается фильтром PM_MIN_EDGE=0.05 — они оставлены здесь,
    # чтобы порог можно было и ослабить, а не только ужесточить.

    # ── 15m, без условия на k: 12 стабильных из 96, случайно ожидалось ~6 ──
    ("15m", "any"): (
        ("17:45", 0.6238, "rev"),    # n=311 z=+4.37 блок 0.567
        ("23:00", 0.3859, "cont"),   # n=311 z=-4.03 блок 0.533
        ("03:00", 0.5924, "rev"),    # n=314 z=+3.27 блок 0.516
        ("21:30", 0.5820, "rev"),    # n=311 z=+2.89 блок 0.550
        ("05:45", 0.5764, "rev"),    # n=314 z=+2.71 блок 0.516
        ("00:00", 0.5669, "rev"),    # n=314 z=+2.37 блок 0.531
        ("02:15", 0.5605, "rev"),    # n=314 z=+2.14 блок 0.531
        ("09:30", 0.5605, "rev"),    # n=314 z=+2.14 блок 0.516
        ("08:45", 0.4395, "cont"),   # n=314 z=-2.14 блок 0.516
        ("03:30", 0.4459, "cont"),   # n=314 z=-1.92 блок 0.531
        ("06:15", 0.5478, "rev"),    # n=314 z=+1.69 блок 0.516  (перевес <5пп)
        ("03:45", 0.5478, "rev"),    # n=314 z=+1.69 блок 0.516  (перевес <5пп)
    ),

    # ── 15m, при k>=2: 9 стабильных из 96, случайно ожидалось ~6 ──
    ("15m", "k2"): (
        ("09:30", 0.6303, "rev"),    # n=165 z=+3.35 блок 0.583
        ("17:45", 0.6287, "rev"),    # n=167 z=+3.33 блок 0.543
        ("03:15", 0.6016, "rev"),    # n=128 z=+2.30 блок 0.533
        ("00:00", 0.6000, "rev"),    # n=160 z=+2.53 блок 0.517
        ("07:15", 0.5926, "rev"),    # n=162 z=+2.36 блок 0.533
        ("08:15", 0.5763, "rev"),    # n=177 z=+2.03 блок 0.543
        ("04:45", 0.5742, "rev"),    # n=155 z=+1.85 блок 0.524
        ("01:00", 0.5679, "rev"),    # n=162 z=+1.73 блок 0.531
        ("03:30", 0.4375, "cont"),   # n=144 z=-1.50 блок 0.517
    ),

    # ── 5m, без условия на k: 13 стабильных из 288, ожидалось ~18 (ШУМ) ──
    ("5m", "any"): (
        ("22:50", 0.4106, "cont"),   # n=302 z=-3.11 блок 0.531
        ("23:50", 0.5875, "rev"),    # n=303 z=+3.04 блок 0.517
        ("17:55", 0.5861, "rev"),    # n=302 z=+2.99 блок 0.519
        ("03:40", 0.5752, "rev"),    # n=306 z=+2.63 блок 0.517
        ("23:05", 0.4257, "cont"),   # n=303 z=-2.59 блок 0.517
        ("21:55", 0.5662, "rev"),    # n=302 z=+2.30 блок 0.517
        ("01:20", 0.4346, "cont"),   # n=306 z=-2.29 блок 0.516
        ("02:20", 0.4379, "cont"),   # n=306 z=-2.17 блок 0.516
        ("15:15", 0.5578, "rev"),    # n=303 z=+2.01 блок 0.517
        ("02:40", 0.4444, "cont"),   # n=306 z=-1.94 блок 0.516
        ("09:05", 0.4510, "cont"),   # n=306 z=-1.71 блок 0.519  (перевес <5пп)
        ("09:30", 0.4542, "cont"),   # n=306 z=-1.60 блок 0.516  (перевес <5пп)
        ("06:55", 0.5392, "rev"),    # n=306 z=+1.37 блок 0.516  (перевес <5пп)
    ),

    # ── 5m, при k>=2: 17 стабильных из 288, ожидалось ~18 (ШУМ) ──
    ("5m", "k2"): (
        ("14:25", 0.6797, "rev"),    # n=153 z=+4.45 блок 0.552
        ("14:55", 0.6552, "rev"),    # n=174 z=+4.09 блок 0.536
        ("12:20", 0.3557, "cont"),   # n=149 z=-3.52 блок 0.615
        ("12:00", 0.6325, "rev"),    # n=166 z=+3.42 блок 0.533
        ("22:45", 0.6336, "rev"),    # n=131 z=+3.06 блок 0.533
        ("07:15", 0.6328, "rev"),    # n=128 z=+3.01 блок 0.543
        ("02:20", 0.3951, "cont"),   # n=162 z=-2.67 блок 0.514
        ("02:05", 0.3986, "cont"),   # n=143 z=-2.43 блок 0.543
        ("18:20", 0.5949, "rev"),    # n=158 z=+2.39 блок 0.533
        ("22:30", 0.5915, "rev"),    # n=142 z=+2.18 блок 0.520
        ("23:20", 0.5814, "rev"),    # n=172 z=+2.13 блок 0.514
        ("05:10", 0.4161, "cont"),   # n=137 z=-1.97 блок 0.515
        ("15:25", 0.4303, "cont"),   # n=165 z=-1.79 блок 0.531
        ("13:00", 0.4305, "cont"),   # n=151 z=-1.71 блок 0.515
        ("17:45", 0.5625, "rev"),    # n=144 z=+1.50 блок 0.520
        ("02:50", 0.4615, "cont"),   # n=156 z=-0.96 блок 0.524  (перевес <5пп)
        ("13:10", 0.5373, "rev"),    # n=134 z=+0.86 блок 0.514  (перевес <5пп)
    ),
}

TF_SEC = {"5m": 300, "15m": 900}
BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"

CRYPTO_THETA = 0.07     # комиссия тейкера = θ·p·(1−p); мейкер платит 0


# ═══════════════════════════════════════════════════════════════ env
def load_env(path: str = ".env"):
    p = Path(path)
    if not p.exists():
        p = Path(__file__).with_name(".env")
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
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
    return int(os.getenv(k, str(d)))


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


def taker_fee(price: float) -> float:
    return CRYPTO_THETA * price * (1.0 - price)


class Cfg:
    def __init__(self):
        self.TRADE_ASSETS = LS("PM_TRADE_ASSETS", ("btc", "eth", "sol", "xrp"))
        self.TFS = LS("PM_TFS", ("15m",))
        self.CONDS = LS("PM_CONDITIONS", ("any", "k2"))

        # ── отбор слотов из таблицы ──
        self.MIN_EDGE = F("PM_MIN_EDGE", 0.05)     # |частота − 0.5| не меньше
        self.SLOT_TZ = S("PM_SLOT_TZ", "America/New_York")

        # ── вход ──
        self.ENTRY_PRICE = F("PM_ENTRY_PRICE", 0.51)
        self.BUILDER_FEE = F("PM_BUILDER_FEE", 0.01)   # доля от тейкерного нотионала
        self.ENTRY_SIZE = F("PM_ENTRY_SIZE", 5.0)
        # не пересекать книгу дороже TAKER_MAX. Мейкер/тейкер определяется в
        # момент ПРИХОДА ордера: заявка, не пересёкшая книгу при постановке,
        # остаётся мейкером до конца окна, что бы дальше ни случилось.
        self.AVOID_TAKER = BL("PM_AVOID_TAKER", True)
        self.TAKER_MAX = F("PM_TAKER_MAX", 0.50)
        self.NO_CROSS_MARGIN = F("PM_NO_CROSS_MARGIN", 0.01)
        self.MAX_CONCURRENT = I("PM_MAX_CONCURRENT", 4)   # активов в одном раунде
        # один рынок на актив в один момент времени + пауза между рынками.
        # Слоты 5m и 15m местами перекрываются и стыкуются встык, поэтому
        # набор прореживается заранее (см. solo_schedule), а замок в рантайме
        # оставлен страховкой на случай сбоя расписания.
        # лесенка размера: после КАЖДОЙ выигранной исполненной сделки размер
        # следующей умножается на FACTOR, максимум STEPS сделок в цепочке,
        # затем сброс к базе. Неисполненная лимитка не считается вовсе:
        # цепочка не двигается ни вверх, ни вниз.
        self.ESCALATE = BL("PM_ESCALATE", False)
        self.ESCALATE_FACTOR = F("PM_ESCALATE_FACTOR", 2.0)
        self.ESCALATE_STEPS = I("PM_ESCALATE_STEPS", 5)
        self.ESCALATE_SCOPE = S("PM_ESCALATE_SCOPE", "asset").strip().lower()
        self.MAX_SIZE = F("PM_MAX_SIZE", 0.0)        # 0 = без потолка
        self.DRY_ASSUME_FILL = BL("PM_DRY_ASSUME_FILL", True)
        self.ONE_AT_A_TIME = BL("PM_ONE_AT_A_TIME", True)
        self.MIN_GAP_MIN = F("PM_MIN_GAP_MIN", 2.0)
        self.P_K2 = F("PM_P_K2", 0.49)      # частота срабатывания условия k>=2

        # ── ранняя проверка исхода в хвосте окна ──
        self.ARM_TRIGGER = F("PM_ARM_TRIGGER", 0.99)
        self.CHECK_LEAD = F("PM_CHECK_LEAD", 3.0)
        self.TAIL_WATCH = F("PM_TAIL_WATCH", 25.0)
        self.POLL_INTERVAL = F("PM_POLL_INTERVAL", 0.25)
        self.CONFIRM_POLLS = I("PM_CONFIRM_POLLS", 2)
        self.POLL_MAX_AGE = F("PM_POLL_MAX_AGE", 2.0)
        self.BOOK_TIMEOUT = F("PM_BOOK_TIMEOUT", 3.0)
        # заранее тянем токены целевого рынка, чтобы в момент решения gamma
        # уже не была в критическом пути
        self.PREFETCH_LEAD = F("PM_PREFETCH_LEAD", 25.0)
        self.PREFETCH_RETRY = F("PM_PREFETCH_RETRY", 2.0)

        # ── постановка заявок на ещё не открывшийся рынок ──
        self.PLACE_RETRY = F("PM_PLACE_RETRY", 0.25)
        self.PLACE_DEADLINE = F("PM_PLACE_DEADLINE", 10.0)

        # ── авторитетный резолв закрытого рынка ──
        self.RESOLVE_BID_HI = F("PM_RESOLVE_BID_HI", 0.90)
        self.RESOLVE_BID_LO = F("PM_RESOLVE_BID_LO", 0.10)
        self.RESOLVE_DELAY = F("PM_RESOLVE_DELAY", 2.0)
        self.RESOLVE_RETRY = F("PM_RESOLVE_RETRY", 1.0)
        self.RESOLVE_ATTEMPTS = I("PM_RESOLVE_ATTEMPTS", 20)
        self.GAMMA_FALLBACK = BL("PM_GAMMA_FALLBACK", True)

        # ── учёт результата поставленных ставок (только журнал) ──
        self.SETTLE_CHECK = BL("PM_SETTLE_CHECK", True)

        # ── гигиена ордеров ──
        self.AUTO_CANCEL_STALE = BL("PM_AUTO_CANCEL_STALE", True)
        self.CANCEL_AFTER = F("PM_CANCEL_AFTER", 5.0)

        self.GAMMA_HOST = S("GAMMA_HOST", "https://gamma-api.polymarket.com")
        self.CLOB_HOST = S("CLOB_HOST", "https://clob.polymarket.com")

        self.DRY_RUN = BL("PM_DRY_RUN", True)
        self.PRIVATE_KEY = S("PM_PRIVATE_KEY")
        self.FUNDER = S("PM_FUNDER")
        self.SIGNATURE_TYPE = I("PM_SIGNATURE_TYPE", 3)
        self.CHAIN_ID = I("CHAIN_ID", 137)

        self.MIN_ORDER_VALUE = F("PM_MIN_ORDER_VALUE", 1.05)
        self.LOG = S("PM_LOG", "./slotbot.jsonl")

    # ── таблица после фильтра по PM_MIN_EDGE ──
    def slots(self) -> dict[tuple[str, str], dict[str, tuple[float, str]]]:
        out: dict[tuple[str, str], dict[str, tuple[float, str]]] = {}
        for tf in self.TFS:
            for cond in self.CONDS:
                rows = SLOT_TABLE.get((tf, cond), ())
                sel = {sl: (rate, d) for sl, rate, d in rows
                       if abs(rate - 0.5) >= self.MIN_EDGE}
                if sel:
                    out[(tf, cond)] = sel
        return out

    def schedule(self) -> dict[tuple[str, str], dict[str, tuple[float, str]]]:
        """Таблица после прореживания под правило «один рынок на актив».

        Возвращает slots() как есть, если PM_ONE_AT_A_TIME=0.
        """
        tables = self.slots()
        if not self.ONE_AT_A_TIME:
            return tables
        keep = solo_schedule(tables, self.MIN_GAP_MIN, self.ENTRY_PRICE, self.P_K2)
        out = {}
        for (tf, cond), tbl in tables.items():
            sel = {sl: v for sl, v in tbl.items() if (tf, sl) in keep}
            if sel:
                out[(tf, cond)] = sel
        return out

    def step_size(self, step: int) -> float:
        """Размер заявки на ступени step (0 = база)."""
        sz = self.ENTRY_SIZE * (self.ESCALATE_FACTOR ** step if self.ESCALATE else 1.0)
        if self.MAX_SIZE:
            sz = min(sz, self.MAX_SIZE)
        return round(sz, 4)

    def tzinfo(self):
        if self.SLOT_TZ.lower().startswith("fixed:"):
            hours = float(self.SLOT_TZ.split(":", 1)[1])
            return timezone(timedelta(hours=hours))
        return ZoneInfo(self.SLOT_TZ)

    def check(self) -> list[str]:
        e = []
        if not set(self.TFS) <= set(TF_SEC):
            e.append(f"PM_TFS только из {sorted(TF_SEC)}")
        if not self.TFS:
            e.append("PM_TFS пуст")
        if not set(self.CONDS) <= {"any", "k2"}:
            e.append("PM_CONDITIONS только из any,k2")
        if not self.CONDS:
            e.append("PM_CONDITIONS пуст")
        if not self.TRADE_ASSETS:
            e.append("PM_TRADE_ASSETS пуст")
        if not set(self.TRADE_ASSETS) <= {"btc", "eth", "sol", "xrp"}:
            e.append("PM_TRADE_ASSETS: таблица измерена только на btc,eth,sol,xrp")
        if not self.slots():
            e.append(f"после фильтра PM_MIN_EDGE={self.MIN_EDGE} не осталось ни "
                     f"одного слота — торговать нечего")
        if not (0 < self.ENTRY_PRICE < 1):
            e.append("PM_ENTRY_PRICE должен быть в (0,1)")
        if self.ENTRY_SIZE <= 0:
            e.append("PM_ENTRY_SIZE должен быть > 0")
        if self.ENTRY_PRICE * self.ENTRY_SIZE < self.MIN_ORDER_VALUE:
            e.append(f"ордер {self.ENTRY_PRICE}@{self.ENTRY_SIZE} = "
                     f"${self.ENTRY_PRICE * self.ENTRY_SIZE:.2f} ниже биржевого "
                     f"минимума ${self.MIN_ORDER_VALUE:.2f} — будет отклонён")
        if self.MAX_CONCURRENT < 1:
            e.append("PM_MAX_CONCURRENT должен быть >= 1")
        if not (0 < self.TAKER_MAX < 1):
            e.append("PM_TAKER_MAX должен быть в (0,1)")
        if self.TAKER_MAX > self.ENTRY_PRICE:
            e.append("PM_TAKER_MAX выше PM_ENTRY_PRICE — тогда запрет на тейкера "
                     "не имеет смысла, снизь PM_TAKER_MAX")
        if self.NO_CROSS_MARGIN < 0:
            e.append("PM_NO_CROSS_MARGIN не может быть отрицательным")
        if self.TAKER_MAX * self.ENTRY_SIZE < self.MIN_ORDER_VALUE:
            e.append(f"откат на тейкерскую цену {self.TAKER_MAX}@{self.ENTRY_SIZE} = "
                     f"${self.TAKER_MAX * self.ENTRY_SIZE:.2f} ниже биржевого "
                     f"минимума ${self.MIN_ORDER_VALUE:.2f}")
        if self.MIN_EDGE < 0:
            e.append("PM_MIN_EDGE не может быть отрицательным")
        if self.MIN_GAP_MIN < 0:
            e.append("PM_MIN_GAP_MIN не может быть отрицательным")
        if not (0 < self.P_K2 <= 1):
            e.append("PM_P_K2 должен быть в (0,1]")
        if self.ESCALATE_SCOPE not in ("asset", "global"):
            e.append("PM_ESCALATE_SCOPE только asset или global")
        if self.ESCALATE:
            if self.ESCALATE_FACTOR <= 1:
                e.append("PM_ESCALATE_FACTOR должен быть > 1")
            if self.ESCALATE_STEPS < 2:
                e.append("PM_ESCALATE_STEPS должен быть >= 2 (иначе лесенки нет)")
            top = self.ENTRY_SIZE * self.ESCALATE_FACTOR ** (self.ESCALATE_STEPS - 1)
            if self.MAX_SIZE and self.MAX_SIZE < self.ENTRY_SIZE:
                e.append("PM_MAX_SIZE ниже базового PM_ENTRY_SIZE")
            if not self.MAX_SIZE and top / self.ENTRY_SIZE > 64:
                e.append(f"верхняя ступень {top:.0f} акций = x{top / self.ENTRY_SIZE:.0f} "
                         f"без потолка PM_MAX_SIZE — поставь потолок")
        if self.ONE_AT_A_TIME and not self.schedule():
            e.append("после прореживания расписания не осталось слотов")
        be_taker = self.ENTRY_PRICE * (1 + self.BUILDER_FEE) + taker_fee(self.ENTRY_PRICE)
        if be_taker >= 1.0:
            e.append("PM_ENTRY_PRICE слишком высок: с комиссией безубыток >= 100%")
        try:
            self.tzinfo()
        except Exception as ex:
            e.append(f"PM_SLOT_TZ={self.SLOT_TZ} не разбирается: {ex}")
        if not (0 < self.ARM_TRIGGER <= 1):
            e.append("PM_ARM_TRIGGER должен быть в (0,1]")
        if self.CHECK_LEAD <= 0:
            e.append("PM_CHECK_LEAD должен быть > 0")
        if self.TAIL_WATCH <= self.CHECK_LEAD:
            e.append("PM_TAIL_WATCH должен быть больше PM_CHECK_LEAD")
        if self.CONFIRM_POLLS < 1:
            e.append("PM_CONFIRM_POLLS должен быть >= 1")
        if self.POLL_INTERVAL <= 0:
            e.append("PM_POLL_INTERVAL должен быть > 0")
        if self.CONFIRM_POLLS * self.POLL_INTERVAL > self.TAIL_WATCH - self.CHECK_LEAD:
            e.append("PM_CONFIRM_POLLS * PM_POLL_INTERVAL не влезает в окно наблюдения")
        if self.BOOK_TIMEOUT <= 0:
            e.append("PM_BOOK_TIMEOUT должен быть > 0")
        if self.PREFETCH_LEAD <= self.CHECK_LEAD:
            e.append("PM_PREFETCH_LEAD должен быть больше PM_CHECK_LEAD, иначе "
                     "прогрев не успевает и gamma остаётся в критическом пути")
        if self.PREFETCH_RETRY <= 0:
            e.append("PM_PREFETCH_RETRY должен быть > 0")
        if not self.DRY_RUN:
            if not self.PRIVATE_KEY:
                e.append("PM_DRY_RUN=0, но PM_PRIVATE_KEY пуст")
            if not self.FUNDER:
                e.append("PM_DRY_RUN=0, но PM_FUNDER пуст")
            if self.SIGNATURE_TYPE not in (0, 1, 2, 3):
                e.append("PM_SIGNATURE_TYPE вне 0..3")
        return e

    def warnings(self) -> list[str]:
        w = []
        if self.ESCALATE:
            top = self.ENTRY_SIZE * self.ESCALATE_FACTOR ** (self.ESCALATE_STEPS - 1)
            if self.MAX_SIZE:
                top = min(top, self.MAX_SIZE)
            w.append(f"PM_ESCALATE=1: ступени "
                     f"{', '.join(f'{self.step_size(i):g}' for i in range(self.ESCALATE_STEPS))} "
                     f"акций, верх {top:g} = x{top / self.ENTRY_SIZE:.0f} от базы. "
                     f"На истории лесенка НЕ меняла край на единицу оборота "
                     f"(0.134 против 0.159 плоского, 20-й перцентиль перестановок), "
                     f"но увеличивала просадку с 13x базовой ставки до 70x")
            if self.ESCALATE_SCOPE == "global" and len(self.TRADE_ASSETS) > 1:
                w.append(f"PM_ESCALATE_SCOPE=global при {len(self.TRADE_ASSETS)} активах: "
                         f"все активы торгуют ОДНИ И ТЕ ЖЕ слоты, их рынки "
                         f"закрываются одновременно, а исходы совпадают в 65% дней. "
                         f"Цепочка тогда не растянута во времени, а складывает "
                         f"1+2+4+8 на одно коррелированное событие. Для нескольких "
                         f"активов правильный режим — asset")
            if not self.DRY_RUN:
                w.append("в бою цепочка двигается только по ФАКТУ исполнения: "
                         "если лимитка не налилась, сделка не считается ни победой, "
                         "ни поражением (строки kind=\"settled\" с filled=0)")
        if self.AVOID_TAKER:
            w.append(f"PM_AVOID_TAKER=1: заявка пересечёт книгу только при аске "
                     f"<= {self.TAKER_MAX}, иначе встанет на {self.ENTRY_PRICE} "
                     f"мейкером. Это меняет размен: комиссии нет, но появляется "
                     f"отбор по исполнению — мейкерская заявка наполняется чаще "
                     f"там, где рынок идёт против неё")
        be_maker = self.ENTRY_PRICE
        be_taker = self.ENTRY_PRICE * (1 + self.BUILDER_FEE) + taker_fee(self.ENTRY_PRICE)
        w.append(f"безубыточный винрейт: {be_maker:.2%} мейкером / "
                 f"{be_taker:.2%} тейкером (комиссия θ·p·(1−p) = "
                 f"{taker_fee(self.ENTRY_PRICE) * 100:.2f}¢)")
        worst = min((r for tbl in self.slots().values() for r, _ in tbl.values()
                     for r in (max(r, 1 - r),)), default=1.0)
        if worst <= be_taker:
            w.append(f"слабейший слот в наборе даёт {worst:.2%} — это НИЖЕ "
                     f"безубытка тейкера {be_taker:.2%}: подними PM_MIN_EDGE")
        if "5m" in self.TFS:
            w.append("PM_TFS включает 5m: устойчивых слотов там 13/288 и 17/288 "
                     "против случайно ожидаемых ~18 — то есть НА УРОВНЕ ШУМА. "
                     "На 15m было 12/96 и 9/96 против ~6")
        if self.ENTRY_PRICE > 0.52:
            w.append(f"PM_ENTRY_PRICE={self.ENTRY_PRICE}: на родственном сигнале "
                     f"ROI падал с +4.9% на 0.50 до +1.6% на 0.53 — выше 0.52 "
                     f"край съедается")
        if self.MIN_EDGE < 0.03:
            w.append(f"PM_MIN_EDGE={self.MIN_EDGE}: слоты с перевесом меньше 3 пп "
                     f"не окупают даже мейкерский безубыток")
        w.append("семейство «часовые слоты» уже провалило out-of-sample: отбор "
                 "по |z|>1.96 на первой половине дал +13.7 пп в выборке и "
                 "−2.45 пп вне её. Устойчивость по 5 блокам сузила круг, но "
                 "все блоки из того же файла — это НЕ проверка вне выборки")
        return w


CFG = Cfg()


# ═══════════════════════════════════════════════════════════════ время
def window_start(now: float, tf: str) -> int:
    w = TF_SEC[tf]
    return int(now) - (int(now) % w)


def slot_label(ts: int, tz) -> str:
    """Метка слота: стенные часы в PM_SLOT_TZ на момент ОТКРЫТИЯ рынка."""
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M")


def slug(asset: str, tf: str, start: int) -> str:
    return f"{asset}-updown-{tf}-{start}"


def opposite(side: str) -> str:
    return "down" if side == "up" else "up"


def slot_windows(tables: dict, p_k2: float) -> list[dict]:
    """Слоты как временные окна одного актива за сутки. Записи any и k2 на
    одном (tf, slot) — это ОДИН ордер, поэтому они схлопываются: частота
    берётся от any (срабатывает всегда), винрейт тоже от неё."""
    acc: dict[tuple[str, str], dict] = {}
    for (tf, cond), tbl in tables.items():
        for sl, (rate, d) in tbl.items():
            eff = rate if d == "rev" else 1.0 - rate      # винрейт нашей стороны
            k = (tf, sl)
            if k in acc:
                if cond == "any":
                    acc[k].update(wr=eff, p=1.0)
                acc[k]["cond"] = "any+k2"
            else:
                acc[k] = {"tf": tf, "slot": sl, "wr": eff, "cond": cond,
                          "p": 1.0 if cond == "any" else p_k2}
    out = []
    for (tf, sl), v in acc.items():
        h, m = int(sl[:2]), int(sl[3:])
        out.append({**v, "open": h * 60 + m, "close": h * 60 + m + TF_SEC[tf] // 60})
    return sorted(out, key=lambda x: x["close"])


def solo_schedule(tables: dict, gap: float, entry_price: float,
                  p_k2: float) -> set[tuple[str, str]]:
    """Максимум ожидаемого края при условии: окна одного актива не
    пересекаются и между закрытием одного и открытием следующего проходит
    не меньше gap минут.

    Точное решение, а не жадность: классическая задача о взвешенном выборе
    непересекающихся интервалов, ДП по отсортированным концам. Вес слота —
    ожидаемый край за сутки, p·(винрейт − цена входа): слот, срабатывающий
    через раз, не должен вытеснять слот, срабатывающий каждый день, только
    потому что у него винрейт выше.
    """
    w = slot_windows(tables, p_k2)
    n = len(w)
    if not n:
        return set()
    val = [x["p"] * (x["wr"] - entry_price) for x in w]
    dp = [0.0] * (n + 1)
    chosen: list[list[int]] = [[] for _ in range(n + 1)]
    for k in range(1, n + 1):
        x = w[k - 1]
        dp[k], chosen[k] = dp[k - 1], chosen[k - 1]
        j = 0
        for t in range(k - 1, -1, -1):
            if t == 0 or w[t - 1]["close"] <= x["open"] - gap:
                j = t
                break
        if val[k - 1] > 0 and dp[j] + val[k - 1] > dp[k]:
            dp[k], chosen[k] = dp[j] + val[k - 1], chosen[j] + [k - 1]
    return {(w[i]["tf"], w[i]["slot"]) for i in chosen[n]}


def entry_price_for(ask: float | None) -> tuple[float, str]:
    """Цена заявки по текущему аску. Возвращает (цена, режим).

    Мейкер/тейкер решается в момент прихода ордера, поэтому правило чисто
    локальное: если заявка не пересекла книгу при постановке, она мейкер до
    конца окна.

        аск <= TAKER_MAX                   -> берём тейкером, но не дороже TAKER_MAX
        TAKER_MAX < аск < ENTRY+MARGIN     -> ENTRY пересёк бы книгу, встаём на TAKER_MAX
        аск >= ENTRY_PRICE + MARGIN        -> ENTRY спокойно висит мейкером
        книги нет (аск не прочитан)        -> ENTRY, пересекать нечего

    MARGIN — запас на гонку между чтением книги и приходом ордера: аск может
    успеть опуститься на тик, и заявка по ENTRY стала бы тейкером по ENTRY.
    """
    if not CFG.AVOID_TAKER:
        return CFG.ENTRY_PRICE, "any"
    if ask is None:
        return CFG.ENTRY_PRICE, "maker"
    if ask <= CFG.TAKER_MAX:
        return CFG.TAKER_MAX, "taker"
    if ask >= CFG.ENTRY_PRICE + CFG.NO_CROSS_MARGIN:
        return CFG.ENTRY_PRICE, "maker"
    return CFG.TAKER_MAX, "maker"


def bet_side(prev_outcome: str, direction: str) -> str:
    """direction='rev' -> против предыдущего исхода; 'cont' -> тот же."""
    return opposite(prev_outcome) if direction == "rev" else prev_outcome


# ═══════════════════════════════════════════════════════════════ журнал
class Journal:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def write(self, kind: str, obj: dict):
        rec = {"kind": kind, "ts": time.time(), **obj}
        with self.lock, open(self.path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def log(msg: str):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


# ═══════════════════════════════════════════════════════════════ рынки
class Markets:
    """Слаг -> condition_id/токены/тик + чтение книги. Сессия своя на поток:
    все вызовы уходят через asyncio.to_thread, чтобы не блокировать loop."""

    def __init__(self):
        import requests
        self._requests = requests
        self._local = threading.local()
        self.cache: dict[str, dict] = {}
        self.outcomes: dict[str, str] = {}
        self.lock = threading.Lock()

    @property
    def s(self):
        s = getattr(self._local, "s", None)
        if s is None:
            s = self._requests.Session()
            s.headers["User-Agent"] = "slotbot/1.0"
            self._local.s = s
        return s

    def _events(self, sl: str) -> list:
        r = self.s.get(f"{CFG.GAMMA_HOST}/events", params={"slug": sl}, timeout=8)
        r.raise_for_status()
        evs = r.json()
        return evs if isinstance(evs, list) else [evs]

    @staticmethod
    def _jload(v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return None
        return v

    def resolve_sync(self, asset: str, tf: str, start: int) -> dict | None:
        sl = slug(asset, tf, start)
        with self.lock:
            hit = self.cache.get(sl)
        if hit:
            return hit
        try:
            evs = self._events(sl)
        except Exception as e:
            log(f"  gamma resolve {sl}: {e}")
            return None
        for ev in evs:
            mkts = (ev or {}).get("markets") or []
            if not mkts:
                continue
            m = mkts[0]
            toks = self._jload(m.get("clobTokenIds"))
            if not toks or len(toks) < 2:
                continue
            outs = self._jload(m.get("outcomes"))
            up, down = toks[0], toks[1]
            # порядок исходов НЕ гарантирован — разбираем по имени, не по индексу
            if outs and len(outs) >= 2:
                lo = [str(o).strip().lower() for o in outs[:2]]
                if lo[0] in ("down", "no"):
                    up, down = toks[1], toks[0]
            info = {
                "slug": sl,
                "condition_id": m.get("conditionId"),
                "up": str(up), "down": str(down),
                "tick_size": str(m.get("orderPriceMinTickSize") or "0.01"),
                "neg_risk": bool(m.get("negRisk", False)),
                "start": start, "end": start + TF_SEC[tf], "tf": tf, "asset": asset,
            }
            with self.lock:
                self.cache[sl] = info
            return info
        return None

    def outcome_via_gamma_sync(self, asset: str, tf: str, start: int) -> str | None:
        """Исход закрытого рынка по gamma outcomePrices — [1,0] / [0,1]."""
        sl = slug(asset, tf, start)
        with self.lock:
            hit = self.outcomes.get(sl)
        if hit:
            return hit
        try:
            evs = self._events(sl)
        except Exception:
            return None
        for ev in evs:
            for m in ((ev or {}).get("markets") or []):
                prices = self._jload(m.get("outcomePrices"))
                outs = self._jload(m.get("outcomes"))
                if not prices or len(prices) < 2:
                    continue
                try:
                    p0, p1 = float(prices[0]), float(prices[1])
                except (TypeError, ValueError):
                    continue
                if abs(p0 - p1) < 0.9:      # ещё не зарезолвлен
                    continue
                names = ([str(o).strip().lower() for o in outs[:2]]
                         if outs and len(outs) >= 2 else ["up", "down"])
                winner = names[0] if p0 > p1 else names[1]
                w = "up" if winner in ("up", "yes") else (
                    "down" if winner in ("down", "no") else None)
                if w:
                    with self.lock:
                        self.outcomes[sl] = w
                    return w
        return None

    def book_sync(self, token: str) -> tuple[float, float] | None:
        """(bid, ask). Цена исхода везде меряется по bid."""
        try:
            r = self.s.get(f"{CFG.CLOB_HOST}/book", params={"token_id": token},
                           timeout=CFG.BOOK_TIMEOUT)
            r.raise_for_status()
            b = r.json()
            bids = b.get("bids") or []
            asks = b.get("asks") or []
            bid = max((float(x["price"]) for x in bids), default=0.0)
            ask = min((float(x["price"]) for x in asks), default=1.0)
            return bid, ask
        except Exception:
            return None

    async def resolve(self, asset: str, tf: str, start: int) -> dict | None:
        return await asyncio.to_thread(self.resolve_sync, asset, tf, start)

    async def outcome_via_gamma(self, asset: str, tf: str, start: int) -> str | None:
        return await asyncio.to_thread(self.outcome_via_gamma_sync, asset, tf, start)

    async def book(self, token: str) -> tuple[float, float] | None:
        return await asyncio.to_thread(self.book_sync, token)


async def resolve_retry(mk: Markets, asset: str, tf: str, start: int,
                        deadline: float, every: float = 1.0) -> dict | None:
    """Следующий рынок может ещё не быть опубликован в gamma, когда мы
    запрашиваем его заранее. Долбим до дедлайна."""
    while True:
        info = await mk.resolve(asset, tf, start)
        if info:
            return info
        if time.time() >= deadline:
            return None
        await asyncio.sleep(every)


# ═══════════════════════════════════════════════════════════════ брокер
class Broker:
    """GTC-лимитка на конкретную цену/размер: встаёт в стакан и ждёт.
    Если аск уже <= нашей цены, заявка пересечёт книгу и исполнится тейкером
    по аску — ожидаемое поведение, см. шапку файла."""

    def __init__(self):
        self.c = None
        if not CFG.DRY_RUN:
            from py_clob_client_v2 import BuilderConfig, ClobClient
            self.c = ClobClient(host=CFG.CLOB_HOST, key=CFG.PRIVATE_KEY,
                                chain_id=CFG.CHAIN_ID, signature_type=CFG.SIGNATURE_TYPE,
                                funder=CFG.FUNDER,
                                builder_config=BuilderConfig(
                                    builder_address=BUILDER_ADDRESS,
                                    builder_code=BUILDER_CODE))
            creds = None
            for _name in ("create_or_derive_api_key", "derive_api_key",
                          "create_or_derive_api_creds", "create_api_key"):
                _fn = getattr(self.c, _name, None)
                if callable(_fn):
                    try:
                        creds = _fn()
                        log(f"api creds получены через {_name}()")
                        break
                    except Exception as e:
                        log(f"  {_name}() не сработал: {e}")
            if creds is None:
                raise SystemExit(
                    "не удалось получить api creds. Доступные методы клиента: "
                    + ", ".join(m for m in dir(self.c) if "api" in m.lower() or "key" in m.lower()))
            self.c.set_api_creds(creds)
            log(f"CLOB v2 подключён. funder={CFG.FUNDER} signature_type={CFG.SIGNATURE_TYPE}")
        self._dry_seq = 0
        self._lock = threading.Lock()

    def _quantize(self, price: float, tick_size: str) -> float:
        tick = float(tick_size)
        dec = len(tick_size.split(".")[1]) if "." in tick_size else 0
        return round(round(price / tick) * tick, dec)

    def place_limit(self, token: str, price: float, size: float, tick_size: str,
                    neg_risk: bool, tag: str, quiet: bool = False) -> str | None:
        price = self._quantize(price, tick_size)
        if CFG.DRY_RUN:
            with self._lock:
                self._dry_seq += 1
                oid = f"dry-{self._dry_seq:06d}"
            log(f"[DRY] {tag}: BUY {size} @ {price} (token ...{token[-6:]}) -> {oid}")
            return oid
        from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, PostOrdersV2Args
        from py_clob_client_v2.order_builder.constants import BUY
        try:
            signed = self.c.create_order(
                OrderArgs(token_id=token, price=price, size=size, side=BUY),
                options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk))
            resp = self.c.post_orders([PostOrdersV2Args(order=signed, orderType=OrderType.GTC)])
        except Exception as e:
            if not quiet:
                log(f"  {tag}: ордер не прошёл: {e}")
            return None
        r = resp[0] if isinstance(resp, list) and resp else resp
        if not isinstance(r, dict) or not r.get("success", True):
            if not quiet:
                log(f"  {tag}: отказ {(r or {}).get('errorMsg')}")
            return None
        oid = r.get("orderID") or r.get("orderId") or r.get("id")
        log(f"{tag}: BUY {size} @ {price} -> ордер {oid} статус {r.get('status')}")
        return str(oid) if oid else None

    _FILL_METHODS = ("get_order", "get_order_by_id", "order", "get_orders")

    def find_fill_method(self) -> str | None:
        """Ищем у клиента метод, которым можно спросить, налилась ли заявка.
        Имена в разных версиях py-clob-client-v2 отличаются, поэтому пробуем
        список, а не хардкодим одно."""
        if CFG.DRY_RUN:
            return "dry"
        for name in self._FILL_METHODS:
            if callable(getattr(self.c, name, None)):
                self._fill_method = name
                return name
        return None

    @staticmethod
    def _matched(obj) -> float | None:
        """Достаём налитый объём из ответа любой формы."""
        if obj is None:
            return None
        if isinstance(obj, list):
            vals = [Broker._matched(x) for x in obj]
            vals = [v for v in vals if v is not None]
            return sum(vals) if vals else None
        if not isinstance(obj, dict):
            obj = getattr(obj, "__dict__", None) or {}
        for k in ("size_matched", "sizeMatched", "matched_size", "matchedSize",
                  "filled_size", "filledSize", "filled"):
            if k in obj:
                try:
                    return float(obj[k])
                except (TypeError, ValueError):
                    return None
        return None

    def filled_size(self, order_id: str) -> float | None:
        """Сколько акций реально налилось. None = узнать не удалось."""
        if CFG.DRY_RUN:
            return CFG.ENTRY_SIZE if CFG.DRY_ASSUME_FILL else 0.0
        name = getattr(self, "_fill_method", None)
        if not name or not order_id:
            return None
        try:
            fn = getattr(self.c, name)
            resp = fn(order_id)
        except Exception as e:
            log(f"  статус ордера {order_id} не получен ({name}): {e}")
            return None
        return self._matched(resp)

    def cancel(self, order_id: str):
        if CFG.DRY_RUN or not order_id or order_id.startswith("dry-"):
            return
        try:
            self.c.cancel(order_id)
        except Exception as e:
            log(f"  отмена {order_id} не прошла: {e}")


# ═══════════════════════════════════════════════════════════ лесенка размера
class Escalator:
    """Цепочка «выиграл — удвоил». Считает ТОЛЬКО исполненные сделки.

    Правила:
      исполнена и выиграла   -> ступень +1; на STEPS-й победе цикл замыкается
                                и ступень возвращается к 0
      исполнена и проиграла  -> ступень 0
      НЕ исполнена           -> ступень не меняется (сделки не было)
      исход не определён     -> ступень 0 (не эскалируем на незнании)

    Порядок применения результатов — строго по времени ЗАКРЫТИЯ рынка, при
    совпадении по имени актива: цепочка обязана быть воспроизводимой.
    """

    def __init__(self):
        self.step: dict[str, int] = {}
        self.done = 0
        self.skipped = 0

    def _key(self, asset: str) -> str:
        return "*" if CFG.ESCALATE_SCOPE == "global" else asset

    def current_step(self, asset: str) -> int:
        return self.step.get(self._key(asset), 0) if CFG.ESCALATE else 0

    def size_for(self, asset: str) -> tuple[float, int]:
        st = self.current_step(asset)
        return CFG.step_size(st), st

    def on_result(self, asset: str, filled: bool, won: bool | None) -> tuple[int, int]:
        """Возвращает (ступень до, ступень после)."""
        k = self._key(asset)
        before = self.step.get(k, 0)
        if not filled:
            self.skipped += 1
            return before, before
        self.done += 1
        if won is True:
            after = before + 1
            if after >= CFG.ESCALATE_STEPS:
                after = 0
        else:
            after = 0
        self.step[k] = after
        return before, after

    def snapshot(self) -> dict:
        return {"scope": CFG.ESCALATE_SCOPE, "steps": dict(self.step),
                "settled": self.done, "unfilled": self.skipped}


# ═══════════════════════════════════════════════════ ядро сигнала: слоты
def slots_for(tf: str, label: str, tables: dict) -> list[dict]:
    """Все записи таблицы, попадающие в этот слот. Может быть 0, 1 или 2
    (условие any и условие k2 пересекаются — это нормально)."""
    out = []
    for (t, cond), tbl in tables.items():
        if t != tf:
            continue
        hit = tbl.get(label)
        if hit:
            out.append({"cond": cond, "rate": hit[0], "direction": hit[1]})
    return out


def decide(asset: str, entries: list[dict], prev_outcome: str | None,
           k_ge_2: bool | None) -> dict | None:
    """Одна ставка на актив. Записи any и k2 на одном слоте объединяются в
    ОДИН ордер: цель — та же сторона того же рынка, дублировать нечего.
    Если стороны разошлись — не ставим вовсе."""
    if prev_outcome is None:
        return None
    fired = []
    for e in entries:
        if e["cond"] == "k2":
            if k_ge_2 is None or not k_ge_2:
                continue
        fired.append(e)
    if not fired:
        return None
    sides = {bet_side(prev_outcome, e["direction"]) for e in fired}
    if len(sides) > 1:
        return {"asset": asset, "conflict": True,
                "conds": [e["cond"] for e in fired],
                "directions": [e["direction"] for e in fired]}
    best = max(fired, key=lambda e: abs(e["rate"] - 0.5))
    return {
        "asset": asset,
        "conflict": False,
        "side": sides.pop(),
        "prev_outcome": prev_outcome,
        "conds": [e["cond"] for e in fired],
        "directions": [e["direction"] for e in fired],
        "rates": [e["rate"] for e in fired],
        "edge": abs(best["rate"] - 0.5),
        "k_ge_2": k_ge_2,
    }


def pick_round(sigs: list[dict], max_concurrent: int) -> list[dict]:
    """Детерминированный порядок: больший перевес, затем имя актива."""
    live = [s for s in sigs if s and not s["conflict"]]
    live.sort(key=lambda s: (-s["edge"], s["asset"]))
    return live[:max_concurrent]


# ═══════════════════════════════════════════ авторитетный резолв окна
async def resolve_outcome(mk: Markets, asset: str, tf: str, start: int) -> str | None:
    """Кто выиграл закрытое окно: бид победителя сходится к ~1, проигравшего
    к ~0 за несколько секунд после закрытия."""
    info = mk.resolve_sync(asset, tf, start)
    if not info:
        return None
    await asyncio.sleep(CFG.RESOLVE_DELAY)
    bu = bd = None
    for _ in range(CFG.RESOLVE_ATTEMPTS):
        bu = await mk.book(info["up"])
        bd = await mk.book(info["down"])
        if bu and bd:
            up_bid, down_bid = bu[0], bd[0]
            if up_bid >= CFG.RESOLVE_BID_HI and down_bid <= CFG.RESOLVE_BID_LO:
                return "up"
            if down_bid >= CFG.RESOLVE_BID_HI and up_bid <= CFG.RESOLVE_BID_LO:
                return "down"
        await asyncio.sleep(CFG.RESOLVE_RETRY)
    if CFG.GAMMA_FALLBACK:
        g = await mk.outcome_via_gamma(asset, tf, start)
        if g:
            log(f"  {asset}/{tf}/{start}: книга не сошлась, исход {g} взят из gamma")
            return g
    if bu and bd:
        winner = "up" if bu[0] > bd[0] else "down"
        log(f"  ! {asset}/{tf}/{start}: исход не сошёлся чётко — беру {winner} по перевесу")
        return winner
    log(f"  ! {asset}/{tf}/{start}: книга недоступна, исход не определён")
    return None


async def past_outcome(mk: Markets, asset: str, tf: str, start: int) -> str | None:
    """Исход давно закрытого окна: сперва gamma (книга там уже пустая),
    затем книга как запасной путь."""
    g = await mk.outcome_via_gamma(asset, tf, start)
    if g:
        return g
    info = await mk.resolve(asset, tf, start)
    if not info:
        return None
    bu = await mk.book(info["up"])
    bd = await mk.book(info["down"])
    if not bu or not bd:
        return None
    if bu[0] >= CFG.RESOLVE_BID_HI and bd[0] <= CFG.RESOLVE_BID_LO:
        return "up"
    if bd[0] >= CFG.RESOLVE_BID_HI and bu[0] <= CFG.RESOLVE_BID_LO:
        return "down"
    return None


# ═══════════════════════════════════ ранняя проверка исхода в хвосте окна
async def early_outcome(mk: Markets, asset: str, tf: str, m_start: int,
                        W: int) -> tuple[str | None, dict]:
    """Опрашиваем ОБЕ стороны с T-TAIL_WATCH до T-CHECK_LEAD. Исход считается
    определённым, когда бид одной из сторон держится >= ARM_TRIGGER
    CONFIRM_POLLS опросов подряд и последнее наблюдение не старше
    POLL_MAX_AGE."""
    info = await resolve_retry(mk, asset, tf, m_start,
                               deadline=m_start + W - CFG.TAIL_WATCH - 1)
    if not info:
        return None, {"error": "market not resolvable"}

    watch_from = m_start + W - CFG.TAIL_WATCH
    check_at = m_start + W - CFG.CHECK_LEAD
    delay = watch_from - time.time()
    if delay > 0:
        await asyncio.sleep(delay)

    conf = {"up": 0, "down": 0}
    last_ok = {"up": 0.0, "down": 0.0}
    px = {"up": None, "down": None}
    while time.time() < check_at:
        # запрос ограничен оставшимся временем: цикл НЕ имеет права вернуться
        # позже check_at, иначе съедается весь бюджет на постановку заявки
        try:
            bu, bd = await asyncio.wait_for(
                asyncio.gather(mk.book(info["up"]), mk.book(info["down"])),
                timeout=max(0.05, check_at - time.time()))
        except (asyncio.TimeoutError, asyncio.CancelledError):
            break
        for side, bk in (("up", bu), ("down", bd)):
            if not bk:
                continue
            px[side] = bk[0]
            if bk[0] >= CFG.ARM_TRIGGER:
                conf[side] += 1
                last_ok[side] = time.time()
            else:
                conf[side] = 0
        if time.time() >= check_at:
            break
        await asyncio.sleep(CFG.POLL_INTERVAL)

    now = time.time()
    winner = None
    for side in ("up", "down"):
        if conf[side] >= CFG.CONFIRM_POLLS and (now - last_ok[side]) <= CFG.POLL_MAX_AGE:
            winner = side
            break
    return winner, {"bid_up": px["up"], "bid_down": px["down"],
                    "conf_up": conf["up"], "conf_down": conf["down"]}


# ═══════════════════════════════════════════════════ постановка заявки
async def place_entry(br: Broker, jr: Journal, mk: Markets, info: dict, side: str,
                      market_start: int, W: int, tag: str, stale_orders: list,
                      meta: dict, size: float) -> str | None:
    """Рынок в момент вызова ещё не открыт (T-CHECK_LEAD), поэтому заявка
    повторяется до PLACE_DEADLINE. Цена выбирается перед КАЖДОЙ попыткой по
    свежему аску: состояние книги между попытками меняется."""
    deadline = market_start + CFG.PLACE_DEADLINE
    token = info[side]
    t0 = time.time()
    attempt = 0
    oid = None
    price, mode, ask = CFG.ENTRY_PRICE, "any", None
    while True:
        attempt += 1
        if CFG.AVOID_TAKER:
            bk = await mk.book(token)
            ask = bk[1] if bk else None
            price, mode = entry_price_for(ask)
        oid = await asyncio.to_thread(
            br.place_limit, token, price, size, info["tick_size"], info["neg_risk"],
            f"{tag} {side}@{price}[{mode}]", attempt > 1 and time.time() < market_start)
        if oid:
            if attempt > 1:
                log(f"{tag}: заявка встала с {attempt}-й попытки (+{time.time() - t0:.1f}с)")
            if mode == "taker":
                log(f"{tag}: аск {ask} <= {CFG.TAKER_MAX} — берём тейкером")
            break
        if time.time() >= deadline:
            log(f"  ! {tag}: заявка так и не встала за {time.time() - t0:.1f}с — сдаюсь")
            break
        await asyncio.sleep(CFG.PLACE_RETRY)

    jr.write("order", {**meta, "side": side, "market_start": market_start,
                       "price": price, "size": size, "order_id": oid,
                       "ask_at_place": ask, "mode": mode, "attempts": attempt,
                       "placed_after_open": time.time() - market_start})
    if oid and CFG.AUTO_CANCEL_STALE:
        stale_orders.append((market_start + W, info["condition_id"], token, oid))
    return oid


async def fire(mk: Markets, br: Broker, jr: Journal, sig: dict, tf: str,
               next_start: int, W: int, stale_orders: list, settle: list,
               size: float, step: int):
    asset = sig["asset"]
    info = await resolve_retry(mk, asset, tf, next_start,
                               deadline=next_start + CFG.PLACE_DEADLINE)
    tag = f"{asset}/{tf} [{next_start}]"
    if not info:
        log(f"  ! {tag}: рынок не резолвится — заявка НЕ выставлена")
        jr.write("skip", {**sig, "tf": tf, "market_start": next_start,
                          "reason": "resolve failed"})
        return
    oid = await place_entry(br, jr, mk, info, sig["side"], next_start, W, tag,
                            stale_orders, {**sig, "tf": tf, "step": step}, size)
    if oid and CFG.SETTLE_CHECK:
        settle.append({**sig, "tf": tf, "market_start": next_start,
                       "market_end": next_start + W, "order_id": oid,
                       "step": step, "size": size})


# ═══════════════════════════════════════════════════════ основной цикл
async def prewarm(mk: Markets, tf: str, start: int, deadline: float):
    """Заранее кладём в кэш condition_id/токены/тик целевого рынка. Вызов
    из fire() потом попадает в кэш и gamma исчезает из критического пути."""
    await asyncio.gather(
        *(resolve_retry(mk, a, tf, start, deadline=deadline,
                        every=CFG.PREFETCH_RETRY) for a in CFG.TRADE_ASSETS),
        return_exceptions=True)


async def sleep_until(ts: float, stop: asyncio.Event) -> bool:
    """True — пора работать, False — попросили остановиться."""
    d = ts - time.time()
    if d <= 0:
        return True
    try:
        await asyncio.wait_for(stop.wait(), timeout=d)
        return False
    except asyncio.TimeoutError:
        return True


async def run_tf(tf: str, mk: Markets, br: Broker, jr: Journal,
                 tables: dict, tz, busy: dict, esc: Escalator,
                 stale_orders: list, settle: list, stop: asyncio.Event):
    W = TF_SEC[tf]
    need_k2 = any(cond == "k2" for (t, cond) in tables if t == tf)

    while not stop.is_set():
        start = window_start(time.time(), tf)
        end = start + W
        nxt = end
        label = slot_label(nxt, tz)
        entries = slots_for(tf, label, tables)

        # ── ничего в этом слоте: спим до следующей границы ──
        if not entries:
            if not await sleep_until(end + 0.5, stop):
                break
            continue

        # ── опоздали внутрь окна наблюдения: пропускаем раунд ──
        watch_from = end - CFG.TAIL_WATCH
        if time.time() > watch_from - 1.0:
            log(f"{tf} слот {label}: раунд начат слишком поздно — пропуск")
            jr.write("round_late", {"tf": tf, "slot": label, "market_start": nxt})
            if not await sleep_until(end + 0.5, stop):
                break
            continue

        conds = "+".join(sorted({e["cond"] for e in entries}))
        log(f"{tf} слот {label} [{conds}] — целевой рынок {nxt}, "
            f"жду хвост окна {start}")
        if not await sleep_until(nxt - CFG.PREFETCH_LEAD, stop):
            break
        warm = asyncio.create_task(prewarm(mk, tf, nxt, deadline=nxt - 0.5))
        if not await sleep_until(watch_from - 0.5, stop):
            warm.cancel()
            break

        # ── исход текущего окна (нужен всегда) ──
        early_res = await asyncio.gather(
            *(early_outcome(mk, a, tf, start, W) for a in CFG.TRADE_ASSETS))
        early = {a: r[0] for a, r in zip(CFG.TRADE_ASSETS, early_res)}
        diag = {a: r[1] for a, r in zip(CFG.TRADE_ASSETS, early_res)}

        # ── исход предыдущего окна (только если нужен k>=2) ──
        prev2: dict[str, str | None] = {a: None for a in CFG.TRADE_ASSETS}
        if need_k2 and any(e["cond"] == "k2" for e in entries):
            got = await asyncio.gather(
                *(past_outcome(mk, a, tf, start - W) for a in CFG.TRADE_ASSETS))
            prev2 = dict(zip(CFG.TRADE_ASSETS, got))

        # ── решение ──
        raw = []
        for a in CFG.TRADE_ASSETS:
            o = early.get(a)
            p2 = prev2.get(a)
            k_ge_2 = None if p2 is None or o is None else (p2 == o)
            raw.append(decide(a, entries, o, k_ge_2))
        for s in raw:
            if s and s["conflict"]:
                log(f"  ! {s['asset']}: условия {s['conds']} требуют разных сторон "
                    f"— ставка отменена")
                jr.write("conflict", {**s, "tf": tf, "slot": label, "market_start": nxt})
        sigs = pick_round(raw, CFG.MAX_CONCURRENT)

        board = "  ".join(
            f"{a}:{early.get(a) or '?'}"
            + (f"/k{'≥2' if prev2.get(a) == early.get(a) and early.get(a) else '=1'}"
               if need_k2 and prev2.get(a) else "")
            for a in CFG.TRADE_ASSETS)
        if sigs:
            desc = ", ".join(f"{s['asset']} {'+'.join(s['conds'])} -> BUY {s['side']}"
                             for s in sigs)
            log(f"{tf} [{label}] {board}  ==> {desc}")
        else:
            log(f"{tf} [{label}] {board}  (ставок нет)")
        jr.write("round", {"tf": tf, "slot": label, "market_start": nxt,
                           "prev_window": start, "early": early, "prev2": prev2,
                           "entries": entries, "signals": sigs, "diag": diag})

        # ── заявки на ещё не открывшийся рынок ──
        # замок «один рынок на актив». Расписание уже прорежено так, что
        # конфликтов быть не должно; это страховка на случай сдвига времени
        # или ручной правки таблицы.
        if CFG.ONE_AT_A_TIME:
            free = []
            for sg in sigs:
                until = busy.get(sg["asset"], 0.0)
                if nxt < until:
                    log(f"  ! {sg['asset']}: рынок занят до "
                        f"{datetime.fromtimestamp(until, timezone.utc):%H:%M:%S}Z "
                        f"— слот {label} пропущен")
                    jr.write("locked", {**sg, "tf": tf, "slot": label,
                                        "market_start": nxt, "busy_until": until})
                else:
                    free.append(sg)
            for sg in free:
                busy[sg["asset"]] = nxt + W + CFG.MIN_GAP_MIN * 60
            sigs = free

        warmed = sum(1 for a in CFG.TRADE_ASSETS
                     if slug(a, tf, nxt) in mk.cache)
        if sigs and warmed < len(CFG.TRADE_ASSETS):
            log(f"  прогрев целевого рынка неполный ({warmed}/{len(CFG.TRADE_ASSETS)}) "
                f"— gamma останется в критическом пути")
        for s in sigs:
            size, step = esc.size_for(s["asset"])
            if step:
                log(f"  {s['asset']}: ступень {step} -> размер {size:g} "
                    f"(x{size / CFG.ENTRY_SIZE:g} от базы)")
            asyncio.create_task(fire(mk, br, jr, s, tf, nxt, W, stale_orders,
                                     settle, size, step))

        # ── сверка ранней проверки с авторитетным резолвом ──
        if not await sleep_until(end + 0.5, stop):
            break
        real = await asyncio.gather(
            *(resolve_outcome(mk, a, tf, start) for a in CFG.TRADE_ASSETS))
        for a, out in zip(CFG.TRADE_ASSETS, real):
            if out is not None and early.get(a) is not None and early[a] != out:
                log(f"  ! {a}/{tf} [{start}]: ранняя проверка разошлась — "
                    f"ждали {early[a]}, резолв {out}")
                jr.write("early_mismatch", {"asset": a, "tf": tf, "start": start,
                                            "early": early[a], "resolved": out})


# ═══════════════════════════════════════════════ учёт результата ставок
async def settle_watch(mk: Markets, br: Broker, jr: Journal, esc: Escalator,
                       settle: list, stop: asyncio.Event):
    """Через RESOLVE_DELAY после закрытия целевого рынка узнаём исход и
    пишем в журнал выиграла ставка или нет. Только учёт, на торговлю не
    влияет — но именно по этим строкам потом считается честный винрейт
    каждого слота."""
    if not CFG.SETTLE_CHECK:
        return
    while not stop.is_set():
        await asyncio.sleep(2.0)
        now = time.time()
        ready, still = [], []
        for it in settle:
            (ready if now >= it["market_end"] + CFG.RESOLVE_DELAY + 3.0
             else still).append(it)
        settle[:] = still
        # цепочка обязана быть воспроизводимой: сперва по времени закрытия
        # рынка, при совпадении — по имени актива
        for it in sorted(ready, key=lambda x: (x["market_end"], x["asset"])):
            fill = await asyncio.to_thread(br.filled_size, it["order_id"])
            filled = bool(fill and fill > 0)
            out = await resolve_outcome(mk, it["asset"], it["tf"], it["market_start"])
            won = None if out is None else (out == it["side"])
            before, after = esc.on_result(it["asset"], filled, won)
            jr.write("settled", {**it, "outcome": out, "won": won,
                                 "filled": fill, "was_filled": filled,
                                 "step_before": before, "step_after": after,
                                 "chain": esc.snapshot()})
            if not filled:
                log(f"  итог {it['asset']}/{it['tf']} [{it['market_start']}]: "
                    f"лимитка не налилась ({fill}) — в цепочку не идёт")
                continue
            nxt_sz = CFG.step_size(after)
            log(f"  итог {it['asset']}/{it['tf']} [{it['market_start']}] "
                f"слот {it.get('slot', '')}: ставили {it['side']} на {fill:g} акций, "
                f"вышло {out} -> {'ПЛЮС' if won else 'минус'}"
                + (f" | ступень {before}->{after}, следующая заявка {nxt_sz:g}"
                   if CFG.ESCALATE else ""))


async def cleanup_stale(br: Broker, stale_orders: list, stop: asyncio.Event):
    """Не часть стратегии: снимаем неисполненные лимитки на уже закрытых
    рынках. Раньше закрытия окна не отменяем — короткий срок жизни заявки
    режет край отбором по исполнению."""
    if not CFG.AUTO_CANCEL_STALE:
        return
    while not stop.is_set():
        await asyncio.sleep(1.0)
        now = time.time()
        still = []
        for market_end, condition_id, token, oid in stale_orders:
            if now >= market_end + CFG.CANCEL_AFTER:
                await asyncio.to_thread(br.cancel, oid)
                log(f"  снял неисполненную лимитку {oid} (рынок уже закрылся)")
            else:
                still.append((market_end, condition_id, token, oid))
        stale_orders[:] = still


# ═══════════════════════════════════════════════════ печать расписания
def expected_orders_per_day(tables: dict, n_assets: int, max_concurrent: int) -> float:
    """Верхняя оценка: слоты any срабатывают всегда, k2 — примерно в
    половине случаев (k>=2 наблюдалось в 48.6% рынков 15m и 49.5% 5m)."""
    P_K2 = 0.49
    per_day = 0.0
    tfs = {t for (t, _) in tables}
    for tf in tfs:
        labels: dict[str, float] = {}
        for (t, cond), tbl in tables.items():
            if t != tf:
                continue
            p = 1.0 if cond == "any" else P_K2
            for sl in tbl:
                labels[sl] = max(labels.get(sl, 0.0), p)   # any и k2 -> один ордер
        per_day += sum(labels.values()) * min(n_assets, max_concurrent)
    return per_day


def print_slots(cfg: Cfg):
    tables = cfg.schedule()
    full = cfg.slots()
    tz = cfg.tzinfo()
    be_t = cfg.ENTRY_PRICE * (1 + cfg.BUILDER_FEE) + taker_fee(cfg.ENTRY_PRICE)
    print(f"\nчасовой пояс слотов: {cfg.SLOT_TZ}  (сейчас "
          f"{datetime.now(tz):%H:%M} по нему)")
    print(f"цена входа {cfg.ENTRY_PRICE} × {cfg.ENTRY_SIZE} акций = "
          f"${cfg.ENTRY_PRICE * cfg.ENTRY_SIZE:.2f} за ордер")
    print(f"безубыток: {cfg.ENTRY_PRICE:.2%} мейкером / {be_t:.2%} тейкером\n")
    for (tf, cond), tbl in sorted(tables.items()):
        print(f"── {tf}, условие {cond}: {len(tbl)} слотов")
        for sl, (rate, d) in sorted(tbl.items()):
            side = "против предыдущего" if d == "rev" else "тот же, что предыдущий"
            eff = rate if d == "rev" else 1 - rate
            mark = " " if eff > be_t else "!"
            print(f"   {mark} {sl}  частота {rate:.4f}  -> ставим {side}"
                  f"   (винрейт слота {eff:.2%})")
        print()
    n = expected_orders_per_day(tables, len(cfg.TRADE_ASSETS), cfg.MAX_CONCURRENT)
    print(f"ожидаемо ~{n:.0f} ордеров в сутки на {len(cfg.TRADE_ASSETS)} активах, "
          f"это ~${n * cfg.ENTRY_PRICE * cfg.ENTRY_SIZE:.0f} оборота в сутки")
    print('"!" — слот, чей винрейт в выборке ниже безубытка тейкера')

    if cfg.ONE_AT_A_TIME:
        w = slot_windows(tables, cfg.P_K2)
        wf = slot_windows(full, cfg.P_K2)
        keep = {(x["tf"], x["slot"]) for x in w}
        drop = [x for x in wf if (x["tf"], x["slot"]) not in keep]
        byopen = sorted(w, key=lambda x: x["open"])
        gaps = [byopen[i + 1]["open"] - byopen[i]["close"] for i in range(len(byopen) - 1)]
        ev_k = sum(x["p"] * (x["wr"] - cfg.ENTRY_PRICE) for x in w)
        ev_f = sum(x["p"] * (x["wr"] - cfg.ENTRY_PRICE) for x in wf)
        n_k = sum(x["p"] for x in w)
        n_f = sum(x["p"] for x in wf)
        print(f"\n── расписание «один рынок на актив», пауза >= {cfg.MIN_GAP_MIN:g} мин")
        print(f"   оставлено {len(w)} слотов из {len(wf)}; "
              f"{n_k:.1f} ордеров на актив в сутки вместо {n_f:.1f} "
              f"({n_k / n_f - 1:+.0%}), ожидаемый край {ev_k / ev_f:.0%} от полной сетки")
        print(f"   минимальная пауза между рынками: {min(gaps):g} мин, "
              f"после последнего до полуночи {24 * 60 - byopen[-1]['close']:g} мин")
        if drop:
            print("   выброшены (перекрывались или стояли встык):")
            for x in sorted(drop, key=lambda y: y["open"]):
                print(f"     {x['slot']} {x['tf']:>3} [{x['cond']}] "
                      f"винрейт {x['wr']:.4f} p={x['p']:.2f}")
    print()


# ═══════════════════════════════════════════════════════════════ selftest
def selftest():
    ok = True

    def chk(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'ok ' if cond else 'FAIL'}] {name}")

    print("\n1. сторона ставки")
    chk("rev от up -> down", bet_side("up", "rev") == "down")
    chk("rev от down -> up", bet_side("down", "rev") == "up")
    chk("cont от up -> up", bet_side("up", "cont") == "up")
    chk("cont от down -> down", bet_side("down", "cont") == "down")
    chk("opposite", opposite("up") == "down" and opposite("down") == "up")

    print("\n2. таблица слотов: целостность")
    bad_fmt = [(k, sl) for k, rows in SLOT_TABLE.items() for sl, _, _ in rows
               if len(sl) != 5 or sl[2] != ":" or not sl.replace(":", "").isdigit()]
    chk("все метки в формате HH:MM", not bad_fmt)
    bad_grid = [(k, sl) for k, rows in SLOT_TABLE.items() for sl, _, _ in rows
                if int(sl[3:]) % (TF_SEC[k[0]] // 60) != 0]
    chk("все метки лежат на сетке своего тф", not bad_grid)
    bad_dir = [(k, sl) for k, rows in SLOT_TABLE.items() for sl, r, d in rows
               if (d == "rev") != (r > 0.5)]
    chk("направление согласовано со стороной от 0.5", not bad_dir)
    dups = [k for k, rows in SLOT_TABLE.items()
            if len({sl for sl, _, _ in rows}) != len(rows)]
    chk("нет дублей слотов внутри одной таблицы", not dups)
    conflicts = []
    for tf in TF_SEC:
        a = {sl: d for sl, _, d in SLOT_TABLE.get((tf, "any"), ())}
        b = {sl: d for sl, _, d in SLOT_TABLE.get((tf, "k2"), ())}
        conflicts += [(tf, sl) for sl in set(a) & set(b) if a[sl] != b[sl]]
    chk("any и k2 нигде не требуют разных сторон", not conflicts)

    print("\n3. отбор по PM_MIN_EDGE")
    saved = CFG.MIN_EDGE, CFG.TFS, CFG.CONDS
    CFG.TFS, CFG.CONDS = ("15m", "5m"), ("any", "k2")
    CFG.MIN_EDGE = 0.0
    n_all = sum(len(t) for t in CFG.slots().values())
    CFG.MIN_EDGE = 0.05
    n_5 = sum(len(t) for t in CFG.slots().values())
    CFG.MIN_EDGE = 0.13
    n_13 = sum(len(t) for t in CFG.slots().values())
    chk(f"фильтр сужает набор ({n_all} -> {n_5} -> {n_13})", n_all > n_5 > n_13)
    CFG.MIN_EDGE = 0.99
    chk("невозможный порог оставляет пусто и ловится check()",
        not CFG.slots() and any("MIN_EDGE" in e for e in CFG.check()))
    CFG.MIN_EDGE, CFG.TFS, CFG.CONDS = saved

    print("\n4. решение на слоте")
    ent_rev = [{"cond": "any", "rate": 0.62, "direction": "rev"}]
    d = decide("btc", ent_rev, "up", None)
    chk("any срабатывает без знания k", d and d["side"] == "down")
    ent_k2 = [{"cond": "k2", "rate": 0.63, "direction": "rev"}]
    chk("k2 не срабатывает при k=1", decide("btc", ent_k2, "up", False) is None)
    chk("k2 не срабатывает при неизвестном k", decide("btc", ent_k2, "up", None) is None)
    d = decide("btc", ent_k2, "up", True)
    chk("k2 срабатывает при k>=2", d and d["side"] == "down")
    chk("без исхода предыдущего окна ставки нет", decide("btc", ent_rev, None, True) is None)
    both = ent_rev + [{"cond": "k2", "rate": 0.63, "direction": "rev"}]
    d = decide("btc", both, "up", True)
    chk("any+k2 на одной стороне дают ОДНУ ставку",
        d and d["side"] == "down" and set(d["conds"]) == {"any", "k2"})
    chk("перевес берётся по сильнейшей записи", abs(d["edge"] - 0.13) < 1e-9)
    d = decide("btc", both, "up", False)
    chk("при k=1 из пары остаётся только any", d and d["conds"] == ["any"])
    clash = [{"cond": "any", "rate": 0.60, "direction": "rev"},
             {"cond": "k2", "rate": 0.40, "direction": "cont"}]
    d = decide("btc", clash, "up", True)
    chk("расхождение сторон -> ставка отменена", d and d["conflict"])
    ent_cont = [{"cond": "any", "rate": 0.39, "direction": "cont"}]
    d = decide("btc", ent_cont, "down", None)
    chk("минусовой слот ставит на продолжение", d and d["side"] == "down")

    print("\n4b. выбор цены: не быть тейкером дороже PM_TAKER_MAX")
    sv = (CFG.AVOID_TAKER, CFG.ENTRY_PRICE, CFG.TAKER_MAX, CFG.NO_CROSS_MARGIN)
    CFG.AVOID_TAKER, CFG.ENTRY_PRICE = True, 0.51
    CFG.TAKER_MAX, CFG.NO_CROSS_MARGIN = 0.50, 0.01
    chk("книги нет -> 0.51 мейкером", entry_price_for(None) == (0.51, "maker"))
    chk("аск 0.60 -> 0.51 мейкером", entry_price_for(0.60) == (0.51, "maker"))
    chk("аск 0.52 -> 0.51 мейкером", entry_price_for(0.52) == (0.51, "maker"))
    chk("аск 0.51 -> откат на 0.50, не пересекаем",
        entry_price_for(0.51) == (0.50, "maker"))
    chk("аск 0.505 -> откат на 0.50", entry_price_for(0.505) == (0.50, "maker"))
    chk("аск 0.50 -> тейкер по 0.50", entry_price_for(0.50) == (0.50, "taker"))
    chk("аск 0.42 -> тейкер, заплатим 0.42", entry_price_for(0.42) == (0.50, "taker"))
    chk("ни при каком аске цена не выше 0.51",
        all(entry_price_for(a / 100)[0] <= 0.51 for a in range(1, 100)))
    chk("тейкерский режим только при аске <= 0.50",
        all((entry_price_for(a / 100)[1] == "taker") == (a / 100 <= 0.50)
            for a in range(1, 100)))
    CFG.NO_CROSS_MARGIN = 0.02
    chk("больший запас: аск 0.52 уже уводит на 0.50",
        entry_price_for(0.52) == (0.50, "maker"))
    CFG.NO_CROSS_MARGIN = 0.01
    CFG.AVOID_TAKER = False
    chk("выключенный флаг возвращает старое поведение",
        entry_price_for(0.30) == (0.51, "any"))
    (CFG.AVOID_TAKER, CFG.ENTRY_PRICE, CFG.TAKER_MAX, CFG.NO_CROSS_MARGIN) = sv

    print("\n4c. расписание «один рынок на актив»")
    sv = (CFG.ONE_AT_A_TIME, CFG.MIN_GAP_MIN, CFG.TFS, CFG.CONDS, CFG.MIN_EDGE)
    CFG.ONE_AT_A_TIME, CFG.MIN_GAP_MIN = True, 2.0
    CFG.TFS, CFG.CONDS, CFG.MIN_EDGE = ("15m", "5m"), ("any", "k2"), 0.05
    w = slot_windows(CFG.schedule(), CFG.P_K2)
    byo = sorted(w, key=lambda x: x["open"])
    gaps = [byo[i + 1]["open"] - byo[i]["close"] for i in range(len(byo) - 1)]
    chk(f"нет пересечений и пауза >= 2 мин (минимум {min(gaps)})", min(gaps) >= 2)
    chk("последнее окно закрывается до полуночи с запасом",
        24 * 60 - byo[-1]["close"] >= 2)
    chk("одновременных стартов не осталось",
        len({x["open"] for x in byo}) == len(byo))
    full = slot_windows(CFG.slots(), CFG.P_K2)
    chk(f"расписание — подмножество сетки ({len(w)} из {len(full)})",
        len(w) < len(full) and
        {(x["tf"], x["slot"]) for x in w} <= {(x["tf"], x["slot"]) for x in full})
    ev_k = sum(x["p"] * (x["wr"] - CFG.ENTRY_PRICE) for x in w)
    ev_f = sum(x["p"] * (x["wr"] - CFG.ENTRY_PRICE) for x in full)
    chk(f"край сохранён на {ev_k / ev_f:.0%}", ev_k / ev_f > 0.75)
    CFG.ONE_AT_A_TIME = False
    chk("выключенный флаг возвращает полную сетку",
        len(slot_windows(CFG.schedule(), CFG.P_K2)) == len(full))
    CFG.ONE_AT_A_TIME = True

    # ДП должен быть оптимальным, а не жадным: проверка на ручном примере
    toy = {("15m", "any"): {"00:00": (0.55, "rev"), "00:20": (0.90, "rev")},
           ("5m", "any"): {"00:10": (0.99, "rev")}}
    got = solo_schedule(toy, 2.0, 0.51, 0.49)
    chk("жадность взяла бы 00:00+00:10, ДП берёт 00:10+... — выбрана пара с "
        f"большим краем ({sorted(got)})",
        got == {("5m", "00:10"), ("15m", "00:20")})
    toy2 = {("15m", "any"): {"00:00": (0.52, "rev")}}
    chk("слот с краем ниже цены входа не берётся вовсе",
        solo_schedule(toy2, 2.0, 0.51, 0.49) == {("15m", "00:00")} and
        solo_schedule({("15m", "any"): {"00:00": (0.505, "rev")}}, 2.0, 0.51, 0.49) == set())
    (CFG.ONE_AT_A_TIME, CFG.MIN_GAP_MIN, CFG.TFS, CFG.CONDS, CFG.MIN_EDGE) = sv

    print("\n4d. лесенка размера")
    sv = (CFG.ESCALATE, CFG.ESCALATE_FACTOR, CFG.ESCALATE_STEPS,
          CFG.ESCALATE_SCOPE, CFG.ENTRY_SIZE, CFG.MAX_SIZE)
    CFG.ESCALATE, CFG.ESCALATE_FACTOR, CFG.ESCALATE_STEPS = True, 2.0, 5
    CFG.ESCALATE_SCOPE, CFG.ENTRY_SIZE, CFG.MAX_SIZE = "global", 5.0, 0.0
    chk("ступени 5,10,20,40,80",
        [CFG.step_size(i) for i in range(5)] == [5.0, 10.0, 20.0, 40.0, 80.0])
    e = Escalator()
    chk("старт с базы", e.size_for("xrp") == (5.0, 0))
    e.on_result("xrp", True, True)
    chk("после победы удвоение", e.size_for("xrp") == (10.0, 1))
    e.on_result("xrp", True, True); e.on_result("xrp", True, True)
    chk("три победы -> x8", e.size_for("xrp") == (40.0, 3))
    e.on_result("xrp", False, True)
    chk("НЕИСПОЛНЕННАЯ победа не двигает цепочку", e.size_for("xrp") == (40.0, 3))
    e.on_result("xrp", False, False)
    chk("неисполненный проигрыш тоже не двигает", e.size_for("xrp") == (40.0, 3))
    e.on_result("xrp", True, True)
    chk("четвёртая победа -> x16 (верх)", e.size_for("xrp") == (80.0, 4))
    e.on_result("xrp", True, True)
    chk("пятая победа замыкает цикл -> база", e.size_for("xrp") == (5.0, 0))
    e.on_result("xrp", True, True); e.on_result("xrp", True, False)
    chk("поражение сбрасывает в базу", e.size_for("xrp") == (5.0, 0))
    e.on_result("xrp", True, True); e.on_result("xrp", True, None)
    chk("неопределённый исход тоже сбрасывает", e.size_for("xrp") == (5.0, 0))
    chk("счётчики: исполненных 9, пропущенных 2",
        e.snapshot()["settled"] == 9 and e.snapshot()["unfilled"] == 2)

    e2 = Escalator()
    e2.on_result("btc", True, True)
    chk("scope=global: победа BTC поднимает размер и для XRP",
        e2.size_for("xrp") == (10.0, 1))
    CFG.ESCALATE_SCOPE = "asset"
    e3 = Escalator()
    e3.on_result("btc", True, True)
    chk("scope=asset: цепочки активов независимы",
        e3.size_for("btc") == (10.0, 1) and e3.size_for("xrp") == (5.0, 0))

    CFG.MAX_SIZE = 25.0
    chk("потолок PM_MAX_SIZE режет верхние ступени",
        [CFG.step_size(i) for i in range(5)] == [5.0, 10.0, 20.0, 25.0, 25.0])
    CFG.MAX_SIZE = 0.0
    CFG.ESCALATE = False
    chk("выключенная лесенка всегда даёт базу",
        all(CFG.step_size(i) == 5.0 for i in range(5))
        and Escalator().size_for("xrp") == (5.0, 0))
    CFG.ESCALATE = True
    CFG.ESCALATE_STEPS = 1
    chk("STEPS=1 ловится проверкой", any("ESCALATE_STEPS" in x for x in CFG.check()))
    CFG.ESCALATE_STEPS, CFG.ESCALATE_FACTOR = 5, 0.5
    chk("FACTOR<=1 ловится", any("ESCALATE_FACTOR" in x for x in CFG.check()))
    CFG.ESCALATE_FACTOR, CFG.ESCALATE_STEPS = 2.0, 9
    chk("x256 без потолка ловится", any("MAX_SIZE" in x for x in CFG.check()))
    (CFG.ESCALATE, CFG.ESCALATE_FACTOR, CFG.ESCALATE_STEPS,
     CFG.ESCALATE_SCOPE, CFG.ENTRY_SIZE, CFG.MAX_SIZE) = sv

    print("\n4e. разбор ответа биржи об исполнении")
    chk("size_matched", Broker._matched({"size_matched": "7"}) == 7.0)
    chk("camelCase", Broker._matched({"sizeMatched": 3.5}) == 3.5)
    chk("список суммируется", Broker._matched([{"filled": 2}, {"filled": 3}]) == 5.0)
    chk("нет поля -> None", Broker._matched({"status": "LIVE"}) is None)
    chk("None -> None", Broker._matched(None) is None)
    chk("мусор -> None", Broker._matched({"size_matched": "abc"}) is None)

    print("\n5. лимит одновременной экспозиции")
    mk_sig = lambda a, e: {"asset": a, "conflict": False, "side": "up", "edge": e}
    pool = [mk_sig("xrp", 0.05), mk_sig("btc", 0.13), mk_sig("eth", 0.09), None,
            {"asset": "sol", "conflict": True}]
    got = pick_round(pool, 2)
    chk("берём ровно MAX_CONCURRENT", len(got) == 2)
    chk("сортировка по перевесу", [g["asset"] for g in got] == ["btc", "eth"])
    chk("конфликтные и пустые отброшены",
        all(g["asset"] not in ("sol",) for g in got))
    chk("порядок воспроизводим", pick_round(pool, 2) == got)

    print("\n6. слоты и окна")
    tz = timezone(timedelta(hours=-4))
    t = int(datetime(2026, 9, 10, 13, 45, tzinfo=timezone.utc).timestamp())
    chk("13:45 UTC = 09:45 при UTC−4", slot_label(t, tz) == "09:45")
    chk("window_start 15m кратен 900", window_start(t + 137, "15m") % 900 == 0)
    chk("window_start 5m кратен 300", window_start(t + 137, "5m") % 300 == 0)
    chk("slug", slug("xrp", "15m", 1785080700) == "xrp-updown-15m-1785080700")
    ny = ZoneInfo("America/New_York")
    t_win = int(datetime(2026, 12, 10, 14, 45, tzinfo=timezone.utc).timestamp())
    chk("зимой America/New_York = UTC−5 (14:45 UTC -> 09:45)",
        slot_label(t_win, ny) == "09:45")
    chk("а fixed:-4 зимой даёт 10:45", slot_label(t_win, tz) == "10:45")

    print("\n7. поиск записей по слоту")
    tables = {("15m", "any"): {"09:30": (0.5605, "rev")},
              ("15m", "k2"): {"09:30": (0.6303, "rev"), "07:15": (0.5926, "rev")},
              ("5m", "any"): {"09:30": (0.55, "rev")}}
    chk("слот 09:30 на 15m даёт обе записи", len(slots_for("15m", "09:30", tables)) == 2)
    chk("слот 07:15 на 15m — одну", len(slots_for("15m", "07:15", tables)) == 1)
    chk("чужой слот — ноль", slots_for("15m", "11:11", tables) == [])
    chk("тф не путаются", len(slots_for("5m", "09:30", tables)) == 1)

    print("\n8. комиссия и безубыток")
    chk("θ·p·(1−p) на 0.51 = 1.75¢", abs(taker_fee(0.51) - 0.017493) < 1e-6)
    chk("безубыток тейкера на 0.51 = 52.75%", abs(0.51 + taker_fee(0.51) - 0.527493) < 1e-6)
    chk("на 0.50 комиссия 1.75¢", abs(taker_fee(0.50) - 0.0175) < 1e-9)
    chk("комиссия падает к краям", taker_fee(0.10) < taker_fee(0.50))

    print("\n9. тайминги: решение до открытия целевого рынка")
    chk("прогрев стартует раньше решения",
        CFG.PREFETCH_LEAD > CFG.CHECK_LEAD)
    chk("прогрев стартует не позже начала опроса книги",
        CFG.PREFETCH_LEAD >= CFG.TAIL_WATCH)
    chk("таймаут книги короче окна наблюдения",
        CFG.BOOK_TIMEOUT < CFG.TAIL_WATCH - CFG.CHECK_LEAD)
    W = TF_SEC["15m"]
    m = 1785080700
    check_at = m + W - CFG.CHECK_LEAD
    chk("решение раньше закрытия окна", check_at < m + W)
    chk("решение раньше открытия целевого ровно на CHECK_LEAD",
        (m + W) - check_at == CFG.CHECK_LEAD)
    chk("наблюдение начинается до решения", m + W - CFG.TAIL_WATCH < check_at)
    chk("дедлайн постановки уже после открытия", m + W + CFG.PLACE_DEADLINE > m + W)

    print("\n10. конфиг")
    errs = CFG.check()
    chk("дефолтный конфиг валиден" if not errs else f"ошибки: {errs}", not errs)
    chk("по умолчанию только 15m", tuple(CFG.TFS) == ("15m",))
    chk("по умолчанию оба условия", set(CFG.CONDS) == {"any", "k2"})
    chk("по умолчанию DRY_RUN", CFG.DRY_RUN)
    chk("по умолчанию цена 0.51", abs(CFG.ENTRY_PRICE - 0.51) < 1e-9)
    chk("по умолчанию запрет тейкера включён", CFG.AVOID_TAKER)
    chk("по умолчанию потолок тейкера 0.50", abs(CFG.TAKER_MAX - 0.50) < 1e-9)
    chk("по умолчанию один рынок на актив", CFG.ONE_AT_A_TIME)
    chk("по умолчанию пауза 2 минуты", abs(CFG.MIN_GAP_MIN - 2.0) < 1e-9)

    print("\n11. кривой конфиг ловится")
    saved = (CFG.TFS, CFG.CONDS, CFG.TRADE_ASSETS, CFG.ENTRY_PRICE,
             CFG.ENTRY_SIZE, CFG.SLOT_TZ, CFG.MAX_CONCURRENT)
    CFG.TFS = ("1m",)
    CFG.CONDS = ("k3",)
    CFG.TRADE_ASSETS = ("doge",)
    CFG.ENTRY_PRICE, CFG.ENTRY_SIZE = 1.4, 0.5
    CFG.SLOT_TZ = "Mars/Olympus"
    CFG.MAX_CONCURRENT = 0
    sv2 = CFG.TAKER_MAX
    CFG.TAKER_MAX = 0.9
    errs = CFG.check()
    CFG.TAKER_MAX = sv2
    chk(f"чужой тф, чужое условие, чужой актив, цена вне (0,1), грош-ордер, "
        f"кривой пояс, нулевой лимит и потолок тейкера выше цены входа "
        f"пойманы ({len(errs)})", len(errs) >= 8)
    (CFG.TFS, CFG.CONDS, CFG.TRADE_ASSETS, CFG.ENTRY_PRICE,
     CFG.ENTRY_SIZE, CFG.SLOT_TZ, CFG.MAX_CONCURRENT) = saved

    print("\n12. предупреждения")
    saved2 = (CFG.TFS, CFG.ENTRY_PRICE)
    CFG.TFS = ("15m", "5m")
    CFG.ENTRY_PRICE = 0.55
    w = CFG.warnings()
    chk("5m и завышенная цена отмечены", sum(("5m" in x or "0.55" in x) for x in w) >= 2)
    chk("предупреждение про провал out-of-sample всегда есть",
        any("out-of-sample" in x for x in w))
    CFG.TFS, CFG.ENTRY_PRICE = saved2

    print("\n13. оценка нагрузки")
    t15 = {("15m", "any"): {"09:30": (0.56, "rev"), "17:45": (0.62, "rev")},
           ("15m", "k2"): {"09:30": (0.63, "rev"), "07:15": (0.59, "rev")}}
    n = expected_orders_per_day(t15, 4, 4)
    chk("пересечение any/k2 считается одним ордером, а не двумя",
        abs(n - (1 + 1 + 0.49) * 4) < 1e-6)
    chk("лимит раунда режет оценку", expected_orders_per_day(t15, 4, 1) * 4 == n)

    print("\n" + ("ВСЁ ЗЕЛЁНОЕ" if ok else "ЕСТЬ ПАДЕНИЯ"))
    return 0 if ok else 1


# ═══════════════════════════════════════════════════════════════ main
async def main_async():
    mk = Markets()
    br = Broker()
    jr = Journal(CFG.LOG)
    stop = asyncio.Event()
    stale_orders: list = []
    settle: list = []
    tables = CFG.schedule()
    tz = CFG.tzinfo()
    busy: dict[str, float] = {}
    esc = Escalator()

    if CFG.ESCALATE:
        m = br.find_fill_method()
        if not m:
            raise SystemExit(
                "PM_ESCALATE=1, но у клиента нет метода, которым можно узнать, "
                "налилась ли заявка. Лесенка без проверки исполнения считала бы "
                "победами неисполненные лимитки и раздувала размер на пустом "
                "месте. Поставь PM_ESCALATE=0 или обнови py-clob-client-v2.\n"
                "  пробовались: " + ", ".join(Broker._FILL_METHODS))
        log(f"исполнение заявок читается через {m}()")

    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, stop.set)
        except NotImplementedError:
            pass  # Windows

    n_slots = sum(len(t) for t in tables.values())
    per_day = expected_orders_per_day(tables, len(CFG.TRADE_ASSETS), CFG.MAX_CONCURRENT)
    log(f"старт: активы={CFG.TRADE_ASSETS} тф={CFG.TFS} условия={CFG.CONDS} "
        f"слотов={n_slots} (перевес >= {CFG.MIN_EDGE:.0%}) пояс={CFG.SLOT_TZ} "
        f"вход={CFG.ENTRY_PRICE}@{CFG.ENTRY_SIZE}"
        + (f" лесенка x{CFG.ESCALATE_FACTOR:g} до {CFG.ESCALATE_STEPS} "
           f"({CFG.ESCALATE_SCOPE}) " if CFG.ESCALATE else " ")
        + f"лимит раунда={CFG.MAX_CONCURRENT} "
        f"~{per_day:.0f} ордеров/сутки "
        f"{'[DRY RUN]' if CFG.DRY_RUN else '[LIVE — РЕАЛЬНЫЕ ДЕНЬГИ]'}")

    tasks = [asyncio.create_task(run_tf(tf, mk, br, jr, tables, tz, busy, esc,
                                        stale_orders, settle, stop))
             for tf in CFG.TFS]
    tasks.append(asyncio.create_task(cleanup_stale(br, stale_orders, stop)))
    tasks.append(asyncio.create_task(settle_watch(mk, br, jr, esc, settle, stop)))
    await stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    log("остановлен")


def main():
    ap = argparse.ArgumentParser(
        description="Polymarket: отработка стабильных часовых слотов 5m/15m")
    ap.add_argument("--selftest", action="store_true", help="проверить логику без сети")
    ap.add_argument("--slots", action="store_true", help="показать таблицу и расписание")
    ap.add_argument("--env", default=".env")
    a = ap.parse_args()

    load_env(a.env)
    globals()["CFG"] = Cfg()

    if a.selftest:
        sys.exit(selftest())

    errs = CFG.check()
    if errs:
        raise SystemExit("ошибки в .env:\n  " + "\n  ".join(errs))

    if a.slots:
        print_slots(CFG)
        return

    for w in CFG.warnings():
        log(f"  ВНИМАНИЕ: {w}")

    if not CFG.DRY_RUN:
        tables = CFG.slots()
        per_day = expected_orders_per_day(tables, len(CFG.TRADE_ASSETS),
                                          CFG.MAX_CONCURRENT)
        be_t = CFG.ENTRY_PRICE * (1 + CFG.BUILDER_FEE) + taker_fee(CFG.ENTRY_PRICE)
        print("\n" + "!" * 66)
        print("PM_DRY_RUN=0 — ордера пойдут на реальные деньги.")
        print(f"  активы {CFG.TRADE_ASSETS}, тф {CFG.TFS}, условия {CFG.CONDS}")
        print(f"  слотов в работе: {sum(len(t) for t in tables.values())}")
        print(f"  вход {CFG.ENTRY_PRICE} × {CFG.ENTRY_SIZE} = "
              f"${CFG.ENTRY_PRICE * CFG.ENTRY_SIZE:.2f} за ордер")
        print(f"  безубыток {CFG.ENTRY_PRICE:.2%} мейкером / {be_t:.2%} тейкером")
        print(f"  ~{per_day:.0f} ордеров/сутки ≈ "
              f"${per_day * CFG.ENTRY_PRICE * CFG.ENTRY_SIZE:.0f} оборота/сутки "
              f"базовым размером")
        if CFG.ESCALATE:
            steps = [CFG.step_size(i) for i in range(CFG.ESCALATE_STEPS)]
            print(f"  ЛЕСЕНКА {CFG.ESCALATE_SCOPE}: ступени "
                  f"{', '.join(f'{x:g}' for x in steps)} акций "
                  f"= ${steps[-1] * CFG.ENTRY_PRICE:.2f} на верхней")
            print(f"  худший одиночный убыток на верхней ступени: "
                  f"${steps[-1] * CFG.ENTRY_PRICE:.2f}")
        print("  сигнал НЕ проверен вне выборки — см. шапку файла")
        print("!" * 66)
        if input("напиши YES для запуска: ").strip() != "YES":
            raise SystemExit("отменено")

    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
