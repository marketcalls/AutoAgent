"""Portfolio replay. All basket symbols on one timeline, sharing one budget.

PLAN.md Part 4. The reason this exists rather than backtesting each symbol and
summing the results:

    Independent-and-sum overstates performance. It ignores the shared risk
    budget, the concurrent position cap, shared margin, correlated drawdowns
    landing on the same sessions, and the tie-break when several symbols signal
    on the same bar.

Every one of those makes the naive number better than reality, never worse.

This module is also the reference implementation of live behaviour. Where it and
vectorbt disagree, this wins, because this is the one that mirrors the executor:
next-bar entry, a native stop checked intrabar, a hard end-of-day flat, and the
same RiskBudget object the executor uses.

Hard-won facts encoded here:

- Signal on bar close, entry on the NEXT bar's open. Filling on the signal bar is
  lookahead and is the single most common reason a backtest beats live.
- The stop is checked against the bar's LOW for a long and HIGH for a short, not
  the close. A stop that only triggers on closes understates drawdown badly.
- When a bar's range covers both the stop and the exit signal, the STOP wins.
  Assuming the favourable fill is how a backtest quietly invents money.
- Costs are charged on both legs. At a 1.5R target on Indian intraday equity,
  unmodelled costs turn a losing system into a winning one on paper.
- Positions are force-flat at squareoff_time regardless of signal state, because
  MIS positions are closed by the broker anyway and at a worse price.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Mapping

import pandas as pd

from ..config import Settings
from ..risk.budget import Position, RiskBudget, RunState
from ..risk.sizing import quantity_for, worst_case_loss
from ..strategies import BacktestAdapter, get_strategy

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Costs:
    """Round-trip cost model for NSE cash intraday, itemised.

    MEASURED CORRECTION, step 3. The first version of this class charged
    brokerage as a flat percentage and rolled every tax into one sell-side
    number. On 10 lakh of notional it produced 2,850 rupees a round trip against
    an actual 398 - overstating costs by 7.2x, which made all three strategies
    look unprofitable when the real question was still open.

    The error was brokerage. Indian discount brokers charge
    min(0.03 percent, 20 rupees) PER ORDER, and at any realistic intraday size
    the 20-rupee cap binds, not the percentage. Percentage brokerage on a 10 lakh
    position is 300 rupees a leg; the truth is 20.

    Everything is itemised rather than lumped so the next person can see which
    component dominates. At 10 lakh notional, STT alone is 63 percent of the bill.

    Slippage is kept separate and deliberately pessimistic. Entry is a LIMIT at
    touch, but every stop and the 15:10 squareoff are MARKET orders.
    """

    brokerage_pct: float = 0.0003        # 0.03 percent per order...
    brokerage_cap: float = 20.0          # ...or 20 rupees, whichever is LOWER
    stt_pct: float = 0.00025             # 0.025 percent, SELL side only
    exchange_txn_pct: float = 0.0000297  # both sides
    stamp_duty_pct: float = 0.00003      # 0.003 percent, BUY side only
    sebi_pct: float = 0.000001           # both sides
    gst_pct: float = 0.18                # on brokerage plus exchange charges
    slippage_pct: float = 0.0005         # per leg, market-order assumption

    def _brokerage(self, notional: float) -> float:
        return min(notional * self.brokerage_pct, self.brokerage_cap)

    def _statutory(self, notional: float, *, is_buy: bool) -> float:
        brokerage = self._brokerage(notional)
        txn = notional * self.exchange_txn_pct
        stt = 0.0 if is_buy else notional * self.stt_pct
        stamp = notional * self.stamp_duty_pct if is_buy else 0.0
        sebi = notional * self.sebi_pct
        gst = (brokerage + txn) * self.gst_pct
        return brokerage + txn + stt + stamp + sebi + gst

    def entry_cost(self, notional: float) -> float:
        return self._statutory(notional, is_buy=True) + notional * self.slippage_pct

    def exit_cost(self, notional: float) -> float:
        return self._statutory(notional, is_buy=False) + notional * self.slippage_pct

    def round_trip(self, notional: float) -> float:
        """Total cost of a full round trip, for reporting and sanity checks."""
        return self.entry_cost(notional) + self.exit_cost(notional)


@dataclass
class Trade:
    """One completed round trip. The unit every metric is computed from."""

    symbol: str
    side: str
    strategy_id: str
    entry_time: datetime
    entry_price: float
    quantity: int
    stop_price: float
    exit_time: datetime | None = None
    exit_price: float = 0.0
    exit_reason: str = ""
    gross_pnl: float = 0.0
    costs: float = 0.0
    net_pnl: float = 0.0
    r_multiple: float = 0.0
    risk_amount: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.exit_time is None


@dataclass
class ReplayResult:
    trades: list[Trade] = field(default_factory=list)
    equity: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    refused: dict[str, int] = field(default_factory=dict)
    sessions: int = 0
    halted_sessions: int = 0
    strategy_id: str = ""

    @property
    def refusal_total(self) -> int:
        return sum(self.refused.values())


def _bar_session(ts: pd.Timestamp) -> date:
    return ts.date()


def _stop_hit(side: str, stop: float, low: float, high: float) -> bool:
    """Did this bar's RANGE reach the stop.

    Checking the close instead of the range is the classic backtest flatter: a
    stop that only fires on closes lets a position survive a spike that would
    have taken it out live.
    """
    return low <= stop if side == "BUY" else high >= stop


class PortfolioReplay:
    """Replay one strategy across the whole basket on a shared budget.

    Constructed per strategy. The selector at step 5 runs one of these for each
    of the three strategies and compares the results.
    """

    def __init__(
        self,
        settings: Settings,
        strategy_id: str,
        *,
        params: Mapping[str, Any] | None = None,
        costs: Costs | None = None,
    ) -> None:
        self.settings = settings
        self.strategy_id = strategy_id
        self.params = dict(params or {})
        self.costs = costs or Costs()
        self.strategy = get_strategy(strategy_id)

    # ------------------------------------------------------------------ signals

    def _signals_for(self, frames: Mapping[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        """Vectorised signals per symbol.

        Safe to compute up front only because the parity test proves the
        vectorised path and the bar-at-a-time path agree exactly. Without that
        guarantee this would have to run incrementally and take far longer.
        """
        adapter = BacktestAdapter(self.strategy, self.params)
        return {sym: adapter.run(frame) for sym, frame in frames.items()}

    # ------------------------------------------------------------------- replay

    def run(self, frames: Mapping[str, pd.DataFrame]) -> ReplayResult:
        """Walk one shared timeline across every symbol in the basket."""
        if not frames:
            return ReplayResult(strategy_id=self.strategy_id)

        signals = self._signals_for(frames)

        # The union of every symbol's index, so a bar missing from one symbol
        # does not shift another symbol's clock.
        timeline = sorted(set().union(*(f.index for f in frames.values())))

        # Priority order is the basket order from config. It is the tie-break
        # when several symbols signal on the same bar and the budget cannot fund
        # them all. Deterministic on purpose - ranking by "signal strength"
        # would be a new parameter with no measured justification.
        priority = [s for s in self.settings.basket_symbols if s in frames]
        priority += [s for s in frames if s not in priority]

        result = ReplayResult(strategy_id=self.strategy_id)
        open_trades: dict[str, Trade] = {}
        pending: dict[str, dict[str, Any]] = {}  # entries armed for the next bar
        equity_points: list[tuple[pd.Timestamp, float]] = []
        cumulative = 0.0

        budget: RiskBudget | None = None
        current_session: date | None = None
        squareoff: time = self.settings.squareoff_time
        start_t: time = self.settings.start_time
        end_t: time = self.settings.end_time

        for ts in timeline:
            session = _bar_session(ts)

            if session != current_session:
                # A new session. Force-flat anything still open at the previous
                # close, then reset the budget: the daily limit and the streak
                # are per session, not per backtest.
                if open_trades:
                    for sym, tr in list(open_trades.items()):
                        px = self._last_close(frames[sym], ts)
                        cumulative += self._close_trade(tr, ts, px, "session_rollover", budget)
                        result.trades.append(tr)
                        open_trades.pop(sym, None)
                if budget is not None and budget.state is RunState.HALTED:
                    result.halted_sessions += 1
                current_session = session
                # persist=False: this is a simulation. See the note on RiskBudget.persist -
                # an earlier version wrote a halt file per simulated session into the
                # LIVE data directory and stopped the real executor from starting.
                budget = RiskBudget(settings=self.settings, trading_date=session, persist=False)
                result.sessions += 1
                pending.clear()

            assert budget is not None
            bar_time = ts.time()

            # ---- marks for every open position, for the MTM budget check ----
            marks = {
                sym: float(frames[sym].loc[ts, "close"])
                for sym in open_trades
                if ts in frames[sym].index
            }

            # ---- 1. exits first, always ----
            for sym in list(open_trades):
                frame = frames[sym]
                if ts not in frame.index:
                    continue
                bar = frame.loc[ts]
                tr = open_trades[sym]
                sig = signals[sym].loc[ts] if ts in signals[sym].index else None

                exit_px: float | None = None
                reason = ""

                if bar_time >= squareoff:
                    # MIS is closed by the broker anyway, and at a worse price.
                    exit_px, reason = float(bar["close"]), "squareoff"
                elif _stop_hit(tr.side, tr.stop_price, float(bar["low"]), float(bar["high"])):
                    # The stop wins any bar whose range covers both the stop and
                    # an exit signal. Assuming the favourable fill is how a
                    # backtest invents money that live trading will not produce.
                    exit_px, reason = tr.stop_price, "stop"
                elif sig is not None:
                    wants_out = bool(sig["long_exit"]) if tr.side == "BUY" else bool(sig["short_exit"])
                    if wants_out:
                        exit_px, reason = float(bar["close"]), "signal"

                if exit_px is not None:
                    cumulative += self._close_trade(tr, ts, exit_px, reason, budget)
                    result.trades.append(tr)
                    open_trades.pop(sym, None)
                    marks.pop(sym, None)

            # ---- 2. fill entries armed on the PREVIOUS bar ----
            for sym in list(pending):
                if sym in open_trades:
                    pending.pop(sym, None)
                    continue
                frame = frames[sym]
                if ts not in frame.index:
                    continue
                armed = pending.pop(sym)
                if _bar_session(ts) != armed["session"]:
                    continue  # never carry an entry across the overnight gap

                fill = float(frame.loc[ts, "open"])
                trade = self._try_open(sym, armed, fill, ts, budget, marks, result)
                if trade is not None:
                    open_trades[sym] = trade

            # ---- 3. arm entries for the NEXT bar ----
            # Signal on close, entry on next open. Filling here would be
            # lookahead and would flatter every result in this file.
            if start_t <= bar_time < end_t:
                breaker = budget.check_breakers(marks, now=datetime.combine(session, bar_time))
                if breaker.allowed and budget.state.may_open:
                    for sym in priority:
                        if sym in open_trades or sym in pending:
                            continue
                        if ts not in signals[sym].index:
                            continue
                        sig = signals[sym].loc[ts]
                        side = "BUY" if bool(sig["long_entry"]) else (
                            "SELL" if bool(sig["short_entry"]) else ""
                        )
                        if not side:
                            continue
                        stop = sig["stop_price"]
                        if pd.isna(stop):
                            result.refused["no_stop"] = result.refused.get("no_stop", 0) + 1
                            continue
                        pending[sym] = {"side": side, "stop": float(stop), "session": session}

            equity_points.append((ts, cumulative))

        # Anything still open at the end of the data is closed at the last price,
        # so an unrealised position cannot inflate the result.
        for sym, tr in list(open_trades.items()):
            last_ts = frames[sym].index[-1]
            cumulative += self._close_trade(
                tr, last_ts, float(frames[sym]["close"].iloc[-1]), "end_of_data", budget
            )
            result.trades.append(tr)

        result.equity = pd.Series(
            [v for _, v in equity_points],
            index=pd.DatetimeIndex([t for t, _ in equity_points]),
            dtype=float,
        )
        return result

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _last_close(frame: pd.DataFrame, before: pd.Timestamp) -> float:
        prior = frame.loc[frame.index < before]
        source = prior if not prior.empty else frame
        return float(source["close"].iloc[-1])

    def _try_open(
        self,
        symbol: str,
        armed: Mapping[str, Any],
        fill: float,
        ts: pd.Timestamp,
        budget: RiskBudget,
        marks: Mapping[str, float],
        result: ReplayResult,
    ) -> Trade | None:
        """Size the trade, ask the budget, and open it if allowed.

        Every refusal is counted. A high refusal rate is the diagnostic that says
        the basket is too large for the allocation, or the risk fraction is too
        high - meaning the measured strategy is not the one that would trade.
        """
        side = str(armed["side"])
        stop = float(armed["stop"])

        # Direction sanity. A long whose stop sits above the fill has no risk
        # distance and would divide badly.
        if (side == "BUY" and stop >= fill) or (side == "SELL" and stop <= fill):
            result.refused["bad_stop_side"] = result.refused.get("bad_stop_side", 0) + 1
            return None

        sized = quantity_for(
            self.settings,
            entry_price=fill,
            stop_price=stop,
            risk_fraction=self.settings.risk_fraction_base,
            budget_cap=budget.remaining_budget(marks),
        )
        if not sized.ok:
            result.refused["sizing"] = result.refused.get("sizing", 0) + 1
            return None

        loss = worst_case_loss(sized.quantity, fill, stop)
        decision = budget.can_open(
            symbol, loss, marks, now=datetime.combine(ts.date(), ts.time())
        )
        if not decision.allowed:
            result.refused[decision.code] = result.refused.get(decision.code, 0) + 1
            return None

        budget.open_position(Position(symbol, sized.quantity, fill, stop, side))
        return Trade(
            symbol=symbol,
            side=side,
            strategy_id=self.strategy_id,
            entry_time=ts.to_pydatetime(),
            entry_price=fill,
            quantity=sized.quantity,
            stop_price=stop,
            risk_amount=loss,
        )

    def _close_trade(
        self,
        trade: Trade,
        ts: pd.Timestamp,
        price: float,
        reason: str,
        budget: RiskBudget | None,
    ) -> float:
        direction = 1 if trade.side == "BUY" else -1
        gross = (price - trade.entry_price) * trade.quantity * direction
        cost = (
            self.costs.entry_cost(trade.entry_price * trade.quantity)
            + self.costs.exit_cost(price * trade.quantity)
        )
        trade.exit_time = ts.to_pydatetime()
        trade.exit_price = price
        trade.exit_reason = reason
        trade.gross_pnl = gross
        trade.costs = cost
        trade.net_pnl = gross - cost
        trade.r_multiple = trade.net_pnl / trade.risk_amount if trade.risk_amount else 0.0
        if budget is not None:
            budget.close_position(
                trade.symbol, trade.net_pnl, now=datetime.combine(ts.date(), ts.time())
            )
        return trade.net_pnl
