"""The executor. One session, start to flat.

PLAN.md Part 6 and Part 8. This is the piece that turns everything else into a
running system: the clock says which phase it is, the strategy says what the
signal is, the budget says whether it is affordable, and one state machine per
symbol does the ordering.

The tick order is fixed and mirrors the replay exactly, which is what makes the
backtest a description of this rather than a separate story:

    1. reconcile        before ANY decision, on every wake
    2. breakers         a position can breach the daily limit with nothing
                        closing, so this runs continuously and not just on a fill
    3. exits            before entries, so a symbol can close and re-enter on the
                        same bar without being double-counted against the cap
    4. entries          in basket PRIORITY order, sequenced not fired together
    5. squareoff        drives everything to flat regardless of state

Entries are SEQUENCED rather than sent in parallel. Each fill consumes margin
and budget, so order N+1 must be sized after order N lands, not before. Five
orders is well inside the 8-per-second client limiter, so throughput is not the
constraint - the running budget's correctness is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping

import pandas as pd

from ..config import Settings
from ..openalgo.client import OpenAlgoClient, get_client
from ..openalgo.frames import get_frame_cache
from ..risk.budget import RiskBudget, RunState
from ..strategies import ExecutionAdapter, get_strategy
from .clock import Phase, SessionClock
from .intents import IntentLog, IntentState
from .machine import Action, SymbolMachine
from .reconcile import reconcile

log = logging.getLogger(__name__)

# States that mean the broker knows something the agent does not yet.
_UNCONFIRMED = (
    IntentState.PENDING_ENTRY,
    IntentState.PENDING_EXIT,
    IntentState.UNKNOWN,
    IntentState.PARTIAL,
)


@dataclass
class TickReport:
    """What happened on one bar. Fed to the SSE stream and the log."""

    ts: datetime
    phase: str
    actions: list[Action] = field(default_factory=list)
    halted: bool = False
    reason: str = ""
    budget_used_pct: float = 0.0
    remaining_budget: float = 0.0
    open_positions: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts.isoformat(),
            "phase": self.phase,
            "halted": self.halted,
            "reason": self.reason,
            "budget_used_pct": round(self.budget_used_pct, 2),
            "remaining_budget": round(self.remaining_budget, 2),
            "open_positions": self.open_positions,
            "actions": [
                {"symbol": a.symbol, "kind": a.kind, "detail": a.detail}
                for a in self.actions
                if a.kind != "none"
            ],
        }


class Executor:
    """Runs one trading session for one selected strategy."""

    def __init__(
        self,
        settings: Settings,
        strategy_id: str,
        *,
        risk_fraction: float,
        trading_date: date | None = None,
        client: OpenAlgoClient | None = None,
        store: IntentLog | None = None,
    ) -> None:
        self.settings = settings
        self.strategy_id = strategy_id
        self.risk_fraction = risk_fraction
        self.trading_date = trading_date or date.today()
        self.client = client or get_client()
        self.store = store or IntentLog(settings.db_path)
        self.clock = SessionClock(settings)
        self.budget = RiskBudget(settings=settings, trading_date=self.trading_date)

        strategy = get_strategy(strategy_id)
        self.machines: dict[str, SymbolMachine] = {}
        self.adapters: dict[str, ExecutionAdapter] = {}
        for symbol in settings.basket_symbols:
            self.machines[symbol] = SymbolMachine(
                settings, self.client, self.store, self.budget, symbol
            )
            self.adapters[symbol] = ExecutionAdapter(strategy)

        self._reconciled = False

    # ------------------------------------------------------------------ startup

    def start(self) -> tuple[bool, str]:
        """Pre-session checks. Returns (may_trade, reason).

        Every refusal here is a safe failure. Refusing to start costs a day of
        opportunity; starting on an unreconciled book costs a position.
        """
        # A sticky halt must survive a restart, or an agent that stopped for a
        # good reason comes back and does it all again.
        if self.budget.restore():
            return False, f"restored halt from disk: {self.budget.halt_reason}"

        if self.settings.kill_switch_engaged():
            return False, "kill switch file is present"

        if not self.settings.trading_enabled:
            return False, "TRADING_ENABLED is false"

        mode = self.client.analyzer_mode()
        if self.settings.require_analyzer_mode and mode != "analyze":
            return False, f"REQUIRE_ANALYZER_MODE is set and the broker reports '{mode}'"

        # ALLOCATION is never read from the broker, but the broker must actually
        # hold at least that much or the sizing is fiction.
        funds = self.client.call_enveloped("funds")
        if funds.get("ok"):
            try:
                cash = float((funds.get("data") or {}).get("availablecash", 0) or 0)
            except (TypeError, ValueError):
                cash = 0.0
            if cash < self.settings.require_funds_at_least:
                return False, (
                    f"broker availablecash {cash:.0f} is below "
                    f"REQUIRE_FUNDS_AT_LEAST {self.settings.require_funds_at_least:.0f}"
                )

        ok, reason = self.reconcile_now()
        if not ok:
            return False, reason

        return True, "ready"

    def reconcile_now(self) -> tuple[bool, str]:
        """Match belief against the broker. Halt on ambiguity."""
        result = reconcile(
            self.client, self.store, self.trading_date,
            strategy_tag=self.settings.default_strategy_name,
        )
        if not result.ok:
            self.budget.halt(f"reconciliation failed: {result.reason}")
            return False, result.reason

        # Settle exits the broker has already completed, before adopting, so a
        # finished trade is closed rather than re-adopted as still open.
        for intent_id in result.exited:
            intent = self.store.get(intent_id)
            if intent is None:
                continue
            machine = self.machines.get(intent.symbol)
            if machine is not None and machine.active is not None:
                machine.settle_exit(
                    intent.planned_entry, intent.fill_qty or intent.planned_qty,
                    costs=0.0, now=datetime.now(),
                )

        # Hand any recovered position back to the machine that owns it, or it
        # would be managed by nobody.
        for intent in self.store.open_positions(self.trading_date):
            machine = self.machines.get(intent.symbol)
            if machine is not None:
                machine.adopt(intent)
                log.info("adopted %s in state %s", intent.symbol, intent.state.value)

        self._reconciled = True
        return True, result.reason or "reconciled"

    # --------------------------------------------------------------------- tick

    def tick(self, now: datetime, frames: Mapping[str, pd.DataFrame]) -> TickReport:
        """Advance every machine one bar."""
        phase = self.clock.phase(now)
        report = TickReport(ts=now, phase=phase.value)

        if not self._reconciled:
            report.halted = True
            report.reason = "tick before reconciliation"
            return report

        marks = {
            sym: float(f["close"].iloc[-1])
            for sym, f in frames.items()
            if f is not None and not f.empty
        }

        # A position can breach the daily limit with nothing closing, so this
        # runs on every bar rather than only after a fill.
        breaker = self.budget.check_breakers(marks, now=now)
        if not breaker.allowed:
            report.halted = True
            report.reason = breaker.reason
            report.actions.extend(self.flatten_all("breaker: " + breaker.code))
            return self._finish(report, marks)

        if self.settings.kill_switch_engaged():
            self.budget.halt("kill switch file appeared")
            report.halted = True
            report.reason = "kill switch"
            report.actions.extend(self.flatten_all("kill switch"))
            return self._finish(report, marks)

        if phase is Phase.SQUAREOFF:
            report.actions.extend(self.flatten_all("squareoff"))
            return self._finish(report, marks)

        if not phase.is_session:
            return self._finish(report, marks)

        allow_entries = phase.allows_entries and self.budget.state is RunState.RUNNING

        # Basket priority order is the tie-break when several symbols signal on
        # the same bar and the budget cannot fund them all. Deterministic on
        # purpose, and sequenced so each entry is sized against a budget that
        # already reflects the one before it.
        for symbol in self.settings.basket_symbols:
            machine = self.machines.get(symbol)
            frame = frames.get(symbol)
            if machine is None or frame is None or frame.empty:
                continue

            signal = None
            try:
                row = self.adapters[symbol].on_frame(frame)
                signal = row.to_dict()
            except Exception:  # noqa: BLE001
                # A strategy that raises must not take the session down; it takes
                # itself out for this bar and the position stays managed.
                log.warning("signal failed for %s", symbol, exc_info=True)

            bar = frame.iloc[-1].to_dict()
            action = machine.on_bar(
                now, bar, signal, marks,
                risk_fraction=self.risk_fraction,
                allow_entries=allow_entries,
            )
            if action.kind != "none":
                report.actions.append(action)
            if action.kind == "halt":
                # A machine reporting halt means it reached UNKNOWN. Nothing new
                # opens until reconciliation resolves it.
                allow_entries = False

        # Reconcile AFTER the machines have acted, so an entry sent on this bar
        # is confirmed on this bar. Found by the step 11 dry run: with
        # reconciliation only at start(), a PENDING_ENTRY never advanced, the
        # squareoff recognised no position, and the session ended with the
        # BROKER holding four positions the agent believed it did not have.
        if any(m.state in _UNCONFIRMED for m in self.machines.values()):
            ok, why = self.reconcile_now()
            if not ok:
                report.halted = True
                report.reason = f"reconciliation failed mid-session: {why}"

        return self._finish(report, marks)

    def _finish(self, report: TickReport, marks: Mapping[str, float]) -> TickReport:
        report.budget_used_pct = self.budget.budget_used_pct(marks)
        report.remaining_budget = self.budget.remaining_budget(marks)
        report.open_positions = len(self.budget.open_positions)
        return report

    # ------------------------------------------------------------------ control

    def flatten_all(self, reason: str) -> list[Action]:
        """Drive every machine to flat, sequenced and confirmed.

        Sequenced rather than fired together: each exit is a market order and
        firing five at once gives up any control over the order they land in.
        """
        out: list[Action] = []
        for symbol in self.settings.basket_symbols:
            machine = self.machines.get(symbol)
            if machine is None:
                continue
            action = machine.force_flat(reason)
            if action.kind != "none":
                out.append(action)
        return out

    def halt(self, reason: str) -> list[Action]:
        actions = self.flatten_all(f"halt: {reason}")
        self.budget.halt(reason)
        return actions

    def reduce_only(self, reason: str) -> None:
        self.budget.reduce_only(reason)

    # ------------------------------------------------------------------- status

    def status(self, marks: Mapping[str, float] | None = None) -> dict[str, Any]:
        marks = marks or {}
        return {
            "trading_date": self.trading_date.isoformat(),
            "strategy_id": self.strategy_id,
            "risk_fraction": self.risk_fraction,
            "state": self.budget.state.value,
            "halt_reason": self.budget.halt_reason,
            "realized_pnl": round(self.budget.realized_pnl, 2),
            "mtm_pnl": round(self.budget.mtm_pnl(marks), 2),
            "budget_used_pct": round(self.budget.budget_used_pct(marks), 2),
            "remaining_budget": round(self.budget.remaining_budget(marks), 2),
            "trade_count": self.budget.trade_count,
            "consecutive_losses": self.budget.consecutive_losses,
            "open_positions": len(self.budget.open_positions),
            "machines": {
                sym: m.state.value for sym, m in self.machines.items()
            },
            "intents": self.store.counts_by_state(self.trading_date),
        }


def load_session_frames(settings: Settings, lookback_bars: int = 900) -> dict[str, pd.DataFrame]:
    """Current session frames for the basket, cleaned at the boundary.

    Warm-up is carried ACROSS sessions deliberately. EMA(30) on 5m bars needs
    about 30 bars and a session has 75, so restarting the series each morning
    would leave the indicator invalid until roughly 11:45.
    """
    cache = get_frame_cache()
    out: dict[str, pd.DataFrame] = {}
    for symbol in settings.basket_symbols:
        result = cache.get_frame(symbol, "NSE", settings.timeframe, lookback_bars=lookback_bars)
        if result.get("ok"):
            out[symbol] = result["frame"]
        else:
            log.warning("no frame for %s: %s", symbol, result.get("error"))
    return out
