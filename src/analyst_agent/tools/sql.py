"""run_sql: the only way the agent reads data. Guard, then BigQuery, then masker."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import structlog
from google.api_core.exceptions import BadRequest, GoogleAPIError
from strands import tool

from analyst_agent.config import Settings
from analyst_agent.data.bq_client import BigQueryRunner
from analyst_agent.observability.metrics import SessionMetrics
from analyst_agent.safety.masker import Pseudonymizer, mask_result
from analyst_agent.safety.policy import DataPolicy
from analyst_agent.safety.sql_guard import SqlRejected, guard_sql
from analyst_agent.session import SessionContext
from analyst_agent.tools.result import tool_error

log = structlog.get_logger(__name__)

EMPTY_HINT = "0 rows. If you suspect a typo in a filter value, check it once; otherwise report zero."
SCOPED_EMPTY_HINT = (
    "0 rows. This user only sees {scope}. If the question is about anything outside that, "
    "answer that it is outside their access, not zero. Otherwise report zero."
)


@dataclass(frozen=True)
class DataAccess:
    """Everything a query needs, built by code for the signed-in user."""

    settings: Settings
    policy: DataPolicy
    session: SessionContext
    runner: BigQueryRunner
    pseudonymizer: Pseudonymizer
    metrics: SessionMetrics = field(default_factory=SessionMetrics)


@dataclass(frozen=True)
class QueryResult:
    """Text for the model, and whether the query failed."""

    text: str
    failed: bool = False


def execute_sql(sql: str, access: DataAccess) -> QueryResult:
    """Guards, runs and masks one query. Never raises: failures come back as text for the model."""
    settings = access.settings
    sql = " ".join(sql.split())
    try:
        guarded = guard_sql(sql, access.policy, access.session.scope, settings.max_result_rows, settings.min_group_size)
    except SqlRejected as exc:
        log.info("sql_rejected", reason=str(exc), sql=sql)
        return QueryResult(f"Rejected: {exc}", failed=True)

    started = time.perf_counter()
    try:
        frame = access.runner.run(guarded.sql)
    except BadRequest as exc:
        message = exc.errors[0]["message"] if exc.errors else exc.message
        # Error positions refer to the query that ran, so log that one too
        log.info("sql_failed", error=message, sql=sql, ran=guarded.sql)
        return QueryResult(f"BigQuery error: {message}", failed=True)
    except (GoogleAPIError, TimeoutError) as exc:
        log.warning("database_unavailable", error=type(exc).__name__, sql=sql)
        return QueryResult("The database is unavailable right now. Stop and tell the user to try again shortly.", failed=True)

    masked = mask_result(frame, guarded, access.pseudonymizer, access.policy.blocked_columns(), settings.min_group_size)
    log.info("sql_executed", rows=len(masked.frame), duration_ms=round((time.perf_counter() - started) * 1000), sql=sql)
    lines = [*masked.notes]
    if frame.empty:
        scope = access.session.scope
        lines.append(EMPTY_HINT if scope.all_data else SCOPED_EMPTY_HINT.format(scope=scope.describe()))
    elif not masked.frame.empty:
        capped = " (capped)" if len(frame) >= settings.max_result_rows else ""
        lines += [f"{len(masked.frame)} rows{capped}:", masked.frame.to_csv(index=False, float_format="%.2f")]
    return QueryResult("\n".join(lines))


def make_run_sql(access: DataAccess):
    """A fresh tool per question, so the budgets cover one question: failed queries end it sooner than good ones."""
    settings = access.settings
    queries = failures = 0
    seen: set[str] = set()

    @tool
    def run_sql(sql: str) -> str | dict:
        """Run one BigQuery SELECT on the available tables and return the result as CSV.

        Args:
            sql: A single BigQuery Standard SQL SELECT statement.
        """
        nonlocal queries, failures
        normalized = " ".join(sql.split())
        if normalized in seen:
            return tool_error("You already ran this exact query. Change it, or stop and report what you found.")
        if failures >= settings.max_sql_failures or queries >= settings.max_sql_queries:
            log.info("sql_budget_exhausted", queries=queries, failures=failures, sql=normalized)
            return tool_error("Query budget used up. Stop and report what you found, or what you tried and why it did not work.")
        seen.add(normalized)
        queries += 1
        result = execute_sql(sql, access)
        failures += result.failed
        return tool_error(result.text) if result.failed else result.text

    return run_sql
