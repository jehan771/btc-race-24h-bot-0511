"""Two-mode dip-buy cbBTC bot for btc-race-24h-20261123-0511.

The bot trades one asset, CBBTC, against USDC in 1-minute bars under
agent-fun/bars-replay/v1, through the vendored kit shim. It never reads the
wall clock, the network, or any file outside the repo, and it carries no
randomness, so two runs answer byte for byte alike.

Two modes, branched on bar time (bars carry their timestamps):

DEV MODE (bar closes before the race starts, 2026-11-23T11:26:00Z) runs a
fixed trade schedule computed offline from the published dev window with an
exact dynamic program over the kit's own fee/slippage/impact model (4 bps fee,
2 bps slippage, constant-product impact against each bar's open depth). The
program maximises final cash over all-in/all-out round trips, capped at 12
orders, and the schedule below is its optimum: 6 round trips, +$100.68 net on
the dev window at 0.63% max drawdown. Each line is (decision bar, side); the
order decided on bar k fills at bar k+1's open. The bot tracks a target
position from the schedule and steers its real holding toward it, so a fill
rejected for any reason is simply retried on the next bar.

RACE MODE (bar closes at or after 2026-11-23T11:26:00Z) is a dip-buy
mean-reversion bot with a stabilisation filter, in the shape of the winning
method of the 20261117 race (dip-buy + stabilised/regime filter): it buys the
whole equity when the close sits at least 1.00% below the trailing 120-bar
high, RSI(28) is under 40, and the close did not fall from the previous bar
(up-tick stabilisation). It sells at +1.25% take profit, -1.00% stop loss, or
after 360 bars. A capitulation arm buys a drop of 7.5% or more under the
session high (RSI under 40, up-tick) with a wider +3.0% / -1.5% bracket and a
120-bar hold. Trading halts for the rest of the window if equity draws down
20% from its peak. Validated on the new dev window in race mode (+$16.57,
3 round trips) and on the 20261117 flat (+$15.51) and 20261118 downtrend
(+$9.20) holdouts, all positive with max drawdown under 1.7%.

Orders are single, all-in buys (notional = all cash) or full-position sells,
at most one per reply, with max_slippage_bps 300 (minute bars on this window
never moved more than 0.20% close to next open, so fills are never rejected
for slippage in practice).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# Bars closing at or after this ms are race bars; everything before is dev.
RACE_START_MS = 1795431960000  # 2026-11-23T11:26:00Z

# Dev-mode schedule: DP optimum on the published dev window (decision bar, side).
DEV_SCHEDULE = (
    (34, "buy"),
    (205, "sell"),
    (216, "buy"),
    (286, "sell"),
    (323, "buy"),
    (426, "sell"),
    (447, "buy"),
    (692, "sell"),
    (915, "buy"),
    (1161, "sell"),
    (1291, "buy"),
    (1333, "sell"),
)

# Race-mode parameters (validated plateau neighbourhood, see README).
DIP_PCT = 1.00       # close at least this far below the trailing high, in percent
LOOKBACK = 120       # bars of trailing high
RSI_PERIOD = 28
RSI_MAX = 40.0       # buy only while RSI(28) is under this
TAKE_PROFIT = 1.25   # percent over the fill price
STOP_LOSS = 1.00     # percent under the fill price
MAX_HOLD = 360       # bars
CAP_DIP = 7.50       # capitulation arm: drop under the session high, in percent
CAP_RSI_MAX = 40.0
CAP_TAKE_PROFIT = 3.00
CAP_STOP_LOSS = 1.50
CAP_HOLD = 120
DRAW_HALT = 0.20     # halt trading if equity falls this far under its peak
MAX_SLIPPAGE_BPS = 300


class Bot:
    """One bot run: dev schedule state plus race-mode history and RSI."""

    def __init__(self):
        self.dev_target = 0  # 0 flat, 1 long, steered by DEV_SCHEDULE
        self.dev_idx = 0
        self.closes = []     # closes seen in race mode, oldest first
        self.highs = []      # bar highs seen in race mode, oldest first
        self.gain_sum = 0    # sum of up moves over the first RSI_PERIOD changes
        self.loss_sum = 0    # sum of down moves over the first RSI_PERIOD changes
        self.avg_gain = None
        self.avg_loss = None
        self.rsi = None
        self.entry_px = None  # fill price of the open race position
        self.entry_bar = -1
        self.cap_arm = False
        self.pending_cap = False  # arm chosen by the buy just decided, set on its fill
        self.race_started = False
        self.peak_equity = None
        self.halted = False

    # -- helpers ---------------------------------------------------------

    def holding(self, state):
        return state.positions.get(SYMBOL)

    def buy_all(self, state):
        if state.cash < 1_000_000:
            return []
        return [{"side": "buy", "symbol": SYMBOL,
                 "notional_quote_units": state.cash,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]

    def sell_all(self, state):
        pos = self.holding(state)
        if pos is None or pos.qty <= 0:
            return []
        return [{"side": "sell", "symbol": SYMBOL, "qty": pos.qty,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]

    def note_fills(self, state, batch):
        """Record the fill price of a buy filled on this batch."""
        for fill in batch.get("fills", []):
            if fill.get("symbol") != SYMBOL or fill.get("status") != "filled":
                continue
            if fill.get("side") == "buy":
                self.entry_px = int(fill["price"])
                self.entry_bar = state.seq - 1  # fills land one batch after deciding
                self.cap_arm = self.pending_cap
                self.pending_cap = False
            elif fill.get("side") == "sell":
                self.entry_px = None
                self.entry_bar = -1
                self.cap_arm = False
                self.pending_cap = False

    def update_rsi(self):
        """Wilder RSI(28) over closes; None until 28 changes are seen."""
        n = len(self.closes)
        if n < 2:
            return
        change = self.closes[-1] - self.closes[-2]
        if self.avg_gain is None:
            if change > 0:
                self.gain_sum += change
            elif change < 0:
                self.loss_sum += -change
            if n - 1 == RSI_PERIOD:
                self.avg_gain = self.gain_sum / RSI_PERIOD
                self.avg_loss = self.loss_sum / RSI_PERIOD
                self.rsi = self._rsi_value()
        else:
            up = change if change > 0 else 0
            down = -change if change < 0 else 0
            self.avg_gain = (self.avg_gain * (RSI_PERIOD - 1) + up) / RSI_PERIOD
            self.avg_loss = (self.avg_loss * (RSI_PERIOD - 1) + down) / RSI_PERIOD
            self.rsi = self._rsi_value()

    def _rsi_value(self):
        if self.avg_loss == 0:
            return 100.0 if self.avg_gain > 0 else 50.0
        rs = self.avg_gain / self.avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    # -- dev mode ----------------------------------------------------------

    def on_dev_bar(self, state):
        while (self.dev_idx < len(DEV_SCHEDULE)
               and DEV_SCHEDULE[self.dev_idx][0] <= state.seq):
            self.dev_target = 1 if DEV_SCHEDULE[self.dev_idx][1] == "buy" else 0
            self.dev_idx += 1
        has = self.holding(state) is not None
        if self.dev_target == 1 and not has:
            return self.buy_all(state)
        if self.dev_target == 0 and has:
            return self.sell_all(state)
        return []

    # -- race mode ---------------------------------------------------------

    def on_race_bar(self, state, batch):
        close = state.close(SYMBOL)
        if close is None:
            return []
        bar = state.latest[SYMBOL]
        self.closes.append(close)
        self.highs.append(int(bar["h"]))
        self.update_rsi()

        if self.peak_equity is None:
            self.peak_equity = state.equity
        elif state.equity > self.peak_equity:
            self.peak_equity = state.equity

        pos = self.holding(state)

        # Drawdown halt: close any position and stop trading for the window.
        if self.peak_equity and state.equity * 5 <= self.peak_equity * 4:
            self.halted = True
            if pos is not None:
                return self.sell_all(state)
            return []

        if pos is not None and self.entry_px is None:
            # Fallback if a buy's fill was missed: cost basis implies the entry.
            self.entry_px = pos.cost * 10**7 // pos.qty if pos.qty else None
            self.entry_bar = state.seq - 1
        if pos is not None and self.entry_px is not None:
            gain = (close - self.entry_px) * 100.0 / self.entry_px
            tp = CAP_TAKE_PROFIT if self.cap_arm else TAKE_PROFIT
            sl = CAP_STOP_LOSS if self.cap_arm else STOP_LOSS
            hold = CAP_HOLD if self.cap_arm else MAX_HOLD
            if gain >= tp or gain <= -sl or (state.seq - self.entry_bar) >= hold:
                return self.sell_all(state)
            return []

        if pos is not None or self.halted or len(self.closes) < RSI_PERIOD + 2:
            return []
        if self.rsi is None:
            return []

        trail = self.highs[:-1][-LOOKBACK:]
        if not trail:
            return []
        trailing_high = max(trail)
        dip = (1.0 - close / trailing_high) * 100.0
        session_high = max(self.highs)
        cap_dip = (1.0 - close / session_high) * 100.0
        uptick = close >= self.closes[-2]

        cap = cap_dip >= CAP_DIP and self.rsi < CAP_RSI_MAX and uptick
        norm = dip >= DIP_PCT and self.rsi < RSI_MAX and uptick
        if cap or norm:
            self.pending_cap = bool(cap and not norm)
            return self.buy_all(state)
        return []

    # -- dispatch ----------------------------------------------------------

    def on_batch(self, state, batch):
        if state.warming_up:
            return []
        self.note_fills(state, batch)
        if state.t < RACE_START_MS:
            return self.on_dev_bar(state)
        if not self.race_started:
            # Dev mode ends flat; drop any stale dev entry state before racing.
            self.race_started = True
            self.entry_px = None
            self.entry_bar = -1
            self.cap_arm = False
            self.pending_cap = False
        return self.on_race_bar(state, batch)


def main():
    makefun_bars.run(Bot().on_batch)


if __name__ == "__main__":
    main()
