# Algorithms (research set)

Strategies used with the shared **path / seconds** datasets. Full sources currently live in **root-level folders** (legacy layout); this directory holds the research index, env examples, and is the target place for unified copies.

| Research name | Code in repo | Env example | TF |
|---------------|--------------|-------------|-----|
| **comeback** | [`../comeback/comeback.py`](../comeback/comeback.py) | [`comeback.env.example`](comeback.env.example) (also under `comeback/`) | 5m leader/underdog pockets |
| **rozi** | [`../rozi/`](../rozi/) (if present) / research dumps | [`rozi.env.example`](rozi.env.example) | 15m tier drops |
| **ineff** | add under `algorithms/ineff.py` when synced | [`ineff.env.example`](ineff.env.example) | 15m edge / lead / false dump |
| **crossreb** | [`crossreb.env.example`](crossreb.env.example) + source to sync | cross + rebound, **1 trade/window** | 15m |
| **preladder** | [`../preladder/`](../preladder/) | research | limits / ladder |
| **doppler** | research companion | — | — |

Root folders (`gozi`, `flash`, `boxarb`, …) stay as the broad production/research collection.

### Conventions

- `PAPER=true` for dry runs; never commit real `.env` / keys.
- Evaluate with **taker + optional builder** fees (see `datasets/README.md`).
- Replay against `datasets/paths/*.jsonl` and `datasets/seconds/*.parquet`.

### AI-agent workflow

1. Read `datasets/README.md` + this file.
2. Load path JSONL (longest record per `slug`).
3. Implement or call a signal rule; report n, WR, ROI, folds, max loss streak.
4. Do not treat paper ROI as live edge.
