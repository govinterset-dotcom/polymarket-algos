# sniper — heatmap tops (BTC 5m + 15m)

Point-in-time pockets: at a fixed **fraction** of the window, buy **leader** or **underdog** when its ask sits in a narrow bin. Selection claim on path: **ROI ≥ +4% after fees** and **all 3 time folds > 0** (12 rules on 5m, 7 on 15m).

Upload `sniper.py` into this folder. Env: [`sniper.env.example`](sniper.env.example).

### Signal list (19)

**BTC 5m underdog**

| Name | frac | ask bin |
|------|------|--------|
| `5m_ud_90_005_010` | 0.90 | 0.05–0.10 |
| `5m_ud_80_005_010` | 0.80 | 0.05–0.10 |
| `5m_ud_85_035_040` | 0.85 | 0.35–0.40 |
| `5m_ud_80_030_035` | 0.80 | 0.30–0.35 |
| `5m_ud_85_015_020` | 0.85 | 0.15–0.20 |
| `5m_ud_90_020_025` | 0.90 | 0.20–0.25 |
| `5m_ud_80_010_015` | 0.80 | 0.10–0.15 |
| `5m_ud_60_050_055` | 0.60 | 0.50–0.55 |
| `5m_ud_75_025_030` | 0.75 | 0.25–0.30 |
| `5m_ud_50_040_045` | 0.50 | 0.40–0.45 |
| `5m_ud_40_045_050` | 0.40 | 0.45–0.50 |

**BTC 5m leader**

| Name | frac | ask bin |
|------|------|--------|
| `5m_ld_05_085_090` | 0.05 | 0.85–0.90 |

**BTC 15m underdog**

| Name | frac | ask bin |
|------|------|--------|
| `15m_ud_25_030_035` | 0.25 | 0.30–0.35 |
| `15m_ud_45_030_035` | 0.45 | 0.30–0.35 |
| `15m_ud_65_025_030` | 0.65 | 0.25–0.30 |
| `15m_ud_60_015_020` | 0.60 | 0.15–0.20 |
| `15m_ud_30_020_025` | 0.30 | 0.20–0.25 |
| `15m_ud_20_045_050` | 0.20 | 0.45–0.50 |

**BTC 15m leader**

| Name | frac | ask bin |
|------|------|--------|
| `15m_ld_15_085_090` | 0.15 | 0.85–0.90 |

Side from Up midpoint; Down ask = `1 − bid_Up`. FAK / paper fill at ask. Each matched pocket can fire separately unless you add `ONE_SIGNAL_PER_WINDOW` in a fork.

---

### Path (selection claim)

- Filter: ROI ≥ **+4%** after taker + builder, **3/3 time folds > 0** on BTC path dumps used for the heatmap.
- 5m: 12 pockets; 15m: 7 pockets.
- Independent of residual-z (unlike path_z); pure book time×price grid.

Re-run the grid on current `datasets/` before trusting the cut — folds can move after regime change.

---
Time×price bins with path claim ROI ≥ +4%, 3/3 folds > 0 (12×5m + 7×15m).

Path claim	Paper (full stack)
Heatmap filter on BTC path	WR ~33%, net negative; late longshot underdogs (~0% WR
### Paper (sniper journal, multi-signal stack)

Broad live of the full 19-rule set (multi entries per window, ~40h class run):

| Metric | Approx. |
|--------|--------|
| Resolved | ~260+ |
| WR | ~**33–34%** |
| Sum PnL | **negative** (order **−$100…−$130** at $5-class stakes) |
| Main drag | Late cheap underdogs (e.g. `*_005_010`) with **near-0% WR** on the sample; stacking several underdogs in one window |

**1 signal / window** variant on the same log still stayed red — stacking was not the only issue; longshot underdog pockets failed out of sample.

**Takeaway for live:** path heatmap ≠ paper. Prefer a **whitelist** of mid-price underdogs / early leaders that still show positive folds on a fresh path slice; disable pure longshot bins (`0.05–0.10` late) until re-validated. Compare with **mojo** (single high-WR leader) and **path_z** (z-conditioned core) which are narrower.

---

### Run

```bash
cd best-live-algos/sniper
cp sniper.env.example sniper.env
# PAPER=true by default
python sniper.py
```

> Path ROI filters and paper results are **not** live guarantees. Wide underdog grids are fragile after fees and in trending regimes.
