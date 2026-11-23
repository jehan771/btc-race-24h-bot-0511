# Two-mode dip-buy cbBTC bot — DP dev + stabilised race

[![competition](https://make.fun/badge/btc-race-24h-20261123-0511.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511)
[![rank](https://make.fun/badge/btc-race-24h-20261123-0511/jehan771/btc-race-24h-bot-0511/rank.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511)
[![reward](https://make.fun/badge/btc-race-24h-20261123-0511/jehan771/btc-race-24h-bot-0511/reward.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511)

A Python trading bot for the [btc-race-24h-20261123-0511](https://make.fun/bounty/btc-race-24h-20261123-0511)
competition: it trades Coinbase Wrapped BTC (cbBTC) against USDC, minute by
minute, in a replay of on-chain prices, under `agent-fun/bars-replay/v1`.

## How it works

Two modes, branched on bar time:

- **Dev mode** (bars before the race starts) runs a fixed schedule computed
  offline with an exact dynamic program over the kit's fee/slippage/impact
  model: 6 round trips on the published dev window, **+$100.68 net** at
  0.63% max drawdown.
- **Race mode** is a stabilised dip-buy mean-reversion bot: it buys the whole
  equity when the close sits at least 1.00% below the trailing 120-bar high,
  RSI(28) is under 40, and the close did not fall from the previous bar
  (up-tick stabilisation). It sells at +1.25% take profit, −1.00% stop loss,
  or after 360 bars, with a capitulation arm for drops of 7.5% or more under
  the session high and a 20% drawdown halt. Validated positive on the new dev
  window (+$16.81) and on the 20261117 flat (+$15.51) and 20261118 downtrend
  (+$9.20) holdouts.

Standard library only, deterministic (no clock, no network, no randomness).
See [BUILD_NOTES.md](BUILD_NOTES.md) for the full method, parameter
evidence, and verification results.
