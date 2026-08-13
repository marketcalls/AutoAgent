"""Tests for the daily loss budget, the breakers and position sizing.

Runnable as a plain script with a PASS/FAIL tally and a non-zero exit, matching
TradingAgent's test style. Not pytest, so it works as a build gate anywhere.

Fully offline. No broker, no network.

The assertions worth reading are the budget ones. Everything else is arithmetic;
those encode the difference between a budget and a tripwire, which is the whole
point of PLAN.md Part 5.
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
from app.risk.budget import Position, RiskBudget, RunState  # noqa: E402
from app.risk.sizing import (  # noqa: E402
    quantity_for,
    resolve_risk_fraction,
    worst_case_loss,
)

results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((ok, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def section(title: str) -> None:
    print()
    print(f"--- {title} ---")


def make_settings(tmp: Path, **over) -> Settings:
    s = Settings.load()
    s.allocation = 1_000_000.0
    s.risk_fraction_base = 0.005          # 5,000 per trade
    s.risk_fraction_floor = 0.00125       # 1,250 floor
    s.daily_loss_limit_pct = 2.0          # 20,000
    s.max_concurrent_positions = 3
    s.max_trades_per_day = 6
    s.max_per_sector = 2
    s.consecutive_loss_pause = 2
    s.consecutive_loss_halt = 3
    s.pause_minutes = 30
    s.scratch_band_r = 0.1                # +/- 500
    s.trading_enabled = True
    s.db_path = tmp / "test.db"
    s.kill_switch_file = tmp / "KILL"
    for k, v in over.items():
        setattr(s, k, v)
    return s


def fresh(tmp: Path, **over) -> RiskBudget:
    return RiskBudget(settings=make_settings(tmp, **over), trading_date=date(2026, 8, 13))


def main() -> int:
    print("AutoAgent risk engine tests")
    print("=" * 70)
    tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    tmp = Path(tmpdir.name)
    s = make_settings(tmp)

    # ------------------------------------------------------------------ sizing
    section("Sizing")

    r = quantity_for(s, risk_fraction=s.risk_fraction_base, entry_price=1000.0, stop_price=990.0)
    check("quantity = risk / stop distance", r.quantity == 500, f"qty={r.quantity} (5000/10)")

    wide = quantity_for(s, risk_fraction=s.risk_fraction_base, entry_price=1000.0, stop_price=950.0)
    check("a wider stop buys fewer shares", wide.quantity == 100, f"qty={wide.quantity} (5000/50)")
    check(
        "both risk the same rupees",
        abs(worst_case_loss(r.quantity, 1000, 990) - worst_case_loss(wide.quantity, 1000, 950)) < 1,
        "5000 either way - volatility sizes the position",
    )

    odd = quantity_for(s, risk_fraction=s.risk_fraction_base, entry_price=1000.0, stop_price=993.0)
    check(
        "quantity rounds DOWN",
        odd.quantity == 714,
        f"qty={odd.quantity}, 5000/7 = 714.28; rounding up would breach the mandate",
    )

    # The stop distance must exceed the ENTIRE risk amount for one share to be too
    # many. At entry 1000 / stop 1 the distance is 999 and five shares fit inside
    # 5000, which an earlier version of this test got wrong.
    too_wide = quantity_for(s, risk_fraction=s.risk_fraction_base, entry_price=6000.0, stop_price=1.0)
    check("refuses when one share exceeds the budget", too_wide.quantity == 0, too_wide.reason)

    check("zero entry refused", quantity_for(s, risk_fraction=s.risk_fraction_base, entry_price=0, stop_price=1).quantity == 0)
    check("stop equal to entry refused", quantity_for(s, risk_fraction=s.risk_fraction_base, entry_price=100, stop_price=100).quantity == 0)

    capped = quantity_for(s, risk_fraction=s.risk_fraction_base, entry_price=1000.0, stop_price=990.0, budget_cap=2000.0)
    check(
        "sizes DOWN to fit the remaining budget",
        capped.quantity == 200,
        f"qty={capped.quantity} from a 2000 cap, not refused outright",
    )
    check(
        "exhausted budget refuses",
        quantity_for(s, risk_fraction=s.risk_fraction_base, entry_price=1000, stop_price=990, budget_cap=0).quantity == 0,
    )

    # freeze_qty of 1 on a cash equity means "not applicable", verified at step 0
    fz = quantity_for(s, risk_fraction=s.risk_fraction_base, entry_price=1000.0, stop_price=990.0, freeze_qty=1)
    check("freeze_qty of 1 is ignored", fz.quantity == 500, f"qty={fz.quantity}")
    fz2 = quantity_for(s, risk_fraction=s.risk_fraction_base, entry_price=1000.0, stop_price=990.0, freeze_qty=100)
    check("a real freeze_qty caps quantity", fz2.quantity == 100, f"qty={fz2.quantity}")

    section("Risk fraction clamping")
    check("None resolves to the FLOOR not the base", resolve_risk_fraction(s, None) == s.risk_fraction_floor)
    check("above the ceiling clamps to base", resolve_risk_fraction(s, 0.05) == s.risk_fraction_base)
    check("below the floor clamps to floor", resolve_risk_fraction(s, 0.0001) == s.risk_fraction_floor)
    check("negative resolves to floor", resolve_risk_fraction(s, -1) == s.risk_fraction_floor)
    check("garbage resolves to floor", resolve_risk_fraction(s, "abc") == s.risk_fraction_floor)  # type: ignore[arg-type]
    check("a valid mid value is honoured", resolve_risk_fraction(s, 0.003) == 0.003)

    # ------------------------------------------------------- budget vs tripwire
    section("Budget, not tripwire")

    b = fresh(tmp)
    marks = {"RELIANCE": 1000.0}
    check("starts with the full budget", b.remaining_budget({}) == 20_000.0)

    # The case the whole design exists for. Down 18,000 against a 20,000 limit,
    # a trade risking 5,000 would reach 23,000 in the worst case. A tripwire
    # permits it and fires afterwards; the budget refuses it now.
    b.realized_pnl = -18_000.0
    d = b.can_open("RELIANCE", worst_case_loss=5_000.0, marks=marks)
    check(
        "refuses a trade whose WORST CASE breaches the limit",
        not d.allowed and d.code == "budget",
        d.reason,
    )
    small = b.can_open("RELIANCE", worst_case_loss=1_500.0, marks=marks)
    check("allows one that fits inside the remainder", small.allowed, "2000 remaining, risking 1500")

    # Unrealized losses must count, or the agent sits underwater with the limit
    # never firing.
    b2 = fresh(tmp)
    b2.open_position(Position("INFY", 100, 1500.0, 1400.0, "BUY"))
    check(
        "unrealized loss consumes budget",
        b2.remaining_budget({"INFY": 1400.0}) == 10_000.0,
        "down 10,000 on an open position, 10,000 of 20,000 left",
    )
    check(
        "mtm is realized plus unrealized",
        b2.mtm_pnl({"INFY": 1400.0}) == -10_000.0,
    )

    # Committed risk on open positions counts against a new entry, because every
    # stop can fill on the same adverse move.
    b3 = fresh(tmp)
    b3.open_position(Position("INFY", 100, 1500.0, 1400.0, "BUY"))  # 10,000 still at risk
    d3 = b3.can_open("RELIANCE", worst_case_loss=15_000.0, marks={"INFY": 1500.0})
    check(
        "already-committed risk counts against a new entry",
        not d3.allowed and d3.code == "budget",
        d3.reason,
    )

    # ------------------------------------------------------------------- caps
    section("Position, sector and trade caps")

    b4 = fresh(tmp)
    for sym, px in (("RELIANCE", 1000.0), ("INFY", 1500.0), ("TATAMOTORS", 700.0)):
        b4.open_position(Position(sym, 1, px, px * 0.99, "BUY"))
    d4 = b4.can_open("SBIN", 100.0, {})
    check("position cap enforced", not d4.allowed and d4.code == "position_cap", d4.reason)

    # The sector cap only bites among symbols the basket actually labels. A symbol
    # outside the basket has no sector and lands in its own bucket, so the test
    # basket carries three banks.
    three_banks = [("HDFCBANK", "BANK"), ("SBIN", "BANK"), ("ICICIBANK", "BANK"),
                   ("RELIANCE", "ENERGY"), ("INFY", "IT")]
    b5 = fresh(tmp, basket=three_banks)
    b5.open_position(Position("HDFCBANK", 1, 1600.0, 1590.0, "BUY"))
    b5.open_position(Position("SBIN", 1, 800.0, 795.0, "BUY"))
    d5 = b5.can_open("ICICIBANK", 100.0, {})
    check(
        "sector cap enforced - two banks is one bet",
        not d5.allowed and d5.code == "sector_cap",
        d5.reason,
    )

    b6 = fresh(tmp)
    b6.trade_count = 6
    d6 = b6.can_open("RELIANCE", 100.0, {})
    check("daily trade cap enforced", not d6.allowed and d6.code == "trade_cap", d6.reason)

    b7 = fresh(tmp)
    b7.open_position(Position("RELIANCE", 1, 1000.0, 990.0, "BUY"))
    d7 = b7.can_open("RELIANCE", 100.0, {})
    check("refuses a second position in the same symbol", not d7.allowed and d7.code == "already_open")

    # --------------------------------------------------------------- breakers
    section("Consecutive-loss breakers")

    b8 = fresh(tmp)
    b8.open_position(Position("RELIANCE", 1, 1000.0, 990.0, "BUY"))
    b8.close_position("RELIANCE", -1000.0)
    check("one loss does not pause", b8.state is RunState.RUNNING, f"streak={b8.consecutive_losses}")

    b8.open_position(Position("INFY", 1, 1500.0, 1490.0, "BUY"))
    b8.close_position("INFY", -1000.0)
    check("two consecutive losses pause", b8.state is RunState.PAUSED, f"until {b8.paused_until}")
    dp = b8.can_open("SBIN", 100.0, {})
    check("a paused session refuses entries", not dp.allowed and dp.code == "paused", dp.reason)

    later = datetime.now() + timedelta(minutes=31)
    b8.can_open("SBIN", 100.0, {}, now=later)
    check("the pause expires on its own", b8.state is RunState.RUNNING, "resumed after 30 minutes")

    b9 = fresh(tmp)
    for sym in ("RELIANCE", "INFY", "SBIN"):
        b9.open_position(Position(sym, 1, 1000.0, 990.0, "BUY"))
        b9.close_position(sym, -1000.0)
    check("three consecutive losses halt", b9.state is RunState.HALTED, b9.halt_reason)

    # A win must clear the streak, or an alternating sequence eventually halts.
    b10 = fresh(tmp)
    b10.open_position(Position("RELIANCE", 1, 1000.0, 990.0, "BUY"))
    b10.close_position("RELIANCE", -1000.0)
    b10.open_position(Position("INFY", 1, 1000.0, 990.0, "BUY"))
    b10.close_position("INFY", +2000.0)
    check("a win resets the streak", b10.consecutive_losses == 0)

    # The scratch band stops noise resetting the counter. 5000 risk * 0.1 = 500.
    b11 = fresh(tmp)
    b11.open_position(Position("RELIANCE", 1, 1000.0, 990.0, "BUY"))
    b11.close_position("RELIANCE", -1000.0)
    b11.open_position(Position("INFY", 1, 1000.0, 990.0, "BUY"))
    b11.close_position("INFY", +100.0)  # inside the band
    check(
        "a scratch trade leaves the streak alone",
        b11.consecutive_losses == 1,
        "+100 is inside the +/-500 scratch band, so it is neither win nor loss",
    )
    check("a scratch trade still counts as a trade", b11.trade_count == 2)

    section("Daily limit breaker")
    b12 = fresh(tmp)
    b12.realized_pnl = -20_000.0
    d12 = b12.check_breakers({})
    check("hitting the daily limit halts", b12.state is RunState.HALTED and not d12.allowed, d12.reason)

    # An open position can breach the limit with no trade closing at all. A
    # breaker that only ran on close would miss this entirely.
    b13 = fresh(tmp)
    b13.open_position(Position("INFY", 200, 1500.0, 1400.0, "BUY"))
    b13.check_breakers({"INFY": 1400.0})
    check(
        "an unrealized breach halts too",
        b13.state is RunState.HALTED,
        "20,000 down on an open position, no trade closed",
    )

    # -------------------------------------------------------- state and switch
    section("Halt is sticky, kill switch, reduce-only")

    b14 = fresh(tmp)
    b14.halt("test halt")
    restored = fresh(tmp)
    check(
        "a halt survives a restart",
        restored.restore() and restored.state is RunState.HALTED,
        f"restored state={restored.state.value}",
    )
    check("a restored halt refuses entries", not restored.can_open("RELIANCE", 1.0, {}).allowed)

    other_day = RiskBudget(settings=make_settings(tmp), trading_date=date(2026, 8, 14))
    check("a halt does not carry into the next session", not other_day.restore())

    b15 = fresh(tmp)
    Path(b15.settings.kill_switch_file).write_text("stop", encoding="utf-8")
    dk = b15.can_open("RELIANCE", 100.0, {})
    check("kill switch file blocks entry", not dk.allowed and dk.code == "kill_switch", dk.reason)
    Path(b15.settings.kill_switch_file).unlink()
    check("removing the file unblocks, no restart", b15.can_open("RELIANCE", 100.0, {}).allowed)

    b16 = fresh(tmp)
    b16.reduce_only("mandate expired")
    dr = b16.can_open("RELIANCE", 100.0, {})
    check("reduce-only refuses new entries", not dr.allowed and dr.code == "reduce_only", dr.reason)
    check("reduce-only still allows closing", b16.state.may_close, "otherwise a position is trapped")
    check("halted allows neither", not RunState.HALTED.may_close and not RunState.HALTED.may_open)

    b17 = fresh(tmp, trading_enabled=False)
    dt = b17.can_open("RELIANCE", 100.0, {})
    check("TRADING_ENABLED=false blocks entry", not dt.allowed and dt.code == "trading_disabled")

    # ------------------------------------------------------------------ summary
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
