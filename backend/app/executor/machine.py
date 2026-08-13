"""The per-symbol order state machine.

PLAN.md Part 6. One instance per basket symbol. Five of them share a single
RiskBudget, a single breaker set and a single reconciliation pass, because the
budget is portfolio-wide while a position is per symbol.

The two states that matter, and why:

    PENDING_ENTRY with no response
        The order was sent and nothing came back. The rule is absolute: NEVER
        blindly retry - reconcile first. A duplicate intraday position costs
        more than a missed trade, and with no client order id (step 0) a retry
        cannot be distinguished from the original at the broker.

    OPEN_UNPROTECTED
        Filled, but the protective stop is not live. The position is naked.
        Retry the stop immediately; if it fails again, exit at market. Never
        wait in this state, and never let a squareoff or a signal exit skip past
        it - a naked position is the one thing an unattended agent must not sit
        on.

Order-of-operations facts that are easy to get wrong:

    Cancel the stop BEFORE sending a market exit. Sending an exit while the stop
    is still working at the broker can fill BOTH, leaving a reversed position -
    short when you were long. This is the single most expensive ordering bug
    available here.

    price_type at the top level, pricetype inside legs. The SDK forwards a wrong
    spelling verbatim and the order silently drops to MARKET.

    The SDK str()-casts every kwarg, so numbers must be formatted deliberately
    rather than left to repr.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Mapping

from ..config import Settings
from ..openalgo.client import OpenAlgoClient
from ..risk.budget import Position, RiskBudget
from ..risk.sizing import quantity_for, worst_case_loss
from .intents import Intent, IntentLog, IntentState, make_intent_id

log = logging.getLogger(__name__)

# Maximum dwell time per state. Every state needs one, or a broker that never
# answers leaves the machine parked forever.
PENDING_ENTRY_TIMEOUT = timedelta(seconds=30)
PENDING_EXIT_TIMEOUT = timedelta(seconds=15)
UNPROTECTED_TIMEOUT = timedelta(seconds=5)

# How many times to retry placing the protective stop before giving up and
# exiting the position at market.
STOP_PLACEMENT_ATTEMPTS = 2


def _num(value: float) -> str:
    """Format a number for the SDK, which str()-casts every kwarg.

    Left to repr, 0.0 reaches the wire as "0.0" and some brokers reject it.
    """
    if value is None:
        return "0"
    if float(value).is_integer():
        return str(int(value))
    return f"{float(value):.2f}"


@dataclass
class Action:
    """What the machine did on this tick, for the live view and the log."""

    symbol: str
    kind: str = "none"     # entry | exit | stop | halt | none
    detail: str = ""
    intent_id: str = ""


class SymbolMachine:
    """Drives one symbol's intent through its lifecycle.

    Holds no market data of its own. The executor feeds it a bar and a signal;
    this decides what to do about them.
    """

    def __init__(
        self,
        settings: Settings,
        client: OpenAlgoClient,
        store: IntentLog,
        budget: RiskBudget,
        symbol: str,
        exchange: str = "NSE",
    ) -> None:
        self.settings = settings
        self.client = client
        self.store = store
        self.budget = budget
        self.symbol = symbol
        self.exchange = exchange
        self.active: Intent | None = None
        self._entered_state_at: datetime = datetime.now()
        self._stop_attempts = 0

    # ------------------------------------------------------------------ state

    @property
    def state(self) -> IntentState:
        return self.active.state if self.active else IntentState.FLAT

    def adopt(self, intent: Intent) -> None:
        """Take ownership of an intent recovered by reconciliation."""
        self.active = intent
        self._entered_state_at = datetime.now()
        self._stop_attempts = 0

    def _move(self, state: IntentState, reason: str) -> None:
        assert self.active is not None
        self.store.transition(self.active, state, reason)
        self._entered_state_at = datetime.now()

    def _dwell(self) -> timedelta:
        return datetime.now() - self._entered_state_at

    # --------------------------------------------------------------- the tick

    def on_bar(
        self,
        bar_ts: datetime,
        bar: Mapping[str, float],
        signal: Mapping[str, Any] | None,
        marks: Mapping[str, float],
        *,
        risk_fraction: float,
        allow_entries: bool,
    ) -> Action:
        """Advance the machine one 5-minute bar.

        Order matters and mirrors the replay: timeouts, then exits, then entries.
        Exits before entries means a symbol can close and re-enter on the same
        bar without ever being double-counted against the position cap.
        """
        now = bar_ts
        act = Action(symbol=self.symbol)

        expired = self._handle_timeouts(now)
        if expired is not None:
            return expired

        if self.state is IntentState.OPEN_UNPROTECTED:
            return self._ensure_stop()

        if self.state.holds_position:
            exited = self._maybe_exit(now, bar, signal)
            if exited is not None:
                return exited

        if allow_entries and self.state in (IntentState.FLAT,) and signal is not None:
            return self._maybe_enter(now, bar, signal, marks, risk_fraction=risk_fraction)

        return act

    # ------------------------------------------------------------- timeouts

    def _handle_timeouts(self, now: datetime) -> Action | None:
        if self.active is None:
            return None

        if self.state is IntentState.PENDING_ENTRY and self._dwell() > PENDING_ENTRY_TIMEOUT:
            # Cancel and let reconciliation decide what actually happened. This
            # deliberately does NOT resend.
            self._cancel(self.active.entry_order_id, "entry timed out")
            self._move(
                IntentState.UNKNOWN,
                f"no confirmation within {PENDING_ENTRY_TIMEOUT.seconds}s; "
                "cancelled and handed to reconciliation. Never resend.",
            )
            return Action(self.symbol, "halt", "entry timeout, awaiting reconciliation",
                          self.active.intent_id)

        if self.state is IntentState.PENDING_EXIT and self._dwell() > PENDING_EXIT_TIMEOUT:
            self._cancel(self.active.exit_order_id, "exit timed out")
            return self._exit_at_market("exit timed out, escalating to market")

        if self.state is IntentState.OPEN_UNPROTECTED and self._dwell() > UNPROTECTED_TIMEOUT:
            return self._exit_at_market("stop could not be placed; refusing to sit naked")

        return None

    # ---------------------------------------------------------------- entries

    def _maybe_enter(
        self,
        now: datetime,
        bar: Mapping[str, float],
        signal: Mapping[str, Any],
        marks: Mapping[str, float],
        *,
        risk_fraction: float,
    ) -> Action:
        side = "BUY" if signal.get("long_entry") else ("SELL" if signal.get("short_entry") else "")
        if not side:
            return Action(self.symbol)

        stop = signal.get("stop_price")
        if stop is None or stop != stop:  # NaN check without importing math
            return Action(self.symbol, "none", "no stop price on the signal bar")

        entry = float(bar["close"])
        stop = float(stop)
        if (side == "BUY" and stop >= entry) or (side == "SELL" and stop <= entry):
            return Action(self.symbol, "none", "stop is on the wrong side of the entry")

        sized = quantity_for(
            self.settings,
            entry_price=entry,
            stop_price=stop,
            risk_fraction=risk_fraction,
            budget_cap=self.budget.remaining_budget(marks),
        )
        if not sized.ok:
            return Action(self.symbol, "none", sized.reason)

        loss = worst_case_loss(sized.quantity, entry, stop)
        decision = self.budget.can_open(self.symbol, loss, marks, now=now)
        if not decision.allowed:
            return Action(self.symbol, "none", f"{decision.code}: {decision.reason}")

        intent = Intent(
            intent_id=make_intent_id(self.settings.default_strategy_name, self.symbol, now, side),
            trading_date=now.date(),
            strategy_id=self.settings.default_strategy_name,
            strategy_version="0.1.0",
            mandate_version=now.date().isoformat(),
            symbol=self.symbol,
            exchange=self.exchange,
            side=side,
            signal_bar_ts=now,
            planned_qty=sized.quantity,
            planned_entry=entry,
            planned_stop=stop,
            risk_amount=loss,
            risk_fraction_used=risk_fraction,
        )
        self.active = intent

        # WRITE BEFORE SEND. A crash between here and the broker call leaves a
        # PENDING_ENTRY row that reconciliation can resolve. Sending first would
        # leave a live position with no record of it at all.
        self.store.put(intent)
        self._move(IntentState.PENDING_ENTRY, "sending entry")

        resp = self.client.call_enveloped(
            "placeorder",
            strategy=self.settings.default_strategy_name,
            symbol=self.symbol,
            action=side,
            exchange=self.exchange,
            price_type="LIMIT",          # top level is price_type, legs are pricetype
            product="MIS",
            quantity=_num(sized.quantity),
            price=_num(entry),
            _order=True,
        )
        if not resp.get("ok"):
            # The call itself failed. That is NOT the same as the order being
            # refused - the request may or may not have reached the broker, so
            # this goes to UNKNOWN for reconciliation, never back to FLAT.
            self._move(IntentState.UNKNOWN, f"entry send failed: {resp.get('error')}")
            return Action(self.symbol, "halt", str(resp.get("error")), intent.intent_id)

        order_id = str((resp.get("data") or {}).get("orderid") or "")
        intent.entry_order_id = order_id
        self.store.put(intent)
        return Action(self.symbol, "entry", f"{side} {sized.quantity} at {entry:.2f}", intent.intent_id)

    # ------------------------------------------------------------------ stops

    def _ensure_stop(self) -> Action:
        """Place the protective stop. The position is naked until this returns."""
        assert self.active is not None
        intent = self.active
        exit_side = "SELL" if intent.side == "BUY" else "BUY"
        self._stop_attempts += 1

        resp = self.client.call_enveloped(
            "placeorder",
            strategy=self.settings.default_strategy_name,
            symbol=self.symbol,
            action=exit_side,
            exchange=self.exchange,
            price_type="SL-M",           # SL-M needs a trigger price, not a price
            product="MIS",
            quantity=_num(intent.fill_qty or intent.planned_qty),
            trigger_price=_num(intent.planned_stop),
            _order=True,
        )
        if resp.get("ok"):
            intent.stop_order_id = str((resp.get("data") or {}).get("orderid") or "")
            intent.stop_price = intent.planned_stop
            self.budget.open_position(
                Position(self.symbol, intent.fill_qty or intent.planned_qty,
                         intent.fill_price or intent.planned_entry,
                         intent.planned_stop, intent.side)
            )
            self._move(IntentState.OPEN, "stop confirmed at the broker")
            return Action(self.symbol, "stop", f"stop live at {intent.planned_stop:.2f}",
                          intent.intent_id)

        if self._stop_attempts >= STOP_PLACEMENT_ATTEMPTS:
            return self._exit_at_market(
                f"stop placement failed {self._stop_attempts} times: {resp.get('error')}"
            )
        return Action(self.symbol, "none", f"stop placement failed, retrying: {resp.get('error')}")

    # ------------------------------------------------------------------ exits

    def _maybe_exit(
        self, now: datetime, bar: Mapping[str, float], signal: Mapping[str, Any] | None
    ) -> Action | None:
        assert self.active is not None
        bar_time = now.time()

        if bar_time >= self.settings.squareoff_time:
            return self._exit_at_market("squareoff")

        if signal is not None:
            wants_out = (
                signal.get("long_exit") if self.active.side == "BUY" else signal.get("short_exit")
            )
            if wants_out:
                return self._exit_at_market("signal exit")

        return None

    def _exit_at_market(self, reason: str) -> Action:
        """Cancel the stop FIRST, then send the market exit.

        Sending the exit while the stop is still working can fill both and leave
        a REVERSED position - short when you were long. Cancel, then exit.
        """
        assert self.active is not None
        intent = self.active

        if intent.stop_order_id:
            self._cancel(intent.stop_order_id, "cancelling stop before market exit")
            intent.stop_order_id = ""

        exit_side = "SELL" if intent.side == "BUY" else "BUY"
        self._move(IntentState.PENDING_EXIT, reason)

        resp = self.client.call_enveloped(
            "placeorder",
            strategy=self.settings.default_strategy_name,
            symbol=self.symbol,
            action=exit_side,
            exchange=self.exchange,
            price_type="MARKET",
            product="MIS",
            quantity=_num(intent.fill_qty or intent.planned_qty),
            _order=True,
        )
        if not resp.get("ok"):
            self._move(IntentState.UNKNOWN, f"exit send failed: {resp.get('error')}")
            return Action(self.symbol, "halt", str(resp.get("error")), intent.intent_id)

        intent.exit_order_id = str((resp.get("data") or {}).get("orderid") or "")
        intent.exit_reason = reason
        self.store.put(intent)
        return Action(self.symbol, "exit", reason, intent.intent_id)

    def settle_exit(self, price: float, qty: int, costs: float, now: datetime) -> None:
        """Record a confirmed exit fill and close the intent."""
        assert self.active is not None
        intent = self.active
        direction = 1 if intent.side == "BUY" else -1
        entry = intent.fill_price or intent.planned_entry
        intent.exit_price = price
        intent.exit_qty = qty
        intent.exit_ts = now
        intent.gross_pnl = (price - entry) * qty * direction
        intent.costs = costs
        intent.net_pnl = intent.gross_pnl - costs
        intent.r_multiple = intent.net_pnl / intent.risk_amount if intent.risk_amount else 0.0
        self._move(IntentState.CLOSED, f"exit filled at {price:.2f}")
        self.budget.close_position(self.symbol, intent.net_pnl, now=now)
        self.active = None

    # ------------------------------------------------------------------ utils

    def _cancel(self, order_id: str, why: str) -> None:
        if not order_id:
            return
        resp = self.client.call_enveloped("cancelorder", order_id=order_id, _order=True)
        if not resp.get("ok"):
            # Worth a warning, not a halt: the order may already be filled or
            # gone, and the caller has its own escalation path.
            log.warning("cancel %s failed (%s): %s", order_id, why, resp.get("error"))

    def force_flat(self, reason: str = "forced flat") -> Action:
        """Drive to FLAT regardless of state. Used at squareoff and on a halt."""
        if self.active is None or not self.state.holds_position:
            return Action(self.symbol, "none", "already flat")
        return self._exit_at_market(reason)
