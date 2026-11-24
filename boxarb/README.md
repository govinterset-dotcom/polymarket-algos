# boxarb

**Nested 5m / 15m / 4h box arbitrage**

## Strategy

Looks for pricing inconsistencies across nested BTC/ETH up-down windows (5m inside 15m inside 4h). When a combination of legs is mispriced relative to the outer window after fees, buys the cheap box.

## Quick start

```bash
cd boxarb
cp boxarb.env.example boxarb.env
python boxarb.py
```

## License

Same as repository root.
