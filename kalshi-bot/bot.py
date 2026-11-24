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
  1. Early in each window, while the contract still trades near 50/50, compare
     the market price with a fair-value estimate of P(up) computed from the
     live spot price, the window's strike and recent realised volatility.
  2. If one side is cheap versus the model by more than the entry fee plus a
     margin, buy that side (an immediate-or-cancel limit order against the book).
  3. Sell the moment the bid is high enough that the sale nets a profit AFTER
     both fees. Otherwise hold to settlement (or use --stop-loss).
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
from dataclasses import dataclass, field
from datetime import datetime, timezone

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
COINBASE_API = "https://api.exchange.coinbase.com"

# Kalshi series -> Coinbase product used as the live spot reference.
SPOT_PRODUCT = {
    "KXBTC15M": "BTC-USD",
    "KXETH15M": "ETH-USD",
    "KXSOL15M": "SOL-USD",
    "KXXRP15M": "XRP-USD",
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
    strike_live: bool = False     # strike confirmed from the live (uncached) endpoint

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

    def open_markets(self, series: str) -> list[Market]:
        data = http_get(f"{self.base}/markets",
                        {"series_ticker": series, "status": "open", "limit": 100})
        return [Market.from_api(m) for m in data.get("markets") or []]

    def market(self, ticker: str) -> Market:
        return Market.from_api(http_get(f"{self.base}/markets/{ticker}")["market"])

    def orderbook(self, ticker: str) -> Book:
        return parse_book(http_get(f"{self.base}/markets/{ticker}/orderbook"))


class Spot:
    """Live spot price from Coinbase (public). Every price we see is remembered,
    because Coinbase's candle endpoint lags by minutes; candles are only used
    for the volatility estimate."""

    def __init__(self, product: str, base: str = COINBASE_API):
        self.product, self.base = product, base
        self._ticks: deque[tuple[float, float]] = deque(maxlen=4000)   # (ts, price)
        self._candles: list[tuple[float, float, float]] = []   # (start_ts, open, close) oldest first
        self._candles_at = 0.0

    def price(self, now: float) -> float:
        t = http_get(f"{self.base}/products/{self.product}/ticker")
        bid, ask, last = _num(t.get("bid")), _num(t.get("ask")), _num(t.get("price"))
        px = (bid + ask) / 2 if bid and ask else last
        if not px:
            raise FeedError("spot ticker had no price")
        self._ticks.append((now, px))
        return px

    def avg_between(self, t0: float, t1: float, min_samples: int = 15) -> float | None:
        """Average of our own samples in [t0, t1]; None if we were not watching then."""
        xs = [p for ts, p in self._ticks if t0 <= ts <= t1]
        return sum(xs) / len(xs) if len(xs) >= min_samples else None

    def candles(self, now: float) -> list[tuple[float, float, float]]:
        if now - self._candles_at >= 30 or not self._candles:
            rows = http_get(f"{self.base}/products/{self.product}/candles", {"granularity": 60})
            # row = [start, low, high, open, close, volume], newest first
            self._candles = sorted((float(r[0]), float(r[3]), float(r[4])) for r in rows)
            self._candles_at = now
        return self._candles

    def sigma_1m(self, now: float, lookback: int = 60) -> float:
        closes = [c for _, _, c in self.candles(now)][-(lookback + 1):]
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


def prob_up(spot: float, strike: float, sigma_1m: float, secs_left: float) -> float:
    """P(price finishes above the strike), modelling the remaining move as a
    driftless random walk with the recently realised volatility."""
    if spot <= 0 or strike <= 0:
        return 0.5
    sd = max(sigma_1m, MIN_SIGMA_1M) * math.sqrt(max(secs_left, 1.0) / 60.0)
    return norm_cdf(math.log(spot / strike) / sd)


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


def usd(cents: float) -> str:
    return f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"


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
        self._entries: dict[str, int] = {}
        self._last_exit_ts = 0.0
        self._last_status = 0.0
        self.stop = False
        os.makedirs(cfg.data_dir, exist_ok=True)
        self.state_path = os.path.join(cfg.data_dir, "state.json")
        self.trades_path = os.path.join(cfg.data_dir, "trades.csv")
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
        self.out("")
        self.out(self.summary())

    def tick(self, now: float):
        spot = self.spot.price(now)          # sampled every tick: the history feeds the basis estimate
        self._settle_closed(now)
        mkt = self._current_market(now)
        if mkt is None:
            if now - self._last_status >= 30:
                self._last_status = now
                self.log(f"no open {self.cfg.series} market right now; waiting")
            return
        book = self.kalshi.orderbook(mkt.ticker)
        sig = self._signal(mkt, book, spot, now)
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
        if m and m.close_ts - now > 1 and now - self._markets_at < 60:
            return m
        if not m and now - self._markets_at < 3:
            return None
        self._markets_at = now
        live = [x for x in self.kalshi.open_markets(self.cfg.series)
                if x.open_ts <= now < x.close_ts - 1]
        new = min(live, key=lambda x: x.close_ts) if live else None
        if new and (not m or new.ticker != m.ticker):
            self.log(f"--- window {new.ticker}  closes {hhmmss(new.close_ts)} UTC  {new.title}")
        self.market = new
        return new

    # ----- signal --------------------------------------------------------- #

    def _signal(self, mkt: Market, book: Book, spot: float, now: float) -> dict | None:
        """Model probability for each side and its edge (in cents, net of the entry fee)
        versus the price we would actually have to pay."""
        sigma = self.spot.sigma_1m(now)
        strike, basis = self._strike(mkt, now)
        if not strike:
            return None
        p_up = prob_up(spot + basis, strike, sigma, mkt.close_ts - now)
        sig = {"spot": spot + basis, "strike": strike, "sigma": sigma, "p_up": p_up, "edge": {}, "fair": {}}
        for side, p in (("yes", p_up), ("no", 1 - p_up)):
            ask = book.best_ask(side)
            sig["fair"][side] = p * 100
            sig["edge"][side] = None if ask is None else p * 100 - ask - fee_cents(self.rate, 100, ask) / 100
        return sig

    def _strike(self, mkt: Market, now: float) -> tuple[float | None, float]:
        """(strike, basis). The strike is the index's 60-second average just before
        the window opened. Kalshi settles on the CF Benchmarks index while we watch
        Coinbase, so `basis` = strike minus OUR average over that same minute, and the
        model compares like with like. That needs the bot to have been watching at the
        open, so a window joined part-way through is observed but not traded."""
        if mkt.ticker in self._strike_cache:
            return self._strike_cache[mkt.ticker]
        ours = self.spot.avg_between(mkt.open_ts - 60, mkt.open_ts)
        if ours is None:
            if self._strike_note != mkt.ticker:
                self._strike_note = mkt.ticker
                self.log("    joined this window part-way through: watching only, trading starts next window")
            return (None, 0.0)
        if not mkt.strike_live:
            # The market list is cached for ~15s and the strike is only set moments
            # after the open, so read it from the live single-market endpoint.
            if now - self._strike_poll < 2:
                return (None, 0.0)
            self._strike_poll = now
            mkt.strike = self.kalshi.market(mkt.ticker).strike
            mkt.strike_live = bool(mkt.strike) or now - mkt.open_ts > 30
            if not mkt.strike_live:
                return (None, 0.0)
        strike = mkt.strike or ours                      # series with no published strike: spot at the open
        basis = strike - ours
        if abs(basis) > 0.003 * ours:                    # a gap that large means bad data, not basis
            basis = 0.0
        self._strike_cache = {mkt.ticker: (strike, basis)}
        self.log(f"    strike {strike:,.2f}   index-vs-Coinbase basis {basis:+,.2f}")
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
        side = max(("yes", "no"), key=lambda s: sig["edge"][s] if sig["edge"][s] is not None else -1e9)
        edge, ask, bid = sig["edge"][side], book.best_ask(side), book.best_bid(side)
        if edge is None or ask is None or bid is None:
            return
        if abs(ask - 50) > cfg.band or ask - bid > cfg.max_spread or edge < cfg.min_edge:
            return

        # Decision made. A real order takes time to reach the exchange, so wait
        # out the latency and fill against a FRESH book, as a limit order.
        limit = min(ask + cfg.slippage, 50 + cfg.band)
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
        else:
            # Marketable order: wait out the latency, then hit whatever bids are really
            # there, keeping only the part that is still profitable after the taker fee.
            self.clock.sleep(self.cfg.latency_ms / 1000.0)
            fills = sim_sell(self.kalshi.orderbook(mkt.ticker), pos.side, pos.qty, target, self.rate)
            while fills and gross(fills) - fees(fills) < filled(fills) * (pos.avg_cost + self.cfg.min_profit) - EPS:
                fills.pop()
        self._book_sale(pos, fills, now, "SELL", "profit")

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
            if now < pos.close_ts or now - pos.last_settle_poll < 5:
                continue
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
            b, a = book.best_bid(side), book.best_ask(side)
            return f"{'--' if b is None else f'{b:.0f}'}/{'--' if a is None else f'{a:.0f}'}"
        left = max(0, int(mkt.close_ts - now))
        line = f"T-{left // 60:02d}:{left % 60:02d}  UP {q('yes')}  DOWN {q('no')}"
        if sig:
            line += (f"  | spot {sig['spot']:,.2f} vs {sig['strike']:,.2f}"
                     f"  model UP {sig['p_up'] * 100:.0f}%")
        pos = self.positions.get(mkt.ticker)
        if pos:
            bid = book.best_bid(pos.side)
            target = self._target(pos)
            line += (f"  | LONG {pos.qty} {'UP' if pos.side == 'yes' else 'DOWN'} @ {pos.entry_price:.1f}c"
                     f", bid {'--' if bid is None else f'{bid:.0f}'}c"
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
    ap.add_argument("--spot", default=None, help="Coinbase product for the spot price (default: by series)")
    ap.add_argument("--bankroll", type=float, default=1000.0, help="starting paper cash, $")
    ap.add_argument("--stake", type=float, default=100.0, help="max $ committed per trade (incl. fees)")
    ap.add_argument("--band", type=float, default=8.0,
                    help="only enter while the price is within this many cents of 50")
    ap.add_argument("--entry-window", type=float, default=6.0,
                    help="only enter during the first N minutes of a window")
    ap.add_argument("--min-edge", type=float, default=1.0,
                    help="required model edge in cents per contract, after the entry fee")
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
    cfg.spot = cfg.spot or SPOT_PRODUCT.get(cfg.series)
    if not cfg.spot:
        ap.error(f"no default spot product for {cfg.series}; pass --spot, e.g. --spot BTC-USD")
    return cfg


def main(argv=None):
    cfg = parse_args(argv)
    bot = Bot(cfg, Kalshi(), Spot(cfg.spot))
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
