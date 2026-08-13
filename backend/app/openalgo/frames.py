"""LIVE session OHLCV frames, cleaned at the boundary and cached for 60 seconds.

Scope, stated first because it is the thing most likely to be got wrong later:

    This module serves the CURRENT session only. Completed sessions are immutable
    and belong in the persistent historical bar store at backend/app/data/bars.py.

TradingAgent had one cache for everything, with a 60-second TTL. That is right for a
live frame and wrong for backtest bars: a 5-minute candle from three weeks ago will
never change, so re-pulling 90 sessions across the basket through a rate-limited API
every morning at 08:45 buys nothing and costs the Planner its time budget. The split
is by lifetime, not by convenience:

    Live frame cache      current session 5m bars   60s TTL, in memory, here
    Historical bar store  completed sessions        persistent on disk, data/bars.py

The 60 seconds is the useful part of the TTL: within one bar-close evaluation the
same frame is read several times - the signal function, the stop calculation, the
regime check - and each of those must see the SAME bars. A shorter TTL would let a
new candle appear between two reads inside one decision.

This is deliberately NOT agno's Toolkit(cache_results=True): that one writes JSON to
disk and survives restarts, which is exactly wrong for market data.

Why cleaning is non-negotiable, and why it happens HERE rather than downstream:

    openalgo's _backend sma/rolling_sum are np.cumsum based, so a single NaN anywhere
    in the input poisons every subsequent output. Measured on 300 bars with one NaN
    injected at index 50, ta.sma(close, 14) returned 263/300 NaN - not 1 bad value,
    250. Broker history routinely has gaps.

    The order is fixed: to_numeric(errors="coerce") -> dropna(subset=OHLC) ->
    sort_index() -> dedupe index keeping last. Coercion first, so a stray string
    becomes NaN and is then dropped rather than exploding inside the Rust backend.
    Dedupe last, because a repeated timestamp from a re-sent bar carries the later,
    corrected values.

clean_ohlcv() is exported so the historical store applies the identical rule. Two
implementations of this cleaning would eventually disagree, and the backtest would
then measure a frame the executor never sees.

Other facts worth keeping:

  - history() returns a DataFrame on success and a DICT on error. isinstance is the
    only reliable check; an error dict is truthy.
  - Period arguments to openalgo.ta must be Python int. JSON decodes numbers to
    float, so int() coercion is mandatory before every ta call - that belongs to the
    caller, but frames are where the float usually enters.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections import OrderedDict
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .client import OpenAlgoClient, get_client

log = logging.getLogger(__name__)

CACHE_TTL_SEC = 60.0
CACHE_MAX_ENTRIES = 32

# The columns cleaning is allowed to touch, and the subset that must be complete for
# a bar to be usable at all. A bar with no volume is still a bar; a bar with no close
# is not.
NUMERIC_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume", "oi")
OHLC_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")

# Rough bars-per-day by interval, used to translate lookback_bars into a date range.
# Indian equity session is 6h15m = 375 minutes.
_BARS_PER_DAY = {
    "1m": 375, "3m": 125, "5m": 75, "10m": 37, "15m": 25,
    "30m": 13, "45m": 9, "60m": 7, "1h": 7, "D": 1, "W": 0.2, "M": 0.05,
}


def clean_ohlcv(raw: pd.DataFrame) -> pd.DataFrame:
    """Apply the non-negotiable cleaning rule to a candle frame.

    Args:
        raw: A frame as returned by history(), indexed by timestamp.

    Returns:
        A new frame: numeric-coerced, rows with an incomplete OHLC dropped, sorted by
        index, with duplicate timestamps collapsed to the last occurrence. May be
        empty if nothing survived.
    """
    df = raw.copy()
    for col in NUMERIC_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    essential = [c for c in OHLC_COLUMNS if c in df.columns]
    df = df.dropna(subset=essential) if essential else df.dropna()
    df = df.sort_index()
    return df[~df.index.duplicated(keep="last")]


class FrameCache:
    """In-memory, TTL-bounded, LRU-capped. One instance per process."""

    def __init__(self, client: OpenAlgoClient | None = None) -> None:
        self._client = client or get_client()
        self._store: OrderedDict[tuple, tuple[float, pd.DataFrame]] = OrderedDict()
        self._lock = threading.Lock()

    def _get(self, key: tuple) -> pd.DataFrame | None:
        with self._lock:
            hit = self._store.get(key)
            if hit is None:
                return None
            stamped, df = hit
            if time.monotonic() - stamped > CACHE_TTL_SEC:
                self._store.pop(key, None)
                return None
            self._store.move_to_end(key)
            return df

    def _put(self, key: tuple, df: pd.DataFrame) -> None:
        with self._lock:
            self._store[key] = (time.monotonic(), df)
            self._store.move_to_end(key)
            while len(self._store) > CACHE_MAX_ENTRIES:
                self._store.popitem(last=False)

    def clear(self) -> None:
        """Drop everything. Called on halt and at session boundaries."""
        with self._lock:
            self._store.clear()

    def _today(self) -> dt.date:
        """Today in the configured market timezone.

        The host clock is not necessarily Asia/Kolkata, and a date rolled early or
        late silently shifts the whole requested window by a session.
        """
        tz_name = getattr(self._client.settings, "timezone", "") or "Asia/Kolkata"
        try:
            return dt.datetime.now(ZoneInfo(tz_name)).date()
        except Exception:  # noqa: BLE001
            log.warning("unknown timezone %r, falling back to host date", tz_name)
            return dt.date.today()

    def date_range_for(self, interval: str, lookback_bars: int) -> tuple[str, str]:
        """Translate a bar count into a start/end date, padded for weekends."""
        per_day = _BARS_PER_DAY.get(interval, 75)
        days = max(2, int(lookback_bars / max(per_day, 0.01)) + 1)
        # Roughly 5 trading days per 7 calendar days, plus slack for holidays.
        calendar_days = int(days * 7 / 5) + 5
        calendar_days = min(calendar_days, 2000)
        end = self._today()
        start = end - dt.timedelta(days=calendar_days)
        return start.isoformat(), end.isoformat()

    def get_frame(self, symbol: str, exchange: str, interval: str,
                  start_date: str | None = None, end_date: str | None = None,
                  lookback_bars: int = 300, source: str = "api") -> dict[str, Any]:
        """Fetch candles for the live path, cleaned and sorted.

        Args:
            symbol: Trading symbol, case-insensitive.
            exchange: Venue. Index exchanges are quote-only but do return history,
                which is how the NIFTY market filter gets its frame.
            interval: One of KNOWN_INTERVALS. AutoAgent trades 5m.
            start_date: ISO date. Derived from lookback_bars when omitted.
            end_date: ISO date. Derived from lookback_bars when omitted.
            lookback_bars: Used only to derive a date range.
            source: SDK passthrough.

        Returns:
            {"ok": True, "frame": DataFrame, "cached": bool, "start_date", "end_date"}
            or {"ok": False, "error": str}. Never raises.
        """
        if not start_date or not end_date:
            start_date, end_date = self.date_range_for(interval, lookback_bars)

        key = (symbol.upper(), exchange.upper(), interval, start_date, end_date, source)
        cached = self._get(key)
        if cached is not None:
            return {"ok": True, "frame": cached, "cached": True,
                    "start_date": start_date, "end_date": end_date}

        try:
            raw = self._client.call(
                "history", symbol=symbol.upper(), exchange=exchange.upper(),
                interval=interval, start_date=start_date, end_date=end_date,
                source=source,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        # history returns a DataFrame on success and a dict on error.
        if not isinstance(raw, pd.DataFrame):
            message = "unknown error"
            if isinstance(raw, dict):
                message = str(raw.get("message") or raw.get("error") or raw)
            return {"ok": False, "error": message}

        if raw.empty:
            return {"ok": False,
                    "error": f"no candles for {symbol} on {exchange} at {interval} "
                             f"between {start_date} and {end_date}"}

        df = clean_ohlcv(raw)
        if df.empty:
            return {"ok": False, "error": "all candles were incomplete after cleaning"}

        dropped = len(raw) - len(df)
        if dropped:
            # Worth a log line: a gappy feed is one of the Part 7 halt conditions, and
            # this is the only place the gap is visible.
            log.info("cleaned %s %s %s: dropped %d of %d bars",
                     symbol.upper(), exchange.upper(), interval, dropped, len(raw))

        self._put(key, df)
        return {"ok": True, "frame": df, "cached": False,
                "start_date": start_date, "end_date": end_date}


_cache: FrameCache | None = None
_cache_lock = threading.Lock()


def get_frame_cache() -> FrameCache:
    """Process-wide singleton. Double-checked so the fast path takes no lock."""
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                _cache = FrameCache()
    return _cache
