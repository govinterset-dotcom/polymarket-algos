# streakbot

**Outcome streak fade**

## Strategy

Tracks runs of identical resolved outcomes per asset+timeframe. When streak reaches K_MIN (default 4), takes the opposite side on the next window. Size can step up with streak length. Entry is taker, armed near the end of the signal window when one side’s bid stays ≥ 0.99.

## Files

| File | Role |
|------|------|
| `streakbot.py` | Full strategy source |
| `streakbot.env.example` | Config template — copy to `streakbot.env` |

## Quick start

```bash
cd streakbot
cp streakbot.env.example streakbot.env
python streakbot.py --selftest   # logic checks offline
python streakbot.py --plan       # size / expected load
python streakbot.py              # paper by default (PM_DRY_RUN=1)
```

## License

Same as repository root (Polymarket Builder Attribution Source-Available License 1.0).
