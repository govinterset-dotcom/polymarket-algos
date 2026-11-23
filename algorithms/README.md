# Algorithms (research set)

## What is in git right now

| File | Status |
|------|--------|
| `comeback.py` | **Wrapper** → full code in [`../comeback/comeback.py`](../comeback/comeback.py) |
| `rozi.py` | **Wrapper** → full code in [`../rozi/rozi.py`](../rozi/rozi.py) |
| `preladder.py` | **Wrapper** → full code in [`../preladder/preladder.py`](../preladder/preladder.py) |
| `crossreb.py` | Loader stub — needs full `crossreb.py` from **algorithms_src.zip** |
| `ineff.py` | **Missing** — add from **algorithms_src.zip** |
| `doppler.py` | **Missing** — add from **algorithms_src.zip** |
| `*.env.example` | Present for crossreb / ineff / rozi / comeback |

**Root folders** (`gozi`, `flash`, `comeback`, `rozi`, `boxarb`, …): each already has its own `*.py` (not env-only).

### One-shot: drop full research sources into this folder

```bash
# put algorithms_src.zip in repo root, then:
unzip -o algorithms_src.zip -d algorithms/
```

That unpacks full `crossreb.py`, `ineff.py`, `doppler.py`, `comeback.py`, `rozi.py`, `preladder.py` + env examples.

### Run

```bash
cd algorithms
cp ../comeback/comeback.env.example comeback.env && python3 comeback.py
cp crossreb.env.example crossreb.env && python3 crossreb.py
```

Use `PAPER=true`. Datasets: `../datasets/`.
