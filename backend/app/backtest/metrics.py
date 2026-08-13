"""Metrics computed from a trade list, and the two-window comparison table.

PLAN.md Part 4. Metrics come from the replay's trade list rather than from
vectorbt, because the replay is the path that mirrors live behaviour. vectorbt
produces the tearsheet and the cross-check; this produces the numbers the
selector actually reads.

Two windows, deliberately:

    long   90 sessions   stability
    short  15 sessions   recency

Their DISAGREEMENT is the useful signal. A strategy strong over 90 sessions and
weak over 15 is in a regime change, which is more informative than either number
alone.

Two traps this module exists to avoid:

1. Annualised ratios on an intraday equity curve are meaningless unless the
   period is right. A 5-minute frequency over 90 sessions reads as roughly 23
   calendar days to a naive annualiser, which inflates every ratio by about 4x.
   Sharpe here is therefore computed per SESSION and annualised on 252 sessions,
   not on wall-clock days.

2. A strategy with four trades in the short window has no measurable statistics.
   `trades` is reported alongside every ratio, and `is_significant` marks whether
   the number deserves to be believed at all.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Iterable, Sequence

from .replay import ReplayResult, Trade

# Below this many trades the ratios are noise. Not a hard rule, but the selector
# should defer to the long window when the short window is under it.
MIN_TRADES_FOR_SIGNIFICANCE = 20

TRADING_SESSIONS_PER_YEAR = 252


@dataclass
class Metrics:
    """Everything the selector needs about one strategy over one window."""

    strategy_id: str = ""
    window: str = ""
    sessions: int = 0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    scratches: int = 0
    win_rate: float = 0.0
    expectancy_r: float = 0.0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    profit_factor: float = 0.0
    gross_pnl: float = 0.0
    costs: float = 0.0
    net_pnl: float = 0.0
    return_pct: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe: float = 0.0
    trades_per_session: float = 0.0
    avg_bars_held: float = 0.0
    exit_reasons: dict[str, int] = field(default_factory=dict)
    refused: dict[str, int] = field(default_factory=dict)
    halted_sessions: int = 0

    @property
    def is_significant(self) -> bool:
        return self.trades >= MIN_TRADES_FOR_SIGNIFICANCE

    @property
    def is_viable(self) -> bool:
        """Positive expectancy AND enough trades to believe it.

        Deliberately strict. The selector's job is to pick between viable
        strategies, and when none are viable the correct answer is to trade
        nothing rather than to pick the least bad loser.
        """
        return self.is_significant and self.expectancy_r > 0 and self.net_pnl > 0

    def as_dict(self) -> dict:
        return asdict(self)


def _max_drawdown(equity: Sequence[float]) -> float:
    """Largest peak-to-trough fall on the cumulative curve, as a positive number."""
    peak = float("-inf")
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        worst = max(worst, peak - value)
    return worst


def _session_returns(trades: Sequence[Trade]) -> dict[date, float]:
    daily: dict[date, float] = {}
    for t in trades:
        if t.exit_time is None:
            continue
        day = t.exit_time.date()
        daily[day] = daily.get(day, 0.0) + t.net_pnl
    return daily


def compute(
    result: ReplayResult,
    allocation: float,
    *,
    window: str = "",
    scratch_r: float = 0.1,
) -> Metrics:
    """Reduce a replay result to the comparison row for one strategy."""
    trades = [t for t in result.trades if t.exit_time is not None]
    m = Metrics(
        strategy_id=result.strategy_id,
        window=window,
        sessions=result.sessions,
        trades=len(trades),
        refused=dict(result.refused),
        halted_sessions=result.halted_sessions,
    )
    if not trades:
        return m

    # Scratch trades are neither wins nor losses. Counting a near-flat trade as a
    # win inflates the win rate and hides how thin the edge is.
    wins = [t for t in trades if t.r_multiple > scratch_r]
    losses = [t for t in trades if t.r_multiple < -scratch_r]
    m.wins, m.losses = len(wins), len(losses)
    m.scratches = len(trades) - m.wins - m.losses

    decided = m.wins + m.losses
    m.win_rate = 100.0 * m.wins / decided if decided else 0.0

    rs = [t.r_multiple for t in trades]
    m.expectancy_r = sum(rs) / len(rs)
    m.avg_win_r = sum(t.r_multiple for t in wins) / len(wins) if wins else 0.0
    m.avg_loss_r = sum(t.r_multiple for t in losses) / len(losses) if losses else 0.0

    won = sum(t.net_pnl for t in trades if t.net_pnl > 0)
    lost = -sum(t.net_pnl for t in trades if t.net_pnl < 0)
    m.profit_factor = (won / lost) if lost > 0 else (float("inf") if won > 0 else 0.0)

    m.gross_pnl = sum(t.gross_pnl for t in trades)
    m.costs = sum(t.costs for t in trades)
    m.net_pnl = sum(t.net_pnl for t in trades)
    m.return_pct = 100.0 * m.net_pnl / allocation if allocation else 0.0

    equity: list[float] = []
    running = 0.0
    for t in trades:
        running += t.net_pnl
        equity.append(running)
    m.max_drawdown = _max_drawdown(equity)
    m.max_drawdown_pct = 100.0 * m.max_drawdown / allocation if allocation else 0.0

    # Per-session returns, annualised on 252 SESSIONS. Annualising an intraday
    # curve on wall-clock days is the trap noted in the module docstring.
    daily = _session_returns(trades)
    if len(daily) > 1:
        values = list(daily.values())
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        sd = math.sqrt(var)
        m.sharpe = (mean / sd) * math.sqrt(TRADING_SESSIONS_PER_YEAR) if sd > 0 else 0.0

    m.trades_per_session = len(trades) / result.sessions if result.sessions else 0.0

    held = [
        (t.exit_time - t.entry_time).total_seconds() / 300.0
        for t in trades
        if t.exit_time is not None
    ]
    m.avg_bars_held = sum(held) / len(held) if held else 0.0

    for t in trades:
        m.exit_reasons[t.exit_reason] = m.exit_reasons.get(t.exit_reason, 0) + 1

    return m


def format_table(rows: Iterable[Metrics]) -> str:
    """Fixed-width comparison table. ASCII only, no icons."""
    rows = list(rows)
    if not rows:
        return "(no results)"

    header = (
        f"{'strategy':<24} {'window':<6} {'trd':>4} {'win%':>6} {'exp R':>7} "
        f"{'PF':>6} {'net':>10} {'ret%':>7} {'maxDD%':>7} {'sharpe':>7} {'ok':>4}"
    )
    lines = [header, "-" * len(header)]
    for m in rows:
        pf = "inf" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
        lines.append(
            f"{m.strategy_id:<24} {m.window:<6} {m.trades:>4} {m.win_rate:>6.1f} "
            f"{m.expectancy_r:>+7.3f} {pf:>6} {m.net_pnl:>10,.0f} "
            f"{m.return_pct:>+7.1f} {m.max_drawdown_pct:>7.1f} {m.sharpe:>7.2f} "
            f"{'yes' if m.is_viable else 'no':>4}"
        )
    return "\n".join(lines)


def compare(
    results_by_window: dict[str, list[ReplayResult]],
    allocation: float,
    *,
    scratch_r: float = 0.1,
) -> list[Metrics]:
    """Build the full two-window comparison the selector reads."""
    out: list[Metrics] = []
    for window, results in results_by_window.items():
        for result in results:
            out.append(compute(result, allocation, window=window, scratch_r=scratch_r))
    out.sort(key=lambda m: (m.strategy_id, m.window))
    return out
