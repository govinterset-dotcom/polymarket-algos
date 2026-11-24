# streakbot

**Outcome streak fade**

## Strategy

Tracks runs of identical resolved outcomes. When streak reaches K_MIN (default 4), takes the opposite side on the next window. Taker entry near end of signal window when one side bid stays ≥ 0.99.

## Quick start

```bash
cd streakbot
cp streakbot.env.example streakbot.env
python streakbot.py --selftest
python streakbot.py --plan
python streakbot.py
```
