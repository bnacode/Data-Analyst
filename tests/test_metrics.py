from types import SimpleNamespace

from strands.hooks import AfterInvocationEvent, AfterToolCallEvent
from strands.telemetry.metrics import AgentInvocation, EventLoopMetrics
from strands.tools.structured_output.structured_output_tool import StructuredOutputTool
from structlog.testing import capture_logs

from analyst_agent.observability.metrics import SessionMetrics


def invoke(metrics, agent, input_tokens, output_tokens, model_calls=1):
    """One agent call as Strands records it, then the hook it fires."""
    usage = {"inputTokens": input_tokens, "outputTokens": output_tokens, "totalTokens": input_tokens + output_tokens}
    invocation = AgentInvocation(cycles=[None] * model_calls, usage=usage)
    agent.event_loop_metrics.agent_invocations.append(invocation)
    metrics.on_invocation(AfterInvocationEvent(agent=agent))


def tool_call(metrics, name, status="success", duration=0.5, tool_input=None):
    result = {"toolUseId": "1", "status": status, "content": []}
    metrics.on_tool_call(
        AfterToolCallEvent(
            agent=agent("coordinator"),
            selected_tool=None,
            tool_use={"name": name, "input": tool_input or {}},
            invocation_state={},
            result=result,
            duration=duration,
        )
    )


def agent(name, model="lite"):
    return SimpleNamespace(name=name, model=SimpleNamespace(config={"model_id": model}), event_loop_metrics=EventLoopMetrics())


def test_reused_agent_counts_each_call_once():
    metrics, coordinator = SessionMetrics(), agent("coordinator")
    invoke(metrics, coordinator, 100, 10, model_calls=2)
    invoke(metrics, coordinator, 300, 30)
    assert metrics.tokens["coordinator", "lite"] == {"input": 400, "output": 40, "model_calls": 3}


def test_tokens_are_kept_per_agent():
    metrics = SessionMetrics()
    invoke(metrics, agent("coordinator"), 100, 10)
    invoke(metrics, agent("data_analyst"), 50, 5)
    invoke(metrics, agent("data_analyst"), 50, 5)  # a fresh subagent on every call
    assert metrics.tokens["data_analyst", "lite"]["input"] == 100
    assert metrics.tokens["coordinator", "lite"]["input"] == 100


def test_tool_calls_errors_and_time():
    metrics = SessionMetrics()
    tool_call(metrics, "run_sql")
    tool_call(metrics, "run_sql", status="error", duration=1.5)
    assert metrics.tool_calls["run_sql"] == 2
    assert metrics.tool_errors["run_sql"] == 1
    assert metrics.tool_seconds["run_sql"] == 2.0


def test_every_logged_event_is_counted():
    metrics = SessionMetrics()
    for event in ("sql_rejected", "sql_rejected", "pii_redacted"):
        assert metrics.count_event(None, "info", {"event": event}) == {"event": event}
    assert metrics.events == {"sql_rejected": 2, "pii_redacted": 1}


def test_report_prices_each_agent_by_its_model():
    metrics = SessionMetrics()
    invoke(metrics, agent("coordinator", "lite"), 1_000_000, 0)
    invoke(metrics, agent("data_analyst", "flash"), 0, 1_000_000)
    invoke(metrics, agent("report_writer", "unknown"), 1_000_000, 0)
    tool_call(metrics, "run_sql")
    metrics.count_event(None, "info", {"event": "sql_rejected"})
    metrics.turn_ms += [1000, 3000]
    report = metrics.report({"lite": (0.30, 2.50), "flash": (1.50, 9.00)})
    assert "Turns: 2 (avg 2.0s, max 3.0s)" in report
    assert "$0.3000" in report and "$9.0000" in report and "no price" in report
    assert "Estimated cost: $9.3000" in report
    assert "run_sql" in report and "sql_rejected" in report


def test_report_counts_answered_questions_and_shows_only_signal_events():
    metrics = SessionMetrics()
    tool_call(metrics, "ask_data_analyst")
    tool_call(metrics, "ask_data_analyst", status="error")
    for event in ("findings", "turn_started", "tool_called", "analysis_incomplete", "model_unavailable"):
        metrics.count_event(None, "info", {"event": event})
    report = metrics.report({})
    assert "Questions: 2 asked, 1 answered" in report
    events = report.split("Events:\n")[1]
    assert "analysis_incomplete" in events and "model_unavailable" in events
    assert "turn_started" not in events and "tool_called" not in events and "findings" not in events


def test_tool_calls_are_logged_with_their_input():
    with capture_logs() as logs:
        tool_call(SessionMetrics(), "ask_data_analyst", status="error", tool_input={"question": "revenue by month"})
    assert logs[0]["event"] == "tool_called"
    assert logs[0]["agent"] == "coordinator" and logs[0]["status"] == "error"
    assert "revenue by month" in logs[0]["input"]


def test_run_sql_calls_leave_the_sql_to_the_sql_events():
    with capture_logs() as logs:
        tool_call(SessionMetrics(), "run_sql", tool_input={"sql": "SELECT 1"})
    assert "input" not in logs[0]


def test_structured_output_is_not_a_tool_call():
    metrics = SessionMetrics()
    event = SimpleNamespace(
        agent=SimpleNamespace(name="data_analyst"),
        selected_tool=StructuredOutputTool.__new__(StructuredOutputTool),
        tool_use={"name": "Findings", "input": {}},
        result={"status": "success"},
        exception=None,
        duration=0.1,
    )
    with capture_logs() as logs:
        metrics.on_tool_call(event)
    assert metrics.tool_calls == {} and logs == []
