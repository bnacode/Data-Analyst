"""Two-step deletion: the agent proposes, the user confirms in the app, code deletes exactly the stored ids."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog

from analyst_agent.reports.store import ReportStore
from analyst_agent.session import SessionContext

log = structlog.get_logger(__name__)

TTL = timedelta(minutes=5)
BULK = 5  # more than this and the user must type the count
YES = ("yes", "y")


@dataclass(frozen=True)
class PendingDeletion:
    """Reports awaiting confirmation. Larger batches need the count typed instead of 'yes'."""

    ids: tuple[int, ...]
    lines: tuple[str, ...]
    expires_at: datetime

    @property
    def needs_count(self) -> bool:
        return len(self.ids) > BULK

    def confirmed_by(self, answer: str) -> bool:
        answer = answer.strip().lower()
        return answer == str(len(self.ids)) if self.needs_count else answer in YES


class Deletions:
    """At most one pending deletion per session. Only confirm() deletes, and only the ids the user saw."""

    def __init__(self, store: ReportStore, session: SessionContext, ttl: timedelta = TTL) -> None:
        self.store = store
        self.session = session
        self.ttl = ttl
        self.pending: PendingDeletion | None = None

    def propose(self, ids: Collection[int] = (), term: str = "", this_conversation: bool = False) -> PendingDeletion | None:
        """Stages the matching reports for deletion, replacing any earlier proposal."""
        self.cancel()  # a new proposal replaces the old one
        thread_id = self.session.thread_id if this_conversation else None
        reports = self.store.find(self.session.user_id, ids, term, thread_id)
        if not reports:
            return None
        expires_at = datetime.now(timezone.utc) + self.ttl
        self.pending = PendingDeletion(tuple(r.id for r in reports), tuple(r.line() for r in reports), expires_at)
        self._audit("deletion_proposed", self.pending.ids)
        return self.pending

    def confirm(self) -> str:
        """Deletes exactly the staged reports if the proposal hasn't expired."""
        pending, self.pending = self.pending, None
        if pending is None:
            return "There is no deletion waiting for confirmation."
        if self._expired(pending):
            self._audit("deletion_expired", pending.ids)
            return "The deletion request expired, so nothing was deleted. Please ask again."
        deleted = self.store.delete(self.session.user_id, pending.ids)
        self._audit("deletion_executed", deleted)
        if not deleted:
            return "Nothing was deleted: those reports no longer exist."
        return f"Deleted {len(deleted)} report(s): {', '.join(f'#{i}' for i in deleted)}."

    def cancel(self) -> str | None:
        pending, self.pending = self.pending, None
        if pending is None:
            return None
        self._audit("deletion_expired" if self._expired(pending) else "deletion_cancelled", pending.ids)
        return "Deletion cancelled. Nothing was deleted."

    def _expired(self, pending: PendingDeletion) -> bool:
        return datetime.now(timezone.utc) >= pending.expires_at

    def _audit(self, event: str, ids: Collection[int]) -> None:
        self.store.audit(self.session.user_id, event, ids)
        log.info(event, report_ids=list(ids))
