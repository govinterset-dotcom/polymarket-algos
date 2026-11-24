# hybrid_momentum_bot (polymizer_hybrid)

**Гибрид Leader / Underdog по ask**

- Лидер (ask ≥ 0.50): процентный рост ≥ LEADER_MIN_RISE_PCT за LEADER_WINDOW_SEC
- Андердог (ask < 0.50): абсолютный рост ≥ UNDERDOG_MIN_RISE_ABS за UNDERDOG_WINDOW_SEC
- Размер клипа растёт по порядку срабатывания на рынке
- Есть entry gate (коридор + оставшееся время)

## Запуск
```bash
python hybrid_momentum_bot.py
```
