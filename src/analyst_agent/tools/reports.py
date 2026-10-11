"""Tools over the saved reports library. The owner comes from the session, never from the LLM; deletion is only proposed."""

from __future__ import annotations

from strands import tool

from analyst_agent.reports.deletion import Deletions
from analyst_agent.reports.store import ReportStore
from analyst_agent.session import SessionContext


def make_report_tools(store: ReportStore, session: SessionContext) -> list:
    """list_reports and read_report, scoped to the session user."""

    @tool
    def list_reports(term: str = "") -> str:
        """List the user's saved reports, newest first.

        Args:
            term: Optional keyword (e.g. a brand or topic) matched against title, summary, body and tags.
        """
        reports = store.list(session.user_id, term)
        if not reports:
            return f"No saved reports matching {term!r}." if term else "No saved reports yet."
        return "\n".join(report.line() for report in reports)

    @tool
    def read_report(report_id: int) -> str:
        """Full text of one of the user's saved reports.

        Args:
            report_id: The report number, e.g. 3 for report #3.
        """
        report = store.get(session.user_id, report_id)
        return report.markdown() if report else f"There is no report #{report_id} in this user's library."

    return [list_reports, read_report]


def make_propose_deletion(deletions: Deletions):
    """Builds propose_deletion; it only stages a deletion for the user to confirm."""

    @tool
    def propose_deletion(report_ids: list[int] | None = None, term: str = "", this_conversation: bool = False) -> str:
        """Propose deleting some of the user's saved reports. Nothing is deleted until the user confirms in the app.

        Args:
            report_ids: Specific report numbers, e.g. [3, 5].
            term: A word or name the reports mention, e.g. a brand like "Nike". Whole words only.
            this_conversation: True to select the reports created in this conversation.
        """
        if not (report_ids or term.strip() or this_conversation):
            return "Give at least one filter: report_ids, term or this_conversation."
        pending = deletions.propose(report_ids or (), term, this_conversation)
        if pending is None:
            return "No saved reports match, so nothing will be deleted. Tell the user."
        return (
            f"{len(pending.ids)} report(s) are awaiting the user's confirmation in the app. "
            "They are NOT deleted yet. Do not say they were deleted; tell the user to confirm below."
        )

    return propose_deletion
