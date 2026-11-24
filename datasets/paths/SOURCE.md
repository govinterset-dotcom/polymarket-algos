# Path data source

Preferred bulk package: **`path.zip`** (~7.5 MB compressed, ~85 MB uncompressed).

### Coverage

Unique markets (windows) in **path**: **~12 600**  
Of which **BTC 5m ≈ 7 000**, **BTC 15m ≈ 3 200** (plus ETH/SOL/XRP in some dumps).

Contents (JSONL order-book paths):

| File in zip | Notes |
|-------------|--------|
| `path/rozi_path.jsonl` | ~18 MB |
| `path/rozi5_path.jsonl` | ~17 MB |
| `path/rozi5s_path.jsonl` | ~4 MB |
| `path/rozi15_path.jsonl` | ~8 MB |
| `path/rozi15s_path.jsonl` | ~22 MB |
| `path/comeback_path.jsonl` | ~11 MB |
| `path/comeback_path (1).jsonl` | ~4 MB (may overlap) |

## Publish once via GitHub Release (easiest)

1. Open https://github.com/govinterset-dotcom/polymarket-algos/releases/new
2. Tag: `datasets-path-2026-11`
3. Title: `Path JSONL archive`
4. Attach **`path.zip`** (drag and drop)
5. Publish release

Then anyone (or an AI agent) downloads:

```bash
curl -L -o path.zip \
  https://github.com/govinterset-dotcom/polymarket-algos/releases/download/datasets-path-2026-11/path.zip
unzip path.zip -d datasets/
# results in datasets/path/*.jsonl
```

The agent channel cannot push this binary; Release UI has no practical size pain for 7.5 MB.
---

## Additional July 2026 BTC/ETH archive (pre-TWAP)

Four additional ZIP archives are published in this directory:

| Archive | Compressed bytes | Markets | Date range | Possible windows covered | Up / Down |
|---|---:|---:|---|---:|---:|
| `btc_5m__.zip` | 14,481,382 | 809 | Jul 6 00:00 – Jul 8 22:55, 2026 | 809 / 851 (95%) | 391 / 418 |
| `btc_15m__.zip` | 9,176,404 | 679 | Jul 6 00:00 – Jul 19 23:50, 2026 | 679 / 1,344 (51%) | 327 / 352 |
| `eth_5m__.zip` | 6,363,011 | 809 | Jul 6 00:00 – Jul 8 22:55, 2026 | 809 / 851 (95%) | 391 / 418 |
| `eth_15m.zip` | 10,088,732 | 270 | Jul 6 00:00 – Jul 8 22:50, 2026 | 270 / 284 (95%) | 133 / 137 |

Total: **2,567 markets** (5m: 1,618; 15m: 949). All records are from July 2026 and predate the August 7, 2026 switch to 60-second TWAP resolution.

### Files and schemas inside each archive

Each dataset contains three files:

- `*_meta.json`: market list with `window_start` (Unix seconds), `coin`, `tf`, `slug`, `market` (market address), `era` (all `v2`), `winner` (`Up` / `Down`), `asset_ids` (Up and Down token IDs), `tick_changes`, and `n_src_rows`.
- `*_top.parquet`: best bid/ask book updates. Columns: `window_start`, `ts` (milliseconds), `up` (true for Up token, false for Down), `bid`, `ask`. Prices are integer thousandths (divide by 1,000; e.g. 460 = 0.46; encoded range 0–1,000).
- `*_trades.parquet`: trades. Columns: `window_start`, `ts`, `ts_recv` (receive time), `up`, `px` (thousandths), `sz` (micro-units; 60,000,000 = 60 shares), `buy`, `fee_bps`. Divide `px` by 1,000 and `sz` by 1,000,000.

### Row volumes and caveats

| Dataset | Top rows | Trade rows | Median top rows / market | Median trades / market |
|---|---:|---:|---:|---:|
| BTC 5m | 1.42m | 1.99m | 1,537 | 2,325 |
| BTC 15m | 1.57m | 0.79m | 2,048 | 1,101 |
| ETH 5m | 1.33m | 0.41m | 1,517 | 485 |
| ETH 15m | 1.28m | 0.12m | 4,297 | 422 |

BTC 5m top rows are split almost evenly between Up and Down token books: 707,764 and 707,878 rows. All BTC 5m markets have pre-window quotes (median 24 rows); the reported data also includes 4 rows after the window end.

The final book agrees with metadata `winner` in 97.3% (BTC 5m), 98.2% (BTC 15m), 97.5% (ETH 5m), and 99.6% (ETH 15m). Use metadata `winner` as the resolved outcome where the final book did not converge or the feed was truncated.

Corresponding BTC and ETH window outcomes agree in approximately 86–87% of cases, not 100%. Despite identical Up/Down totals in BTC 5m and ETH 5m, the per-window outcomes are not identical.

**Coverage note:** BTC 15m has only 679 of 1,344 possible windows (51%) across its 14-day span; the other three datasets cover approximately 95% of their respective possible windows.

**Nested duplicate:** `eth_15m.zip` contains `eth_5m__.zip`, byte-for-byte identical to the separately published `eth_5m__.zip`. This nested copy is not additional coverage.

**Regime caveat:** these are pre-TWAP observations. The August 7, 2026 change to 60-second TWAP resolution means their historical outcome behavior should not be assumed to represent current markets. The counts and row statistics in this section reflect the supplied archive inventory.
