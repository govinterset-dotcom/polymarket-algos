# path_z — role + residual-z signals (BTC 5m / 15m)

Buy **leader** or **underdog** when Binance residual `z` is already against / with the current book leader, at fixed time fractions.

```text
z = log(spot_now / window_open) / (σ_1s * sqrt(remaining_s))
fav  = z >  +Z_THR
vs   = z <  -Z_THR
```

**Recommended live core** (others off via `DISABLE_SIGNALS`):

| Signal | TF | Role | State | Fraction |
|--------|----|------|-------|----------|
| `15m_U_fav_20` | 15m | underdog | fav | 0.20 |
| `15m_U_fav_25` | 15m | underdog | fav | 0.25 |
| `15m_U_fav_40` | 15m | underdog | fav | 0.40 |
| `5m_L_vs_35` | 5m | leader | vs | 0.35 |
| `5m_L_vs_40` | 5m | leader | vs | 0.40 |

Defaults: `Z_THR=0.55`, `SIGNAL_MAX_PX=0.95`, **one signal per window**, one side only. Execution: FAK / paper fill at ask.

Upload `path_z.py` into this folder; env template: [`path_z.env.example`](path_z.env.example).

---

### Backtest (BTC path ∩ Binance seconds, Z_THR=0.55)

Stake $5, taker `0.07×p×(1−p)` + 1% builder. Cap ask ≤ 0.95.

| Signal | n | WR | mean PnL | sum | weeks+ |
|--------|---|-----|----------|-----|--------|
| **15m_U_fav_20** | 354 | 28% | +$0.06 | **+$22** | 1/3 |
| **15m_U_fav_25** | 416 | 27% | +$0.23 | **+$97** | 2/3 |
| **15m_U_fav_40** | 500 | 24% | +$0.25 | **+$124** | 2/3 |
| **5m_L_vs_35** | 745 | 83% | +$0.10 | **+$74** | **3/3** |
| **5m_L_vs_40** | 772 | 84% | +$0.08 | **+$60** | 2/3 |
| 5m_L_vs_45 (off) | 753 | 84% | ≈0 | +$4 | 1/3 |
| 5m_L_vs_25 (off) | 677 | 81% | ≈0 | −$0 | 2/3 |
| 5m_L_vs_30 (off) | 717 | 83% | +$0.12 | +$85 | 3/3 |
| 15m_L_vs_20 (off) | 390 | 79% | +$0.13 | +$49 | 2/3 |

**U_fav block alone:** n≈1270, WR≈26%, **sum ≈ +$243** (payoff-driven).  
**L_vs_35/40:** high WR, thin edge, better week stability than early 5m leader-vs.

---

### Paper (~21h, 25–26 Nov 2026, broader signal set)

Journal with multi-rule list (before tightening to the core above):

| Metric | Value |
|--------|--------|
| Signals / resolved | 286 / 214 |
| WR | ~61% |
| Sum PnL | **−$35** |
| Deposit | 100 → ~75 (min ~32) |
| Max DD | **~$−123** |
| Side bias | ~91% Down |

**Per-rule paper (1 fill = 1 trade):**

| Signal | n | WR | sum PnL |
|--------|---|-----|---------|
| 15m_U_fav_20 | 35 | 40% | **+$49** |
| 15m_U_fav_25 | 13 | 46% | **+$22** |
| 15m_U_fav_40 | 12 | 42% | **+$10** |
| 5m_L_vs_35/40/45 | 4 | 100% | +$1 (too few) |
| 5m_L_vs_30 | 48 | 67% | −$21 |
| 15m_L_vs_20 | 40 | 55% | −$43 |
| 5m_L_vs_25 | 120 | 69% | −$53 |

Paper matched path on **15m underdog + fav**: positive despite low WR.  
**5m/15m leader + vs** at cheap–mid asks dragged the run (regime + inflated |z|). That is why the live shortlist drops `*_L_vs_25/30` and `15m_L_vs_*`.

---

### Run

```bash
cd best-live-algos/path_z
cp path_z.env.example path_z.env
# PAPER=true by default; live: PAPER=false + keys
python path_z.py
```

Expected startup:

```text
Z_THR=0.55  active signals=5  ONE_SIGNAL=True  ONE_SIDE=True
DISABLED (...): 5m_L_vs_25, 5m_L_vs_30, 5m_L_vs_45, 15m_L_vs_20, ...
  ON  15m_U_fav_20  ...
  ON  15m_U_fav_25  ...
  ON  15m_U_fav_40  ...
  ON  5m_L_vs_35  ...
  ON  5m_L_vs_40  ...
```

> Path and paper are **not** live guarantees. U_fav edges are payoff-driven (low WR); L_vs edges are thin after fees. Always re-check folds after regime shifts.
