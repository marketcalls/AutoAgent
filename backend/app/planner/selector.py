"""Daily strategy selection. Deterministic, with hysteresis and a viability gate.

PLAN.md Part 4. The model does not make this choice; it explains it. Everything
here is plain Python so the same inputs always produce the same plan, and so the
selection can be replayed over history to see whether it ever helped.

The order of the gates matters, and it is the opposite of the obvious one:

    1. Is ANY strategy viable at all?   If not -> trade nothing. Stop.
    2. What regime is it?               Picks the candidate.
    3. Does the candidate clear the incumbent by the hysteresis margin?

Viability comes FIRST because of what step 4 measured. All three strategies had
negative expectancy over 93 sessions on both windows, with profit factors between
0.13 and 0.73. A selector that always returns its best candidate would have
picked a loser every single morning and called it a decision. Choosing the least
bad of three losing strategies is choosing how to lose.

So "trade nothing" is a first-class outcome here, not an error path. It is what
the system should do far more often than a naive selector would.

Hysteresis exists because the alternative is thrash. Without a switching margin
the pick flips on noise, and you end up owning the average of three strategies
plus the turnover of changing between them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from ..backtest.metrics import Metrics
from .regime import RegimeRead

log = logging.getLogger(__name__)

# Which strategy suits which regime. All three are trend-following, so this maps
# regime STRENGTH and direction onto strategy speed and direction, not onto
# different kinds of edge.
REGIME_PREFERENCE: dict[str, tuple[str, ...]] = {
    # Strong trend, upward: the fast long-only one first.
    "trend_up": ("stg1_ema_10_20", "stg2_supertrend_3_10", "stg3_sma10_ema30"),
    # Strong trend, either direction: the bidirectional one first.
    "trend_down": ("stg2_supertrend_3_10", "stg3_sma10_ema30", "stg1_ema_10_20"),
    # Weak or mixed: the slowest, which whipsaws least.
    "chop": ("stg3_sma10_ema30", "stg2_supertrend_3_10", "stg1_ema_10_20"),
}

NO_TRADE = "none"


@dataclass
class Selection:
    """The morning decision, and enough context to explain it."""

    strategy_id: str = NO_TRADE
    risk_fraction: float | None = None
    regime: str = ""
    reason: str = ""
    candidates: list[str] = field(default_factory=list)
    viable: list[str] = field(default_factory=list)
    incumbent: str = ""
    switched: bool = False
    trade_today: bool = False

    def as_dict(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "risk_fraction": self.risk_fraction,
            "regime": self.regime,
            "reason": self.reason,
            "candidates": self.candidates,
            "viable": self.viable,
            "incumbent": self.incumbent,
            "switched": self.switched,
            "trade_today": self.trade_today,
        }


def _score(long: Metrics | None, short: Metrics | None) -> float:
    """Blend the two windows into one comparable number.

    Weighted toward the LONG window. The short window is there to detect a
    regime change, not to drive the choice: fifteen sessions of intraday trades
    is a small sample and chasing it is how a selector becomes a performance
    chaser.

    A short window under the significance threshold is ignored entirely rather
    than being allowed to vote with noise.
    """
    if long is None:
        return float("-inf")
    if short is None or not short.is_significant:
        return long.expectancy_r
    return 0.7 * long.expectancy_r + 0.3 * short.expectancy_r


def select(
    metrics: Sequence[Metrics],
    regime: RegimeRead,
    *,
    hysteresis_margin: float,
    incumbent: str = "",
    risk_fraction_base: float = 0.005,
) -> Selection:
    """Pick one strategy for the session, or decline to trade."""
    by_strategy: dict[str, dict[str, Metrics]] = {}
    for m in metrics:
        by_strategy.setdefault(m.strategy_id, {})[m.window] = m

    sel = Selection(regime=regime.label, incumbent=incumbent)
    sel.candidates = sorted(by_strategy)

    # ---- gate 1: is anything worth trading at all -------------------------
    viable = {
        sid: windows
        for sid, windows in by_strategy.items()
        if (windows.get("long") is not None and windows["long"].is_viable)
    }
    sel.viable = sorted(viable)

    if not viable:
        best = max(
            ((sid, _score(w.get("long"), w.get("short"))) for sid, w in by_strategy.items()),
            key=lambda pair: pair[1],
            default=("", float("-inf")),
        )
        sel.strategy_id = NO_TRADE
        sel.trade_today = False
        sel.reason = (
            "no strategy is viable: none has positive expectancy over the long "
            f"window with enough trades to believe it. Best of a bad set was "
            f"{best[0] or 'n/a'} at {best[1]:+.3f}R. Trading nothing today."
        )
        log.warning("selection: %s", sel.reason)
        return sel

    # ---- gate 2: regime picks the preferred order --------------------------
    order = REGIME_PREFERENCE.get(regime.label, REGIME_PREFERENCE["chop"])
    ranked = sorted(
        viable,
        key=lambda sid: (
            order.index(sid) if sid in order else len(order),
            -_score(viable[sid].get("long"), viable[sid].get("short")),
        ),
    )
    challenger = ranked[0]

    # ---- gate 3: hysteresis ------------------------------------------------
    if incumbent and incumbent in viable and incumbent != challenger:
        inc_score = _score(viable[incumbent].get("long"), viable[incumbent].get("short"))
        cha_score = _score(viable[challenger].get("long"), viable[challenger].get("short"))
        if cha_score - inc_score < hysteresis_margin:
            sel.strategy_id = incumbent
            sel.trade_today = True
            sel.risk_fraction = risk_fraction_base
            sel.reason = (
                f"keeping {incumbent}: {challenger} scores {cha_score:+.3f}R against "
                f"{inc_score:+.3f}R, a margin of {cha_score - inc_score:+.3f}R which is "
                f"under the {hysteresis_margin:.3f}R needed to switch"
            )
            return sel
        sel.switched = True

    sel.strategy_id = challenger
    sel.trade_today = True
    sel.risk_fraction = risk_fraction_base
    long_m = viable[challenger].get("long")
    sel.reason = (
        f"regime is {regime.label} ({regime.detail}); {challenger} is the preferred "
        f"viable strategy at {long_m.expectancy_r:+.3f}R expectancy over "
        f"{long_m.trades} trades"
        + (f", switched from {incumbent}" if sel.switched else "")
    )
    return sel


def scale_risk(
    selection: Selection,
    metrics: Mapping[str, Metrics],
    *,
    base: float,
    floor: float,
) -> float:
    """Scale the risk fraction DOWN on weaker evidence. Never up.

    The mandate ceiling is `base`. Everything here can only reduce it, which is
    the same rule the Planner's own proposal is held to.
    """
    if not selection.trade_today:
        return floor
    m = metrics.get(selection.strategy_id)
    if m is None or not m.is_significant:
        return floor
    # Full size needs a clearly positive expectancy. Half an R of edge is not a
    # reason to press.
    quality = min(1.0, max(0.25, m.expectancy_r / 0.25)) if m.expectancy_r > 0 else 0.25
    return max(floor, min(base, base * quality))
