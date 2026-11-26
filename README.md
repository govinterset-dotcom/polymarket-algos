# Polymarket Algorithms

Open research workspace for **Polymarket** short-horizon markets (BTC/ETH up-down 5m / 15m): live-style Python bots, fee-aware backtests, and **historical datasets** (order-book paths + Binance seconds).

The goal is simple: anyone — researchers, traders, or **AI agents** — can clone the repo, load the data, and test or invent strategies without reinventing market plumbing.

> Paper / historical results are **not** guarantees of live performance. Fees (taker + optional builder) matter.

---

## Best live algorithms

Curated signals that held up on **path backtests** (and where available, paper). See **[best-live-algos/](best-live-algos/)**.

- **[mojo](best-live-algos/mojo/)** — BTC 5m **leader at 25% of the window, ask ≥ 0.95** (`leader_25_095_100` only). Path: n≈208, WR≈99%, all weeks green, ~6 trades/day; paper 23–26 Nov: 39/39 wins. Small edge after fees — high-WR “crumbs”, not high ROI.
- **[path_z](best-live-algos/path_z/)** — BTC 5m/15m **role + Binance residual-z**. Live core: `15m_U_fav_20/25/40` + `5m_L_vs_35/40`. Path: U_fav block ~+$243 (n≈1.3k, low WR); L_vs_35/40 ~+$135 (WR≈84%). Paper (~21h, wider set) net negative until L_vs_25/30 and multi-stack are disabled; U_fav alone stayed green.

---

## Layout

```
polymarket-algos/
  best-live-algos/     # curated live-oriented signals (mojo, path_z, …)
  algorithms/          # strategy code + env examples (research set)
  datasets/            # historical data + schema docs (updated over time)
  <legacy folders>/    # older one-folder-per-bot layout still present at repo root
  README.md
  LICENSE
```

| Path | Purpose |
|------|---------|
| **[best-live-algos/](best-live-algos/)** | Shortlist with path + paper notes |
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
| **ineff — lead only** (spot move ≥0.2% in 5m + residual edge, ask < 0.60) | BTC 15m paths ∩ seconds (~14.7d overlap) | **75** | **~56%** | **~+30%** | Best single slice; max loss streak ~4 |
| **ineff — lead + false_dump** | same | **185** | **~37%** | **~+10%** | false_dump alone was weak / negative |
| **crossreb — rebound only** (mid was ≤0.25, then ask ∈ [0.70, 0.85], frac≥0.50) | BTC 15m full path (~43d) | **874** | **~76%** | **~+1.4%** | Only stably non-negative crossreb slice; folds ~+3.6% / +0.6% / −0.2%; ~20/day |
| **crossreb — cross** (below 0.65 → thr 0.70, ask≤0.80) | same | ~3020 | ~72% | **~−4%** | High frequency (~70/day); all folds negative on this dump |
| **crossreb — both** | same | ~3894 | ~73% | **~−2.8%** | Cross drag dominates rebound |
| **rozi** (book dump tiers, e.g. soft streak filters) | 15m paths | often small n | ~30–45% in tight grids | **fragile** | Edge sensitive to window length / FAST_S; weekly decay |
| **comeback** (underdog / pocket rules) | 5m+15m paths | varies | zone-dependent | mixed | Some frac zones toxic — filter |
| **mojo / leader_25** | BTC 5m path | **208** | **~99%** | small + after fees | Isolated leader ask≥0.95 @25%; paper 39/39 |
| **path_z U_fav core** | path ∩ seconds | ~1.3k | ~26% | positive $ sum | Payoff-driven underdog + fav z |
| **path_z L_vs 35/40** | path ∩ seconds | ~1.5k | ~84% | small + | Thin leader + vs residual |

**crossreb detail:** on the full BTC 15m path archive, **rebound-only** stayed near flat-to-slightly-positive after fees (~+1.4% ROI, ~20 trades/day). Default **cross** alone is negative (~−4%) at high n — the older docstring “+2.5%” does **not** reproduce on this dump. Prefer `ENABLE_CROSS=false` + rebound defaults for paper follow-ups, then re-check folds.

**Takeaway:** the **clearest paper plus** was **ineff lead** on path+spot. Unfiltered path-only rules on full BTC 5m/15m paths were typically **−3% to −7% ROI after fees** at high n. **mojo** is high-WR crumbs; **path_z** mixes low-WR payoff pockets with thin high-WR leader-vs.

### Other 5m / 15m bots (potential + path checks)

| Bot | Idea | Path replay (BTC) | Potential |
|-----|------|-------------------|-----------|
| **[closer](closer/)** | Leader last ~3–5s | WR ~85–90%, ask ~0.93–0.95 → **ROI negative** as taker | Cap max ask / maker |
| **[gozi](gozi/)** | Late impulse last ~30s on 5m | n~700, WR~81%, ask~0.89, **ROI ~−10%** | Ask filters |
| **[dozi](dozi/)** | Impulse + volume | Same class as gozi | Strict timing + ask cap |
| **[flash](flash/)** | CEX flash → PM | Needs CEX tape; related to ineff lead | High if lag stable |
| **[chainlink_edge](chainlink_edge/)** | Oracle vs book | Needs oracle feed | Structural |
| **[boxarb](boxarb/)** | Nested 5m/15m/4h | Multi-TF books | Rare arb |
| **[preladder](preladder/)** | Both-sides grid | Stress-test path | Maker research |
| **[streakbot](streakbot/)** / **[udogq](udogq/)** | Series / underdog+σ | Filters on top of other signals | Confirm / residual |

**Late-leader proxies (fees in):** last 30s bid≥0.80 on 5m → WR~82%, ROI **~−10%**; last 60s bid≥0.75 ask≤0.90 on 15m → WR~81%, ROI **~−4%**.

### Path-only baselines (full BTC dumps)

| Rule | TF | n (order) | WR | ROI |
|------|----|-----------|-----|-----|
| Cross ~0.70, ask ≤ 0.80 | 15m / 5m | ~2.8k / ~5.9k | ~73–74% | **−3…−5%** |
| Cheap leader 0.55–0.70 | 15m / 5m | ~2.7k / ~5.5k | ~61–63% | **−5…−7%** |
| Underdog 0.25–0.40 | 15m / 5m | ~2.7k / ~5.4k | ~32–33% | **−5…−7%** |
| Corridor 0.30–0.45 | 15m / 5m | ~2.8k / ~5.9k | ~38% | **~−5%** |

### How to push results further

Tighten **max ask**, **frac**, one entry per reason, skip true dumps, require **spot lead** where seconds exist; always report folds and loss streaks. PRs that reproduce grids on `datasets/` are welcome.

---

## For humans and AI agents

1. **Backtest** on real 5m/15m windows (winner + bid/ask path).
2. **Join** book paths with Binance seconds for spot–book edge.
3. **Compare** under the same fee model.
4. **Iterate with an AI agent** on `datasets/README.md` + a bot folder.

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
pip install -r requirements.txt
cd algorithms
cp comeback.env.example comeback.env   # PAPER=true
python comeback.py
```

Or the curated live shortlist:

```bash
cd best-live-algos/mojo && python mojo.py
cd best-live-algos/path_z && python path_z.py   # after uploading path_z.py
```

Datasets: **[datasets/README.md](datasets/README.md)**.

---

## Builder attribution

Private keys and funders are **never** committed — only `*.env.example`.

---

## License

Polymarket Builder Attribution Source-Available License 1.0 — see [LICENSE](LICENSE).

## Additional July 2026 pre-TWAP order-book and trade archive

Four BTC/ETH archives under [datasets/paths/](datasets/paths/): **2,567 unique windows**, July 6–19, 2026 (pre-TWAP). See [datasets/README.md](datasets/README.md). Do not assume transfer to post-TWAP regime.
