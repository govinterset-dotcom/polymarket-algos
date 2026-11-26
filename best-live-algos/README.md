# Best live algorithms

Curated signals with a positive **path backtest** and notes from **paper** runs.

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
cp mojo.env.example mojo.env
python mojo.py
```

> Paper and path results are **not** live guarantees. High-WR “crumbs”, not high ROI.

---

## path_z — role + residual-z (BTC 5m / 15m)

Trade **leader** or **underdog** when Binance residual `z` is already **vs** / **fav** the book leader, at fixed fractions.

**Live core:** `15m_U_fav_20/25/40` + `5m_L_vs_35/40`  
Details, tables, paper cut: **[path_z/](path_z/)**.

| Block | Path (fees in) | Paper (~21h, broader set) |
|-------|----------------|---------------------------|
| 15m underdog + fav | n≈1.3k, WR≈26%, **sum ~+$243** | **+$81** on U_fav rules |
| 5m leader + vs @35/40 | n≈1.5k, WR≈84%, **sum ~+$135** | sparse on paper; early L_vs_25/30 dragged full run |
| Full multi-rule paper | — | WR~61%, **−$35**, maxDD ~$−123 |

Tighten with `DISABLE_SIGNALS`, `ONE_SIGNAL_PER_WINDOW=true`. Upload `path_z.py` yourself; env: [`path_z/path_z.env.example`](path_z/path_z.env.example).

> Payoff-driven U_fav (low WR) + thin high-WR L_vs. Regime shifts can flip paper vs path — re-fold after changes.
