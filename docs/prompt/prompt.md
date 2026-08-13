# How this project was specified

A verbatim, ordered record of every instruction typed during the build, with a short note
on what each one produced.

Kept for the same reason the sibling project keeps one: the scope arrived in pieces, and
several late messages changed decisions that had already been made. Reading the plan alone
makes the design look like it was reasoned out in one sitting. It was not.

The prompts are reproduced as typed, including typos.

---

## 1. Understand the sibling project

> understand 'd:\AI Bootcamp 2026\Day09\TradingAgent' and understand
> 'd:\AI Bootcamp 2026\Day09\TradingAgent\docs\reference' spin multiple agents to
> understand it

Eight agents in parallel: backend core, client and safety layers, tools and indicators,
frontend, project docs, the Agno reference, the OpenAlgo reference, and the research
notes.

The finding that shaped everything after it: TradingAgent is *architecturally
anti-autonomous*. Its README headline is "every order stops and asks you to approve it,"
and its plan lists autonomous trading under explicit non-goals.

## 2. What could be built on top of it

> i want to build a self autonomous AI Agent inspired from
> 'd:\AI Bootcamp 2026\Day09\TradingAgent' What are my options? What i can build. Dont
> code but it is just a brainstroming

Established the central point: autonomy does not remove the human approval layer, it moves
it. From per-trade to per-mandate. Pre-authorisation instead of just-in-time
authorisation.

Also established where the model belongs - not in the trigger path - which every later
decision follows from.

## 3. An agent builder with permissions

> the goal is to build a UI where the user can create the agent via prompt, user can do
> the pre authorization. the user can provide permissions to access funds, orderbook,
> tradebook, positions, access to risk metrics (history of trades), indicators (trend,
> volatility etc). So user can able to build their own AI Agent. What could be the
> enhances and what are the gaps?

Not the project that got built, but it surfaced three gaps that carried forward: analyzer
mode is application-wide so paper and live cannot coexist; N agents on one account is a
portfolio problem rather than N independent problems; and position attribution does not
exist because the broker nets everything.

## 4. Narrow to one agent

> for time being we are going to build a single AI Agent which is going to trade Equity
> Stocks intraday. Most likely the primary goal is to maximize the money (profits) and
> reduce the drawdown and losses. Who provide we in crisp terms what should be the design
> in simple words and crisp

Produced the session clock, the fixed-risk sizing rule, and the graduated circuit
breakers. Also the sentence that turned out to be the whole story: *the architecture
protects you, it does not make you money.*

## 5. The three strategies, and the vectorbt question

> what is i provide access to three strategies and before the market starts the AI agent
> should allow to run one of it based up its results, risk profile. strategy 1)ema 10 and
> ema 20 (long only strategy) 2)supertrend 3,10 (long and short) 3)sma 10 and ema 30.
> timeframe is 5min (Fixed). Need controls for startime , end time and squareoff. Daily
> Loss Limit. AI Agent decide the qty. do we need vectorbt here?

Answer given: no, not for signals. Two pushbacks were raised here and both survived into
the plan - selecting on recent performance is chasing, and all three strategies are
trend-following so they fail together.

## 6. The architecture sketch

> [hand-drawn diagram: Main -> Backtesting -> Stg1/Stg2/Stg3, 5min timeframe]
>
> this is in my mind what do you think about it. we are not using vectorbt for signal. but
> for strategy selection and backtesting metrics so that main agent on a daily basis can
> select a strategy to run and also decide on how much to bet.

The one structural correction: strategies belong ABOVE both the backtester and the
executor, not inside the backtester. That correction became the parity test, which is the
project's hardest gate.

## 7. Industry standards

> what are the features of autonomous trading agent as per industry standard?

MiFID II RTS 6, SEC 15c3-5, SEBI. Produced the gap table against what TradingAgent already
had - roughly 60 percent of the controls, near zero of the operational half.

## 8. Choosing what to build first

> 1. Daily loss limit + consecutive-loss halt
> 2. Order state machine with idempotency
> this makes sense to me

These two became Part 5 and Part 6 of the plan, and they are the two that later caught the
most bugs.

## 9. Load the backtesting skills

> load the skills 'd:\AI Bootcamp 2026\Day09\.claude'

## 10. Write the plan

> yes now can you build the plan for the autoagent
> 'd:\AI Bootcamp 2026\Day09\AutoAgent\docs'

`docs/plan/PLAN.md`, 12 parts and 4 appendices, following the sibling project's structure.

## 11. The stack

> stack is fastapi, shadcn ui, react , sqlite - use the latest stack similar to
> 'd:\AI Bootcamp 2026\Day09\TradingAgent' + vectorbt

## 12. The correction that mattered most

> vectorbt is only for backtesting - we need to have execution strategies as well for the
> same strategies which are backtesting using vectorbt

This is the most consequential message in the list. It produced the two-adapter design and
the parity test that proves the live path and the measured path produce identical signals.
Without it, the morning selection would have been measuring a strategy that never traded.

## 13. A basket, not one stock

> can i run portfolio of symbols let say a basket of 5 symbols in a given day for intraday?

Produced the binding constraint: `max_concurrent x risk_per_trade <= daily_loss_limit`.
Five at 0.5 percent is 2.5 percent against a 2 percent limit, so five is a candidate pool
rather than a target. Also the correlation point - five Indian equities on one trend
strategy are closer to two independent bets than five.

## 14. The allocation

> what is if i have 1Cr fund can i able to allocate 10 Lakhs for this strategy?

Produced the rule the whole risk layer rests on: **ALLOCATION is configured and never read
from the broker.** The sibling project sizes against available funds because a human sees
each order. This one runs unattended.

It proved itself on first contact at step 0 - the account held 99,99,984 against a
10,00,000 allocation, so sizing against broker funds would have produced positions ten
times too large, silently.

## 15. Fold both into the plan

> yes update the plan with both changes

## 16. Publish

> commit and push the autoagent with readme, add gitignore, MIT License , public repo
> 'd:\AI Bootcamp 2026\Day09\AutoAgent' commit and push the plan as well

## 17. The build loop

> start implement the plan in a loop , with 2min of sleep time. every phase test, validate,
> commit and push and explain the process what you done in simple words in a seperate .md
> file 'd:\AI Bootcamp 2026\Day09\AutoAgent\docs\progress' write inside here. do this in a
> loop

Every two minutes, for the rest of the build. See the appendix.

## 18. Use the skills

> and for implementing related to vectorbt use vectorbt skills

## 19. The .env correction

> i see a difference in .env file of 'd:\AI Bootcamp 2026\Day09\TradingAgent' and
> 'd:\AI Bootcamp 2026\Day09\AutoAgent' can you make sure you are folling very similar to
> TradingAgent

A real catch. The first version had dropped the entire ported risk block and the provider
configuration guide. Rewritten to follow the sibling structure section by section, 60 keys,
with `.env` and `.env.example` verified to match exactly.

## 20. Continue

> yes then continue the loop after matching the .env

## 21. Parallelise

> spin multiple agents to implement the app faster

Four agents on disjoint file sets: config, the OpenAlgo client port, the strategies and
parity test, and the vectorbt environment plus the bar store. Later two more for the API
and the frontend.

## 22. This file

> all the prompt what i typed and your response write to prompt.md
> 'd:\AI Bootcamp 2026\Day09\AutoAgent\docs\prompt'

---

## Appendix: the loop prompt

Fired every two minutes from message 17 to the end of the build.

> Continue building AutoAgent at "D:/AI Bootcamp 2026/Day09/AutoAgent" per
> docs/plan/PLAN.md. Each iteration: implement the next unfinished step from the plan's
> Part 9 build order (steps 0-12), validate it by actually running it (not just writing
> it), then commit and push to origin/main, then write a numbered progress .md into
> docs/progress/ (001-*.md, 002-*.md, ...) explaining in simple words what was done, what
> was tested, what was found, and what is next. Follow Part 10 conventions: no icons or
> emoji anywhere in code, logs, comments or docs; ASCII-safe logging; module docstrings
> record hard-won runtime facts; comments explain why not what. Never commit .env or
> secrets. Stop the loop when all 12 build steps are done and the app runs end to end.

---

## What the record shows

Three messages changed the architecture after it had been decided.

Message 12 - *"we need to have execution strategies as well"* - produced the parity test,
which is the only reason the backtest describes the thing that actually trades.

Message 14 - the allocation question - produced the rule that stopped every position being
sized ten times too large.

Message 19 - the `.env` comparison - caught a whole missing configuration block.

None of the three were in the original brief. All three are load-bearing.

The plan also predicted its own largest risk correctly: *"The three strategies may have no
positive expectancy after costs. This is the largest risk and the architecture cannot
mitigate it."* Measured over 93 sessions, they do not. The machine works; the edge does
not exist yet.
