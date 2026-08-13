# AutoAgent

An autonomous intraday equity trading agent for Indian markets, built on
[OpenAlgo](https://docs.openalgo.in).

It trades a fixed basket of liquid stocks on a 5-minute timeframe. Each morning it picks
one of three fixed strategies, sizes positions from a risk formula, trades the session
unattended, and squares off before the close.

**You approve a mandate once, before the market opens. Not every trade.**

> **Status: planning.** This repository currently contains the build plan only. No code
> has been written yet. See [`docs/plan/PLAN.md`](docs/plan/PLAN.md).

---

## What it does

| | |
|---|---|
| Market | Indian cash equity, NSE |
| Style | Intraday, MIS, flat before the close |
| Timeframe | 5 minutes, fixed |
| Basket | Five liquid symbols, reviewed weekly |
| Strategies | Three, fixed parameters, one selected per session |
| Capital | A configured allocation, not the whole account |
| Human role | Approve the mandate at 08:45, halt at any time |

### The three strategies

| ID | Definition | Direction |
|---|---|---|
| `stg1_ema_10_20` | EMA(10) crosses EMA(20) | Long only |
| `stg2_supertrend_3_10` | Supertrend, multiplier 3, period 10 | Long and short |
| `stg3_sma10_ema30` | SMA(10) crosses EMA(30) | To be decided |

All three are trend-following. Parameters are fixed by design - the system selects
between strategies, it does not optimise them.

---

## How it is put together

```
                  strategies/          one copy of the signal logic
                  stg1  stg2  stg3     5min, fixed params
                       |     |
        +--------------+     +--------------+
        |                                   |
  BacktestAdapter                   ExecutionAdapter
  whole frame at once               growing frame, per bar close
        |                                   |
  Backtester + vectorbt              Executor state machine
  metrics, comparison                live orders, stops, squareoff
        |                                   |
        +----------> Planner <--------------+
                     select strategy, set risk fraction,
                     LOCK for the session
```

**One signal function, two adapters.** The backtester and the live executor call the
same code. A parity test asserts that feeding the execution adapter one bar at a time
over history produces exactly the same signals as the backtest adapter's vectorised run.
Without that guarantee, the morning selection measures a strategy that never trades.

**vectorbt is a backtesting dependency only.** It never runs during a session.

**The LLM plans, it does not trade.** It reads the regime, writes the plan and its
rationale, and proposes a risk fraction within a bounded band. Every signal, entry, exit
and stop is deterministic code. A model that cannot move a position cannot talk itself
into one.

---

## Safety

Four layers, inherited from the sibling project
[TradingAgent](https://github.com/marketcalls/TradingAgent) and adapted for autonomy.

| Layer | Mechanism | What defeats it |
|---|---|---|
| 1. Instructions | Prompt rules | Any model mistake. Assume it fails |
| 2. Tool scoping | Order tools absent unless enabled | Nothing the model can do |
| 3. Human approval | **Per mandate, pre-session** | Approving without reading |
| 4. RiskGuard | Deterministic checks before the broker | Only a config change |

Layers 2 and 4 are the ones that actually hold. The plan never relies on a layer above
the one below it.

### Controls

- **Daily loss limit as a budget.** Checked before every entry against remaining budget,
  not after the fact. Realized plus unrealized, marked continuously.
- **Consecutive-loss halt.** Pause at two, flat and stop at three.
- **Position cap.** `max_concurrent x risk_per_trade` must not exceed the daily limit.
- **Sector cap and market filter.** Five correlated names are not five independent bets.
- **Reduce-only.** The state between running and killed. Close positions, open nothing.
- **Kill switch.** A file on disk. Takes effect without a restart.
- **Order state machine with idempotency.** Never blindly retry an order. Reconcile
  against the broker first, and halt rather than guess.

### Session clock

| Time | Event |
|---|---|
| 08:45 | Plan: backtest, regime read, select strategy, publish for approval |
| 09:15 | Executor starts, plan locked, observe only |
| 09:30 | Trading window opens |
| 14:45 | No new entries |
| 15:10 | Force flat |
| 15:35 | Reconcile, metrics, journal |

---

## Stack

**Backend** - Python 3.14, FastAPI, SQLite, pandas, `openalgo`, vectorbt, Agno +
LiteLLM for the planner.

**Frontend** - React 19, TypeScript, Vite, Tailwind 4, shadcn/ui.

---

## Relationship to TradingAgent

[TradingAgent](https://github.com/marketcalls/TradingAgent) is a chat assistant that
stops and asks a human to approve every order. AutoAgent trades unattended inside a
mandate approved once before the session.

| | TradingAgent | AutoAgent |
|---|---|---|
| Driver | Human message | Clock |
| Approval | Per order | Per mandate |
| LLM role | Decides and acts through tools | Plans and explains only |
| State | Per chat turn | Persistent across the session |
| Failure mode | Wrong answer | Wrong position |

They are siblings, not versions, and they share libraries rather than a process.
TradingAgent remains the right tool when a human is present and wants to ask questions.

---

## Roadmap

Steps 0-5 produce a research tool with no execution risk. Nothing can trade until step
8, and nothing trades live until step 12. Full detail in
[`docs/plan/PLAN.md`](docs/plan/PLAN.md).

| # | Step |
|---|---|
| 0 | Verify open questions against the installed packages |
| 1 | Scaffolding, config, historical bar store |
| 2 | Strategy module and both adapters - **parity test is the gate** |
| 3 | Portfolio replay backtester |
| 4 | Metrics and strategy comparison |
| 5 | Regime read and selector |
| 6 | Intent log and order state machine |
| 7 | Risk engine |
| 8 | Executor, sandbox only |
| 9 | Planner |
| 10 | Control surface |
| 11 | Paper campaign, 20+ sessions |
| 12 | Live at quarter size |

---

## Disclaimer

This software places real orders with a real broker. It is provided as-is, with no
warranty, and nothing here is investment advice.

Trading is risky and intraday leverage magnifies that risk. The architecture in this
repository is designed to protect capital from software failure. **It cannot protect you
from a strategy that has no edge.** Whether these three strategies are profitable after
costs is an open question the plan itself flags as the largest risk in the project.

Run it in analyzer mode. Measure it. Only then decide.

---

## Licence

MIT. See [LICENSE](LICENSE).
