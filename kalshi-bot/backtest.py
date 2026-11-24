#!/usr/bin/env python3
"""
Quick backtest of "buy the 20-40c side early, sell at the first profit" on
Kalshi's own history for the 15-minute crypto windows.

    python3 backtest.py --days 3          (downloads once into data/backtest/, then reuses it)

What it replays, one trade per window:
  * at the end of each of the first N minutes, if a side is offered inside the
    price range, buy it at that offer and pay the taker fee;
  * a sell order rests at the first whole cent that is a profit after the fee;
    it fills if a LATER minute's best bid reaches it;
  * otherwise the contract is paid $1 or $0 from Kalshi's official result.

It is run three ways: buying blindly whenever a side is in range, buying only
when the model likes that side, and buying only when the model does NOT.

Limits (why this is "quick", not exact): prices are 1-minute bars, so entries
happen at minute ends and a touch inside the entry minute is ignored; the model
here sees only Coinbase 1-minute closes, not the bot's live five-exchange blend
or its learned evidence; and it assumes the quoted size was enough for the stake.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import time
from datetime import datetime, timezone

from bot import ASSET, COINBASE_API, KALSHI_API, FeedError, http_get, norm_cdf, parse_ts, _num

FEE = 0.07


def fee_c(price_c: float) -> float:
    return FEE * price_c * (100 - price_c) / 100


def cached(path: str, fetch):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    data = fetch()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)
    return data


def polite_get(url: str, params: dict | None = None, pause: float = 0.5):
    """GET with patience: Kalshi rate-limits history calls."""
    for attempt in range(6):
        try:
            out = http_get(url, params, timeout=15, retries=0)
            time.sleep(pause)
            return out
        except FeedError as e:
            if "429" not in str(e) and attempt >= 2:
                raise
            time.sleep(3 * (attempt + 1))
    raise FeedError(f"gave up on {url}")


# ---- data ------------------------------------------------------------------- #

def settled_markets(series: str, since: float, until: float) -> list[dict]:
    out, cursor = [], ""
    while True:
        params = {"series_ticker": series, "status": "settled", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        page = polite_get(f"{KALSHI_API}/markets", params)
        rows = page.get("markets") or []
        for m in rows:
            o, c = parse_ts(m.get("open_time")), parse_ts(m.get("close_time"))
            if o and c and since <= c <= until and m.get("result") in ("yes", "no"):
                out.append({"ticker": m["ticker"], "open": o, "close": c,
                            "strike": _num(m.get("floor_strike")), "result": m["result"]})
        cursor = page.get("cursor") or ""
        oldest = min((parse_ts(m.get("close_time")) or until for m in rows), default=0)
        if not cursor or not rows or oldest < since:
            break
    return sorted(out, key=lambda m: m["open"])


def cents(v) -> float | None:
    """A price from Kalshi history, in cents, whether sent as "0.4300" dollars or 43."""
    x = _num(v)
    if x is None:
        return None
    return x * 100 if isinstance(v, str) or x <= 1.0 else x


def ohlc(obj: dict | None, field: str) -> float | None:
    if not obj:
        return None
    for key in (field + "_dollars", field):
        if obj.get(key) is not None:
            return cents(obj[key]) if key.endswith("_dollars") else cents(obj[key])
    return None


def market_bars(series: str, m: dict) -> list[dict]:
    """One bar per minute of the window: closing bid/ask for UP and the minute's bid/ask extremes."""
    data = polite_get(f"{KALSHI_API}/series/{series}/markets/{m['ticker']}/candlesticks",
                      {"start_ts": int(m["open"]), "end_ts": int(m["close"]), "period_interval": 1})
    bars = []
    for c in data.get("candlesticks") or []:
        bid, ask = c.get("yes_bid"), c.get("yes_ask")
        bar = {"end": c.get("end_period_ts"),
               "bid": ohlc(bid, "close"), "ask": ohlc(ask, "close"),
               "bid_high": ohlc(bid, "high"), "ask_low": ohlc(ask, "low")}
        if None not in bar.values():
            bars.append(bar)
    return sorted(bars, key=lambda b: b["end"])


def spot_minutes(asset: str, since: float, until: float, cache_dir: str) -> dict[int, tuple[float, float]]:
    """Coinbase 1-minute candles: minute start -> (open, close)."""
    out = {}
    t = int(since // 18000 * 18000)
    while t < until:
        def fetch(t=t):
            iso = lambda x: datetime.fromtimestamp(x, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return polite_get(f"{COINBASE_API}/products/{asset}-USD/candles",
                              {"granularity": 60, "start": iso(t), "end": iso(t + 18000 - 60)}, pause=0.25)
        path = os.path.join(cache_dir, f"spot_{asset}_{t}.json")
        if t + 18000 > time.time():                      # the chunk still in progress is not cached
            rows = fetch()
        else:
            rows = cached(path, fetch)
        for r in rows:
            out[int(r[0])] = (float(r[3]), float(r[4]))
        t += 18000
    return out


# ---- model (the bot's fair-value formula, on 1-minute Coinbase closes) ------ #

def model_up(spot: dict, m: dict, minute_end: float) -> float | None:
    strike = m["strike"]
    now_bar = spot.get(int(minute_end) - 60)
    before_open = spot.get(int(m["open"]) - 60)
    if not strike or not now_bar or not before_open:
        return None
    basis = strike - (before_open[0] + before_open[1]) / 2     # index vs Coinbase at the open
    closes = [spot[t][1] for t in range(int(minute_end) - 3660, int(minute_end), 60) if t in spot]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    if len(rets) < 30:
        return None
    sd = max(statistics.pstdev(rets), 0.00015) * math.sqrt(max(m["close"] - minute_end, 1) / 60)
    return norm_cdf(math.log((now_bar[1] + basis) / strike) / sd) * 100


# ---- replay ----------------------------------------------------------------- #

def replay_window(m: dict, bars: list[dict], spot: dict, a, want: str, exit_rule: str):
    """Returns (pnl per $100 staked, sold_at_profit, entry price) or None if no trade."""
    for i, b in enumerate(bars):
        minute = round((b["end"] - m["open"]) / 60)            # 1 = end of the first minute
        if minute < 1 or minute > a.entry_window:
            continue
        for side in ("yes", "no"):
            ask = b["ask"] if side == "yes" else 100 - b["bid"]
            bid = b["bid"] if side == "yes" else 100 - b["ask"]
            if not a.min_price <= ask <= a.max_price or ask - bid > a.max_spread:
                continue
            cost = ask + fee_c(ask)
            if want != "blind":
                p = model_up(spot, m, b["end"])
                if p is None:
                    return None
                likes = (p if side == "yes" else 100 - p) - cost >= a.min_edge
                if likes != (want == "model"):
                    continue
            first_profit = math.ceil(cost + a.min_profit - 1e-9)
            target = {"first": first_profit, "plus5": ask + 5, "plus10": ask + 10, "hold": 101}[exit_rule]
            target = max(target, first_profit)
            for later in bars[i + 1:]:
                best_bid = later["bid_high"] if side == "yes" else 100 - later["ask_low"]
                if best_bid >= target:
                    return (target - cost) / cost * 100, True, ask
            return ((100.0 if m["result"] == side else 0.0) - cost) / cost * 100, False, ask
    return None


def summarize(name: str, trades: list[tuple[float, bool, float]], windows: int) -> str:
    if not trades:
        return f"   {name:<34}{0:>7}"
    pnl = [t[0] for t in trades]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]
    se = statistics.pstdev(pnl) / math.sqrt(len(pnl)) if len(pnl) > 1 else float("nan")
    return (f"   {name:<34}{len(trades):>7}{len(wins) / len(trades) * 100:>9.1f}%"
            f"{(sum(wins) / len(wins) if wins else 0):>+10.2f}{(sum(losses) / len(losses) if losses else 0):>+10.2f}"
            f"{sum(pnl) / len(pnl):>+10.2f} +/-{2 * se:<6.2f}{sum(pnl):>+11.0f}")


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", default="KXBTC15M")
    ap.add_argument("--days", type=float, default=3.0)
    ap.add_argument("--min-price", type=float, default=20.0)
    ap.add_argument("--max-price", type=float, default=40.0)
    ap.add_argument("--entry-window", type=float, default=6.0, help="minutes")
    ap.add_argument("--min-edge", type=float, default=1.0)
    ap.add_argument("--min-profit", type=float, default=0.5)
    ap.add_argument("--max-spread", type=float, default=3.0)
    ap.add_argument("--cache-dir", default=os.path.join(here, "data", "backtest"))
    a = ap.parse_args()
    a.series = a.series.upper()
    asset = ASSET.get(a.series, "BTC")

    until = time.time() - 120
    since = until - a.days * 86400
    print(f"Listing settled {a.series} windows for the last {a.days:g} days ...", flush=True)
    markets = settled_markets(a.series, since, until)
    print(f"{len(markets)} windows. Downloading minute bars (cached after the first run) ...", flush=True)
    bars = {}
    for n, m in enumerate(markets, 1):
        path = os.path.join(a.cache_dir, a.series, m["ticker"] + ".json")
        try:
            bars[m["ticker"]] = cached(path, lambda m=m: market_bars(a.series, m))
        except FeedError as e:
            print(f"   skipped {m['ticker']}: {e}")
        if n % 50 == 0:
            print(f"   {n}/{len(markets)}", flush=True)
    spot = spot_minutes(asset, since - 7200, until, a.cache_dir)
    usable = [m for m in markets if len(bars.get(m["ticker"], [])) >= 10]
    ups = sum(1 for m in usable if m["result"] == "yes")
    print(f"\n{len(usable)} windows with full minute data "
          f"({datetime.fromtimestamp(usable[0]['open'], timezone.utc):%b %d %H:%M} to "
          f"{datetime.fromtimestamp(usable[-1]['close'], timezone.utc):%b %d %H:%M} UTC); "
          f"{ups} resolved UP, {len(usable) - ups} DOWN.\n" if usable else "\nNo usable windows.\n")
    if not usable:
        return

    def run(want, exit_rule):
        return [t for m in usable if (t := replay_window(m, bars[m["ticker"]], spot, a, want, exit_rule))]

    head = (f"   {'':<34}{'trades':>7}{'winners':>10}{'avg win':>10}{'avg loss':>10}"
            f"{'avg P&L':>10}{'':<10}{'total':>11}   (all $ per $100 staked)")
    print(f"BUY THE {a.min_price:g}-{a.max_price:g}c SIDE IN THE FIRST {a.entry_window:g} MINUTES, "
          "SELL AT THE FIRST PROFIT, ELSE HOLD TO THE CLOSE")
    print(head)
    blind = run("blind", "first")
    print(summarize("every chance (no model)", blind, len(usable)))
    print(summarize("only when the model likes it", run("model", "first"), len(usable)))
    print(summarize("only when the model does NOT", run("avoid", "first"), len(usable)))
    if blind:
        avg_cost = sum(t[2] + fee_c(t[2]) for t in blind) / len(blind)
        need = 100 * avg_cost / math.ceil(avg_cost + a.min_profit)
        print(f"   A side was in range in {len(blind)} of {len(usable)} windows. "
              f"Break-even needs roughly {need:.0f}% winners at these prices.")
    print("\nSAME ENTRIES (no model), DIFFERENT EXITS")
    print(head)
    for rule, label in (("first", "sell at the first profit"), ("plus5", "sell 5c above entry"),
                        ("plus10", "sell 10c above entry"), ("hold", "never sell, hold to the close")):
        print(summarize(label, run("blind", rule), len(usable)))
    print("\n   'avg P&L' is the number that matters: above zero by more than its +/- means it made money")
    print("   over this period. A few days is a small sample; rerun with more --days to firm it up.")


if __name__ == "__main__":
    main()
