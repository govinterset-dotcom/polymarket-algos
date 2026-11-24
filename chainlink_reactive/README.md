# chainlink_reactive_bot

**Одноуровневый реактивный сигнал по Chainlink RTDS**

Ядро сигнала полностью на мелких колебаниях Chainlink-фида (тот же RTDS-поток, по которому резолвятся рынки Polymarket).

- Окно lookback (по умолчанию 5с)
- Размер клипа ранжируется по силе скачка (BASE / STRONG)
- Книга используется **только** для цены исполнения (ask), не как фильтр сигнала
- Есть calibration logger для подбора порогов
- Paper по умолчанию

## Запуск
```bash
pip install aiohttp python-dotenv py_clob_client_v2 websockets
python chainlink_reactive_bot.py                     # paper
EXECUTION_ENABLED=true python chainlink_reactive_bot.py
```

Ключевые env: `CL_LOOKBACK_SEC`, `CL_MIN_MOVE_PCT`, `CL_STRONG_MOVE_PCT`, `CLIP_SIZE_BASE`, `CLIP_SIZE_STRONG`, `MAX_CLIP_PRICE`, `MIN_CLIP_PRICE`.
