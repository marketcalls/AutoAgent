"""The Part 3 strategy contract, and the helpers all three strategies share.

Every strategy module exposes the same surface:

    STRATEGY_ID     str
    DIRECTION       "long" or "both"
    DEFAULT_PARAMS  mapping
    warmup_bars(params)      -> int
    signals(frame, params)   -> DataFrame indexed like frame, columns
                                long_entry, long_exit, short_entry, short_exit
                                (bool) and stop_price (float)

Rules the contract enforces, from PLAN.md Part 3:

  - Signal on bar close, entry on next bar open. A signal at bar i may read bars
    0..i and nothing beyond. The adapters and the parity test exist to prove it.
  - Stateless and pure. Position state lives in the executor, never here.
  - int() coercion on every period before it reaches openalgo.ta.

Runtime facts measured against openalgo 2.0.3 on 2026-08-13. Each one has already
cost debugging time somewhere:

  - A period argument must be a Python int. ta.ema(close, 10.0) raises TypeError,
    and JSON decodes every number to float, so a period arriving from config or a
    tool call is a float unless something coerces it. int_period() is that
    something, and it is called on every period on every path.
  - ta.ema and ta.sma raise ValueError when the series is shorter than the period
    ("Period (10) cannot be greater than data length (5)"). They do not return an
    all-NaN series, so a caller must guard on length rather than inspect NaN.
  - ta.ema returns a finite value at bar 0. There is no NaN warm-up region at all.
    Those first values are seeded rather than converged, they cross each other
    freely, and they will manufacture entries that no live session could have
    taken. The warm-up mask applied by assemble() is the only thing standing
    between a strategy and those phantom signals - nothing in the library does it.
  - ta.sma, ta.atr and ta.supertrend do leave period-1 leading NaN. The three
    indicator families therefore disagree about warm-up, which is a second reason
    the mask is computed here rather than inferred from NaN.
  - ta is strictly causal. A run over frame[:i+1] equals a run over the whole
    frame at bar i, to the last bit - measured on ema, atr and supertrend across
    2000 bars. That exactness is what makes the parity gate achievable at all;
    without it the live path could only ever be approximately the measured one.
  - Truncating history does move a recursive indicator, because the seed decays
    rather than disappears. Measured drift at bar i for EMA(30) against the full
    run: 7.0e-03 with a 120-bar tail, 1.2e-06 at 250, 2.3e-13 at 500, and exactly
    0.0 at 750. Hence DEFAULT_TAIL_BARS in adapters.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

import numpy as np
import pandas as pd

# Column order is part of the contract. Consumers index by name, but the replay
# loop and vectorbt both read positionally in places.
SIGNAL_COLUMNS: tuple[str, ...] = (
    "long_entry",
    "long_exit",
    "short_entry",
    "short_exit",
    "stop_price",
)
BOOL_COLUMNS: tuple[str, ...] = SIGNAL_COLUMNS[:4]
OHLC_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")

DIRECTIONS: tuple[str, ...] = ("long", "both")

# An EMA seeded at bar 0 needs roughly three time constants before the seed's
# weight decays under a few percent. Wilder-smoothed series (ATR, supertrend)
# behave the same way. Three periods is the convergence heuristic used for every
# recursive indicator in this package; SMA needs only its own period.
EMA_WARMUP_MULTIPLE: int = 3


class StrategyError(ValueError):
    """A frame or parameter set that cannot produce a valid signal frame."""


@runtime_checkable
class Strategy(Protocol):
    """Structural type satisfied by each strategy module.

    A module, not a class, so that a strategy is a file and there is exactly one
    of them per strategy id.
    """

    STRATEGY_ID: str
    DIRECTION: str
    DEFAULT_PARAMS: Mapping[str, Any]

    def warmup_bars(self, params: Mapping[str, Any] | None = ...) -> int:
        ...

    def signals(
        self, frame: pd.DataFrame, params: Mapping[str, Any] | None = ...
    ) -> pd.DataFrame:
        ...


def int_period(value: Any, name: str) -> int:
    """Coerce a period to a Python int, or refuse it.

    openalgo.ta raises TypeError on a float period and JSON decodes numbers to
    float, so 14.0 arriving from a config file or a tool argument must become 14
    here rather than blow up three frames deeper.
    """
    if isinstance(value, bool):
        raise StrategyError(f"{name} must be a whole number, got {value!r}")
    try:
        as_float = float(value)
    except (TypeError, ValueError) as exc:
        raise StrategyError(f"{name} must be a whole number, got {value!r}") from exc
    if not as_float.is_integer():
        raise StrategyError(f"{name} must be a whole number, got {value!r}")
    period = int(as_float)
    if period < 1:
        raise StrategyError(f"{name} must be positive, got {period}")
    return period


def positive_float(value: Any, name: str) -> float:
    """Coerce a multiplier to float and require it to be strictly positive."""
    if isinstance(value, bool):
        raise StrategyError(f"{name} must be a number, got {value!r}")
    try:
        as_float = float(value)
    except (TypeError, ValueError) as exc:
        raise StrategyError(f"{name} must be a number, got {value!r}") from exc
    if not np.isfinite(as_float) or as_float <= 0:
        raise StrategyError(f"{name} must be positive and finite, got {value!r}")
    return as_float


def resolve_params(
    defaults: Mapping[str, Any], params: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Overlay caller parameters onto the defaults, refusing unknown keys.

    A misspelled key that is silently ignored produces a strategy running on its
    defaults while every log line claims otherwise, which is the kind of drift
    the whole two-adapter design exists to prevent.
    """
    resolved = dict(defaults)
    if not params:
        return resolved
    unknown = sorted(set(params) - set(defaults))
    if unknown:
        raise StrategyError(
            f"unknown parameter(s) {unknown}; known: {sorted(defaults)}"
        )
    resolved.update(params)
    return resolved


def resolve_direction(value: Any, name: str = "direction") -> str:
    """Validate a direction string against the two the contract allows."""
    if not isinstance(value, str) or value.lower() not in DIRECTIONS:
        raise StrategyError(f"{name} must be one of {list(DIRECTIONS)}, got {value!r}")
    return value.lower()


def require_frame(frame: Any) -> pd.DataFrame:
    """Check the input is an OHLC frame before any indicator touches it."""
    if not isinstance(frame, pd.DataFrame):
        raise StrategyError(f"frame must be a DataFrame, got {type(frame).__name__}")
    missing = [c for c in OHLC_COLUMNS if c not in frame.columns]
    if missing:
        raise StrategyError(f"frame is missing column(s) {missing}")
    return frame


def column(frame: pd.DataFrame, name: str) -> np.ndarray:
    """Return one column as a contiguous float64 array.

    Everything reaching ta goes through here, for two reasons. A frame rebuilt
    from a single bar Series comes back with object dtype, which ta cannot use.
    And feeding ta identical dtypes from both adapters removes one more way for
    the vectorised and incremental paths to disagree.
    """
    values = np.asarray(frame[name], dtype=float)
    if values.ndim != 1:
        raise StrategyError(f"column {name} is not one-dimensional")
    return values


def empty_signals(index: pd.Index) -> pd.DataFrame:
    """A contract-shaped frame with no signals and no stop.

    Returned whenever there are too few bars to evaluate. The frame is not an
    error: a live session genuinely has no opinion before its warm-up completes.
    """
    n = len(index)
    data: dict[str, np.ndarray] = {c: np.zeros(n, dtype=bool) for c in BOOL_COLUMNS}
    data["stop_price"] = np.full(n, np.nan, dtype=float)
    return pd.DataFrame(data, index=index, columns=list(SIGNAL_COLUMNS))


def assemble(
    index: pd.Index,
    *,
    long_entry: np.ndarray,
    long_exit: np.ndarray,
    short_entry: np.ndarray,
    short_exit: np.ndarray,
    stop_price: np.ndarray,
    warmup: int,
) -> pd.DataFrame:
    """Build the signal frame and blank its warm-up region.

    The mask is positional, so in the execution adapter it lands on the oldest
    bars of the tail window - bars the caller never reads - and in the backtest
    it lands on the start of history. Both paths mask the same count, which is
    what keeps them in step.
    """
    n = len(index)
    parts = {
        "long_entry": long_entry,
        "long_exit": long_exit,
        "short_entry": short_entry,
        "short_exit": short_exit,
    }
    data: dict[str, np.ndarray] = {}
    for name, values in parts.items():
        arr = np.asarray(values, dtype=bool)
        if arr.shape != (n,):
            raise StrategyError(f"{name} has length {arr.shape}, expected ({n},)")
        data[name] = arr
    stop = np.asarray(stop_price, dtype=float)
    if stop.shape != (n,):
        raise StrategyError(f"stop_price has length {stop.shape}, expected ({n},)")
    data["stop_price"] = stop

    if warmup > 0:
        mask = np.arange(n) < int(warmup)
        for name in BOOL_COLUMNS:
            data[name] = np.where(mask, False, data[name])
        data["stop_price"] = np.where(mask, np.nan, stop)

    return pd.DataFrame(data, index=index, columns=list(SIGNAL_COLUMNS))
