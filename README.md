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
| **gozi** | Late-window strong mid impulse from a cheap start; limit entry with volume-progression cycle | `impulse` `late-window` `5m` `volume-progression` |
| **boxarb** | Nested 5m/15m/4h box arbitrage on the same Chainlink TWAP stream | `arbitrage` `nested-markets` `fak` |
| **flash** | Cross-exchange (Binance + OKX) flash moves as proxy for Polymarket Up/Down | `flash` `cross-exchange` `latency` |
| **ineff** | Residual edge between spot-derived fair value and Polymarket ask (15m) | `inefficiency` `fair-value` `edge-residual` |
| **streakbot** | Series of identical outcomes — arm near window end, take with confirmation | `streak` `series` `tail-confirm` |
| **arb_bot** | Classic Up + Down combination arbitrage when the book leaves edge after fees | `arbitrage` `spread` |

Every live execution path carries the same embedded Polymarket **Builder attribution** (address + code). Private keys, funder addresses and API credentials are never committed — only `.env.example` files with empty placeholders.

> **Validation note:** Comeback has a documented **paper run on live Polymarket order books**. None of the figures are a guarantee of future live returns.

[Repository](https://github.com/govinterset-dotcom/polymarket-algos) · [Polymarket](https://polymarket.com) · [Polymarket Tools Directory](https://polymart.app) · [LaunchPoly](https://launchpoly.com/?ref=Zvh7C4TEHjNHWf2769P6tduJPju58jcq)

---

## At a glance

| Strategy | Market | Core signal |
|---|---|---|
| **Comeback** | BTC 5m | High ask after a deep low in the same window |
| **Premove** | BTC 5m → next 5m | Late order-book jump predicts next market |
| **Rozi** | BTC 15m | Sharp drop into a price tier after a fast move |
| **Gozi** | BTC 5m | Strong late impulse + ask-floor filter |
| **Boxarb** | nested windows | Box arb across 5m/15m/4h |
| **Flash** | multi-asset | CEX flash → Polymarket |
| **Ineff** | BTC 15m | Fair-value residual |
| **Streakbot** | series | Outcome streak + tail confirm |
| **Arb bot** | Up/Down | Classic combination edge |

## Builder attribution

All live modules embed the same Builder address and code (not configurable via `.env`):

`comeback`, `premove`, `rozi`, `gozi`, `boxarb`, `flash`, `ineff`, `streakbot`, `arb_bot`.

## License

Polymarket Builder Attribution Source-Available License 1.0 — see [LICENSE](LICENSE).
