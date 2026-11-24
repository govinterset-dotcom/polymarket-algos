# Polymarket Algorithms

Open research workspace for **Polymarket** short-horizon markets (BTC/ETH up-down 5m / 15m): live-style Python bots, fee-aware backtests, and **historical datasets** (order-book paths + Binance seconds).

The goal is simple: anyone — researchers, traders, or **AI agents** — can clone the repo, load the data, and test or invent strategies without reinventing market plumbing.

> Paper / historical results are **not** guarantees of live performance. Fees (taker + optional builder) matter.

---

## Layout

```
polymarket-algos/
  algorithms/          # strategy code + env examples (research set)
  datasets/            # historical data + schema docs (updated over time)
  <legacy folders>/    # older one-folder-per-bot layout still present at repo root
  README.md
  LICENSE
```

| Path | Purpose |
|------|---------|
| **[algorithms/](algorithms/)** | Runnable bots used in recent research (comeback, rozi, ineff, crossreb, …) |
| **[datasets/](datasets/)** | Path JSONL (per-second book) and BTC seconds parquet; **data will grow and improve** |
| Root strategy folders | Historical collection (gozi, flash, boxarb, …) — same license |

**Datasets coverage (path):** unique markets (windows) **~12 600** — of which **BTC 5m ≈ 7 000**, **BTC 15m ≈ 3 200** (plus ETH/SOL/XRP in some dumps). See [datasets/](datasets/).

---

## Best paper results (research bots on repo data)

Offline replays on archives under **[datasets/](datasets/)** (order-book paths ± BTC seconds). Fee model: taker ≈ `0.07 × p × (1−p)` per share + ~1% builder. Fixed stake in tables. **Not live performance.**

### Strongest cuts we measured

| Algorithm / slice | Data | n | WR | ROI (fees in) | Notes |
|-------------------|------|---|-----|---------------|--------|
| **ineff — lead only** (spot move ≥0.2% in 5m + residual edge, ask &lt; 0.60) | BTC 15m paths ∩ seconds (~14.7d overlap) | **75** | **~56%** | **~+30%** | Best single slice; max loss streak ~4 |
| **ineff — lead + false_dump** | same | **185** | **~37%** | **~+10%** | false_dump alone was weak / negative |
| **crossreb-style** (cross ~0.70 / ask ≤ 0.80, 1 trade / window) | path research cuts | moderate n | mid–high WR | small **+** when filtered | Needs fold re-check; raw unfiltered cross on full path ≈ **−3…−5%** |
| **rozi** (book dump tiers, e.g. soft streak filters) | 15m paths | often small n | ~30–45% in tight grids | **fragile** | Edge sensitive to window length / FAST_S; weekly decay in one study |
| **comeback** (underdog / pocket rules) | 5m+15m paths | varies | zone-dependent | mixed | Some frac zones (e.g. late window) looked toxic — filter, don’t blanket |

**Takeaway:** the **clearest paper plus** in our runs was **ineff lead** on path+spot, not raw mid/ask thresholds. Unfiltered path-only rules (cheap leader, underdog, open corridor) on full BTC 5m/15m paths were typically **−3% to −7% ROI after fees** at high n — useful as baselines, not as deploy configs.

### Other 5m / 15m bots (potential + path checks)

These live in root folders and target the same short windows. Path replays (BTC, taker+builder) and design notes:

| Bot | Idea | Path replay (BTC) | Potential |
|-----|------|-------------------|-----------|
| **[closer](closer/)** | Buy the **leader in the last ~3–5s** (tail) | Rare clean prints on path (many tails are 0/flat); when bid ≥0.85–0.90, **WR ~85–90%** but **avg ask ~0.93–0.95 → ROI negative** after taker fees | **High WR / fee-sensitive.** Cap max ask, use maker, or only trade when ask ≪ implied WR |
| **[gozi](gozi/)** | **Late impulse** last ~30s on **5m** (strong leader + volume) | Proxy “last 30s bid≥0.80”: n~700 (5m), WR~81%, avg ask~0.89, **ROI ~−10%** taker | Same story: direction is right, **price too expensive** as taker. Filters on max ask / size progression are the lever |
| **[dozi](dozi/)** | Impulse family companion (volume + timing) | Same class as gozi; no separate edge without volume tape | Potential only with **strict timing + ask cap** |
| **[flash](flash/)** | **CEX flash** (Binance/OKX) → Polymarket side | Needs CEX tape (not path-only). Conceptually related to **ineff lead** | **High potential** if flash→book lag is stable; join with `datasets/seconds/` |
| **[chainlink_edge](chainlink_edge/)** | Oracle / Chainlink vs book lag | Needs oracle feed + book | Structural edge class; not in path JSONL alone |
| **[boxarb](boxarb/)** | Nested **5m/15m/4h** box on same TWAP stream | Needs multi-TF live books | Arb-style; positive when boxes misprice, rare |
| **[preladder](preladder/)** | Both-sides limit grid before open + ladder | Path can stress-test fill paths; live queue risk | Inventory / maker research, not a simple WR story |
| **[streakbot](streakbot/)** | Series of outcomes, arm near end | Needs sequence of windows, not one path | Optional confirm filter on top of closer/gozi |
| **[udogq](udogq/)** | Underdog from quotes + σ vs strike | Related to residual fair; path+seconds | Same family as ineff residual; corridor filters matter |

**Late-leader family (closer / gozi proxies) — full path, fees in:**

| Proxy rule | TF | n | WR | avg ask | ROI |
|------------|----|---|-----|---------|-----|
| Last 30s, bid ≥ 0.80 | 5m | ~714 | ~82% | ~0.89 | **~−10%** |
| Last 30s, bid ≥ 0.90 | 5m | ~811 | ~86% | ~0.94 | **~−10%** |
| Last 60s, bid ≥ 0.75, ask ≤ 0.90 | 15m | ~515 | ~81% | ~0.83 | **~−4%** |
| Mid-window leader bid ≥ 0.70, ask ≤ 0.85 | 15m | ~2.5k | ~76% | ~0.77 | **~−4%** |

So: **WR looks “great”, ROI does not** — until max-ask / maker / spot-confirm is added. That is exactly where filters can turn a high-WR idea into a candidate (or prove it stays negative).

### Path-only baselines (full BTC dumps, high frequency)

| Rule (sketch) | TF | n (order) | WR | ROI after fees |
|---------------|----|-----------|-----|----------------|
| Cross ~0.70, ask ≤ 0.80 | 15m / 5m | ~2.8k / ~5.9k | ~73–74% | **−3% to −5%** |
| Cheap leader ask ∈ [0.55, 0.70] | 15m / 5m | ~2.7k / ~5.5k | ~61–63% | **−5% to −7%** |
| Underdog ask ∈ [0.25, 0.40] | 15m / 5m | ~2.7k / ~5.4k | ~32–33% | **−5% to −7%** |
| Corridor ask ∈ [0.30, 0.45] | 15m / 5m | ~2.8k / ~5.9k | ~38% | **~−5%** |

### How to push results further

Signals can be **improved and cleaned with filters** — that is the intended use of this repo:

- price corridor and **max ask** (critical for closer/gozi);
- **frac of window** (avoid open and final seconds unless intentionally tail-trading);
- **one entry per reason / side**;
- skip **true dumps** (book + fair both extreme);
- require **spot lead** or residual edge where seconds exist (ineff / flash class);
- report **3 time folds**, max loss streak, trades/day — not only headline WR.

Reproduce, tighten, or refute on the same `datasets/`; PRs and issues with grids are welcome. Do not treat any row above as a finished live strategy.

---

## For humans and AI agents

You can use this repo to:

1. **Backtest** a rule on real 5m/15m windows (winner + bid/ask path).
2. **Join** book paths with Binance second bars for spot–book edge studies.
3. **Compare** strategies under the same fee model (taker ≈ `0.07 × p × (1−p)` per share, optional builder % on notional).
4. **Iterate with an AI agent**: point the agent at `datasets/README.md` + a bot folder, ask for a grid search, fold stability, or a new signal class.

Suggested agent prompt skeleton:

```text
Load datasets/README.md and algorithms/<name>.
Replay BTC 5m/15m path JSONL with fee+builder.
Report n, WR, ROI, max loss streak, and 3 time folds.
Do not claim live profitability from paper alone.
```

---

## Quick start

```bash
git clone https://github.com/govinterset-dotcom/polymarket-algos.git
cd polymarket-algos
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt  # requests websockets py-clob-client pyarrow pandas ...

# Paper-run a research bot
cd algorithms
cp comeback.env.example comeback.env
# edit PAPER=true
python comeback.py
```

Datasets: see **[datasets/README.md](datasets/README.md)**. Large binaries may ship via **Git LFS** or **Release assets** as the archive grows.

---

## Builder attribution

Live CLOB paths may embed builder codes in source. Private keys and funders are **never** committed — only `*.env.example`.

---

## License

Polymarket Builder Attribution Source-Available License 1.0 — see [LICENSE](LICENSE).

## Additional July 2026 pre-TWAP order-book and trade archive

Four BTC/ETH archives were added under [datasets/paths/](datasets/paths/). They contain **2,567 unique market windows** across 5m and 15m markets, covering July 6–19, 2026 (before the August 7, 2026 switch to 60-second TWAP resolution). Counts, archive contents, field definitions, and research caveats are documented in [datasets/README.md](datasets/README.md) and [datasets/paths/SOURCE.md](datasets/paths/SOURCE.md).

These July 2026 markets belong to the pre-TWAP resolution regime; do not assume their outcome behavior transfers directly to the post-switch regime.
