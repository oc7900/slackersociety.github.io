"""Offline tests for the fill simulator, fee maths and the trading loop.
Run:  python3 -m unittest -v   (from this folder). No network needed -- the
exchange and the clock are scripted fakes."""
import tempfile
import unittest

import bot as B


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def time(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


class FakeSpot:
    def __init__(self, clock, path):
        self.clock, self.path = clock, path     # path(t) -> spot price

    def price(self, now):
        return self.path(now)

    def sigma_1m(self, now):
        return 0.0005

    def avg_between(self, t0, t1, min_samples=15):
        return self.path(t1)


class FakeKalshi:
    """One 15-minute market whose YES bid/ask follow a script; 500 contracts a level."""

    def __init__(self, clock, open_ts, yes_bid, result="yes", strike=100_000.0, depth=500):
        self.clock, self.open_ts, self.close_ts = clock, open_ts, open_ts + 900
        self.yes_bid, self.result, self.strike, self.depth = yes_bid, result, strike, depth

    def fee_rate(self, series):
        return 0.07

    def _market(self):
        closed = self.clock.t >= self.close_ts + 30      # result posts 30s after the close
        return B.Market("T-1", "test", self.open_ts, self.close_ts, self.strike,
                        "active", self.result if closed else "")

    def open_markets(self, series):
        return [self._market()] if self.clock.t < self.close_ts else []

    def market(self, ticker):
        return self._market()

    def orderbook(self, ticker):
        yb = self.yes_bid(self.clock.t - self.open_ts)   # 1c wide market: YES ask = bid + 1
        return B.Book(yes=[(yb, self.depth), (yb - 1, self.depth)],
                      no=[(100 - yb - 1, self.depth), (100 - yb - 2, self.depth)])


def make_bot(kalshi, spot, clock, **over):
    args = ["--data-dir", tempfile.mkdtemp(), "--reset", "--status-every", "1e9"]
    for k, v in over.items():
        args += ["--" + k.replace("_", "-"), str(v)]
    lines = []
    bot = B.Bot(B.parse_args(args), kalshi, spot, clock=clock, out=lines.append)
    return bot, lines


class Fees(unittest.TestCase):
    def test_taker_fee_rounds_up_to_the_cent(self):
        self.assertEqual(B.fee_cents(0.07, 100, 50), 175)    # $1.75 on 100 contracts at 50c
        self.assertEqual(B.fee_cents(0.07, 1, 50), 2)        # 1.75c -> 2c
        self.assertEqual(B.fee_cents(0.07, 10, 20), 12)      # 11.2c -> 12c
        self.assertEqual(B.fee_cents(0.07, 100, 99), 7)      # 6.93c -> 7c

    def test_profit_target_covers_both_fees(self):
        # bought 100 @ 50c + $1.75 fee -> avg cost 51.75c. Selling at 54c nets 54 - 1.74 = 52.26c.
        self.assertEqual(B.profit_target(51.75, 100, 0.5, 0.07), 54)
        self.assertEqual(B.profit_target(51.75, 100, 0.0, 0.07), 54)
        self.assertEqual(B.profit_target(51.75, 100, 1.0, 0.07), 55)
        self.assertIsNone(B.profit_target(99.9, 100, 0.5, 0.07))


class Books(unittest.TestCase):
    def test_parse_cent_levels(self):
        b = B.parse_book({"orderbook": {"yes": [[40, 10], [48, 30]], "no": [[45, 5], [50, 20]]}})
        self.assertEqual(b.best_bid("yes"), 48)
        self.assertEqual(b.best_ask("yes"), 50)      # 100 - best NO bid
        self.assertEqual(b.best_bid("no"), 50)
        self.assertEqual(b.best_ask("no"), 52)       # 100 - best YES bid
        self.assertEqual(b.asks("yes"), [(50, 20), (55, 5)])

    def test_parse_fixed_point_dollar_levels(self):
        b = B.parse_book({"orderbook_fp": {"yes_dollars": [["0.4000", "10.00"], ["0.4800", "30.00"]],
                                           "no_dollars": [["0.5000", "20.00"]]}})
        self.assertEqual(b.bids("yes"), [(48.0, 30), (40.0, 10)])
        self.assertEqual(b.best_ask("yes"), 50.0)

    def test_live_api_shapes(self):
        # trimmed from real responses (2026-11-24): fixed-point strings, ascending, fractional sizes
        b = B.parse_book({"orderbook_fp": {
            "no_dollars": [["0.4470", "15.77"], ["0.4480", "4.14"], ["0.4490", "3027.00"], ["0.4500", "0.60"]],
            "yes_dollars": [["0.5200", "788.43"], ["0.5300", "4779.39"], ["0.5400", "1095.37"]]}})
        self.assertEqual(b.bids("yes")[0], (54.0, 1095))          # best bid is the LAST element
        self.assertEqual(b.asks("yes")[0], (55.1, 3027))          # the 0.60-contract level is ignored
        m = B.Market.from_api({
            "ticker": "KXBTC15M-26NOV240400-00", "title": "BTC price up in next 15 mins?",
            "open_time": "2026-11-24T08:45:00Z", "close_time": "2026-11-24T09:00:00Z",
            "floor_strike": 60303.97, "strike_type": "greater_or_equal", "status": "active", "result": ""})
        self.assertEqual((m.strike, m.close_ts - m.open_ts, m.result), (60303.97, 900.0, ""))

    def test_spot_history_average(self):
        s = B.Spot("BTC-USD")
        for i in range(60):
            s._ticks.append((1000.0 + i, 100.0 + i))
        self.assertAlmostEqual(s.avg_between(1000, 1059), 129.5)
        self.assertIsNone(s.avg_between(2000, 2060))              # was not watching then

    def test_empty_book(self):
        b = B.parse_book({"orderbook": {"yes": None, "no": None}})
        self.assertIsNone(b.best_bid("yes"))
        self.assertIsNone(b.best_ask("yes"))


class Fills(unittest.TestCase):
    def setUp(self):
        self.book = B.Book(yes=[(48, 30), (47, 100)], no=[(50, 20), (49, 100), (40, 1000)])

    def test_buy_walks_the_book_and_never_exceeds_displayed_size(self):
        fills = B.sim_buy(self.book, "yes", 10_000, 51, 0.07)
        self.assertEqual([(f.price, f.qty) for f in fills], [(50, 20), (51, 100)])   # stops at the limit

    def test_buy_stays_inside_budget_including_fees(self):
        fills = B.sim_buy(self.book, "yes", 5_000, 60, 0.07)
        self.assertLessEqual(B.gross(fills) + B.fees(fills), 5_000)
        self.assertEqual(B.filled(fills), 95)        # 20 @ 50 + 75 @ 51 = $48.25 + $1.67 fees

    def test_buy_no_fill_when_offer_is_above_limit(self):
        self.assertEqual(B.sim_buy(self.book, "yes", 10_000, 49, 0.07), [])

    def test_sell_hits_bids_down_to_min_price(self):
        fills = B.sim_sell(self.book, "yes", 80, 48, 0.07)
        self.assertEqual([(f.price, f.qty) for f in fills], [(48, 30)])              # partial fill
        fills = B.sim_sell(self.book, "yes", 80, 1, 0.07)
        self.assertEqual([(f.price, f.qty) for f in fills], [(48, 30), (47, 50)])


class Model(unittest.TestCase):
    def test_prob_up(self):
        self.assertAlmostEqual(B.prob_up(100, 100, 0.001, 600), 0.5)
        self.assertGreater(B.prob_up(100.2, 100, 0.0005, 600), 0.85)
        self.assertLess(B.prob_up(99.8, 100, 0.0005, 600), 0.15)
        # the same gap matters more with less time left
        self.assertGreater(B.prob_up(100.05, 100, 0.0005, 60), B.prob_up(100.05, 100, 0.0005, 840))


class Loop(unittest.TestCase):
    def run_scenario(self, yes_bid, spot_path, result, minutes=17, **over):
        clock = Clock()
        k = FakeKalshi(clock, clock.t, yes_bid, result)
        bot, lines = make_bot(k, FakeSpot(clock, spot_path), clock, minutes=minutes, **over)
        bot.run()
        return bot, lines

    def test_buys_cheap_side_then_sells_as_soon_as_it_is_in_profit(self):
        # Spot jumps above the strike 30s in; the market stays at 50/51 until 60s, then reprices to 58.
        bot, lines = self.run_scenario(
            yes_bid=lambda s: 50 if s < 60 else 58,
            spot_path=lambda t: 100_000 if t < 1_000_030 else 100_150,
            result="yes", max_entries=1)
        text = "\n".join(lines)
        self.assertIn("BUY", text)
        self.assertIn("SELL", text)
        (trade,) = bot.closed
        self.assertEqual(trade["side"], "yes")
        self.assertEqual(trade["how"], "SELL")
        # $100 stake: 192 @ 51c = $97.92 + $3.36 fee would bust the budget -> 189 contracts.
        buy_cost = 189 * 51 + B.fee_cents(0.07, 189, 51)
        sell_net = 189 * 58 - B.fee_cents(0.07, 189, 58)
        self.assertAlmostEqual(trade["pnl_c"], sell_net - buy_cost)
        self.assertAlmostEqual(bot.cash_c, 100_000 + sell_net - buy_cost)
        self.assertGreater(trade["pnl_c"], 0)
        self.assertEqual(bot.positions, {})

    def test_loser_is_held_and_settled_from_the_official_result(self):
        # Buys UP on a spot pop, then the market collapses and resolves NO.
        bot, lines = self.run_scenario(
            yes_bid=lambda s: 50 if s < 60 else 20,
            spot_path=lambda t: 100_000 if t < 1_000_030 else 100_150,
            result="no", max_entries=1)
        (trade,) = bot.closed
        self.assertEqual(trade["how"], "SETTLE-LOSS")
        buy_cost = 189 * 51 + B.fee_cents(0.07, 189, 51)
        self.assertAlmostEqual(trade["pnl_c"], -buy_cost)       # the whole stake, no settlement fee
        self.assertAlmostEqual(bot.cash_c, 100_000 - buy_cost)

    def test_winner_never_reaching_target_is_paid_100c_at_settlement(self):
        bot, _ = self.run_scenario(
            yes_bid=lambda s: 50, spot_path=lambda t: 100_000 if t < 1_000_030 else 100_150,
            result="yes", max_entries=1)
        (trade,) = bot.closed
        self.assertEqual(trade["how"], "SETTLE-WIN")
        self.assertAlmostEqual(bot.cash_c, 100_000 + 189 * 100 - (189 * 51 + B.fee_cents(0.07, 189, 51)))

    def test_stop_loss_exits_at_the_real_bid(self):
        bot, _ = self.run_scenario(
            yes_bid=lambda s: 50 if s < 60 else 38,
            spot_path=lambda t: 100_000 if t < 1_000_030 else 100_150,
            result="no", max_entries=1, stop_loss=10)
        (trade,) = bot.closed
        self.assertEqual(trade["how"], "STOP")
        self.assertAlmostEqual(
            trade["pnl_c"],
            189 * 38 - B.fee_cents(0.07, 189, 38) - (189 * 51 + B.fee_cents(0.07, 189, 51)))

    def test_buys_down_when_spot_drops(self):
        bot, _ = self.run_scenario(
            yes_bid=lambda s: 49 if s < 60 else 40,      # NO bid goes 50 -> 59
            spot_path=lambda t: 100_000 if t < 1_000_030 else 99_850,
            result="no", max_entries=1)
        (trade,) = bot.closed
        self.assertEqual((trade["side"], trade["how"]), ("no", "SELL"))
        self.assertGreater(trade["pnl_c"], 0)

    def test_no_trade_without_edge_or_outside_the_entry_window(self):
        # fair coin, fairly priced: nothing to do
        bot, _ = self.run_scenario(yes_bid=lambda s: 50, spot_path=lambda t: 100_000, result="yes")
        self.assertEqual(bot.closed, [])
        # edge appears only after minute 6 -> too late in the window
        bot, _ = self.run_scenario(
            yes_bid=lambda s: 50, spot_path=lambda t: 100_000 if t < 1_000_400 else 100_150, result="yes")
        self.assertEqual(bot.closed, [])
        # price already far from 50/50 -> skip
        bot, _ = self.run_scenario(
            yes_bid=lambda s: 70, spot_path=lambda t: 100_400, result="yes")
        self.assertEqual(bot.closed, [])

    def test_partial_fill_when_the_book_is_thin(self):
        clock = Clock()
        k = FakeKalshi(clock, clock.t, lambda s: 50 if s < 60 else 58, "yes", depth=40)
        bot, _ = make_bot(k, FakeSpot(clock, lambda t: 100_000 if t < 1_000_030 else 100_150), clock,
                          minutes=3, max_entries=1, slippage=0)
        bot.run()
        (trade,) = bot.closed
        # only 40 contracts were offered at 51c, so only 40 were bought
        self.assertAlmostEqual(trade["pnl_c"], 40 * 58 - B.fee_cents(0.07, 40, 58)
                               - (40 * 51 + B.fee_cents(0.07, 40, 51)))

    def test_state_survives_a_restart(self):
        clock = Clock()
        k = FakeKalshi(clock, clock.t, lambda s: 50, "yes")
        spot = FakeSpot(clock, lambda t: 100_000 if t < 1_000_030 else 100_150)
        bot, _ = make_bot(k, spot, clock, minutes=2, max_entries=1)
        bot.run()
        self.assertEqual(len(bot.positions), 1)
        args = ["--data-dir", bot.cfg.data_dir, "--minutes", "16", "--status-every", "1e9"]
        bot2 = B.Bot(B.parse_args(args), k, spot, clock=clock, out=lambda *_: None)
        self.assertEqual(len(bot2.positions), 1)
        bot2.run()
        self.assertEqual(bot2.closed[0]["how"], "SETTLE-WIN")


if __name__ == "__main__":
    unittest.main()
