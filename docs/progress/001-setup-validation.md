# 001 - Setup and validation (build step 0)

Date: 2026-08-13

## What this step was for

The plan has a Part 1 called "ground truth". Some of it was copied from the sibling
TradingAgent project and is already proven. The rest was marked OPEN, meaning: we think
this is true, but nobody has checked it on this machine with these package versions.

Step 0 exists so that the guessing stops before any code is written on top of a guess.
Five questions were open. Four are now answered.

## What was built

**`scripts/validate_setup.py`** - a script that answers those five questions by actually
calling the live OpenAlgo server and inspecting the installed packages. It is read-only
by default. It can also place a test order, but only with an explicit `--with-orders`
flag, and even then it refuses to run unless the broker reports analyzer (paper) mode.
It exits with code 1 if anything fails, so it works as a build gate.

**`.env.example` and `.env`** - the full configuration surface, 60 settings. The example
file is committed; the real `.env` holds the API key and is gitignored.

## What was tested and what came back

26 checks passed, 1 failed, 1 skipped. The server was live and the broker was zerodha in
analyzer mode.

### The important finding: there is no client order id

The biggest open question was whether OpenAlgo lets you attach your own identifier to an
order, so that if the connection drops mid-order you can ask the broker "did you get the
one I called ABC123?"

**It does not.** The order function takes only strategy, symbol, action, exchange, price
type, product and quantity. There is no slot for a caller-supplied id.

Why this matters: an autonomous agent that sends an order and then loses the connection
has to work out whether the order arrived. With a client id that is a lookup. Without
one, the agent has to match on symbol, side, quantity and roughly when it was sent -
which is usually enough, but not always.

So the design changes. Where the match is clear, the agent resolves it automatically.
Where it is ambiguous, **the agent stops and asks a human rather than guessing**. A
duplicate intraday position costs more than a missed trade.

The `strategy` field is settable and does survive the round trip, so it stays as the
attribution tag - it is how you tell this agent's orders apart from your own.

### History depth is better than expected

One request returned 4,698 five-minute bars covering 63 trading sessions. Asking for 180
days returned 9,048 bars over 121 sessions.

This is good news for the morning routine. The strategy comparison needs about 90
sessions of history for five symbols. That is five requests, not hundreds, and after the
first fill only the newest day needs fetching.

### The indicator library behaves as the plan assumed

Three things were checked because getting them wrong produces silently wrong numbers
rather than an error:

- Supertrend returns two values, not one.
- ADX returns three values, and **ADX is the third one**, not the first. Reading element
  zero would give you the +DI line and every regime decision would be wrong.
- Passing a period of `10.0` instead of `10` raises an error. This matters because JSON
  turns numbers into floats, so any value arriving from a config file or an API call has
  to be converted back to an integer first.

### The allocation rule proved itself immediately

The broker account holds just under 1 crore. The agent is configured to trade 10 lakh.

If position sizing read the account balance - which is what the sibling project does,
because a human approves each of its orders - every position would have come out ten
times larger than intended. Nothing would have errored. It would just have been wrong.

This is why the plan insists the allocation is a configured number that is never read
from the broker. The check now runs at startup and refuses to begin a session if the
account holds less than the allocation.

### One real failure

vectorbt does not currently import:

```
ImportError: Numba needs NumPy 2.4 or less. Got NumPy 2.5.
```

vectorbt is needed for the backtest statistics at steps 3 and 4. It is never used during
a live trading session, so it can live in its own isolated environment without affecting
the part of the system that places orders. Being resolved now.

## What is still open

- **Sandbox fill realism.** Does paper mode fill orders at a realistic price, or does it
  just fill everything instantly at the last traded price? This decides how much a month
  of paper trading actually proves. Deferred to step 8, when there is an executor to
  test with.
- **Order-update websocket.** The port answers, but whether the feed is reliable enough
  to confirm fills is a step 6 question. Until then the agent will poll.

## What is next

Step 1 and step 2, running in parallel:

- Configuration and logging
- The OpenAlgo client layer, ported from TradingAgent with two known bugs fixed
- The three strategies and their two adapters, plus the parity test that proves the live
  path and the backtest path produce identical signals
- The historical bar store, and a working vectorbt environment
