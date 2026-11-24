# gozi

**Late Strong Impulse (BTC 5m)**

## Strategy

In the last ~30s of a 5-minute BTC Up/Down window, detect a sharp mid-price recovery (start mid < 0.45, delta ≥ 0.30 over 2–6s). Only enter if current ask ≥ ASK_FLOOR (0.60) to avoid already-faded moves. Places LIMIT BUY at ORDER_PRICE with a volume progression cycle after fills. Historically the ask-floor filter lifts win-rate from ~50% to ~85% on the research set.

## Files

| File | Role |
|------|------|
| `gozi.py` | Full strategy source |
| `gozi.env.example` | Config template — copy to `gozi.env` |

## Quick start

```bash
cd gozi
cp gozi.env.example gozi.env
# edit PAPER=true / PM_DRY_RUN=1 and keys if needed
python gozi.py
```

Paper mode is the default in most bots. Live trading requires `PRIVATE_KEY` (and optionally `FUNDER` + `SIGNATURE_TYPE`) and explicit disable of paper/dry-run flags.

## License

Same as repository root (Polymarket Builder Attribution Source-Available License 1.0).
Live paths may embed builder codes; private keys are never committed.
