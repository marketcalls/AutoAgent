"""Data layer.

Two stores with different lifetimes, and confusing them is a real bug rather than a
style question (PLAN.md Part 7):

    live frame cache   current session, 60s TTL, in-memory   app/openalgo/frames.py
    historical store   completed sessions, immutable, disk   app/data/bars.py

Only completed sessions are ever written to disk. A session in progress is served by
the live cache, which is why nothing here has a TTL.
"""

from __future__ import annotations

from .bars import (
    BarStore,
    append_session,
    clean_frame,
    ensure_history,
    get_bar_store,
    get_frame,
    last_stored_session,
)

__all__ = [
    "BarStore",
    "append_session",
    "clean_frame",
    "ensure_history",
    "get_bar_store",
    "get_frame",
    "last_stored_session",
]
