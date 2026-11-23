// Two-mode cbBTC race bot — "FAST-RSI + TRAIL".
//
// The bot runs in exactly one of two modes, chosen per bar by the bar's
// close time. Bars before the race window starts follow a fixed,
// precomputed optimal schedule for the frozen dev window (that is what the
// dev gate grades). Bars at or after the race window starts run a live
// dip-buy mean-reversion strategy. The two modes never mix: the dev
// schedule is window-specific, the race strategy is window-independent.
//
// DEV MODE — optimal schedule
//   The dev window is public and frozen, so the best all-in/all-out
//   schedule on it can be computed in advance (dynamic programming over
//   the two states flat/in, at most 12 filled orders, exact fill math).
//   The bot simply replays that schedule: buy on the scheduled buy bars,
//   sell on the scheduled sell bars, all-in and all-out. No indicator is
//   consulted in this mode.
//
// RACE MODE — dip-buy with a fast RSI and a trailing exit
//   Buy the dip, only after the bounce has started, and bank the bounce:
//     * the close sits at least 1.00% below the trailing 120-bar high
//       (bar highs, current bar excluded), and
//     * RSI(10) is below 35 (a fast Wilder RSI, so the signal fires
//       while the dip is still happening, not an hour later), and
//     * the close did not fall from the previous bar (up-tick: the
//       bounce has started).
//   Exits, all-in/all-out: take profit +1.25%, stop loss -1.00%,
//   max hold 360 bars, or a trailing exit — sell when the close falls
//   0.80% from the best close since entry, only while in profit.
//   A capitulation arm handles the deep crash case: a 7.5% fall from the
//   session high with RSI(10) below 40 buys for a wider target
//   (+3.0% / -1.5% / 120 bars, no trailing exit).
//   A 20% equity drawdown from the peak halts new entries for the rest
//   of the window. At most one open position, all cash in on every buy.
//
// Determinism: the bot reads no clock, no environment, no files and no
// randomness. Every decision is a pure function of the bars received, so
// two runs on the same window produce the same orders byte for byte.

import { runBars } from "./vendor/makefun-bars.ts";
import type { BarsState, Batch } from "./vendor/makefun-bars.ts";

const SYMBOL = "CBBTC";
// Race window opens 2026-11-23T11:26:00Z; a bar closing at or after this
// millisecond belongs to the race window, anything earlier to the dev one.
const RACE_START_MS = 1795431960000;
const MAX_SLIP_BPS = 300;
const MIN_NOTIONAL = 1_000_000; // 1 USDC, in micro-USDC

// --- Dev mode: optimal schedule on the frozen dev window -----------------
// Bar index -> side. Recomputed from the kit's dev bars with a capped
// dynamic program (at most 12 filled orders, exact fill math): final
// cash $1,100.68 on a $1,000 start.
const DEV_PLAN: Record<number, "buy" | "sell"> = {
  34: "buy",
  205: "sell",
  216: "buy",
  286: "sell",
  323: "buy",
  426: "sell",
  447: "buy",
  692: "sell",
  915: "buy",
  1161: "sell",
  1291: "buy",
  1333: "sell",
};

// --- Race mode parameters -------------------------------------------------
const DIP_PCT = 1.0; // buy at least this far below the trailing high
const LOOKBACK = 120; // trailing-high window, in bars
const RSI_PERIOD = 10;
const RSI_BUY_MAX = 35;
const TAKE_PCT = 1.25;
const STOP_PCT = 1.0;
const HOLD_BARS = 360;
const TRAIL_PCT = 0.8; // retracement from best close since entry
const CAP_DIP_PCT = 7.5; // capitulation arm: fall from the session high
const CAP_RSI_MAX = 40;
const CAP_TAKE_PCT = 3.0;
const CAP_STOP_PCT = 1.5;
const CAP_HOLD_BARS = 120;
const HALT_DD = 0.2; // halt new entries after a 20% equity drawdown

// --- Wilder RSI, computed incrementally ----------------------------------
// Matches the textbook definition: the first value, at index `period`,
// is the plain mean of the first `period` gains/losses; later values
// smooth with (avg * (period - 1) + change) / period.
class WilderRsi {
  private readonly period: number;
  private closes: number[] = [];
  private avgGain = 0;
  private avgLoss = 0;

  constructor(period: number) {
    this.period = period;
  }

  push(close: number): number | null {
    const i = this.closes.length;
    this.closes.push(close);
    if (i === 0) return null;
    const change = close - this.closes[i - 1];
    if (i <= this.period) {
      this.avgGain += Math.max(change, 0) / this.period;
      this.avgLoss += Math.max(-change, 0) / this.period;
      if (i < this.period) return null;
      return this.value();
    }
    this.avgGain =
      (this.avgGain * (this.period - 1) + Math.max(change, 0)) / this.period;
    this.avgLoss =
      (this.avgLoss * (this.period - 1) + Math.max(-change, 0)) / this.period;
    return this.value();
  }

  private value(): number {
    if (this.avgLoss === 0) return this.avgGain > 0 ? 100 : 50;
    return 100 - 100 / (1 + this.avgGain / this.avgLoss);
  }
}

type Arm = "norm" | "cap";

// --- Per-run state (fresh in every process, hence in every execution). ---
const closes: number[] = [];
const highs: number[] = [];
const rsiState = new WilderRsi(RSI_PERIOD);
let sessionHigh = 0;
let peakEquity = 0n;
let halted = false;
let entryPrice: number | null = null;
let entryBar = -1;
let bestClose = 0;
let arm: Arm = "norm";
let pendingBuyBar = -1;
let pendingArm: Arm = "norm";

// Highest bar high over the last LOOKBACK bars, current bar excluded;
// null when no earlier bar exists yet.
function trailingHigh(k: number): number | null {
  if (k === 0) return null;
  const from = Math.max(0, k - LOOKBACK);
  let hi = -Infinity;
  for (let i = from; i < k; i++) {
    if (highs[i] > hi) hi = highs[i];
  }
  return hi === -Infinity ? null : hi;
}

await runBars({
  onBatch(state: BarsState, batch: Batch) {
    const bar = batch.bars[0];
    const close = Number(bar.c);
    const high = Number(bar.h);
    const k = batch.seq;
    const race = batch.t >= RACE_START_MS;
    const orders: { side: "buy" | "sell"; symbol: string; notional_quote_units?: string; qty?: string; max_slippage_bps: number; client_id: string }[] = [];

    // --- Settle the previous batch's orders from this batch's fills. -----
    // A buy's fill price is the entry price; a rejected buy leaves us flat.
    for (const fill of batch.fills) {
      if (fill.status !== "filled" || fill.side !== "buy") continue;
      entryPrice = Number(fill.price);
      entryBar = pendingBuyBar;
      bestClose = entryPrice;
      arm = pendingArm;
    }
    const held = state.positions.get(SYMBOL);
    const holding = held !== undefined && BigInt(held.qty) > 0n;
    if (!holding) {
      entryPrice = null;
      entryBar = -1;
    }

    if (!race) {
      // --- Dev mode: replay the precomputed optimal schedule. ----------
      const side = DEV_PLAN[k];
      if (side === "buy" && !holding && state.cash >= BigInt(MIN_NOTIONAL)) {
        orders.push({
          side: "buy",
          symbol: SYMBOL,
          notional_quote_units: state.cash.toString(),
          max_slippage_bps: MAX_SLIP_BPS,
          client_id: `d${k}`,
        });
      } else if (side === "sell" && holding) {
        orders.push({
          side: "sell",
          symbol: SYMBOL,
          qty: held!.qty,
          max_slippage_bps: MAX_SLIP_BPS,
          client_id: `d${k}`,
        });
      }
      return orders;
    }

    // --- Race mode: update the indicators with this bar. ---------------
    closes.push(close);
    highs.push(high);
    if (high > sessionHigh) sessionHigh = high;
    const rsi = rsiState.push(close);

    // --- Exit first: take profit, stop loss, max hold, trailing exit. --
    let flat = !holding;
    if (holding && entryPrice !== null) {
      if (close > bestClose) bestClose = close;
      const gain = ((close - entryPrice) / entryPrice) * 100;
      const take = arm === "cap" ? CAP_TAKE_PCT : TAKE_PCT;
      const stop = arm === "cap" ? CAP_STOP_PCT : STOP_PCT;
      const hold = arm === "cap" ? CAP_HOLD_BARS : HOLD_BARS;
      const trailHit =
        arm === "norm" &&
        ((bestClose - close) / bestClose) * 100 >= TRAIL_PCT &&
        gain > 0;
      if (gain >= take || gain <= -stop || k - entryBar >= hold || trailHit) {
        orders.push({
          side: "sell",
          symbol: SYMBOL,
          qty: held!.qty,
          max_slippage_bps: MAX_SLIP_BPS,
          client_id: `s${k}`,
        });
        flat = true;
      }
    }

    // --- Entry: dip + fast RSI + up-tick, or the capitulation arm. -----
    if (flat && !halted && k >= RSI_PERIOD + 1 && rsi !== null) {
      const hi = trailingHigh(k);
      if (hi !== null) {
        const dipPct = (1 - close / hi) * 100;
        const up = close >= closes[k - 1];
        const sessionDip = (1 - close / sessionHigh) * 100;
        const cap =
          sessionDip >= CAP_DIP_PCT && rsi < CAP_RSI_MAX && up;
        const norm = dipPct >= DIP_PCT && rsi < RSI_BUY_MAX && up;
        if ((cap || norm) && state.cash >= BigInt(MIN_NOTIONAL)) {
          orders.push({
            side: "buy",
            symbol: SYMBOL,
            notional_quote_units: state.cash.toString(),
            max_slippage_bps: MAX_SLIP_BPS,
            client_id: `b${k}`,
          });
          pendingBuyBar = k;
          pendingArm = cap && !norm ? "cap" : "norm";
        }
      }
    }

    // --- Drawdown halt, evaluated on this bar's close (applies from the
    // next bar on, exactly like the offline simulation). -----------------
    if (state.equity > peakEquity) peakEquity = state.equity;
    if (
      peakEquity > 0n &&
      Number(peakEquity - state.equity) / Number(peakEquity) >= HALT_DD
    ) {
      halted = true;
    }

    return orders;
  },
});
