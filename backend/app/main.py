"""FastAPI control surface: mandate approval, the live session view, and the halt button.

PLAN.md Part 8. This process is the window onto a session; it does not trade and it
holds no strategy logic. The executor owns the loop, the broker connection and the
ordered squareoff. Everything here reads state or sets a flag.

Runtime facts this module exists to record:

  - setup_logging() runs at IMPORT time, before any other app import. Agno installs a
    ColoredRichHandler with box-drawing glyphs the moment anything under app/ pulls it
    in, and replacing the root handler afterwards is too late. That ordering is the
    only reason for the noqa: E402 block below, and it is the same ordering
    TradingAgent uses.

  - THE INHERITED BUG, FIXED HERE ON PURPOSE. TradingAgent's /api/health and
    /api/mode call client.ping() and client.analyzer_mode() directly inside
    `async def`. The OpenAlgo SDK is fully synchronous, so each of those calls blocks
    the entire event loop for a broker round trip - up to the 15s timeout when
    OpenAlgo is wedged - and every other request, the SSE stream included, stalls
    behind it. Every broker read here goes through asyncio.to_thread and lands in a
    short-lived cache, so a screen full of pollers costs one round trip per
    BROKER_CACHE_TTL seconds instead of one per request.

  - SSE frames carry no "event:" line. The discriminator is a "type" field inside the
    JSON payload, which means EventSource cannot consume this stream; the client uses
    fetch + getReader(). Contract copied from TradingAgent unchanged.

  - The halt button sets STATE. It does not cancel orders or flatten positions,
    because the executor owns the broker connection and the sequenced squareoff in
    PLAN.md Part 5 (cancel stops BEFORE sending market exits, or a stop and an exit
    can both fill and leave a reversed position). The state is written to the same
    sticky halt file RiskBudget persists to, so a halt pressed while nothing is
    running is still in force when an executor starts. A halt that does not survive a
    restart is not a halt.

  - The executor package is owned by another build step and may not exist yet, so it
    is imported defensively and probed exactly ONCE per process. When it is absent
    this module keeps its own RiskBudget for the trading day against the same halt
    file. That degrades the live view to "no positions known" rather than to a 500,
    and `source` on the session body says which it is - the UI must not paint an
    unknown book as a flat one.

  - The trading date comes from the configured timezone, not the host clock. A host
    running in UTC rolls the date at 05:30 IST, mid-session, and would then serve the
    wrong plan file for the rest of the day.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, AsyncIterator

from .config import get_settings, setup_logging

setup_logging()

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402

from .api.schemas import (  # noqa: E402
    ApprovalRecord,
    ApproveRequest,
    ApproveResponse,
    BreakerStatus,
    ConfigResponse,
    ErrorPayload,
    HaltRequest,
    HealthResponse,
    NoticePayload,
    PingPayload,
    PlanConsequences,
    PlanResponse,
    PositionView,
    ReduceOnlyRequest,
    SessionResponse,
    SSEEventType,
    StateResponse,
    TradeRow,
    TradesResponse,
)
from .openalgo.client import get_client  # noqa: E402
from .risk.budget import RiskBudget, RunState  # noqa: E402
from .version import get_version  # noqa: E402

log = logging.getLogger("app.main")

settings = get_settings()
client = get_client()

app = FastAPI(title="AutoAgent", version=get_version())
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Broker reads are cached for this long. Long enough that a dashboard polling health
# once a second costs one ping every five, short enough that a broker that drops out
# is visible well inside a 5-minute bar.
BROKER_CACHE_TTL = 5.0
MARKS_CACHE_TTL = 5.0

# Stream cadence. The state frame goes out every STATE_INTERVAL regardless; the poll
# runs faster so a halt shows up in well under a second rather than on the next beat.
STATE_INTERVAL_SEC = 2.0
POLL_INTERVAL_SEC = 0.25
PING_INTERVAL_SEC = 30.0

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # nginx buffers a proxied response by default, which holds every frame back until
    # the buffer fills. On a 2-second heartbeat that reads as a dead stream.
    "X-Accel-Buffering": "no",
}


# --- clock ------------------------------------------------------------------


def _load_timezone():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001
        # tzdata missing or the name is wrong. The host clock is wrong for a market
        # in another zone, so this is loud rather than silent.
        log.warning("timezone %r unusable, falling back to the host clock",
                    settings.timezone, exc_info=True)
        return None


_TZ = _load_timezone()


def _now() -> datetime:
    return datetime.now(_TZ) if _TZ is not None else datetime.now()


def _today() -> date:
    return _now().date()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


# --- broker reads, off the event loop ---------------------------------------

_broker_cache: dict[str, Any] = {"at": 0.0, "value": None}
_broker_lock = asyncio.Lock()


def _read_broker() -> dict[str, Any]:
    """One ping plus one analyzer-status call. BLOCKING - only call in a thread."""
    ping = client.ping()
    connected = bool(ping.get("ok"))
    data = ping.get("data") or {}
    return {
        "connected": connected,
        "broker": data.get("broker") if connected else None,
        # analyzer mode is application-wide, not per API key, so this answers for the
        # whole OpenAlgo instance including any TradingAgent session beside it.
        "mode": client.analyzer_mode(),
        "error": None if connected else ping.get("error"),
    }


async def _broker_status() -> dict[str, Any]:
    """Cached broker reachability and mode.

    See the module docstring: the synchronous SDK must never be called from a
    coroutine directly. The double-check inside the lock means a burst of concurrent
    requests produces ONE round trip rather than one each.
    """
    cached = _broker_cache["value"]
    if cached is not None and time.monotonic() - _broker_cache["at"] < BROKER_CACHE_TTL:
        return cached

    async with _broker_lock:
        cached = _broker_cache["value"]
        if cached is not None and time.monotonic() - _broker_cache["at"] < BROKER_CACHE_TTL:
            return cached
        try:
            value = await asyncio.to_thread(_read_broker)
        except Exception as exc:  # noqa: BLE001
            log.warning("broker status read failed: %s", exc)
            value = {"connected": False, "broker": None, "mode": "unknown",
                     "error": str(exc)}
        _broker_cache["value"] = value
        _broker_cache["at"] = time.monotonic()
        return value


# symbol -> (last price, monotonic timestamp)
_marks_cache: dict[str, tuple[float, float]] = {}
_marks_lock = asyncio.Lock()


def _quote_exchange() -> str:
    """Cash equity NSE. Position records carry no exchange, and the mandate is one
    exchange wide, so the allowlist's first entry is the right default."""
    return settings.allowed_exchanges[0] if settings.allowed_exchanges else "NSE"


def _read_marks(symbols: list[str]) -> dict[str, float]:
    """BLOCKING. One quote per symbol, at most max_concurrent_positions of them."""
    exchange = _quote_exchange()
    out: dict[str, float] = {}
    for symbol in symbols:
        price = client.ltp(symbol, exchange)
        if price:
            out[symbol] = float(price)
    return out


async def _marks(symbols: list[str]) -> dict[str, float]:
    """Last prices for open positions, cached.

    Nothing is fetched when the book is empty, which is the whole cost of the live
    view when no executor is attached: zero broker calls.
    """
    if not symbols:
        return {}
    now = time.monotonic()
    stale = [s for s in symbols if now - _marks_cache.get(s, (0.0, 0.0))[1] > MARKS_CACHE_TTL]
    if stale:
        async with _marks_lock:
            now = time.monotonic()
            stale = [s for s in symbols
                     if now - _marks_cache.get(s, (0.0, 0.0))[1] > MARKS_CACHE_TTL]
            if stale:
                try:
                    fresh = await asyncio.to_thread(_read_marks, stale)
                except Exception as exc:  # noqa: BLE001
                    log.warning("marks read failed: %s", exc)
                    fresh = {}
                stamp = time.monotonic()
                for symbol, price in fresh.items():
                    _marks_cache[symbol] = (price, stamp)
    return {s: _marks_cache[s][0] for s in symbols if s in _marks_cache}


# --- the executor bridge ----------------------------------------------------
#
# The executor is another build step's file and may not exist yet. Probed once, by
# name, and cached either way: a failed import re-hits the filesystem every time
# otherwise, and this runs on every stream tick. An executor that appears after this
# process started needs a restart to be seen, which is the normal case anyway since
# the executor and the API start together.

_EXECUTOR_HOOKS: tuple[tuple[str, str], ...] = (
    (".executor.runner", "get_budget"),
    (".executor.runner", "get_runner"),
    (".executor.runner", "get_executor"),
    (".executor", "get_budget"),
    (".executor", "get_runner"),
    (".executor.machine", "get_budget"),
)

_hook_probed = False
_hook: Any = None
_hook_lock = threading.Lock()


def _executor_hook() -> Any:
    global _hook_probed, _hook
    if _hook_probed:
        return _hook
    with _hook_lock:
        if _hook_probed:
            return _hook
        for module_name, attr in _EXECUTOR_HOOKS:
            try:
                module = importlib.import_module(module_name, __package__)
            except ImportError:
                continue
            except Exception:  # noqa: BLE001
                # A half-written module raises SyntaxError or worse, and the control
                # surface must still come up so the halt button exists.
                log.warning("executor module %s failed to import", module_name,
                            exc_info=True)
                continue
            fn = getattr(module, attr, None)
            if callable(fn):
                log.info("executor bridge: using %s%s.%s", __package__, module_name, attr)
                _hook = fn
                break
        else:
            log.info("no executor attached; serving the control surface's own budget")
        _hook_probed = True
        return _hook


def _executor_budget() -> RiskBudget | None:
    fn = _executor_hook()
    if fn is None:
        return None
    try:
        obj = fn()
    except Exception:  # noqa: BLE001
        log.warning("executor hook raised", exc_info=True)
        return None
    if isinstance(obj, RiskBudget):
        return obj
    budget = getattr(obj, "budget", None)
    return budget if isinstance(budget, RiskBudget) else None


_fallback_budget: RiskBudget | None = None
_fallback_lock = threading.Lock()


def _control_budget() -> RiskBudget:
    """This process's own budget, used when no executor is attached.

    restore() is called on creation because a halt is sticky: the file it reads is
    the same one the executor writes, so a session halted yesterday-by-restart or by
    a breaker still reads as halted here.
    """
    global _fallback_budget
    day = _today()
    with _fallback_lock:
        if _fallback_budget is None or _fallback_budget.trading_date != day:
            budget = RiskBudget(settings=settings, trading_date=day)
            budget.restore()
            _fallback_budget = budget
        return _fallback_budget


def _budget() -> tuple[RiskBudget, bool]:
    """(budget, executor_attached)."""
    live = _executor_budget()
    if live is not None:
        return live, True
    return _control_budget(), False


# --- session snapshot -------------------------------------------------------


async def _session_snapshot() -> SessionResponse:
    budget, attached = _budget()
    positions = list(budget.open_positions.values())
    marks = await _marks([p.symbol for p in positions])

    views: list[PositionView] = []
    for p in positions:
        last = marks.get(p.symbol)
        views.append(PositionView(
            symbol=p.symbol,
            sector=settings.sector_of(p.symbol),
            side=p.side,
            quantity=p.quantity,
            entry_price=p.entry_price,
            stop_price=p.stop_price,
            last_price=last,
            unrealized=round(p.unrealized(last or 0.0), 2),
            worst_case_remaining=round(p.worst_case_remaining(), 2),
        ))

    unrealized = budget.unrealized_pnl(marks)
    mtm = budget.mtm_pnl(marks)
    remaining = budget.remaining_budget(marks)
    state = budget.state

    breakers = BreakerStatus(
        kill_switch=settings.kill_switch_engaged(),
        trading_enabled=settings.trading_enabled,
        halted=state is RunState.HALTED,
        reduce_only=state is RunState.REDUCE_ONLY,
        paused=state is RunState.PAUSED,
        paused_until=_iso(budget.paused_until),
        halt_reason=budget.halt_reason,
        consecutive_losses=budget.consecutive_losses,
        consecutive_loss_pause_at=settings.consecutive_loss_pause,
        consecutive_loss_halt_at=settings.consecutive_loss_halt,
        budget_exhausted=remaining <= 0,
        position_cap_reached=len(positions) >= settings.max_concurrent_positions,
        trade_cap_reached=budget.trade_count >= settings.max_trades_per_day,
    )

    return SessionResponse(
        trading_date=budget.trading_date.isoformat(),
        as_of=_now().isoformat(timespec="seconds"),
        source="executor" if attached else "control-surface",
        executor_attached=attached,
        # A PAUSED state whose deadline has passed is reported as it stands. Expiring
        # it is the executor's transition to make on its next wake, and a read
        # endpoint that mutates session state would race with it.
        state=state.value,
        halt_reason=budget.halt_reason,
        paused_until=_iso(budget.paused_until),
        positions=views,
        position_count=len(views),
        max_concurrent_positions=settings.max_concurrent_positions,
        realized_pnl=round(budget.realized_pnl, 2),
        unrealized_pnl=round(unrealized, 2),
        mtm_pnl=round(mtm, 2),
        budget_used_pct=round(budget.budget_used_pct(marks), 2),
        remaining_budget=round(remaining, 2),
        daily_loss_limit_amount=settings.daily_loss_limit_amount,
        allocation=settings.allocation,
        trade_count=budget.trade_count,
        max_trades_per_day=settings.max_trades_per_day,
        consecutive_losses=budget.consecutive_losses,
        breakers=breakers,
    )


# --- plan file --------------------------------------------------------------
#
# data/plans/<date>.json, located off db_path the same way RiskBudget locates its
# halt file, so moving DB_PATH moves the whole data set together.

_plan_write_lock = threading.Lock()


def _plans_dir() -> Path:
    return Path(settings.db_path).parent / "plans"


def _plan_path(day: date) -> Path:
    return _plans_dir() / f"{day.isoformat()}.json"


def _read_plan_file(day: date) -> dict[str, Any] | None:
    path = _plan_path(day)
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        log.warning("plan file %s is unreadable", path, exc_info=True)
        return None
    return loaded if isinstance(loaded, dict) else None


def _write_plan_file(day: date, payload: dict[str, Any]) -> None:
    """Atomic replace. A torn plan file at 08:45 would fail the whole session."""
    path = _plan_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _effective_risk_fraction(proposed: Any) -> tuple[float, str]:
    """Apply the mandate band to whatever the plan proposed.

    PLAN.md Part 5: the Planner may only scale risk DOWN, and anything missing or out
    of range falls back to the FLOOR, not the ceiling, so a malformed plan trades
    smaller rather than larger. This is re-derived on every read rather than trusted
    from the file, because the file is written by a model-driven step.
    """
    base = settings.risk_fraction_base
    floor = settings.risk_fraction_floor
    if proposed is None:
        return floor, "the plan proposed no risk fraction, so the floor applies"
    try:
        value = float(proposed)
    except (TypeError, ValueError):
        return floor, f"the plan proposed {proposed!r}, which is not a number; floor applies"
    if value <= 0:
        return floor, f"the plan proposed {value}, which is not positive; floor applies"
    if value > base + 1e-9:
        return floor, (f"the plan proposed {value}, above the mandate ceiling {base}; "
                       f"out of range falls back to the floor, never the ceiling")
    if value < floor:
        return floor, f"the plan proposed {value}, below the floor {floor}; floor applies"
    return value, "as proposed by the plan, inside the mandate band"


def _consequences(risk_fraction: Any) -> PlanConsequences:
    """Server-computed, never taken from the plan's prose. PLAN.md Part 8."""
    effective, note = _effective_risk_fraction(risk_fraction)
    per_trade = settings.allocation * effective
    at_risk = per_trade * settings.max_concurrent_positions
    proposed: float | None
    try:
        proposed = float(risk_fraction) if risk_fraction is not None else None
    except (TypeError, ValueError):
        proposed = None
    return PlanConsequences(
        allocation=settings.allocation,
        risk_fraction=effective,
        risk_fraction_proposed=proposed,
        risk_fraction_note=note,
        risk_amount_per_trade=round(per_trade, 2),
        max_concurrent_positions=settings.max_concurrent_positions,
        capital_at_risk=round(at_risk, 2),
        capital_at_risk_pct=round(
            100.0 * at_risk / settings.allocation if settings.allocation else 0.0, 4),
        daily_loss_limit_pct=settings.daily_loss_limit_pct,
        daily_loss_limit_amount=settings.daily_loss_limit_amount,
        max_trades_per_day=settings.max_trades_per_day,
        start_time=f"{settings.start_time:%H:%M}",
        end_time=f"{settings.end_time:%H:%M}",
        squareoff_time=f"{settings.squareoff_time:%H:%M}",
    )


def _plan_response(day: date, raw: dict[str, Any] | None) -> PlanResponse:
    """Read tolerantly.

    The planner writes this file and is a separate build step, so the fields are
    accepted either nested under "selection" (Selection.as_dict()) or flat at the top
    level, and the regime either as a label string or as a RegimeRead dump.
    """
    path = str(_plan_path(day))
    if raw is None:
        return PlanResponse(exists=False, trading_date=day.isoformat(), path=path)

    selection = raw.get("selection") if isinstance(raw.get("selection"), dict) else {}
    merged: dict[str, Any] = {**raw, **selection}

    regime = merged.get("regime")
    if isinstance(regime, dict):
        regime_label = str(regime.get("label") or regime.get("direction") or "")
    else:
        regime_label = str(regime or "")

    basket = merged.get("basket")
    if isinstance(basket, list):
        basket_out = [str(b) for b in basket]
    else:
        basket_out = [f"{sym}:{sec}" for sym, sec in settings.basket]

    approval_raw = raw.get("approval")
    approval = None
    if isinstance(approval_raw, dict):
        approval = ApprovalRecord(
            approved=bool(approval_raw.get("approved")),
            note=approval_raw.get("note"),
            at=str(approval_raw.get("at") or ""),
        )

    strategy_id = merged.get("strategy_id") or merged.get("strategy")
    risk_fraction = merged.get("risk_fraction")

    return PlanResponse(
        exists=True,
        trading_date=str(merged.get("trading_date") or day.isoformat()),
        path=path,
        strategy_id=str(strategy_id) if strategy_id else None,
        regime=regime_label,
        reason=str(merged.get("reason") or ""),
        risk_fraction=(float(risk_fraction)
                       if isinstance(risk_fraction, (int, float)) else None),
        basket=basket_out,
        trade_today=bool(merged.get("trade_today")),
        approval=approval,
        consequences=_consequences(risk_fraction),
        raw=raw,
    )


# --- intent log -------------------------------------------------------------


def _read_trades_sqlite(day: date) -> tuple[str, list[dict[str, Any]]]:
    """BLOCKING. Today's intent rows, or an empty list if the log is absent.

    The intent log's schema belongs to the executor build step, so this asks sqlite
    what exists rather than assuming: no table means no trades yet, which is the
    normal state before the first entry of the day and not an error.
    """
    path = Path(settings.db_path)
    if not path.exists():
        return "absent", []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return "absent", []
    try:
        conn.row_factory = sqlite3.Row
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        table = next((t for t in ("intents", "intent_log", "trades") if t in names), None)
        if table is None:
            return "absent", []
        columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "trading_date" in columns:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE trading_date = ?", (day.isoformat(),)
            ).fetchall()
        else:
            rows = conn.execute(f"SELECT * FROM {table} LIMIT 500").fetchall()
        return f"sqlite:{table}", [dict(r) for r in rows]
    except sqlite3.Error as exc:
        log.warning("intent log read failed: %s", exc)
        return "absent", []
    finally:
        conn.close()


def _read_trades_executor(day: date) -> tuple[str, list[dict[str, Any]]] | None:
    """The executor's own reader, when it exists. Same defensive probe as the budget."""
    for module_name, attr in ((".executor.intents", "load_today"),
                              (".executor.intents", "today"),
                              (".executor.intents", "read_today")):
        try:
            module = importlib.import_module(module_name, __package__)
        except Exception:  # noqa: BLE001
            return None
        fn = getattr(module, attr, None)
        if not callable(fn):
            continue
        try:
            rows = fn(day)
        except TypeError:
            rows = fn()
        except Exception:  # noqa: BLE001
            log.warning("intent log hook %s raised", attr, exc_info=True)
            return None
        if isinstance(rows, list):
            return "executor", [r if isinstance(r, dict) else dict(r) for r in rows]
    return None


# --- SSE --------------------------------------------------------------------


def sse(event: SSEEventType, data: dict[str, Any]) -> str:
    """One frame. No "event:" line - the discriminator is the "type" key inside."""
    return f"data: {json.dumps({'type': event, **data}, default=str, ensure_ascii=False)}\n\n"


def _state_notice(previous: str, current: str) -> NoticePayload:
    level = "error" if current == RunState.HALTED.value else (
        "warning" if current in (RunState.REDUCE_ONLY.value, RunState.PAUSED.value)
        else "info")
    return NoticePayload(level=level, message=f"run state {previous} -> {current}")


async def _stream(request: Request) -> AsyncIterator[str]:
    """State every STATE_INTERVAL_SEC, plus an immediate frame on any change."""
    last_fingerprint = ""
    last_state = ""
    last_emit = 0.0
    last_ping = time.monotonic()
    try:
        while True:
            if await request.is_disconnected():
                return

            snapshot = await _session_snapshot()
            payload = snapshot.model_dump(mode="json")
            # as_of moves every tick, so it is excluded or every frame looks changed.
            fingerprint = json.dumps(
                {k: v for k, v in payload.items() if k != "as_of"},
                sort_keys=True, default=str)

            now = time.monotonic()
            first = not last_fingerprint
            changed = not first and fingerprint != last_fingerprint

            if snapshot.state != last_state:
                if last_state:
                    yield sse("notice",
                              _state_notice(last_state, snapshot.state).model_dump())
                last_state = snapshot.state

            if first or changed or (now - last_emit) >= STATE_INTERVAL_SEC:
                yield sse("state", {**payload, "changed": changed})
                last_fingerprint = fingerprint
                last_emit = now

            if now - last_ping >= PING_INTERVAL_SEC:
                yield sse("ping", PingPayload(
                    at=_now().isoformat(timespec="seconds")).model_dump())
                last_ping = now

            await asyncio.sleep(POLL_INTERVAL_SEC)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("stream failed")
        yield sse("error", ErrorPayload(message=str(exc),
                                        kind=type(exc).__name__).model_dump())


# --- routes -----------------------------------------------------------------

# Values that must never appear in a response body verbatim, whatever redacted()
# does. Short strings are excluded so a placeholder cannot match half the dump.
_SECRETS: tuple[str, ...] = tuple(
    v for v in (settings.openalgo_api_key, settings.litellm_api_key) if len(v) >= 8)


def _scrub(dump: dict[str, Any]) -> dict[str, Any]:
    """Second pass over Settings.redacted(), which is not trusted to be complete.

    A config endpoint that leaks a broker key hands over the account, so anything
    still containing a live credential verbatim is replaced outright.
    """
    out: dict[str, Any] = {}
    for key, value in dump.items():
        if isinstance(value, str) and any(secret in value for secret in _SECRETS):
            out[key] = "(redacted)"
        else:
            out[key] = value
    return out


@app.get("/api/health")
async def health() -> HealthResponse:
    broker = await _broker_status()
    return HealthResponse(
        ok=True,
        version=get_version(),
        openalgo_connected=bool(broker["connected"]),
        broker=broker["broker"],
        mode=str(broker["mode"]),
        trading_enabled=settings.trading_enabled,
        allocation=settings.allocation,
        missing_keys=settings.missing(),
    )


@app.get("/api/config")
async def config() -> ConfigResponse:
    errors = settings.validate()
    return ConfigResponse(
        settings=_scrub(settings.redacted()),
        errors=errors,
        missing_keys=settings.missing(),
        valid=not errors,
    )


@app.get("/api/plan")
async def plan() -> PlanResponse:
    day = _today()
    raw = await asyncio.to_thread(_read_plan_file, day)
    return _plan_response(day, raw)


@app.post("/api/plan/approve")
async def approve_plan(req: ApproveRequest) -> ApproveResponse:
    """The pre-session human gate. PLAN.md Part 8.

    A plan must exist to be approved. Recording an approval against nothing would
    leave a mandate the executor could later match against a plan written afterwards,
    which is the one direction this gate must not fail in.
    """
    day = _today()
    record = ApprovalRecord(
        approved=bool(req.approved),
        note=req.note,
        at=_now().isoformat(timespec="seconds"),
    )

    def _persist() -> dict[str, Any]:
        with _plan_write_lock:
            existing = _read_plan_file(day)
            if existing is None:
                raise FileNotFoundError(str(_plan_path(day)))
            existing["approval"] = record.model_dump()
            _write_plan_file(day, existing)
            return existing

    try:
        await asyncio.to_thread(_persist)
    except FileNotFoundError as exc:
        raise HTTPException(
            404,
            f"no plan for {day.isoformat()} at {exc}; there is nothing to approve",
        ) from exc

    log.info("mandate %s for %s%s",
             "APPROVED" if record.approved else "REJECTED",
             day.isoformat(),
             f": {record.note}" if record.note else "")
    return ApproveResponse(ok=True, trading_date=day.isoformat(), approval=record,
                           path=str(_plan_path(day)))


@app.get("/api/session")
async def session() -> SessionResponse:
    return await _session_snapshot()


@app.get("/api/trades")
async def trades() -> TradesResponse:
    day = _today()
    found = await asyncio.to_thread(_read_trades_executor, day)
    if found is None:
        found = await asyncio.to_thread(_read_trades_sqlite, day)
    source, rows = found
    items = [TradeRow(**row) for row in rows]
    return TradesResponse(trading_date=day.isoformat(), source=source,
                          count=len(items), items=items)


@app.post("/api/halt")
async def halt(req: HaltRequest) -> StateResponse:
    """Drive the session to HALTED. The big red button.

    Sets state and persists it; it does not touch the broker. The executor performs
    the sequenced squareoff from PLAN.md Part 5 - cancel resting entries, cancel the
    SL-M stops, THEN send market exits, because a stop and a market exit can both
    fill and leave a reversed position.
    """
    budget, attached = _budget()
    previous = budget.state.value
    reason = (req.reason or "").strip() or "halted from the control surface"
    budget.halt(f"manual: {reason}")
    note = ("the executor will cancel orders and flatten on its next wake"
            if attached else
            "no executor is attached; the halt is persisted and a session starting "
            "later will come back halted")
    log.error("HALT requested from the control surface: %s", reason)
    return StateResponse(
        ok=True,
        trading_date=budget.trading_date.isoformat(),
        state=budget.state.value,
        previous_state=previous,
        changed=budget.state.value != previous,
        halt_reason=budget.halt_reason,
        note=note,
        executor_attached=attached,
    )


@app.post("/api/reduce-only")
async def reduce_only(req: ReduceOnlyRequest) -> StateResponse:
    """Positions may close, nothing new may open.

    The state between running and halted. A hard stop while a position is open traps
    that position; reduce-only is what lets it be closed cleanly.
    """
    budget, attached = _budget()
    previous = budget.state.value
    reason = (req.reason or "").strip() or "reduce-only from the control surface"
    budget.reduce_only(f"manual: {reason}")
    if budget.state is RunState.HALTED:
        # reduce_only() is a no-op on a halted session by design: reduce-only is a
        # LESS restrictive state and must never walk a halt back.
        note = "already halted; reduce-only does not relax a halt"
    elif attached:
        note = "the executor will stop opening and manage existing positions only"
    else:
        note = ("no executor is attached; the state is persisted and a session "
                "starting later will come back reduce-only")
    log.warning("REDUCE-ONLY requested from the control surface: %s", reason)
    return StateResponse(
        ok=True,
        trading_date=budget.trading_date.isoformat(),
        state=budget.state.value,
        previous_state=previous,
        changed=budget.state.value != previous,
        halt_reason=budget.halt_reason,
        note=note,
        executor_attached=attached,
    )


@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    return StreamingResponse(_stream(request), media_type="text/event-stream",
                             headers=SSE_HEADERS)
