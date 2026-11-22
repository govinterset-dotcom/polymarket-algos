#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Поиск сигнала для АНДЕРДОГОВ по голым котировкам.

Андердог определяется БЕЗ стакана: состояние, когда цена ушла от страйка
далеко относительно того, сколько успеет пройти за оставшееся время,
    d = |S - K| / (sigma_1m * sqrt(минут осталось)).
Связь d с ценой стакана измерена отдельно: корреляция 0.985, то есть d
это и есть цена. Поэтому отбирать случаи можно на всей истории свечей,
а стаканы нужны один раз в конце — чтобы проверить, не сидит ли найденное
уже в цене (это делает udogbook.py).

Запуск:  python3 udogq.py ~/bybit_stats btc,eth 1.5
"""
from __future__ import annotations

# ----------------------------------------------------------------------------- builder (attribution)
BUILDER_ADDRESS = "0xd61e22734b257afc0710f6b530a0abc244eac34f"
BUILDER_CODE = "0xcd125f8d13ec24d28c7f5f0d7f1809dcf1ce83d3125a17738dbae34508117ede"

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import pmudog as P

VOL_SHORT = 30
VOL_LONG = 120


def build(klines: Path, coins: list[str], dmin: float = 1.5,
          train_frac: float = 0.7) -> tuple[pd.DataFrame, dict]:
    frames = []
    for coin in coins:
        d1 = P.load_klines(klines, coin)
        ts = d1.ts.to_numpy()
        c = d1.c.to_numpy(float)
        o = d1.o.to_numpy(float)
        lr = np.concatenate([[np.nan], np.diff(np.log(c))])
        vol = pd.Series(lr).rolling(VOL_SHORT).std().to_numpy()
        vol_long = pd.Series(lr).rolling(VOL_LONG).std().to_numpy()
        sf = {t: P.slot_frame(d1, t).set_index("start") for t in (5, 15)}
        for tf in (15, 5):
            W = tf * 60
            base = ts - (ts % W)
            uniq, inv = np.unique(base, return_inverse=True)
            first = np.full(len(uniq), len(ts), int)
            np.minimum.at(first, inv, np.arange(len(ts)))
            last = np.zeros(len(uniq), int)
            np.maximum.at(last, inv, np.arange(len(ts)))
            full = np.bincount(inv, minlength=len(uniq)) == tf
            K = o[first]
            up_won = c[last] > K
            m_idx = (ts - base) // 60
            keep = (full[inv] & (m_idx >= 1) & (m_idx <= tf - 1)
                    & np.isfinite(vol) & (vol > 0) & np.isfinite(vol_long)
                    & (vol_long > 0))
            idx = np.flatnonzero(keep)
            if not len(idx):
                continue
            S = c[idx]
            Km = K[inv[idx]]
            left = (tf - m_idx[idx]).astype(float)
            with np.errstate(divide="ignore", invalid="ignore"):
                d = ((S - Km) / S) / (vol[idx] * np.sqrt(left))
            behind_up = S < Km
            won = np.where(behind_up, up_won[inv[idx]], ~up_won[inv[idx]])
            ws = base[idx]
            exc = np.abs((c - K[inv]) / K[inv])
            run_max = pd.Series(exc).groupby(inv).cummax().to_numpy()[idx]
            now = np.abs((S - Km) / Km)
            with np.errstate(divide="ignore", invalid="ignore"):
                otyg = np.where(run_max > 1e-12, (run_max - now) / run_max, 0.0)
            rec = {"coin": coin, "tf": tf, "ws": ws, "мин": m_idx[idx],
                   "доля_окна": m_idx[idx] / tf, "d": np.abs(d),
                   "выиграл": won.astype(int), "сигма_бп": vol[idx] * 1e4,
                   "вол_к_фону": vol[idx] / vol_long[idx],
                   "разрыв_бп": now * 1e4, "отыграно": np.clip(otyg, 0, 1),
                   "мин_осталось": left, "сторона_up": behind_up.astype(int)}
            for tag, step in (("5", 300), ("15", 900)):
                pv = (ws // step) * step - step
                sl = sf[int(tag)]
                rec[f"серия{tag}"] = np.abs(sl["st"].reindex(pv).to_numpy())
                rec[f"импульс{tag}"] = sl["kx"].reindex(pv).to_numpy()
                dr = sl["dir"].reindex(pv).to_numpy()
                rec[f"по_сигналу{tag}"] = np.where(
                    behind_up, dr < 0, dr > 0).astype(float)
            frames.append(pd.DataFrame(rec))
    D = pd.concat(frames, ignore_index=True)
    D = D[np.isfinite(D.d) & (D.d >= dmin) & (D.d < 20)].reset_index(drop=True)
    D["день"] = pd.to_datetime(D.ws, unit="s").dt.date
    days = np.sort(D.день.unique())
    cut = days[int(len(days) * train_frac)]
    tr = D.день < cut
    FEATS = ["доля_окна", "сигма_бп", "импульс15", "вол_к_фону", "отыграно",
             "импульс5", "разрыв_бп", "по_сигналу15"]
    FEATS = [c for c in FEATS if c in D]
    Z = D[FEATS].rank(pct=True)
    signs = {}
    for c in FEATS:
        a = Z.loc[tr, c].fillna(0.5)
        y = D.loc[tr, "выиграл"]
        signs[c] = float(np.sign(np.corrcoef(a, y)[0, 1]))
    D["скор"] = sum(signs[c] * Z[c].fillna(0.5) for c in FEATS)
    return D, {"cut": cut, "signs": signs, "feats": FEATS}


def _lift_table(D, meta):
    cut = meta["cut"]
    TR, TE = D[D.день < cut], D[D.день >= cut]
    print(f"\n{'='*92}\nПОДЪЁМ НАД БАЗОЙ ПО КАЖДОМУ ФАКТОРУ (внутри корзин d)")
    rows = []
    cand = meta["feats"] + [c for c in ("мин_осталось", "серия5", "серия15",
                                        "по_сигналу5") if c in D]
    for c in cand:
        out = {"фактор": c}
        for name, X in (("обуч", TR), ("тест", TE)):
            hw = hn = lw = ln = 0
            for _, g in X.groupby(pd.cut(X.d, [1.5, 2, 2.5, 3, 4, 20]), observed=True):
                v = g[c].dropna()
                if len(v) < 200:
                    continue
                q1, q3 = v.quantile(1 / 3), v.quantile(2 / 3)
                top = g[g[c] >= q3] if q3 > q1 else g[g[c] > q1]
                bot = g[g[c] <= q1]
                hw += int(top.выиграл.sum()); hn += len(top)
                lw += int(bot.выиграл.sum()); ln += len(bot)
            if hn < 300 or ln < 300:
                continue
            ph, pl = hw / hn, lw / ln
            se = math.sqrt(ph * (1 - ph) / hn + pl * (1 - pl) / ln)
            out[f"разница_{name}"] = round(ph - pl, 4)
            out[f"t_{name}"] = round((ph - pl) / se, 2) if se > 0 else 0.0
        if "разница_обуч" in out and "разница_тест" in out:
            rows.append(out)
    if rows:
        T = pd.DataFrame(rows)
        print(T.reindex(T.разница_обуч.abs().sort_values(ascending=False).index).to_string(index=False))


def main():
    kl = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path("./klines")
    coins = (sys.argv[2] if len(sys.argv) > 2 else "btc,eth").split(",")
    dmin = float(sys.argv[3]) if len(sys.argv) > 3 else 1.5
    D, meta = build(kl, coins, dmin)
    n = len(D)
    print(f"состояний андердога (d >= {dmin}): {n:,}".replace(",", " "))
    print(f"окон {D.ws.nunique():,}".replace(",", " ")
          + f" | период {D.день.min()} … {D.день.max()}"
          + f" | разворотов {int(D.выиграл.sum()):,}".replace(",", " "))
    print(f"раскол: обучение до {meta['cut']}, тест после")
    bins = [1.5, 1.75, 2, 2.25, 2.5, 3, 3.5, 4, 5, 20]
    print(f"\n{'='*92}\nБАЗА: как часто отстающая сторона отыгрывает, по d")
    g = D.groupby(pd.cut(D.d, bins), observed=True).agg(
        n=("выиграл", "size"), d=("d", "mean"), разворот=("выиграл", "mean"))
    print(g.round(4).to_string())
    _lift_table(D, meta)
    print(f"\n{'='*92}\nКОМБИНАЦИЯ: частота разворота по децилям скора")
    print("знаки факторов (по обучению):",
          {k: int(v) for k, v in meta["signs"].items()})
    base = D.groupby(pd.cut(D.d, bins), observed=True).выиграл.mean()
    for name, X in (("ОБУЧЕНИЕ", D[D.день < meta["cut"]]),
                    ("ТЕСТ", D[D.день >= meta["cut"]])):
        X = X.copy()
        X["дец"] = pd.qcut(X.скор, 10, labels=False, duplicates="drop")
        X["_b"] = base.reindex(pd.cut(X.d, bins)).to_numpy()
        t = X.groupby("дец", observed=True).agg(
            n=("выиграл", "size"), d=("d", "mean"),
            разворот=("выиграл", "mean"), база=("_b", "mean"))
        t["подъём"] = t.разворот - t.база
        t["±90%"] = 1.2816 * np.sqrt(t.разворот * (1 - t.разворот) / t.n)
        print(f"\n--- {name}")
        print(t.round(4).to_string())
    print("\n  ВАЖНО: 'подъём' считается над МОЕЙ формулой, а не над ценой "
          "рынка.\n  Рынок знает больше формулы — проверь через udogbook.py.")


if __name__ == "__main__":
    main()
