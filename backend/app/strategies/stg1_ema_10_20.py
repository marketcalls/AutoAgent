"""stg1_ema_10_20 - EMA(10) crossing EMA(20), long only.

PLAN.md Part 3, row 1. Entry when the fast EMA crosses above the slow EMA, exit
on the opposite cross. The fastest of the three, and the one the selector picks
for a strong upward trend.

The stop: "slow EMA, or 1.5 x ATR(14), whichever is WIDER". Wider means further
from price, and for a long that means LOWER, so the level is the minimum of the
two candidates - not the maximum. Taking the minimum also disposes of a case that
would otherwise need handling: on a bar where the slow EMA sits above the close,
the EMA stop would be an instant loss, and the ATR leg is always below the close,
so the minimum is never above price.

Runtime facts:
  - ta.ema returns a finite value at bar 0 with no NaN warm-up, so the seeded
    early values cross each other and would fire entries a live session could not
    have taken. warmup_bars() plus the mask in base.assemble is what removes them.
  - ta.ema raises ValueError when the series is shorter than the period, so the
    length guard below is load-bearing, not defensive.
  - ta.crossover already fires only on the transition bar. exrem is applied
    anyway: it costs one pass and it is the difference between a rule that holds
    for these two series and a rule that holds for any pair, including one whose
    definition changes later.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd
from openalgo import ta

from .base import (
    EMA_WARMUP_MULTIPLE,
    StrategyError,
    assemble,
    column,
    empty_signals,
    int_period,
    positive_float,
    require_frame,
    resolve_params,
)

STRATEGY_ID: str = "stg1_ema_10_20"
DIRECTION: str = "long"
DEFAULT_PARAMS: dict[str, Any] = {
    "fast": 10,
    "slow": 20,
    "atr_period": 14,
    "atr_mult": 1.5,
}


def _resolved(params: Mapping[str, Any] | None) -> tuple[int, int, int, float]:
    values = resolve_params(DEFAULT_PARAMS, params)
    fast = int_period(values["fast"], "fast")
    slow = int_period(values["slow"], "slow")
    atr_period = int_period(values["atr_period"], "atr_period")
    atr_mult = positive_float(values["atr_mult"], "atr_mult")
    if fast >= slow:
        raise StrategyError(f"fast ({fast}) must be shorter than slow ({slow})")
    return fast, slow, atr_period, atr_mult


def _warmup(slow: int, atr_period: int) -> int:
    # Both legs are recursive and seeded at bar 0, so both need the convergence
    # multiple. One definition, used by warmup_bars() and by signals(), because a
    # mask that disagrees with the declared warm-up is a parity failure waiting.
    return max(EMA_WARMUP_MULTIPLE * slow, EMA_WARMUP_MULTIPLE * atr_period)


def warmup_bars(params: Mapping[str, Any] | None = None) -> int:
    """Bars discarded before a signal is trusted."""
    _, slow, atr_period, _ = _resolved(params)
    return _warmup(slow, atr_period)


def signals(
    frame: pd.DataFrame, params: Mapping[str, Any] | None = None
) -> pd.DataFrame:
    """Signal frame for `frame`, per the Part 3 contract.

    Args:
        frame: OHLC frame, cleaned by the data layer, ascending and unique.
        params: Overrides for DEFAULT_PARAMS. Unknown keys are refused.

    Returns:
        DataFrame indexed like `frame` with long_entry, long_exit, short_entry,
        short_exit (bool) and stop_price (float). Short columns are always False.
    """
    require_frame(frame)
    fast, slow, atr_period, atr_mult = _resolved(params)
    warmup = _warmup(slow, atr_period)
    if len(frame) <= warmup:
        return empty_signals(frame.index)

    close = column(frame, "close")
    high = column(frame, "high")
    low = column(frame, "low")

    ema_fast = np.asarray(ta.ema(close, fast), dtype=float)
    ema_slow = np.asarray(ta.ema(close, slow), dtype=float)
    atr = np.asarray(ta.atr(high, low, close, atr_period), dtype=float)

    raw_entry = np.asarray(ta.crossover(ema_fast, ema_slow), dtype=bool)
    raw_exit = np.asarray(ta.crossunder(ema_fast, ema_slow), dtype=bool)
    long_entry = np.asarray(ta.exrem(raw_entry, raw_exit), dtype=bool)
    long_exit = np.asarray(ta.exrem(raw_exit, raw_entry), dtype=bool)

    # Wider stop = lower level for a long. See the module docstring.
    stop_price = np.minimum(ema_slow, close - atr_mult * atr)

    flat = np.zeros(len(frame), dtype=bool)
    return assemble(
        frame.index,
        long_entry=long_entry,
        long_exit=long_exit,
        short_entry=flat,
        short_exit=flat,
        stop_price=stop_price,
        warmup=warmup,
    )
