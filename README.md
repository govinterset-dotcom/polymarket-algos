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

## Paper replays on `datasets/` (informative, not a live claim)

Several signal classes were **replayed offline** on the path archives in this repository (BTC 5m / 15m order-book paths from `datasets/paths/`, fee model taker + ~1% builder, fixed stake). Numbers below are **paper / historical** only: they show that the data is usable for research and that **raw unfiltered rules are usually thin or negative after fees**. Positive pockets appear when signals are combined with spot lead, price corridors, or stricter filters — and those edges need independent re-checks on time folds and out-of-sample windows.

**Illustrative path-only baselines (BTC, fill at ask, fees in):**

| Rule (sketch) | TF | n (order) | WR (order) | ROI after fees |
|---------------|----|-----------|------------|----------------|
| Mid/ask “cross” ~0.70, ask ≤ 0.80 | 15m / 5m | high thousands | ~73–74% | about **−3% to −5%** |
| Cheap leader ask ∈ [0.55, 0.70] | 15m / 5m | high thousands | ~61–63% | about **−5% to −7%** |
| Underdog ask ∈ [0.25, 0.40] | 15m / 5m | high thousands | ~32–33% | about **−5% to −7%** |
| Edge-style corridor ask ∈ [0.30, 0.45] | 15m / 5m | high thousands | ~38% | about **−5%** |

**With spot + book (ineff-style lead / false_dump on overlapping BTC seconds + 15m paths):** lead-only was the stronger slice in one research cut (higher WR, shorter loss streaks); false_dump alone was weaker; **time folds were uneven** — treat as a hypothesis to re-run, not a finished product.

**What this is for:** the repo ships **real windows + outcomes** so anyone can verify, reject, or improve these rules. You are invited to:

- tighten **filters** (frac of window, max ask, one entry per reason/side, skip true dumps);
- add **spot / σ / residual fair** where seconds data exists;
- test **stability across folds** and assets (ETH dumps under `datasets/paths/`);
- clean signals rather than stack uncorrelated noise.

Directions that often matter more than a single headline WR: fee-aware ROI, max loss streak, trades per day, and whether the edge survives a later calendar slice. Pull requests and issues that reproduce a grid on `datasets/` are welcome.

---

## Builder attribution

Live CLOB paths may embed builder codes in source. Private keys and funders are **never** committed — only `*.env.example`.

---

## License

Polymarket Builder Attribution Source-Available License 1.0 — see [LICENSE](LICENSE).
## Additional July 2026 pre-TWAP order-book and trade archive

Four BTC/ETH archives were added under [datasets/paths/](datasets/paths/). They contain **2,567 unique market windows** across 5m and 15m markets, covering July 6–19, 2026 (before the August 7, 2026 switch to 60-second TWAP resolution). Counts, archive contents, field definitions, and research caveats are documented in [datasets/README.md](datasets/README.md) and [datasets/paths/SOURCE.md](datasets/paths/SOURCE.md).

These July 2026 markets belong to the pre-TWAP resolution regime; do not assume their outcome behavior transfers directly to the post-switch regime.
