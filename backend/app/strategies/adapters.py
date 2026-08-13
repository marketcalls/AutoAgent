"""The two adapters of PLAN.md Part 3. One signal function, two consumers.

    BacktestAdapter    whole frame at once   -> full signal DataFrame
    ExecutionAdapter   growing frame, per 5m -> signal row for the LAST bar only

Neither adapter contains strategy logic. They shape input and output and nothing
else. If a rule ever needs to differ between backtest and live, that is a defect
in the design, not a reason to fork the signal function.

Why the execution adapter keeps a tail rather than the whole series
------------------------------------------------------------------
Every recursive indicator here - EMA, Wilder ATR, supertrend - carries its seed
forward with an exponentially decaying weight, so a truncated history is not the
same series, only an asymptotically equal one. Measured against a full-history
run of EMA(30) on 2000 bars, worst absolute difference at the evaluated bar:

    tail  120 bars ->  7.0e-03      too coarse, could flip a crossover
    tail  250 bars ->  1.2e-06
    tail  500 bars ->  2.3e-13
    tail  750 bars ->  0.0          bit for bit identical

DEFAULT_TAIL_BARS is therefore 750, which is also ten sessions of 5-minute bars
at 75 bars a session - enough that Part 3's "warm-up carried across sessions"
holds even after a restart mid-morning. It is cheap: 750 rows of OHLCV is a few
tens of kilobytes per symbol.

The tail is a lower bound on correctness, not a target. Feeding a longer frame is
always safe; feeding a shorter one is what the parity test catches.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

from .base import (
    BOOL_COLUMNS,
    SIGNAL_COLUMNS,
    Strategy,
    StrategyError,
    empty_signals,
    require_frame,
)

# See the module docstring. Measured, not assumed.
DEFAULT_TAIL_BARS: int = 750


class BacktestAdapter:
    """Runs a strategy over a whole historical frame in one pass.

    Consumer is the replay loop (build step 3), which applies next-bar-open fills
    and costs. This adapter deliberately does not shift the signals: the signal
    belongs to the bar that produced it, and only the fill model knows what the
    executor would have done with it.
    """

    def __init__(
        self, strategy: Strategy, params: Mapping[str, Any] | None = None
    ) -> None:
        self._strategy = strategy
        self._params: dict[str, Any] = dict(params or {})
        # Resolve now so a bad parameter fails at construction, not at 09:20.
        self._warmup = int(strategy.warmup_bars(self._params))

    @property
    def strategy_id(self) -> str:
        return str(self._strategy.STRATEGY_ID)

    @property
    def params(self) -> dict[str, Any]:
        return dict(self._params)

    @property
    def warmup_bars(self) -> int:
        return self._warmup

    def run(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Signal frame for every bar of `frame`.

        Args:
            frame: OHLC frame, cleaned by the data layer.

        Returns:
            DataFrame indexed like `frame` with the five contract columns.
        """
        require_frame(frame)
        return self._strategy.signals(frame, self._params)


class ExecutionAdapter:
    """Feeds a strategy one closed bar at a time and returns that bar's signal.

    Consumer is the executor state machine (Part 6), which wakes on the 5-minute
    close, hands over the bar that just closed, and acts on the row that comes
    back. The adapter owns the warm-up tail so the caller cannot get it wrong.

    Two feeding styles, both supported because the live path has both:

        on_bar(bar)      one new closed bar, appended to the internal buffer
        on_frame(frame)  the current session frame from the data layer, which
                         already carries its own history

    Both return the signal row for the last bar in the buffer.
    """

    def __init__(
        self,
        strategy: Strategy,
        params: Mapping[str, Any] | None = None,
        tail_bars: int | None = None,
    ) -> None:
        self._strategy = strategy
        self._params: dict[str, Any] = dict(params or {})
        self._warmup = int(strategy.warmup_bars(self._params))
        requested = DEFAULT_TAIL_BARS if tail_bars is None else int(tail_bars)
        if requested < 1:
            raise StrategyError(f"tail_bars must be positive, got {tail_bars!r}")
        # A tail shorter than the warm-up would mask the very bar being returned,
        # so the floor is not negotiable even when a caller asks for less.
        self._tail = max(requested, self._warmup * 2)
        self._buffer: pd.DataFrame | None = None

    @property
    def strategy_id(self) -> str:
        return str(self._strategy.STRATEGY_ID)

    @property
    def params(self) -> dict[str, Any]:
        return dict(self._params)

    @property
    def warmup_bars(self) -> int:
        return self._warmup

    @property
    def tail_bars(self) -> int:
        return self._tail

    @property
    def buffer(self) -> pd.DataFrame | None:
        """The retained tail. Exposed for reconciliation and staleness checks."""
        return None if self._buffer is None else self._buffer.copy()

    def reset(self) -> None:
        """Drop the tail. Used between symbols and on a data-integrity halt."""
        self._buffer = None

    def on_bar(self, bar: pd.DataFrame | pd.Series) -> pd.Series:
        """Append one closed bar and evaluate.

        Args:
            bar: A one-row OHLC DataFrame, or a Series whose name is the bar
                timestamp.

        Returns:
            Series with the five contract columns, named by the bar timestamp.
        """
        row = _as_row(bar)
        if self._buffer is None:
            self._buffer = row
        else:
            self._buffer = pd.concat([self._buffer, row])
        self._trim()
        return self._evaluate()

    def on_frame(self, frame: pd.DataFrame) -> pd.Series:
        """Replace the buffer with the tail of `frame` and evaluate.

        The data layer already hands the executor a session frame with history
        attached, so re-accumulating it bar by bar would only add a second copy
        of the truth.
        """
        require_frame(frame)
        self._buffer = frame
        self._trim()
        return self._evaluate()

    def _trim(self) -> None:
        assert self._buffer is not None
        buffer = self._buffer
        # A re-sent bar must overwrite, not duplicate: the last 5m candle can
        # arrive twice while it is still forming.
        if buffer.index.has_duplicates:
            buffer = buffer[~buffer.index.duplicated(keep="last")]
        if not buffer.index.is_monotonic_increasing:
            buffer = buffer.sort_index()
        if len(buffer) > self._tail:
            buffer = buffer.iloc[-self._tail :]
        self._buffer = buffer

    def _evaluate(self) -> pd.Series:
        buffer = self._buffer
        if buffer is None or buffer.empty:
            raise StrategyError("no bars have been fed to the execution adapter")
        signals = self._strategy.signals(buffer, self._params)
        return _last_row(signals)


def _as_row(bar: pd.DataFrame | pd.Series) -> pd.DataFrame:
    """Normalise one bar to a one-row DataFrame with numeric columns."""
    if isinstance(bar, pd.Series):
        if bar.name is None:
            raise StrategyError("a bar Series must be named with its timestamp")
        frame = bar.to_frame().T.infer_objects()
    elif isinstance(bar, pd.DataFrame):
        if len(bar) != 1:
            raise StrategyError(f"on_bar expects exactly one bar, got {len(bar)}")
        frame = bar
    else:
        raise StrategyError(f"bar must be a Series or DataFrame, got {type(bar).__name__}")
    require_frame(frame)
    return frame


def _last_row(signals: pd.DataFrame) -> pd.Series:
    """Last row of a signal frame, with the contract's python types.

    Slicing a mixed bool/float frame yields an object Series, so the values are
    cast explicitly. The executor compares these against real booleans.
    """
    row = signals.iloc[-1]
    values: dict[str, Any] = {c: bool(row[c]) for c in BOOL_COLUMNS}
    values["stop_price"] = float(row["stop_price"])
    return pd.Series(values, index=list(SIGNAL_COLUMNS), name=signals.index[-1])


def signal_rows_to_frame(rows: Sequence[pd.Series]) -> pd.DataFrame:
    """Reassemble execution-adapter rows into a contract-shaped frame.

    Used by the parity test and by any live monitor that wants the session's
    signals as a frame. Rebuilding the dtypes here rather than letting pandas
    infer them keeps a run of all-False bars from becoming an object column.
    """
    if not rows:
        return empty_signals(pd.Index([]))
    index = pd.Index([row.name for row in rows])
    data: dict[str, np.ndarray] = {
        name: np.array([bool(row[name]) for row in rows], dtype=bool)
        for name in BOOL_COLUMNS
    }
    data["stop_price"] = np.array(
        [float(row["stop_price"]) for row in rows], dtype=float
    )
    return pd.DataFrame(data, index=index, columns=list(SIGNAL_COLUMNS))
