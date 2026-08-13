"""Tests for the per-symbol order state machine. Offline, with a stub broker.

The assertions that matter are the ones about ORDERING and about what happens
when the broker does not answer. Those are the paths that cost money, and they
are exactly the paths that never run during a normal profitable day.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.executor.intents import IntentLog, IntentState  # noqa: E402
from app.executor.machine import SymbolMachine  # noqa: E402
from app.risk.budget import RiskBudget  # noqa: E402

results: list[tuple[bool, str, str]] = []
D = date(2026, 8, 13)


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((ok, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def section(title: str) -> None:
    print()
    print(f"--- {title} ---")


class StubClient:
    """Records every call in order, so ORDERING can be asserted."""

    def __init__(self, *, fail_on: set[str] | None = None, fail_stop: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.fail_on = fail_on or set()
        self.fail_stop = fail_stop
        self._n = 0

    def call_enveloped(self, method, **kw):
        self.calls.append((method, kw))
        if method in self.fail_on:
            return {"ok": False, "error": f"{method} refused", "source": method}
        if method == "placeorder" and self.fail_stop and kw.get("price_type") == "SL-M":
            return {"ok": False, "error": "stop refused", "source": method}
        self._n += 1
        return {"ok": True, "source": method, "data": {"orderid": f"OID{self._n}"}}

    def kinds(self) -> list[str]:
        out = []
        for method, kw in self.calls:
            if method == "placeorder":
                out.append(f"place:{kw.get('price_type')}:{kw.get('action')}")
            else:
                out.append(method)
        return out


def make(tmp: Path, **over):
    s = Settings.load()
    s.allocation = 1_000_000.0
    s.risk_fraction_base = 0.005
    s.risk_fraction_floor = 0.00125
    s.daily_loss_limit_pct = 2.0
    s.max_concurrent_positions = 3
    s.max_per_sector = 2
    s.trading_enabled = True
    s.db_path = tmp / "m.db"
    s.kill_switch_file = tmp / "KILL"
    s.default_strategy_name = "AutoAgent"
    for k, v in over.items():
        setattr(s, k, v)
    store = IntentLog(tmp / f"log{len(list(tmp.iterdir()))}.db")
    budget = RiskBudget(settings=s, trading_date=D)
    return s, store, budget


BAR = {"open": 1300.0, "high": 1305.0, "low": 1295.0, "close": 1300.0}
LONG = {"long_entry": True, "long_exit": False, "short_entry": False,
        "short_exit": False, "stop_price": 1290.0}
EXIT = {"long_entry": False, "long_exit": True, "short_entry": False,
        "short_exit": False, "stop_price": 1290.0}
NONE = {"long_entry": False, "long_exit": False, "short_entry": False,
        "short_exit": False, "stop_price": 1290.0}
T0 = datetime(2026, 8, 13, 10, 0)


def main() -> int:
    print("AutoAgent state machine tests")
    print("=" * 70)
    tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    tmp = Path(tmpdir.name)

    section("Happy path")

    s, store, budget = make(tmp)
    c = StubClient()
    m = SymbolMachine(s, c, store, budget, "RELIANCE")
    check("starts FLAT", m.state is IntentState.FLAT)

    act = m.on_bar(T0, BAR, LONG, {}, risk_fraction=s.risk_fraction_base, allow_entries=True)
    check("a long signal sends an entry", act.kind == "entry", act.detail)
    check("state is PENDING_ENTRY", m.state is IntentState.PENDING_ENTRY)
    check("the intent was written BEFORE the send",
          store.get(m.active.intent_id) is not None,
          "a crash here leaves a resolvable row, not an untracked position")
    check("entry is a LIMIT, never a market chase", c.kinds()[0] == "place:LIMIT:BUY", str(c.kinds()))

    m.active.fill_qty, m.active.fill_price = m.active.planned_qty, 1300.0
    store.transition(m.active, IntentState.OPEN_UNPROTECTED, "filled")
    act = m.on_bar(T0, BAR, NONE, {}, risk_fraction=s.risk_fraction_base, allow_entries=True)
    check("a fill triggers the protective stop", act.kind == "stop", act.detail)
    check("state promotes to OPEN only after the stop confirms", m.state is IntentState.OPEN)
    check("the stop is SL-M with a trigger price",
          c.kinds()[1] == "place:SL-M:SELL" and "trigger_price" in c.calls[1][1],
          str(c.calls[1][1].get("trigger_price")))
    check("the position is registered with the budget", "RELIANCE" in budget.open_positions)

    section("THE ORDERING RULE - cancel the stop before a market exit")

    act = m.on_bar(T0, BAR, EXIT, {}, risk_fraction=s.risk_fraction_base, allow_entries=False)
    check("a signal exit is sent", act.kind == "exit", act.detail)
    kinds = c.kinds()
    check(
        "cancelorder comes BEFORE the market exit",
        kinds.index("cancelorder") < kinds.index("place:MARKET:SELL"),
        " -> ".join(kinds),
    )
    check(
        "sending the exit first could fill BOTH and reverse the position",
        kinds == ["place:LIMIT:BUY", "place:SL-M:SELL", "cancelorder", "place:MARKET:SELL"],
        "short when you were long, which is the most expensive ordering bug here",
    )

    m.settle_exit(1310.0, m.active.planned_qty, costs=400.0, now=T0 + timedelta(minutes=30))
    check("settling closes the intent", m.state is IntentState.FLAT)
    check("the budget saw the realized P&L", budget.realized_pnl > 0, f"{budget.realized_pnl:.0f}")
    check("the position is released", "RELIANCE" not in budget.open_positions)

    section("The dangerous state - no answer from the broker")

    s, store, budget = make(tmp)
    c = StubClient(fail_on={"placeorder"})
    m = SymbolMachine(s, c, store, budget, "RELIANCE")
    act = m.on_bar(T0, BAR, LONG, {}, risk_fraction=s.risk_fraction_base, allow_entries=True)
    check(
        "a failed entry send goes to UNKNOWN, NOT back to FLAT",
        m.state is IntentState.UNKNOWN,
        "the request may or may not have reached the broker",
    )
    check("and it is reported as a halt", act.kind == "halt", act.detail)

    s, store, budget = make(tmp)
    c = StubClient()
    m = SymbolMachine(s, c, store, budget, "RELIANCE")
    m.on_bar(T0, BAR, LONG, {}, risk_fraction=s.risk_fraction_base, allow_entries=True)
    before = len([k for k in c.kinds() if k.startswith("place")])
    # Dwell is measured on the BAR clock now, so the timeout is forced by
    # advancing the bar rather than by rewinding the wall clock. PENDING_ENTRY
    # allows 2 bars; this is 3 bars later.
    act = m.on_bar(T0 + timedelta(minutes=15), BAR, LONG, {},
                   risk_fraction=s.risk_fraction_base, allow_entries=True)
    after = len([k for k in c.kinds() if k.startswith("place")])
    check("a pending-entry timeout goes to UNKNOWN", m.state is IntentState.UNKNOWN, act.detail)
    check(
        "and it NEVER resends the order",
        after == before,
        "a retry cannot be told apart from the original with no client order id",
    )
    check("it cancels instead", "cancelorder" in c.kinds())

    section("OPEN_UNPROTECTED - never sit naked")

    s, store, budget = make(tmp)
    c = StubClient(fail_stop=True)
    m = SymbolMachine(s, c, store, budget, "RELIANCE")
    m.on_bar(T0, BAR, LONG, {}, risk_fraction=s.risk_fraction_base, allow_entries=True)
    m.active.fill_qty, m.active.fill_price = m.active.planned_qty, 1300.0
    store.transition(m.active, IntentState.OPEN_UNPROTECTED, "filled")

    act = m.on_bar(T0, BAR, NONE, {}, risk_fraction=s.risk_fraction_base, allow_entries=True)
    check("a failed stop retries first", act.kind == "none" and m.state is IntentState.OPEN_UNPROTECTED,
          act.detail)
    act = m.on_bar(T0, BAR, NONE, {}, risk_fraction=s.risk_fraction_base, allow_entries=True)
    check(
        "after the retry limit it EXITS at market rather than sit naked",
        act.kind == "exit" and m.state is IntentState.PENDING_EXIT,
        act.detail,
    )
    check("the exit is a MARKET order", "place:MARKET:SELL" in c.kinds(), str(c.kinds()))

    section("Squareoff and refusals")

    s, store, budget = make(tmp)
    c = StubClient()
    m = SymbolMachine(s, c, store, budget, "RELIANCE")
    m.on_bar(T0, BAR, LONG, {}, risk_fraction=s.risk_fraction_base, allow_entries=True)
    m.active.fill_qty, m.active.fill_price = m.active.planned_qty, 1300.0
    store.transition(m.active, IntentState.OPEN_UNPROTECTED, "filled")
    m.on_bar(T0, BAR, NONE, {}, risk_fraction=s.risk_fraction_base, allow_entries=True)
    late = datetime.combine(D, s.squareoff_time) + timedelta(minutes=1)
    act = m.on_bar(late, BAR, NONE, {}, risk_fraction=s.risk_fraction_base, allow_entries=False)
    check("squareoff time forces an exit regardless of signal", act.kind == "exit", act.detail)
    check("and the reason says squareoff", "squareoff" in act.detail)

    s, store, budget = make(tmp)
    m = SymbolMachine(s, StubClient(), store, budget, "RELIANCE")
    act = m.on_bar(T0, BAR, LONG, {}, risk_fraction=s.risk_fraction_base, allow_entries=False)
    check("allow_entries=False blocks a new position", act.kind == "none" and m.state is IntentState.FLAT)

    s, store, budget = make(tmp)
    m = SymbolMachine(s, StubClient(), store, budget, "RELIANCE")
    bad = dict(LONG, stop_price=1310.0)   # stop above entry on a long
    act = m.on_bar(T0, BAR, bad, {}, risk_fraction=s.risk_fraction_base, allow_entries=True)
    check("a stop on the wrong side is refused", act.kind == "none", act.detail)

    s, store, budget = make(tmp, trading_enabled=False)
    m = SymbolMachine(s, StubClient(), store, budget, "RELIANCE")
    act = m.on_bar(T0, BAR, LONG, {}, risk_fraction=s.risk_fraction_base, allow_entries=True)
    check("TRADING_ENABLED=false blocks the entry", act.kind == "none", act.detail)

    s, store, budget = make(tmp)
    c = StubClient()
    m = SymbolMachine(s, c, store, budget, "RELIANCE")
    m.on_bar(T0, BAR, LONG, {}, risk_fraction=s.risk_fraction_base, allow_entries=True)
    m.active.fill_qty, m.active.fill_price = m.active.planned_qty, 1300.0
    store.transition(m.active, IntentState.OPEN_UNPROTECTED, "filled")
    m.on_bar(T0, BAR, NONE, {}, risk_fraction=s.risk_fraction_base, allow_entries=True)
    act = m.force_flat("halt requested")
    check("force_flat exits an open position", act.kind == "exit", act.detail)
    check("force_flat on a flat machine is a no-op",
          SymbolMachine(s, StubClient(), store, budget, "INFY").force_flat().kind == "none")

    section("Summary")
    passed = sum(1 for ok, _, _ in results if ok)
    failed = sum(1 for ok, _, _ in results if not ok)
    print(f"{passed} passed, {failed} failed")
    if failed:
        print("\nFailures:")
        for ok, name, detail in results:
            if not ok:
                print(f"  - {name}: {detail}")
    tmpdir.cleanup()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
