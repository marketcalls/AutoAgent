# 006 - The planner, the control surface, and the dry run (steps 9, 10, 11)

Date: 2026-08-13

This is the last build note. Steps 0 through 11 are done and the app runs end to end.
Step 12 is going live with real money, which is not a decision code should make on its
own.

## The planner

Runs once before the open and exits. It loads history, backtests all three strategies
over both windows, reads the market regime, **decides**, then asks the model to explain
the decision, then writes a plan file for a human to approve.

The order is the point. The model arrives *after* the decision is made. It writes the
explanation and may propose a smaller risk size, and that is all. It cannot change the
strategy, cannot increase risk, and cannot make the agent trade on a day the rules
declined.

Why not let the model choose? Because the choice has to be replayable. If a model picked
the strategy, you could not backtest the morning decision, two runs on the same data
could disagree, and a bad day could never be properly explained afterwards.

One asymmetry worth stating plainly:

- **Model unavailable** - the plan is still written. Losing the narrator must not stop
  the machine.
- **Backtest fails** - no plan is written and the session does not run. The explanation
  is commentary; the numbers are the decision.

Here is what it actually produced:

> We are standing down today because every tested strategy shows negative expectancy over
> the long window, with the best performer at -0.157R and no edge in either direction. The
> chop regime, with median ADX at 21.7 and only two of five symbols trending, offers no
> reliable setup. Trading nothing avoids the certainty of expected loss.

## A live switch was emptying every backtest

The first run of the planner produced a table of zeros and concluded, entirely
plausibly, that no strategy was viable.

The risk engine checks the kill switch and the master trading switch before allowing any
position. Those are **live** switches - they say whether this process may trade right
now, not whether the market would have allowed the trade. With trading disabled, which is
the shipped default, every simulated entry was refused.

Earlier measurements only looked right because I had turned trading on by hand in
throwaway scripts. Run the way the system actually ships, the backtest was empty and said
so in language indistinguishable from a real result.

This is the second time a simulation read live state. The first wrote halt files into the
live directory and stopped the real agent from starting. The rule is now explicit in the
code: **a simulation must not consult live operational state at all.**

## The control surface

A web page, not a chat. Four things: the plan awaiting approval, the live position board,
a large halt button, and the post-session review.

The plan page **re-derives** the risk figures rather than echoing them, so the limits are
enforced by the server, not by whatever wrote the file. A plan asking for 5% renders as
0.125% with the reason attached, and the page shows the consequence in rupees: 1,250 at
risk per trade, 3,750 across three positions, 0.375 percent of the allocation.

Two things worth recording:

**Vite serves on the hostname, not the IP.** `http://127.0.0.1:5178` is refused while
`http://localhost:5178` works. Twenty minutes lost to that, and it is documented in the
sibling project too.

**The blocking fix, measured.** A cold health check takes 42.6 ms because it really calls
the broker; warm, 0.9 ms. Thirty simultaneous requests that all miss the cache take 46 ms
in total - one broker round trip, not thirty. The live event stream keeps its 2-second
rhythm even under that load.

## The dry run, and why it mattered most

The plan calls for paper trading in the broker's sandbox. That can only run during market
hours, and the sandbox refuses intraday orders after 15:15. So instead: drive the real
executor through a real historical session, bar by bar, against a stub broker that answers
consistently.

**It found five bugs that 115 passing tests could not**, because each needed a whole
session to appear.

**The executor never re-checked its position during the session.** It reconciled at
startup and never again. So an order that was sent never got confirmed, the end-of-day
squareoff saw nothing it recognised as a position, and the first run finished with the
**broker holding four positions the agent believed it did not have** - overnight, with no
stop and nobody managing them.

**Three bugs about time, stacked.** Timeouts were measured against the wall clock while
the machine runs on bar timestamps; replayed over history no wall-clock time passes, so no
timeout could ever fire. Fixing that exposed the next one: 30 seconds is a sensible limit
on a wall clock and meaningless on a clock that jumps five minutes at a time, so every
order timed out on the next bar before it could be confirmed. And the price data carries a
timezone while broker timestamps do not, so comparing them raised an error that would have
fired on the first real reconciliation.

**A successful exit read as a problem.** After the exit order filled, the agent was still
in "exiting" while the broker was already flat - and the safety check that watches for
"I think I hold something the broker does not show" halted the session over it. The agent
was treating its own successful exit as a discrepancy.

After the fixes: 72 bars, 12 actions, no halt, every position closed at 15:10.

```
BROKER FLAT: True
```

The stub does not model partial fills, rejections or slippage. This proves the wiring; the
sandbox proves the fills. Confusing the two is how a dry run becomes false comfort.

## Where the project stands

Everything in the plan is built and tested except going live.

```
parity checks       130   live market data
risk budget          46
state machine        30
reconciliation       20
selector             19
                    245 total
```

The app runs: planner writes a plan, API serves it with the limits applied, the page
renders it, the executor takes a session to flat.

## The one thing that does not work

**None of the three strategies makes money.**

Over 93 sessions across five stocks, every one loses, on both measurement windows, and at
zero slippage the gross result is still flat to negative. The problem is the signal, not
the costs.

So the agent, working exactly as designed, declines to trade every morning. That is the
correct behaviour and it is also not a business.

The plan predicted this as its largest risk and said the architecture could not fix it.
That turned out to be exactly right.

## What is next, and who decides

Step 12 is live trading at reduced size. It needs a human to turn trading on, and it
should not happen while the measured edge is negative.

Two honest options, and the choice is the owner's:

1. **Find an edge.** Everything built is independent of the strategy - swapping one in is
   a single file with a known contract, and the backtest and comparison tooling now make
   testing an idea quick. Different timeframe, wider stops, a trend filter, a different
   family of strategy altogether.
2. **Run it in the sandbox anyway.** It would faithfully record a system declining to
   trade, which proves the operational side keeps working across real sessions but tells
   you nothing about profitability.

The machine is finished. The edge is not.
