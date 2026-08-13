"""Request and response bodies for the AutoAgent control surface.

PLAN.md Part 8. Facts these models exist to hold in place:

  - Every number on the mandate card is computed by the BACKEND, never echoed from
    the plan's own prose. That rule is carried over from TradingAgent's confirmation
    card - "the model does not produce these numbers; the backend does" - and it is
    what PlanConsequences is for. The plan file's proposed risk fraction is
    re-derived against the mandate band rather than displayed as written, because a
    plan that proposes 5% must show as the floor, not as 5%.

  - TradeRow accepts extra keys. The intent record in PLAN.md Appendix B is owned by
    the executor build step; pinning its columns here would make this file the thing
    that breaks when one is added. The fields the UI needs are typed, everything else
    passes through untouched.

  - SSE frames carry their discriminator INSIDE the JSON as "type". There is no
    "event:" line, so a browser EventSource cannot read this stream at all - the
    client uses fetch + getReader(). Same contract as TradingAgent, deliberately, so
    one sse.ts serves both apps.

  - RunState values are the strings from risk/budget.py: running, paused,
    reduce_only, halted. They are typed as plain str rather than an enum copy so a
    new state added there does not silently fail validation here and blank the live
    view mid-session.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# The four frame kinds on /api/stream. Listed here rather than in main.py so the
# frontend has one place to read the union from.
SSEEventType = Literal["state", "notice", "error", "ping"]


# --- requests ---------------------------------------------------------------


class ApproveRequest(BaseModel):
    """The pre-session human gate.

    A rejection is recorded exactly like an approval. "No plan was approved" and
    "the plan was explicitly rejected" are different mornings and the audit trail
    must be able to tell them apart.
    """

    approved: bool
    note: str | None = None


class HaltRequest(BaseModel):
    """The big red button. reason is free text and lands in the halt record."""

    reason: str | None = None


class ReduceOnlyRequest(BaseModel):
    """Positions may close, nothing new may open."""

    reason: str | None = None


# --- health and config ------------------------------------------------------


class HealthResponse(BaseModel):
    ok: bool = True
    version: str = ""
    openalgo_connected: bool = False
    broker: str | None = None
    # analyze | live | unknown. "unknown" must be read as live by anything that
    # gates on it - the safe reading of an unreadable answer is the dangerous one.
    mode: str = "unknown"
    trading_enabled: bool = False
    allocation: float = 0.0
    missing_keys: list[str] = Field(default_factory=list)


class ConfigResponse(BaseModel):
    """The redacted settings dump plus whatever validate() found wrong with it."""

    settings: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    missing_keys: list[str] = Field(default_factory=list)
    valid: bool = True


# --- plan and mandate -------------------------------------------------------


class ApprovalRecord(BaseModel):
    approved: bool = False
    note: str | None = None
    at: str = ""


class PlanConsequences(BaseModel):
    """What approving this plan actually commits, in rupees.

    Computed from Settings and the plan's risk fraction. risk_fraction here is the
    EFFECTIVE one after the mandate band is applied, and risk_fraction_note says why
    it differs from the plan's proposal when it does.
    """

    allocation: float = 0.0
    risk_fraction: float = 0.0
    risk_fraction_proposed: float | None = None
    risk_fraction_note: str = ""
    risk_amount_per_trade: float = 0.0
    max_concurrent_positions: int = 0
    capital_at_risk: float = 0.0
    capital_at_risk_pct: float = 0.0
    daily_loss_limit_pct: float = 0.0
    daily_loss_limit_amount: float = 0.0
    max_trades_per_day: int = 0
    start_time: str = ""
    end_time: str = ""
    squareoff_time: str = ""


class PlanResponse(BaseModel):
    """Today's plan, or exists=False when the planner has not written one.

    Absent is not an error. No plan means no session, which PLAN.md Part 11 calls a
    safe failure: a model provider outage at 08:45 should leave the agent flat, not
    trading on a stale mandate.
    """

    exists: bool = False
    trading_date: str = ""
    path: str = ""
    strategy_id: str | None = None
    regime: str = ""
    reason: str = ""
    risk_fraction: float | None = None
    basket: list[str] = Field(default_factory=list)
    trade_today: bool = False
    approval: ApprovalRecord | None = None
    consequences: PlanConsequences | None = None
    # The planner's full artifact - metrics table, rationale, regime detail. Passed
    # through so the mandate card can show the evidence without a second endpoint.
    raw: dict[str, Any] = Field(default_factory=dict)


class ApproveResponse(BaseModel):
    ok: bool = True
    trading_date: str = ""
    approval: ApprovalRecord
    path: str = ""


# --- live session -----------------------------------------------------------


class PositionView(BaseModel):
    symbol: str = ""
    sector: str = ""
    side: str = ""
    quantity: int = 0
    entry_price: float = 0.0
    stop_price: float = 0.0
    last_price: float | None = None
    unrealized: float = 0.0
    # Loss still on the table if the stop fills from here. This is the number the
    # budget check reserves against, not the current unrealized.
    worst_case_remaining: float = 0.0


class BreakerStatus(BaseModel):
    """Every gate that can stop an entry, in one object.

    kill_switch is a filesystem test rather than a flag, which is what lets it work
    without a restart and without this process being healthy.
    """

    kill_switch: bool = False
    trading_enabled: bool = False
    halted: bool = False
    reduce_only: bool = False
    paused: bool = False
    paused_until: str | None = None
    halt_reason: str = ""
    consecutive_losses: int = 0
    consecutive_loss_pause_at: int = 0
    consecutive_loss_halt_at: int = 0
    budget_exhausted: bool = False
    position_cap_reached: bool = False
    trade_cap_reached: bool = False


class SessionResponse(BaseModel):
    """The live view, and the body of every "state" frame on /api/stream.

    `source` says where the numbers came from. "executor" means a running session
    handed over its own budget. "control-surface" means no executor is attached, so
    positions and MTM read as empty - they are unknown, not zero, and the UI must
    say so rather than paint a flat book.
    """

    trading_date: str = ""
    as_of: str = ""
    source: str = "control-surface"
    executor_attached: bool = False
    state: str = "running"
    halt_reason: str = ""
    paused_until: str | None = None
    positions: list[PositionView] = Field(default_factory=list)
    position_count: int = 0
    max_concurrent_positions: int = 0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    mtm_pnl: float = 0.0
    budget_used_pct: float = 0.0
    remaining_budget: float = 0.0
    daily_loss_limit_amount: float = 0.0
    allocation: float = 0.0
    trade_count: int = 0
    max_trades_per_day: int = 0
    consecutive_losses: int = 0
    breakers: BreakerStatus = Field(default_factory=BreakerStatus)


class StateResponse(BaseModel):
    """What a halt or reduce-only request actually did.

    `note` exists because the button does less than it looks like it does. This
    process sets state and persists it; the executor owns the broker connection and
    performs the ordered squareoff. When no executor is attached the state is still
    written to the sticky halt file, so the next executor to start comes back halted.
    """

    ok: bool = True
    trading_date: str = ""
    state: str = ""
    previous_state: str = ""
    changed: bool = False
    halt_reason: str = ""
    note: str = ""
    executor_attached: bool = False


# --- trades -----------------------------------------------------------------


class TradeRow(BaseModel):
    """One intent record, PLAN.md Appendix B. One row per trade, not per order."""

    model_config = ConfigDict(extra="allow")

    intent_id: str | None = None
    trading_date: str | None = None
    strategy_id: str | None = None
    symbol: str | None = None
    side: str | None = None
    state: str | None = None
    planned_qty: int | None = None
    planned_entry: float | None = None
    planned_stop: float | None = None
    fill_price: float | None = None
    fill_qty: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    gross_pnl: float | None = None
    costs: float | None = None
    net_pnl: float | None = None
    r_multiple: float | None = None


class TradesResponse(BaseModel):
    trading_date: str = ""
    # "executor" | "sqlite:<table>" | "absent". An absent log is a normal state
    # before the first trade of the day, not a failure.
    source: str = "absent"
    count: int = 0
    items: list[TradeRow] = Field(default_factory=list)


# --- SSE payloads -----------------------------------------------------------
#
# These are the frame bodies WITHOUT the "type" key; sse() in main.py merges it in.
# A "state" frame is a SessionResponse dump plus a `changed` flag, so it has no
# model of its own.


class NoticePayload(BaseModel):
    level: str = "info"  # info | warning | error
    message: str = ""


class ErrorPayload(BaseModel):
    message: str = ""
    kind: str | None = None


class PingPayload(BaseModel):
    at: str = ""
