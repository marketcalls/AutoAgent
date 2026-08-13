# 002 - Config, client, strategies, risk engine (build steps 1, 2, 7)

Date: 2026-08-13

Four pieces were built in parallel by separate agents working on files that do not
overlap. This note covers what each one is for, in plain words, and the one result that
mattered most.

## The headline: the parity test passes

This is the check the whole project leans on, so it is worth explaining why it exists.

The agent picks a strategy each morning by backtesting all three over recent history.
Then it trades the winner live. Those are two different pieces of code running the same
idea: the backtest sees the whole history at once, while the live system sees one
five-minute bar at a time as the day unfolds.

If those two ever disagree, the morning selection is measuring a strategy that is not
the one that actually trades. The system would look like it was working and be quietly
wrong. Nothing else in the plan would catch it.

So the strategies are written **once** and wrapped in two thin adapters - one that takes
the whole frame, one that is fed a bar at a time. The parity test proves the two agree:

```
parity over 1023 bars, one bar at a time
  stg1_ema_10_20           bool mismatches    0   max stop delta 0.0000000000
  stg2_supertrend_3_10     bool mismatches    0   max stop delta 0.0000000000
  stg3_sma10_ema30         bool mismatches    0   max stop delta 0.0000000000

PARITY HOLDS
```

Zero disagreements on real RELIANCE data. Stop prices matched to the last decimal, which
is better than expected - EMA accumulates differently depending on how much history it
has seen, so some drift would have been reasonable.

## What was built

### Configuration

Reads the 60 settings from `.env` and refuses to start on a broken combination. The
check that matters:

```
INVARIANT broken: max_concurrent_positions (5) * risk_fraction_base (0.005) = 0.02500,
that is 2.500% of allocation (25000.00 rupees), which exceeds daily_loss_limit_pct / 100
= 0.02000 (2.0%, 20000.00 rupees). Lower MAX_CONCURRENT_POSITIONS to 4, or lower
RISK_FRACTION_BASE to 0.00400.
```

In plain terms: if you allow three positions each risking 0.5%, that is 1.5% of your
money at stake at once, and your daily stop is 2%, so it fits. Allow five and it is 2.5%,
which does not fit - you could blow through the daily limit in one bad move and the limit
would never have had a chance to stop you. The system now refuses to start rather than
discover this at 2pm.

### The broker client

Ported from the sibling TradingAgent project, which had already learned these lessons the
hard way. It talks to OpenAlgo, returns errors as values rather than throwing them, and
cleans price data at the boundary.

That cleaning matters more than it sounds. The indicator library adds up running totals,
so a single missing price poisons every number after it. Measured previously: one gap in
300 bars left an average price broken for 263 of them. After cleaning, a 14-period
average over 1,698 bars has 13 missing values - just the warm-up at the start, which is
expected and correct.

### The three strategies

Each one reads price history and says: enter here, exit here, put the stop there.

```
stg1_ema_10_20           long  44/ 45  short   0/  0
stg2_supertrend_3_10     long  22/ 21  short  21/ 22
stg3_sma10_ema30         long  33/ 33  short   0/  0
```

Two of them only buy. Supertrend both buys and sells short. Entry and exit counts match
within one, which is what you want - the odd one out is a position still open at the end
of the data.

### The risk engine

This is the part that decides how much to buy and when to stop for the day.

**Size comes from the stop, not from a guess.** You decide what a trade is allowed to
lose - 0.5% of the allocation, 5,000 rupees. Then quantity is simply that divided by how
far away the stop is. A jumpy stock needs a wider stop, so you buy fewer shares. Both
trades risk the same money. You never have to think about position size again.

**The daily limit is a budget, not an alarm.** The difference is the whole point.

An alarm asks "have I lost 2% yet?" after each trade. A budget asks, before every single
trade, "if I am wrong about this one, can I still afford it?"

Down 18,000 with a 20,000 limit, a trade risking 5,000 would end at 23,000 if it loses.
The alarm lets you take it and then goes off. The budget refuses it. That case is tested
directly.

Three things it counts that a simple version would miss:

- **Losses on positions you still hold.** Otherwise you can be deep underwater and the
  limit never fires because nothing has been sold yet.
- **Risk already committed.** If three stops can all be hit by the same market move, that
  is one risk, not three separate ones.
- **Costs.** A trade 50 rupees up that paid 80 in brokerage is a loss. Calling it a win
  would let a losing streak run past its limit.

**Stopping is sticky.** When the agent halts, it writes that to disk *before* announcing
it. Restart the process and it comes back halted, not fresh. An agent that forgets it
stopped is worse than one that never stopped.

## Bugs found

**In my own code, the bad one.** The sizing function let you leave out the risk fraction,
and quietly used the minimum when you did. Any caller that forgot the argument got
quarter-size positions and nothing complained. "The planner did not answer" and "this
caller did not say" need opposite safe answers, so they cannot share the same blank. The
argument is now required.

**Two of my test assertions were wrong, not the code.** One case I thought would be
refused was actually affordable - five shares at a 999-rupee stop distance fits inside a
5,000 budget. The other used a stock that is not in the basket, so the sector limit never
applied to it. Both are now written down in the test so nobody re-introduces them.

**In the ported code.** The function that caps how much data goes to the model cut the
text to 12,000 characters and *then* wrapped it in a wrapper - so the result was 12,323
characters. It had been over its own limit the whole time.

**A rate-limiter bug.** The original slept while holding a lock, so every other thread
queued behind it instead of waiting in parallel. Now measured: the lock is held during 0
of 8 samples taken across a one-second wait.

## Numbers from testing

- 46 risk engine tests pass, fully offline
- 36 client checks pass against the live broker
- Parity holds on all three strategies over 1,023 real bars
- History: one request returns 4,698 five-minute bars across 63 sessions

## What is next

- The vectorbt environment. It will not import because a dependency needs an older NumPy.
  It is only used for backtest statistics and never during trading, so it can be isolated.
- The portfolio backtest: all five stocks on one timeline sharing one budget. Testing them
  one at a time and adding up would overstate the results, because it ignores the budget,
  the position cap, and the fact that correlated stocks lose together.
- Then the strategy selector, the order state machine, and the executor.
