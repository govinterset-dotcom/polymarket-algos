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
