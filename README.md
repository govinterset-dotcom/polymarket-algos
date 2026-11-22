# Polymarket Up/Down Trading Algorithms

**A multi-year collection of research-driven Python strategies for Polymarket — order-book, disputes, grids, oracles, slots, and more.**

Years of experiments and iterations. Strategies differ in frequency, payoff profile, and source of edge.

### Core

- **Comeback** — late recovery of a nearly-lost side
- **Premove** — late book jump → next window
- **Rozi** — sharp drops into price tiers

### Extended collection

| Folder | Essence | Tags |
|--------|---------|------|
| **gozi / dozi** | Late / early strong impulse + volume progression | impulse, volume |
| **boxarb** | Nested 5m/15m/4h box arbitrage | arbitrage, nested |
| **flash** | CEX flash → Polymarket | cross-exchange |
| **closer** | Buy the leading side in the last seconds | tail, closer |
| **crossreb** | CROSS + rebound from deep low (15m) | cross, rebound |
| **streakbot** | Outcome series + tail confirm | streak |
| **arb_bot** | Classic Up+Down combination arb | arbitrage |
| **hitday** | Daily hit-price ladders vs Binance | daily, ladder |
| **pmgrid / preladder** | Limit grids (live / pre-open) | grid, limit |
| **dispute_ladder / dispute_cheap** | Post-UMA disputant strategies | uma, dispute |
| **autotaker** | Feed-driven auto-take on ProposePrice | feeds, tennis, esports |
| **merge_split** | CTF split & merge via proxy wallet | ctf, merge |
| **chainlink_edge** | Direct Chainlink lag vs book | oracle |
| **slotbot / martbot / hourbot** | Slots, martingale after losses, hourly series | slots, series |
| **spread_arb_bot** | Up-BTC vs Up-ETH spread | spread, btc-eth |
| **reward_farmer** | Liquidity / reward farming | rewards |
| **btc_impulse_v4** | Impulse variant | impulse |
| **polymoly / propwatch / secunda / tennischeck / udogq** | Experimental / specialized | research |

Builder address/code is embedded in live CLOB paths. **No private keys or funders in the repo** — only empty `.env.example`.

> Paper and historical figures are not guarantees of future live returns.

[GitHub](https://github.com/govinterset-dotcom/polymarket-algos) · [Polymarket](https://polymarket.com)

## License

Polymarket Builder Attribution Source-Available License 1.0 — see [LICENSE](LICENSE).
