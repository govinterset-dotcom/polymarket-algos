# Polymarket Up/Down Algorithms

Three live trading algorithms for **BTC Up/Down** markets on [Polymarket](https://polymarket.com):

| Algorithm | TF | Idea | Frequency (history) | WR (history) |
|-----------|-----|------|---------------------|--------------|
| **Comeback** | 5m / 15m | Late comeback of a near-loser side | ~41 entries/day (5m) | ~97% |
| **Premove** | 5m → next 5m | Book jump at end of window → enter next window same side | ~7–8 signals/day | ~64–65% |
| **Rozi** | 15m | Catch sharp mid drops by tiers + streak filter | ~1.4 signals/day | ~30% |

All three act as **takers** (FAK) via Polymarket CLOB. Default `PAPER=true` — no live orders.

> **Disclaimer.** Research code. Past results do not guarantee future performance. Live execution (latency, slippage, regime change) can differ a lot from replay. Use at your own risk.

---

## Quick start

### 1. Requirements

- Python 3.10+
- `requests`, `websockets`
- Live mode: `py_clob_client_v2`

```bash
pip install requests websockets
# live:
pip install py_clob_client_v2
```

### 2. Polymarket keys (only for `PAPER=false`)

1. Wallet with USDC on Polygon (chain id 137).
2. Private key → `PK=0x...`
3. Optional CLOB API keys — leave empty and the client will create/derive them.
4. If you use Polymarket Proxy / Gnosis Safe — set `FUNDER` and `SIGNATURE_TYPE` (1 or 2).

Docs: [CLOB](https://docs.polymarket.com) · [py-clob-client](https://github.com/Polymarket/py-clob-client)

### 3. Run

```bash
git clone https://github.com/govinterset-dotcom/polymarket-algos.git
cd polymarket-algos

cp comeback/comeback.env.example comeback/comeback.env
cd comeback && python comeback.py
```

Same for `premove/` and `rozi/`. Config is loaded from `<name>.env` next to the script (or `ENV_FILE=...`).

---

## Comeback

**Idea:** side already traded ≤ `MIN_LOW` in the window, then its ask **first** enters `[ENTRY_LO, ENTRY_HI]` after `FRAC_MIN` of the window → buy as taker.

### Performance card (history)

**Comeback, BTC 5m, entry from second 260, price 0.85–0.99**

**Setup:** 5940 five-minute BTC windows over 24 days (13–29 Oct + 2–8 Nov), second-by-second book. Resolution by book/gamma. Taker fee `0.07·p·(1−p)`. Entry at signal ask, no queue/slippage modeled.

| Metric | Value |
|--------|-------|
| Entries | 994 (≈41/day), 291 windows |
| Win rate | 96.9% (95–98%), 31 losses |
| Result per $1 turnover | +2.8¢ (+1.1…+4.3) |
| Win per $1 stake | +$0.063 |
| Loss per $1 stake | −$1.005 |
| Wins per loss | ~31 (breakeven ~16) |
| Avg entry price | 0.94 |

**Fixed stake ($ over 24 days):**

| Stake | PnL | Max DD | Worst day |
|-------|-----|--------|-----------|
| $5 | +$146 | −$45 | −$17 |
| $10 | +$293 | −$89 | −$33 |
| $25 | +$732 | −$223 | −$83 |

In stake units: ~+29, DD 8.9, worst day −3.3. Losing days 6/24. Max loss streak 5, win streak 167.

**By period:** Oct 17d, 42/day, WR 97.9%, +3.9% on turnover; Nov 7d, 40/day, WR 94.2%, ~0%.

**Expect:** ~97 wins per 100 entries. Small edge → one loss wipes ~16 wins. Realistic range 0…+3% on turnover.

**Caveats:** 260s threshold and price band chosen after seeing this data; live ~250ms latency may worsen fill price.

### Key params (`comeback.env`)

| Param | Default | Meaning |
|-------|---------|--------|
| `MIN_LOW` | 0.10 | Side was ≤ this mid |
| `ENTRY_LO` / `ENTRY_HI` | 0.85 / 0.88 | Ask band |
| `FRAC_MIN` | 0.80 | Not earlier than this fraction |
| `STAKE_PCT` | 4.0 | % of deposit per trade |
| `CAP_PX` | 0.90 | Hard taker price cap |
| `TFS` | 5,15 | Timeframes |
| `SIGNAL_1S` | false | true = once per second |
| `SHADOW` | true | Parallel mode journal-only |

See `comeback/comeback.env.example`.

```bash
cd comeback && cp comeback.env.example comeback.env && python comeback.py
```

Journal: `comeback.jsonl`.

---

## Premove (`premove_live`)

**Idea:** in last `JUMP_LAST_S` seconds of a 5m window, Up mid jumps `|Δmid| ≥ JUMP_MIN` over k∈{3,5,10}s → immediately buy **same side** on the **next** 5m market on the pre-open book.

### Performance card (history)

**Premove, BTC 5m, working config (core + chase-15m-extreme filter)**

**Setup:** 24 days, 5940 five-minutes. Book/gamma resolution, fee included. Entry price from real pre-start asks (~0.56 journal, live 0.48–0.61).

| Variant | Signals/day | WR (interval) | Oct / Nov |
|---------|-------------|---------------|-----------|
| Core no filters | 9.5 | 62.9% (56–69%) | 61.1% / 67.7% |
| **Working: + chase filter** | **7.6** | **64.5% (57–71%)** | **62.5% / 70.2%** |
| Full QF | 4.8 | 64.9% | 63.1% / 70.0% |
| Working + Down only | 4.0 | 70.5% | 71.8% / 66.7% |
| Working + jump in 3s | 2.8 | 72.1% | 73.6% / 66.7% |

**Working summary:** 183 signals, 118/65, WR 64.5%, max streaks 12W / 5L. Breakeven entry ≈0.63 (at WR 57% ≈0.55).

**Money ($10/trade, 24 days, after fee):**

| Entry px | On turnover | PnL | Max DD | Worst day | Losing days |
|----------|-------------|-----|--------|-----------|-------------|
| 0.50 | +25.5% | +$466 | −$52 | −$12 | 5/23 |
| **0.55** | **+14.1%** | **+$258** | **−$52** | **−$15** | **6/23** |
| 0.60 | +4.7% | +$85 | −$54 | −$20 | 9/23 |
| 0.65 | −3.2% | −$60 | −$111 | −$26 | 13/23 |

**Expect:** 7–8 trades/day, ~2 of 3 win. Keep `CAP_PX` around 0.58–0.60.

**Caveats:** WR interval 57–71%; chase filter tuned on same data; entry price assumption 0.55.

### Key params (`premove_live.env`)

| Param | Default | Meaning |
|-------|---------|--------|
| `JUMP_MIN` | 0.40 | Min mid jump |
| `JUMP_LAST_S` | 25 | Search in last N seconds |
| `JUMP_KS` | 3,5,10 | Comparison windows |
| `CAP_PX` | 0.80 | Price cap (consider 0.58–0.60) |
| `MAX_SLIP` | 0.03 | min(CAP, ask+slip) |
| `STAKE_PCT` | 2.0 | % of deposit |
| `SIDES` | Up,Down | Allowed sides |
| `ENTRY_AFTER_START_S` | 3.0 | Try after target start |

```bash
cd premove && cp premove_live.env.example premove_live.env && python premove_live.py
```

Journal: `premove_live.jsonl`.

---

## Rozi

**Idea:** per side on 15m markets — tiers `level:ref:cap:size`. First touch of mid ≤ level, if mid was ≥ ref over `FAST_S` and enough time left → taker (or trap limit). Optional Binance 5m streak filter.

### Performance card (history)

**Rozi 15m · BTC — card config**

**Config:** BTC 15m, taker, tiers 0.15 / 0.10, streak ≥2 on 5m or 15m (OR), extreme/amp off.

**Run:** 7 Oct – 11 Nov 2026 (~35 days), replay on real books. ~50 signals (~1.4/day). WR ~30%. Avg result ~+$10 per $10 stake. Sum over period at $10/signal ~+$500. Max loss streak 10. DD at $10 ~−$100.

- Tier 0.15: rarer, higher WR (~45%)
- Tier 0.10: more often, lower WR (~18%), still positive payoff

**Expect:** 1–2 entries/day. Wins < half by design (cheap entry). Streaks of 5–10 losses are normal. BTC only.

**One-liner:** ~30% WR, ~+$10 per $10 risked, ~1.4 entries/day, DD ~10 stakes.

Replay ≠ live: slippage, fees, regime can hurt numbers.

### Key params (`rozi.env`)

| Param | Example | Meaning |
|-------|---------|--------|
| `TIERS` | `0.15:0.45:0.17:3,0.10:0.40:0.12:3` | level:ref:cap:size |
| `FAST_S` | 60 | Fast-drop window |
| `REM_MIN_FRAC` | 0.33 | Min remaining fraction |
| `STREAK_FILTER` | on | 5m streak filter |
| `STREAK_5M_MIN` | 2 | Min consecutive candles |
| `ENTRY_MODE` | taker | taker or trap |
| `STAKE_PCT` | 0 | 0 = $ from TIERS; >0 = % deposit |
| `ADD_USDC` | 0 | Add-on confirmation (0=off) |

```bash
cd rozi && cp rozi.env.example rozi.env && python rozi.py
```

Journal: `rozi.jsonl`, path log: `rozi_path.jsonl`.

---

## Notes

- **Paper vs live:** `PAPER=true` simulates fills at ask; `PAPER=false` needs `PK`.
- **Compounding:** `COMPOUND=true` → deposit = `DEPOSIT` + journal PnL.
- **Resolution:** `RESOLVE_BY_BOOK=true` uses last `BOOK_HOLD_S` of winner bid ≥ `BOOK_WIN_PX`, gamma verifies in background.
- **Never commit real `.env`** — only `*.env.example` are in the repo.
- Use a dedicated trading wallet with limited balance.

## Structure

```
polymarket-algos/
├── README.md
├── requirements.txt
├── .gitignore
├── comeback/
│   ├── comeback.py
│   └── comeback.env.example
├── premove/
│   ├── premove_live.py
│   └── premove_live.env.example
└── rozi/
    ├── rozi.py
    └── rozi.env.example
```

## License / disclaimer

Provided as-is. Author is not responsible for financial losses. Prediction-market trading can result in total loss of capital. Historical cards above are not a promise of profit.
