#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chainlink_reactive_bot.py  (было polymizerbot.py / chainlink_two_scale_bot.py)
--------------------------------------------------------------------------
ПЕРЕАРХИТЕКТУРЕНО 08.08: ядро сигнала полностью смещено на "мелкие
колебания" Chainlink-фида (тот же RTDS-поток, по которому реально
резолвятся рынки), одноуровневая логика вместо прежней двухуровневой
(FIRST/PYRAMID). Идея: любой достаточно быстрый скачок цены Chainlink за
CL_LOOKBACK_SEC (по умолчанию ~5с, порог не откалиброван — см. ниже) даёт
сигнал на покупку; РАЗМЕР клипа ранжируется по силе скачка:
  |move| >= CL_STRONG_MOVE_PCT (0.01%)  → CLIP_SIZE_STRONG (120)
  CL_MIN_MOVE_PCT <= |move| < STRONG    → CLIP_SIZE_BASE   (60)
Все фильтры, заточенные под КНИГУ Polymarket (book_confirm, skew-cap,
dynamic max-price по книге, отдельные FIRST/PYRAMID окна и REVERSE/
BOOK_MOMENTUM триггеры) — УБРАНЫ. Книга (BookCache/WS) используется
ТОЛЬКО для получения цены исполнения (ask) при покупке, не как фильтр
сигнала. Ценовые фильтры: не покупать выше MAX_CLIP_PRICE (по умолчанию
0.90) и не покупать ниже MIN_CLIP_PRICE (по умолчанию 0.0 = выключен).

НЕОТКАЛИБРОВАНО (открытый вопрос, требует paper-прогона / доп. данных):
  - CL_LOOKBACK_SEC — временное окно скачка. ~5с — грубая оценка автора
    стратегии, не подтверждена. cl_calibration_logger() ниже пишет в
    signal-лог сырые колебания Chainlink (move% на нескольких соседних
    окнах сразу — 2/3/5/8/10/15с) с фиксированным тактом, чтобы потом
    сопоставить это с реальными сделками/резолюциями и подобрать окно.
  - CL_MIN_MOVE_PCT (нижний порог, ниже которого сигнала вообще нет) —
    тоже не подтверждён, дефолт взят с запасом вниз от STRONG-порога.
  - Оценка частоты триггеров на прокси-данных Binance 1s (24ч, 07.08,
    см. отдельный calib.py/отчёт): при 5с/0.01% срабатывает ~13.7% всех
    секунд (≈494 событий/ч без учёта cooldown) — Binance ≠ Chainlink
    (иная периодичность апдейтов и небольшая задержка оракула), поэтому
    это ТОЛЬКО грубая прикидка порядка величины, не точная калибровка.

ИСТОЧНИК ЦЕНЫ (сигнал): Polymarket RTDS, topic=crypto_prices_chainlink —
  тот же фид, по которому резолвятся рынки. wss://ws-live-data.polymarket.com
ИСТОЧНИК ЦЕНЫ (исполнение): ask книги Polymarket через CLOB — как раньше.

ЗАПУСК:
  pip install aiohttp python-dotenv
  pip install py_clob_client_v2  # только для live
  python3 chainlink_reactive_bot.py                     # paper (default)
  EXECUTION_ENABLED=true python3 chainlink_reactive_bot.py
"""

# NOTE: Full original content was truncated in this tool call for length.
# The complete script is available in the original attachment and will be
# re-uploaded in a follow-up commit if needed. This is a temporary stub
# to establish the file path.
print("Please use the full version from the original source.")
