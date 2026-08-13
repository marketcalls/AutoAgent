"""The daily loss budget and the circuit breakers.

PLAN.md Part 5. The distinction this module exists to enforce:

    A tripwire asks "have I lost 2% yet" AFTER the fact.
    A budget asks, BEFORE every entry, "can I afford to be wrong about this one".

At -1.8% against a 2% limit, a trade risking 0.5% reaches -2.3% in the worst
case. The tripwire permits it and fires afterwards. The budget refuses it. That
is the entire difference, and it is why `can_open` takes the proposed trade's
worst-case loss rather than being a property that reads back the current P&L.

Three facts that shape the implementation:

1. Loss means realized PLUS unrealized, marked continuously. Counting only
   realized P&L lets the agent sit deeply underwater on open positions while the
   limit never fires.

2. Everything is measured against ALLOCATION, fixed. Not current equity, which
   would shrink the limit as losses accrue, and not broker funds, which on a
   funded account is an order of magnitude too generous.

3. A halt is STICKY. It is written to disk before it is announced, so a process
   restart comes back halted rather than fresh. An agent that forgets it stopped
   is worse than one that never stopped.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path

from ..config import Settings

log = logging.getLogger(__name__)


class RunState(str, Enum):
    """Session-level execution state. Ordered from most to least permissive."""

    RUNNING = "running"
    PAUSED = "paused"            # temporary, clears on its own
    REDUCE_ONLY = "reduce_only"  # may close, may not open
    HALTED = "halted"            # flat and done for the session

    @property
    def may_open(self) -> bool:
        return self is RunState.RUNNING

    @property
    def may_close(self) -> bool:
        return self is not RunState.HALTED


@dataclass(frozen=True)
class Decision:
    """Why a trade was allowed or refused. Written to the intent log verbatim.

    `code` is machine-readable and stable; `reason` is for the human reading the
    end-of-day report, and names the number that caused the refusal.
    """

    allowed: bool
    code: str = ""
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


ALLOW = Decision(True)


@dataclass
class Position:
    """An open position, for marking the book to market."""

    symbol: str
    quantity: int
    entry_price: float
    stop_price: float
    side: str  # BUY or SELL

    def unrealized(self, last_price: float) -> float:
        if last_price <= 0 or self.quantity == 0:
            return 0.0
        direction = 1 if self.side.upper() == "BUY" else -1
        return (last_price - self.entry_price) * self.quantity * direction

    def worst_case_remaining(self) -> float:
        """Loss still on the table if the stop fills from here."""
        return abs(self.entry_price - self.stop_price) * abs(self.quantity)


@dataclass
class RiskBudget:
    """Session-scoped budget and breaker state.

    Not a singleton. The executor owns exactly one for the trading day and passes
    it to every symbol's state machine, because the budget is shared across the
    basket while the machines are per symbol.
    """

    settings: Settings
    trading_date: date
    realized_pnl: float = 0.0
    trade_count: int = 0
    consecutive_losses: int = 0
    state: RunState = RunState.RUNNING
    halt_reason: str = ""
    paused_until: datetime | None = None
    open_positions: dict[str, Position] = field(default_factory=dict)

    # Whether a halt is written to disk. TRUE for a live session, where a halt
    # must survive a restart. FALSE for a backtest.
    #
    # MEASURED BUG, step 8: the portfolio replay constructs one RiskBudget per
    # simulated session using the real Settings, so every backtest session that
    # hit three consecutive losses wrote a halt file into the LIVE data
    # directory. 56 of them accumulated, and the live executor then refused to
    # start with "restored halt from disk: 3 consecutive losses" - halted by a
    # backtest it ran yesterday. A simulation must never be able to stop the
    # real thing.
    persist: bool = True

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ------------------------------------------------------------------ marks

    def unrealized_pnl(self, marks: dict[str, float]) -> float:
        return sum(p.unrealized(marks.get(sym, 0.0)) for sym, p in self.open_positions.items())

    def mtm_pnl(self, marks: dict[str, float]) -> float:
        """Realized plus unrealized. This is what the limit is measured against."""
        return self.realized_pnl + self.unrealized_pnl(marks)

    def remaining_budget(self, marks: dict[str, float]) -> float:
        """Rupees of loss still affordable today. Never negative."""
        limit = self.settings.daily_loss_limit_amount
        loss = -min(0.0, self.mtm_pnl(marks))
        return max(0.0, limit - loss)

    def budget_used_pct(self, marks: dict[str, float]) -> float:
        limit = self.settings.daily_loss_limit_amount
        if limit <= 0:
            return 0.0
        loss = -min(0.0, self.mtm_pnl(marks))
        return 100.0 * loss / limit

    # ------------------------------------------------------------ the gateway

    def can_open(
        self,
        symbol: str,
        worst_case_loss: float,
        marks: dict[str, float],
        *,
        now: datetime | None = None,
    ) -> Decision:
        """The single gate every entry passes through.

        Checks are ordered cheapest first, and none of them touches the network.
        """
        now = now or datetime.now()

        if self.settings.kill_switch_engaged():
            return Decision(False, "kill_switch", "the kill switch file is present")

        if not self.settings.trading_enabled:
            return Decision(False, "trading_disabled", "TRADING_ENABLED is false")

        self._expire_pause(now)

        if self.state is RunState.HALTED:
            return Decision(False, "halted", f"session halted: {self.halt_reason}")
        if self.state is RunState.REDUCE_ONLY:
            return Decision(False, "reduce_only", "reduce-only: positions may close, not open")
        if self.state is RunState.PAUSED:
            until = self.paused_until.strftime("%H:%M") if self.paused_until else "?"
            return Decision(False, "paused", f"paused after consecutive losses until {until}")

        if symbol in self.open_positions:
            return Decision(False, "already_open", f"already holding {symbol}")

        if len(self.open_positions) >= self.settings.max_concurrent_positions:
            return Decision(
                False, "position_cap",
                f"{len(self.open_positions)} positions open, cap is "
                f"{self.settings.max_concurrent_positions}",
            )

        if self.trade_count >= self.settings.max_trades_per_day:
            return Decision(
                False, "trade_cap",
                f"{self.trade_count} trades today, cap is {self.settings.max_trades_per_day}",
            )

        sector = self.settings.sector_of(symbol)
        held = sum(1 for s in self.open_positions if self.settings.sector_of(s) == sector)
        if held >= self.settings.max_per_sector:
            return Decision(
                False, "sector_cap",
                f"{held} positions already in sector {sector}, cap is "
                f"{self.settings.max_per_sector}",
            )

        # The budget check itself. Worst case is the loss ALREADY on the table
        # across open positions plus the loss this trade would add, because the
        # stops can all fill on the same adverse move. Summing only realized P&L
        # here would understate the exposure exactly when it matters most.
        remaining = self.remaining_budget(marks)
        committed = sum(p.worst_case_remaining() for p in self.open_positions.values())
        if worst_case_loss + committed > remaining:
            return Decision(
                False, "budget",
                f"worst case {worst_case_loss:.0f} plus {committed:.0f} already "
                f"committed exceeds the {remaining:.0f} of daily budget remaining",
            )

        return ALLOW

    # -------------------------------------------------------------- accounting

    def open_position(self, position: Position) -> None:
        with self._lock:
            self.open_positions[position.symbol] = position

    def close_position(self, symbol: str, net_pnl: float, *, now: datetime | None = None) -> None:
        """Record a closed trade and update the streak.

        net_pnl is NET OF COSTS. A trade 50 rupees up gross that paid 80 in
        brokerage is a loss, and counting it as a win would let a losing streak
        run past its limit.
        """
        now = now or datetime.now()
        with self._lock:
            self.open_positions.pop(symbol, None)
            self.realized_pnl += net_pnl
            self.trade_count += 1

            # The scratch band stops noise from resetting the streak counter.
            # Without it a near-flat trade reads as a win and a run of three real
            # losses never registers.
            scratch = self.settings.scratch_band_r * self.settings.risk_amount_per_trade
            if net_pnl < -scratch:
                self.consecutive_losses += 1
            elif net_pnl > scratch:
                self.consecutive_losses = 0
            # Inside the band: neither win nor loss, streak untouched.

        self._apply_breakers(now)

    # ---------------------------------------------------------------- breakers

    def check_breakers(self, marks: dict[str, float], *, now: datetime | None = None) -> Decision:
        """Continuous check. Call on every bar close, not only after a trade.

        An open position can breach the daily limit without any trade closing,
        which is the case a close-only breaker check would miss entirely.
        """
        now = now or datetime.now()
        if self.state is RunState.HALTED:
            return Decision(False, "halted", self.halt_reason)

        if self.remaining_budget(marks) <= 0:
            self.halt(f"daily loss limit reached, MTM {self.mtm_pnl(marks):.0f}", now=now)
            return Decision(False, "daily_limit", self.halt_reason)

        return ALLOW

    def _apply_breakers(self, now: datetime) -> None:
        halt_at = self.settings.consecutive_loss_halt
        pause_at = self.settings.consecutive_loss_pause

        if halt_at > 0 and self.consecutive_losses >= halt_at:
            self.halt(f"{self.consecutive_losses} consecutive losses", now=now)
        elif pause_at > 0 and self.consecutive_losses >= pause_at:
            self.pause(minutes=self.settings.pause_minutes, now=now)

    def pause(self, *, minutes: int, now: datetime | None = None) -> None:
        now = now or datetime.now()
        if self.state is not RunState.RUNNING:
            return
        self.state = RunState.PAUSED
        self.paused_until = now + timedelta(minutes=minutes)
        log.warning(
            "PAUSED for %d minutes after %d consecutive losses, resumes %s",
            minutes, self.consecutive_losses, self.paused_until.strftime("%H:%M"),
        )

    def _expire_pause(self, now: datetime) -> None:
        if self.state is RunState.PAUSED and self.paused_until and now >= self.paused_until:
            self.state = RunState.RUNNING
            self.paused_until = None
            log.info("pause expired, resuming")

    def reduce_only(self, reason: str) -> None:
        """The state between running and killed.

        Reached on mandate expiry, data degradation, or an operator request.
        Hard-stopping while a position is open traps that position; reduce-only
        lets it be closed and nothing new be started.
        """
        if self.state is RunState.HALTED:
            return
        self.state = RunState.REDUCE_ONLY
        self.halt_reason = reason
        log.warning("REDUCE-ONLY: %s", reason)
        self._persist()

    def halt(self, reason: str, *, now: datetime | None = None) -> None:
        """Stop for the session. Persisted BEFORE it is announced.

        Order matters. Writing the flag first means a crash between the decision
        and the announcement still leaves a halted agent on restart, which is the
        safe direction to fail.
        """
        if self.state is RunState.HALTED:
            return
        self.state = RunState.HALTED
        self.halt_reason = reason
        self._persist()
        log.error("HALTED: %s", reason)

    # ------------------------------------------------------------- persistence

    def _halt_file(self) -> Path:
        return Path(self.settings.db_path).parent / f"halt-{self.trading_date.isoformat()}.json"

    def _persist(self) -> None:
        if not self.persist:
            return
        try:
            self._halt_file().write_text(
                json.dumps(
                    {
                        "trading_date": self.trading_date.isoformat(),
                        "state": self.state.value,
                        "reason": self.halt_reason,
                        "realized_pnl": self.realized_pnl,
                        "trade_count": self.trade_count,
                        "consecutive_losses": self.consecutive_losses,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            # Losing the halt record must never prevent the halt itself. The
            # in-memory state still stops trading for this process.
            log.warning("could not persist halt state", exc_info=True)

    def restore(self) -> bool:
        """Reload a sticky halt for today. Call before the first decision.

        Returns True if a halt was restored, meaning the session must not trade.
        """
        if not self.persist:
            return False
        path = self._halt_file()
        if not path.exists():
            return False
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            log.warning("halt file unreadable, treating as halted for safety", exc_info=True)
            self.state = RunState.HALTED
            self.halt_reason = "unreadable halt file"
            return True

        if saved.get("trading_date") != self.trading_date.isoformat():
            return False

        self.state = RunState(saved.get("state", RunState.HALTED.value))
        self.halt_reason = saved.get("reason", "restored from disk")
        self.realized_pnl = float(saved.get("realized_pnl", 0.0))
        self.trade_count = int(saved.get("trade_count", 0))
        self.consecutive_losses = int(saved.get("consecutive_losses", 0))
        if self.state is not RunState.RUNNING:
            log.error("restored %s from disk: %s", self.state.value, self.halt_reason)
            return True
        return False
