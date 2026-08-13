"""Reconciliation. What the agent believes against what the broker holds.

PLAN.md Part 6. Runs before ANY decision, on every wake, and after every fill.

    If they cannot be reconciled, HALT. Do not guess, and do not trade around an
    unexplained position.

The reason this is hard here is the step 0 finding: OpenAlgo accepts no
client-supplied order id, so an intent cannot be matched to a broker order by
key. It has to be matched on shape:

    (strategy tag, symbol, side, quantity, time window)

That is usually enough and occasionally is not. When it is not, the correct
answer is to stop, because the failure mode of guessing is a duplicate intraday
position - which costs more than every missed trade of the day put together.

A second problem the account creates: the positionbook NETS everything. If the
human also holds RELIANCE manually, the broker reports one position and neither
party can tell whose it is. Hence the standing advice to keep the basket off the
manual watchlist, and hence "a position in a symbol the agent does not believe it
holds" is an unreconciled state even when every other symbol matches.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

from ..openalgo.client import OpenAlgoClient
from .intents import Intent, IntentLog, IntentState

log = logging.getLogger(__name__)

# How far either side of the intent's send time a broker order may sit and still
# be considered the same order. Generous, because a slow broker leg is common and
# a false NON-match is the dangerous direction: it makes the agent think its
# order vanished when it did not.
MATCH_WINDOW = timedelta(minutes=5)

_OPEN_STATUSES = {"open", "pending", "trigger pending", "open pending", "validation pending"}
_DONE_STATUSES = {"complete", "completed", "filled"}
_DEAD_STATUSES = {"rejected", "cancelled", "canceled"}


@dataclass
class Reconciliation:
    """The outcome. `ok` false means the session must not proceed."""

    ok: bool = True
    resolved: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    orphan_positions: list[str] = field(default_factory=list)
    missing_positions: list[str] = field(default_factory=list)
    # Intents whose exit has completed at the broker, for the caller to settle.
    exited: list[str] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "resolved": self.resolved,
            "ambiguous": self.ambiguous,
            "orphan_positions": self.orphan_positions,
            "missing_positions": self.missing_positions,
            "exited": self.exited,
            "reason": self.reason,
        }


def _norm(value: Any) -> str:
    return str(value or "").strip().upper()


def _to_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _naive(value: datetime | None) -> datetime | None:
    """Drop tzinfo so two datetimes can be compared.

    Frames carry a tz-aware index (Asia/Kolkata) while broker order timestamps
    parse naive, and subtracting the two raises TypeError. Found by the step 11
    dry run, and it would have fired on the first live reconciliation of a
    pending order. Both sides are the same local wall time, so dropping tzinfo
    is correct here rather than merely convenient.
    """
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _order_time(row: Mapping[str, Any]) -> datetime | None:
    raw = row.get("timestamp") or row.get("order_timestamp") or row.get("time")
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M:%S", "%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt == "%H:%M:%S":
                today = date.today()
                parsed = parsed.replace(year=today.year, month=today.month, day=today.day)
            return parsed
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def candidates_for(
    intent: Intent, orders: Sequence[Mapping[str, Any]], *, strategy_tag: str
) -> list[Mapping[str, Any]]:
    """Broker orders that could be this intent's entry.

    Deliberately NOT tolerant on quantity. A partial fill shows a different
    filled quantity but the ORDER quantity still matches what was sent, so an
    exact match on order quantity is right and a fuzzy one would let a different
    order masquerade as this one.
    """
    want_sym, want_side = _norm(intent.symbol), _norm(intent.side)
    want_qty = int(intent.planned_qty)
    sent_at = _naive(intent.signal_bar_ts)

    out: list[Mapping[str, Any]] = []
    for row in orders:
        if _norm(row.get("symbol")) != want_sym:
            continue
        if _norm(row.get("action")) != want_side:
            continue
        if _to_int(row.get("quantity")) != want_qty:
            continue
        # The strategy tag is the only thing that survives the round trip, so it
        # is the strongest discriminator available. Only enforce it when the
        # broker actually returns the field.
        tag = row.get("strategy")
        if tag is not None and _norm(tag) != _norm(strategy_tag):
            continue
        placed = _naive(_order_time(row))
        if placed is not None and sent_at is not None and abs(placed - sent_at) > MATCH_WINDOW:
            continue
        out.append(row)
    return out


def reconcile(
    client: OpenAlgoClient,
    log_store: IntentLog,
    trading_date: date,
    *,
    strategy_tag: str,
) -> Reconciliation:
    """Match every unresolved intent against the broker. Halt on ambiguity."""
    result = Reconciliation()

    book = client.call_enveloped("orderbook")
    positions = client.call_enveloped("positionbook")
    if not book.get("ok") or not positions.get("ok"):
        result.ok = False
        result.reason = (
            "could not read the orderbook or positionbook, so nothing can be "
            "reconciled. Refusing to trade blind."
        )
        return result

    data = book.get("data") or {}
    orders: Sequence[Mapping[str, Any]] = (
        data.get("orders") if isinstance(data, dict) else data
    ) or []
    pos_rows: Sequence[Mapping[str, Any]] = positions.get("data") or []

    broker_positions = {
        _norm(p.get("symbol")): _to_int(p.get("quantity"))
        for p in pos_rows
        if _to_int(p.get("quantity")) != 0
    }

    unresolved = log_store.unresolved(trading_date)

    for intent in unresolved:
        matches = candidates_for(intent, orders, strategy_tag=strategy_tag)

        if len(matches) > 1:
            # Two orders that look identical. Without a client order id there is
            # no way to tell which is this intent, and picking one could leave a
            # real position unmanaged. Stop.
            result.ambiguous.append(intent.intent_id)
            continue

        if not matches:
            if intent.state is IntentState.PENDING_ENTRY:
                # Sent, and the broker has no record. The order never landed.
                # This is the SAFE resolution of the dangerous state - but only
                # because the orderbook was readable, which was checked above.
                log_store.transition(
                    intent, IntentState.ABANDONED,
                    "no matching broker order; the order never reached the broker",
                )
                result.resolved.append(intent.intent_id)
            continue

        row = matches[0]
        status = _norm(row.get("order_status") or row.get("status"))
        filled = _to_int(row.get("filled_quantity") or row.get("filledshares"))
        order_id = str(row.get("orderid") or row.get("order_id") or "")

        if not intent.entry_order_id and order_id:
            intent.entry_order_id = order_id

        if status.lower() in _DEAD_STATUSES:
            log_store.transition(intent, IntentState.REJECTED, f"broker reports {status}")
            result.resolved.append(intent.intent_id)
        elif status.lower() in _DONE_STATUSES:
            intent.fill_qty = filled or intent.planned_qty
            intent.fill_price = float(row.get("average_price") or row.get("price") or 0.0)
            intent.fill_ts = _order_time(row) or datetime.now()
            # Filled, but this function cannot know whether the protective stop
            # is live. OPEN_UNPROTECTED is the honest state; the machine promotes
            # it to OPEN only after it confirms the stop at the broker.
            log_store.transition(
                intent, IntentState.OPEN_UNPROTECTED,
                f"broker reports filled {intent.fill_qty} at {intent.fill_price}",
            )
            result.resolved.append(intent.intent_id)
        elif status.lower() in _OPEN_STATUSES:
            if filled and filled < intent.planned_qty:
                intent.fill_qty = filled
                log_store.transition(
                    intent, IntentState.PARTIAL, f"filled {filled} of {intent.planned_qty}"
                )
            # Still working. Leave it pending; the timeout in the machine handles
            # an order that never completes.
            result.resolved.append(intent.intent_id)
        else:
            result.ambiguous.append(intent.intent_id)

    # Position checks run over the CURRENT open set, after the transitions above,
    # and independently of whether an order matched.
    #
    # An earlier version nested the missing-position check inside the "no
    # matching order" branch, which meant it never fired: a filled order stays in
    # the orderbook for the rest of the day, so a matching order is always found
    # and the branch was unreachable. The agent could believe it held a position
    # the broker had squared off and never notice. Caught by test_reconcile.
    held_now = log_store.open_positions(trading_date)
    for intent in held_now:
        if _norm(intent.symbol) in broker_positions:
            continue
        if intent.state is IntentState.PENDING_EXIT:
            # An exit was sent and the broker is now flat in this symbol. That
            # is the exit HAVING WORKED, not a discrepancy.
            #
            # MEASURED, step 11: without this case the dry run halted the
            # session every time a position closed normally - the agent read
            # its own successful exit as "I believe I hold something the broker
            # does not show". PENDING_EXIT is the one holds_position state
            # where a flat broker is the EXPECTED outcome.
            result.exited.append(intent.intent_id)
            continue
        result.missing_positions.append(intent.intent_id)

    # A position the agent has no intent for at all. On a shared account this is
    # usually the human's own trade, which is exactly why it cannot be ignored:
    # the agent must not manage, square off, or size around something it did not
    # open.
    known = {_norm(i.symbol) for i in held_now}
    for symbol in broker_positions:
        if symbol not in known:
            result.orphan_positions.append(symbol)

    if result.ambiguous:
        result.ok = False
        result.reason = (
            f"{len(result.ambiguous)} intent(s) match more than one broker order and "
            "cannot be resolved without a client order id: "
            f"{', '.join(result.ambiguous)}. Halting rather than guessing."
        )
    elif result.missing_positions:
        result.ok = False
        result.reason = (
            "the agent believes it holds a position the broker does not show: "
            f"{', '.join(result.missing_positions)}. Halting."
        )
    elif result.orphan_positions:
        # Not fatal. It is almost certainly a manual trade, and the agent's job
        # is to leave it alone rather than to stop the session over it.
        result.reason = (
            "positions present that this agent did not open, and will not manage: "
            f"{', '.join(result.orphan_positions)}"
        )
        log.warning("reconcile: %s", result.reason)

    return result
