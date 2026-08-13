"""The intent log. One row per TRADE, not per order.

PLAN.md Appendix B. This single artifact does three jobs:

    reconciliation source   what the agent believes it did
    audit trail             what it decided and why, months later
    P&L and metrics source  what it actually made

A trade is three orders - entry, stop, exit - so an order-level audit alone
cannot answer "what position do I think I hold". That is why this sits ABOVE the
order-level audit rather than replacing it.

THE FINDING THAT SHAPES THIS FILE, from step 0:

    OpenAlgo's placeorder takes no client-supplied order id. There is no field
    that survives to the broker and back. The reconciliation key is therefore
    (strategy, symbol, side, quantity, time window) matched against the
    orderbook.

So intent_id is DETERMINISTIC and internal. It cannot be pushed to the broker,
but it can guarantee that the same signal never produces two different intents:
derive it from (strategy_id, symbol, signal_bar_timestamp, direction) and a
retry of the same signal collides with the row already written instead of
creating a second one.

Write ordering is the whole safety property:

    write the intent  ->  send the order  ->  write the result

A crash between the first and second leaves a PENDING_ENTRY row with no broker
order id, which reconciliation resolves against the orderbook. A crash after the
second leaves the same row and the same resolution. What must never happen is
sending first and writing after, because then a crash leaves a live position the
agent has no record of.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)


class IntentState(str, Enum):
    """Lifecycle of one trade. See PLAN.md Part 6."""

    FLAT = "flat"
    SIGNAL = "signal"
    PENDING_ENTRY = "pending_entry"          # order sent, no confirmation - the danger
    REJECTED = "rejected"
    PARTIAL = "partial"
    UNKNOWN = "unknown"                      # sent, no answer - reconcile, never retry
    OPEN_UNPROTECTED = "open_unprotected"    # filled, stop not live - the emergency
    OPEN = "open"
    PENDING_EXIT = "pending_exit"
    CLOSED = "closed"
    ABANDONED = "abandoned"                  # never reached the broker

    @property
    def is_terminal(self) -> bool:
        return self in (IntentState.CLOSED, IntentState.REJECTED, IntentState.ABANDONED)

    @property
    def holds_position(self) -> bool:
        """States in which a real position may exist at the broker.

        UNKNOWN is included deliberately. Not knowing is not the same as not
        holding, and treating it as flat is how an agent abandons a live
        position.
        """
        return self in (
            IntentState.OPEN,
            IntentState.OPEN_UNPROTECTED,
            IntentState.PENDING_EXIT,
            IntentState.PARTIAL,
            IntentState.UNKNOWN,
        )


@dataclass
class Intent:
    """One trade, from signal to flat."""

    intent_id: str
    trading_date: date
    strategy_id: str
    strategy_version: str
    mandate_version: str
    symbol: str
    exchange: str
    side: str                       # BUY or SELL
    signal_bar_ts: datetime
    planned_qty: int = 0
    planned_entry: float = 0.0
    planned_stop: float = 0.0
    risk_amount: float = 0.0
    risk_fraction_used: float = 0.0
    state: IntentState = IntentState.SIGNAL
    state_history: list[dict[str, Any]] = field(default_factory=list)
    entry_order_id: str = ""
    fill_price: float = 0.0
    fill_qty: int = 0
    fill_ts: datetime | None = None
    stop_order_id: str = ""
    stop_price: float = 0.0
    exit_order_id: str = ""
    exit_price: float = 0.0
    exit_qty: int = 0
    exit_ts: datetime | None = None
    exit_reason: str = ""
    gross_pnl: float = 0.0
    costs: float = 0.0
    net_pnl: float = 0.0
    r_multiple: float = 0.0
    note: str = ""

    def transition(self, new_state: IntentState, reason: str = "") -> None:
        self.state_history.append(
            {
                "from": self.state.value,
                "to": new_state.value,
                "ts": datetime.now().isoformat(),
                "reason": reason,
            }
        )
        self.state = new_state


def make_intent_id(strategy_id: str, symbol: str, signal_bar_ts: datetime, side: str) -> str:
    """Deterministic id. The same signal can never produce two different ids.

    Not a hash. A readable key is worth more than a short one when someone is
    reading the log at 3pm trying to work out what happened, and collisions are
    the POINT here - a retry must collide.
    """
    stamp = signal_bar_ts.strftime("%Y%m%dT%H%M")
    return f"{strategy_id}:{symbol}:{stamp}:{side.upper()}"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    intent_id         TEXT PRIMARY KEY,
    trading_date      TEXT NOT NULL,
    strategy_id       TEXT NOT NULL,
    strategy_version  TEXT,
    mandate_version   TEXT,
    symbol            TEXT NOT NULL,
    exchange          TEXT,
    side              TEXT NOT NULL,
    signal_bar_ts     TEXT,
    planned_qty       INTEGER,
    planned_entry     REAL,
    planned_stop      REAL,
    risk_amount       REAL,
    risk_fraction_used REAL,
    state             TEXT NOT NULL,
    state_history     TEXT,
    entry_order_id    TEXT,
    fill_price        REAL,
    fill_qty          INTEGER,
    fill_ts           TEXT,
    stop_order_id     TEXT,
    stop_price        REAL,
    exit_order_id     TEXT,
    exit_price        REAL,
    exit_qty          INTEGER,
    exit_ts           TEXT,
    exit_reason       TEXT,
    gross_pnl         REAL,
    costs             REAL,
    net_pnl           REAL,
    r_multiple        REAL,
    note              TEXT,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intents_date  ON intents(trading_date);
CREATE INDEX IF NOT EXISTS idx_intents_state ON intents(state);
CREATE INDEX IF NOT EXISTS idx_intents_sym   ON intents(trading_date, symbol);
"""

_DT_FIELDS = ("fill_ts", "exit_ts")


class IntentLog:
    """SQLite-backed intent store.

    Every write is committed immediately. Batching would be faster and would also
    mean a crash loses exactly the rows that matter most.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        """Always wrap this in contextlib.closing.

        `with sqlite3.connect(...) as conn` is a TRANSACTION context manager, not
        a closer - it commits or rolls back and leaves the handle open. Under
        CPython the handle is then released by refcounting, which is why this
        does not visibly leak today, but relying on refcounting for resource
        cleanup is wrong and stops being true the moment a connection is captured
        by a traceback, a closure, or a different interpreter.

        Flagged during the step 10 API build.
        """
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    # ----------------------------------------------------------------- writing

    def put(self, intent: Intent) -> None:
        """Insert or update. Called BEFORE the order is sent, and after."""
        row = asdict(intent)
        row["trading_date"] = intent.trading_date.isoformat()
        row["signal_bar_ts"] = intent.signal_bar_ts.isoformat()
        row["state"] = intent.state.value
        row["state_history"] = json.dumps(intent.state_history, default=str)
        for f in _DT_FIELDS:
            row[f] = row[f].isoformat() if row[f] else None
        row["updated_at"] = datetime.now().isoformat()

        cols = ", ".join(row)
        marks = ", ".join(f":{c}" for c in row)
        with self._lock, closing(self._connect()) as conn:
            conn.execute(f"INSERT OR REPLACE INTO intents ({cols}) VALUES ({marks})", row)
            conn.commit()

    def transition(self, intent: Intent, state: IntentState, reason: str = "") -> None:
        """Move state and persist in one step, so the two cannot drift apart."""
        intent.transition(state, reason)
        self.put(intent)
        log.info("intent %s -> %s (%s)", intent.intent_id, state.value, reason or "-")

    # ----------------------------------------------------------------- reading

    def get(self, intent_id: str) -> Intent | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        return _from_row(row) if row else None

    def for_date(self, trading_date: date) -> list[Intent]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM intents WHERE trading_date = ? ORDER BY signal_bar_ts",
                (trading_date.isoformat(),),
            ).fetchall()
        return [_from_row(r) for r in rows]

    def unresolved(self, trading_date: date) -> list[Intent]:
        """Intents that may correspond to a real position or a live order.

        This is what reconciliation reads on every wake. Anything here must be
        matched against the broker before the agent makes a single new decision.
        """
        live = [s.value for s in IntentState if not s.is_terminal and s is not IntentState.SIGNAL]
        marks = ",".join("?" * len(live))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM intents WHERE trading_date = ? AND state IN ({marks})",
                (trading_date.isoformat(), *live),
            ).fetchall()
        return [_from_row(r) for r in rows]

    def open_positions(self, trading_date: date) -> list[Intent]:
        held = [s.value for s in IntentState if s.holds_position]
        marks = ",".join("?" * len(held))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM intents WHERE trading_date = ? AND state IN ({marks})",
                (trading_date.isoformat(), *held),
            ).fetchall()
        return [_from_row(r) for r in rows]

    def closed(self, trading_date: date) -> list[Intent]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM intents WHERE trading_date = ? AND state = ? ORDER BY exit_ts",
                (trading_date.isoformat(), IntentState.CLOSED.value),
            ).fetchall()
        return [_from_row(r) for r in rows]

    def realized_pnl(self, trading_date: date) -> float:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(net_pnl), 0) AS total FROM intents "
                "WHERE trading_date = ? AND state = ?",
                (trading_date.isoformat(), IntentState.CLOSED.value),
            ).fetchone()
        return float(row["total"] or 0.0)

    def counts_by_state(self, trading_date: date) -> dict[str, int]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM intents WHERE trading_date = ? GROUP BY state",
                (trading_date.isoformat(),),
            ).fetchall()
        return {r["state"]: int(r["n"]) for r in rows}


def _from_row(row: sqlite3.Row) -> Intent:
    data = dict(row)
    data.pop("updated_at", None)
    data["trading_date"] = date.fromisoformat(data["trading_date"])
    data["signal_bar_ts"] = datetime.fromisoformat(data["signal_bar_ts"])
    for f in _DT_FIELDS:
        data[f] = datetime.fromisoformat(data[f]) if data[f] else None
    data["state"] = IntentState(data["state"])
    data["state_history"] = json.loads(data["state_history"] or "[]")
    for f in ("planned_qty", "fill_qty", "exit_qty"):
        data[f] = int(data[f] or 0)
    for f in (
        "planned_entry", "planned_stop", "risk_amount", "risk_fraction_used",
        "fill_price", "stop_price", "exit_price", "gross_pnl", "costs",
        "net_pnl", "r_multiple",
    ):
        data[f] = float(data[f] or 0.0)
    for f in (
        "strategy_version", "mandate_version", "exchange", "entry_order_id",
        "stop_order_id", "exit_order_id", "exit_reason", "note",
    ):
        data[f] = data[f] or ""
    return Intent(**data)


def summarize(intents: Iterable[Intent]) -> dict[str, Any]:
    """Compact snapshot for the live view and the end-of-day report."""
    items = list(intents)
    closed = [i for i in items if i.state is IntentState.CLOSED]
    return {
        "total": len(items),
        "closed": len(closed),
        "open": sum(1 for i in items if i.state.holds_position),
        "realized_pnl": sum(i.net_pnl for i in closed),
        "wins": sum(1 for i in closed if i.net_pnl > 0),
        "losses": sum(1 for i in closed if i.net_pnl < 0),
        "states": {s.value: sum(1 for i in items if i.state is s) for s in IntentState
                   if any(i.state is s for i in items)},
    }
