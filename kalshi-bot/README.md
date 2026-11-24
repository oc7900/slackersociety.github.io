# Kalshi paper scalper

A paper-trading bot for Kalshi's 15-minute "BTC price up or down" markets
(also ETH / SOL / XRP). It trades **fake money against the real, live order
book**: no account, no API key, and it never sends an order.

```
python3 bot.py                 # run until Ctrl-C  ($1,000 paper bankroll, $100 per trade)
python3 bot.py --report        # results so far
python3 bot.py --reset         # wipe saved state and start over
python3 bot.py --help          # every setting
python3 -m unittest            # offline tests (no network)
```

Python 3.9+, standard library only. State and the trade log live in `data/`
(`state.json`, `trades.csv`), so you can stop and restart it.

## What it does

1. **Waits for a new 15-minute window** and reads the strike (the "price to beat").
2. **Picks a side.** Each second it estimates the fair probability that BTC
   finishes above the strike, from the live Coinbase price, how far it is from
   the strike, how much time is left and how volatile the last hour was. If
   UP or DOWN is selling for less than that fair value by more than the
   entry fee plus a margin (`--min-edge`), that is the side it buys.
3. **Only early and only near 50/50**: first 6 minutes of the window
   (`--entry-window`), price within 8¢ of 50¢ (`--band`).
4. **Sells the instant it is in profit.** From the moment it buys, a sell order
   rests at the lowest price that is a profit after all fees (`--min-profit`,
   default ½¢ per contract). When the market reaches it, it is out.
5. **If the price never gets there**, the position is held to the close and paid
   $1 or $0 per contract from Kalshi's official result. (`--stop-loss 10` sells
   instead once the bid is 10¢ under the entry.)
6. Repeats, up to 3 round trips per window, window after window.

## Why the paper fills are realistic

- Buys lift the real resting offers, level by level, and never take more
  contracts than are actually displayed. Whole contracts only.
- A buy is a limit order placed *after* a simulated 150 ms delay, against a
  freshly fetched book. If the offer is gone by then, there is no fill.
- Kalshi's taker fee, `0.07 x contracts x P x (1 - P)` rounded up to the cent,
  is charged on every buy (the rate is read from Kalshi's series record).
- The resting sell is counted as filled only when bids at or above its price
  show up for the whole position, and it gets its own price, never the higher bid.
- Settlement uses the result Kalshi publishes for that exact market.

Where it is rougher than reality: it polls once a second, so a spike that
touches the sell price and vanishes inside a second is missed (this understates
wins), and your own orders would move the market slightly at larger sizes.

## Read this before trusting the idea with real money

"More than a 50% chance of being in profit at some point" is true, and it is not
enough. What matters is the win rate **versus the size of a win and a loss**:

- Buy 189 contracts at 51¢ ($100). The fee is $3.36, so the cost is 52.8¢ each.
- The first profitable exit is 54¢: a winner makes about **$2.30**.
- A loser held to the close loses the whole **$100**.
- So the break-even win rate is about **97.7%**, not 50%.

In a market that is priced fairly, a 51¢ contract touches 54¢ before the close
about 51/54 = 94% of the time. That is a lot of small wins, and still a losing
bot: roughly $3.40 lost per $100 trade, which is exactly the entry fee. A stop-loss
does not fix this; it trades fewer total wipe-outs for more medium losses.

The only thing that can make it profitable is the side-picking being genuinely
better than the market's price. This bot's model is sensible but simple, and
the other side of these trades is professional market makers with faster data.
That is what the paper run is for: `--report` prints your real win rate next to
the break-even win rate for your actual trades. Let it run for a few hundred
trades before drawing any conclusion.

## Settings worth knowing

| Flag | Default | Meaning |
|---|---|---|
| `--series` | `KXBTC15M` | also `KXETH15M`, `KXSOL15M`, `KXXRP15M` |
| `--bankroll` / `--stake` | 1000 / 100 | paper cash, and max $ per trade |
| `--band` | 8 | enter only within this many cents of 50¢ |
| `--entry-window` | 6 | enter only in the first N minutes |
| `--min-edge` | 1 | cents the model must say you are ahead, after the entry fee |
| `--min-profit` | 0.5 | cents per contract of net profit that triggers the sale |
| `--exit` | `limit` | `market` = hit the bid instead (pays the fee twice) |
| `--stop-loss` | 0 (off) | cents below entry at which to bail out |
| `--max-entries` | 3 | round trips per window |

The first window after start-up is watched but not traded: the bot needs to
have seen the price at the window's open to line Coinbase up with the index
Kalshi settles on.

Going live would need a Kalshi API key and order placement; none of that is
here on purpose.
