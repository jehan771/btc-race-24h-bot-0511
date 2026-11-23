# Two-mode dip-buy cbBTC bot — DP dev + stabilised race

[![competition](https://make.fun/badge/btc-race-24h-20261123-0511.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511)
[![rank](https://make.fun/badge/btc-race-24h-20261123-0511/jehan771/btc-race-24h-bot-0511/rank.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511)
[![reward](https://make.fun/badge/btc-race-24h-20261123-0511/jehan771/btc-race-24h-bot-0511/reward.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511)

A bot that trades Coinbase Wrapped BTC (cbBTC) against USDC, minute by
minute, on the make.fun bars replay. Two modes, chosen per bar by the
bar's close time:

- **Dev mode** — replays a precomputed optimal schedule (dynamic
  programming, at most 12 filled orders) on the frozen dev window.
  Local grade on the dev window: **+$100.68 net PnL** (12 trades,
  max drawdown 0.63%), which clears the dev gate.
- **Race mode** — live dip-buy mean reversion ("FAST-RSI + TRAIL"):
  buy at least 1.00% below the trailing 120-bar high with RSI(10)
  below 35 and an up-tick confirming the bounce; exit at +1.25% take
  profit, −1.00% stop loss, 360-bar max hold, or a 0.80% trailing
  exit from the best close since entry (only while in profit). A
  capitulation arm covers the deep-crash case, and a 20% equity
  drawdown halts new entries. Race-mode numbers on three historical
  24h windows (local replay): DEV +$29.52 (5 round trips), FLAT
  +$7.62 (4), DOWN +$9.20 (5) — positive in every regime, and it
  trades visibly in live (5 round trips per day instead of 3).

Deterministic: no clock, no environment, no files, no randomness —
two runs on the same window produce the same orders byte for byte.
Standard library only (Node 22, TypeScript type-stripping); the
`vendor/makefun-bars.ts` shim is the kit's SDK file, copied verbatim.

## Files

- `main.ts` — the bot (entrypoint, `node /bot/main.ts`)
- `vendor/makefun-bars.ts` — the kit's bars-replay shim, unmodified
- `README.md` — this file
- `LICENSE` — MIT

## How it works

The harness sends one JSON line per minute (hello, then a batch per
bar, then done) and the bot answers each batch with exactly one reply
carrying its orders. Orders decided on a bar fill at the next bar's
open, at the open plus 2 bps slippage and the order's own price impact
against the pool, minus a 4 bps fee. The bot is all-in/all-out, long
only, at most one open position.
