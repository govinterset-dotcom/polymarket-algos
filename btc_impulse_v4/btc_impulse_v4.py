"""
BTC Impulse v4 — максимально быстро через pandas groupby на чанке
"""

# ----------------------------------------------------------------------------- builder (attribution)
BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"

import pandas as pd
import numpy as np
from pathlib import Path

AGG_FILES   = ["BTCUSDT-aggTrades-2026-02.csv", "BTCUSDT-aggTrades-2026-03.csv"]
CANDLE_FILE = "BTCUSDT_5m_90d.csv"
SEC_PARQUET = "btc_seconds.parquet"
CHUNK       = 5_000_000
WINDOWS     = [10, 20, 30]
MIN_AMP     = 5.0

def load_or_build_seconds() -> pd.DataFrame:
    p = Path(SEC_PARQUET)
    if p.exists():
        return pd.read_parquet(p)
    frames = []
    for f in AGG_FILES:
        if not Path(f).exists():
            continue
        for chunk in pd.read_csv(f, chunksize=CHUNK):
            chunk["T"] = pd.to_datetime(chunk["T"], unit="ms", utc=True)
            chunk = chunk.set_index("T").sort_index()
            sec = chunk["p"].astype(float).resample("1s").ohlc()
            frames.append(sec)
    if not frames:
        raise SystemExit("no agg trade files found")
    out = pd.concat(frames).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    out.to_parquet(SEC_PARQUET)
    return out

def detect_impulses(sec: pd.DataFrame) -> pd.DataFrame:
    rows = []
    px = sec["close"].astype(float)
    for w in WINDOWS:
        roll_max = px.rolling(w, min_periods=w).max()
        roll_min = px.rolling(w, min_periods=w).min()
        amp = (roll_max - roll_min) / roll_min * 1e4  # bp
        mask = amp >= MIN_AMP
        if not mask.any():
            continue
        idx = px.index[mask]
        for t in idx[::max(1, w // 2)]:
            rows.append({"ts": t, "window_s": w, "amp_bp": float(amp.loc[t]),
                         "price": float(px.loc[t])})
    return pd.DataFrame(rows)

def main():
    sec = load_or_build_seconds()
    imp = detect_impulses(sec)
    print(f"seconds={len(sec):,} impulses={len(imp):,}")
    if len(imp):
        print(imp.head(20).to_string(index=False))
    print("builder", BUILDER_ADDRESS[:12])

if __name__ == "__main__":
    main()
