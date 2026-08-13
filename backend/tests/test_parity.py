"""The step 2 build gate: backtest adapter and execution adapter must agree.

PLAN.md Part 3:

    Run the execution adapter bar by bar over a historical frame, feeding it one
    bar at a time as if live. Assert the resulting signal series is IDENTICAL to
    the backtest adapter's vectorised run over the same frame.

Any difference means the live path and the measured path have diverged, and the
morning selection is then measuring a strategy that will not trade. On a mismatch
this file reports the first differing bar, both values, and which of the three
usual causes it is:

    1. lookahead - the vectorised run peeking at a bar the incremental path
       cannot see. Detected by re-running the vectorised path over bars 0..i and
       checking whether its answer at bar i changes.
    2. warm-up - the incremental path starting with fewer bars than the
       vectorised one.
    3. float accumulation drift - a different accumulation order in a recursive
       indicator, EMA above all.

Booleans are compared exactly and always will be; only stop_price carries a
tolerance, and the run prints the largest difference it actually observed so the
tolerance stays a measured number rather than an assumed one.

Live data is RELIANCE 5m over 60 calendar days from OpenAlgo at
http://127.0.0.1:5000. If the server is unreachable the live section reports SKIP
and the gate still runs in full against a deterministic synthetic frame.

Run:  python backend/tests/test_parity.py
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

# Indian market data carries the rupee sign and a cp1252 console raises on it.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.strategies import (  # noqa: E402
    BOOL_COLUMNS,
    DEFAULT_TAIL_BARS,
    SIGNAL_COLUMNS,
    STRATEGIES,
    BacktestAdapter,
    ExecutionAdapter,
    StrategyError,
    get_strategy,
    signal_rows_to_frame,
    strategy_ids,
)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str]] = []

# stop_price is a float level rebuilt from a truncated history, so it is compared
# with a tolerance. 1e-9 relative is roughly 1.3e-6 rupees on a 1300 rupee stock:
# five orders of magnitude tighter than one tick, and still far above the drift
# a 750-bar tail actually produces. Booleans get no tolerance at all.
STOP_RTOL = 1e-9
STOP_ATOL = 1e-9


def check(name: str, condition: bool, detail: str = "") -> bool:
    status = PASS if condition else FAIL
    results.append((name, status))
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))
    return bool(condition)


def skip(name: str, detail: str = "") -> None:
    results.append((name, SKIP))
    print(f"  [{SKIP}] {name}" + (f" - {detail}" if detail else ""))


def note(text: str) -> None:
    print(f"        {text}")


# --------------------------------------------------------------------- frames


def synthetic_frame(n: int = 1200, seed: int = 20260813) -> pd.DataFrame:
    """A deterministic 5-minute frame that all three strategies trade.

    Drifting random walk with a slow sine on top, so the trend reverses often
    enough to produce crossovers in both directions without being pure noise.
    """
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 2.0, n)
    swing = 18.0 * np.sin(np.linspace(0, 9 * np.pi, n))
    close = 1300.0 + np.cumsum(steps) + swing
    high = close + rng.uniform(0.3, 4.0, n)
    low = close - rng.uniform(0.3, 4.0, n)
    open_ = close + rng.normal(0.0, 1.2, n)
    volume = rng.uniform(5e4, 5e5, n)
    index = pd.date_range("2026-05-04 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    frame = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )
    frame.index.name = "timestamp"
    return frame


def clean_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """The non-negotiable cleaning from PLAN.md Part 7.

    One NaN poisons every later value of a cumsum-based indicator, so this runs
    once at the boundary and never again downstream.
    """
    frame = frame.copy()
    for col in ("open", "high", "low", "close", "volume"):
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    frame = frame.sort_index()
    return frame[~frame.index.duplicated(keep="last")]


def read_env() -> dict[str, str]:
    """Read .env by hand. No secret is ever printed, only whether one exists."""
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def fetch_live_frame(days: int = 60) -> tuple[pd.DataFrame | None, str]:
    """RELIANCE 5m bars over `days` calendar days, or a reason it is unavailable."""
    env = read_env()
    api_key = env.get("OPENALGO_API_KEY", "")
    host = env.get("OPENALGO_HOST", "http://127.0.0.1:5000")
    if not api_key:
        return None, "OPENALGO_API_KEY missing from .env"
    try:
        from openalgo import api

        client = api(api_key=api_key, host=host, version="v1", timeout=20.0)
        today = datetime.now().date()
        frame = client.history(
            symbol="RELIANCE",
            exchange="NSE",
            interval="5m",
            start_date=(today - timedelta(days=days)).isoformat(),
            end_date=today.isoformat(),
        )
    except Exception as exc:  # noqa: BLE001 - any transport failure is a SKIP
        return None, f"{type(exc).__name__}: {str(exc)[:80]}"
    # history returns a DataFrame on success and a dict on error. Always check.
    if not isinstance(frame, pd.DataFrame):
        message = frame.get("message", frame) if isinstance(frame, dict) else frame
        return None, f"history error: {str(message)[:80]}"
    if frame.empty:
        return None, "history returned no bars"
    return clean_frame(frame), host


# ------------------------------------------------------------------ diagnosis


def values_match(column: str, left: Any, right: Any) -> bool:
    if column in BOOL_COLUMNS:
        return bool(left) == bool(right)
    return bool(
        np.isclose(
            float(left), float(right), rtol=STOP_RTOL, atol=STOP_ATOL, equal_nan=True
        )
    )


def diagnose(
    strategy: ModuleType,
    params: dict[str, Any] | None,
    frame: pd.DataFrame,
    i: int,
    column: str,
    live_value: Any,
    vector_value: Any,
    tail_bars: int,
    warmup: int,
) -> str:
    """Name which of the three usual causes produced a mismatch at bar i."""
    prefix = BacktestAdapter(strategy, params).run(frame.iloc[: i + 1])
    prefix_value = prefix.iloc[-1][column]
    if not values_match(column, prefix_value, vector_value):
        return (
            "LOOKAHEAD in the vectorised path: re-running it over bars 0..%d gives "
            "%r at that bar, the full-frame run gives %r" % (i, prefix_value, vector_value)
        )
    if i + 1 <= tail_bars:
        return (
            "WARM-UP difference: at bar %d the execution buffer held every bar from 0, "
            "the same history the vectorised run used, so the divergence is in the "
            "adapter's buffering rather than in history length" % i
        )
    if column not in BOOL_COLUMNS:
        gap = abs(float(live_value) - float(vector_value))
        if np.isfinite(gap) and gap < 1e-3:
            return (
                "FLOAT ACCUMULATION DRIFT: %.3e apart with a %d-bar tail; raise "
                "DEFAULT_TAIL_BARS or widen the stop_price tolerance" % (gap, tail_bars)
            )
    return (
        "WARM-UP difference: the %d-bar execution tail is too short at bar %d "
        "(warm-up is %d bars); the seed of a recursive indicator has not decayed"
        % (tail_bars, i, warmup)
    )


# ---------------------------------------------------------------- the gate


def run_parity(strategy: ModuleType, frame: pd.DataFrame, label: str,
               params: dict[str, Any] | None = None) -> None:
    """Feed the execution adapter bar by bar and compare against one vector run."""
    name = str(strategy.STRATEGY_ID)
    vector = BacktestAdapter(strategy, params).run(frame)
    adapter = ExecutionAdapter(strategy, params)

    started = time.perf_counter()
    rows = [adapter.on_bar(frame.iloc[[i]]) for i in range(len(frame))]
    live = signal_rows_to_frame(rows)
    elapsed = time.perf_counter() - started

    check(
        f"{name} [{label}] execution adapter covers every bar",
        len(live) == len(frame) and live.index.equals(frame.index),
        f"{len(live)} rows in {elapsed:.1f}s, tail={adapter.tail_bars}, "
        f"warmup={adapter.warmup_bars}",
    )

    for column in BOOL_COLUMNS:
        left = live[column].to_numpy(dtype=bool)
        right = vector[column].to_numpy(dtype=bool)
        identical = bool(np.array_equal(left, right))
        detail = f"{int(right.sum())} signals"
        if not identical:
            i = int(np.flatnonzero(left != right)[0])
            cause = diagnose(
                strategy, params, frame, i, column, left[i], right[i],
                adapter.tail_bars, adapter.warmup_bars,
            )
            detail = (
                f"first mismatch at bar {i} ({frame.index[i]}): "
                f"execution={left[i]} backtest={right[i]}"
            )
            check(f"{name} [{label}] {column} identical", False, detail)
            note(f"cause: {cause}")
            note(f"total differing bars: {int((left != right).sum())} of {len(left)}")
            continue
        check(f"{name} [{label}] {column} identical", True, detail)

    left_stop = live["stop_price"].to_numpy(dtype=float)
    right_stop = vector["stop_price"].to_numpy(dtype=float)
    close = np.isclose(
        left_stop, right_stop, rtol=STOP_RTOL, atol=STOP_ATOL, equal_nan=True
    )
    both_finite = np.isfinite(left_stop) & np.isfinite(right_stop)
    worst = (
        float(np.max(np.abs(left_stop[both_finite] - right_stop[both_finite])))
        if both_finite.any()
        else 0.0
    )
    if not bool(close.all()):
        i = int(np.flatnonzero(~close)[0])
        cause = diagnose(
            strategy, params, frame, i, "stop_price", left_stop[i], right_stop[i],
            adapter.tail_bars, adapter.warmup_bars,
        )
        check(
            f"{name} [{label}] stop_price within tolerance", False,
            f"first mismatch at bar {i} ({frame.index[i]}): "
            f"execution={left_stop[i]!r} backtest={right_stop[i]!r}",
        )
        note(f"cause: {cause}")
    else:
        check(
            f"{name} [{label}] stop_price within tolerance", True,
            f"max observed difference {worst:.3e} over {int(both_finite.sum())} bars",
        )

    # NaN placement is part of the contract: a bar with no valid stop must be NaN
    # in both paths, not zero in one of them.
    check(
        f"{name} [{label}] stop_price NaN placement identical",
        bool(np.array_equal(np.isnan(left_stop), np.isnan(right_stop))),
        f"{int(np.isnan(right_stop).sum())} NaN bars",
    )


def run_signal_sanity(strategy: ModuleType, frame: pd.DataFrame, label: str) -> None:
    """Counts and shapes that a broken-but-consistent strategy would still fail."""
    name = str(strategy.STRATEGY_ID)
    signals = BacktestAdapter(strategy).run(frame)
    warmup = int(strategy.warmup_bars())
    close = frame["close"].to_numpy(dtype=float)

    long_entry = signals["long_entry"].to_numpy(bool)
    long_exit = signals["long_exit"].to_numpy(bool)
    entries, exits = int(long_entry.sum()), int(long_exit.sum())
    check(
        f"{name} [{label}] produces long signals", entries > 0 and exits > 0,
        f"{entries} entries, {exits} exits over {len(frame)} bars",
    )

    # exrem must have collapsed each run to one signal, so entries and exits
    # strictly alternate. Two entries with no exit between them means a position
    # would be doubled.
    marks = ["E" if long_entry[i] else "X"
             for i in np.flatnonzero(long_entry | long_exit)]
    alternates = all(a != b for a, b in zip(marks, marks[1:]))
    check(f"{name} [{label}] entries and exits alternate", alternates,
          "".join(marks[:12]) + ("..." if len(marks) > 12 else ""))

    check(
        f"{name} [{label}] no signal inside the warm-up region",
        not signals.iloc[:warmup][list(BOOL_COLUMNS)].to_numpy(bool).any(),
        f"warm-up {warmup} bars",
    )
    check(
        f"{name} [{label}] stop_price is NaN through the warm-up",
        bool(signals.iloc[:warmup]["stop_price"].isna().all()),
    )

    long_bars = long_entry
    stop = signals["stop_price"].to_numpy(float)
    check(
        f"{name} [{label}] long stop sits below the close on every entry bar",
        bool(np.all(stop[long_bars] < close[long_bars])),
        f"worst gap {float(np.min(close[long_bars] - stop[long_bars])):.2f}"
        if long_bars.any() else "no entries",
    )

    if str(strategy.DIRECTION) == "long":
        check(
            f"{name} [{label}] long-only strategy never emits a short signal",
            not signals[["short_entry", "short_exit"]].to_numpy(bool).any(),
        )
    else:
        short_bars = signals["short_entry"].to_numpy(bool)
        check(
            f"{name} [{label}] short stop sits above the close on every entry bar",
            bool(np.all(stop[short_bars] > close[short_bars])),
            f"{int(short_bars.sum())} short entries",
        )


def run_lookahead_probe(strategy: ModuleType, frame: pd.DataFrame, label: str) -> None:
    """A signal at bar i may read bars 0..i only.

    Distinct from the parity run: this compares the vectorised path against
    itself, so it isolates a forward reference inside the strategy from anything
    the execution adapter's buffering might be doing.
    """
    name = str(strategy.STRATEGY_ID)
    vector = BacktestAdapter(strategy).run(frame)
    warmup = int(strategy.warmup_bars())
    probes = np.linspace(warmup + 1, len(frame) - 1, 12).astype(int).tolist()
    worst_stop = 0.0
    bad: list[int] = []
    for i in probes:
        prefix = BacktestAdapter(strategy).run(frame.iloc[: i + 1]).iloc[-1]
        full = vector.iloc[i]
        same = all(bool(prefix[c]) == bool(full[c]) for c in BOOL_COLUMNS)
        left, right = float(prefix["stop_price"]), float(full["stop_price"])
        if np.isfinite(left) and np.isfinite(right):
            worst_stop = max(worst_stop, abs(left - right))
        if not same or not values_match("stop_price", left, right):
            bad.append(i)
    check(
        f"{name} [{label}] no forward reference at {len(probes)} sampled bars",
        not bad,
        f"max stop difference {worst_stop:.3e}" if not bad else f"bars {bad}",
    )


# ------------------------------------------------------------------- contract


def test_contract() -> None:
    print("\n=== contract ===")
    frame = synthetic_frame(400)
    check("three strategies registered",
          strategy_ids() == ["stg1_ema_10_20", "stg2_supertrend_3_10",
                             "stg3_sma10_ema30"], str(strategy_ids()))
    try:
        get_strategy("stg4")
        check("unknown strategy id refused", False, "no error")
    except StrategyError as exc:
        check("unknown strategy id refused", "known:" in str(exc), str(exc)[:60])

    for strategy_id, strategy in STRATEGIES.items():
        signals = BacktestAdapter(strategy).run(frame)
        check(f"{strategy_id} returns the five contract columns",
              list(signals.columns) == list(SIGNAL_COLUMNS), str(list(signals.columns)))
        check(f"{strategy_id} is indexed like the input frame",
              signals.index.equals(frame.index))
        check(f"{strategy_id} booleans are bool dtype and the stop is float",
              all(signals[c].dtype == bool for c in BOOL_COLUMNS)
              and signals["stop_price"].dtype == float,
              str({c: str(signals[c].dtype) for c in SIGNAL_COLUMNS}))

        # JSON decodes numbers to float and openalgo.ta raises TypeError on a
        # float period, so the coercion is not cosmetic.
        floats = {k: (float(v) if isinstance(v, (int, float))
                      and not isinstance(v, bool) else v)
                  for k, v in strategy.DEFAULT_PARAMS.items()}
        coerced = BacktestAdapter(strategy, floats).run(frame)
        check(f"{strategy_id} accepts float periods from JSON",
              coerced.equals(signals), str(floats))

        first = next(iter(strategy.DEFAULT_PARAMS))
        try:
            BacktestAdapter(strategy, {first: 10.5}).run(frame)
            check(f"{strategy_id} refuses a fractional period", False, "no error")
        except StrategyError as exc:
            check(f"{strategy_id} refuses a fractional period", True, str(exc)[:52])
        try:
            BacktestAdapter(strategy, {"perod": 10}).run(frame)
            check(f"{strategy_id} refuses an unknown parameter", False, "no error")
        except StrategyError as exc:
            check(f"{strategy_id} refuses an unknown parameter",
                  "unknown parameter" in str(exc), str(exc)[:52])

        # Too few bars is not an error - a live session has no opinion yet.
        warmup = int(strategy.warmup_bars())
        short = BacktestAdapter(strategy).run(frame.iloc[: warmup - 1])
        check(f"{strategy_id} returns an empty signal frame below warm-up",
              not short[list(BOOL_COLUMNS)].to_numpy(bool).any()
              and bool(short["stop_price"].isna().all()),
              f"{warmup - 1} bars, warm-up {warmup}")
        try:
            BacktestAdapter(strategy).run(frame.drop(columns=["high"]))
            check(f"{strategy_id} refuses a frame without OHLC", False, "no error")
        except StrategyError as exc:
            check(f"{strategy_id} refuses a frame without OHLC",
                  "missing column" in str(exc), str(exc)[:52])

    check("stg3 direction defaults to long, the open decision in PLAN Part 3",
          STRATEGIES["stg3_sma10_ema30"].DEFAULT_PARAMS["direction"] == "long")
    both = BacktestAdapter(STRATEGIES["stg3_sma10_ema30"],
                           {"direction": "both"}).run(frame)
    check("stg3 direction=both enables short signals",
          bool(both["short_entry"].to_numpy(bool).any()),
          f"{int(both['short_entry'].sum())} short entries")
    try:
        BacktestAdapter(STRATEGIES["stg3_sma10_ema30"], {"direction": "flat"}).run(frame)
        check("stg3 refuses an unknown direction", False, "no error")
    except StrategyError as exc:
        check("stg3 refuses an unknown direction", "must be one of" in str(exc),
              str(exc)[:52])


def test_adapters() -> None:
    print("\n=== adapters ===")
    frame = synthetic_frame(400)
    strategy = STRATEGIES["stg1_ema_10_20"]
    vector = BacktestAdapter(strategy).run(frame)

    adapter = ExecutionAdapter(strategy)
    check("execution adapter floors the tail above the warm-up",
          adapter.tail_bars >= adapter.warmup_bars * 2,
          f"tail={adapter.tail_bars} warmup={adapter.warmup_bars} "
          f"default={DEFAULT_TAIL_BARS}")

    tight = ExecutionAdapter(strategy, tail_bars=5)
    check("a tail shorter than the warm-up is raised to the floor",
          tight.tail_bars > 5, f"asked 5, using {tight.tail_bars}")

    row = ExecutionAdapter(strategy).on_frame(frame)
    check("on_frame returns the last bar of the frame",
          row.name == frame.index[-1] and list(row.index) == list(SIGNAL_COLUMNS),
          str(row.name))
    check("on_frame agrees with the vectorised run",
          all(bool(row[c]) == bool(vector.iloc[-1][c]) for c in BOOL_COLUMNS)
          and values_match("stop_price", row["stop_price"],
                           vector.iloc[-1]["stop_price"]))

    # The executor may hand over a Series rather than a one-row frame.
    series_fed = ExecutionAdapter(strategy)
    for i in range(len(frame)):
        last = series_fed.on_bar(frame.iloc[i])
    check("a bar fed as a Series gives the same row as a one-row frame",
          all(bool(last[c]) == bool(vector.iloc[-1][c]) for c in BOOL_COLUMNS)
          and values_match("stop_price", last["stop_price"],
                           vector.iloc[-1]["stop_price"]))

    # A re-sent bar must overwrite, since a forming 5m candle can arrive twice.
    dedupe = ExecutionAdapter(strategy)
    for i in range(len(frame) - 1):
        dedupe.on_bar(frame.iloc[[i]])
    dedupe.on_bar(frame.iloc[[len(frame) - 2]])
    resent = dedupe.on_bar(frame.iloc[[len(frame) - 1]])
    check("a repeated bar overwrites instead of duplicating",
          resent.name == frame.index[-1]
          and values_match("stop_price", resent["stop_price"],
                           vector.iloc[-1]["stop_price"]))

    trimmed = ExecutionAdapter(strategy, tail_bars=200)
    for i in range(len(frame)):
        trimmed.on_bar(frame.iloc[[i]])
    buffer = trimmed.buffer
    check("the buffer never grows past the tail",
          buffer is not None and len(buffer) == trimmed.tail_bars,
          f"{0 if buffer is None else len(buffer)} rows held")

    try:
        ExecutionAdapter(strategy).on_frame(frame.iloc[:0])
        check("evaluating with no bars is refused", False, "no error")
    except StrategyError as exc:
        check("evaluating with no bars is refused", True, str(exc)[:52])


def test_parity_synthetic() -> None:
    print("\n=== parity: synthetic frame ===")
    frame = synthetic_frame(1200)
    note(f"{len(frame)} bars, {frame.index[0]} to {frame.index[-1]}")
    for strategy in STRATEGIES.values():
        run_lookahead_probe(strategy, frame, "synthetic")
        run_parity(strategy, frame, "synthetic")
        run_signal_sanity(strategy, frame, "synthetic")
    # The open decision on stg3 has to hold under parity too, or choosing "both"
    # at step 4 would silently break the gate.
    run_parity(STRATEGIES["stg3_sma10_ema30"], frame, "synthetic both",
               {"direction": "both"})


def test_parity_live() -> None:
    print("\n=== parity: live RELIANCE 5m ===")
    frame, detail = fetch_live_frame(days=60)
    if frame is None:
        skip("live RELIANCE 5m frame", detail)
        skip("parity on live data", "no live frame; synthetic gate above still ran")
        return
    sessions = len(pd.Series(frame.index).dt.date.unique())
    check("live RELIANCE 5m frame fetched and cleaned",
          len(frame) > 1000 and sessions > 20,
          f"{len(frame)} bars over {sessions} sessions from {detail}")
    check("cleaned frame has no NaN in OHLC and a unique ascending index",
          not frame[["open", "high", "low", "close"]].isna().any().any()
          and frame.index.is_monotonic_increasing
          and not frame.index.has_duplicates)
    note(f"{frame.index[0]} to {frame.index[-1]}")
    for strategy in STRATEGIES.values():
        run_lookahead_probe(strategy, frame, "live")
        run_parity(strategy, frame, "live")
        run_signal_sanity(strategy, frame, "live")


def main() -> int:
    print("AutoAgent parity gate - PLAN.md build step 2")
    print("=" * 70)
    started = time.perf_counter()
    test_contract()
    test_adapters()
    test_parity_synthetic()
    test_parity_live()

    n_pass = sum(1 for _, s in results if s == PASS)
    n_fail = sum(1 for _, s in results if s == FAIL)
    n_skip = sum(1 for _, s in results if s == SKIP)
    print("\n=== Summary ===")
    for name, status in results:
        if status == FAIL:
            print(f"  FAILED: {name}")
    print(f"  {n_pass} passed, {n_fail} failed, {n_skip} skipped "
          f"in {time.perf_counter() - started:.1f}s")
    if n_fail:
        print("  Step 2 gate is CLOSED. Nothing downstream is trustworthy.")
    else:
        print("  Step 2 gate is OPEN for all three strategies.")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
