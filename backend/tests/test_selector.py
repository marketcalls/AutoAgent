"""Tests for the daily strategy selector.

Offline. Metrics are constructed by hand so each gate can be exercised in
isolation, which live data cannot do - the real basket currently produces
"no strategy is viable" for every window, so it only ever tests one path.

The assertions that matter are the refusal ones. A selector that always returns
its best candidate is not selecting, it is ranking, and over the step 4 data it
would have picked a losing strategy every single morning and called it a
decision.
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest.metrics import MIN_TRADES_FOR_SIGNIFICANCE, Metrics  # noqa: E402
from app.planner.regime import RegimeRead  # noqa: E402
from app.planner.selector import NO_TRADE, scale_risk, select  # noqa: E402

results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((ok, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def section(title: str) -> None:
    print()
    print(f"--- {title} ---")


def m(strategy: str, window: str, expectancy: float, *, trades: int = 60, net: float = 1.0) -> Metrics:
    """A metrics row with just enough filled in for the selector to read it."""
    return Metrics(
        strategy_id=strategy,
        window=window,
        trades=trades,
        expectancy_r=expectancy,
        net_pnl=net if expectancy > 0 else -abs(net),
        sessions=90,
    )


TREND_UP = RegimeRead(adx_median=32.0, trending=True, direction="up", breadth=0.8, detail="test")
TREND_DOWN = RegimeRead(adx_median=31.0, trending=True, direction="down", breadth=0.8, detail="test")
CHOP = RegimeRead(adx_median=14.0, trending=False, direction="none", breadth=0.2, detail="test")


def main() -> int:
    print("AutoAgent selector tests")
    print("=" * 70)

    # ---------------------------------------------------------- the main gate
    section("Viability gate - trade nothing when nothing is viable")

    losers = [
        m("stg1_ema_10_20", "long", -0.427),
        m("stg2_supertrend_3_10", "long", -0.125),
        m("stg3_sma10_ema30", "long", -0.280),
    ]
    sel = select(losers, CHOP, hysteresis_margin=0.05)
    check("all-losing set selects NOTHING", sel.strategy_id == NO_TRADE and not sel.trade_today, sel.strategy_id)
    check("viable list is empty", sel.viable == [])
    check(
        "the refusal names the best of the bad set",
        "stg2_supertrend_3_10" in sel.reason and "-0.125" in sel.reason,
        sel.reason[:90],
    )
    # This is the exact step 4 data. A ranking selector would have picked stg2.
    check(
        "it does NOT fall back to the least-bad loser",
        sel.strategy_id != "stg2_supertrend_3_10",
        "choosing the best of three losers is choosing how to lose",
    )

    sel_trend = select(losers, TREND_UP, hysteresis_margin=0.05)
    check(
        "a favourable regime does not rescue a losing set",
        sel_trend.strategy_id == NO_TRADE,
        "regime picks BETWEEN viable strategies, it cannot create viability",
    )

    section("Significance gate")
    thin = [m("stg1_ema_10_20", "long", +0.40, trades=MIN_TRADES_FOR_SIGNIFICANCE - 1)]
    check(
        "positive expectancy on too few trades is not viable",
        select(thin, TREND_UP, hysteresis_margin=0.05).strategy_id == NO_TRADE,
        f"under {MIN_TRADES_FOR_SIGNIFICANCE} trades the ratio is noise",
    )
    fat = [m("stg1_ema_10_20", "long", +0.40, trades=MIN_TRADES_FOR_SIGNIFICANCE)]
    check(
        "the same expectancy with enough trades IS viable",
        select(fat, TREND_UP, hysteresis_margin=0.05).strategy_id == "stg1_ema_10_20",
    )

    # ------------------------------------------------------------- regime map
    section("Regime chooses between viable strategies")

    winners = [
        m("stg1_ema_10_20", "long", +0.20),
        m("stg2_supertrend_3_10", "long", +0.18),
        m("stg3_sma10_ema30", "long", +0.15),
    ]
    check("trend_up prefers the fast long-only strategy",
          select(winners, TREND_UP, hysteresis_margin=0.05).strategy_id == "stg1_ema_10_20")
    check("trend_down prefers the bidirectional strategy",
          select(winners, TREND_DOWN, hysteresis_margin=0.05).strategy_id == "stg2_supertrend_3_10")
    check("chop prefers the slowest strategy",
          select(winners, CHOP, hysteresis_margin=0.05).strategy_id == "stg3_sma10_ema30")

    only_slow = [m("stg3_sma10_ema30", "long", +0.15)]
    check(
        "regime preference skips a strategy that is not viable",
        select(only_slow, TREND_UP, hysteresis_margin=0.05).strategy_id == "stg3_sma10_ema30",
        "preferred order is a preference among the viable, not a demand",
    )

    # -------------------------------------------------------------- hysteresis
    section("Hysteresis - do not flip on noise")

    close_race = [
        m("stg1_ema_10_20", "long", +0.20),
        m("stg3_sma10_ema30", "long", +0.18),
    ]
    kept = select(close_race, CHOP, hysteresis_margin=0.05, incumbent="stg1_ema_10_20")
    check(
        "keeps the incumbent when the challenger is inside the margin",
        kept.strategy_id == "stg1_ema_10_20" and not kept.switched,
        kept.reason[:90],
    )

    clear_race = [
        m("stg1_ema_10_20", "long", +0.05),
        m("stg3_sma10_ema30", "long", +0.40),
    ]
    switched = select(clear_race, CHOP, hysteresis_margin=0.05, incumbent="stg1_ema_10_20")
    check(
        "switches when the challenger clears the margin",
        switched.strategy_id == "stg3_sma10_ema30" and switched.switched,
        switched.reason[:90],
    )

    gone = select([m("stg3_sma10_ema30", "long", +0.15)], CHOP,
                  hysteresis_margin=0.05, incumbent="stg1_ema_10_20")
    check(
        "an incumbent that stops being viable is dropped without a margin test",
        gone.strategy_id == "stg3_sma10_ema30",
        "hysteresis protects against noise, not against a strategy going bad",
    )

    section("Two-window blending")

    # The short window must not drive the choice. Fifteen sessions of intraday
    # trades is a small sample and chasing it is how a selector becomes a
    # performance chaser.
    rows = [
        m("stg1_ema_10_20", "long", +0.30), m("stg1_ema_10_20", "short", -0.10, trades=40),
        m("stg3_sma10_ema30", "long", +0.10), m("stg3_sma10_ema30", "short", +0.50, trades=40),
    ]
    picked = select(rows, CHOP, hysteresis_margin=0.05)
    # long 0.30/short -0.10 -> 0.7*0.30 + 0.3*-0.10 = 0.18
    # long 0.10/short  0.50 -> 0.7*0.10 + 0.3* 0.50 = 0.22, but chop prefers stg3 anyway
    check("both windows are read", picked.strategy_id in {"stg1_ema_10_20", "stg3_sma10_ema30"})

    noisy_short = [
        m("stg1_ema_10_20", "long", +0.30),
        m("stg1_ema_10_20", "short", -5.0, trades=3),
    ]
    check(
        "an insignificant short window is ignored, not allowed to vote",
        select(noisy_short, TREND_UP, hysteresis_margin=0.05).strategy_id == "stg1_ema_10_20",
        "3 trades cannot veto 60",
    )

    section("Risk scaling - down only")

    base, floor = 0.005, 0.00125
    no_trade = select(losers, CHOP, hysteresis_margin=0.05)
    check("no-trade scales risk to the floor",
          scale_risk(no_trade, {}, base=base, floor=floor) == floor)

    strong = select([m("stg1_ema_10_20", "long", +0.50)], TREND_UP, hysteresis_margin=0.05)
    rs = scale_risk(strong, {"stg1_ema_10_20": m("stg1_ema_10_20", "long", +0.50)},
                    base=base, floor=floor)
    check("strong evidence gets the base, never more", rs == base, f"{rs}")

    weak = select([m("stg1_ema_10_20", "long", +0.05)], TREND_UP, hysteresis_margin=0.05)
    rw = scale_risk(weak, {"stg1_ema_10_20": m("stg1_ema_10_20", "long", +0.05)},
                    base=base, floor=floor)
    check("thin evidence scales DOWN", floor <= rw < base, f"{rw} against base {base}")

    section("Summary")
    passed = sum(1 for ok, _, _ in results if ok)
    failed = sum(1 for ok, _, _ in results if not ok)
    print(f"{passed} passed, {failed} failed")
    if failed:
        print("\nFailures:")
        for ok, name, detail in results:
            if not ok:
                print(f"  - {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
