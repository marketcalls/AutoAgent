"""stg2_supertrend_3_10 - Supertrend, multiplier 3, period 10. Long and short.

PLAN.md Part 3, row 2. The only bidirectional strategy of the three, and the one
the selector picks for a strong trend of either sign. Stop-and-reverse: the bar
that exits a long is the bar that enters a short, so long_exit and short_entry
carry the same events, as do short_exit and long_entry.

The native stop is the supertrend line itself, which needs no ATR of its own -
the line already sits an ATR multiple away from price and on the correct side of
it, which is exactly what a stateless stop column cannot otherwise express for a
strategy that trades both ways.

Runtime facts measured against openalgo 2.0.3:
  - ta.supertrend(high, low, close, period=10, multiplier=3.0) returns a 2-TUPLE,
    (line, direction). Not a DataFrame and not three values. "Supertrend 3,10"
    means multiplier 3 and period 10, which is also the library default order.
  - The direction convention is INVERTED relative to the common one. Measured
    over 600 bars: direction == -1 on all 278 bars where close was above the
    line, and direction == +1 on all 313 bars where close was below it. So -1 is
    the UPTREND. Reading +1 as "up" reverses every trade this strategy takes and
    the code would still run, which is why the check is written out here.
  - The line and the direction both carry period-1 leading NaN, and the first
    non-NaN bar therefore looks like a direction change. warmup_bars() covers it.
  - The recursion is self-correcting: with a 500-bar tail the line matched the
    full-history run bit for bit at every sampled bar, and the direction never
    disagreed at any tail length tested down to 120 bars.
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
    resolve_params,
)

STRATEGY_ID: str = "stg2_supertrend_3_10"
DIRECTION: str = "both"
DEFAULT_PARAMS: dict[str, Any] = {
    "period": 10,
    "multiplier": 3.0,
}

# Direction as openalgo reports it, not as the textbooks write it.
_UPTREND: float = -1.0


def _resolved(params: Mapping[str, Any] | None) -> tuple[int, float]:
    values = resolve_params(DEFAULT_PARAMS, params)
    period = int_period(values["period"], "period")
    multiplier = positive_float(values["multiplier"], "multiplier")
    return period, multiplier


def warmup_bars(params: Mapping[str, Any] | None = None) -> int:
    """Bars discarded before a signal is trusted."""
    period, _ = _resolved(params)
    return EMA_WARMUP_MULTIPLE * period


def signals(
    frame: pd.DataFrame, params: Mapping[str, Any] | None = None
) -> pd.DataFrame:
    """Signal frame for `frame`, per the Part 3 contract.

    Args:
        frame: OHLC frame, cleaned by the data layer, ascending and unique.
        params: Overrides for DEFAULT_PARAMS. Unknown keys are refused.

    Returns:
        DataFrame indexed like `frame` with long_entry, long_exit, short_entry,
        short_exit (bool) and stop_price (float), the stop being the supertrend
        line.
    """
    require_frame(frame)
    period, multiplier = _resolved(params)
    warmup = EMA_WARMUP_MULTIPLE * period
    if len(frame) <= warmup:
        return empty_signals(frame.index)

    close = column(frame, "close")
    high = column(frame, "high")
    low = column(frame, "low")

    line, direction = ta.supertrend(high, low, close, period=period, multiplier=multiplier)
    line = np.asarray(line, dtype=float)
    direction = np.asarray(direction, dtype=float)

    # NaN never equals _UPTREND, so the warm-up region reads as "not up" and no
    # transition is manufactured at the first non-NaN bar.
    uptrend = direction == _UPTREND
    previous = np.concatenate(([False], uptrend[:-1]))
    raw_up = uptrend & ~previous
    raw_down = (~uptrend) & previous

    long_entry = np.asarray(ta.exrem(raw_up, raw_down), dtype=bool)
    long_exit = np.asarray(ta.exrem(raw_down, raw_up), dtype=bool)

    return assemble(
        frame.index,
        long_entry=long_entry,
        long_exit=long_exit,
        # Stop and reverse: the flip down closes the long and opens the short.
        short_entry=long_exit.copy(),
        short_exit=long_entry.copy(),
        stop_price=line,
        warmup=warmup,
    )
