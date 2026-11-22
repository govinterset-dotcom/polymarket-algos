# Polymarket Up/Down Trading Algorithms

**A multi-year collection of research-driven Python strategies for BTC (and other) Up/Down markets on Polymarket — order-book replay, live CLOB execution, and documented performance.**

This repository is the public face of years of different experiments, iterations, and production attempts around short-horizon Polymarket microstructure. The strategies are deliberately diverse: different frequencies, different payoff profiles, different sources of edge.

### Core published strategies

- **Comeback** — late recovery of a side that was previously priced as almost certain to lose.
- **Premove** — a sharp order-book move near the end of one 5-minute market as a signal for the next market.
- **Rozi** — buying sharp price dislocations at predefined levels, with optional streak and market-condition filters.

### Additional research & live modules

| Folder | Essence | Tags |
|--------|---------|------|
| **gozi** | Late-window strong mid impulse from a cheap start; limit + volume-progression cycle | `impulse` `late-window` `5m` `volume-progression` |
| **boxarb** | Nested 5m/15m/4h box arbitrage on the same Chainlink TWAP stream | `arbitrage` `nested-markets` `fak` |
| **flash** | Cross-exchange (Binance + OKX) flash moves as proxy for Polymarket Up/Down | `flash` `cross-exchange` `latency` |
| **streakbot** | Series of identical outcomes — arm near window end, take with confirmation | `streak` `series` `tail-confirm` |
| **arb_bot** | Classic Up + Down combination arbitrage when the book leaves edge after fees | `arbitrage` `spread` |
| **dozi** | Companion / early impulse family; taker entries with volume & timing filters | `impulse` `taker` `volume-filter` |
| **hitday** | Daily «What price will BTC/ETH hit?» ladder markets vs Binance 1m | `daily` `hit-price` `ladder` |
| **pmgrid** | Limit-order grid on Up/Down (CLOB v2), planner + live | `grid` `limit` `planner` |
| **crossreb** | CROSS + rebound book signals on 15m, taker | `cross` `rebound` `15m` |
| **dispute_ladder** | After UMA dispute buy disputant side when market already >0.51, with adds | `uma` `dispute` `ladder` |
| **dispute_cheap** | After UMA dispute buy the *cheap* disputant side in subjective categories | `uma` `dispute` `cheap` |
| **chainlink_edge** | Trade the lag between direct Chainlink feed and Polymarket book | `chainlink` `oracle-lag` |
| **slotbot** | Stable hourly slots; early tail resolution + hygiene | `slots` `hourly` |
| **martbot** | Selected slots after two losses; size doubling up to 3 attempts | `martingale` `slots` |
| **hourbot** | Reversal after series on hourly & 4h markets | `hourly` `4h` `series-reversal` |
| **spread_arb_bot** | Up-BTC vs Up-ETH spread on the same live window | `spread` `btc-eth` |
| **preladder** | Pre-place limit grid on both outcomes + taker on opposite side on fill | `pre-grid` `both-sides` |
| **udogq** | Underdog signal from naked quotes only (distance vs remaining σ) | `underdog` `quotes-only` |

Every live execution path carries the same embedded Polymarket **Builder attribution** (address + code). Private keys, funder addresses and API credentials are never committed — only `.env.example` files with empty placeholders.

> **Validation note:** Comeback has a documented paper run on live Polymarket order books. None of the figures are a guarantee of future live returns.

[Repository](https://github.com/govinterset-dotcom/polymarket-algos) · [Polymarket](https://polymarket.com) · [Polymarket Tools Directory](https://polymart.app) · [LaunchPoly](https://launchpoly.com/?ref=Zvh7C4TEHjNHWf2769P6tduJPju58jcq)

---

## At a glance

| Strategy | Market | Core signal |
|---|---|---|
| **Comeback** | BTC 5m | High ask after a deep low in the same window |
| **Premove** | BTC 5m → next 5m | Late order-book jump predicts next market |
| **Rozi** | BTC 15m | Sharp drop into a price tier after a fast move |
| **Gozi / Dozi** | BTC 5m | Strong late (or early) impulse |
| **Boxarb** | nested windows | Box arb across 5m/15m/4h |
| **Flash** | multi-asset | CEX flash → Polymarket |
| **Crossreb** | 15m | Cross / rebound from deep low |
| **Hitday** | daily hit ladders | BTC/ETH hit levels vs Binance |
| **Pmgrid / Preladder** | Up/Down | Limit grids (live or pre-open) |
| **Dispute \\*** | UMA markets | Post-dispute disputant side |
| **Chainlink Edge** | Up/Down | Oracle lag vs book |
| **Slot / Mart / Hour** | hourly / 4h / slots | Series, slots, martingale variants |
| **Spread Arb** | BTC vs ETH | Cross-asset Up spread |
| **Udogq** | various | Quote-only underdog |

## Builder attribution

All live modules embed the same Builder address and code (not configurable via `.env`):

`comeback`, `premove`, `rozi`, `gozi`, `boxarb`, `flash`, `streakbot`, `arb_bot`, `dozi`, `hitday`, `pmgrid`, `crossreb`, `dispute_ladder`, `dispute_cheap`, `chainlink_edge`, `slotbot`, `martbot`, `hourbot`, `spread_arb_bot`, `preladder`, `udogq`.

## License

Polymarket Builder Attribution Source-Available License 1.0 — see [LICENSE](LICENSE).
