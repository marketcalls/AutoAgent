"""Tests for reconciliation. Fully offline, with a stub broker.

These are the assertions that decide whether an unattended agent recovers from a
dropped connection or doubles a position. The ones that matter most are the
refusals: ambiguity must HALT, not resolve to a guess.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.executor.intents import Intent, IntentLog, IntentState, make_intent_id  # noqa: E402
from app.executor.reconcile import candidates_for, reconcile  # noqa: E402

results: list[tuple[bool, str, str]] = []
TAG = "AutoAgent"
D = date(2026, 8, 13)
TS = datetime(2026, 8, 13, 9, 35)


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((ok, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def section(title: str) -> None:
    print()
    print(f"--- {title} ---")


class StubClient:
    """Only the two endpoints reconcile() reads."""

    def __init__(self, orders=None, positions=None, fail=False):
        self.orders = orders or []
        self.positions = positions or []
        self.fail = fail

    def call_enveloped(self, method, **kwargs):
        if self.fail:
            return {"ok": False, "error": "broker unreachable", "source": method}
        if method == "orderbook":
            return {"ok": True, "source": method, "data": {"orders": self.orders}}
        if method == "positionbook":
            return {"ok": True, "source": method, "data": self.positions}
        return {"ok": False, "error": "unexpected", "source": method}


def order(symbol="RELIANCE", action="BUY", qty=100, status="complete",
          filled=100, oid="250813000001", offset_min=0, strategy=TAG, price=1300.0):
    ts = TS + timedelta(minutes=offset_min)
    return {
        "orderid": oid, "symbol": symbol, "action": action, "quantity": qty,
        "order_status": status, "filled_quantity": filled, "average_price": price,
        "strategy": strategy, "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
    }


def make_log(tmp: Path) -> IntentLog:
    return IntentLog(tmp / f"t{len(list(tmp.iterdir()))}.db")


def pending_intent(store: IntentLog, symbol="RELIANCE", qty=100, side="BUY") -> Intent:
    i = Intent(
        intent_id=make_intent_id("stg1", symbol, TS, side), trading_date=D,
        strategy_id="stg1", strategy_version="1", mandate_version="1",
        symbol=symbol, exchange="NSE", side=side, signal_bar_ts=TS,
        planned_qty=qty, planned_entry=1300.0, planned_stop=1290.0, risk_amount=1000.0,
    )
    store.put(i)
    store.transition(i, IntentState.PENDING_ENTRY, "order sent")
    return i


def main() -> int:
    print("AutoAgent reconciliation tests")
    print("=" * 70)
    tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    tmp = Path(tmpdir.name)

    section("Matching on shape, since there is no client order id")

    i = pending_intent(make_log(tmp))
    check("an exact match is found", len(candidates_for(i, [order()], strategy_tag=TAG)) == 1)
    check("a different symbol does not match",
          candidates_for(i, [order(symbol="INFY")], strategy_tag=TAG) == [])
    check("a different side does not match",
          candidates_for(i, [order(action="SELL")], strategy_tag=TAG) == [])
    check(
        "a different quantity does not match",
        candidates_for(i, [order(qty=50)], strategy_tag=TAG) == [],
        "order quantity is exact; a partial fill changes filled_quantity, not quantity",
    )
    check("another strategy's order does not match",
          candidates_for(i, [order(strategy="Manual")], strategy_tag=TAG) == [])
    check("an order outside the time window does not match",
          candidates_for(i, [order(offset_min=45)], strategy_tag=TAG) == [])
    check("an order inside the window does match",
          len(candidates_for(i, [order(offset_min=3)], strategy_tag=TAG)) == 1)

    section("Resolving the dangerous state")

    store = make_log(tmp); i = pending_intent(store)
    r = reconcile(StubClient(orders=[order()], positions=[
        {"symbol": "RELIANCE", "quantity": "100"}]), store, D, strategy_tag=TAG)
    got = store.get(i.intent_id)
    check(
        "a filled order becomes OPEN_UNPROTECTED, not OPEN",
        got.state is IntentState.OPEN_UNPROTECTED,
        "reconcile cannot know whether the stop is live; only the machine can",
    )
    check("the fill is recorded", got.fill_qty == 100 and got.fill_price == 1300.0)
    check("reconciliation succeeds", r.ok, r.reason)

    store = make_log(tmp); i = pending_intent(store)
    reconcile(StubClient(orders=[], positions=[]), store, D, strategy_tag=TAG)
    check(
        "PENDING_ENTRY with no broker record becomes ABANDONED",
        store.get(i.intent_id).state is IntentState.ABANDONED,
        "the order never landed - safe to conclude ONLY because the book was readable",
    )

    store = make_log(tmp); i = pending_intent(store)
    reconcile(StubClient(orders=[order(status="rejected", filled=0)], positions=[]),
              store, D, strategy_tag=TAG)
    check("a rejected order becomes REJECTED", store.get(i.intent_id).state is IntentState.REJECTED)

    store = make_log(tmp); i = pending_intent(store)
    reconcile(StubClient(orders=[order(status="open", filled=40)],
                         positions=[{"symbol": "RELIANCE", "quantity": "40"}]),
              store, D, strategy_tag=TAG)
    check("a part-filled working order becomes PARTIAL",
          store.get(i.intent_id).state is IntentState.PARTIAL,
          f"filled 40 of 100")

    section("The refusals - halt rather than guess")

    store = make_log(tmp); i = pending_intent(store)
    two = [order(oid="A1"), order(oid="A2")]
    r = reconcile(StubClient(orders=two, positions=[{"symbol": "RELIANCE", "quantity": "200"}]),
                  store, D, strategy_tag=TAG)
    check(
        "two identical-looking orders HALT rather than resolve",
        not r.ok and i.intent_id in r.ambiguous,
        r.reason[:100],
    )
    check(
        "the ambiguous intent is left untouched",
        store.get(i.intent_id).state is IntentState.PENDING_ENTRY,
        "picking one could leave a real position unmanaged",
    )

    store = make_log(tmp); i = pending_intent(store)
    r = reconcile(StubClient(fail=True), store, D, strategy_tag=TAG)
    check(
        "an unreadable orderbook HALTS",
        not r.ok and "reconciled" in r.reason,
        "without the book, ABANDONED cannot be distinguished from filled",
    )
    check("and does NOT abandon the intent",
          store.get(i.intent_id).state is IntentState.PENDING_ENTRY,
          "this is the bug that would silently drop a live position")

    store = make_log(tmp)
    i = pending_intent(store)
    reconcile(StubClient(orders=[order()], positions=[{"symbol": "RELIANCE", "quantity": "100"}]),
              store, D, strategy_tag=TAG)
    r = reconcile(StubClient(orders=[order()], positions=[]), store, D, strategy_tag=TAG)
    check(
        "believing in a position the broker does not show HALTS",
        not r.ok and i.intent_id in r.missing_positions,
        r.reason[:90],
    )

    section("Shared account - orphan positions")

    store = make_log(tmp)
    r = reconcile(
        StubClient(orders=[], positions=[{"symbol": "TCS", "quantity": "50"}]),
        store, D, strategy_tag=TAG,
    )
    check(
        "a position the agent never opened is reported",
        "TCS" in r.orphan_positions,
        "the positionbook nets everything on a shared account",
    )
    check(
        "but an orphan does NOT halt the session",
        r.ok,
        "it is almost certainly a manual trade; the agent's job is to leave it alone",
    )

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
