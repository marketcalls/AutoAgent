# 005 - The state machine, the executor, and a backtest that halted the agent (steps 6, 8)

Date: 2026-08-13

## The bug of the day

Running the executor's startup check against the real broker for the first time:

```
start -> False | restored halt from disk: 3 consecutive losses
```

Nothing had ever traded. Not one order had been placed.

Here is what happened. The backtest builds a fresh risk budget for each simulated
trading day, using the real configuration. When a simulated day hit three losing trades
in a row, the risk engine did exactly what it is designed to do: it stopped, and it wrote
that decision to disk so a restart could not undo it.

Except the disk it wrote to was the live one. Fifty-six of those files had piled up from
backtesting - one per simulated bad day - and the live agent read the most recent and
refused to start.

**A simulation had stopped the real thing.**

The fix is one flag: the risk engine now knows whether it is running for real. Live
sessions persist their halts, simulations do not. Verified by re-running the backtest and
confirming it now writes zero files to the live directory.

What makes this worth writing down is that it was the *good* kind of failure. The sticky
halt worked precisely as designed, and that is the only reason the contamination became
visible at all. Had halts been forgotten on restart, the backtest would have written
those files and nobody would ever have noticed.

## What was built

### The state machine

One per stock, five of them sharing a single pot of risk. Each one drives a trade from
"the strategy wants in" through to "we are flat", and handles everything that can go
wrong on the way.

**The ordering rule.** The test pins the exact sequence of broker calls:

```
place LIMIT buy -> place SL-M sell -> cancel -> place MARKET sell
```

The protective stop is cancelled *before* the exit order is sent. Send the exit while
the stop is still sitting at the broker and both can fill - leaving you short when you
were long. It is the most expensive ordering mistake available in this system, so the
test checks the exact order rather than merely that both calls happened.

**Never retry blindly.** If an order is sent and nothing comes back, the machine does not
send it again. It cancels, and hands the problem to reconciliation. Since the broker
offers no way to tag an order as yours, a second order is indistinguishable from the
first - and ending up with two positions costs far more than missing one trade. A test
asserts that the order count does not increase after a timeout.

**Never sit naked.** There is a state for "the buy filled but the stop has not been
accepted yet". The position is unprotected in that moment. The machine retries the stop
once, and if it still fails, it closes the position at market rather than leave it
exposed. A fill alone is not protection, so the machine only calls a position "open"
after the broker confirms the stop.

### Reconciliation

Runs before any decision, every time the agent wakes. It compares what the agent believes
against what the broker actually holds.

Four outcomes:

| Situation | What happens |
|---|---|
| Two orders that look identical | **Stop.** Cannot tell which is ours |
| Cannot read the order book | **Stop.** And specifically do not conclude anything |
| We think we hold something the broker does not show | **Stop** |
| Broker holds something we never opened | Note it, carry on |

The second is subtle. Without the order book, "the order never arrived" and "the order
filled" look identical. Treating the second as the first would silently abandon a live
position, so an unreadable book must resolve nothing at all.

The fourth is the shared-account case. If you also trade this account by hand, the broker
reports one merged position per stock. A position the agent never opened is almost
certainly yours, and the agent's job is to leave it alone, not to halt the day over it.

**A bug the tests caught here too.** The check for "we think we hold something the broker
does not show" was nested inside the "no matching order" branch - and a filled order stays
in the order book all day, so a match was always found and that check could never run. The
agent could have believed it held a position the broker had already closed.

### The executor

Ties it together. A clock decides which phase the day is in; the strategy says what the
signal is; the budget says whether it is affordable; the machines do the ordering.

Entries are sent **one at a time**, not all at once. Each fill uses up margin and risk
budget, so the second order has to be sized after the first lands. Speed was never the
constraint here; getting the running budget right is.

One small clock detail that would have been wrong all day: five-minute bars are counted
from the 09:15 open, so they close at :20, :25, :30. Counting from the top of the hour
instead gives :00 and :05 - five minutes out, every bar, forever.

## What was tested

```
risk budget      46 passed
state machine    30 passed
reconciliation   20 passed
selector         19 passed
                115 total, all offline
parity          130 passed against live market data
```

The web API was started and every route checked by hand: all return 200, the health
check reports a live broker connection in analyzer mode, and the live event stream emits
correctly formatted frames.

## Also found

**The frontend would not build.** TypeScript 7 removed a configuration option that
TypeScript 5 required. One line, but the build fails outright until it is removed.

**The web API had left its own halt file behind** from its testing, exactly the same class
of problem as the backtest. Cleared. Worth watching: anything that can write live state
during a test needs to be told it is a test.

## What is next

- Finish the control surface: the approval page, the live board, and the halt button
- The planner that runs before the open and writes the day's plan
- A paper campaign

The honest position has not changed. The machine is nearly complete and correct, and it
currently has nothing profitable to run. It will faithfully decline to trade every
morning until a strategy with a real edge exists. Everything built is independent of the
strategy, so that is a single module - but it is the module that matters.
