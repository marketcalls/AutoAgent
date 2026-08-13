"""Strategy layer. The one place a trading rule is defined.

`backtest/` and `executor/` both import from here and neither may reimplement a
rule. That single constraint is what the parity test in backend/tests/test_parity
enforces, and it is the reason PLAN.md Part 2 puts this package above both
consumers rather than inside either.

    from app.strategies import get_strategy, BacktestAdapter, ExecutionAdapter

    strategy = get_strategy("stg2_supertrend_3_10")
    signals  = BacktestAdapter(strategy).run(frame)          # research
    row      = ExecutionAdapter(strategy).on_bar(last_bar)   # live

Strategy ids are the attribution key that travels through OpenAlgo's `strategy`
field into the broker orderbook, so they are stable identifiers, not labels.
"""

from __future__ import annotations

from types import ModuleType

from . import stg1_ema_10_20, stg2_supertrend_3_10, stg3_sma10_ema30
from .adapters import (
    DEFAULT_TAIL_BARS,
    BacktestAdapter,
    ExecutionAdapter,
    signal_rows_to_frame,
)
from .base import (
    BOOL_COLUMNS,
    DIRECTIONS,
    OHLC_COLUMNS,
    SIGNAL_COLUMNS,
    Strategy,
    StrategyError,
    empty_signals,
)

_MODULES: tuple[ModuleType, ...] = (
    stg1_ema_10_20,
    stg2_supertrend_3_10,
    stg3_sma10_ema30,
)

STRATEGIES: dict[str, ModuleType] = {m.STRATEGY_ID: m for m in _MODULES}


def strategy_ids() -> list[str]:
    """Every registered strategy id, in the plan's order."""
    return list(STRATEGIES)


def get_strategy(strategy_id: str) -> ModuleType:
    """Look up a strategy module by id.

    Args:
        strategy_id: One of the ids in STRATEGIES.

    Returns:
        The strategy module, which satisfies the Strategy protocol.

    Raises:
        StrategyError: If the id is unknown. The message lists the known ids,
            because a locked morning plan naming a strategy that does not exist
            must fail loudly rather than default to something.
    """
    try:
        return STRATEGIES[strategy_id]
    except KeyError as exc:
        raise StrategyError(
            f"unknown strategy {strategy_id!r}; known: {strategy_ids()}"
        ) from exc


__all__ = [
    "BOOL_COLUMNS",
    "DEFAULT_TAIL_BARS",
    "DIRECTIONS",
    "OHLC_COLUMNS",
    "SIGNAL_COLUMNS",
    "STRATEGIES",
    "BacktestAdapter",
    "ExecutionAdapter",
    "Strategy",
    "StrategyError",
    "empty_signals",
    "get_strategy",
    "signal_rows_to_frame",
    "strategy_ids",
    "stg1_ema_10_20",
    "stg2_supertrend_3_10",
    "stg3_sma10_ema30",
]
