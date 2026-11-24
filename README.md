# Polymarket Algorithms

Open research workspace for **Polymarket** short-horizon markets (BTC/ETH up-down 5m / 15m): live-style Python bots, fee-aware backtests, and **historical datasets** (order-book paths + Binance seconds).

The goal is simple: anyone — researchers, traders, or **AI agents** — can clone the repo, load the data, and test or invent strategies without reinventing market plumbing.

> Paper / historical results are **not** guarantees of live performance. Fees (taker + optional builder) matter.

---

## Layout

```
polymarket-algos/
  algorithms/          # strategy code + env examples (research set)
  datasets/            # historical data + schema docs (updated over time)
  <legacy folders>/    # older one-folder-per-bot layout still present at repo root
  README.md
  LICENSE
```

| Path | Purpose |
|------|---------|
| **[algorithms/](algorithms/)** | Runnable bots used in recent research (comeback, rozi, ineff, crossreb, …) |
| **[datasets/](datasets/)** | Path JSONL (per-second book) and BTC seconds parquet; **data will grow and improve** |
| Root strategy folders | Historical collection (gozi, flash, boxarb, …) — same license |

**Datasets coverage (path):** unique markets (windows) **~12 600** — of which **BTC 5m ≈ 7 000**, **BTC 15m ≈ 3 200** (plus ETH/SOL/XRP in some dumps). See [datasets/](datasets/).

---

## For humans and AI agents

You can use this repo to:

1. **Backtest** a rule on real 5m/15m windows (winner + bid/ask path).
2. **Join** book paths with Binance second bars for spot–book edge studies.
3. **Compare** strategies under the same fee model (taker ≈ `0.07 × p × (1−p)` per share, optional builder % on notional).
4. **Iterate with an AI agent**: point the agent at `datasets/README.md` + a bot folder, ask for a grid search, fold stability, or a new signal class.

Suggested agent prompt skeleton:

```text
Load datasets/README.md and algorithms/<name>.
Replay BTC 5m/15m path JSONL with fee+builder.
Report n, WR, ROI, max loss streak, and 3 time folds.
Do not claim live profitability from paper alone.
```

---

## Quick start

```bash
git clone https://github.com/govinterset-dotcom/polymarket-algos.git
cd polymarket-algos
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt  # requests websockets py-clob-client pyarrow pandas ...

# Paper-run a research bot
cd algorithms
cp comeback.env.example comeback.env
# edit PAPER=true
python comeback.py
```

Datasets: see **[datasets/README.md](datasets/README.md)**. Large binaries may ship via **Git LFS** or **Release assets** as the archive grows.

---

## Builder attribution

Live CLOB paths may embed builder codes in source. Private keys and funders are **never** committed — only `*.env.example`.

---

## License

Polymarket Builder Attribution Source-Available License 1.0 — see [LICENSE](LICENSE).
