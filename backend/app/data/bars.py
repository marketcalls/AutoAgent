"""Persistent historical 5-minute bar store (PLAN.md Part 7).

Two stores exist and they are not the same thing. `openalgo/frames.py` is a 60-second
in-memory TTL cache for the LIVE session - correct for quotes, wrong for backtest bars.
This module is the other half: completed sessions of 5m bars, on disk, immutable.

Why it exists at all. The Planner runs at 08:45 and needs 90 sessions across a
six-name basket. Measured against the live OpenAlgo instance on 2026-08-13:

    90 calendar days  -> 4,698 bars across  63 sessions
    180 calendar days -> 9,048 bars across 121 sessions

One call covers the whole lookback, so the first fill is cheap. Re-pulling it every
morning through a rate-limited API is not, and it buys nothing: a completed session of
5-minute bars never changes. After the first fill each morning costs exactly one call
per symbol, covering one new session.

Runtime facts this module was built against, all measured, not assumed:

  - `history` returns a DataFrame on success and a dict on error. Always isinstance
    check. This is the single most common way a caller of this API crashes.
  - The returned index is a tz-aware DatetimeIndex in Asia/Kolkata named "timestamp".
    Columns arrive alphabetically ordered (close, high, low, oi, open, volume), not in
    OHLCV order, so they are reordered on the way in.
  - Bars per completed session are NOT constant and NOT the same across instruments.
    Measured on 2026-08-13: RELIANCE on NSE returned 75 bars ending 15:25 up to
    2026-07-31, then 72 bars ending 15:10 from 2026-08-03 onward. NIFTY on NSE_INDEX
    returned 75 bars ending 15:25 throughout. A "session is complete when it has 75
    bars" or "...when its last bar is 15:25" test would therefore reject every recent
    equity session. Completeness is decided on the wall clock instead - see
    `_storable_through`.
  - NIFTY on NSE_INDEX does return 5m history. It is quote-only for trading, but the
    market filter in Part 5 needs its bars, so it is stored like any other symbol.
    Its volume column is all zeros; do not build a volume filter on an index.

NaN poisoning is why cleaning happens exactly once, here, at the boundary.
`openalgo.ta` rolling sums are np.cumsum based, so one NaN anywhere poisons every
later value: measured in TradingAgent on 300 bars with a single NaN injected at index
50, `ta.sma(close, 14)` returned 263/300 NaN. The rule is fixed and matches frames.py
exactly - to_numeric(errors="coerce") -> dropna(subset=OHLC) -> sort_index() ->
dedupe index keeping last.

Storage format: parquet, one file per symbol, via pyarrow (22.0.0 verified installed
before choosing it). Reasons, in order of weight:

  1. Round-trip fidelity. The tz-aware DatetimeIndex and the float64/int64 dtypes come
     back identical - verified with `read_parquet(...).equals(original)`. CSV returns
     strings and would need re-parsing and re-coercion on every read, which is a second
     cleaning boundary and therefore a second place for a NaN to enter.
  2. Size and speed. 90 sessions of 5m bars is roughly 6,700 rows; snappy-compressed
     parquet holds that in tens of kilobytes.
  3. Schema stability. Every write rewrites the whole file from one concatenated frame,
     so a column can never end up int64 in one row group and float64 in another.

"Appended" here means read-modify-write, not append-in-place: parquet has no in-place
append and pandas offers none. At this file size a full rewrite is milliseconds, and it
is what lets dedupe and sort run across the whole history rather than per chunk. Writes
go to a temp file and are then os.replace()d over the target, so a crash mid-write
cannot leave a truncated store behind.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Protocol

import pandas as pd

log = logging.getLogger(__name__)

# backend/app/data/bars.py -> backend/app/data -> backend/app -> backend -> AutoAgent
PROJECT_ROOT = Path(__file__).resolve().parents[3]

INTERVAL = "5m"
EXCHANGE_TZ = "Asia/Kolkata"

OHLC: tuple[str, ...] = ("open", "high", "low", "close")
NUMERIC_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume", "oi")
COLUMN_ORDER: tuple[str, ...] = ("open", "high", "low", "close", "volume", "oi")

# NSE cash equity closes at 15:30 IST. The settle margin is a safety choice, not a
# measurement: it costs nothing (the Planner runs pre-open, when yesterday is complete
# under any rule) and it removes any chance of freezing a session the feed had not
# finished writing. Before this boundary, today's bars belong to the live cache.
SESSION_CLOSE_IST = dt.time(15, 30)
SETTLE_MINUTES = 30

DEFAULT_SESSIONS = 90

# Measured, not assumed: 63 sessions per 90 calendar days and 121 per 180 give 0.70 and
# 0.672 sessions per calendar day. 1.5 calendar days per session plus a fixed slack
# clears both, and clears a run of exchange holidays.
CALENDAR_DAYS_PER_SESSION = 1.5
CALENDAR_SLACK_DAYS = 10

_PARQUET_ENGINE = "pyarrow"
_COMPRESSION = "snappy"


class HistoryFetch(Protocol):
    """Signature of the injected history fetcher.

    Returns whatever the OpenAlgo SDK returns: a DataFrame on success, a dict on error.
    The store checks the type; the fetcher does not need to.
    """

    def __call__(
        self,
        symbol: str,
        exchange: str,
        interval: str,
        start_date: str,
        end_date: str,
    ) -> Any: ...


# --------------------------------------------------------------------------- helpers


def _now_ist() -> dt.datetime:
    from zoneinfo import ZoneInfo

    return dt.datetime.now(ZoneInfo(EXCHANGE_TZ))


def _storable_through(now: dt.datetime | None = None) -> dt.date:
    """Latest session date that may be written to disk.

    A session in progress belongs to the live cache, never here. Completeness is a wall
    clock test rather than a bar-count or last-bar-time test, because bars per session
    are instrument-dependent and change without notice - see the module docstring.

    Returns today once the settle margin after the close has passed, otherwise
    yesterday. Whether that date is a trading day is not this function's problem: a
    holiday simply yields no bars for it.
    """
    now = now or _now_ist()
    close_plus_settle = (
        dt.datetime.combine(now.date(), SESSION_CLOSE_IST, tzinfo=now.tzinfo)
        + dt.timedelta(minutes=SETTLE_MINUTES)
    )
    return now.date() if now >= close_plus_settle else now.date() - dt.timedelta(days=1)


def _calendar_days_for(sessions: int) -> int:
    return int(sessions * CALENDAR_DAYS_PER_SESSION) + CALENDAR_SLACK_DAYS


def _session_dates(frame: pd.DataFrame) -> pd.Series:
    """Session date per bar, as a Series aligned on the frame index."""
    return pd.Series(frame.index.date, index=frame.index, name="session")


def clean_frame(frame: pd.DataFrame, *, tz: str = EXCHANGE_TZ) -> pd.DataFrame:
    """The one cleaning boundary. Identical in order and intent to frames.py.

    to_numeric(errors="coerce") -> dropna(subset=OHLC) -> sort_index() -> dedupe index
    keeping last. Coercion runs first so a stray string becomes NaN and is dropped here
    rather than exploding inside the Rust indicator backend later.

    Args:
        frame: raw OHLCV frame, indexed by timestamp or carrying a timestamp column.
        tz: exchange timezone. A naive index is assumed to be exchange-local and is
            localized, not converted - the alternative silently shifts every bar by
            5h30m.

    Returns:
        A cleaned copy in OHLCV column order.

    Raises:
        ValueError: if the frame carries none of the OHLC columns.
    """
    df = frame.copy()

    if not isinstance(df.index, pd.DatetimeIndex):
        if "timestamp" in df.columns:
            df = df.set_index("timestamp")
        df.index = pd.to_datetime(df.index, errors="coerce")
        df = df[df.index.notna()]

    df.index = (
        df.index.tz_localize(tz) if df.index.tz is None else df.index.tz_convert(tz)
    )
    df.index.name = "timestamp"

    for col in NUMERIC_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    essential = [c for c in OHLC if c in df.columns]
    if not essential:
        raise ValueError(f"frame carries no OHLC columns, only {list(df.columns)}")

    df = df.dropna(subset=essential)
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]

    ordered = [c for c in COLUMN_ORDER if c in df.columns]
    return df[ordered + [c for c in df.columns if c not in ordered]]


def _read_dotenv() -> dict[str, str]:
    """Minimal .env reader for the fallback client only.

    app/config.py owns settings. This exists so the store is usable at build step 1,
    before config.py and the ported openalgo client land, and is bypassed the moment
    either is importable.
    """
    env: dict[str, str] = {}
    path = PROJECT_ROOT / ".env"
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


_sdk_lock = threading.Lock()
_sdk_api: Any = None


def _sdk_fetch(
    symbol: str, exchange: str, interval: str, start_date: str, end_date: str
) -> Any:
    """Direct openalgo SDK fallback. One call per symbol per morning, so the absence of
    the ported client's token bucket is not a rate-limit risk here."""
    global _sdk_api
    if _sdk_api is None:
        with _sdk_lock:
            if _sdk_api is None:
                from openalgo import api

                env = _read_dotenv()
                key = os.environ.get("OPENALGO_API_KEY") or env.get("OPENALGO_API_KEY", "")
                if not key:
                    raise RuntimeError("OPENALGO_API_KEY is not set in the environment or .env")
                _sdk_api = api(
                    api_key=key,
                    host=os.environ.get("OPENALGO_HOST")
                    or env.get("OPENALGO_HOST")
                    or "http://127.0.0.1:5000",
                    version=os.environ.get("OPENALGO_API_VERSION")
                    or env.get("OPENALGO_API_VERSION")
                    or "v1",
                    timeout=float(
                        os.environ.get("OPENALGO_TIMEOUT")
                        or env.get("OPENALGO_TIMEOUT")
                        or 30.0
                    ),
                )
    return _sdk_api.history(
        symbol=symbol,
        exchange=exchange,
        interval=interval,
        start_date=start_date,
        end_date=end_date,
    )


def default_fetch(
    symbol: str, exchange: str, interval: str, start_date: str, end_date: str
) -> Any:
    """Prefer the ported client (token buckets, uniform envelope); fall back to the SDK.

    The fallback is not a permanent design. It exists because the bar store is build
    step 1 and app/openalgo/ arrives with it; once that package is present this branch
    is never taken.
    """
    try:
        from ..openalgo.client import get_client  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - any import failure means "not ported yet"
        return _sdk_fetch(symbol, exchange, interval, start_date, end_date)
    return get_client().call(
        "history",
        symbol=symbol,
        exchange=exchange,
        interval=interval,
        start_date=start_date,
        end_date=end_date,
    )


# ----------------------------------------------------------------------------- store


class BarStore:
    """Immutable on-disk history of completed 5-minute sessions.

    One parquet file per symbol under `data/bars/`, named `SYMBOL_EXCHANGE_5m.parquet`.
    The exchange is in the filename because NIFTY exists on both NSE and NSE_INDEX and
    they are different series; the public API still takes a bare symbol and resolves the
    file by glob, which is unambiguous as long as one symbol is not stored twice.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        interval: str = INTERVAL,
        fetch: HistoryFetch | Callable[..., Any] | None = None,
        tz: str = EXCHANGE_TZ,
        retain_sessions: int | None = None,
    ) -> None:
        if root is None:
            configured = os.environ.get("BARS_DIR") or _read_dotenv().get("BARS_DIR") or "data/bars"
            root = Path(configured)
            if not root.is_absolute():
                root = PROJECT_ROOT / root
        self.root = Path(root)
        self.interval = interval
        self.tz = tz
        # Retention is an open question in PLAN.md Part 11, so the default is to keep
        # everything ever fetched. 90 sessions is roughly 160 KB per symbol; a decade
        # is single-digit megabytes. Deleting history the vendor may no longer serve is
        # the expensive mistake, not disk.
        self.retain_sessions = retain_sessions
        self._fetch: Callable[..., Any] = fetch or default_fetch
        self._lock = threading.Lock()
        # Proof that the store did what it claims. ensure_history reports `fetched`;
        # these accumulate across a process so a caller can assert "no network".
        self.api_calls = 0
        self.disk_reads = 0

    # -- paths ------------------------------------------------------------

    def path_for(self, symbol: str, exchange: str | None = None) -> Path | None:
        """Resolve the file for a symbol. Returns None if it is not stored yet and no
        exchange was given to construct the name from."""
        symbol = symbol.upper()
        if exchange:
            return self.root / f"{symbol}_{exchange.upper()}_{self.interval}.parquet"
        matches = sorted(self.root.glob(f"{symbol}_*_{self.interval}.parquet"))
        if len(matches) > 1:
            raise ValueError(
                f"{symbol} is stored under more than one exchange "
                f"({[m.name for m in matches]}); pass exchange explicitly"
            )
        return matches[0] if matches else None

    def exchange_of(self, symbol: str) -> str | None:
        """Exchange a stored symbol was fetched from, read back off the filename."""
        path = self.path_for(symbol)
        if path is None:
            return None
        stem = path.stem  # SYMBOL_EXCHANGE_5m
        return stem[len(symbol.upper()) + 1 : -(len(self.interval) + 1)]

    # -- read -------------------------------------------------------------

    def _read(self, symbol: str, exchange: str | None = None) -> pd.DataFrame | None:
        path = self.path_for(symbol, exchange)
        if path is None or not path.exists():
            return None
        self.disk_reads += 1
        frame = pd.read_parquet(path, engine=_PARQUET_ENGINE)
        # Stored data was cleaned before it was written; re-cleaning on read would be a
        # second boundary and would hide a corrupt file instead of surfacing it.
        return frame

    def get_frame(self, symbol: str, sessions: int | None = None) -> pd.DataFrame:
        """Read stored bars as one continuous multi-session frame.

        Continuity matters: EMA(30) on 5m bars needs roughly 30 bars of warm-up and a
        session is only about 75, so restarting the series each morning leaves the
        indicator invalid until nearly midday. Callers get an unbroken frame and slice
        it themselves.

        Args:
            symbol: trading symbol, case-insensitive.
            sessions: keep only the most recent N sessions. None keeps everything.

        Returns:
            Cleaned OHLCV frame indexed by tz-aware timestamp. Empty frame if the symbol
            has never been stored.
        """
        frame = self._read(symbol)
        if frame is None or frame.empty:
            return pd.DataFrame(columns=list(OHLC) + ["volume"])
        if sessions is None:
            return frame
        dates = _session_dates(frame)
        keep = sorted(dates.unique())[-int(sessions) :]
        return frame.loc[dates.isin(keep)]

    def stored_sessions(self, symbol: str) -> list[dt.date]:
        """Every session date on disk for this symbol, ascending."""
        frame = self._read(symbol)
        if frame is None or frame.empty:
            return []
        return sorted(_session_dates(frame).unique())

    def last_stored_session(self, symbol: str) -> dt.date | None:
        """Newest session date on disk, or None if the symbol is not stored."""
        sessions = self.stored_sessions(symbol)
        return sessions[-1] if sessions else None

    # -- write ------------------------------------------------------------

    def _write(self, path: Path, frame: pd.DataFrame) -> None:
        """Atomic whole-file rewrite. A crash mid-write must not truncate the store."""
        if self.retain_sessions:
            dates = _session_dates(frame)
            keep = sorted(dates.unique())[-int(self.retain_sessions) :]
            frame = frame.loc[dates.isin(keep)]
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        frame.to_parquet(tmp, engine=_PARQUET_ENGINE, compression=_COMPRESSION, index=True)
        os.replace(tmp, path)

    def _merge(self, stored: pd.DataFrame | None, incoming: pd.DataFrame) -> pd.DataFrame:
        if stored is None or stored.empty:
            return incoming
        merged = pd.concat([stored, incoming])
        merged = merged.sort_index()
        # keep="last" means the incoming bar wins a collision. Callers filter incoming
        # to strictly new sessions before merging, so a collision only happens on an
        # explicit replace.
        return merged[~merged.index.duplicated(keep="last")]

    def append_session(
        self, symbol: str, frame: pd.DataFrame, *, exchange: str | None = None,
        replace: bool = False,
    ) -> int:
        """Append exactly one completed session to the store.

        Args:
            symbol: trading symbol.
            frame: bars for a single session date. Cleaned here; callers need not.
            exchange: required the first time a symbol is stored, to name the file.
            replace: overwrite a session date already on disk. Off by default because
                completed sessions are immutable - a silent overwrite would let a bad
                fetch destroy good history.

        Returns:
            Number of bars written for that session.

        Raises:
            ValueError: if the frame spans more than one session, if the session is not
                complete yet, if the session is already stored and replace is False, or
                if the symbol is new and no exchange was given.
        """
        cleaned = clean_frame(frame, tz=self.tz)
        if cleaned.empty:
            return 0

        dates = sorted(_session_dates(cleaned).unique())
        if len(dates) != 1:
            raise ValueError(f"append_session takes one session, got {len(dates)}: {dates}")
        session = dates[0]

        boundary = _storable_through()
        if session > boundary:
            raise ValueError(
                f"session {session} is not complete yet (storable through {boundary}); "
                "bars for a session in progress belong to the live cache"
            )

        with self._lock:
            path = self.path_for(symbol, exchange)
            if path is None:
                raise ValueError(f"{symbol} is not stored yet; pass exchange to create it")
            stored = self._read(symbol, exchange)
            if stored is not None and session in set(_session_dates(stored).unique()):
                if not replace:
                    raise ValueError(
                        f"session {session} for {symbol} is already stored and completed "
                        "sessions are immutable; pass replace=True to overwrite"
                    )
                stored = stored.loc[_session_dates(stored) != session]
            self._write(path, self._merge(stored, cleaned))

        return len(cleaned)

    # -- fill -------------------------------------------------------------

    def ensure_history(
        self,
        symbol: str,
        exchange: str,
        sessions: int = DEFAULT_SESSIONS,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Bring the store up to the last completed session, fetching the minimum.

        Three paths, and the point of the module is that the third is the usual one:

          full        no file yet, or force, or stored history does not reach back far
                      enough for the requested lookback. One call covers the window.
          incremental stored but stale. One call covering the missing days only.
          disk        already current. No network call at all.

        A symbol whose listed history is shorter than the requested lookback takes the
        full path every morning. That costs the same ONE call as the incremental path -
        rate limits count calls, not bars - so it is left alone rather than tracked.

        `sessions` is a floor on what must be present, never a ceiling on what is kept.
        Part 4 uses two windows, 90 sessions and 15, against the same files; if the
        short-window call trimmed the store to 15 the long-window call would refetch
        everything the next time it ran. Windowing is a read-time concern - see
        `get_frame` - and retention belongs to `retain_sessions`.

        Args:
            symbol: trading symbol.
            exchange: NSE for cash equity, NSE_INDEX for NIFTY.
            sessions: minimum lookback in completed sessions. 90 is the Part 4 long
                window.
            force: refetch the whole window and replace the file. The repair path for a
                store suspected of holding bad bars.

        Returns:
            A result dict. `fetched` is False when the call was served entirely from
            disk, which is the assertion worth making in tests.
        """
        symbol, exchange = symbol.upper(), exchange.upper()
        boundary = _storable_through()
        today = _now_ist().date()
        target_start = today - dt.timedelta(days=_calendar_days_for(sessions))

        with self._lock:
            path = self.root / f"{symbol}_{exchange}_{self.interval}.parquet"
            stored = None if force else self._read(symbol, exchange)
            stored_dates = (
                sorted(_session_dates(stored).unique()) if stored is not None and not stored.empty else []
            )
            last = stored_dates[-1] if stored_dates else None

            if not stored_dates or force:
                mode, start = "full", target_start
            elif len(stored_dates) < sessions and stored_dates[0] > target_start:
                mode, start = "full", target_start
            elif last is not None and last >= boundary:
                result = self._describe(symbol, exchange, path, stored, mode="disk", fetched=False)
                log.info("bars %s: already current through %s, no api call", symbol, last)
                return result
            else:
                mode, start = "incremental", last + dt.timedelta(days=1)  # type: ignore[operator]

            raw = self._fetch(
                symbol=symbol,
                exchange=exchange,
                interval=self.interval,
                start_date=start.isoformat(),
                end_date=today.isoformat(),
            )
            self.api_calls += 1

            # history returns a DataFrame on success and a dict on error, always.
            if not isinstance(raw, pd.DataFrame):
                message = "unknown error"
                if isinstance(raw, dict):
                    message = str(raw.get("message") or raw.get("error") or raw)
                return {
                    "ok": False, "symbol": symbol, "exchange": exchange, "mode": mode,
                    "fetched": True, "error": message,
                }

            fetched_bars = len(raw)
            cleaned = clean_frame(raw, tz=self.tz)

            # Never store a partial session, and never rewrite one already frozen.
            dates = _session_dates(cleaned)
            keep = dates <= boundary
            if mode == "incremental" and last is not None:
                keep &= dates > last
            cleaned = cleaned.loc[keep]

            # force replaces the file; a coverage-driven full fill merges, because the
            # vendor's window may no longer reach as far back as the store already does.
            merged = cleaned if force else self._merge(stored, cleaned)
            if merged.empty:
                return {
                    "ok": False, "symbol": symbol, "exchange": exchange, "mode": mode,
                    "fetched": True, "fetched_bars": fetched_bars,
                    "error": "no complete sessions in the fetched window",
                }

            self._write(path, merged)
            written_dates = sorted(_session_dates(merged).unique())

        result = self._describe(symbol, exchange, path, merged, mode=mode, fetched=True)
        result["fetched_bars"] = fetched_bars
        # Sessions on disk now that were not on disk before, counted after any retention
        # trim so the number describes the file rather than the fetch.
        result["appended_sessions"] = len(set(written_dates) - set(stored_dates))
        log.info(
            "bars %s: %s fill, %d bars over %d sessions through %s",
            symbol, mode, result["bars"], result["sessions"], result["last_session"],
        )
        return result

    def _describe(
        self, symbol: str, exchange: str, path: Path, frame: pd.DataFrame | None,
        *, mode: str, fetched: bool,
    ) -> dict[str, Any]:
        dates = sorted(_session_dates(frame).unique()) if frame is not None and not frame.empty else []
        return {
            "ok": True,
            "symbol": symbol,
            "exchange": exchange,
            "path": str(path),
            "mode": mode,
            "fetched": fetched,
            "bars": 0 if frame is None else len(frame),
            "sessions": len(dates),
            "first_session": dates[0].isoformat() if dates else None,
            "last_session": dates[-1].isoformat() if dates else None,
            "bytes": path.stat().st_size if path.exists() else 0,
        }


# ------------------------------------------------------------------- module surface

_store: BarStore | None = None
_store_lock = threading.Lock()


def get_bar_store() -> BarStore:
    """Process-wide default store, rooted at BARS_DIR (default data/bars)."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = BarStore()
    return _store


def ensure_history(
    symbol: str, exchange: str, sessions: int = DEFAULT_SESSIONS, *, force: bool = False
) -> dict[str, Any]:
    """See BarStore.ensure_history."""
    return get_bar_store().ensure_history(symbol, exchange, sessions, force=force)


def get_frame(symbol: str, sessions: int | None = None) -> pd.DataFrame:
    """See BarStore.get_frame."""
    return get_bar_store().get_frame(symbol, sessions)


def append_session(
    symbol: str, frame: pd.DataFrame, *, exchange: str | None = None, replace: bool = False
) -> int:
    """See BarStore.append_session."""
    return get_bar_store().append_session(symbol, frame, exchange=exchange, replace=replace)


def last_stored_session(symbol: str) -> dt.date | None:
    """See BarStore.last_stored_session."""
    return get_bar_store().last_stored_session(symbol)
