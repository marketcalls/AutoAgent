"""The session clock. Everything the executor does hangs off this.

PLAN.md Part 6. The schedule is the agent: there is no event loop deciding what
to do, only a clock deciding which phase it is in.

    08:45  plan      backtest, regime, select, publish for approval
    09:15  observe   executor starts, plan LOCKED, no entries yet
    09:30  trade     entries allowed
    14:45  manage    no NEW entries; existing positions still managed
    15:10  squareoff force flat at market
    15:35  report    reconcile, metrics, journal

Two facts that constrain the times, both measured rather than assumed:

    The first 5-minute bar closes at 09:20, so no signal can exist before then
    and START_TIME cannot be earlier.

    The OpenAlgo sandbox rejects MIS orders after 15:15 IST, and brokers
    auto-square-off around then at a price you do not choose. 15:10 stays clear
    of both.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from enum import Enum

from ..config import Settings

# The first 5m bar of an Indian equity session closes at 09:20. Nothing can
# legitimately signal before it.
FIRST_BAR_CLOSE = time(9, 20)
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


class Phase(str, Enum):
    BEFORE = "before"        # before the market opens
    PLAN = "plan"            # pre-open planning window
    OBSERVE = "observe"      # open, plan locked, no entries yet
    TRADE = "trade"          # entries allowed
    MANAGE = "manage"        # no new entries, manage what is open
    SQUAREOFF = "squareoff"  # force flat
    REPORT = "report"        # after the close
    CLOSED = "closed"

    @property
    def allows_entries(self) -> bool:
        return self is Phase.TRADE

    @property
    def is_session(self) -> bool:
        return self in (Phase.OBSERVE, Phase.TRADE, Phase.MANAGE, Phase.SQUAREOFF)


class SessionClock:
    """Answers "what phase is it" and nothing else.

    Deliberately takes the time as an argument rather than reading the wall
    clock, so a whole session can be replayed through the executor deterministically.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.plan_at = time(8, 45)
        self.open_at = MARKET_OPEN
        self.start_at = max(settings.start_time, FIRST_BAR_CLOSE)
        self.end_at = settings.end_time
        self.squareoff_at = settings.squareoff_time
        self.report_at = time(15, 35)

    def phase(self, now: datetime) -> Phase:
        t = now.time()
        if t < self.plan_at:
            return Phase.BEFORE
        if t < self.open_at:
            return Phase.PLAN
        if t < self.start_at:
            return Phase.OBSERVE
        if t < self.end_at:
            return Phase.TRADE
        if t < self.squareoff_at:
            return Phase.MANAGE
        if t < self.report_at:
            return Phase.SQUAREOFF
        if t <= MARKET_CLOSE.replace(hour=23, minute=59):
            return Phase.REPORT
        return Phase.CLOSED

    def is_bar_close(self, now: datetime, minutes: int = 5) -> bool:
        """True on a 5-minute boundary aligned to the 09:15 open.

        Aligned to the OPEN, not to the hour. Indian equity sessions start at
        09:15, so bars close at :20, :25, :30 and so on - a naive modulo on the
        wall clock would fire on :00 and :05 and be five minutes out all day.
        """
        if not self.is_open(now):
            return False
        opened = datetime.combine(now.date(), self.open_at)
        elapsed = int((now - opened).total_seconds() // 60)
        return elapsed > 0 and elapsed % minutes == 0

    def is_open(self, now: datetime) -> bool:
        return self.open_at <= now.time() <= MARKET_CLOSE

    def next_bar_close(self, now: datetime, minutes: int = 5) -> datetime:
        opened = datetime.combine(now.date(), self.open_at)
        if now < opened:
            return opened + timedelta(minutes=minutes)
        elapsed = (now - opened).total_seconds() / 60.0
        nxt = (int(elapsed // minutes) + 1) * minutes
        return opened + timedelta(minutes=nxt)

    def describe(self, now: datetime) -> str:
        p = self.phase(now)
        return (
            f"{now.strftime('%H:%M')} {p.value}: "
            f"trade {self.start_at:%H:%M}-{self.end_at:%H:%M}, "
            f"flat {self.squareoff_at:%H:%M}"
        )
