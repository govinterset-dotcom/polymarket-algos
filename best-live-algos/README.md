# Best live algorithms

Curated signals with a positive **path backtest** and a stable **paper** run.

---

## mojo — `leader_25_095_100`

Single active rule (others off via `DISABLE_SIGNALS` in `mojo.env`):

| Parameter | Value |
|-----------|--------|
| Market | BTC up/down **5m** |
| Timing | **25%** of the window (~second 75) |
| Side | **Leader** (mid > 0.5) |
| Price | ask ∈ **[0.95, 1.00)** |
| Execution | FAK / paper fill at ask |

Files: [`mojo/mojo.py`](mojo/mojo.py), [`mojo/mojo.env`](mojo/mojo.env).

### Backtest (BTC 5m path, ~7,450 windows, ~37 days)

Stake $5, taker fee `0.07×p×(1−p)` + 1% builder:

| Metric | Value |
|--------|--------|
| Trades | **208** |
| Win rate | **~99.0%** |
| Avg ask | ~0.963 |
| Mean PnL / trade | **+$0.08** |
| Sum PnL | **+$16.3** |
| Frequency | **~5.6 / day** (on active days median ~8) |
| Positive weeks | **6 / 6** |

Edge is thin (WR − price ≈ +2.7¢ before fees). After fee/builder a small profit remains with very high stability.

### Paper (comeback → mojo journal, 23–26 Nov 2026)

`leader_25` only (including windows that previously also fired leader_20):

| Metric | Value |
|--------|--------|
| Signals | **39** over ~4 days (~10/day) |
| Win rate | **100%** (39/39) |
| Sum PnL | **~$+2.3** (stake $5) |

The full comeback core (leader_20 + leader_25 + underdog_50) was net negative on the same stretch because of the underdog and cheaper leader pockets. Isolating **leader_25 only** removes that drag.

### Run

```bash
cd best-live-algos/mojo
cp mojo.env.example mojo.env   # or use mojo.env
# PAPER=true by default; live: PAPER=false + keys
python mojo.py
```

Startup log should show:

```text
DISABLE_SIGNALS (2): leader_20_090_095, underdog_50_040_045
active signals: 1
  ON  leader_25_095_100  leader  frac=0.25  ask=[0.95,1.00)
```

> Paper and path results are **not** live guarantees. The edge is small — high-WR “crumbs”, not a high-ROI strategy.
