# 004 - The selector, and the intent log (steps 5 and 6)

Date: 2026-08-13

## The selector, and why it usually says no

Step 4 found that all three strategies lose money. That changed what step 5 needed to be.

The obvious selector ranks three strategies and returns the best one. Given the actual
numbers, that selector would have picked a losing strategy every single morning and
called it a decision. Ranking is not selecting.

So the gates run in this order, which is the opposite of the obvious one:

1. **Is anything worth trading at all?** If not, trade nothing. Stop here.
2. **What kind of day is it?** This picks between the survivors.
3. **Is the new pick clearly better than yesterday's?** If not, keep yesterday's.

Run against the real basket today, it says:

```
regime     : chop
viable     : []
DECISION   : none  trade_today=False
reason     : no strategy is viable: none has positive expectancy over the long
             window with enough trades to believe it. Best of a bad set was
             stg2_supertrend_3_10 at -0.125R. Trading nothing today.
```

That is the system working, not failing. Doing nothing is a valid and often correct
output for a trend-following system on a choppy basket.

A test asserts explicitly that it does **not** quietly fall back to the least-bad loser,
and another asserts that a favourable market cannot rescue a losing set - the market read
chooses *between* workable strategies, it cannot make an unworkable one workable.

## Two settings that were guesses are now measurements

**How trending is trending?** ADX is the standard measure. Across the five stocks the
medians came out:

```
RELIANCE  27.3    HDFCBANK  15.0    INFY  26.1    TMCV  24.6    SBIN  15.6
basket mean 21.7
```

Set the bar at 20 and three of five count as trending, so the day reads as a trend day.
Set it at 25 and only two do, so it reads as chop. Set it at 30 and none do.

25 is the conventional threshold, and it agrees with what the profit figures say
independently - these are large, liquid stocks that mostly chop on a five-minute chart.
So `REGIME_ADX_FLOOR=25`.

**How much better must a challenger be before switching?** `HYSTERESIS_MARGIN=0.05`, in
units of risk per trade. Without a margin the choice flips on noise and you end up owning
the average of all three strategies plus the cost of switching between them.

Two smaller decisions worth recording. The recent window is deliberately given less weight
than the long one, and a recent window with too few trades is ignored entirely rather than
allowed to vote - three trades should not overrule sixty. And an incumbent strategy that
stops being viable is dropped immediately with no margin test, because the margin exists
to resist noise, not to protect a strategy that has genuinely stopped working.

## The intent log

An order-level record cannot answer the question that matters most to an unattended
system: **what do I think I am holding right now?**

A single trade is three orders - the entry, the stop, and the exit. So there is now one
record per *trade*, sitting above the order-level audit rather than replacing it. That one
record does three jobs: it is what reconciliation reads, it is the audit trail, and it is
where profit and loss comes from.

### The part that took the most thought

Step 0 found that OpenAlgo has no field for a caller-supplied order id. Nothing you attach
travels to the broker and comes back.

That matters because of one specific failure: the agent sends an order, the connection
drops, and it does not know whether the order arrived. With an id you would simply ask.
Without one you have to match on stock, side, quantity and roughly when.

The response is a deterministic internal id built from the strategy, the stock, the exact
bar that produced the signal, and the direction:

```
stg1:RELIANCE:20260813T0935:BUY
```

It is readable rather than a hash, because someone reading the log mid-afternoon needs to
understand it. And **collisions are the point**. If the same signal is processed twice, it
produces the same id, which lands on the same database row instead of creating a second
one. Verified: writing the same intent twice leaves exactly one row.

The other half is write ordering:

```
write the intent  ->  send the order  ->  write the result
```

A crash between the first and second leaves a record saying "I sent something and never
heard back", which reconciliation can resolve against the broker's order book. Sending
first and writing after would leave a live position with no record of it at all.

### One state deserves special mention

`UNKNOWN` is treated as **possibly holding a position**, not as flat.

Not knowing is not the same as not holding. An agent that treats an unanswered order as
"nothing happened" is an agent that walks away from a live position.

## What was tested

- 19 selector tests, offline, covering every gate and both refusal paths
- Intent log: deterministic ids, retry collision, the full lifecycle from signal to
  closed, and that unresolved records disappear only when genuinely resolved
- 46 risk engine tests still pass
- 130 parity checks still pass

## What is next

- The order state machine that drives an intent through its lifecycle, and reconciliation
  against the broker on every wake
- The executor loop
- The control surface: a web page showing the plan, the live position board, and a large
  halt button

The open question from the last note has not gone away: the machine is being finished, but
it currently has nothing profitable to run. Everything built is strategy-agnostic, so a
better strategy is a single module - but until there is one, a paper campaign would
faithfully record a system that correctly declines to trade.
