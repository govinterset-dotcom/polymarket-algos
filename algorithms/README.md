# Algorithms (research set)

Canonical copies of strategies used in recent path / seconds analysis.

| File | Role | TF focus |
|------|------|----------|
| `comeback.py` + `comeback.env.example` | Point-in-time leader / underdog pockets on BTC 5m (heatmap-filtered rules) | 5m |
| `rozi.py` + `rozi.env.example` | Tier drop / recovery taker (H1-style configs in env) | 15m |
| `ineff.py` + `ineff.env.example` | Residual edge / spot lead / false dump vs fair Φ model | 15m |
| `crossreb.py` + `crossreb.env.example` | Book **cross** threshold + **rebound** (1 trade/window recommended) | 15m |
| `preladder.py` | Pre-window / ladder limit experiments | research |
| `doppler.py` | Companion research module | research |

### Conventions

- Config via `ENV_FILE=...` or `<name>.env` next to the script.
- `PAPER=true` by default in examples — no orders.
- Journals: `*.jsonl` events (`signal`, `fill`, `resolved`).
- Compounding and stake floors follow each bot’s env (`DEPOSIT`, `STAKE_PCT`, `MIN_SHARES`).

### Notes

- Root-level folders (e.g. `comeback/`, `rozi/`) remain for the broader historical collection.
- Prefer **this** `algorithms/` tree for reproducibility against `datasets/`.
