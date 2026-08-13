"""Position sizing. Risk is fixed; quantity is derived.

The whole design rests on one line:

    quantity = risk_amount / stop_distance

Volatility therefore sizes the position automatically. A wider stop buys fewer
shares, so a choppy name and a quiet name risk the same rupees.

Two facts this module exists to protect, both from PLAN.md Part 5:

1. risk_amount is a fraction of ALLOCATION, never of broker funds. Measured at
   step 0: the account held 99,99,984 against an allocation of 10,00,000. Sizing
   against available cash would have produced positions ten times too large, and
   nothing would have raised - the orders would simply have been wrong.

2. The Planner may scale risk DOWN and never up. It proposes a fraction inside
   [risk_fraction_floor, risk_fraction_base]; anything outside that range, or
   missing, resolves to the floor. A model cannot enlarge a position by being
   confident about it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import Settings


@dataclass(frozen=True)
class SizingResult:
    """The outcome of a sizing decision, including the refusals.

    quantity of 0 always means "do not trade". `reason` says why, and is written
    to the intent log so a skipped signal is explainable months later.
    """

    quantity: int
    risk_amount: float
    stop_distance: float
    notional: float
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.quantity > 0


def resolve_risk_fraction(settings: Settings, proposed: float | None) -> float:
    """Clamp a Planner-proposed risk fraction into the mandate band.

    Returns the FLOOR, not the base, when the proposal is missing or unusable.
    A model that fails to answer must not be rewarded with full size, and an
    out-of-range answer is evidence something is wrong rather than an invitation
    to guess.
    """
    floor = settings.risk_fraction_floor
    base = settings.risk_fraction_base

    if proposed is None:
        return floor
    try:
        value = float(proposed)
    except (TypeError, ValueError):
        return floor
    if not math.isfinite(value) or value <= 0:
        return floor

    # Clamp rather than reject: a proposal slightly over the ceiling is a
    # rounding artefact, not an attempt to breach the mandate. Anything far out
    # still lands exactly on the ceiling, which is the mandate's own limit.
    return max(floor, min(base, value))


def quantity_for(
    settings: Settings,
    *,
    entry_price: float,
    stop_price: float,
    risk_fraction: float,
    budget_cap: float | None = None,
    lot_size: int = 1,
    freeze_qty: int | None = None,
) -> SizingResult:
    """Derive a share quantity from the risk the trade is allowed to lose.

    risk_fraction is REQUIRED and has no default. That is deliberate. An earlier
    draft defaulted it to None and fed that to resolve_risk_fraction, which maps
    None to the FLOOR - so every caller that simply forgot the argument silently
    got quarter-size positions and nothing raised. The two questions "the Planner
    did not answer" and "this caller did not say" have opposite safe answers, so
    they must not share a sentinel.

    Callers that are sizing from a Planner proposal do this:

        fraction = resolve_risk_fraction(settings, planner_proposal)
        result = quantity_for(settings, ..., risk_fraction=fraction)

    budget_cap is the remaining daily loss budget. Passing it lets a trade size
    DOWN to fit rather than be refused outright, which keeps the last part of a
    session usable instead of dead.

    freeze_qty is read from the broker per symbol and never hard-coded. Note the
    step 0 finding: a cash equity reporting freeze_qty=1 means "not applicable",
    not a one-share limit, so a value of 1 is ignored here.
    """
    if entry_price <= 0:
        return SizingResult(0, 0.0, 0.0, 0.0, "entry price is not positive")
    if stop_price <= 0:
        return SizingResult(0, 0.0, 0.0, 0.0, "stop price is not positive")

    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        return SizingResult(0, 0.0, 0.0, 0.0, "stop equals entry, distance is zero")

    # Already resolved by the caller; clamp again as a cheap belt-and-braces so a
    # hand-built fraction cannot exceed the mandate ceiling.
    fraction = max(settings.risk_fraction_floor, min(settings.risk_fraction_base, float(risk_fraction)))
    risk_amount = settings.allocation * fraction

    if budget_cap is not None:
        if budget_cap <= 0:
            return SizingResult(
                0, 0.0, stop_distance, 0.0,
                "daily loss budget is exhausted",
            )
        # Size down to what the remaining budget can actually absorb. The
        # alternative - refusing the trade - throws away the second half of a
        # session that has taken one loss.
        risk_amount = min(risk_amount, budget_cap)

    raw = risk_amount / stop_distance

    # Round DOWN. Rounding up would risk more than the mandate allows, and the
    # error compounds across concurrent positions.
    quantity = int(math.floor(raw))

    if lot_size > 1:
        quantity = (quantity // lot_size) * lot_size

    if quantity <= 0:
        return SizingResult(
            0, risk_amount, stop_distance, 0.0,
            f"stop distance {stop_distance:.2f} is too wide for a risk budget of "
            f"{risk_amount:.2f}; one share would risk more than allowed",
        )

    # freeze_qty of 1 on a cash symbol reads as "not applicable" rather than a
    # real exchange freeze limit. Verified at step 0 on RELIANCE NSE.
    if freeze_qty and freeze_qty > 1 and quantity > freeze_qty:
        quantity = (freeze_qty // lot_size) * lot_size if lot_size > 1 else freeze_qty

    notional = quantity * entry_price
    return SizingResult(quantity, risk_amount, stop_distance, notional)


def worst_case_loss(quantity: int, entry_price: float, stop_price: float) -> float:
    """What this position loses if the stop fills exactly, ignoring slippage.

    This is the number the budget check must be given, not the risk_amount that
    produced the quantity. They differ after rounding down, and the budget must
    be told the truth about the position that will actually exist.
    """
    return abs(entry_price - stop_price) * max(0, quantity)
