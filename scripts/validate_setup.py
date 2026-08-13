"""Step 0 of the build order: answer the OPEN items in PLAN.md Part 1.

Run this before writing any other code. Several design decisions in the plan are
conditional on what this script finds, and guessing wrong is expensive later:

  OPEN 1  Does OpenAlgo pass a client-supplied order id through to the broker?
          If not, UNKNOWN-state resolution in Part 6 cannot be automatic and must
          halt and escalate instead. This is the finding that shapes the state
          machine.
  OPEN 2  Is the order-update WebSocket usable for fill confirmation, or must the
          executor poll the orderbook?
  OPEN 3  How many sessions of 5m bars does one history call return? This sets the
          shape of the historical bar store in Part 7.
  OPEN 4  Does analyzer mode model fills realistically, or fill everything at the
          touch? This bounds what the paper campaign at step 11 proves.
  OPEN 5  Are freeze quantity and lot size available per symbol? They must never be
          hard-coded.

Read-only by default. Pass --with-orders to run the two checks that need to place
an order; those refuse to run unless the analyzer reports "analyze", so they can
never touch a live broker.

Exit code is 1 if any check fails, so this is usable as a build gate.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

# stdout must be reconfigured before anything prints. Indian market data carries
# the rupee sign and a cp1252 console raises on it.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append(("PASS" if ok else "FAIL", name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def skip(name: str, why: str) -> None:
    results.append(("SKIP", name, why))
    print(f"[SKIP] {name} - {why}")


def note(text: str) -> None:
    print(f"       {text}")


def section(title: str) -> None:
    print()
    print(f"--- {title} ---")


def mask(value: str) -> str:
    """Never print a credential. First and last four characters plus length."""
    if not value:
        return "(empty)"
    if len(value) <= 8:
        return f"(set, {len(value)} chars)"
    return f"{value[:4]}...{value[-4:]} ({len(value)} chars)"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = PROJECT_ROOT / ".env"
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--with-orders",
        action="store_true",
        help="also run checks that place an order (analyzer mode only)",
    )
    args = parser.parse_args()

    print("AutoAgent setup validation - PLAN.md Part 1 OPEN items")
    print("=" * 70)

    env = load_env()

    # ---------------------------------------------------------------- packages
    section("Environment")
    import importlib.metadata as md

    wanted = {
        "openalgo": "2.0.3",
        "agno": "2.8.7",
        "litellm": "1.96.2",
    }
    for pkg, pin in wanted.items():
        try:
            got = md.version(pkg)
        except Exception:
            check(f"{pkg} installed", False, "not installed")
            continue
        check(f"{pkg} == {pin}", got == pin, f"found {got}")

    for pkg in ("vectorbt", "pandas", "numpy", "fastapi", "uvicorn"):
        try:
            note(f"{pkg} {md.version(pkg)}")
        except Exception:
            check(f"{pkg} installed", False, "not installed")

    check("python >= 3.12", sys.version_info >= (3, 12), sys.version.split()[0])

    api_key = env.get("OPENALGO_API_KEY", "")
    host = env.get("OPENALGO_HOST", "http://127.0.0.1:5000")
    check("OPENALGO_API_KEY present", bool(api_key), mask(api_key))
    if not api_key:
        print("\nCannot continue without an API key.")
        return 1

    # ------------------------------------------------------------ connectivity
    section("Connectivity")
    from openalgo import api, ta  # noqa: E402

    client = api(api_key=api_key, host=host, version="v1", timeout=15.0)

    # The endpoint name carries no leading slash on purpose. BaseAPI concatenates
    # onto an already slash-terminated base, so "/ping" becomes /api/v1//ping,
    # the server 308s, httpx does not follow, and the SDK returns a fake error
    # dict containing HTML instead of failing loudly.
    ping = client._make_request("ping", {"apikey": api_key})
    ok = isinstance(ping, dict) and ping.get("status") == "success"
    broker = (ping.get("data") or {}).get("broker", "?") if isinstance(ping, dict) else "?"
    check("ping authenticates", ok, f"broker={broker}")

    analyzer = client.analyzerstatus()
    mode = "unknown"
    if isinstance(analyzer, dict) and analyzer.get("status") == "success":
        data = analyzer.get("data") or {}
        mode = data.get("mode") or ("analyze" if data.get("analyze_mode") else "live")
    check("analyzer status readable", mode in ("analyze", "live"), f"mode={mode}")

    funds = client.funds()
    cash = None
    if isinstance(funds, dict) and funds.get("status") == "success":
        try:
            cash = float((funds.get("data") or {}).get("availablecash", 0) or 0)
        except (TypeError, ValueError):
            cash = None
    check("funds readable", cash is not None, f"availablecash={cash}")

    allocation = float(env.get("ALLOCATION", 0) or 0)
    if cash is not None:
        check(
            "availablecash >= ALLOCATION",
            cash >= allocation,
            f"{cash} vs allocation {allocation}",
        )

    # ------------------------------------------------------- OPEN 1: order ids
    section("OPEN 1 - client order id passthrough")
    import inspect

    sig = inspect.signature(api.placeorder)
    params = set(sig.parameters)
    has_client_id = bool(
        params & {"client_order_id", "clientorderid", "tag", "order_tag", "correlation_id"}
    )
    check(
        "placeorder has NO client order id parameter",
        not has_client_id,
        f"named params: {sorted(p for p in params if p != 'self')}",
    )
    note("kwargs are forwarded but str()-cast; the server validates its own schema.")
    note("Reconciliation key is therefore (strategy, symbol, side, qty, time window).")

    src = inspect.getsource(api.placeorder)
    check(
        "strategy field is settable per order",
        "strategy" in sig.parameters,
        "usable as the attribution tag",
    )
    check(
        "kwargs are str()-cast before sending",
        "str(value)" in src,
        "so booleans reach the wire as 'True'",
    )

    # ------------------------------------------------ OPEN 5: symbol metadata
    section("OPEN 5 - freeze quantity and lot size")
    info = client.symbol(symbol="RELIANCE", exchange="NSE")
    sym = (info.get("data") or {}) if isinstance(info, dict) else {}
    check("symbol lookup works", bool(sym), f"keys={sorted(sym)[:8]}")
    for field in ("lotsize", "tick_size", "freeze_qty"):
        check(f"symbol carries {field}", field in sym, f"value={sym.get(field)}")

    # ------------------------------------------------- OPEN 3: history depth
    section("OPEN 3 - intraday history depth")
    import pandas as pd

    intervals = client.intervals()
    supported: list[str] = []
    if isinstance(intervals, dict) and intervals.get("status") == "success":
        for group in (intervals.get("data") or {}).values():
            if isinstance(group, list):
                supported.extend(group)
    check("5m interval supported", "5m" in supported, f"{len(supported)} intervals offered")

    today = datetime.now().date()
    for days in (10, 30, 90, 180):
        start = (today - timedelta(days=days)).isoformat()
        frame = client.history(
            symbol="RELIANCE", exchange="NSE", interval="5m",
            start_date=start, end_date=today.isoformat(),
        )
        # history returns a DataFrame on success and a dict on error. Always check.
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            sessions = len(pd.Series(frame.index).dt.date.unique())
            note(f"{days:3d} calendar days -> {len(frame):5d} bars, {sessions:3d} sessions")
            if days == 90:
                check("90 days of 5m bars in one call", len(frame) > 4000, f"{len(frame)} bars")
        else:
            msg = frame.get("message", frame) if isinstance(frame, dict) else frame
            note(f"{days:3d} calendar days -> error: {str(msg)[:80]}")

    # --------------------------------------------------- indicators the plan needs
    section("Indicators used by the three strategies")
    frame = client.history(
        symbol="RELIANCE", exchange="NSE", interval="5m",
        start_date=(today - timedelta(days=30)).isoformat(),
        end_date=today.isoformat(),
    )
    if isinstance(frame, pd.DataFrame) and not frame.empty:
        # One NaN poisons every subsequent value of a cumsum-based indicator, so
        # cleaning happens once at the boundary and never again downstream.
        for col in ("open", "high", "low", "close", "volume"):
            if col in frame.columns:
                frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frame = frame.dropna(subset=["open", "high", "low", "close"]).sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        check("clean frame has no NaN in OHLC", not frame[["open", "high", "low", "close"]].isna().any().any(), f"{len(frame)} bars")

        close, high, low = frame["close"], frame["high"], frame["low"]
        finite = lambda s: int(pd.Series(s).notna().sum())  # noqa: E731

        e10, e20 = ta.ema(close, 10), ta.ema(close, 20)
        check("ta.ema usable for stg1", finite(e10) > len(frame) - 30 and finite(e20) > len(frame) - 40)

        st = ta.supertrend(high, low, close, period=10, multiplier=3.0)
        check("ta.supertrend returns a 2-tuple for stg2", isinstance(st, tuple) and len(st) == 2, f"type={type(st).__name__}")

        s10, e30 = ta.sma(close, 10), ta.ema(close, 30)
        check("ta.sma + ta.ema usable for stg3", finite(s10) > len(frame) - 30 and finite(e30) > len(frame) - 50)

        atr = ta.atr(high, low, close, 14)
        check("ta.atr usable for stops", finite(atr) > len(frame) - 30)

        adx = ta.adx(high, low, close, 14)
        # adx returns (+DI, -DI, ADX). ADX is THIRD, not first.
        check("ta.adx returns a 3-tuple for the regime read", isinstance(adx, tuple) and len(adx) == 3, "ADX is element [2]")

        try:
            ta.ema(close, 10.0)
            check("float period rejected by ta", False, "10.0 was accepted, expected TypeError")
        except TypeError:
            check("float period rejected by ta", True, "int() coercion is mandatory")
    else:
        skip("indicator checks", "no history frame available")

    # ------------------------------------------------------------- vectorbt
    section("vectorbt")
    try:
        import vectorbt as vbt
        import numpy as np

        n = 200
        px = pd.Series(np.linspace(100, 120, n), index=pd.date_range("2026-01-01", periods=n, freq="5min"))
        entries = pd.Series(False, index=px.index)
        exits = pd.Series(False, index=px.index)
        entries.iloc[10] = True
        exits.iloc[50] = True
        pf = vbt.Portfolio.from_signals(px, entries, exits, init_cash=100000, fees=0.0003)
        check("vectorbt from_signals runs", pf.total_return() != 0, f"total_return={pf.total_return():.4f}")
        check("vectorbt exposes trade records", hasattr(pf, "trades"), f"{len(pf.trades.records_readable)} trades")
    except Exception as exc:  # noqa: BLE001
        check("vectorbt usable", False, f"{type(exc).__name__}: {exc}")

    # --------------------------------------------- OPEN 2: order-update stream
    section("OPEN 2 - order update websocket")
    ws_url = env.get("OPENALGO_WS_URL", "ws://127.0.0.1:8765")
    try:
        import socket
        from urllib.parse import urlparse

        parsed = urlparse(ws_url)
        sock = socket.create_connection((parsed.hostname, parsed.port or 8765), timeout=4.0)
        sock.close()
        check("websocket port reachable", True, ws_url)
        note("Reachability only. Full subscribe_orders behaviour is verified at step 6.")
    except Exception as exc:  # noqa: BLE001
        check("websocket port reachable", False, f"{type(exc).__name__}: {exc}")
        note("Executor will poll the orderbook for fills instead.")

    # ----------------------------------------------- OPEN 4: sandbox fill model
    section("OPEN 4 - sandbox fill realism")
    if not args.with_orders:
        skip("sandbox fill check", "needs --with-orders")
    elif mode != "analyze":
        skip("sandbox fill check", f"analyzer reports '{mode}', refusing to place an order")
    else:
        resp = client.placeorder(
            strategy="AutoAgentValidate", symbol="RELIANCE", action="BUY",
            exchange="NSE", price_type="MARKET", product="MIS", quantity=1,
        )
        placed = isinstance(resp, dict) and resp.get("status") == "success"
        oid = resp.get("orderid") if isinstance(resp, dict) else None
        check("sandbox accepts an order", placed, f"orderid={oid} mode={resp.get('mode')}")
        if placed and oid:
            status = client.orderstatus(order_id=oid, strategy="AutoAgentValidate")
            sdata = (status.get("data") or {}) if isinstance(status, dict) else {}
            note(f"order_status={sdata.get('order_status')} avg_price={sdata.get('average_price')}")
            check(
                "sandbox reports a fill price",
                bool(sdata.get("average_price")),
                "paper results assume touch fills unless this differs from LTP",
            )
            # Leave nothing open. This is analyzer mode, but the habit matters.
            client.placeorder(
                strategy="AutoAgentValidate", symbol="RELIANCE", action="SELL",
                exchange="NSE", price_type="MARKET", product="MIS", quantity=1,
            )
            note("squared off the validation position")

        # OPEN 1, live half: does an unknown kwarg survive to the orderbook?
        book = client.orderbook()
        orders = ((book.get("data") or {}).get("orders") or []) if isinstance(book, dict) else []
        if orders:
            note(f"orderbook row keys: {sorted(orders[0])}")
            check(
                "orderbook exposes fields needed to match an order",
                {"symbol", "action", "quantity"} <= set(orders[0]),
                "reconciliation key is viable",
            )

    # ------------------------------------------------------------------ summary
    section("Summary")
    passed = sum(1 for r in results if r[0] == "PASS")
    failed = sum(1 for r in results if r[0] == "FAIL")
    skipped = sum(1 for r in results if r[0] == "SKIP")
    print(f"{passed} passed, {failed} failed, {skipped} skipped")
    if failed:
        print("\nFailures:")
        for status, name, detail in results:
            if status == "FAIL":
                print(f"  - {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
