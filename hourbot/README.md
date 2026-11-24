# hourbot

**H1 / H4 mean-reversion after series**

## Strategy

Two rules on hourly and 4h BTC markets:
- H1: series ≥ 3 hours same direction and move ≥ 2σ → fade next hour (~5.6 signals/day, hist WR ~0.56).
- H4: series ≥ 2 × 4h candles and last candle ≥ 1σ → fade next 4h (~1.4/day, hist WR ~0.59).

σ from recent minute log-returns. Decision made PM_RULE_LEAD seconds before window open.

## Quick start

```bash
cd hourbot
cp hourbot.env.example hourbot.env
python hourbot.py --selftest
python hourbot.py --plan
python hourbot.py          # PM_DRY_RUN=1 by default
```

## License

Same as repository root (Polymarket Builder Attribution Source-Available License 1.0).
