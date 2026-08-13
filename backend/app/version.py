"""AutoAgent version.

This file is the single source of truth for version information (PLAN.md Part 10).
Nothing else in the tree may hard-code a version string: the intent log records the
running version against every trade, so a second copy that drifts would misattribute
which code produced which position.
"""

from __future__ import annotations

VERSION = "0.1.0"


def get_version() -> str:
    """Return the current AutoAgent version.

    Returns:
        str: The current version string (e.g. '0.1.0').
    """
    return VERSION
