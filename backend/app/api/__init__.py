"""HTTP surface package.

Holds the pydantic bodies for every route in main.py and nothing else. There is no
router here on purpose: the control surface is a handful of endpoints and one SSE
stream, and splitting them across routers would cost more indirection than it saves.

The rule that keeps this package honest: a shape the frontend reads is declared here
first, then produced in main.py. Anything returned as a bare dict is a shape nobody
has agreed to, and it is how the UI ends up rendering a field the backend renamed.
"""

from __future__ import annotations
