"""Data Analyst subagent: turns one question into SQL, runs it, returns structured findings."""

from __future__ import annotations

import structlog
from pydantic import BaseModel, Field
from strands import Agent, tool

from analyst_agent.agents.models import MODEL_DOWN, error_cause, gemini_model, model_unavailable
from analyst_agent.prompts.data_analyst import DATA_ANALYST_PROMPT
from analyst_agent.tools.clock import current_time
from analyst_agent.tools.result import tool_error
from analyst_agent.tools.schema import render_schema
from analyst_agent.tools.sql import DataAccess, make_run_sql

log = structlog.get_logger(__name__)


class Findings(BaseModel):
    """What the Data Analyst returns for one question."""

    answer: str = Field(description="Direct answer in 1-3 sentences, with the key numbers.")
    table: str = Field("", description="Key result rows as a small markdown table, if useful.")
    sql: str = Field("", description="The final SQL that produced the answer.")
    caveats: list[str] = Field(default_factory=list, description="Assumptions, privacy notes, or why it could not be answered.")


INCOMPLETE = Findings(
    answer="The analysis could not be completed.", caveats=["The step budget ran out before an answer was found."]
)


def make_ask_data_analyst(access: DataAccess, max_calls: int | None = None):
    """Builds the ask_data_analyst tool; `max_calls` caps how often one agent may call it."""
    settings = access.settings
    schema = render_schema(access.policy)
    calls = 0

    @tool
    def ask_data_analyst(question: str) -> str | dict:
        """Answer one data question by querying the database.

        Args:
            question: One focused, standalone question, e.g. "Monthly revenue in 2024 by department".
        """
        nonlocal calls
        if max_calls is not None and calls >= max_calls:
            return tool_error("Analysis budget used up. Continue with the findings you have.")
        calls += 1
        analyst = Agent(
            name="data_analyst",
            model=gemini_model(settings, settings.analyst_model),
            system_prompt=DATA_ANALYST_PROMPT.format(schema=schema, max_queries=settings.max_sql_queries),
            tools=[current_time, make_run_sql(access)],
            callback_handler=None,
            retry_strategy=None,
            hooks=[access.metrics],
        )
        try:
            # One turn per query, plus room for current_time, a refused query and the findings
            result = analyst(question, structured_output_model=Findings, limits={"turns": settings.max_sql_queries + 3})
        except Exception as exc:
            if not model_unavailable(exc):
                raise
            log.warning("model_unavailable", agent="data_analyst", error=error_cause(exc))
            return tool_error(MODEL_DOWN)
        findings = result.structured_output
        if findings is None:
            log.warning("analysis_incomplete", question=question)
            return tool_error(INCOMPLETE.model_dump_json())
        log.info("findings", question=question, answer=findings.answer, caveats=findings.caveats)
        access.session.notebook.append(f"Question: {question}\nFindings: {findings.model_dump_json()}")
        return findings.model_dump_json()

    return ask_data_analyst
