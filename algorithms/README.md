# Algorithms (research set)

Each strategy has **code + env example**. Prefer running from this folder with `ENV_FILE=...`.

| Entry | Source of truth | Env |
|-------|-----------------|-----|
| `comeback.py` | [`../comeback/comeback.py`](../comeback/comeback.py) (wrapper) | `comeback.env.example` / `../comeback/comeback.env.example` |
| `rozi.py` | [`../rozi/rozi.py`](../rozi/rozi.py) (wrapper) | `rozi.env.example` |
| `preladder.py` | [`../preladder/preladder.py`](../preladder/preladder.py) (wrapper) | `../preladder/preladder.env.example` |
| `crossreb.py` | **full source in this folder** | `crossreb.env.example` |
| `ineff.py` | **full source in this folder** | `ineff.env.example` |
| `doppler.py` | **full source in this folder** (when present) | — |

### Run

```bash
cd algorithms
cp crossreb.env.example crossreb.env   # edit PAPER=true
python3 crossreb.py

cp ../comeback/comeback.env.example comeback.env
python3 comeback.py
```

Root folders (`gozi`, `flash`, `boxarb`, …) remain the broader collection — each still has its own `*.py`.

Replay research against `../datasets/` (path zip + seconds).
