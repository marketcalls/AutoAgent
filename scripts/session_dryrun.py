"""Drive the REAL executor through a full historical session, bar by bar.

Build step 11. The plan calls for a paper campaign in analyzer mode, and that is
still the goal - but it can only run during market hours, and the OpenAlgo
sandbox rejects MIS orders after 15:15 IST. This harness answers the same
question at any hour: does the executor, wired exactly as it ships, take a
session from reconciliation through entries, stops and squareoff to flat?

What is REAL here:
    the Executor, the SymbolMachine, the RiskBudget, the IntentLog,
    reconciliation, the clock, the strategies and their execution adapters,
    and real historical 5m bars for the real basket

What is stubbed:
    the broker. Orders are accepted and filled at the requested price, and the
    orderbook and positionbook are answered consistently so reconciliation has
    something truthful to reconcile against.

The stub deliberately does NOT model partial fills, rejects or slippage. This
harness proves the WIRING; the sandbox proves the fills. Conflating the two is
how a dry run becomes a false comfort.

A note on the strategy argument: the selector currently returns "none" for every
day, because no strategy has positive expectancy. Passing --strategy forces one
so the machinery can be exercised. That is a test affordance, not a bypass - the
live executor takes its strategy from the approved plan.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

import pandas as pd  # noqa: E402

from app.config import get_settings, setup_logging  # noqa: E402
from app.executor.intents import IntentLog, IntentState  # noqa: E402
from app.executor.runner import Executor  # noqa: E402
from app.openalgo.frames import get_frame_cache  # noqa: E402
from app.strategies import STRATEGIES  # noqa: E402


class StubBroker:
    """A broker that answers consistently. Fills at the requested price."""

    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []
        self.positions: dict[str, dict[str, Any]] = {}
        self._n = 0

    def ping(self) -> dict[str, Any]:
        return {"ok": True, "source": "ping", "data": {"broker": "stub", "message": "pong"}}

    def analyzer_mode(self) -> str:
        return "analyze"

    def call_enveloped(self, method: str, **kw: Any) -> dict[str, Any]:
        if method == "funds":
            return {"ok": True, "source": method,
                    "data": {"availablecash": "10000000"}}

        if method == "orderbook":
            return {"ok": True, "source": method, "data": {"orders": list(self.orders)}}

        if method == "positionbook":
            rows = [
                {"symbol": s, "quantity": str(p["qty"]), "product": "MIS",
                 "average_price": str(p["price"])}
                for s, p in self.positions.items() if p["qty"] != 0
            ]
            return {"ok": True, "source": method, "data": rows}

        if method == "placeorder":
            self._n += 1
            oid = f"DRY{self._n:06d}"
            symbol = kw.get("symbol", "")
            side = kw.get("action", "")
            qty = int(float(kw.get("quantity", 0) or 0))
            ptype = kw.get("price_type", "MARKET")
            price = float(kw.get("price") or kw.get("trigger_price") or 0.0)

            # A resting stop is not a fill. Recording it as complete would make
            # every position look closed the instant the stop was placed.
            status = "trigger pending" if ptype == "SL-M" else "complete"
            self.orders.append({
                "orderid": oid, "symbol": symbol, "action": side, "quantity": qty,
                "order_status": status, "filled_quantity": qty if status == "complete" else 0,
                "average_price": price, "strategy": kw.get("strategy", ""),
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            if status == "complete":
                pos = self.positions.setdefault(symbol, {"qty": 0, "price": price})
                pos["qty"] += qty if side == "BUY" else -qty
                pos["price"] = price
            return {"ok": True, "source": method, "data": {"orderid": oid}}

        if method == "cancelorder":
            oid = str(kw.get("order_id", ""))
            for row in self.orders:
                if row["orderid"] == oid:
                    row["order_status"] = "cancelled"
            return {"ok": True, "source": method, "data": {"orderid": oid}}

        return {"ok": False, "source": method, "error": f"stub has no {method}"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", default="stg2_supertrend_3_10", choices=list(STRATEGIES))
    parser.add_argument("--session", default="", help="YYYY-MM-DD, defaults to the latest stored")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging()
    settings = get_settings()

    # The dry run must not touch live state: its own database, its own kill
    # switch path. Two components have already leaked live state during testing
    # (steps 8 and 9), so this is deliberate rather than tidy.
    tmp = Path(tempfile.mkdtemp(prefix="autoagent-dryrun-"))
    settings.db_path = tmp / "dryrun.db"
    settings.kill_switch_file = tmp / "KILL"
    settings.trading_enabled = True

    print("AutoAgent session dry run")
    print("=" * 78)
    print(f"strategy   : {args.strategy}")
    print(f"basket     : {', '.join(settings.basket_symbols)}")
    print(f"allocation : {settings.allocation:,.0f}")
    print(f"scratch db : {settings.db_path}")

    cache = get_frame_cache()
    frames: dict[str, pd.DataFrame] = {}
    for symbol in settings.basket_symbols:
        result = cache.get_frame(symbol, "NSE", settings.timeframe, lookback_bars=3000)
        if result.get("ok"):
            frames[symbol] = result["frame"]
    if not frames:
        print("no history available")
        return 1

    sessions = sorted({ts.date() for ts in next(iter(frames.values())).index})
    target = date.fromisoformat(args.session) if args.session else sessions[-1]
    if target not in sessions:
        print(f"no data for {target}; latest stored is {sessions[-1]}")
        return 1
    print(f"session    : {target}")
    print()

    broker = StubBroker()
    store = IntentLog(settings.db_path)
    ex = Executor(
        settings, args.strategy,
        risk_fraction=settings.risk_fraction_base,
        trading_date=target, client=broker, store=store,
    )

    ok, why = ex.start()
    print(f"start      : {ok} - {why}")
    if not ok:
        return 1

    # Bars for the target session only, with the prior history kept as warm-up.
    # Restarting the series each morning would leave EMA(30) invalid until
    # roughly 11:45.
    bars = sorted({ts for ts in next(iter(frames.values())).index if ts.date() == target})
    print(f"bars       : {len(bars)}  {bars[0].strftime('%H:%M')} to {bars[-1].strftime('%H:%M')}")
    print()

    ticks = 0
    acted = 0
    for ts in bars:
        window = {
            sym: frame.loc[frame.index <= ts]
            for sym, frame in frames.items()
            if not frame.loc[frame.index <= ts].empty
        }
        report = ex.tick(ts.to_pydatetime(), window)
        ticks += 1
        for action in report.actions:
            acted += 1
            print(f"  {ts.strftime('%H:%M')}  {action.kind:6s} {action.symbol:10s} {action.detail}")
        if report.halted:
            print(f"  {ts.strftime('%H:%M')}  HALTED - {report.reason}")
            break

    print()
    print(f"ticks {ticks}, actions {acted}")

    status = ex.status()
    print(f"final state: {status['state']}")
    print(f"machines   : {status['machines']}")
    print(f"intents    : {status['intents']}")

    # The only outcome that matters: nothing may still be holding at the end.
    holding = [i for i in store.open_positions(target)]
    flat = not holding
    print()
    print(f"FLAT AT END: {flat}" + ("" if flat else f" - still holding {[i.symbol for i in holding]}"))

    broker_open = {s: p["qty"] for s, p in broker.positions.items() if p["qty"] != 0}
    print(f"BROKER FLAT: {not broker_open}" + ("" if not broker_open else f" - {broker_open}"))

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if flat else 1


if __name__ == "__main__":
    raise SystemExit(main())
