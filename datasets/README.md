# Datasets

Historical **Polymarket up/down** windows and **Binance BTC** second bars used for offline research.

**Status:** living archive — coverage, assets, and quality will be **updated and improved** over time. Check `MANIFEST.md` for file list and hashes when present.

### Coverage (path archive)

Unique markets (windows) in **path**: **~12 600**  
Of which **BTC 5m ≈ 7 000**, **BTC 15m ≈ 3 200** (plus ETH/SOL/XRP in some dumps).

Main bulk download: [`paths/path.zip`](paths/path.zip).

---

## 1. Order-book paths (`paths/`)

**Format:** JSON Lines. One object per market window.

| Field | Type | Meaning |
|-------|------|---------|
| `slug` | string | e.g. `btc-updown-15m-1730000000` |
| `asset` | string | `btc`, `eth`, … (if present) |
| `start` | int | window start unix seconds |
| `winner` | string | `Up` or `Down` (resolved) |
| `bid` | float[] | per-second **Up** best bid (index ≈ second of window) |
| `ask` | float[] | per-second **Up** best ask |
| `source` | string | optional resolve source (`book` / `gamma`) |

**Down side quotes (synthetic):**

- `mid_down ≈ 1 - mid_up`
- `ask_down ≈ 1 - bid_up`
- `bid_down ≈ 1 - ask_up`

**TF:** filename / slug contains `5m` or `15m`. Length of arrays ≈ `tf * 60`.

**Typical sources in this project:**

| Pattern | Content |
|---------|---------|
| `*rozi*path*.jsonl` | 15m-heavy paths from rozi runs |
| `*comeback*path*.jsonl` | 5m/15m paths from comeback logging |
| Prefer **longest** path per `slug` when merging duplicates |

**Use:** replay any rule that only needs book + winner (comeback pockets, cross/rebound, rozi tiers, maker ladders).

---

## 2. Spot seconds (`seconds/`)

**Format:** Parquet (columns at least `ts_sec`, `close` — confirm with `pyarrow`).

| Pattern | Role |
|---------|------|
| `btc_seconds_YYYY-MM-DD_YYYY-MM-DD.parquet` | BTCUSDT-style second (or dense) bars for the date range in the name |

**Use:** fair-value / lead-lag / residual edge (`ineff`), distance filters, σ from 1m aggregates.

Join to a window with `start ≤ ts < start + length` and nearest-second match (tolerance ~30–90s if sparse).

---

## 3. Fees (recommended evaluation)

When reporting ROI, align with live bots:

- **Taker fee** ≈ `FEE_RATE * p * (1-p)` per share (`FEE_RATE=0.07` common).
- **Builder** optional: `BUILDER_FEE * usdc_spent` (e.g. 1%).

Net win: `shares - usdc - fees`; loss: `-usdc - fees`.

---

## 4. Quality / caveats

- Paths may have **gaps** (`null` bid/ask); forward-fill carefully or skip ticks with wide spread.
- Duplicate slugs: keep the record with the **longest** `bid` array.
- Early research files may mix assets; filter `slug` / `asset` for BTC-only studies.
- Parquet files are large; clone with **Git LFS** if enabled, or download from **Releases**.

---

## 5. Layout on disk

```
datasets/
  README.md          # this file
  MANIFEST.md        # sizes + short hashes of published files
  paths/             # *.jsonl window paths
  seconds/           # btc_seconds_*.parquet
```

If GitHub rejects very large blobs, binaries are published as **release assets** with the same names as in `MANIFEST.md`.

---

## 6. Minimal Python loaders

```python
import json
from pathlib import Path

def load_paths(dirpath, tf="15m", asset_prefix="btc"):
    best = {}
    for p in Path(dirpath).glob("*.jsonl"):
        with p.open() as f:
            for line in f:
                r = json.loads(line)
                slug = r.get("slug") or ""
                if f"-{tf}-" not in slug or not slug.startswith(asset_prefix):
                    continue
                if not r.get("winner") or not r.get("bid"):
                    continue
                prev = best.get(slug)
                if prev is None or len(r["bid"]) > len(prev["bid"]):
                    best[slug] = r
    return best
```

```python
import pyarrow.parquet as pq
t = pq.read_table("datasets/seconds/btc_seconds_....parquet", columns=["ts_sec", "close"])
```
---

## 7. Additional July 2026 BTC/ETH archive (pre-TWAP)

Four archives were added to [`paths/`](paths/). According to the archive inventory, they contain **2,567 markets** in total. These are July 2026 markets collected before the switch to 60-second TWAP resolution on August 7, 2026.

### Market coverage

| Dataset | Markets | Period (UTC) | Window coverage | Up / Down |
|---|---:|---|---:|---:|
| BTC 5m | 809 | Jul 6 00:00 – Jul 8 22:55 (3.0 days) | 809 / 851 (95%) | 391 / 418 |
| BTC 15m | 679 | Jul 6 00:00 – Jul 19 23:50 (14.0 days) | 679 / 1,344 (51%) | 327 / 352 |
| ETH 5m | 809 | Jul 6 00:00 – Jul 8 22:55 (3.0 days) | 809 / 851 (95%) | 391 / 418 |
| ETH 15m | 270 | Jul 6 00:00 – Jul 8 22:50 (3.0 days) | 270 / 284 (95%) | 133 / 137 |

By timeframe: **1,618 5m markets** (809 BTC + 809 ETH) and **949 15m markets** (679 BTC + 270 ETH). The 2,567 figure is the sum of the four asset/timeframe datasets; BTC and ETH windows are separate market records. BTC and ETH outcomes for corresponding windows agree in approximately 86–87% of cases, so they should not be treated as identical outcomes.

| Archive | Compressed size |
|---|---:|
| `btc_5m__.zip` | 14,481,382 bytes |
| `btc_15m__.zip` | 9,176,404 bytes |
| `eth_5m__.zip` | 6,363,011 bytes |
| `eth_15m.zip` | 10,088,732 bytes |

**Nested duplicate:** `eth_15m.zip` also contains `eth_5m__.zip`; that nested file is byte-for-byte identical to the separately published `eth_5m__.zip`. Do not count it as an additional dataset or additional markets.

### Archive structure and schemas

Each asset/timeframe dataset contains three data files:

- **`*_meta.json`** — market inventory and metadata: `window_start` (Unix seconds), `coin`, `tf`, `slug`, `market` (market address), `era` (all records are `v2`), `winner` (`Up` or `Down`), `asset_ids` (Up and Down token IDs), `tick_changes`, and `n_src_rows`.
- **`*_top.parquet`** — best bid/ask snapshots as the order book changes. Columns: `window_start`, `ts` (milliseconds), `up` (true = Up token, false = Down token), `bid`, `ask`. **Prices are stored in thousandths**: divide by 1,000 (e.g. 460 = 0.460; encoded range 0–1,000).
- **`*_trades.parquet`** — trade records. Columns: `window_start`, `ts`, `ts_recv` (receive timestamp), `up`, `px` (price in thousandths), `sz` (size in micro-units; 60,000,000 = 60 shares), `buy`, and `fee_bps`. Divide `px` by 1,000 and `sz` by 1,000,000 for human/share units.

### Data volume

| Dataset | Top-of-book rows | Trades | Median top rows / market | Median trades / market |
|---|---:|---:|---:|---:|
| BTC 5m | 1.42 million | 1.99 million | 1,537 | 2,325 |
| BTC 15m | 1.57 million | 0.79 million | 2,048 | 1,101 |
| ETH 5m | 1.33 million | 0.41 million | 1,517 | 485 |
| ETH 15m | 1.28 million | 0.12 million | 4,297 | 422 |

For BTC 5m, top-of-book rows are almost evenly split between Up and Down tokens (707,764 and 707,878 rows respectively), so both token books are represented.

### Interpretation and caveats

- **Pre-TWAP regime:** all four datasets are from July 2026, before the August 7 switch to 60-second TWAP resolution. Resolution behavior and market outcomes in this archive may differ from the current regime; do not transfer backtest conclusions to post-switch markets without a separate test.
- **Pre-window quotes:** 100% of BTC 5m markets have quotes before the window start (median 24 rows); the data also contains a median/observed 4 rows after the window end as reported in the archive analysis. This permits pre-open behavior analysis not available in the `rozi5_path` paths.
- **Use `winner` from metadata as the resolved outcome:** the final book agrees with metadata `winner` in 97.3% of BTC 5m, 98.2% of BTC 15m, 97.5% of ETH 5m, and 99.6% of ETH 15m markets. Remaining cases may have a book that did not converge to 0/1 or truncated data.
- **Coverage is uneven:** BTC 15m contains 679 of 1,344 possible windows (51%) over its 14-day span; the other three datasets cover approximately 95% of their stated windows.
- **Cross-asset dependence:** corresponding BTC and ETH window outcomes agree in about 86–87% of cases, not 100%. Identical Up/Down counts in BTC 5m and ETH 5m (391/418) do not mean the individual outcomes are identical.
- The row counts and descriptive statistics above are the supplied archive inventory; inspect the parquet/meta files when exact per-record validation or a different deduplication rule is required.
