"""stg3_sma10_ema30 - SMA(10) crossing EMA(30). Long only by default.

PLAN.md Part 3, row 3. The slowest of the three and the one the selector picks
for a weak or mixed regime, where the fewest whipsaws matters more than speed.
Stop is 1.5 x ATR(14) with no moving-average leg.

OPEN DECISION, carried from PLAN.md Part 3 and Part 11
------------------------------------------------------
The plan leaves the direction for this strategy unspecified: "Long-only and
long-and-short produce materially different statistics and sizing. Decide before
the first backtest."

This module implements LONG ONLY as the default and exposes a `direction`
parameter accepting "long" or "both", so build step 4 can measure the two against
each other rather than argue about them. Long-only is the default because it is
the conservative reading - it halves the trade count, it removes short exposure
from a basket whose correlation is already the concern of Part 5, and a shorter
sizing surface is easier to defend at step 12. The decision is not made here; it
is made by the step 4 metrics table, and until then the default is the one that
risks less.

With direction="both" the stop must flip sides, because a stop below price is
worthless on a short. It follows the implied trend state (SMA above EMA means the
strategy would be long) rather than any position state, since the contract
requires this function to stay stateless.

Runtime facts:
  - ta.ema returns a finite value at bar 0 with no NaN warm-up while ta.sma
    leaves period-1 NaN, so the two legs of this crossover disagree about their
    own warm-up. That mismatch is precisely what produces a phantom cross at the
    start of a frame, and the warmup mask is what removes it.
  - ta.sma and ta.ema both raise ValueError when the series is shorter than the
    period, so the length guard below is load-bearing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd
from openalgo import ta

from .base import (
    EMA_WARMUP_MULTIPLE,
    assemble,
    column,
    empty_signals,
    int_period,
    positive_float,
    require_frame,
    resolve_direction,
    resolve_params,
)

STRATEGY_ID: str = "stg3_sma10_ema30"
# The module default. The `direction` parameter can widen it to "both" - see the
# open decision in the module docstring.
DIRECTION: str = "long"
DEFAULT_PARAMS: dict[str, Any] = {
    "sma_period": 10,
    "ema_period": 30,
    "atr_period": 14,
    "atr_mult": 1.5,
    "direction": "long",
}


def _resolved(params: Mapping[str, Any] | None) -> tuple[int, int, int, float, str]:
    values = resolve_params(DEFAULT_PARAMS, params)
    sma_period = int_period(values["sma_period"], "sma_period")
    ema_period = int_period(values["ema_period"], "ema_period")
    atr_period = int_period(values["atr_period"], "atr_period")
    atr_mult = positive_float(values["atr_mult"], "atr_mult")
    direction = resolve_direction(values["direction"])
    return sma_period, ema_period, atr_period, atr_mult, direction


def _warmup(sma_period: int, ema_period: int, atr_period: int) -> int:
    # SMA is a flat window and needs only its own period; the two recursive legs
    # need the convergence multiple.
    return max(
        sma_period,
        EMA_WARMUP_MULTIPLE * ema_period,
        EMA_WARMUP_MULTIPLE * atr_period,
    )


def warmup_bars(params: Mapping[str, Any] | None = None) -> int:
    """Bars discarded before a signal is trusted."""
    sma_period, ema_period, atr_period, _, _ = _resolved(params)
    return _warmup(sma_period, ema_period, atr_period)


def signals(
    frame: pd.DataFrame, params: Mapping[str, Any] | None = None
) -> pd.DataFrame:
    """Signal frame for `frame`, per the Part 3 contract.

    Args:
        frame: OHLC frame, cleaned by the data layer, ascending and unique.
        params: Overrides for DEFAULT_PARAMS, including direction, which accepts
            "long" (default) or "both". Unknown keys are refused.

    Returns:
        DataFrame indexed like `frame` with long_entry, long_exit, short_entry,
        short_exit (bool) and stop_price (float). With direction="long" the short
        columns are always False and the stop always sits below the close.
    """
    require_frame(frame)
    sma_period, ema_period, atr_period, atr_mult, direction = _resolved(params)
    warmup = _warmup(sma_period, ema_period, atr_period)
    if len(frame) <= warmup:
        return empty_signals(frame.index)

    close = column(frame, "close")
    high = column(frame, "high")
    low = column(frame, "low")

    fast = np.asarray(ta.sma(close, sma_period), dtype=float)
    slow = np.asarray(ta.ema(close, ema_period), dtype=float)
    atr = np.asarray(ta.atr(high, low, close, atr_period), dtype=float)

    raw_up = np.asarray(ta.crossover(fast, slow), dtype=bool)
    raw_down = np.asarray(ta.crossunder(fast, slow), dtype=bool)
    long_entry = np.asarray(ta.exrem(raw_up, raw_down), dtype=bool)
    long_exit = np.asarray(ta.exrem(raw_down, raw_up), dtype=bool)

    band = atr_mult * atr
    if direction == "both":
        # Stateless side selection: the stop sits on the side the trend implies,
        # because the function is not allowed to know which way the executor is
        # positioned.
        long_biased = fast > slow
        stop_price = np.where(long_biased, close - band, close + band)
        short_entry = long_exit.copy()
        short_exit = long_entry.copy()
    else:
        stop_price = close - band
        flat = np.zeros(len(frame), dtype=bool)
        short_entry = flat
        short_exit = flat.copy()

    return assemble(
        frame.index,
        long_entry=long_entry,
        long_exit=long_exit,
        short_entry=short_entry,
        short_exit=short_exit,
        stop_price=stop_price,
        warmup=warmup,
    )
