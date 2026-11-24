# flash

**CEX flash → Polymarket lag**

## Strategy

Watches Binance/OKX spot. When real price crosses the Polymarket window open in the last 20–40s, Polymarket often still quotes the old side for 1–2 seconds. Bot FAK-buys the new side to capture the lag. Edge is real but sample of pure cross events is small — use paper mode first.

## Files

| File | Role |
|------|------|
| `flash.py` | Full strategy source |
| `flash.env.example` | Config template — copy to `flash.env` |

## Quick start

```bash
cd flash
cp flash.env.example flash.env
python flash.py
```

## License

Same as repository root (Polymarket Builder Attribution Source-Available License 1.0).
