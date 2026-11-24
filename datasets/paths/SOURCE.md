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
