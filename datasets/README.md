# Datasets

Historical **Polymarket up/down** windows and **Binance BTC** second bars used for offline research.

**Status:** living archive — coverage, assets, and quality will be **updated and improved** over time. Check `MANIFEST.md` for file list and hashes when present.

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
