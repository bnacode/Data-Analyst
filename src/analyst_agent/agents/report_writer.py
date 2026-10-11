"""Report Writer subagent: gathers what it needs from the analyst, writes the report; code saves it."""

from __future__ import annotations

from pathlib import Path

import structlog
import yaml
from pydantic import BaseModel, Field
from strands import Agent, tool

from analyst_agent.agents.data_analyst import make_ask_data_analyst
from analyst_agent.agents.models import MODEL_DOWN, error_cause, gemini_model, model_unavailable
from analyst_agent.prompts.report_writer import REPORT_WRITER_PROMPT
from analyst_agent.reports.store import ReportStore
from analyst_agent.tools.clock import current_time
from analyst_agent.tools.result import tool_error
from analyst_agent.tools.sql import DataAccess

log = structlog.get_logger(__name__)

DEFAULT_PERSONA = "Clear, concise and practical. Lead with the most important insight."
MAX_NOTEBOOK_ENTRIES = 10


class Report(BaseModel):
    """Structured output of the Report Writer; code saves it, the model never writes files."""

    title: str = Field(description="Short, specific title, e.g. 'Q1 2026 Sales Review'.")
    summary: str = Field(description="One or two sentences with the headline result.")
    body: str = Field(description="Markdown with Key insights, Action items and Caveats sections.")
    tags: list[str] = Field(description="3-6 short lowercase topics: period, brands, metrics.")


def load_persona(path: Path) -> str:
    """Read on every report, so edits apply without a restart. A broken file never blocks a report."""
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))["instructions"].strip()
    except (OSError, yaml.YAMLError, TypeError, KeyError, AttributeError):
        log.warning("persona_unavailable", path=str(path))
        return DEFAULT_PERSONA


def make_write_report(access: DataAccess, store: ReportStore):
    """Builds the write_report tool, which runs the Report Writer subagent and saves its report."""
    settings = access.settings
    session = access.session

    @tool
    def write_report(request: str) -> str | dict:
        """Write an executive report with insights and action items, and save it to the user's library.

        Args:
            request: What the report should cover, e.g. "Q1 sales performance with action items for Q2".
        """
        prompt = REPORT_WRITER_PROMPT.format(
            scope=session.scope.describe(),
            max_calls=settings.max_report_analyses,
            persona=load_persona(settings.config_dir / "persona.yaml"),
        )
        # Findings carry database values, so they go in the user turn, never the system prompt
        findings = "\n\n".join(session.notebook[-MAX_NOTEBOOK_ENTRIES:]) or "None yet."
        writer = Agent(
            name="report_writer",
            model=gemini_model(settings, settings.report_model),
            system_prompt=prompt,
            tools=[current_time, make_ask_data_analyst(access, max_calls=settings.max_report_analyses)],
            callback_handler=None,
            retry_strategy=None,
            hooks=[access.metrics],
        )
        try:
            result = writer(
                f"{request}\n\n## Findings gathered so far in this conversation\n{findings}",
                structured_output_model=Report,
                limits={"turns": settings.max_report_analyses + 3},
            )
        except Exception as exc:
            if not model_unavailable(exc):
                raise
            log.warning("model_unavailable", agent="report_writer", error=error_cause(exc))
            return tool_error(MODEL_DOWN)
        report = result.structured_output
        if report is None:
            log.warning("report_failed")
            return tool_error("The report could not be written. Tell the user and suggest a narrower request.")
        saved = store.save(session.user_id, session.thread_id, report.title, report.summary, report.body, report.tags)
        log.info("report_saved", report_id=saved.id)
        return f"Saved as report #{saved.id}: {saved.title}\n{saved.summary}\nFull report: {store.file(saved.id)}"

    return write_report
