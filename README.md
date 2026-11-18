# Polymarket Up/Down Trading Algorithms

**Three research-driven Python strategies for BTC Up/Down markets on Polymarket — with historical order-book replay, live CLOB execution, and documented performance.**

This repository explores three different sources of short-horizon market behavior:

- **Comeback** — late recovery of a side that was previously priced as almost certain to lose.
- **Premove** — a sharp order-book move near the end of one 5-minute market as a signal for the next market.
- **Rozi** — buying sharp price dislocations at predefined levels, with optional streak and market-condition filters.

The strategies are deliberately different: they trade at different frequencies, have different win-rate/payoff profiles, and target different market behaviors.

> **Validation note:** Comeback has a documented **paper run on live Polymarket order books** (real-time websocket books, simulated fills only). Historical research cards for all three strategies remain below. None of these figures are a guarantee of future live returns.

[Repository](https://github.com/govinterset-dotcom/polymarket-algos) · [Polymarket](https://polymarket.com) · [Polymarket Tools Directory](https://polymart.app) · [LaunchPoly](https://launchpoly.com/?ref=Zvh7C4TEHjNHWf2769P6tduJPju58jcq) · [Prediction Market Tools](https://www.predictionmarket.tools/)

<a href="https://launchpoly.com/?ref=Zvh7C4TEHjNHWf2769P6tduJPju58jcq" target="_blank" rel="noopener">
  <img src="https://launchpoly.com/api/badges/claim/Zvh7C4TEHjNHWf2769P6tduJPju58jcq.svg?theme=light&size=medium"
       alt="Featured on LaunchPoly" width="150" height="44" />
</a>

---

## At a glance

| Strategy | Market | Core signal | Documented frequency | Documented win rate |
|---|---|---|---:|---:|
| **Comeback** | BTC 5m (paper live books) | High ask after a deep low in the same window | ~6–7 entries/hour in the paper sample | **96.6%** (paper) |
| **Premove** | BTC 5m → next 5m | Late order-book jump predicts direction in the next market | ~7–8 signals/day | 64.5% |
| **Rozi** | BTC 15m | Sharp drop into a price tier after a fast move | ~1.4 signals/day | ~30% |

These figures are not directly comparable. A high win rate does not automatically mean a better strategy: entry price, payout asymmetry, fees, execution quality, and drawdown matter just as much.

## Why this project is interesting

Most short-term trading examples focus on indicators calculated from exchange candles. These strategies instead investigate **Polymarket's own order book**:

- bid/ask dynamics and mid-price changes;
- the timing and sequence of price-level touches;
- the transition between consecutive 5-minute markets;
- execution constraints, taker fees, and fill behavior;
- streaks, filters, and market regimes.

The goal is to make the research inspectable: the code, parameters, performance assumptions, and known limitations are documented rather than hidden behind a black-box signal.

## License

This repository is distributed under the **Polymarket Builder Attribution Source-Available License 1.0** (see [LICENSE](LICENSE)).

You may study, run, modify, and redistribute the code under the license terms. For live Polymarket production use, the original Builder attribution must be preserved in the order-submission path unless the copyright holder gives prior written permission. Modified redistributions must retain the license and identify their changes.

This is a custom source-available license, **not an OSI-approved open-source license**. It does not guarantee Builder rewards, attributed volume, or fees; those are governed by Polymarket's current Builder Program rules. Third-party dependencies remain under their own licenses.

## Polymarket Builder attribution

All three live strategy implementations currently include the same Polymarket Builder attribution configuration:

- `comeback/comeback.py`
- `premove/premove_live.py`
- `rozi/rozi.py`

The Builder address and Builder Code are embedded in the live CLOB client configuration. They are intentionally not exposed as ordinary strategy parameters in the example `.env` files, so changing a trading threshold does not silently change order attribution.

**How attribution works:** when a live order is submitted through the strategy's Polymarket CLOB integration with the configured Builder Code, eligible matched volume can be attributed to the associated Builder profile, subject to Polymarket's current program rules. Paper-mode signals and simulated fills do not create eligible attributed trading volume.

**Important distinction:** publishing this repository does not itself generate Builder volume or rewards. A user must place eligible trades through an integrating third-party application. Trades made directly on Polymarket.com are not eligible for Builder attribution. Never route trades or generate artificial volume solely to manipulate rewards or leaderboard rankings.

The repository is an open research codebase, not a hosted trading service. Users should inspect the code and understand the execution and fee model before running it. Any product or service built around these algorithms must clearly disclose fees and follow Polymarket's Builder Program requirements.

Official resources: [Polymarket Builders](https://builders.polymarket.com/) · [Builder Program Code of Conduct](https://builders.polymarket.com/code-of-conduct).

## Performance and research notes

The cards below summarize specific datasets and configurations. A **paper run on live books** tests the signal against real-time Polymarket websocket data without sending orders. A **historical research card** estimates behavior on recorded windows. Neither is a forecast of live production results.

### 1. Comeback — late recovery

**Hypothesis:** When a side has already printed a deep low inside a short window, and its ask later returns to a high band, that recovery can carry information about the final outcome — especially if only the first side of the window is traded.

**Signal outline**
1. Window fraction is past `FRAC_MIN`, with optional early/late zones (`FRAC_EARLY_MAX` / `FRAC_LATE_MIN`).
2. Ask first enters the entry corridor (`ENTRY_LO`–`ENTRY_HI`).
3. Optional `ONE_SIDE_ONLY`: at most one side per window.
4. Capped FAK taker entry; hedges and add-ons disabled in the reference config.

#### Paper validation on live order books (reference config)

This is the configuration shipped in `comeback/comeback.env.example`.

| | |
|---|---|
| Mode | Paper (`PAPER=true`) — no real orders, real-time Polymarket books |
| Market | BTC Up/Down |
| Period | ~17.5 hours (2026-11-17 10:15 UTC → 2026-11-18 03:45 UTC) |
| Resolved trades | **117** |
| Wins / losses | **113 / 4** |
| Win rate | **96.6%** |
| Turnover | **$4,453** |
| Net PnL (after modeled taker fee) | **+$149.32** |
| Return on turnover | **+3.35%** (~3.4¢ per $1 turned) |
| Average fill price | **0.92** |
| Approximate max drawdown (from peak) | **−$63** |
| Max consecutive losses | **1** |

**Reference parameters (excerpt)**

| Parameter | Value | Role |
|---|---|---|
| `ENTRY_LO` / `ENTRY_HI` | 0.92 / 0.99 | Ask corridor for entry |
| `FRAC_MIN` | 0.55 | Minimum window progress |
| `FRAC_EARLY_MAX` / `FRAC_LATE_MIN` | 0.65 / 0.85 | Prefer early or very late fraction; mid-zone treated as toxic |
| `ONE_SIDE_ONLY` | true | First qualifying side only |
| `CAP_PX` | 0.99 | Taker price ceiling |
| `HEDGE_LEVELS` / `ADD_LEVELS` | empty | Hedges and adds off |
| `STAKE_PCT` | 2 | Stake as % of deposit (compounded) |
| `FEE_RATE` | 0.07 | Modeled taker fee factor |

During this segment the bot consumed live CLOB websocket books, logged signals and simulated fills to `comeback.jsonl`, and resolved outcomes primarily from the book (`RESOLVE_BY_BOOK`). Entry asks clustered at 0.92–0.94. The run is suitable as a **starting point for independent paper testing**; it is not a claim about live fill quality or future regimes.

**Risk profile:** high win rate, small average win versus full stake on a loss. One loss can offset many wins. Fees, latency, and available depth matter when moving from paper to live.

#### Earlier historical research card (wider corridor)

Separate from the paper live-book run above:

- Dataset: 5,940 five-minute windows over 24 days (13–29 October and 2–8 November 2026).
- Tested entry range: **0.85–0.99**, from second 260 onward.
- Entries: **994** (~41/day); win rate **96.9%**; ~**+$0.028** per $1 turnover; average entry **0.94**.

Do not mix metrics across configurations. The shipped example env follows the **paper reference config** (0.92–0.99 corridor, one-side, hedges off), not the wider historical card.

### 2. Premove — next-window continuation

**Hypothesis:** A sufficiently large move in the Polymarket Up order book during the final seconds of a 5-minute market may provide a directional signal for the next 5-minute market.

**Signal outline**
1. Observe the Up order book during the final `JUMP_LAST_S` seconds.
2. Detect a mid-price change of at least `JUMP_MIN` over one of the configured lookback periods.
3. Map an upward move to Up and a downward move to Down.
4. Attempt to enter the same side in the next 5-minute market, preferably using its pre-open order book.

**Historical BTC 5m research card**

- Dataset: 5,940 five-minute windows over 24 days.
- Working research variant: core signal plus chase filter.
- Signals: **183** (about 7.6 per day).
- Outcomes: **118 wins / 65 losses**.
- Win rate: **64.5%** (reported interval 57–71%).
- At $10 per trade and assumed $0.55 average entry: **+$258** after modeled fees over 24 days; maximum drawdown **−$52**; worst day **−$15**.

**Entry-price sensitivity**

| Assumed entry price | Return on turnover | PnL at $10/trade |
|---:|---:|---:|
| $0.50 | +25.5% | +$466 |
| $0.55 | +14.1% | +$258 |
| $0.60 | +4.7% | +$85 |
| $0.65 | −3.2% | −$60 |

Entry price is critical. The historical edge deteriorates sharply as the next market's entry becomes more expensive. The live implementation has a configurable price cap; use the actual fill price, not the signal price, when evaluating performance.

### 3. Rozi — sharp dislocations

**Hypothesis:** A rapid decline into a predefined low-price tier can create an asymmetric opportunity if the side had traded materially higher shortly before the drop.

**Signal outline**
1. Track each side's live Polymarket mid-price.
2. Detect its first touch of a configured tier.
3. Require a sufficiently high recent reference price and enough time remaining.
4. Optionally filter by Binance volume, candle streaks, opening-price movement, and other conditions.
5. Enter with a capped taker order or use the optional post-only trap mode.

**Historical BTC 15m research card**

- Period: 7 October – 11 November 2026 (about 35 days).
- Configuration: BTC 15m, taker, tiers around 0.15 and 0.10, streak filter enabled.
- Signals: about **50** (roughly 1.4 per day).
- Win rate: about **30%**.
- Reported total: approximately **+$500** at $10 per signal.
- Maximum loss streak: **10**.
- Approximate drawdown at $10 per signal: **−$100**.

Rozi is intentionally a low-win-rate / asymmetric-payoff strategy. Its evaluation should focus on average net payout, losing streaks, drawdown, and the quality of fills — not win rate alone.

---

## Quick start

### Requirements

- Python 3.10+
- `requests`
- `websockets`
- `py_clob_client_v2` for live execution

Install the basic dependencies:

```bash
git clone https://github.com/govinterset-dotcom/polymarket-algos.git
cd polymarket-algos
pip install -r requirements.txt
```

For live trading, install the CLOB client used by the live implementation:

```bash
pip install py_clob_client_v2
```

### Run in paper mode

Paper mode is enabled by default in the example env files. Start with one strategy and inspect its journal before considering live execution.

**Comeback** (reference paper config)
```bash
cd comeback
cp comeback.env.example comeback.env
python comeback.py
```

**Premove**
```bash
cd ../premove
cp premove_live.env.example premove_live.env
python premove_live.py
```

**Rozi**
```bash
cd ../rozi
cp rozi.env.example rozi.env
python rozi.py
```

Configuration is loaded from the strategy's `.env` file next to its script, or from the path supplied through `ENV_FILE`.

### Live execution

Live trading requires a Polygon wallet and valid Polymarket CLOB credentials. The **signature type** and **funder** must match how the account was created on Polymarket — this is the most common first-connection failure (401 / rejected orders).

#### `SIGNATURE_TYPE` (`py-clob-client-v2`)

| Value | Name | Typical account |
|------:|------|-----------------|
| `-1` | (omit) | Default in examples: do not pass `signature_type` to the client |
| `0` | EOA | MetaMask / hardware wallet where the private key **is** the address that holds funds |
| `1` | POLY_PROXY | Older Magic / email login accounts |
| `2` | POLY_GNOSIS_SAFE | Browser wallet connected through Polymarket's proxy (common MetaMask-on-site flow) |
| `3` | POLY_1271 | Newer email / Magic / deposit wallets (frequent after 2025–2026) |

Rules of thumb:

- Email or Google login on Polymarket.com → try **`3`**, then **`1`** if orders fail.
- MetaMask (or similar) connected in the browser on Polymarket.com → usually **`2`**.
- Pure EOA you fund yourself and trade from directly → **`0`**.
- For types **1 / 2 / 3**, set `FUNDER` to the wallet address shown in your Polymarket profile (the proxy/deposit address that holds USDC), not necessarily the EOA derived from `PK`.

Wrong `SIGNATURE_TYPE` is the first thing to check if live auth or order posting fails. Defaults in the example env files stay at `-1` so existing setups are not changed; set the value explicitly for your account type.

Other live checklist:

- Keep `PAPER=true` until the data feed, market selection, order sizing, and journal output have been checked.
- Never put a private key, API secret, or real `.env` file in Git.
- Use a dedicated wallet with limited funds.
- Start with the smallest permitted order size.
- Check actual fills, fees, partial fills, rejected orders, and resolution records.

The example environment files are templates. Review every parameter before enabling live orders.

## Repository structure

```text
polymarket-algos/
├── README.md
├── requirements.txt
├── .gitignore
├── comeback/
│   ├── comeback.py
│   └── comeback.env.example   # reference paper config (0.92–0.99, one-side)
├── premove/
│   ├── premove_live.py
│   └── premove_live.env.example
└── rozi/
    ├── rozi.py
    └── rozi.env.example
```

## Reproducibility and evaluation

When evaluating or comparing these strategies, record at least:

- dataset dates, market IDs, and timeframes;
- exact strategy version and full parameter set;
- signal timestamp and executable bid/ask at decision time;
- requested price, actual average fill, filled quantity, and rejected quantity;
- fees and any other execution costs;
- resolution source and final outcome;
- PnL, return on turnover, win rate, maximum drawdown, and loss streaks.

Do not compare configurations unless they use the same data period, fee model, entry assumptions, and resolution rules. Parameters selected after inspecting a dataset can overfit that dataset; out-of-sample testing is essential.

## Configuration and journals

Each strategy has its own `.env.example` file with configurable signal, risk, and execution parameters.

- `PAPER=true` is the default in examples.
- `COMPOUND=true` enables journal-based compounding where supported.
- JSONL journals record signals, execution events, and resolution information.
- Rozi can additionally write a path log for order-book analysis.

See the individual strategy source files and example environment files for the full parameter list.

## Contributing

Issues, reproducibility checks, bug reports, and well-documented research ideas are welcome.

When reporting a result, include the strategy version, timeframe, dates, parameters, fee assumptions, and whether the numbers come from historical replay, paper-on-live-books, or live fills. Do not post wallet private keys, API secrets, or unredacted account data.

## Disclaimer

This repository is provided for research and educational purposes. It is not investment advice and does not guarantee profit. Prediction-market trading can result in the loss of the entire amount committed. Historical performance and prior paper or live results do not guarantee future outcomes. Real execution can differ because of latency, spread, liquidity, slippage, fees, market changes, and operational failures.

---

**If you find the research useful, consider starring the repository.** Stars help other developers discover the project. If you test a strategy or reproduce a result, please open an issue with your methodology and findings.
