"""Backtest package - research and pre-open only.

The scope rule from PLAN.md Part 4, restated because it is load-bearing: vectorbt is a
research and pre-open dependency. It is NEVER imported by the executor and never runs
during a trading session. The live path is the execution adapter plus the Part 6 state
machine.

That rule is enforced physically here. vectorbt does not run in the interpreter the
executor uses; it runs in a separate, pinned environment. See ENVIRONMENT.md in this
directory for why, for the exact working version set, and for the commands to
reproduce it.

Consequence for anything added to this package: nothing at module import time may
import vectorbt, or `import app.backtest` breaks for every caller running under the
executor's interpreter. Import it inside the function that needs it.

Modules planned here (PLAN.md Appendix D):
    replay.py      faithful fill simulation, produces the trade list
    metrics.py     two-window statistics from a trade list
    vbt_report.py  vectorbt statistics and tearsheet, isolated environment only
"""

from __future__ import annotations

__all__: list[str] = []
