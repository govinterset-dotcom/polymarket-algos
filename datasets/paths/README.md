# Path datasets (JSONL)

Place `*.jsonl` window paths here (one market per line).

See [../README.md](../README.md) for schema (`slug`, `start`, `winner`, `bid`, `ask`).

### Coverage

Unique markets (windows) in **path**: **~12 600**  
Of which **BTC 5m ≈ 7 000**, **BTC 15m ≈ 3 200** (plus ETH/SOL/XRP in some dumps).

Bulk archive: **[path.zip](path.zip)**.

Preferred published names (see `../MANIFEST.md`):

- `rozi15s_path-3.jsonl` — longest 15m BTC-oriented path set
- `rozi_path.jsonl`, `rozi15_path.jsonl`, `rozi15s_path.jsonl`
- `comeback_path.jsonl` — 5m/15m from comeback logging

Upload large files via **Git LFS** or a repo **Release**; keep `MANIFEST.md` hashes in sync.
---

## Additional July 2026 BTC/ETH archives (pre-TWAP)

Four ZIP archives were added:

| Archive | Markets | Period | Window coverage | Up / Down |
|---|---:|---|---:|---:|
| `btc_5m__.zip` | 809 | Jul 6 00:00 – Jul 8 22:55, 2026 | 809 / 851 (95%) | 391 / 418 |
| `btc_15m__.zip` | 679 | Jul 6 00:00 – Jul 19 23:50, 2026 | 679 / 1,344 (51%) | 327 / 352 |
| `eth_5m__.zip` | 809 | Jul 6 00:00 – Jul 8 22:55, 2026 | 809 / 851 (95%) | 391 / 418 |
| `eth_15m.zip` | 270 | Jul 6 00:00 – Jul 8 22:50, 2026 | 270 / 284 (95%) | 133 / 137 |

**Total: 2,567 markets** — 1,618 5m and 949 15m. The `eth_15m.zip` archive contains a nested copy of `eth_5m__.zip`, byte-for-byte identical to the separately published archive; do not count it twice.

Each archive contains a metadata JSON file, a top-of-book Parquet file, and a trades Parquet file. Metadata includes market/window identifiers, token IDs, outcome and tick-change information. Top-of-book records contain bid/ask updates for both Up and Down tokens; trades contain execution price, size, side, receive timestamp and fee basis points. Prices (`bid`, `ask`, `px`) are encoded in thousandths and must be divided by 1,000. Trade sizes (`sz`) are in micro-units and must be divided by 1,000,000.

These are July 2026 **pre-TWAP** markets, before the August 7, 2026 move to 60-second TWAP resolution. See [`../README.md`](../README.md) for row volumes, field-level schemas, coverage details and research caveats.
