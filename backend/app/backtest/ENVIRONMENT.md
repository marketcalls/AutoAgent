# Backtest environment

How to run vectorbt for this project, why it does not run in the same interpreter as
the executor, and the exact version set that works.

Written 2026-08-13, resolving the one failure step 0 found:

```
[FAIL] vectorbt usable - ImportError: Numba needs NumPy 2.4 or less. Got NumPy 2.5.
```

---

## The failure, diagnosed

Two independent version conflicts, not one. The error message only names the first.

**1. numba against numpy.** `vectorbt/_typing.py` imports `numba.core.registry`, and
numba raises during its own `_ensure_critical_deps()`:

```
numba 0.65.1  requires  numpy<2.5,>=1.22        global numpy is 2.5.2
numba 0.66.0  requires  numpy<2.5,>=1.22        still excludes 2.5
numba 0.67.0  requires  numpy<2.6,>=1.22        includes 2.5
```

**2. vectorbt against pandas.** Not reported, because the import dies before it gets
there:

```
vectorbt 1.0.0  requires  pandas<3.0,>=2.0      global pandas is 3.0.5
vectorbt 1.1.0  requires  pandas<4.0,>=3.0.3    and numpy>=2.4.6, numba>=0.66
```

So the installed vectorbt 1.0.0 was never going to work on this stack even with the
numpy problem solved. That matters for evaluating the options below: any fix built
around "pin numpy down" also drags pandas down a major version.

## Options evaluated

### (a) Dedicated venv with numpy pinned to 2.4 or less - rejected as stated

The isolation is right; the pin is wrong. numpy at 2.4 keeps vectorbt 1.0.0, which
requires `pandas<3.0`. The backtest environment would then run pandas 2.x while the
executor runs pandas 3.x.

That directly threatens the gate the whole plan rests on. PLAN.md Part 3 requires the
execution adapter and the backtest adapter to produce **identical** signal series from
the same frame, and Part 9 makes that a build gate. Running the two halves on different
pandas major versions - different copy-on-write semantics, different default string
dtype, different resample and groupby edge behaviour - is exactly the silent
backtest-live divergence risk 3 in Part 11 describes. A parity test that passes under
that arrangement proves less than it appears to.

### (b) Downgrade numpy globally - rejected

`pandas 3.0.5` declares `numpy>=2.3.3` on Python 3.14, so numpy 2.4.6 would satisfy it
and the executor would import. But it does not actually fix anything on its own:
vectorbt 1.0.0 still refuses pandas 3, so this option only reaches a working vectorbt by
also downgrading pandas to 2.x - a global, executor-affecting downgrade in service of a
research tool.

Blast radius, measured on the global environment: 46 installed distributions declare a
numpy dependency and 22 declare pandas. The executor's own broker dependency,
`openalgo==2.0.3`, is pinned exactly by policy (Part 1) precisely because moving what is
underneath it is expensive. Moving the numeric floor under all of them, untested, to
make an offline tool import is the wrong trade.

### (c) Newer numba and newer vectorbt - accepted, and this is the mechanism

`numba 0.67.0` widens the pin to `numpy<2.6`, and `vectorbt 1.1.0` targets exactly the
stack this project already runs: `numpy>=2.4.6`, `pandas>=3.0.3,<4.0`, `numba>=0.66`,
Python `>=3.11,<3.15`. Nothing has to move down. numpy and pandas stay where the
executor needs them.

## Decision

**Option (c)'s versions, installed into option (a)'s isolated environment.**

A dedicated venv at `AutoAgent/.venv-backtest/`, pinned to the *same* numpy and pandas
the global environment runs, differing only in what the executor never imports: numba,
llvmlite and vectorbt.

Two reasons for keeping the isolation even though (c) would also work as a global
upgrade:

1. **The executor's dependency surface stops changing.** vectorbt is a research and
   pre-open dependency (Part 4). Nothing that only serves research should be able to
   move a package the order path imports. Upgrading numba globally is a small risk
   today, but the rule "the backtest environment never touches the trading environment"
   costs nothing to hold and does not need re-litigating at every future upgrade.
2. **The scope rule becomes physical rather than a convention.** "vectorbt is never
   imported by the executor" is enforced by vectorbt not being installed where the
   executor runs. A stray `import vectorbt` in execution code fails at once, in
   development, instead of at 09:20 on a Tuesday.

The cost is that numpy and pandas are duplicated on disk and their pins must be kept
equal to the global ones by hand. That is a real maintenance obligation - if the two
ever diverge on a pandas major version, this document has failed and option (a)'s
problem is back. **The pins are stated below for that reason: they are the contract,
not a snapshot.**

Note what is *not* claimed: this is not a statement that a global upgrade to
`numba==0.67.0` plus `vectorbt==1.1.0` would break anything. It was measured to be
compatible. If the team later prefers one interpreter, that upgrade is the supported
path and the version set is identical.

---

## Reproduce

From the repository root, `AutoAgent/`. The global interpreter must be Python 3.14
(`vectorbt 1.1.0` accepts `>=3.11,<3.15`).

```
python -m venv .venv-backtest
.venv-backtest\Scripts\python.exe -m pip install --upgrade pip
.venv-backtest\Scripts\python.exe -m pip install ^
    "numpy==2.5.2" "pandas==3.0.5" "numba==0.67.0" "vectorbt==1.1.0" ^
    "openalgo==2.0.3" "pyarrow==22.0.0"
```

`openalgo` is in the list because `strategies/` calls `openalgo.ta` for its indicators
and the backtest adapter runs the same strategy module the executor does. `pyarrow` is
there because the historical bar store (`app/data/bars.py`) is parquet.

Run anything in that environment with its interpreter directly:

```
.venv-backtest\Scripts\python.exe your_script.py
```

`.venv-backtest/` is not covered by the current `.gitignore` (which lists `.venv/` and
`venv/`). **Add `.venv-backtest/` to `.gitignore` before any commit.**

## The version set that works

Verified together on Windows 11, 2026-08-13.

| Package | Isolated backtest env | Global (executor) env | Must match? |
|---|---|---|---|
| Python | 3.14.4 | 3.14.4 | yes |
| numpy | 2.5.2 | 2.5.2 | **yes** |
| pandas | 3.0.5 | 3.0.5 | **yes** |
| numba | 0.67.0 | 0.65.1 | no |
| llvmlite | 0.49.0 | 0.47.0 | no |
| vectorbt | 1.1.0 | 1.0.0 (broken, unused) | no |
| openalgo | 2.0.3 | 2.0.3 | yes, pinned by policy |
| pyarrow | 22.0.0 | 22.0.0 | no |

numpy and pandas are the two that matter. They are shared inputs to the strategy code
that both environments run, and a divergence there is a parity-gate failure waiting to
happen. numba, llvmlite and vectorbt exist only inside the venv's blast radius.

The broken `vectorbt 1.0.0` in the global environment is harmless - nothing imports it -
but `scripts/validate_setup.py` will keep reporting the step 0 failure until it is
either uninstalled globally or the check is pointed at this interpreter.

## Verification

Real run, real bars, on `.venv-backtest`. Source data is the historical bar store,
which is read from disk with a fetcher that raises if anything attempts a network call.

Strategy is EMA(10)/EMA(20) on RELIANCE 5m over 90 stored sessions, signals shifted one
bar so vectorbt cannot fill on the signal bar itself (PLAN.md Part 4, requirement 1),
with fees at 0.05 percent per side and slippage at 0.03 percent.

```
interpreter : D:\AI Bootcamp 2026\Day09\AutoAgent\.venv-backtest\Scripts\python.exe
python      : 3.14.4
numpy       : 2.5.2
pandas      : 3.0.5
numba       : 0.67.0
vectorbt    : 1.1.0

RELIANCE 5m from the bar store: 6723 bars, 90 sessions, 2026-04-06 .. 2026-08-13
signals: 137 entries, 137 exits

--- vbt.Portfolio.from_signals stats ---
Start                         2026-04-06 09:15:00+05:30
End                           2026-08-13 15:10:00+05:30
Period                                 23 days 08:15:00
Start Value                                   1000000.0
End Value                                 811806.790097
Total Return [%]                             -18.819321
Benchmark Return [%]                          -1.860013
Max Gross Exposure [%]                            100.0
Total Fees Paid                           125240.172858
Max Drawdown [%]                              23.200947
Max Drawdown Duration                  18 days 09:10:00
Total Trades                                        137
Total Closed Trades                                 137
Total Open Trades                                     0
Open Trade PnL                                      0.0
Win Rate [%]                                  18.978102
Best Trade [%]                                 5.264649
Worst Trade [%]                               -2.983333
Avg Winning Trade [%]                          0.986443
Avg Losing Trade [%]                          -0.414607
Avg Winning Trade Duration       0 days 04:57:06.923076
Avg Losing Trade Duration        0 days 01:05:40.540540
Profit Factor                                  0.559066
Expectancy                                 -1373.673065
Sharpe Ratio                                   -9.91308
Calmar Ratio                                  -4.144703
Omega Ratio                                    0.866145
Sortino Ratio                                -13.770515

trades recorded: 137
```

The script that produced it:

```python
"""Environment smoke test. NOT the backtester: no next-bar-OPEN fill, no SL-M stop,
no 15:10 square-off, no shared portfolio budget. Those are build steps 3 and 4."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]      # adjust to the AutoAgent root
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np, numba, pandas as pd, vectorbt as vbt
from openalgo import ta
from app.data.bars import BarStore

def _no_network(**kwargs):
    raise AssertionError("backtesting must never hit the broker API")

store = BarStore(fetch=_no_network)
frame = store.get_frame("RELIANCE", 90)
close = frame["close"]

fast = pd.Series(ta.ema(close, int(10)), index=close.index)
slow = pd.Series(ta.ema(close, int(20)), index=close.index)
above = fast > slow
entries = (above & ~above.shift(1, fill_value=False)).shift(1, fill_value=False)
exits = (~above & above.shift(1, fill_value=False)).shift(1, fill_value=False)

pf = vbt.Portfolio.from_signals(
    close, entries, exits,
    init_cash=1_000_000, fees=0.0005, slippage=0.0003, freq="5min",
)
print(pf.stats().to_string())
print(pf.trades.records_readable.tail(3).to_string())
```

### Reading those numbers

They verify the environment. They do not measure the strategy, and three things in that
output are artefacts of the smoke test rather than findings:

- **`Period` reads 23 days for 90 sessions.** With `freq="5min"`, vectorbt counts bar
  time, and 6,723 five-minute bars is 23.3 days of market time. Every annualised ratio
  in the table - Sharpe, Calmar, Sortino - is scaled off that, so those figures are not
  comparable to daily-frequency ones. The metrics module at step 4 has to decide this
  deliberately; do not copy `freq="5min"` into it without deciding.
- **Overnight gaps are held through.** `from_signals` with no exit signal keeps the
  position across the close. The real system is flat by 15:10, so the modelled EOD flat
  (Part 4, requirement 3) changes both the trade list and the cost total.
- **Fees of 125,240 against a 1,000,000 book over 137 trades** is the point Part 4 makes
  about unmodelled costs, visible in one number.

## Rules for anything added to this package

- **Nothing here may be imported by the executor.** vectorbt is not installed in the
  executor's interpreter, so a violation fails loudly and immediately.
- **No module-level `import vectorbt`,** including in `__init__.py`. `app.backtest` has
  to stay importable from the global interpreter - `replay.py` and `metrics.py` run
  there. Import vectorbt inside the function that uses it, in `vbt_report.py` only.
- **The replay loop wins any disagreement** with vectorbt (Part 4), because it is the
  one that mirrors live. A disagreement is still a bug report against both.
