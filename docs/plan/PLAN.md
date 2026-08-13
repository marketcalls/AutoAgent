# AutoAgent - Autonomous Intraday Equity Trading Agent

Build plan. Version 0.1, drafted 2026-08-13.

Sibling project to `Day09/TradingAgent`. That app is a chat assistant that asks a human
to approve every order. This one trades on its own inside a mandate the human approves
once, before the market opens.

---

## Part 0 - Scope

### What this is

A single autonomous agent that trades Indian equity intraday on a 5-minute timeframe
through OpenAlgo. Each morning it selects one of three fixed strategies, applies it to a
**fixed basket of five liquid symbols**, sizes positions from a risk formula against a
**configured capital allocation**, trades the session unattended, and squares off before
the close.

Reference configuration: 10 lakh allocated out of a larger account, five symbols in the
basket, at most three positions open at once.

The human approves a mandate before the session and can halt it at any time. The human
does not approve individual trades.

### What it is not

- Not a chat interface. TradingAgent already is one, and it is the right tool when a
  human is present.
- Not multi-strategy concurrent. One strategy runs per session, locked at 09:15.
- Not multi-account or multi-user. Single account, single OpenAlgo instance.
- Not options, futures, or overnight. Cash equity, MIS, flat by 15:10.
- Not a parameter optimiser. The three strategies have fixed parameters, deliberately.
- Not a replacement for TradingAgent. They share libraries, not a process.

### The premise being inverted

TradingAgent is organised around one sentence: "every order stops and asks you to
approve it before anything reaches your broker." Its own plan lists autonomous trading
under explicit non-goals.

Removing that gate deletes safety layer 3. The remaining layers become load-bearing:

| Layer | TradingAgent | AutoAgent |
|---|---|---|
| 1. Instructions | Prompt rules | Same. Assume it fails |
| 2. Tool scoping | Order tools absent unless enabled | Same, plus mandate-scoped |
| 3. Human approval | Per order | **Per mandate, pre-session** |
| 4. RiskGuard | Deterministic, post-approval | Same, plus loss budget and breakers |

The design principle carried over unchanged: never rely on a layer above the one below
it. Layer 3 moving from per-trade to per-mandate is the entire architectural change.

### Goal, stated honestly

Maximise return subject to surviving. In practice that means the risk rules are fixed
and the strategy is variable, not the other way round. Position sizing and the loss
budget control drawdown; the strategy controls whether there is an edge at all.

**The architecture protects capital. It does not create edge.** The three strategies
are the only unknown in this plan. Everything else is engineering that can be got right.

---

## Part 1 - Ground truth

Facts proven in TradingAgent that this plan depends on. Where a fact is unverified for
this project it is marked OPEN and carries a verification step in Part 9.

### Proven, reusable as-is

- `openalgo/client.py` - reflective SDK wrapper, uniform `{ok, source, data, error,
  kind, mode}` envelope, errors returned not raised, two token buckets (40/s general,
  8/s order) under the documented 50/10 ceilings.
- **Leading-slash trap.** An endpoint with a leading `/` yields `/api/v1//ping`, a 308
  the SDK does not follow, surfacing as a fake error dict containing HTML. Strip it.
- **`price_type` vs `pricetype`.** Top-level SDK kwarg is `price_type`; inside basket,
  margin, GTT and options-multi legs it is `pricetype`. A wrong spelling in a leg is
  forwarded verbatim, ignored, and the order silently drops to MARKET.
- Request field is `order_id`; response field is `orderid`.
- `history` and `instruments` return a DataFrame on success, a dict on error. Always
  `isinstance` check.
- **NaN poisoning.** `openalgo.ta` uses cumsum-based rolling sums. One NaN at bar 50 of
  300 left `ta.sma(close, 14)` returning 263/300 NaN. `frames.py` cleans at the boundary
  with `to_numeric(coerce) -> dropna -> sort_index -> dedupe`. Non-negotiable.
- Period arguments must be Python `int`. JSON decodes numbers to float, so `int(period)`
  coercion is mandatory before every `ta` call.
- `ta.supertrend(high, low, close, period=10, multiplier=3.0)` - so "Supertrend 3,10"
  means multiplier 3, period 10, which matches the library defaults.
- `ta.vi` and `ta.ulcerindex` are all-NaN in openalgo 2.0.3. Not used here.
- `safety/risk.py` - deterministic, prompt-immune. Kill switch is a file test on
  `data/KILL`, so it takes effect without a restart.
- `safety/audit.py` - two rows per order, attempt before the broker call and result
  after, to daily JSONL plus SQLite. A crash mid-flight still leaves evidence.
- Analyzer mode is a full-fidelity sandbox with 1 crore of simulated capital, and is
  **application-wide** - not per API key.
- MIS orders are rejected by the sandbox after 15:15 IST. Discovered in TradingAgent
  progress doc 007.

### RESOLVED at step 0

Measured against the installed packages and a live OpenAlgo instance (broker zerodha,
analyzer mode) on 2026-08-13 by `scripts/validate_setup.py`. 26 checks passed, 1 failed.

1. **Client order ID passthrough - ANSWERED: there is none.**
   `api.placeorder` accepts only `strategy, symbol, action, exchange, price_type,
   product, quantity, **kwargs`. No `client_order_id`, `tag`, or `correlation_id`. All
   `**kwargs` are `str()`-cast and forwarded, but the server validates its own schema.

   **Consequence, and it shapes Part 6:** the reconciliation key is
   `(strategy, symbol, side, quantity, time window)` matched against the orderbook.
   UNKNOWN-state resolution therefore **cannot be fully automatic**. Where a match is
   ambiguous the executor halts and escalates. The deterministic `intent_id` remains the
   internal key; it simply cannot be pushed to the broker.

   `strategy` is settable per order and is the only attribution tag that survives the
   round trip. Orderbook rows expose `symbol`, `action` and `quantity`, so the composite
   key is viable.

2. **Order-update WebSocket - port reachable** at `ws://127.0.0.1:8765`. Reachability
   only; `subscribe_orders` behaviour is verified at step 6. Until then the executor
   polls the orderbook for fills. The dedupe rule stands if both a broker WS and an
   HTTPS postback are configured: `orderid` + `order_status` + `filled_quantity`.

3. **Intraday history depth - generous, one call covers the lookback.** Measured on
   RELIANCE NSE 5m:

   | Calendar days | Bars | Sessions |
   |---|---|---|
   | 10 | 648 | 9 |
   | 30 | 1,698 | 23 |
   | 90 | 4,698 | 63 |
   | 180 | 9,048 | 121 |

   Roughly 75 bars per session, matching the 375-minute equity session. The 90-session
   long lookback fits in a single request, so the first fill of the bar store is cheap
   and only the newest session needs fetching thereafter.

4. **Sandbox fill realism - still open.** Requires `--with-orders`, which was not run in
   this pass. Deferred to step 8, when the executor exists and can be exercised in the
   sandbox end to end.

5. **Freeze quantity and lot size - available per symbol.** `/symbol` returns
   `lotsize`, `tick_size` and `freeze_qty`. For RELIANCE NSE cash: `lotsize=1`,
   `tick_size=0.1`, `freeze_qty=1`. Read them, never hard-code them. Note `freeze_qty=1`
   on a cash equity looks like "not applicable" rather than a real freeze limit; treat a
   value of 1 on a cash symbol as absent and confirm before relying on it for sizing.

### Also confirmed at step 0

- `ta.supertrend(high, low, close, period=10, multiplier=3.0)` returns a **2-tuple**.
- `ta.adx` returns a **3-tuple** and ADX is element **[2]**, not [0].
- `ta.ema(close, 10.0)` raises `TypeError`. **`int()` coercion is mandatory** before
  every `ta` call, and JSON decodes numbers to float.
- `ta.ema`, `ta.sma`, `ta.atr` all produce finite series on a cleaned live frame.
- Broker `availablecash` was 99,99,984 against an `ALLOCATION` of 10,00,000 - exactly
  the situation that makes the Part 5 allocation rule load-bearing. Sizing against
  broker funds would have produced positions ten times too large.

### OPEN - still unresolved

- **vectorbt is not currently importable.** `ImportError: Numba needs NumPy 2.4 or less.
  Got NumPy 2.5.` Installed: numpy 2.5.2, pandas 3.0.5, vectorbt 1.0.0. This blocks
  steps 3 and 4. vectorbt never runs during a session, so an isolated environment is an
  acceptable resolution. Being resolved separately; the finding is recorded in
  `backend/app/backtest/ENVIRONMENT.md`.
- Sandbox fill realism, item 4 above, deferred to step 8.

### Version pins inherited

`agno==2.8.7`, `litellm==1.96.2`, `openalgo==2.0.3`. Pinned exactly in TradingAgent
because an upgrade to any of them can move the confirmation gate, the provider
workarounds, or the indicator surface. Same policy here.

---

## Part 2 - Architecture

### Process shape

Two processes, one shared library.

```
                  +----------------------------+
                  |    strategies/  (shared)   |   one copy of signal logic
                  |    stg1  stg2  stg3        |   5min, fixed params
                  +------+---------------+-----+
                         |               |
           +-------------+               +-------------+
           |                                           |
   +-------v--------+                          +-------v--------+
   |  Backtester    |                          |   Executor     |
   |  replay + vbt  |                          |  live, on 5m   |
   |  metrics       |                          |  bar close     |
   +-------+--------+                          +-------+--------+
           |  metrics                                  |
           |                                   +-------v--------+
           |      +--------------------+       |  Risk engine   |
           +----->|  Planner (pre-open)|------>|  budget, size, |
                  |  select strategy   | locked|  breakers      |
           +----->|  set risk fraction | plan  +-------+--------+
           |      |  LOCK for session  |               |
   +-------+------+--------------------+       +-------v--------+
   |  Regime read |                            |   OpenAlgo     |
   |  ADX / chop  |                            +----------------+
   +--------------+
                           +----------------+
                           |  Intent log    |  <- reconciliation,
                           |  (SQLite)      |     audit, and P&L
                           +----------------+
```

**Planner** runs once, pre-open, and exits. **Executor** runs the session and exits at
squareoff. Neither is long-lived across days, which makes crash recovery a first-class
path rather than an afterthought.

### Why strategies sit above both

If the executor holds its own copy of the EMA crossover logic, the morning selection is
evaluating a strategy that is not the one that trades. That drift is silent, and it is
the single most common way this architecture fails.

One module. Two consumers. The backtester calls it over history; the executor calls it
over the live frame. Identical inputs must produce identical outputs.

### Where the LLM sits

Not in the trigger path.

| LLM does | Deterministic code does |
|---|---|
| Pre-open: read regime, write the plan and its rationale | Every signal, entry, exit, stop |
| Pre-open: propose risk fraction within a bounded band | Position sizing arithmetic |
| Post-close: journal, flag anomalies | All risk checks and circuit breakers |
| On halt: explain what happened | Square-off and reconciliation |

Reasons, in order of weight:
1. **Backtestability.** A non-deterministic trigger cannot be replayed, so the morning
   selection would be measuring something other than what runs.
2. **Speed.** A 5-minute bar close leaves seconds, not tens of seconds.
3. **Rationalisation.** A model asked "should I take this trade" will find reasons. A
   model asked "explain what the rules did" cannot move the position.
4. **Provider risk.** If the model is unavailable at 09:00, the session should not run
   at all - a clean, safe failure. If it were in the trigger path, an outage mid-session
   would leave open positions with no decision-maker.

### Technology stack

Same stack as TradingAgent at its current version, plus vectorbt and shadcn/ui.

**Backend**

| Layer | Choice | Note |
|---|---|---|
| Runtime | Python 3.14 | Same as TradingAgent |
| API | FastAPI + uvicorn | Port 8090, so both apps can run side by side |
| Streaming | SSE, `type` field inside the JSON payload | Same contract as TradingAgent - no `event:` line, so the client uses `fetch` + `getReader()` |
| Store | SQLite | Intent log, order audit, historical bars, session metrics |
| Data | pandas, numpy | |
| Broker | `openalgo==2.0.3` | Pinned |
| Indicators | `openalgo.ta` | Rust-backed, already fast |
| Backtest | vectorbt | Metrics and comparison only - see Part 4 |
| LLM | `agno==2.8.7` + `litellm==1.96.2` | Planner only, never in the execution path |

**Frontend**

| Layer | Choice | Note |
|---|---|---|
| Framework | React 19 | |
| Language | TypeScript 7 | `strict`, plus `noUnusedLocals` / `noUnusedParameters` |
| Build | Vite 8 | `/api` proxied to 8090, so all fetches are bare relative paths |
| Styling | Tailwind 4 via `@tailwindcss/vite` | CSS-first. Theme in an `@theme` block in `index.css`. No `tailwind.config.js`, no `postcss.config.js` |
| Components | **shadcn/ui** | New relative to TradingAgent |
| Icons | lucide-react | |
| Charts | Recharts or lightweight-charts | Equity curve, drawdown, price with signals |

**Note on shadcn.** TradingAgent already carries `clsx` and `tailwind-merge` as
dependencies and defines the `cn()` helper in `lib/format.ts` - shadcn's own convention -
but never installed shadcn itself. Those dependencies were leftovers from the
reference app. Adopting shadcn here makes them load-bearing rather than vestigial.

shadcn components land in `src/components/ui/` and are owned source, not a dependency.
Application components sit above them in `src/components/`. Tailwind 4 is supported by
current shadcn; the theme tokens live in the same `@theme` block as everything else.

Two conventions carried from TradingAgent that shadcn must not override:
- Theme is class-based on `&lt;html&gt;` with an anti-FOUC script before first paint, not
  `prefers-color-scheme` alone.
- Colour tokens are semantic (`danger`, `success`, `warn` with `-soft` and `-border`
  variants), and every use goes through a token. A component referencing a token that
  does not exist renders invisibly, which has already happened once in TradingAgent
  (`ThinkingSelector.tsx:11-14`).

**Deliberately not used:** no state library, no router until there is more than one
page, no react-query. TradingAgent runs its entire UI on component state and a single
container, and this app has less UI than that one.

### Reused from TradingAgent

| Component | Change needed |
|---|---|
| `openalgo/client.py` | None |
| `openalgo/normalize.py` | None |
| `openalgo/constants.py` | None |
| `openalgo/frames.py` | Split live cache from historical store (Part 7) |
| `safety/risk.py` | Add loss budget, breakers, reduce-only |
| `safety/audit.py` | Keep as order-level audit; add trade-level intent log above it |
| `indicators/` | Reused by strategies; registry not needed at runtime |
| `config.py` | Extend with mandate and schedule settings |
| `tools/` (read-only) | Reused by the Planner LLM only |

### Rejected alternatives

**Extend TradingAgent in place.** Rejected. The chat app is request-driven and
single-turn; this is clock-driven and stateful. Sharing a process would couple a
safety-critical execution loop to an interactive UI, and TradingAgent's whole value is
that its order path is small and auditable.

**LLM chooses the trade.** Rejected. See above.

**Agno agent as the executor.** Rejected. Agno is the right frame for the Planner, which
is a reasoning task with tools. The executor is a state machine on a timer and gains
nothing from an agent framework.

**Run all three strategies at one third size.** Genuinely arguable and not fully
rejected - diversification usually beats selection. Kept as a fallback in Part 11 if
selection proves to have no signal.

---

## Part 3 - Strategy layer

### The three strategies

| ID | Definition | Direction | Native stop |
|---|---|---|---|
| `stg1_ema_10_20` | EMA(10) crosses EMA(20) | Long only | slow EMA, or 1.5 x ATR(14), wider of the two |
| `stg2_supertrend_3_10` | Supertrend, multiplier 3, period 10 | Long and short | the supertrend line |
| `stg3_sma10_ema30` | SMA(10) crosses EMA(30) | **OPEN - see below** | 1.5 x ATR(14) |

**OPEN:** direction for `stg3` is unspecified. Long-only and long-and-short produce
materially different statistics and sizing. Decide before the first backtest.

All three are trend-following. They differ in speed and direction, not in kind, and
they will lose in the same chop. Part 4 accounts for this.

### Contract every strategy implements

```
signals(frame, params) -> DataFrame indexed like frame, columns:
    long_entry   bool
    long_exit    bool
    short_entry  bool     (always False for long-only strategies)
    short_exit   bool
    stop_price   float    native stop level for the current bar
```

Rules the contract enforces:

- **Signal on bar close, entry on next bar open.** Never enter on the bar that produced
  the signal. This is lookahead bias and it is the single most common reason a backtest
  looks good and live does not. Signal at 09:20 close, entry at 09:25 open.
- **No forward references.** A signal at bar `i` may read bars `0..i` only.
- **Stateless.** The function is pure. Position state lives in the executor, not here.
- **Warm-up carried across sessions.** EMA(30) on 5-minute bars needs roughly 30 bars.
  A session has 75. Restarting the series each morning leaves the indicator invalid
  until around 11:45. Feed a continuous multi-day frame, unbroken.
- **`int()` coercion** on every period before calling `ta`.

### Two adapters, one signal function

**vectorbt is a backtesting tool only. It never runs during a session.** Every strategy
therefore needs a live execution path as well - and that path must produce the same
signals the backtest measured, or the morning selection is meaningless.

The answer is one signal function with two thin adapters around it.

```
                    strategies/stg2_supertrend.py
                    signals(frame, params) -> DataFrame
                              |
              +---------------+---------------+
              |                               |
    BacktestAdapter                   ExecutionAdapter
    whole frame at once               growing frame, per bar close
    -> boolean arrays                 -> signal for the LAST bar only
    -> vectorbt / replay              -> executor state machine
```

| | Backtest adapter | Execution adapter |
|---|---|---|
| Input | Full historical frame, all sessions | Live frame, current session plus warm-up tail |
| Called | Once per strategy per backtest run | Once per 5-minute bar close |
| Returns | Full signal series | Signal for the most recent closed bar |
| Consumer | Replay loop, then vectorbt for stats | State machine (Part 6) |
| Fills | Simulated, next bar open, modelled costs | Real, LIMIT at touch with SL-M stop |

Neither adapter contains strategy logic. They shape input and output only. If a rule
ever needs to differ between backtest and live, that is a signal the design is wrong,
not a reason to fork the function.

### The parity test - a hard build gate

The guarantee is not architectural intent, it is a test.

> Run the **execution adapter** bar by bar over a historical frame, feeding it one bar
> at a time as if live. Assert the resulting signal series is **identical** to the
> **backtest adapter's** vectorised run over the same frame.

Any difference means the live path and the measured path have diverged, and the
divergence is almost always one of three things:

1. Vectorised code peeking at a future bar that the incremental path cannot see.
2. Warm-up differences - the incremental path starting with fewer bars.
3. Floating point drift from a different accumulation order, particularly in EMA.

The test runs per strategy, in CI, and is a gate on build step 3. Nothing downstream is
trustworthy without it.

### Execution-side responsibilities the backtest does not have

The adapters are symmetric; the surrounding systems are not. The execution path owns
several things the backtester models rather than performs:

- Partial fills and rejects
- Order timeouts and reconciliation (Part 6)
- Placing and maintaining the SL-M stop at the broker
- Squareoff at `squareoff_time` regardless of signal state
- Data staleness detection (Part 7)
- Risk budget and breaker checks before every entry (Part 5)

The backtester must **model** each of these or its metrics overstate reality. The most
commonly missed are costs and the EOD flat.

### Signal hygiene

Raw crossover signals fire repeatedly while a condition holds. Use `openalgo.ta.exrem`
to keep only the first signal in a run, and `flip` for position-state tracking. Both are
documented in the reference under `openalgo-prompt/indicators/`.

Because both adapters call the same function, this hygiene applies identically to
backtest and live - which is exactly the point.

---

## Part 4 - Backtest and daily selection

### The selection problem, stated honestly

Selecting whichever strategy performed best recently is performance chasing. You will
systematically pick the one that just got lucky and is about to mean-revert. It is a
well-documented way to underperform all three.

Because all three strategies are trend-following, "which one" is also a weaker question
than "should I trade at all today."

### Selection rule

Primary axis is regime, measured pre-open. Backtest metrics act as a **sanity filter**,
not the selector.

| Regime | Selection |
|---|---|
| Strong trend, upward bias | `stg1_ema_10_20` - fast, long only |
| Strong trend, either direction | `stg2_supertrend_3_10` - bidirectional |
| Weak or mixed | `stg3_sma10_ema30` - slowest, fewest whipsaws |
| Choppy (ADX below floor) | **Trade nothing** |

Sanity filter: any strategy whose recent metrics are catastrophically broken - negative
expectancy over both lookback windows, or max drawdown beyond a threshold - is excluded
regardless of what the regime says. If the regime pick is excluded, trade nothing.

Two rules that matter more than the table:

- **Hysteresis.** Do not switch unless the new pick beats the incumbent by a defined
  margin. Otherwise the system flips on noise and you own the average of three
  strategies with extra turnover.
- **Lock.** Once selected at 09:15, frozen for the session. No mid-day switching, ever.

### Metrics computed per strategy

Two lookback windows, because their disagreement is itself information:

- **Long:** 60-90 sessions. Stability.
- **Short:** 10-20 sessions. Recency.

Per window: expectancy in R, win rate, profit factor, max drawdown, Sharpe, trade count,
average bars held, and net-of-cost P&L. Trade count matters - a strategy with four
trades in the short window has no measurable statistics and should defer to the long
window.

### Where vectorbt fits

**Signals come from `strategies/`. vectorbt consumes them.** It is used for portfolio
simulation and the statistics suite, never for signal generation.

Scope, stated as a rule: **vectorbt is a research and pre-open dependency. It is never
imported by the executor and never runs during a session.** The live path is the
execution adapter plus the state machine in Part 6. See Part 3 for how the same strategy
serves both.

Three configuration requirements, each of which silently corrupts results if missed:

1. **Shift signals by one bar** or vectorbt fills on the signal bar's close, which is
   lookahead bias.
2. **Configure fees and slippage.** Indian intraday equity costs are material relative
   to a 1.5R target. Unmodelled costs make every strategy look profitable.
3. **Model the hard 15:10 square-off.** `sl_stop` covers the stop; end-of-day flat needs
   injected exit signals. This is the awkward part and the reason the replay loop below
   exists.

### Replay loop vs vectorbt - the decision

A faithful replay loop is needed regardless, because live behaviour includes next-bar
entry, SL-M stops, partial fills and a hard EOD flat. Once that loop exists it produces
a trade list, and metrics from a trade list are straightforward.

**Decision:** own replay produces the trade list and the fills. vectorbt and the
`strategy-compare` skill produce the comparison table and the tearsheet. If the two ever
disagree, the replay wins, because it is the one that matches live.

vectorbt earns its keep offline, for research: "is 10/20 defensible at all across 200
stocks and three years." Not in the daily path.

### Keep `optimize` out of the daily loop

Fixed parameters are an advantage. There is no parameter overfitting in this design,
only selection bias, which hysteresis handles. Sweeping parameters every morning
reintroduces the exact problem the design currently avoids. Use `optimize` rarely, and
freeze what it tells you.

### The basket

Five fixed liquid symbols, reviewed weekly rather than daily. Selected from the F&O
stock list as a liquidity proxy, filtered on turnover, ATR percentage inside a band, and
price range.

Five is chosen deliberately. Fewer than three and there are too few trades per week to
measure anything. More than about eight and the daily budget refuses most signals
anyway, so the extra names only add data cost.

### Selection scope

Selection is **universe-level**, not per-symbol. One strategy applied to all five.

Per-symbol selection multiplies the decision surface by five on samples that are already
short, and gets noisy fast. Revisit only if universe-level selection proves to have
signal.

### The backtest must be portfolio-level

**Backtesting each symbol independently and summing the results overstates them.** The
independent-and-sum approach silently ignores:

- The shared risk budget - some of those trades would never have been taken
- The concurrent position cap
- Shared margin
- Correlated drawdowns landing on the same sessions
- The tie-break when several symbols signal on the same bar

The replay loop therefore runs **all five symbols on one timeline**, with one shared
budget, one position cap, and the Part 5 priority rule applied at every bar.

This is where vectorbt genuinely earns its place: multi-asset portfolio simulation with
shared cash is its actual strength, and 2D signal arrays are its native input. The
budget and breaker logic stays custom in the replay - vectorbt does not know about
consecutive-loss halts - but the portfolio mechanics, equity curve and statistics are
exactly what it is good at.

A useful diagnostic to record alongside the headline metrics: **how many signals were
refused for lack of budget.** A high refusal rate means the basket is too large for the
allocation, or the risk fraction is too high, and the measured strategy is not the one
that would trade.

---

## Part 5 - Risk and safety

### The allocation is the whole world

The agent trades a **configured allocation**, not the account balance. If the account
holds 1 crore and the allocation is 10 lakh, every number in this Part derives from
10 lakh.

```
ALLOCATION = 10,00,000        configured, fixed, never read from the broker
```

**This is a trap in the ported code.** TradingAgent's affordability check in
`safety/risk.py` compares required margin against `available_cash x
MAX_ORDER_PCT_OF_FUNDS`, where `available_cash` comes from the broker `funds` call.
Ported unchanged against a 1 crore account, it would approve positions ten times the
intended size. The check must become:

```
usable        = ALLOCATION - realised_loss_today
affordability = required_margin <= usable x pct
```

The broker `funds` call keeps exactly one job: confirming enough real margin exists to
place the order at all. **It never sets the size.**

Pre-open assertion: broker `availablecash` must be at least `ALLOCATION`, or the session
does not start.

**Fixed, not compounding.** The allocation is reviewed manually, monthly. Compounding it
off P&L grows risk during a winning streak, which is precisely when a strategy is most
likely to stop working.

Two consequences of sharing an account:

- **Margin is shared even though the allocation is not.** MIS margin comes from the same
  pool. If anything else runs on the account, the affordability check can pass while
  real margin is unavailable. Reserve it, or monitor a floor.
- **Position attribution is not clean.** The broker positionbook nets everything on the
  account. Order-level attribution comes free from the `strategy` field; position-level
  requires the intent log (Appendix B). Simplest mitigation: keep the basket off your
  manual watchlist.

### Sizing

Fixed risk per trade. Everything follows from it.

```
risk_amount   = ALLOCATION x risk_fraction
stop_distance = strategy native stop (Part 3)
quantity      = risk_amount / stop_distance
```

Volatility therefore sets the size automatically: a wider stop buys fewer shares.

Worked example at the reference allocation:

| Setting | Value |
|---|---|
| Allocation | 10,00,000 |
| Risk per trade, 0.5% | 5,000 |
| Daily loss limit, 2% | 20,000 |
| Max concurrent, 3 | 15,000 at risk = 1.5% |
| Realistic worst day | 21,000 - 23,000 including slippage |

A 5,000 risk budget against an ATR-based stop on a liquid 1,500 rupee stock is roughly
2-3 lakh of notional per position, which MIS leverage covers comfortably inside the
allocation.

**Allocation sizing guidance.** Much below 5 lakh and risk per trade gets small enough
that brokerage becomes a material fraction of every win. Much above 50 lakh and a
5-symbol mid-cap basket starts moving on your own orders.

### What the LLM may decide about size

The Planner proposes a **risk fraction inside a bounded band**, and may only scale down.

```
risk_fraction = base x quality_multiplier
quality_multiplier clamped to [0.25, 1.0]
base = mandate ceiling, e.g. 0.5% per trade
```

It can never exceed the mandate ceiling regardless of what the metrics say. If the
Planner returns nothing, or returns something out of range, the system uses the floor,
not the ceiling.

If a formal sizing rule is wanted, use quarter-Kelly at most. Kelly on a few hundred
intraday trades overestimates badly, and the failure mode is a large position taken
immediately before the strategy stops working.

### Daily loss limit as a budget, not a tripwire

The naive form asks "have I lost 2% yet." The correct form asks, before every entry:

```
remaining_budget = daily_limit - current_MTM_loss
if proposed_trade_max_loss > remaining_budget:
    skip, or size down to fit
```

At -1.8% against a 2% limit, a trade risking 0.5% would reach -2.3% in the worst case.
The budget check refuses it. The tripwire check permits it and then fires afterwards.

**Loss means realized plus unrealized, marked continuously.** Realized-only means you
can be deeply underwater on open positions while the limit never fires.

**Measured against ALLOCATION, fixed.** Not current equity, and not account funds. If
the limit floats with equity it shrinks as you lose; if it floats with the account it is
ten times too generous.

### The budget is also the position limit

The constraint that binds a multi-symbol basket:

```
max_concurrent x risk_per_trade  <=  daily_loss_limit
```

**Compare it with a tolerance.** Measured at step 1: `4 * 0.005` evaluates to
`0.020000000000000004` in binary floating point, which is arithmetically *at* a 2%
limit but numerically above it. Without a tolerance the config would reject a
perfectly valid `MAX_CONCURRENT_POSITIONS=4` as an arithmetic accident. `1e-9` is
applied here and to the floor-versus-base check.

On the reference allocation:

| Concurrent | At risk | Against a 2% limit |
|---|---|---|
| 5 | 25,000 = 2.5% | Breaks the limit |
| 4 | 20,000 = 2.0% | Exactly at it, no slippage headroom |
| 3 | 15,000 = 1.5% | Works, 0.5% headroom |

**No separate rule is needed.** The budget check already refuses the fourth or fifth
signal because its worst-case loss exceeds remaining budget. `MAX_CONCURRENT_POSITIONS`
exists as a second, cheaper guard that fires before any market call.

The consequence for Part 4's basket: five symbols is a **candidate pool, not a mandate
to hold five.** The system selects among whichever signals fire, and refuses the rest.

### Correlation - five names are not five bets

Five Indian equity names, intraday, traded by a trend-following strategy, are heavily
correlated. When NIFTY falls, every long position loses together. Effective
diversification is closer to **1.5 to 2 independent bets, not 5**, and the sum of five
independent risk budgets therefore understates the real drawdown.

Three mitigations, cheapest first:

| Control | Rule |
|---|---|
| Sector cap | Maximum 2 concurrent positions per sector. Two banks is one bet |
| Market filter | No new longs while NIFTY is below its own trend filter; no new shorts while above |
| Net vs gross exposure | Track and cap both |

The net/gross distinction matters because `stg2_supertrend_3_10` is bidirectional. Short
on two names and long on three is partially hedged - lower risk and lower expected
return than three longs. A limit on gross exposure alone will not see this.

### Simultaneous signals

All five bars close at the same instant. If three fire at 09:35 and the budget allows
two, the tie-break must be deterministic.

| Rule | Verdict |
|---|---|
| Fixed priority order | **Chosen.** Deterministic, backtestable, cannot become a fitted parameter |
| Liquidity rank | Reasonable, defer |
| Signal strength | Rejected for now - needs a per-strategy definition, which is a new parameter to justify |

Entries are **sequenced, not fired together**. Each fill consumes margin and budget, so
size order N+1 after order N fills, not before. Five orders is well inside the 8/second
client limiter, so throughput is not the constraint - correctness of the running budget
is.

### Graduated response

| Trigger | Response |
|---|---|
| 50% of daily budget consumed | Halve the risk fraction |
| 2 consecutive losses | Pause 30 minutes, require a fresh setup |
| 3 consecutive losses | Flat, halt for the session |
| Daily budget exhausted | Flat, halt for the session |
| Max trades per day reached | No new entries, manage existing |

Counting rules:

- A loss is **net of costs**. A trade up 50 rupees gross that paid 80 in brokerage is a
  loss.
- Define a scratch band. Anything within +/- 0.1R counts as neither win nor loss.
  Without it, noise resets the streak counter constantly.
- The streak resets on a genuine win. The daily budget resets at the **session
  boundary**, not midnight.

### Halt semantics

Order matters:

1. Cancel all pending orders
2. Flatten open positions at market
3. Set halt state
4. Notify
5. **Halt is persisted and sticky.** A process restart must come back halted, not fresh.

Set the limit with headroom. Flattening at market costs slippage, so a 2% limit
realistically stops around 2.1-2.3%.

### Reduce-only

The state between running and killed. Entered on mandate expiry, breaker trip, data
degradation, or manual request. In reduce-only the executor may close positions and
modify stops, and may not open anything new.

Hard revocation while a position is open traps that position. Reduce-only is the answer
to every "what if" in Part 11.

### Kill switch

Inherited from TradingAgent: `data/KILL` file existence, checked before every order, no
restart required. Extended here to also drive the executor into reduce-only and then
flat.

### Pre-trade controls carried over

From `safety/risk.py`, unchanged in intent: exchange allowlist, product allowlist,
symbol denylist, index-not-tradable, quantity bounds, notional caps, fat-finger price
deviation (20% from LTP), duplicate suppression window, and per-session order cap.

Three changes required while porting:

1. **Affordability sizes against `ALLOCATION`, not broker `available_cash`.** See the
   top of this Part. This is the change that must not be missed.
2. `check_basket` compares combined notional against `max_order_value` without the
   `if cap and ...` guard used elsewhere, so at the shipped default of `0.0` any basket
   is refused. Carry the fix even though baskets are not used here today.
3. `RateLimiter.acquire` sleeps while holding the lock, serialising all threads under
   contention.

---

## Part 6 - Execution and state machine

### One machine per symbol, one budget for all

The executor holds **five independent state machines keyed by symbol**, sharing a single
set of session-level resources.

| Per symbol | Shared across the basket |
|---|---|
| State machine instance | Risk budget and MTM |
| Intent record | Breaker set and halt state |
| Position, stop, orders | Reconciliation pass |
| Signal evaluation | Squareoff sequence |
| | Concurrent position count |

Two rules follow:

- **A symbol's machine may only act after the shared budget approves it.** The machines
  do not compete; they queue against one budget in the Part 5 priority order.
- **Halt is global, not per symbol.** A breaker trip drives every machine to flat, not
  just the one that lost.

The intent log is already keyed by symbol, so this costs no schema change.

### States

Each symbol's machine runs this independently.

```
FLAT
 +- SIGNAL                 setup valid on bar close
     +- PENDING_ENTRY      order sent, no confirmation      <- danger
         +- REJECTED           -> FLAT
         +- PARTIAL            -> keep or cancel remainder
         +- UNKNOWN            -> reconcile, never blind retry
         +- FILLED
             +- OPEN_UNPROTECTED   filled, stop not live    <- emergency
                 +- OPEN           stop confirmed at broker
                     +- PENDING_EXIT
                         +- FLAT
HALTED / REDUCE_ONLY       reachable from any state
```

### The two states that matter

**PENDING_ENTRY with no response.** You do not know whether the order reached the
broker. The rule is absolute: **never blindly retry - always reconcile first.** A
duplicate intraday position costs more than a missed trade.

**OPEN_UNPROTECTED.** Filled, but the stop did not place. The position is naked. Retry
the stop immediately; if it fails again, exit at market. Never wait in this state.

### Idempotency

```
1. Derive intent_id deterministically from
   (strategy_id, symbol, signal_bar_timestamp, direction)
   - the same signal can never produce two different ids
2. Write the intent to durable store BEFORE sending
3. Send
4. Write the result after
5. On restart or timeout: any intent without a confirmed result
   -> query the broker, match, resolve. Do not resend.
```

**Depends on OPEN item 1** in Part 1. If OpenAlgo passes a client order id through to
the broker, resolution is exact. If not, the reconciliation key becomes
`(strategy, symbol, side, quantity, time window)` matched against the orderbook, which
is weaker. **Where matching is ambiguous, halt and escalate. Do not guess.**

RiskGuard's existing 10-second duplicate fingerprint is a useful second layer, though
note it stringifies quantity, so `10` and `10.0` fingerprint differently.

### Timeouts

Every state has a maximum dwell time.

| State | Timeout | Action |
|---|---|---|
| PENDING_ENTRY | 30s | Cancel, reconcile |
| PENDING_EXIT | 15s | Escalate to market |
| OPEN_UNPROTECTED | 5s | Retry stop, then exit at market |

At squareoff time, anything not FLAT is driven to FLAT.

### Reconciliation

On every wake, before any decision: pull orderbook, positionbook and tradebook, compare
against persisted state, resolve. **If they cannot be reconciled, halt.** Do not guess,
and do not trade around an unexplained position.

One pass covers all five symbols. A position in a symbol the agent does not believe it
holds is an unreconciled state even if the other four match - because the account is
shared with your manual trading, and the positionbook nets everything. This is why the
basket should stay off your manual watchlist.

### Order mechanics

- MIS, cash equity, NSE
- Entry: LIMIT at touch, reprice once, then skip. Never chase with MARKET.
- Stop: SL-M, placed immediately on fill
- Exit at squareoff: MARKET. Do not rely on broker auto-squareoff timing
- One position per symbol, ever
- `strategy` field set per strategy id - this is the attribution key through the
  broker's own orderbook and tradebook

### Squareoff across the basket

At `squareoff_time`, every machine not already FLAT is driven to FLAT. This is
**sequenced and confirmed, never fire-and-forget**:

1. Cancel every resting entry order across all symbols
2. Cancel every SL-M stop, so a stop and a market exit cannot both fill
3. Send market exits, one symbol at a time
4. Confirm flat against the positionbook
5. Retry any symbol still showing a position; escalate and notify if a retry fails

Step 2 matters more than it looks. Sending a market exit while the stop is still live at
the broker can fill both and leave a reversed position - short when you were long. Cancel
first, then exit.

### Session clock

| Time | Event |
|---|---|
| 08:45 | Planner: holiday and timings check, funds, confirm flat, backtests, regime, select, publish plan |
| 09:15 | Executor starts. Plan LOCKED. Observe only |
| `start_time` (default 09:30) | Trading window opens |
| `end_time` (default 14:45) | No new entries |
| `squareoff_time` (default 15:10) | Force flat, MARKET |
| 15:35 | Reconcile, compute metrics, journal, push report |

15:10 also keeps clear of the 15:15 MIS cutoff found in TradingAgent progress doc 007.

Note the first 5-minute bar closes at 09:20, so no signal can exist before then, and
`start_time` cannot be earlier.

---

## Part 7 - Data layer

### Two stores, different lifetimes

TradingAgent's `frames.py` has a 60-second TTL in-memory cache. Correct for live quotes,
wrong for backtest bars.

| Store | Contents | Lifetime |
|---|---|---|
| Live frame cache | Current session 5m bars | 60s TTL, in-memory, inherited unchanged |
| Historical bar store | Completed sessions, 5m | Persistent on disk, immutable |

Historical 5-minute bars do not change. Fetch only the new session each morning and
append. Otherwise the Planner re-pulls 90 sessions across the watchlist through a
rate-limited API every day at 08:45.

### Cleaning

Unchanged from `frames.py` and non-negotiable:
`to_numeric(errors="coerce") -> dropna(subset=OHLC) -> sort_index() -> dedupe index`.

### Data integrity checks

The executor halts, rather than trading, when:

- Last bar is older than expected for the wall clock (stale feed)
- A bar is missing from the expected sequence
- A price moves beyond a sanity band between consecutive bars (bad tick)
- The broker error rate crosses a threshold

### Universe

**Five fixed symbols**, reviewed weekly, not rebuilt daily. Liquid only - start from the
F&O stock list as a liquidity proxy, then filter on turnover, ATR percentage inside a
band (movement, not chaos), and price range.

Weekly review rather than daily is deliberate. A basket that changes every morning makes
the backtest describe a different portfolio each session, and the metrics stop being
comparable across days. Exclude a name for a known event on the day; otherwise leave the
basket alone between reviews.

Sector labels are needed for the Part 5 sector cap, so the basket definition carries a
sector per symbol.

Historical bars are stored for all five plus NIFTY, which the market filter needs.

---

## Part 8 - Control surface

Minimal. This is not a chat app.

### Mandate approval, pre-session

The screen a human sees at 08:45. It must show **server-computed consequences**, not
the plan's own prose - the same principle as TradingAgent's confirmation card:
*"The model does not produce these numbers; the backend does."*

- Selected strategy, and why (regime read plus metrics table)
- Watchlist
- Risk fraction and resulting worst-case daily loss in rupees
- Max concurrent positions and total capital at risk
- Start, end and squareoff times
- Approve / Reject / Approve-in-paper

### Live view, during session

Position table, current MTM, budget consumed as a fraction of the daily limit, breaker
status, state machine state per symbol, and a large halt button.

### Post-session

Trade list, metrics, equity curve, and the LLM journal. Every trade links to its intent
record, its mandate version, and the audit rows for its orders.

### Notification

Push, not pull. Nobody is watching a screen. Telegram and WhatsApp send endpoints
already exist in the OpenAlgo surface and are wrapped in TradingAgent's `system.py`.

Push on: plan published, session start, halt of any kind, breaker trip, unreconciled
state, and end-of-day summary.

---

## Part 9 - Build order

Each step ends in something runnable and verified against live data or the sandbox.

| # | Step | Deliverable | Gate |
|---|---|---|---|
| 0 | Verify OPEN items | Findings written into Part 1 | Client order id question answered |
| 1 | Scaffolding | Config, logging, `.env`, historical bar store | Settings print, no secrets leaked |
| 2 | Strategy module + both adapters | Three strategies against the Part 3 contract, backtest and execution adapters | **Parity test passes for all three.** Signal counts sane, no lookahead |
| 3 | Portfolio replay backtester | All five symbols on one timeline, shared budget, position cap, costs, EOD flat | Replay and `strategy-compare` agree within tolerance; budget-refusal rate recorded |
| 4 | Metrics and comparison | Two-window metrics table per strategy | Table for all three on one liquid name |
| 5 | Regime read and selector | Deterministic selection with hysteresis | Selection stable across a month of history |
| 6 | Intent log and state machine | Full lifecycle, persisted, reconciliation | Kill mid-order, restart, reconcile clean |
| 7 | Risk engine | Budget, breakers, sizing, reduce-only | Unit test per breaker; halt is sticky |
| 8 | Executor | Live loop on 5m close, sandbox only | Full session in analyzer mode, flat at 15:10 |
| 9 | Planner | LLM regime read, plan, rationale, risk fraction | Plan produced without model access fails safe |
| 10 | Control surface | Mandate approval, live view, notifications | Halt from UI verified mid-session |
| 11 | Paper campaign | 20+ sessions in analyzer mode | Metrics recorded, no unreconciled states |
| 12 | Live at quarter size | One strategy, reduced risk fraction | Manual supervision for the first week |

Steps 0-5 produce a research tool with no execution risk. Nothing can trade until step
8, and nothing trades live until step 12.

**Two gates carry the whole plan.**

**Step 2 - the parity test.** If the execution adapter and the backtest adapter disagree
on a single bar, everything measured afterwards describes a strategy that will not
trade. Fix it there, not later.

**Step 3 - replay against vectorbt.** If the two disagree on the trade list, find out
why before building anything on top of either. The replay wins by definition, because
it is the one that mirrors live, but a disagreement usually means the replay has a bug
too.

---

## Part 10 - Conventions

Carried from TradingAgent, which enforces them by review rather than tooling.

- **No icons or emoji** in code, comments, logs, or model instructions.
- **ASCII-safe logging.** Reconfigure stdout to UTF-8 with `errors="replace"`; cp1252
  consoles raise on the rupee sign.
- **Module docstrings record hard-won runtime facts.** The `price_type` / `pricetype`
  split, the int-coercion rule, the leading-slash 308. These are the notes that stop a
  future edit from silently reintroducing a bug.
- **Comments explain why, not what.**
- Type hints and Google-style docstrings on anything the LLM calls as a tool.
- Secrets only in `.env`; `.env` gitignored, `.env.example` committed.
- Version in `version.py` as the single source of truth.
- Every tool result returns non-empty JSON, capped at 12,000 characters.
- `agno`, `litellm` and `openalgo` pinned exactly; everything else floats.
- **Measured numbers, not assumed ones.** Where this plan states a threshold, the
  implementation should record the measurement that justified it.

---

## Part 11 - Risks and open questions

### Risks ranked by cost

1. **No edge.** The three strategies may have no positive expectancy after costs on
   Indian intraday equity. This is the largest risk and the architecture cannot mitigate
   it. Mitigation: step 4 answers it before anything is built on top.
2. **Selection has no signal.** The three strategies may be close enough that daily
   selection is noise. Mitigation: measure it at step 5. If confirmed, fall back to
   running the single best strategy, or all three at one third size.
3. **Backtest-live divergence.** The failure mode where the morning selection measures
   one thing and the executor trades another. Mitigated by the shared signal function,
   the two-adapter design, the **parity test at step 2**, next-bar entry, modelled
   costs, and the step 3 gate. This is the risk the architecture is most directly
   shaped around.
4. **Duplicate orders on an ambiguous UNKNOWN.** Mitigated by the intent log, the
   deterministic intent id, and halt-on-ambiguity. Severity depends on OPEN item 1.
5. **Correlated drawdown across the basket.** Five names losing together turns three
   independent 0.5% risks into something closer to a single 1.5% risk. Mitigated by the
   sector cap, the NIFTY market filter, and net/gross exposure limits - but not
   eliminated. The portfolio-level backtest at step 3 is what measures the true
   drawdown; per-symbol backtests would hide it.
6. **Allocation leak.** The affordability check sizing against broker funds instead of
   `ALLOCATION` would produce positions ten times too large on a 1 crore account, and
   would do so silently. Mitigated by an explicit test at step 7 asserting that sizing
   is unchanged when broker funds are varied.
7. **Analyzer mode is application-wide.** Paper and live cannot run at once on one
   OpenAlgo instance. Constrains step 11 and step 12 overlap.
8. **Sandbox is not the market.** Paper results overstate fills. OPEN item 4 bounds how
   much the paper campaign proves.
9. **Model provider outage at 08:45.** Handled: no plan means no session. Explicitly a
   safe failure.
10. **Slippage on halt.** Flattening at market during a breaker trip costs more than the
    limit implies. Handled by headroom.

### Open questions

- Direction for `stg3_sma10_ema30`.
- Regime metric and its threshold. ADX is the obvious candidate; the floor below which
  the system trades nothing must be measured, not guessed.
- Hysteresis margin for switching.
- Which five symbols, and their priority order for the tie-break.
- Gross and net exposure caps.
- Whether partial fills are kept or cancelled.
- Retention policy for the intent log and historical bar store.

Resolved since v0.1 of this plan:

- Basket is **fixed at five, reviewed weekly**, not rebuilt daily (Part 7).
- A market-wide NIFTY trend filter **is** included, as the cheapest correlation
  mitigation (Part 5).
- Capital is a **configured allocation**, never the account balance (Part 5).

### Explicitly deferred

- Multiple concurrent strategies
- Multi-account and multi-user
- Options and futures
- Overnight positions
- Execution algorithms (TWAP, VWAP, POV)
- Transaction cost analysis beyond per-trade slippage
- Self-trade prevention and order-to-trade ratio monitoring - relevant at size, not at
  one strategy on 15 names

---

## Appendix A - Configuration surface

```
# Schedule
START_TIME=09:30
END_TIME=14:45
SQUAREOFF_TIME=15:10
TIMEFRAME=5m                     # fixed

# Capital
ALLOCATION=1000000               # 10 lakh. Configured, NEVER read from funds
REQUIRE_FUNDS_AT_LEAST=1000000   # pre-open assertion against broker availablecash

# Risk
RISK_FRACTION_BASE=0.005         # 0.5% of ALLOCATION per trade, mandate ceiling
RISK_FRACTION_FLOOR=0.00125      # 0.25 x base
DAILY_LOSS_LIMIT_PCT=2.0         # of ALLOCATION
MAX_CONCURRENT_POSITIONS=3       # must satisfy MAX_CONCURRENT x RISK_BASE <= LIMIT
MAX_TRADES_PER_DAY=6
CONSECUTIVE_LOSS_HALT=3
CONSECUTIVE_LOSS_PAUSE=2
PAUSE_MINUTES=30
SCRATCH_BAND_R=0.1

# Exposure
MAX_PER_SECTOR=2
MARKET_FILTER_SYMBOL=NIFTY
MARKET_FILTER_EXCHANGE=NSE_INDEX
# Added to .env.example AND parsed by config.py together at step 5, once the
# portfolio backtest has produced a number to set them to. Listing a key in only
# one of the two files is how they drift apart.
#   MAX_GROSS_EXPOSURE_PCT
#   MAX_NET_EXPOSURE_PCT
#   MIN_TURNOVER
#   ATR_PCT_BAND

# Selection
LOOKBACK_LONG_SESSIONS=90
LOOKBACK_SHORT_SESSIONS=15
HYSTERESIS_MARGIN=               # OPEN - measure at step 5
REGIME_ADX_FLOOR=                # OPEN - measure at step 5

# Basket - fixed, reviewed weekly, priority order is the tie-break
BASKET=                          # SYMBOL:SECTOR, five entries, ordered

# A symbol with no sector label is bucketed as UNCLASSIFIED rather than given its
# own bucket, so MAX_PER_SECTOR still constrains it. Treating an unlabelled name as
# uncorrelated with everything else is the unsafe reading of a typo.

# Inherited from TradingAgent
OPENALGO_API_KEY=
OPENALGO_HOST=http://127.0.0.1:5000
LITELLM_MODEL=
TRADING_ENABLED=false
REQUIRE_ANALYZER_MODE=true
KILL_SWITCH_FILE=data/KILL
DEFAULT_STRATEGY_NAME=AutoAgent
TIMEZONE=Asia/Kolkata
```

---

## Appendix B - Intent record

One row per trade, not per order. A trade is three orders: entry, stop, exit.

```
intent_id                deterministic, see Part 6
trading_date
strategy_id
strategy_version
mandate_version
symbol, exchange, side
signal_bar_ts
planned_qty, planned_entry, planned_stop
risk_amount, risk_fraction_used
state
state_history            [(state, ts, reason)]
entry_order_id, fill_price, fill_qty, fill_ts
stop_order_id, stop_price
exit_order_id, exit_price, exit_qty, exit_ts, exit_reason
gross_pnl, costs, net_pnl, r_multiple
```

This single artifact is simultaneously the **reconciliation source**, the **audit
trail**, and the **P&L and metrics source**. The order-level audit from
`safety/audit.py` sits below it, unchanged.

---

## Appendix C - Relationship to TradingAgent

| | TradingAgent | AutoAgent |
|---|---|---|
| Driver | Human message | Clock |
| Approval | Per order | Per mandate |
| LLM role | Decides and acts through tools | Plans and explains only |
| State | Per chat turn | Persistent across the session |
| Failure mode | Wrong answer | Wrong position |
| Shared | `openalgo/`, `safety/`, `indicators/`, conventions | |

The two are siblings, not versions. TradingAgent remains the right tool when a human is
present and wants to ask questions. AutoAgent is for the session where nobody is
watching.

---

## Appendix D - Repository layout

```
AutoAgent/
  .env / .env.example
  backend/
    app/
      main.py                 FastAPI, SSE, control routes
      config.py               settings, mandate defaults, logging
      version.py
      strategies/
        base.py               the Part 3 contract
        stg1_ema_10_20.py
        stg2_supertrend_3_10.py
        stg3_sma10_ema30.py
        adapters.py           BacktestAdapter, ExecutionAdapter
      backtest/
        replay.py             faithful fill simulation, trade list
        metrics.py            two-window stats from a trade list
        vbt_report.py         vectorbt stats and tearsheet
      planner/
        regime.py             ADX / choppiness read
        selector.py           deterministic pick + hysteresis
        agent.py              agno Planner, rationale and risk fraction
      executor/
        clock.py              session schedule
        machine.py            order state machine (Part 6)
        intents.py            intent log read/write
        reconcile.py          broker vs persisted state
      risk/
        budget.py             daily loss budget, breakers
        sizing.py             risk-to-quantity
        guard.py              ported RiskGuard, with the Part 5 fixes
      data/
        bars.py               persistent historical store
        live.py               session frame, inherited TTL cache
        universe.py           watchlist construction
      openalgo/               ported unchanged from TradingAgent
      safety/audit.py         ported unchanged, order-level
    tests/
      test_parity.py          the step 2 gate, per strategy
      test_replay.py
      test_state_machine.py
      test_risk_budget.py
      test_reconcile.py
    requirements.txt
  frontend/
    src/
      components/ui/          shadcn, owned source
      components/             MandateCard, LiveBoard, TradeTable,
                              EquityCurve, HaltButton, PlanSummary
      lib/                    api.ts, sse.ts, format.ts
      App.tsx  main.tsx  index.css
    package.json  vite.config.ts  tsconfig.json  components.json
  data/
    autoagent.db              intents, audit, metrics
    bars/                     historical 5m store
    KILL                      kill switch, absent by default
  docs/
    plan/PLAN.md              this document
    progress/                 one numbered file per build step
    reference/                pointers to TradingAgent docs, not copies
  scripts/
    validate_setup.py         Part 1 OPEN items, run first
```

The `strategies/` directory is the one place a strategy is defined. `backtest/` and
`executor/` both import from it and neither may reimplement it. That single rule is what
the parity test enforces.
