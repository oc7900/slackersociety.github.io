#!/usr/bin/env python3
"""
Do the 15-minute windows come in "regimes"?  Tested on Kalshi's own history.

    python3 regime_check.py --days 60      (downloads once into data/backtest/, then reuses it)

The idea under test:
  * quiet market -> the price leans one way early and stays there      ("lean and stay")
  * busy market  -> the lead changes hands a few times before settling ("flip-flop")
  * the two come in clusters, so the last few windows tell you which kind is next.

Each window is typed from UP's price at the end of each minute. A side "leads" once
it is priced 55c or more; the lead "changes" when the other side reaches 55c.

  1. How common is each type, and how common would it be on a pure random walk,
     where nothing at all is predictable?
  2. Does the mix depend on how volatile the hour before the window was?
  3. Do the types cluster: does the last window (or the last four) say what comes next?
  4. Is the leader priced right in each regime? If it wins as often as it is priced,
     knowing the regime is worth nothing.
  5. Early blow-outs: one side already 95c+ a few minutes in.
  6. Has the answer to 4 changed over the months?

Limits: prices are 1-minute bars, so swings faster than a minute are not counted and
entries happen at minute ends; volatility is from Coinbase 1-minute closes; and it
assumes the quoted size was enough for the stake. Windows older than about two months
come from Kalshi's archive, one request each: --days 180 takes around half an hour
the first time.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from backtest import download_bars, fee_c, parse_bars, polite_get, settled_markets, spot_minutes
from bot import ASSET, KALSHI_API, FeedError, _num, norm_cdf, parse_ts

LEAN = 5.0                      # a lead is 55c+; a lead change is the other side reaching 55c+
MINUTES = range(1, 15)          # minute closes that exist before settlement
KINDS = ("lean and stay", "one change", "flip-flop")


# ---- data ------------------------------------------------------------------- #

def archived_markets(series: str, since: float) -> list[dict]:
    """Windows older than about two months live in Kalshi's archive, listed newest first."""
    out, cursor = [], ""
    while True:
        params = {"series_ticker": series, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        page = polite_get(f"{KALSHI_API}/historical/markets", params)
        rows = page.get("markets") or []
        oldest = None
        for m in rows:
            o, c = parse_ts(m.get("open_time")), parse_ts(m.get("close_time"))
            if not o or not c:
                continue
            oldest = c if oldest is None else min(oldest, c)
            if c >= since and m.get("result") in ("yes", "no"):
                out.append({"ticker": m["ticker"], "open": o, "close": c,
                            "strike": _num(m.get("floor_strike")), "result": m["result"]})
        cursor = page.get("cursor") or ""
        if not cursor or not rows or oldest is None or oldest < since:
            break
    return out


def archived_bars(series: str, markets: list[dict], cache_dir: str) -> dict[str, list[dict]]:
    """Minute bars for archived windows, cached like the recent ones. The archive hands
    out one window per request, so this is the slow part of going back further."""
    def path(m):
        return os.path.join(cache_dir, series, m["ticker"] + ".json")

    def cached_bars(m):
        try:
            with open(path(m)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def fetch(m):
        try:
            data = polite_get(f"{KALSHI_API}/historical/markets/{m['ticker']}/candlesticks", {
                "start_ts": int(m["open"]), "end_ts": int(m["close"]), "period_interval": 1}, pause=0.3)
        except FeedError:
            return
        # the archive names prices "close"; parse_bars expects the live name "close_dollars"
        candles = [{"end_period_ts": c.get("end_period_ts"),
                    **{side: {k + "_dollars": v for k, v in (c.get(side) or {}).items()}
                       for side in ("yes_bid", "yes_ask")}} for c in data.get("candlesticks") or []]
        with open(path(m) + ".tmp", "w") as f:
            json.dump(parse_bars(candles, m), f)
        os.replace(path(m) + ".tmp", path(m))

    os.makedirs(os.path.join(cache_dir, series), exist_ok=True)
    todo = [m for m in markets if len(cached_bars(m)) < 10]
    if todo:
        print(f"   {len(todo)} archived windows to fetch, one request each (about {len(todo) / 500:.0f} min) ...",
              flush=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            for i, _ in enumerate(pool.map(fetch, todo), 1):
                if i % 1000 == 0:
                    print(f"   {i}/{len(todo)}", flush=True)
    return {m["ticker"]: cached_bars(m) for m in markets}


def load_windows(series: str, days: float, cache_dir: str, max_spread: float) -> list[dict]:
    """One record per window that has a two-sided quote at every minute close."""
    until = time.time() - 120
    since = until - days * 86400
    print(f"Listing settled {series} windows for the last {days:g} days ...", flush=True)
    recent = settled_markets(series, since, until)
    older = []
    if not recent or min(m["open"] for m in recent) > since + 3600:      # the recent list stops short
        known = {m["ticker"] for m in recent}
        older = [m for m in archived_markets(series, since) if m["ticker"] not in known]
    markets = sorted(recent + older, key=lambda m: m["open"])
    print(f"{len(markets)} windows. Downloading minute bars (cached after the first run) ...", flush=True)
    bars = download_bars(series, recent, cache_dir)
    bars.update(archived_bars(series, older, cache_dir))
    spot = spot_minutes(ASSET.get(series, "BTC"), since - 7200, until, cache_dir)
    out = []
    for m in markets:
        by_minute = {round((b["end"] - m["open"]) / 60): b for b in bars.get(m["ticker"], [])}
        if any(k not in by_minute for k in MINUTES):
            continue
        bid = [by_minute[k]["bid"] for k in MINUTES]
        ask = [by_minute[k]["ask"] for k in MINUTES]
        mid = [(b + a) / 2 for b, a in zip(bid, ask)]
        if any(a - b > max_spread and 3 <= x <= 97 for a, b, x in zip(ask, bid, mid)):
            continue
        closes = [spot[t][1] for t in range(int(m["open"]) - 3660, int(m["open"]), 60) if t in spot]
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
        vol = math.sqrt(sum(r * r for r in rets) / len(rets)) * 1e4 if len(rets) >= 45 else None
        w = {"open": m["open"], "up": m["result"] == "yes", "bid": bid, "ask": ask, "mid": mid, "vol": vol}
        w.update(path_type(mid))
        out.append(w)
    return out


# ---- typing a window -------------------------------------------------------- #

def path_type(mid: list[float], upto: int = 14) -> dict:
    """Lead changes among the first `upto` minute closes, and who led first and last."""
    leader, first, changes = 0, 0, 0
    for x in mid[:upto]:
        side = 1 if x >= 50 + LEAN else -1 if x <= 50 - LEAN else 0
        if side and not leader:
            first = side
        elif side and side != leader:
            changes += 1
        leader = side or leader
    kind = None if not first else KINDS[min(changes, 2)]
    return {"changes": changes, "first": first, "last": leader, "kind": kind}


def random_walk_windows(n: int = 40000, seed: int = 7, burst: float = 0.65) -> list[dict]:
    """A market with nothing to predict: fair prices on a driftless random walk, quoted
    1c wide and sampled once a minute like the real bars. The strike is the average of
    the minute before the open and the result is the average of the last minute, as on
    Kalshi. Movement arrives unevenly from minute to minute, about as unevenly as BTC's
    does (`burst`: in BTC the biggest minute carries ~35% of a window's movement), and
    the price allows for it. How volatile the walk is overall cancels out."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        pace = [math.exp(rng.gauss(0, burst)) for _ in range(16)]   # minute before the open, 1..14, the last
        x = rng.gauss(0, math.sqrt(pace[0] / 3))                    # price vs strike at the open
        mid = []
        for k in MINUTES:
            x += rng.gauss(0, math.sqrt(pace[k]))
            left = sum(pace[k + 1:15]) + pace[15] / 3               # movement still to come
            mid.append(min(99.5, max(0.5, math.floor(100 * norm_cdf(x / math.sqrt(left))) + 0.5)))
        w = {"up": x + rng.gauss(0, math.sqrt(pace[15] / 3)) >= 0, "mid": mid}
        w.update(path_type(mid))
        out.append(w)
    return out


# ---- small statistics ------------------------------------------------------- #

def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def pm(xs) -> str:
    """mean +/- two standard errors"""
    if len(xs) < 20:
        return f"{'--':>13}"
    m = mean(xs)
    se = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1) / len(xs))
    return f"{m:>+6.2f} +/-{2 * se:<4.2f}"


def corr(xs, ys) -> float:
    mx, my = mean(xs), mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx, syy = sum((x - mx) ** 2 for x in xs), sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy) if sxx and syy else float("nan")


def profile(ws: list[dict]) -> str:
    typed = [w for w in ws if w["kind"]]
    share = [sum(1 for w in typed if w["kind"] == k) / len(typed) * 100 for k in KINDS]
    first_wins = mean([1.0 if (w["first"] > 0) == w["up"] else 0.0 for w in typed]) * 100
    late = mean([1.0 if (w["last"] > 0) != w["up"] else 0.0 for w in typed]) * 100
    return (f"{len(ws):>8}{share[0]:>14.1f}%{share[1]:>11.1f}%{share[2]:>10.1f}%"
            f"{mean([w['changes'] for w in typed]):>10.2f}{first_wins:>14.1f}%{late:>17.1f}%")


def leader_at(w: dict, minute: int):
    """(leader's mid, leader's ask, underdog's ask, leader won) at a minute close; None at exactly 50."""
    i = minute - 1
    mid = w["mid"][i]
    if mid == 50:
        return None
    if mid > 50:
        return mid, w["ask"][i], 100 - w["bid"][i], w["up"]
    return 100 - mid, 100 - w["bid"][i], w["ask"][i], not w["up"]


def priced_right(ws: list[dict], minute: int, lo: float, hi: float) -> str:
    """Leader priced lo..hi at this minute: how often it wins vs its price, and what
    buying either side at the ask and holding to the close nets after the fee."""
    gross, lead, dog = [], [], []
    for w in ws:
        got = leader_at(w, minute)
        if not got or not lo <= got[0] <= hi:
            continue
        price, ask, dog_ask, won = got
        gross.append((100.0 if won else 0.0) - price)
        lead.append((100.0 if won else 0.0) - ask - fee_c(ask))
        dog.append((0.0 if won else 100.0) - dog_ask - fee_c(dog_ask))
    return f"{len(gross):>7}{pm(gross):>18}{pm(lead):>18}{pm(dog):>18}"


# ---- report ----------------------------------------------------------------- #

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", default="KXBTC15M")
    ap.add_argument("--days", type=float, default=60.0)
    ap.add_argument("--max-spread", type=float, default=3.0)
    ap.add_argument("--cache-dir", default=os.path.join(here, "data", "backtest"))
    a = ap.parse_args()
    a.series = a.series.upper()

    ws = load_windows(a.series, a.days, a.cache_dir, a.max_spread)
    if len(ws) < 200:
        print(f"\nOnly {len(ws)} usable windows: too few to say anything. Try more --days.")
        return
    day = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%b %d")
    print(f"\n{len(ws)} windows with a quote at every minute ({day(ws[0]['open'])} to {day(ws[-1]['open'])} UTC); "
          f"UP won {mean([1.0 if w['up'] else 0.0 for w in ws]) * 100:.1f}%.\n")
    walk = random_walk_windows()
    noise = 100 / math.sqrt(len(ws))                      # two standard errors of a share near 50%

    head = (f"   {'':<22}{'windows':>8}{'lean and stay':>15}{'one change':>12}{'flip-flop':>11}"
            f"{'changes':>10}{'1st lean wins':>15}{'last leader lost':>18}")
    print("1. HOW OFTEN EACH TYPE HAPPENS, AGAINST A PURE RANDOM WALK")
    print(head)
    print(f"   {'real windows':<22}{profile(ws)}")
    print(f"   {'random walk':<22}{profile(walk)}")
    print("   The random walk is a market with nothing to predict. Real shares within about")
    print(f"   +/-{noise:.1f} points of it are the same thing: both types turn up this often by chance alone.\n")

    print("2. BY HOW VOLATILE THE HOUR BEFORE THE WINDOW WAS")
    with_vol = sorted((w for w in ws if w["vol"] is not None), key=lambda w: w["vol"])
    third = len(with_vol) // 3
    regimes = {"quiet third": with_vol[:third], "middle third": with_vol[third:2 * third],
               "busy third": with_vol[2 * third:]}
    print(head)
    for name, group in regimes.items():
        print(f"   {name:<22}{profile(group)}")
    if third:
        typed = [w for w in with_vol if w["kind"]]
        print(f"   Lead changes vs prior-hour volatility: correlation "
              f"{corr([math.log(w['vol']) for w in typed], [w['changes'] for w in typed]):+.3f} "
              f"(0 = unrelated, 1 = the same thing; noise is +/-{2 / math.sqrt(len(typed)):.3f}).\n")

    print("3. DO THE TYPES CLUSTER?")
    for lag in (1, 2, 4, 8):
        pairs = [(a_["changes"], b_["changes"]) for a_, b_ in zip(ws, ws[lag:])
                 if b_["open"] - a_["open"] == 900 * lag]
        print(f"   lead changes in a window vs the window {lag} back: correlation "
              f"{corr([p[0] for p in pairs], [p[1] for p in pairs]):+.3f}")
    print(f"   (noise is +/-{2 / math.sqrt(len(ws)):.3f}; volatility itself scores about +0.6 on this test)")
    print(f"   {'after a ... window':<24}{'next is: lean and stay':>24}{'one change':>12}{'flip-flop':>11}{'windows':>9}")
    for k in KINDS:
        nxt = [b_["kind"] for a_, b_ in zip(ws, ws[1:])
               if a_["kind"] == k and b_["kind"] and b_["open"] - a_["open"] == 900]
        if nxt:
            print(f"   {k:<24}" + f"{nxt.count(KINDS[0]) / len(nxt) * 100:>23.1f}%"
                  + f"{nxt.count(KINDS[1]) / len(nxt) * 100:>11.1f}%{nxt.count(KINDS[2]) / len(nxt) * 100:>10.1f}%{len(nxt):>9}")
    print("   Rows that look alike mean the last window says nothing about the next.\n")

    print("4. IS THE LEADER PRICED RIGHT IN EACH REGIME?   (cents per contract, +/- two standard errors)")
    print("   'win rate - price' is before costs. The other two are what a trader keeps: bought at the")
    print("   ask, 7% taker fee paid, held to the close. Leader priced 55-80c.")
    calm4 = [b_ for i, b_ in enumerate(ws) if i >= 4 and b_["open"] - ws[i - 4]["open"] == 3600
             and mean([w["changes"] for w in ws[i - 4:i]]) <= 0.5]
    chop4 = [b_ for i, b_ in enumerate(ws) if i >= 4 and b_["open"] - ws[i - 4]["open"] == 3600
             and mean([w["changes"] for w in ws[i - 4:i]]) >= 1.5]
    after = {k: [b_ for a_, b_ in zip(ws, ws[1:]) if a_["kind"] == k and b_["open"] - a_["open"] == 900]
             for k in KINDS}
    groups = [("all windows", ws)] + list(regimes.items()) + [
        ("after lean and stay", after[KINDS[0]]), ("after flip-flop", after[KINDS[2]]),
        ("last 4 were steady", calm4), ("last 4 were choppy", chop4)]
    for minute in (3, 6):
        print(f"   at the end of minute {minute}:")
        print(f"   {'':<22}{'trades':>7}{'win rate - price':>18}{'buy the leader':>18}{'buy the underdog':>18}")
        for name, group in groups:
            print(f"   {name:<22}{priced_right(group, minute, 55, 80)}")
    print("   by what this window has done so far, at the end of minute 7:")
    print(f"   {'':<22}{'trades':>7}{'win rate - price':>18}{'buy the leader':>18}{'buy the underdog':>18}")
    for name, test in (("steady so far", lambda c: c == 0), ("one change so far", lambda c: c == 1),
                       ("flip-flop so far", lambda c: c >= 2)):
        print(f"   {name:<22}{priced_right([w for w in ws if test(path_type(w['mid'], 7)['changes'])], 7, 55, 90)}")
    print("   An edge needs 'win rate - price' to be clear of zero by more than the ~2c it costs to trade.\n")

    print("5. EARLY BLOW-OUTS: buy a side already offered at 95-99c, hold to the close")
    print(f"   {'minute':<10}{'trades':>7}{'won':>9}{'avg ask':>10}{'P&L per contract':>20}{'losers':>9}")
    for minute in (5, 6, 7, 8, 10, 12, 14):
        pnl, asks, lost = [], [], 0
        for w in ws:
            got = leader_at(w, minute)
            if got and 95 <= got[1] < 99:
                pnl.append((100.0 if got[3] else 0.0) - got[1] - fee_c(got[1]))
                asks.append(got[1])
                lost += 0 if got[3] else 1
        if len(pnl) >= 20:
            print(f"   {minute:<10}{len(pnl):>7}{(1 - lost / len(pnl)) * 100:>8.1f}%{mean(asks):>10.1f}{pm(pnl):>20}{lost:>9}")
    print("   One loser costs about 96c and one winner makes about 3c, so this is decided by a handful")
    print("   of windows. Do not lean on it without far more data than this.\n")

    print("6. HAS THE MARKET CHANGED?   The early leader (end of minute 3, priced 55-80c), month by month")
    print(f"   {'':<22}{'trades':>7}{'win rate - price':>18}{'buy the leader':>18}{'buy the underdog':>18}")
    quiet = {id(w) for w in regimes["quiet third"]}
    months: dict[str, list[dict]] = {}
    for w in ws:
        months.setdefault(datetime.fromtimestamp(w["open"], timezone.utc).strftime("%Y-%m"), []).append(w)
    for name, group in months.items():
        print(f"   {name:<22}{priced_right(group, 3, 55, 80)}")
    print("   the same, quiet hours only:")
    for name, group in months.items():
        print(f"   {name:<22}{priced_right([w for w in group if id(w) in quiet], 3, 55, 80)}")
    print("   A mispricing that shows in the old months and not the recent ones has been competed away;")
    print("   only the recent months say anything about what trading it now would earn.")


if __name__ == "__main__":
    main()
