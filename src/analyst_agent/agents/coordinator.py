"""Coordinator agent: talks to the user and routes work to tools and subagents."""

from __future__ import annotations

from strands import Agent

from analyst_agent.agents.data_analyst import make_ask_data_analyst
from analyst_agent.agents.models import gemini_model
from analyst_agent.prompts.coordinator import COORDINATOR_PROMPT
from analyst_agent.agents.report_writer import make_write_report
from analyst_agent.reports.deletion import Deletions
from analyst_agent.reports.store import ReportStore
from analyst_agent.tools.reports import make_propose_deletion, make_report_tools
from analyst_agent.tools.schema import make_describe_schema
from analyst_agent.tools.sql import DataAccess


def build_coordinator(access: DataAccess, store: ReportStore, deletions: Deletions) -> Agent:
    """The user-facing agent with all tools, bound to the signed-in user's session."""
    settings = access.settings
    return Agent(
        name="coordinator",
        model=gemini_model(settings, settings.coordinator_model),
        system_prompt=COORDINATOR_PROMPT.format(scope=access.session.scope.describe()),
        tools=[
            make_describe_schema(access.policy),
            make_ask_data_analyst(access),
            make_write_report(access, store),
            *make_report_tools(store, access.session),
            make_propose_deletion(deletions),
        ],
        callback_handler=None,
        retry_strategy=None,
        hooks=[access.metrics],
    )
