#!/usr/bin/env python3
"""
Is there any edge?  Measures it from what bot.py recorded in data/.

    python3 edge_check.py            (run bot.py first; a day of data is a fair start)

Three questions, each answered from the per-second log rather than from the
handful of trades the bot happened to take:

  1. How well does our rebuilt index track the real one?            (basis.csv)
  2. When the model disagrees with the market, who is right?        (ticks.csv)
  3. Replaying "buy near 50/50, sell at the first profit" at EVERY
     recorded second: does picking the side with the model beat
     picking it blindly, and does either make money after fees?     (ticks.csv + windows.csv)
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import statistics
from collections import defaultdict

FEE = 0.07


def load(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def fee_c(price_c: float) -> float:
    return FEE * price_c * (100 - price_c) / 100          # cents per contract


def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def replica_quality(rows: list[dict]):
    print("1. HOW CLOSE IS OUR REBUILT INDEX TO THE REAL ONE?")
    if len(rows) < 3:
        print(f"   only {len(rows)} window opens recorded; need a few more.\n")
        return
    print(f"   At each window open the real index value is known (it is the strike). Over {len(rows)} opens:")
    print("   source        typical gap     wobble (std dev)   <- wobble is what hurts; a steady gap is corrected for")
    for col in [c for c in rows[0] if c not in ("ticker", "strike")]:
        gaps = [float(r["strike"]) - float(r[col]) for r in rows if r.get(col)]
        if len(gaps) >= 3:
            print(f"   {col:<12} {mean(gaps):>+9.2f}      {statistics.pstdev(gaps):>9.2f}       ({len(gaps)} opens)")
    print()


def by_window(ticks: list[dict]) -> dict[str, list[dict]]:
    out = defaultdict(list)
    for r in ticks:
        try:
            row = {"ts": float(r["ts"]), "left": float(r["secs_left"]), "model": float(r["model_up"]),
                   "bid": float(r["up_bid"]), "ask": float(r["up_ask"])}
        except (KeyError, ValueError):
            continue
        row["mid"] = (row["bid"] + row["ask"]) / 2
        out[r["ticker"]].append(row)
    for rows in out.values():
        rows.sort(key=lambda x: x["ts"])
    return out


def who_is_right(windows: dict[str, list[dict]], results: dict[str, str]):
    print("2. WHEN THE MODEL DISAGREES WITH THE MARKET, WHO IS RIGHT?")
    for horizon in (10, 30, 60):
        pairs = []                                     # (model minus market now, market's move after)
        for rows in windows.values():
            j, last_used = 0, -1e18
            for i, r in enumerate(rows):
                if not 15 <= r["mid"] <= 85 or r["left"] < horizon + 20 or r["ts"] - last_used < horizon:
                    continue
                while j < len(rows) and rows[j]["ts"] < r["ts"] + horizon:
                    j += 1
                if j < len(rows) and rows[j]["ts"] - r["ts"] <= horizon + 5:
                    pairs.append((r["model"] - r["mid"], rows[j]["mid"] - r["mid"]))
                    last_used = r["ts"]
        if len(pairs) < 30:
            print(f"   next {horizon:>2}s: not enough data yet ({len(pairs)} samples)")
            continue
        sxx = sum(g * g for g, _ in pairs)
        slope = sum(g * m for g, m in pairs) / sxx if sxx else 0.0
        resid = [m - slope * g for g, m in pairs]
        se = math.sqrt(sum(e * e for e in resid) / (len(pairs) - 1) / sxx) if sxx else float("nan")
        big = [m * (1 if g > 0 else -1) for g, m in pairs if abs(g) >= 3]
        print(f"   next {horizon:>2}s: for each 1c the model says UP is too cheap, the market then moves "
              f"{slope:+.3f}c toward the model (+/-{2 * se:.3f}, {len(pairs)} samples)"
              + (f"; when they differ by 3c+, it moves {mean(big):+.2f}c toward the model ({len(big)} cases)"
                 if len(big) >= 10 else ""))
    print("   Reading it: clearly above zero = the model sees moves before the market does (an edge exists,")
    print("   though it must exceed ~2.3c per contract of fee + spread to be worth trading). Around zero or")
    print("   below = the market already knows what the model knows.")

    sq_model, sq_mkt = [], []
    for ticker, rows in windows.items():
        if results.get(ticker) not in ("yes", "no"):
            continue
        outcome = 100.0 if results[ticker] == "yes" else 0.0
        for r in rows[::30]:
            if r["left"] >= 540:                       # the entry zone: first six minutes
                sq_model.append((r["model"] - outcome) ** 2)
                sq_mkt.append((r["mid"] - outcome) ** 2)
    n_win = len([t for t in windows if results.get(t) in ("yes", "no")])
    if sq_model:
        print(f"   Forecast error in the first 6 minutes over {n_win} resolved windows (lower is better): "
              f"market {math.sqrt(mean(sq_mkt)):.1f}, model {math.sqrt(mean(sq_model)):.1f}"
              + ("   (too few windows to mean much yet)" if n_win < 50 else ""))
    print()


def replay(windows: dict[str, list[dict]], results: dict[str, str], band: float, entry_secs: float,
           min_edge: float, min_profit: float):
    print("3. REPLAYING THE STRATEGY AT EVERY RECORDED SECOND")
    print(f"   Buy at the offer when within {band:g}c of 50 in the first {entry_secs / 60:g} min; rest a sell at the")
    print("   first price that is a profit after the fee; otherwise settle at Kalshi's result.")
    groups = {"model's pick": [], "the other side": [], "either side (blind)": []}
    used = set()
    for ticker, rows in windows.items():
        result = results.get(ticker)
        if result not in ("yes", "no"):
            continue
        n = len(rows)
        best_bid_after = {"yes": [0.0] * (n + 1), "no": [0.0] * (n + 1)}
        for i in range(n - 1, -1, -1):
            best_bid_after["yes"][i] = max(best_bid_after["yes"][i + 1], rows[i]["bid"])
            best_bid_after["no"][i] = max(best_bid_after["no"][i + 1], 100 - rows[i]["ask"])
        for i, r in enumerate(rows):
            if r["left"] < 900 - entry_secs:
                continue
            for side in ("yes", "no"):
                ask = r["ask"] if side == "yes" else 100 - r["bid"]
                fair = r["model"] if side == "yes" else 100 - r["model"]
                if abs(ask - 50) > band:
                    continue
                cost = ask + fee_c(ask)
                target = math.ceil(cost + min_profit - 1e-9)
                if best_bid_after[side][i + 1] >= target:
                    pnl, won = target - cost, True
                else:
                    pnl, won = (100.0 if result == side else 0.0) - cost, False
                edge = fair - cost
                other_ask = 100 - r["bid"] if side == "yes" else r["ask"]
                other_edge = (100 - fair) - other_ask - fee_c(other_ask)
                rec = (pnl, won, cost)
                groups["either side (blind)"].append(rec)
                if edge >= min_edge and edge >= other_edge:
                    groups["model's pick"].append(rec)
                    used.add(ticker)
                elif other_edge >= min_edge:
                    groups["the other side"].append(rec)
    if not groups["either side (blind)"]:
        print("   no resolved windows with entry-zone data yet.\n")
        return
    print(f"   {'':<22}{'entries':>9}{'sold at profit':>16}{'avg P&L/contract':>19}{'per $100 staked':>17}")
    for name, recs in groups.items():
        if not recs:
            print(f"   {name:<22}{0:>9}")
            continue
        pnl, cost = mean([p for p, _, _ in recs]), mean([c for _, _, c in recs])
        print(f"   {name:<22}{len(recs):>9}{mean([w for _, w, _ in recs]) * 100:>15.1f}%"
              f"{pnl:>+18.2f}c{pnl / cost * 100:>+16.2f}")
    print(f"   (entries overlap heavily: they come from {len(used)} windows where the model made a pick, so treat")
    print("   this as roughly that many independent trials. Under ~100 windows it is noise.)")
    print("   Reading it: an edge means \"model's pick\" is clearly better than \"either side (blind)\" AND above zero.")
    print()


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=os.path.join(here, "data"))
    ap.add_argument("--band", type=float, default=8.0)
    ap.add_argument("--entry-window", type=float, default=6.0, help="minutes")
    ap.add_argument("--min-edge", type=float, default=1.0)
    ap.add_argument("--min-profit", type=float, default=0.5)
    a = ap.parse_args()
    ticks = load(os.path.join(a.data_dir, "ticks.csv"))
    results = {r["ticker"]: r["result"] for r in load(os.path.join(a.data_dir, "windows.csv"))}
    windows = by_window(ticks)
    print(f"{len(ticks):,} seconds recorded across {len(windows)} windows, {len(results)} resolved.\n")
    replica_quality(load(os.path.join(a.data_dir, "basis.csv")))
    if not ticks:
        print("No tick data yet: run bot.py for a while first.")
        return
    who_is_right(windows, results)
    replay(windows, results, a.band, a.entry_window * 60, a.min_edge, a.min_profit)


if __name__ == "__main__":
    main()
