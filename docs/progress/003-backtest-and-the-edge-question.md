# 003 - The portfolio backtest, and the answer nobody wanted (steps 3 and 4)

Date: 2026-08-13

## The short version

The machine works. The strategies do not.

All three strategies lose money over 93 trading sessions on the five-stock basket,
before and after costs, on both the long and the short measurement window. This is not
a bug in the backtest. It is the answer to the question the backtest was built to ask.

## What was built

**The portfolio replay.** All five stocks on one shared timeline, sharing one pot of
risk, one position limit, and one set of circuit breakers. Signals are read at the close
of a bar and the trade opens at the next bar's open. Stops are checked against the bar's
high and low rather than its close. Everything is flat at 15:10.

**The metrics layer.** Turns a list of completed trades into the comparison table the
morning selector will read.

## Why one shared timeline matters

The obvious way to test five stocks is to test each one separately and add up the
results. That overstates performance every time, and never understates it, because it
quietly ignores:

- The shared risk budget. Some of those trades would never have been allowed.
- The limit of three positions at once.
- The fact that correlated stocks lose on the same days, so the drawdowns stack.
- What happens when three stocks signal on the same bar and there is only room for two.

## The result

Over 93 sessions (long window) and 19 sessions (short window):

```
strategy                 window  trd   win%   exp R     PF        net    ret%  maxDD%   ok
stg1_ema_10_20           long    269   20.2  -0.427   0.65   -268,224   -26.8    35.4   no
stg1_ema_10_20           short    55   14.8  -0.916   0.13   -136,543   -13.7    13.5   no
stg2_supertrend_3_10     long    330   34.6  -0.125   0.73   -199,138   -19.9    27.2   no
stg2_supertrend_3_10     short    69   31.8  -0.261   0.51    -84,183    -8.4     8.6   no
stg3_sma10_ema30         long    267   24.5  -0.280   0.72   -201,339   -20.1    31.7   no
stg3_sma10_ema30         short    55   23.1  -0.532   0.53    -64,480    -6.4     6.2   no
```

Reading this in plain terms:

- **Expectancy** is what an average trade makes, measured in units of what it risked.
  Every number is negative. The best is Supertrend at -0.125, meaning an average trade
  loses about an eighth of what it put at risk.
- **Profit factor** is money won divided by money lost. Above 1.0 is profitable. The best
  here is 0.73.
- **Both windows agree.** If only the recent window were bad, that would suggest a rough
  patch. Both being bad over 93 sessions says the strategies do not work on these stocks
  at this timeframe, full stop.

The two windows exist precisely so their disagreement can be spotted. There is no
disagreement to spot.

## Was it the costs?

That was the obvious suspicion, so it was tested directly by re-running with slippage
turned down to nothing:

```
5.0 bps/leg -> net -186,039   gross  -1,268
2.0 bps/leg -> net -108,215   gross  -1,012
1.0 bps/leg -> net  -82,861   gross  -1,847
0.0 bps/leg -> net  -62,778   gross  -7,157
```

Even with zero slippage the gross result is roughly flat to negative. **The problem is
the signal, not the friction.** Trading costs make a bad system worse; they did not
create the problem.

## Three bugs found by running it

**The cost model was 7.2 times too pessimistic.** It charged brokerage as a flat
percentage of turnover. Indian discount brokers charge the *lower* of 0.03 percent or 20
rupees per order, and at any realistic size the 20-rupee cap is what applies. On 10 lakh
of stock that is 20 rupees, not 300. The model said a round trip cost 2,850 rupees; the
real figure is 398.

This one nearly caused a false conclusion. Had it gone unnoticed, all three strategies
would have been written off on a spreadsheet error rather than on their actual
performance.

**Position sizes could exceed the entire allocation.** Sizing works backwards from the
stop: risk 5,000 rupees, stop 10 rupees away, buy 500 shares. But on five-minute bars the
stop is often very close - 2 rupees on a 1,300-rupee stock - and then the formula asks
for 2,500 shares, which is 32.5 lakh of stock against a 10 lakh allocation. Three of
those at once is roughly ten times the account.

A live broker would simply reject those orders. In a backtest they silently inflate
turnover and therefore costs. There is now a separate cap on position value. Risk
controls how much you can lose; this controls how much you can buy. Both are needed and
the plan only had the first.

**TATAMOTORS no longer exists under that name.** It returns an error; the stock is now
TMCV after the demerger. The first measurement quietly ran on four stocks instead of five.

## Two landmines found in the indicator library

Reported by the agent that built the strategies, and both would have produced working
code that was silently wrong:

**Supertrend's direction flag is backwards** from the usual convention. Measured over 600
bars: it reads -1 on all 278 bars where price was above the line, and +1 on all 313 where
it was below. Reading +1 as "uptrend" would have reversed every single trade, and nothing
would have crashed.

**Moving averages disagree about their own warm-up.** The exponential average returns a
number from the very first bar, with no warm-up gap at all, while the simple average and
ATR correctly leave the early bars blank. Those made-up early EMA values cross each other
and generate entries that no live session could ever have taken. Each strategy now
declares its own warm-up period and those bars are masked out.

## Numbers

- 130 parity checks pass; live and backtest signal paths are bit-identical
- 46 risk engine tests pass
- 93 sessions of real 5-minute data across five stocks
- Best strategy expectancy: -0.125R

## What this means for the plan

The plan named this as risk number one: *"The three strategies may have no positive
expectancy after costs. This is the largest risk and the architecture cannot mitigate
it."* It also said step 4 would answer it before anything was built on top. That is
exactly what happened.

Everything built so far is strategy-agnostic and still stands: the risk engine, the
budget, the shared-timeline replay, the parity guarantee, the bar store, the cost model.
Swapping in a different strategy is a single module.

What does not stand is the step 5 selector as designed. Choosing the best of three losing
strategies each morning is choosing which way to lose. The selector should be built to
answer "should I trade at all today" first, and "which one" second - which is what the
plan already argues for, but the weighting has changed.

## What is next

An honest decision is needed before more building, and it belongs to the project owner:

1. Keep building the machine and accept that it will paper-trade a losing strategy until
   a better one is found. The infrastructure is genuinely reusable.
2. Pause and investigate the edge: different timeframe, different stops, a trend filter,
   or a different strategy family altogether. The replay and metrics now make that a
   fast question to ask.

The recommendation is (2) before step 5, because the selector's whole purpose assumes
there is something worth selecting.
