"""The store's one source of the current time.

Every timestamp the store writes on its own (a job's start time, a project claim)
comes from `now()`, so tests fix it with one fixture instead of extra parameters.
"""

from __future__ import annotations

from datetime import datetime, timezone


def now() -> datetime:
    """The current time, aware, in UTC."""
    return datetime.now(timezone.utc)
