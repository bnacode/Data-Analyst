"""current_time: the date is read when needed, so long-running sessions never go stale."""

from __future__ import annotations

from datetime import datetime, timezone

from strands import tool


@tool
def current_time() -> str:
    """Current date and time in UTC. Call it before resolving relative dates like "last month" or "this year"."""
    return datetime.now(timezone.utc).strftime("%A %Y-%m-%d %H:%M UTC")
