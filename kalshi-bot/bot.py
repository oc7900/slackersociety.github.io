#!/usr/bin/env python3
"""
Kalshi paper scalper
====================

Paper-trades Kalshi's short "price up or down" crypto windows (default: the
15-minute BTC series) using LIVE public market data. No account, no API key,
no real orders -- but fills are simulated against the real order book:

  * buys lift the real resting offers level by level (never more size than is
    actually displayed), sells hit the real resting bids
  * Kalshi's taker fee is charged on every fill, rounded up to the cent
  * a position that is still open at the close is settled from Kalshi's own
    official result for that market

Strategy
--------
  1. Early in each window, look at the side Kalshi prices at 20-40c (the underdog)
     and estimate what it is really worth: a fair-value probability from the live
     price, the window's target (strike), the time left and recent volatility,
     nudged by what the bot has learned the evidence is worth: momentum, setbacks
     from recent highs and lows, reversion, exchanges moving ahead of the others,
     and pressure in Kalshi's own order book.
  2. If that side is cheap versus the estimate by more than the entry fee plus a
     margin, buy it (an immediate-or-cancel limit order against the book).
  3. Sell the moment the price is high enough that the sale nets a profit after
     all fees. Otherwise hold to settlement (or use --stop-loss).
  4. Repeat, window after window.

Python 3.9+, standard library only.   python3 bot.py --help
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import signal
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
COINBASE_API = "https://api.exchange.coinbase.com"

# Kalshi series -> the asset it is about.
ASSET = {"KXBTC15M": "BTC", "KXETH15M": "ETH", "KXSOL15M": "SOL", "KXXRP15M": "XRP"}


def _first(d: dict):
    return next(iter(d.values()))


# Kalshi settles these markets on a CF Benchmarks index, which is built from the
# order books of several exchanges. These are the constituents that publish a
# keyless ticker; blending them tracks the index far better than any one exchange.
# name -> (url for an asset, function pulling (bid, ask) out of the JSON reply)
FEEDS = {
    "coinbase": (lambda a: f"{COINBASE_API}/products/{a}-USD/ticker",
                 lambda j: (j["bid"], j["ask"])),
    "kraken": (lambda a: f"https://api.kraken.com/0/public/Ticker?pair={'XBT' if a == 'BTC' else a}USD",
               lambda j: (_first(j["result"])["b"][0], _first(j["result"])["a"][0])),
    "bitstamp": (lambda a: f"https://www.bitstamp.net/api/v2/ticker/{a.lower()}usd/",
                 lambda j: (j["bid"], j["ask"])),
    "gemini": (lambda a: f"https://api.gemini.com/v1/pubticker/{a.lower()}usd",
               lambda j: (j["bid"], j["ask"])),
    "cryptocom": (lambda a: f"https://api.crypto.com/exchange/v1/public/get-tickers?instrument_name={a}_USD",
                  lambda j: (j["result"]["data"][0]["b"], j["result"]["data"][0]["k"])),
}

DEFAULT_FEE_RATE = 0.07   # Kalshi general taker fee: 0.07 * contracts * P * (1 - P)
MAKER_FEE_RATE = 0.0175   # resting orders: only on series flagged "quadratic_with_maker_fees"
MIN_SIGMA_1M = 0.00015    # floor on 1-minute volatility so the model is never over-confident
EPS = 1e-9


class FeedError(Exception):
    """A market-data request failed; the bot skips the tick and tries again."""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def http_get(url: str, params: dict | None = None, timeout: float = 8.0, retries: int = 2):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"User-Agent": "paper-bot/1.0", "Accept": "application/json"})
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(1.0 + attempt)          # rate limited: back off
            elif e.code < 500:
                break                              # 4xx will not fix itself
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last = e
        time.sleep(0.25 * (attempt + 1))
    raise FeedError(f"GET {url} -> {last}")


# --------------------------------------------------------------------------- #
# Market data model
# --------------------------------------------------------------------------- #

def parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _num(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


@dataclass
class Market:
    ticker: str
    title: str
    open_ts: float
    close_ts: float
    strike: float | None          # "price to beat"; None if Kalshi does not publish one
    status: str
    result: str                   # "yes" / "no" once settled, else ""

    @classmethod
    def from_api(cls, m: dict) -> "Market":
        strike = _num(m.get("floor_strike"))
        if m.get("strike_type") not in (None, "", "greater", "greater_or_equal") or not strike:
            strike = None
        return cls(
            ticker=m["ticker"],
            title=m.get("title") or m.get("yes_sub_title") or "",
            open_ts=parse_ts(m.get("open_time")) or 0.0,
            close_ts=parse_ts(m.get("close_time")) or 0.0,
            strike=strike,
            status=(m.get("status") or "").lower(),
            result=(m.get("result") or "").lower(),
        )


@dataclass
class Book:
    """Kalshi only publishes BIDS: resting YES bids and resting NO bids.
    An offer to sell YES at p is the same thing as a NO bid at (100 - p)."""
    yes: list[tuple[float, int]] = field(default_factory=list)  # (price in cents, contracts), best first
    no: list[tuple[float, int]] = field(default_factory=list)

    def bids(self, side: str) -> list[tuple[float, int]]:
        return self.yes if side == "yes" else self.no

    def asks(self, side: str) -> list[tuple[float, int]]:
        opp = self.no if side == "yes" else self.yes
        return [(round(100 - p, 4), q) for p, q in opp]   # cheapest first

    def best_bid(self, side: str) -> float | None:
        b = self.bids(side)
        return b[0][0] if b else None

    def best_ask(self, side: str) -> float | None:
        a = self.asks(side)
        return a[0][0] if a else None


def parse_book(payload: dict) -> Book:
    """Accepts both orderbook encodings Kalshi has used: integer-cent levels
    (`yes: [[53, 100]]`) and fixed-point dollar strings (`yes_dollars: [["0.5300", "100.00"]]`)."""
    ob = payload.get("orderbook_fp") or payload.get("orderbook") or {}

    def side(name: str) -> list[tuple[float, int]]:
        levels, scale = ob.get(name + "_dollars"), 100.0
        if levels is None:
            levels, scale = ob.get(name), 1.0
        out = []
        for lvl in levels or []:
            p, q = _num(lvl[0]), _num(lvl[1])
            if p is None or q is None:
                continue
            p, q = round(p * scale, 4), int(q + EPS)      # whole contracts only
            if q > 0 and 0 < p < 100:
                out.append((p, q))
        out.sort(reverse=True)
        return out

    return Book(yes=side("yes"), no=side("no"))


class Kalshi:
    def __init__(self, base: str = KALSHI_API):
        self.base = base

    def fee_rates(self, series: str) -> tuple[float, float]:
        """(taker, maker) fee coefficients for the series, from Kalshi's own series
        record. Falls back to the general schedule (7% taker) if it cannot be read."""
        try:
            s = http_get(f"{self.base}/series/{series}").get("series") or {}
        except FeedError:
            return DEFAULT_FEE_RATE, 0.0
        mult = _num(s.get("fee_multiplier"))
        mult = mult if mult and 0 < mult <= 3 else 1.0
        maker = MAKER_FEE_RATE * mult if "maker" in (s.get("fee_type") or "") else 0.0
        return DEFAULT_FEE_RATE * mult, maker

    def current_markets(self, series: str, now: float) -> list[Market]:
        """Markets of the series closing within the next 16 minutes. Asking by close
        time (with a fresh timestamp) bypasses the 15-second cache on the plain
        "open markets" list, which shows a new window up to 30 seconds late. It is
        a costlier call for Kalshi's rate limit, so it is only used at window changes."""
        try:
            data = http_get(f"{self.base}/markets", {
                "series_ticker": series, "min_close_ts": int(now), "max_close_ts": int(now) + 960,
                "limit": 20}, retries=0)
        except FeedError:
            # Rate-limited or down: the cached list costs nothing against the limit.
            data = http_get(f"{self.base}/markets",
                            {"series_ticker": series, "status": "open", "limit": 20}, retries=0)
        return [Market.from_api(m) for m in data.get("markets") or []]

    def market(self, ticker: str) -> Market:
        return Market.from_api(http_get(f"{self.base}/markets/{ticker}")["market"])

    def orderbook(self, ticker: str) -> Book:
        return parse_book(http_get(f"{self.base}/markets/{ticker}/orderbook"))


class Spot:
    """Our replica of the settlement index: the average mid-price across the
    constituent exchanges, fetched in parallel every tick. Every value is
    remembered, so the bot can average its own readings over any past minute."""

    STALE = 3.0          # seconds a quote stays usable
    BENCH = 60.0         # seconds a feed that keeps failing is rested

    def __init__(self, asset: str, feeds: list[str] | None = None):
        self.asset = asset
        self.feeds = {n: FEEDS[n] for n in (feeds or FEEDS)}
        self._pool = ThreadPoolExecutor(max_workers=len(self.feeds))
        self._last: dict[str, tuple[float, float]] = {}       # feed -> (ts, mid)
        self._offset = {n: 0.0 for n in self.feeds}           # feed's usual gap to the blend
        self._fails = {n: 0 for n in self.feeds}
        self._benched: dict[str, float] = {}
        self.live: list[str] = []                             # feeds used in the latest price
        self._ticks: deque[tuple[float, float]] = deque(maxlen=4000)   # (ts, price)
        self._feed_ticks = {n: deque(maxlen=300) for n in self.feeds}  # per exchange, for comparison
        self._candles: list[tuple[float, float, float]] = []   # (start_ts, open, close) oldest first
        self._candles_at = 0.0

    def _fetch(self, name: str) -> float:
        url, parse = self.feeds[name]
        bid, ask = (float(x) for x in parse(http_get(url(self.asset), timeout=2.0, retries=0)))
        if not 0 < bid <= ask * 1.01:
            raise FeedError(f"{name}: implausible quote {bid}/{ask}")
        return (bid + ask) / 2

    def price(self, now: float) -> float:
        jobs = {n: self._pool.submit(self._fetch, n) for n in self.feeds
                if self._benched.get(n, 0) <= now}
        for n, job in jobs.items():
            try:
                self._last[n] = (now, job.result())
                self._feed_ticks[n].append(self._last[n])
                self._fails[n] = 0
            except Exception:                      # one exchange being down must not stop the bot
                self._fails[n] += 1
                if self._fails[n] >= 5:
                    self._benched[n] = now + self.BENCH
        mids = {n: m for n, (ts, m) in self._last.items() if now - ts <= self.STALE}
        if not mids:
            raise FeedError("no exchange price feed answered")
        # Each exchange sits a few dollars off the blend. Removing that usual gap
        # keeps the blend from jumping when one exchange misses a tick.
        px = sum(m - self._offset[n] for n, m in mids.items()) / len(mids)
        if len(mids) == len(self.feeds) - len([n for n in self._benched if self._benched[n] > now]):
            raw = sum(mids.values()) / len(mids)
            for n, m in mids.items():
                self._offset[n] += 0.02 * ((m - raw) - self._offset[n])
        self.live = sorted(mids)
        self._ticks.append((now, px))
        return px

    def avg_between(self, t0: float, t1: float, min_samples: int = 15) -> float | None:
        """Average of our own samples in [t0, t1]; None if we were not watching then."""
        xs = [p for ts, p in self._ticks if t0 <= ts <= t1]
        return sum(xs) / len(xs) if len(xs) >= min_samples else None

    def momentum(self, now: float, secs: float = 60.0) -> float:
        """Log return of the index replica over the last `secs` seconds (0 if unknown)."""
        if not self._ticks:
            return 0.0
        then = min(self._ticks, key=lambda t: abs(t[0] - (now - secs)))
        if abs(then[0] - (now - secs)) > 5 or then[1] <= 0:
            return 0.0
        return math.log(self._ticks[-1][1] / then[1])

    def evidence(self, now: float) -> dict[str, float]:
        """Clues about where the index goes next, all in basis points (0.01%):
        momentum at three speeds, the setback from the recent high and low, the gap to
        the recent average (reversion), and how far each exchange is ahead of the blend."""
        if not self._ticks:
            return {}
        px = self._ticks[-1][1]
        recent = [p for ts, p in self._ticks if now - ts <= 300]
        ev = {
            "mom10": self.momentum(now, 10) * 1e4,
            "mom60": self.momentum(now, 60) * 1e4,
            "mom300": self.momentum(now, 300) * 1e4,
        }
        if len(recent) >= 60:
            ev["off_high"] = math.log(px / max(recent)) * 1e4      # <= 0: how far below the 5-min high
            ev["off_low"] = math.log(px / min(recent)) * 1e4       # >= 0: how far above the 5-min low
            ev["vs_avg"] = math.log(px / (sum(recent) / len(recent))) * 1e4
        for n, (ts, mid) in self._last.items():
            if now - ts <= self.STALE:
                ev["lead_" + n] = (mid - self._offset[n] - px) / px * 1e4
        return ev

    def feed_avgs(self, t0: float, t1: float) -> dict[str, float]:
        """Each exchange's own average over [t0, t1] (to see which tracks the index best)."""
        out = {}
        for n, ticks in self._feed_ticks.items():
            xs = [p for ts, p in ticks if t0 <= ts <= t1]
            if len(xs) >= 15:
                out[n] = sum(xs) / len(xs)
        return out

    def candles(self, now: float) -> list[tuple[float, float, float]]:
        if now - self._candles_at >= 30 or not self._candles:
            self._candles_at = now
            rows = http_get(f"{COINBASE_API}/products/{self.asset}-USD/candles", {"granularity": 60})
            # row = [start, low, high, open, close, volume], newest first
            self._candles = sorted((float(r[0]), float(r[3]), float(r[4])) for r in rows)
        return self._candles

    def sigma_1m(self, now: float, lookback: int = 60) -> float:
        """Recent volatility: standard deviation of 1-minute returns over the last hour."""
        try:
            closes = [c for _, _, c in self.candles(now)][-(lookback + 1):]
        except FeedError:
            closes = [c for _, _, c in self._candles][-(lookback + 1):]
        if len(closes) < 11:                       # no candles: use our own readings, a minute apart
            closes, nxt = [], 0.0
            for ts, p in self._ticks:
                if ts >= nxt:
                    closes.append(p)
                    nxt = ts + 60
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
        if len(rets) < 10:
            return MIN_SIGMA_1M * 4
        return max(statistics.pstdev(rets), MIN_SIGMA_1M)


# --------------------------------------------------------------------------- #
# Fees + fill simulation (pure functions)
# --------------------------------------------------------------------------- #

def fee_cents(rate: float, qty: int, price_c: float) -> int:
    """Kalshi taker fee in cents: rate * contracts * P * (1 - P), rounded UP to the cent."""
    p = price_c / 100.0
    return int(math.ceil(rate * qty * p * (1 - p) * 100 - 1e-7))


@dataclass
class Fill:
    price: float   # cents per contract
    qty: int
    fee: int       # cents


def sim_buy(book: Book, side: str, budget_c: float, limit_c: float, rate: float) -> list[Fill]:
    """Immediate-or-cancel limit buy. Lifts real offers, cheapest first, up to the
    limit price, never taking more than is displayed, keeping cost + fees within budget."""
    fills, spent = [], 0.0
    for price, avail in book.asks(side):
        if price > limit_c + EPS:
            break
        qty = min(avail, int((budget_c - spent) // price))
        while qty > 0 and spent + qty * price + fee_cents(rate, qty, price) > budget_c + EPS:
            qty -= 1
        if qty <= 0:
            break
        fee = fee_cents(rate, qty, price)
        fills.append(Fill(price, qty, fee))
        spent += qty * price + fee
    return fills


def sim_sell(book: Book, side: str, qty: int, min_price_c: float, rate: float) -> list[Fill]:
    """Immediate-or-cancel limit sell. Hits real bids, best first, down to min price."""
    fills = []
    for price, avail in book.bids(side):
        if qty <= 0 or price < min_price_c - EPS:
            break
        q = min(avail, qty)
        fills.append(Fill(price, q, fee_cents(rate, q, price)))
        qty -= q
    return fills


def gross(fills: list[Fill]) -> float:
    return sum(f.price * f.qty for f in fills)


def fees(fills: list[Fill]) -> int:
    return sum(f.fee for f in fills)


def filled(fills: list[Fill]) -> int:
    return sum(f.qty for f in fills)


def profit_target(avg_cost_c: float, qty: int, min_profit_c: float, rate: float) -> float | None:
    """Lowest whole-cent bid at which selling `qty` contracts nets at least
    avg cost + min profit per contract AFTER the exit fee. None if unreachable."""
    p = max(1, math.ceil(avg_cost_c - EPS))
    while p <= 99:
        if p - fee_cents(rate, qty, p) / qty >= avg_cost_c + min_profit_c - EPS:
            return float(p)
        p += 1
    return None


# --------------------------------------------------------------------------- #
# Fair-value model
# --------------------------------------------------------------------------- #

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_up(spot: float, strike: float, sigma_1m: float, secs_left: float, drift: float = 0.0) -> float:
    """P(price finishes above the strike). Where the price is now versus the target,
    plus `drift` (how much further momentum is expected to carry it, as a log return),
    measured against how far it could plausibly wander in the time left given the
    recently realised volatility."""
    if spot <= 0 or strike <= 0:
        return 0.5
    sd = max(sigma_1m, MIN_SIGMA_1M) * math.sqrt(max(secs_left, 1.0) / 60.0)
    return norm_cdf((math.log(spot / strike) + drift) / sd)


# --------------------------------------------------------------------------- #
# Learning what the evidence is worth
# --------------------------------------------------------------------------- #

EVIDENCE = (["mom10", "mom60", "mom300", "off_high", "off_low", "vs_avg"]
            + ["lead_" + n for n in FEEDS] + ["book_tilt", "mkt_mom30"])


class Learner:
    """Works out, from the bot's own recordings, how much each clue says about the
    index's move over the next minute, and how far that forecast can be trusted.

    Every second the clues are stored; a minute later the move that actually
    followed is known, and the weights are refitted (ridge regression, so weak
    clues get weights near zero). Each forecast is also scored against what then
    happened -- always on data the weights had not seen -- and `trust` is the share
    of the forecast that actually showed up. The bot uses forecast x trust, so
    clues that do not hold up are ignored automatically."""

    HORIZON = 60.0        # seconds ahead being forecast
    MIN_FIT = 1200        # matured samples before the first fit (~20 minutes)
    MIN_SCORED = 900      # scored forecasts before any trust is given (~15 minutes more)
    REFIT_EVERY = 300.0
    RIDGE = 0.5
    KEEP = 0.5 ** (1 / 21600.0)       # old evidence fades: half-life 6 hours
    KEEP_SCORE = 0.5 ** (1 / 7200.0)  # trust adapts faster: half-life 2 hours

    def __init__(self, names=EVIDENCE):
        k = len(names)
        self.names = list(names)
        self.A = [[0.0] * k for _ in range(k)]
        self.b = [0.0] * k
        self.n = 0.0
        self.w = [0.0] * k
        self.fitted = False
        self.sxy = self.sxx = 0.0
        self.scored = 0.0
        self._pending: deque = deque()
        self._last_fit = 0.0

    def observe(self, now: float, clues: dict[str, float], px: float) -> float:
        """Feed this second's clues and price; returns the expected log return of the
        index over the next minute (0 until the evidence has earned some trust)."""
        while self._pending and now - self._pending[0][0] >= self.HORIZON:
            ts, x0, px0, pred0 = self._pending.popleft()
            if now - ts > self.HORIZON + 5 or px0 <= 0:
                continue                                  # a gap in the data: unusable
            y = math.log(px / px0) * 1e4
            for i, xi in enumerate(x0):
                self.b[i] = self.b[i] * self.KEEP + xi * y
                row = self.A[i]
                for j, xj in enumerate(x0):
                    row[j] = row[j] * self.KEEP + xi * xj
            self.n = self.n * self.KEEP + 1
            if pred0 is not None:
                self.sxy = self.sxy * self.KEEP_SCORE + pred0 * y
                self.sxx = self.sxx * self.KEEP_SCORE + pred0 * pred0
                self.scored = self.scored * self.KEEP_SCORE + 1
        if self.n >= self.MIN_FIT and now - self._last_fit >= self.REFIT_EVERY:
            self._last_fit = now
            self.fit()
        x = [clues.get(name, 0.0) for name in self.names]
        pred = sum(wi * xi for wi, xi in zip(self.w, x)) if self.fitted else None
        self._pending.append((now, x, px, pred))
        return 0.0 if pred is None else self.trust() * pred * 1e-4

    def trust(self) -> float:
        """Share of the forecast that has actually materialised, out of sample (0..1)."""
        if self.scored < self.MIN_SCORED or self.sxx <= 0:
            return 0.0
        return min(1.0, max(0.0, self.sxy / self.sxx))

    def fit(self):
        k = len(self.names)
        m = [self.A[i][:] + [self.b[i]] for i in range(k)]
        for i in range(k):
            m[i][i] += self.RIDGE * self.A[i][i] + 1e-9   # shrink each weight toward zero
        for c in range(k):                                # Gauss-Jordan with partial pivoting
            p = max(range(c, k), key=lambda r: abs(m[r][c]))
            if abs(m[p][c]) < 1e-12:
                continue
            m[c], m[p] = m[p], m[c]
            for r in range(k):
                if r != c and m[r][c]:
                    f = m[r][c] / m[c][c]
                    m[r] = [a - f * b for a, b in zip(m[r], m[c])]
        self.w = [m[i][k] / m[i][i] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(k)]
        self.fitted = True

    def describe(self) -> str:
        if not self.fitted:
            return f"still gathering evidence ({self.n:,.0f} of {self.MIN_FIT:,} samples before the first fit)"
        # effect of a typical-sized reading of each clue on the forecast, in basis points
        eff = sorted(((self.w[i] * math.sqrt(self.A[i][i] / max(self.n, 1)), n)
                      for i, n in enumerate(self.names)), key=lambda t: -abs(t[0]))
        top = ", ".join(f"{n} {e:+.2f}" for e, n in eff[:5] if abs(e) >= 0.005) or "nothing stands out"
        return (f"evidence so far ({self.n:,.0f} samples), typical pull on the next minute in bp: {top}"
                f"  | trust {self.trust() * 100:.0f}%")

    def state(self) -> dict:
        return {"names": self.names, "A": self.A, "b": self.b, "n": self.n, "w": self.w,
                "fitted": self.fitted, "sxy": self.sxy, "sxx": self.sxx, "scored": self.scored}

    def restore(self, st: dict):
        if st.get("names") == self.names:
            self.A, self.b, self.n, self.w = st["A"], st["b"], st["n"], st["w"]
            self.fitted, self.sxy, self.sxx, self.scored = st["fitted"], st["sxy"], st["sxx"], st["scored"]


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #

@dataclass
class Position:
    ticker: str
    side: str            # "yes" (up) or "no" (down)
    qty: int
    cost_c: float        # total paid for the contracts still held, incl. entry fees
    entry_price: float   # average fill price, cents
    entry_ts: float
    close_ts: float
    realized_c: float = 0.0   # P&L already banked on partial exits
    fees_c: float = 0.0
    last_settle_poll: float = 0.0

    @property
    def avg_cost(self) -> float:
        return self.cost_c / self.qty if self.qty else 0.0


def hhmmss(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S")


def cents(p: float | None) -> str:
    """Price for display: whole cents, or one decimal in Kalshi's sub-cent zones."""
    return "--" if p is None else f"{p:.1f}".rstrip("0").rstrip(".")


def usd(c: float) -> str:
    return f"{'-' if c < 0 else ''}${abs(c) / 100:,.2f}"


class Bot:
    def __init__(self, cfg, kalshi: Kalshi, spot: Spot, clock=time, out=print):
        self.cfg, self.kalshi, self.spot, self.clock, self.out = cfg, kalshi, spot, clock, out
        self.rate, self.maker_rate = DEFAULT_FEE_RATE, 0.0
        self.cash_c = cfg.bankroll * 100.0
        self.start_cash_c = self.cash_c
        self.positions: dict[str, Position] = {}
        self.closed: list[dict] = []          # one record per finished round trip
        self.fees_paid_c = 0.0
        self.market: Market | None = None
        self._markets_at = 0.0
        self._strike_cache: dict[str, tuple[float, float]] = {}   # ticker -> (strike, basis)
        self._strike_poll = 0.0
        self._strike_note = ""
        self.learner = Learner()
        self._mids: deque[tuple[float, float]] = deque(maxlen=120)   # this window's UP price history
        self.learner_path = os.path.join(cfg.data_dir, "learner.json")
        if os.path.exists(self.learner_path):
            try:
                with open(self.learner_path) as f:
                    self.learner.restore(json.load(f))
            except (OSError, ValueError, KeyError):
                pass                                     # unreadable: just start learning afresh
        self._entries: dict[str, int] = {}
        self._last_exit_ts = 0.0
        self._last_status = 0.0
        self.stop = False
        os.makedirs(cfg.data_dir, exist_ok=True)
        self.state_path = os.path.join(cfg.data_dir, "state.json")
        self.trades_path = os.path.join(cfg.data_dir, "trades.csv")
        self.ticks_path = os.path.join(cfg.data_dir, "ticks.csv")       # research log, see edge_check.py
        self.windows_path = os.path.join(cfg.data_dir, "windows.csv")
        self.basis_path = os.path.join(cfg.data_dir, "basis.csv")
        self._ticks_file = None
        self._awaiting_result: list[Market] = []
        self._result_poll = 0.0
        if cfg.reset:
            for p in (self.state_path, self.trades_path):
                if os.path.exists(p):
                    os.remove(p)
        self._load()

    # ----- persistence ---------------------------------------------------- #

    def _load(self):
        if not os.path.exists(self.state_path):
            return
        with open(self.state_path) as f:
            s = json.load(f)
        self.cash_c = s["cash_c"]
        self.start_cash_c = s["start_cash_c"]
        self.fees_paid_c = s.get("fees_paid_c", 0.0)
        self.closed = s.get("closed", [])
        self.positions = {t: Position(**p) for t, p in s.get("positions", {}).items()}
        self.out(f"resumed: cash {usd(self.cash_c)}, {len(self.closed)} closed trades, "
                 f"{len(self.positions)} open position(s)  (--reset to start over)")

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({
                "cash_c": self.cash_c, "start_cash_c": self.start_cash_c,
                "fees_paid_c": self.fees_paid_c, "closed": self.closed,
                "positions": {t: vars(p) for t, p in self.positions.items()},
            }, f, indent=1)
        os.replace(tmp, self.state_path)

    def _record(self, now, ticker, action, side, qty, price_c, fee_c, pnl_c, note=""):
        now = self.clock.time()
        new = not os.path.exists(self.trades_path)
        with open(self.trades_path, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["utc_time", "ticker", "action", "side", "contracts", "avg_price_cents",
                            "fee_usd", "realized_pnl_usd", "cash_after_usd", "note"])
            w.writerow([datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        ticker, action, side, qty, f"{price_c:.2f}", f"{fee_c / 100:.2f}",
                        "" if pnl_c is None else f"{pnl_c / 100:.2f}", f"{self.cash_c / 100:.2f}", note])

    def log(self, msg: str):
        self.out(f"{hhmmss(self.clock.time())}  {msg}")

    def _save_learner(self):
        tmp = self.learner_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.learner.state(), f)
        os.replace(tmp, self.learner_path)

    def _record_tick(self, now: float, mkt: Market, book: Book, sig: dict):
        """One row per second: what the model thought and what the market charged.
        edge_check.py reads this to test whether the model knows anything the market doesn't."""
        if self.cfg.no_record:
            return
        if self._ticks_file is None:
            new = not os.path.exists(self.ticks_path)
            self._ticks_file = open(self.ticks_path, "a", newline="")
            if new:
                self._ticks_file.write("ts,ticker,secs_left,strike,index,feeds,sigma_1m,model_up,"
                                       "up_bid,up_ask,up_bid_size,up_ask_size,mom_60_bp,learned_bp\n")
        yes, no = book.yes[:1], book.no[:1]
        if not yes or not no:
            return
        self._ticks_file.write(
            f"{now:.0f},{mkt.ticker},{mkt.close_ts - now:.0f},{sig['strike']:.2f},{sig['spot']:.2f},"
            f"{len(self.spot.live)},{sig['sigma']:.6f},{sig['p_up'] * 100:.2f},"
            f"{yes[0][0]:g},{100 - no[0][0]:g},{yes[0][1]},{no[0][1]},{sig['mom'] * 1e4:.3f},{sig['learned'] * 1e4:.4f}\n")
        self._ticks_file.flush()

    def _record_basis(self, mkt: Market, strike: float, ours: float):
        """At each open the true index value is known (it is the strike). Log how far
        the replica, and each exchange alone, was from it: the replica's error."""
        if self.cfg.no_record or not mkt.strike or not hasattr(self.spot, "feed_avgs"):
            return
        feeds = self.spot.feed_avgs(mkt.open_ts - 60, mkt.open_ts)
        new = not os.path.exists(self.basis_path)
        with open(self.basis_path, "a", newline="") as f:
            if new:
                f.write("ticker,strike,replica," + ",".join(FEEDS) + "\n")
            f.write(f"{mkt.ticker},{strike:.2f},{ours:.2f},"
                    + ",".join(f"{feeds[n]:.2f}" if n in feeds else "" for n in FEEDS) + "\n")

    def _record_results(self, now: float):
        """Log how every watched window resolved, traded or not."""
        if not self._awaiting_result or now - self._result_poll < 5:
            return
        self._result_poll = now
        mkt = self._awaiting_result[0]
        if now < mkt.close_ts + 20:                 # not urgent: keep clear of the window change
            return
        if now - mkt.close_ts > 600:                # give up on a window that never resolves
            self._awaiting_result.pop(0)
            return
        m = self.kalshi.market(mkt.ticker)
        if m.result in ("yes", "no"):
            self._awaiting_result.pop(0)
            new = not os.path.exists(self.windows_path)
            with open(self.windows_path, "a", newline="") as f:
                if new:
                    f.write("ticker,open_ts,close_ts,strike,result\n")
                f.write(f"{m.ticker},{m.open_ts:.0f},{m.close_ts:.0f},{m.strike or ''},{m.result}\n")

    # ----- main loop ------------------------------------------------------ #

    def run(self):
        cfg = self.cfg
        self.rate, self.maker_rate = self.kalshi.fee_rates(cfg.series)
        self.out(f"PAPER TRADING {cfg.series} | cash {usd(self.cash_c)} | stake {usd(cfg.stake * 100)}/trade"
                 f" | fees: taker {self.rate:.3f}*C*P*(1-P), resting {self.maker_rate:.4f}"
                 f" | exit by {cfg.exit} order | no real orders are ever sent")
        started = self.clock.time()
        while not self.stop:
            t0 = self.clock.time()
            try:
                self.tick(t0)
            except FeedError as e:
                self.log(f"data feed hiccup, retrying: {e}")
            if cfg.minutes and self.clock.time() - started >= cfg.minutes * 60:
                break
            self.clock.sleep(max(0.0, cfg.poll - (self.clock.time() - t0)))
        self._save()
        self._save_learner()
        if self._ticks_file:
            self._ticks_file.close()
            self._ticks_file = None
        self.out("")
        self.out(self.summary())

    def tick(self, now: float):
        spot = self.spot.price(now)          # sampled every tick: the history feeds the basis estimate
        self._settle_closed(now)
        if not self.cfg.no_record:
            self._record_results(now)
        mkt = self._current_market(now)
        if mkt is None:
            if now - self._last_status >= 30:
                self._last_status = now
                self.log(f"no open {self.cfg.series} market right now; waiting")
            return
        book = self.kalshi.orderbook(mkt.ticker)
        learned = self.learner.observe(now, self._clues(now, book), spot)
        sig = self._signal(mkt, book, spot, now, learned)
        if sig:
            self._record_tick(now, mkt, book, sig)
        pos = self.positions.get(mkt.ticker)
        if pos:
            self._manage(pos, mkt, book, now)
        elif sig:
            self._maybe_enter(mkt, book, sig, now)
        if now - self._last_status >= self.cfg.status_every:
            self._last_status = now
            self._status(mkt, book, sig, now)

    def _current_market(self, now: float) -> Market | None:
        m = self.market
        if m and m.close_ts - now > 0:
            return m                               # a window's market never changes mid-window
        if now - self._markets_at < 3:
            return None
        self._markets_at = now
        live = [x for x in self.kalshi.current_markets(self.cfg.series, now)
                if x.open_ts <= now < x.close_ts]
        new = min(live, key=lambda x: x.close_ts) if live else None
        if new:
            self.log(f"--- window {new.ticker}  closes {hhmmss(new.close_ts)} UTC  {new.title}")
            self.log("    " + self.learner.describe())
            self._awaiting_result.append(new)
            self._mids.clear()
            self._save_learner()
        self.market = new
        return new

    # ----- signal --------------------------------------------------------- #

    def _clues(self, now: float, book: Book) -> dict[str, float]:
        """Everything the learner weighs: price action on the exchanges plus what
        Kalshi's own book is doing."""
        clues = self.spot.evidence(now) if hasattr(self.spot, "evidence") else {}
        if book.yes and book.no:
            bid_sz, ask_sz = book.yes[0][1], book.no[0][1]
            clues["book_tilt"] = (bid_sz - ask_sz) / (bid_sz + ask_sz)       # -1..1: buyers vs sellers of UP
            mid = (book.yes[0][0] + 100 - book.no[0][0]) / 2
            old = [m for ts, m in self._mids if 25 <= now - ts <= 35]
            if old:
                clues["mkt_mom30"] = mid - old[0]                            # UP price change, last 30s
            self._mids.append((now, mid))
        return clues

    def _signal(self, mkt: Market, book: Book, spot: float, now: float, learned: float = 0.0) -> dict | None:
        """Model probability for each side and its edge (in cents, net of the entry fee)
        versus the price we would actually have to pay."""
        sigma = self.spot.sigma_1m(now)
        strike, basis = self._strike(mkt, now)
        if not strike:
            return None
        # Expected further move over the next minute: whatever the learner has found
        # the evidence to be worth, plus an optional fixed momentum assumption.
        secs_left = mkt.close_ts - now
        mom = self.spot.momentum(now, 60.0)
        drift = (learned + self.cfg.momentum * mom) * min(1.0, secs_left / 60.0)
        p_up = prob_up(spot + basis, strike, sigma, secs_left, drift)
        sig = {"spot": spot + basis, "strike": strike, "sigma": sigma, "p_up": p_up, "mom": mom,
               "learned": learned, "edge": {}, "fair": {}}
        for side, p in (("yes", p_up), ("no", 1 - p_up)):
            ask = book.best_ask(side)
            sig["fair"][side] = p * 100
            sig["edge"][side] = None if ask is None else p * 100 - ask - fee_cents(self.rate, 100, ask) / 100
        return sig

    def _strike(self, mkt: Market, now: float) -> tuple[float | None, float]:
        """(strike, basis). The strike is the index's 60-second average just before
        the window opened. `basis` = strike minus OUR replica's average over that same
        minute: whatever small gap is left between the replica and the real index, so
        the model compares like with like. That needs the bot to have been watching at the
        open, so a window joined part-way through is observed but not traded."""
        if mkt.ticker in self._strike_cache:
            return self._strike_cache[mkt.ticker]
        ours = self.spot.avg_between(mkt.open_ts - 60, mkt.open_ts)
        if ours is None:
            if self._strike_note != mkt.ticker:
                self._strike_note = mkt.ticker
                self.log("    joined this window part-way through: watching only, trading starts next window")
            return (None, 0.0)
        if not mkt.strike and now - mkt.open_ts <= 30:
            # The strike is published a few seconds after the open: ask again.
            if now - self._strike_poll >= 3:
                self._strike_poll = now
                for x in self.kalshi.current_markets(self.cfg.series, now):
                    if x.ticker == mkt.ticker:
                        mkt.strike = x.strike
            if not mkt.strike:
                return (None, 0.0)
        strike = mkt.strike or ours                      # series with no published strike: spot at the open
        basis = strike - ours
        if abs(basis) > 0.003 * ours:                    # a gap that large means bad data, not basis
            basis = 0.0
        self._strike_cache = {mkt.ticker: (strike, basis)}
        self.log(f"    strike {strike:,.2f}   our index replica read {ours:,.2f} ({basis:+,.2f})"
                 f" from {len(self.spot.live)} exchange(s): {' '.join(self.spot.live)}")
        self._record_basis(mkt, strike, ours)
        return strike, basis

    # ----- entries -------------------------------------------------------- #

    def _maybe_enter(self, mkt: Market, book: Book, sig: dict, now: float):
        cfg = self.cfg
        if now - mkt.open_ts > cfg.entry_window * 60:          # only early in the window
            return
        if self._entries.get(mkt.ticker, 0) >= cfg.max_entries or now - self._last_exit_ts < cfg.cooldown:
            return
        budget = min(cfg.stake * 100.0, self.cash_c)
        if budget < 100:
            return
        # Only the side Kalshi currently prices inside the chosen range (by default the
        # 20-40c underdog) is a candidate, and only if the model thinks it is worth more
        # than it costs after the entry fee.
        picks = []
        for side in ("yes", "no"):
            edge, ask, bid = sig["edge"][side], book.best_ask(side), book.best_bid(side)
            if (edge is not None and ask is not None and bid is not None
                    and cfg.min_price <= ask <= cfg.max_price
                    and ask - bid <= cfg.max_spread and edge >= cfg.min_edge):
                picks.append((edge, side, ask))
        if not picks:
            return
        edge, side, ask = max(picks)

        # Decision made. A real order takes time to reach the exchange, so wait
        # out the latency and fill against a FRESH book, as a limit order.
        limit = min(ask + cfg.slippage, cfg.max_price)
        self.clock.sleep(cfg.latency_ms / 1000.0)
        fresh = self.kalshi.orderbook(mkt.ticker)
        fills = sim_buy(fresh, side, budget, limit, self.rate)
        qty = filled(fills)
        if not qty:
            self.log(f"signal {side.upper()} @ {ask:.0f}c (edge {edge:+.1f}c) but the offer was gone -- no fill")
            self._last_exit_ts = now   # brief cooldown before trying again
            return
        cost, fee = gross(fills) + fees(fills), fees(fills)
        self.cash_c -= cost
        self.fees_paid_c += fee
        pos = Position(mkt.ticker, side, qty, cost, gross(fills) / qty, now, mkt.close_ts, fees_c=fee)
        self.positions[mkt.ticker] = pos
        self._entries[mkt.ticker] = self._entries.get(mkt.ticker, 0) + 1
        target = self._target(pos)
        self._record(now, mkt.ticker, "BUY", side, qty, pos.entry_price, fee, None,
                     f"model {sig['fair'][side]:.1f}c edge {edge:+.1f}c")
        plan = "hold to settlement"
        if target:
            plan = (f"resting sell order at {target:.0f}c" if cfg.exit == "limit"
                    else f"sell when bid >= {target:.0f}c")
        self.log(f"BUY  {qty} {'UP/YES' if side == 'yes' else 'DOWN/NO'} @ {pos.entry_price:.1f}c"
                 f"  cost {usd(cost)} (fee {usd(fee)})  model {sig['fair'][side]:.1f}c, edge {edge:+.1f}c"
                 f"  -> {plan}")
        self._save()

    # ----- exits ---------------------------------------------------------- #

    def _target(self, pos: Position) -> float | None:
        """Price at which this position is sold for a profit after all fees."""
        rate = self.maker_rate if self.cfg.exit == "limit" else self.rate
        return profit_target(pos.avg_cost, pos.qty, self.cfg.min_profit, rate)

    def _manage(self, pos: Position, mkt: Market, book: Book, now: float):
        cfg = self.cfg
        bid = book.best_bid(pos.side)
        if bid is None:
            return
        target = self._target(pos)
        if target is not None and bid >= target - EPS:
            self._take_profit(pos, mkt, book, target, now)
        elif cfg.stop_loss and bid <= pos.entry_price - cfg.stop_loss + EPS:
            self._dump(pos, mkt, now, "STOP", f"stop-loss ({cfg.stop_loss:.0f}c)")
        elif cfg.exit_before_close and mkt.close_ts - now <= cfg.exit_before_close:
            self._dump(pos, mkt, now, "TIMEOUT", "time exit")

    def _take_profit(self, pos: Position, mkt: Market, book: Book, target: float, now: float):
        if self.cfg.exit == "limit":
            # Our sell order has been resting at `target` since the entry. A bid showing
            # at or above it can only exist once every offer at that price, ours included,
            # has been bought. We count the fill only when those bids cover the whole
            # position, and at OUR price (not the higher bid), at the resting-order fee.
            qty = filled(sim_sell(book, pos.side, pos.qty, target, 0.0))
            fills = [Fill(target, qty, fee_cents(self.maker_rate, qty, target))] if qty == pos.qty else []
            depth = sum(q for p, q in book.bids(pos.side) if p >= target - EPS)
            why = f"profit (book showed {depth:,} bid at {cents(target)}c or better, best {cents(book.best_bid(pos.side))}c)"
        else:
            # Marketable order: wait out the latency, then hit whatever bids are really
            # there, keeping only the part that is still profitable after the taker fee.
            self.clock.sleep(self.cfg.latency_ms / 1000.0)
            fills = sim_sell(self.kalshi.orderbook(mkt.ticker), pos.side, pos.qty, target, self.rate)
            while fills and gross(fills) - fees(fills) < filled(fills) * (pos.avg_cost + self.cfg.min_profit) - EPS:
                fills.pop()
            why = "profit"
        self._book_sale(pos, fills, now, "SELL", why)

    def _dump(self, pos: Position, mkt: Market, now: float, action: str, why: str):
        """Get out now: marketable order against the real bids, taker fee."""
        self.clock.sleep(self.cfg.latency_ms / 1000.0)
        fills = sim_sell(self.kalshi.orderbook(mkt.ticker), pos.side, pos.qty, 1, self.rate)
        self._book_sale(pos, fills, now, action, why)

    def _book_sale(self, pos: Position, fills: list[Fill], now: float, action: str, why: str):
        qty = filled(fills)
        if not qty:
            return                      # nothing there to trade with
        avg_cost = pos.avg_cost
        proceeds, fee = gross(fills) - fees(fills), fees(fills)
        pnl = proceeds - avg_cost * qty
        self.cash_c += proceeds
        self.fees_paid_c += fee
        pos.fees_c += fee
        pos.realized_c += pnl
        pos.cost_c -= avg_cost * qty
        pos.qty -= qty
        self._record(now, pos.ticker, action, pos.side, qty, gross(fills) / qty, fee, pnl, why)
        self.log(f"{action} {qty} {'UP/YES' if pos.side == 'yes' else 'DOWN/NO'} @ {gross(fills) / qty:.1f}c"
                 f"  {why}: {usd(pnl)} after fees, held {now - pos.entry_ts:.0f}s"
                 + (f"  ({pos.qty} still open)" if pos.qty else ""))
        if pos.qty == 0:
            self._close(pos, now, action)
        self._save()

    def _settle_closed(self, now: float):
        """Positions whose window has closed are paid out from Kalshi's official result."""
        for pos in list(self.positions.values()):
            if now < pos.close_ts + 4 or now - pos.last_settle_poll < 5:
                continue                # Kalshi posts the result a few seconds after the close
            pos.last_settle_poll = now
            m = self.kalshi.market(pos.ticker)
            if m.result not in ("yes", "no"):
                continue                # not determined yet; no fee is charged at settlement
            won = m.result == pos.side
            payout = pos.qty * 100.0 if won else 0.0
            pnl = payout - pos.cost_c
            self.cash_c += payout
            pos.realized_c += pnl
            self._record(now, pos.ticker, "SETTLE", pos.side, pos.qty, 100.0 if won else 0.0, 0, pnl,
                         f"result={m.result}")
            self.log(f"SETTLED {pos.ticker}: result {m.result.upper()} -> {pos.qty} "
                     f"{'UP/YES' if pos.side == 'yes' else 'DOWN/NO'} paid {usd(payout)}  P&L {usd(pnl)}")
            pos.qty, pos.cost_c = 0, 0.0
            self._close(pos, now, "SETTLE-WIN" if won else "SETTLE-LOSS")
            self._save()

    def _close(self, pos: Position, now: float, how: str):
        del self.positions[pos.ticker]
        self._last_exit_ts = now
        self.closed.append({"ticker": pos.ticker, "side": pos.side, "pnl_c": round(pos.realized_c, 4),
                            "fees_c": pos.fees_c, "how": how, "held_s": round(now - pos.entry_ts)})
        self.log(self.summary(one_line=True))

    # ----- reporting ------------------------------------------------------ #

    def _status(self, mkt: Market, book: Book, sig: dict | None, now: float):
        def q(side):
            return f"{cents(book.best_bid(side))}/{cents(book.best_ask(side))}"
        left = max(0, int(mkt.close_ts - now))
        line = f"T-{left // 60:02d}:{left % 60:02d}  UP {q('yes')}  DOWN {q('no')}"
        if sig:
            line += (f"  | spot {sig['spot']:,.2f} vs {sig['strike']:,.2f}"
                     f"  last min {sig['mom'] * sig['spot']:+,.0f}  model UP {sig['p_up'] * 100:.0f}%")
        pos = self.positions.get(mkt.ticker)
        if pos:
            bid = book.best_bid(pos.side)
            target = self._target(pos)
            line += (f"  | LONG {pos.qty} {'UP' if pos.side == 'yes' else 'DOWN'} @ {pos.entry_price:.1f}c"
                     f", bid {cents(bid)}c"
                     + (f", sells at {target:.0f}c" if target else ""))
        else:
            line += f"  | flat, cash {usd(self.cash_c)}"
        self.log(line)

    def summary(self, one_line: bool = False) -> str:
        n = len(self.closed)
        wins = [t["pnl_c"] for t in self.closed if t["pnl_c"] > 0]
        losses = [t["pnl_c"] for t in self.closed if t["pnl_c"] <= 0]
        net = sum(t["pnl_c"] for t in self.closed)
        if one_line:
            wr = f"{len(wins) / n * 100:.0f}%" if n else "n/a"
            return f"     total: {n} trades, {wr} winners, net {usd(net)}, cash {usd(self.cash_c)}"
        avg_w = sum(wins) / len(wins) if wins else 0.0
        avg_l = -sum(losses) / len(losses) if losses else 0.0
        lines = [
            "================ PAPER RESULTS ================",
            f"closed trades      {n}   ({len(wins)} winners / {len(losses)} losers)",
            f"win rate           {len(wins) / n * 100:.1f}%" if n else "win rate           n/a",
            f"average winner     {usd(avg_w)}",
            f"average loser      {usd(-avg_l)}",
        ]
        if wins and losses:
            lines.append(f"break-even win rate at these sizes: {avg_l / (avg_l + avg_w) * 100:.1f}%"
                         "   <- your win rate has to beat THIS, not 50%")
        lines += [
            f"fees paid          {usd(self.fees_paid_c)}",
            f"net P&L            {usd(net)}",
            f"cash               {usd(self.cash_c)}   (started {usd(self.start_cash_c)})",
        ]
        for p in self.positions.values():
            lines.append(f"still open         {p.qty} {p.side.upper()} {p.ticker} (cost {usd(p.cost_c)})")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(
        description="Kalshi paper scalper: live prices, simulated fills, no real orders.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--series", default="KXBTC15M", help="Kalshi series ticker to trade")
    ap.add_argument("--asset", default=None, help="underlying asset, e.g. BTC (default: from the series)")
    ap.add_argument("--feeds", default=",".join(FEEDS),
                    help="exchanges blended into the index replica")
    ap.add_argument("--no-record", action="store_true",
                    help="do not write the per-second research log (data/ticks.csv)")
    ap.add_argument("--bankroll", type=float, default=1000.0, help="starting paper cash, $")
    ap.add_argument("--stake", type=float, default=100.0, help="max $ committed per trade (incl. fees)")
    ap.add_argument("--min-price", type=float, default=20.0,
                    help="only buy a side Kalshi prices at or above this many cents")
    ap.add_argument("--max-price", type=float, default=40.0,
                    help="...and at or below this many cents (20-40 = the underdog side; 42-58 = near 50/50)")
    ap.add_argument("--entry-window", type=float, default=6.0,
                    help="only enter during the first N minutes of a window")
    ap.add_argument("--min-edge", type=float, default=1.0,
                    help="required model edge in cents per contract, after the entry fee")
    ap.add_argument("--momentum", type=float, default=0.0,
                    help="extra fixed assumption: share of the last minute's move that carries on "
                         "(negative = reverses). Normally left at 0: the bot learns this from its own data")
    ap.add_argument("--min-profit", type=float, default=0.5,
                    help="take profit once the sale nets at least this many cents/contract after ALL fees")
    ap.add_argument("--exit", choices=["limit", "market"], default="limit",
                    help="limit: a sell order rests at the profit price from the moment of entry (how you "
                         "would really do it; no fee on most series). market: watch the bid and hit it "
                         "(pays the taker fee again, so it needs a bigger move)")
    ap.add_argument("--stop-loss", type=float, default=0.0,
                    help="sell if the bid falls this many cents below entry (0 = hold losers to settlement)")
    ap.add_argument("--exit-before-close", type=float, default=0.0,
                    help="dump any open position this many seconds before the close (0 = hold to settlement)")
    ap.add_argument("--max-spread", type=float, default=3.0, help="skip entries when bid/ask is wider, cents")
    ap.add_argument("--slippage", type=float, default=1.0, help="limit = seen ask + this many cents")
    ap.add_argument("--max-entries", type=int, default=3, help="max round trips per window")
    ap.add_argument("--cooldown", type=float, default=20.0, help="seconds to wait after an exit")
    ap.add_argument("--poll", type=float, default=1.0, help="seconds between order-book checks")
    ap.add_argument("--latency-ms", type=float, default=150.0,
                    help="simulated delay between deciding and the order reaching the book")
    ap.add_argument("--status-every", type=float, default=15.0, help="seconds between status lines")
    ap.add_argument("--minutes", type=float, default=0.0, help="stop after N minutes (0 = run until Ctrl-C)")
    ap.add_argument("--data-dir", default=os.path.join(here, "data"), help="where state + trade log live")
    ap.add_argument("--reset", action="store_true", help="wipe saved state and start from --bankroll")
    ap.add_argument("--report", action="store_true", help="print results so far and exit")
    cfg = ap.parse_args(argv)
    cfg.series = cfg.series.upper()
    cfg.asset = (cfg.asset or ASSET.get(cfg.series) or "").upper()
    if not cfg.asset:
        ap.error(f"don't know the asset behind {cfg.series}; pass --asset, e.g. --asset BTC")
    cfg.feeds = [f for f in cfg.feeds.lower().split(",") if f]
    unknown = [f for f in cfg.feeds if f not in FEEDS]
    if unknown or not cfg.feeds:
        ap.error(f"unknown feed(s) {unknown}; choose from {', '.join(FEEDS)}")
    return cfg


def main(argv=None):
    cfg = parse_args(argv)
    bot = Bot(cfg, Kalshi(), Spot(cfg.asset, cfg.feeds))
    if cfg.report:
        print(bot.summary())
        return

    def on_signal(*_):
        bot.stop = True
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    bot.run()


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    main()
