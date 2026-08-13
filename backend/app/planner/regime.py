"""Regime read. Is today trend-shaped or chop-shaped.

PLAN.md Part 4. This exists because all three strategies are trend-following and
therefore fail in the same conditions. "Which strategy" is a weaker question than
"is today worth trading at all", and only a regime read can answer the second.

Two hard-won facts from step 0 and step 2, both of which produce silently wrong
numbers rather than errors:

  ta.adx returns a THREE-tuple and ADX is element [2], not [0]. Element [0] is
  +DI. Reading index 0 gives a number that moves plausibly and means something
  else entirely.

  ta.ema has NO NaN warm-up - it returns a finite value at bar 0 - while ta.adx
  and ta.atr correctly leave the warm-up blank. Any comparison between them must
  mask the warm-up region explicitly.

The regime is read PRE-OPEN, from history up to the previous close. It is never
recomputed mid-session: the plan is locked at 09:15 and a regime that flips at
noon would only produce thrash.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping

import pandas as pd
from openalgo import ta

log = logging.getLogger(__name__)

ADX_PERIOD = 14

# Read across this many recent bars rather than one, so a single spike on the
# last bar cannot decide a whole session.
REGIME_LOOKBACK_BARS = 75  # one session of 5m bars


@dataclass(frozen=True)
class RegimeRead:
    """What the market looked like going into today."""

    adx: float = 0.0
    adx_median: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0
    trending: bool = False
    direction: str = "none"      # up | down | none
    market_above_trend: bool = False
    breadth: float = 0.0         # share of basket symbols that are trending
    detail: str = ""

    @property
    def label(self) -> str:
        if not self.trending:
            return "chop"
        return f"trend_{self.direction}"


def _adx_triple(frame: pd.DataFrame, period: int = ADX_PERIOD) -> tuple[float, float, float, pd.Series]:
    """Return the latest (+DI, -DI, ADX) plus the ADX series.

    ta.adx returns (+DI, -DI, ADX). ADX is THIRD. Verified at step 0.
    """
    period = int(period)  # ta raises TypeError on a float period, and JSON gives floats
    plus_di, minus_di, adx = ta.adx(frame["high"], frame["low"], frame["close"], period)
    adx_s = pd.Series(adx, index=frame.index).dropna()
    if adx_s.empty:
        return 0.0, 0.0, 0.0, adx_s
    return (
        float(pd.Series(plus_di, index=frame.index).dropna().iloc[-1]),
        float(pd.Series(minus_di, index=frame.index).dropna().iloc[-1]),
        float(adx_s.iloc[-1]),
        adx_s,
    )


def read_symbol(frame: pd.DataFrame, *, adx_floor: float, period: int = ADX_PERIOD) -> RegimeRead:
    """Regime for one symbol."""
    if frame is None or len(frame) < period * 3:
        return RegimeRead(detail="insufficient history")

    plus_di, minus_di, adx, adx_s = _adx_triple(frame, period)
    tail = adx_s.tail(REGIME_LOOKBACK_BARS)
    median = float(tail.median()) if not tail.empty else 0.0

    # Median over the last session rather than the final bar. A single spike is
    # not a regime, and the final bar of a session is often the least typical.
    trending = median >= adx_floor
    direction = "none"
    if trending:
        direction = "up" if plus_di > minus_di else "down"

    return RegimeRead(
        adx=adx,
        adx_median=median,
        plus_di=plus_di,
        minus_di=minus_di,
        trending=trending,
        direction=direction,
        detail=f"adx median {median:.1f} over {len(tail)} bars against floor {adx_floor:.1f}",
    )


def read_basket(
    frames: Mapping[str, pd.DataFrame],
    *,
    adx_floor: float,
    market_frame: pd.DataFrame | None = None,
    market_ema_period: int = 50,
) -> RegimeRead:
    """Regime across the whole basket, plus the index trend filter.

    Universe-level, not per symbol. Selecting a strategy per symbol multiplies the
    decision surface by five on samples that are already short.
    """
    reads = {sym: read_symbol(f, adx_floor=adx_floor) for sym, f in frames.items()}
    usable = [r for r in reads.values() if r.adx_median > 0]
    if not usable:
        return RegimeRead(detail="no usable symbol history")

    breadth = sum(1 for r in usable if r.trending) / len(usable)
    median = sum(r.adx_median for r in usable) / len(usable)

    ups = sum(1 for r in usable if r.trending and r.direction == "up")
    downs = sum(1 for r in usable if r.trending and r.direction == "down")

    # A majority of the basket must be trending. One strong name against four
    # choppy ones is not a trending day for a basket strategy.
    trending = breadth > 0.5
    direction = "none"
    if trending:
        direction = "up" if ups >= downs else "down"

    market_above = False
    if market_frame is not None and len(market_frame) > market_ema_period:
        close = market_frame["close"]
        ema = pd.Series(ta.ema(close, int(market_ema_period)), index=close.index)
        market_above = bool(close.iloc[-1] > ema.iloc[-1])

    return RegimeRead(
        adx_median=median,
        trending=trending,
        direction=direction,
        market_above_trend=market_above,
        breadth=breadth,
        detail=(
            f"{sum(1 for r in usable if r.trending)}/{len(usable)} symbols trending, "
            f"mean adx median {median:.1f}, floor {adx_floor:.1f}, "
            f"index {'above' if market_above else 'below'} its {market_ema_period} EMA"
        ),
    )
