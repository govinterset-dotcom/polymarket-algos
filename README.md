# Polymarket Algorithms

A curated collection of **research and production-oriented** Python strategies for [Polymarket](https://polymarket.com) — CLOB trading, CTF merge/redeem, UMA disputes, grids, oracles, and cross-venue signals.

Built over multiple years of iteration on live books, historical windows, and fee-aware execution.

---

## Structure

Each strategy lives in its own folder:

```
strategy_name/
  strategy_name.py      # main module (Builder attribution embedded)
  README.md             # essence + tags
  strategy_name.env.example
```

Core live modules (preserved as-is):

| Module | Role |
|--------|------|
| **comeback** | Late recovery of a nearly-lost side |
| **premove** | Late book jump → next window |
| **rozi** | Sharp drops into price tiers |

---

## Extended collection

| Folder | Essence | Tags |
|--------|---------|------|
| **gozi** | Late strong impulse in the final ~30s of BTC 5m; limit + volume cycle | `impulse` `late-window` `5m` |
| **dozi** | Companion impulse; taker + volume filters | `impulse` `taker` |
| **flash** | CEX flash (Binance/OKX) → Polymarket Up/Down | `flash` `cross-exchange` |
| **closer** | Buy the leading side in the last seconds of the window | `closer` `tail` |
| **boxarb** | Nested 5m/15m/4h box arbitrage | `arbitrage` `nested` |
| **streakbot** | Outcome streak + tail confirmation | `streak` `series` |
| **arb_bot** | Classic Up+Down combination arb after fees | `arbitrage` `spread` |
| **hitday** | Daily hit-price ladders vs CEX | `daily` `ladder` |
| **pmgrid** / **preladder** | Limit grids (live / pre-open) | `grid` `limit` |
| **crossreb** | CROSS + rebound on 15m | `cross` `rebound` |
| **dispute_ladder** / **dispute_cheap** | Post-UMA disputant strategies | `uma` `dispute` |
| **autotaker** | Feed-driven take on ProposePrice | `feeds` `uma` |
| **merge_split** | CTF split / merge / **redeem** via proxy wallet | `ctf` `redeem` |
| **chainlink_edge** | Chainlink lag vs book | `oracle` |
| **slotbot** / **martbot** / **hourbot** | Slots, martingale, hourly series | `slots` `series` |
| **spread_arb_bot** | Up-BTC vs Up-ETH spread | `spread` `btc-eth` |
| **reward_farmer** | Liquidity / reward farming | `rewards` |
| **btc_impulse_v4** | Impulse research variant | `impulse` `btc` |
| **udogq** / **secunda** / **propwatch** / **tennischeck** / **polymoly** | Specialized / experimental | `research` |

---

## Builder attribution

Live CLOB paths embed:

```python
BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"
```

These are **not** read from `.env`. Private keys and funder addresses are **never** committed — only empty `*.env.example` files.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cd gozi
cp gozi.env.example .env
# edit .env — set PAPER=true for dry runs
python gozi.py
```

> Paper and historical figures are not guarantees of future live returns.

---

## License

Polymarket Builder Attribution Source-Available License 1.0 — see [LICENSE](LICENSE).
