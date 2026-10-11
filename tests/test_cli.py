from contextlib import nullcontext

import pytest
import structlog
from google.genai import errors
from strands.types.exceptions import ModelThrottledException
from structlog.testing import capture_logs

from analyst_agent import cli
from analyst_agent.config import Settings
from analyst_agent.reports.store import ReportStore
from analyst_agent.safety.masker import Pseudonymizer
from analyst_agent.session import DataScope, Role, SessionContext
from analyst_agent.tools.sql import DataAccess
from tests.test_agents import wrapped


class FakeConsole:
    def __init__(self, answers):
        self.answers = list(answers)
        self.printed = []

    def input(self, prompt=""):
        return self.answers.pop(0)

    def print(self, *args, **kwargs):
        self.printed.append(" ".join(map(str, args)))

    def status(self, *args):
        return nullcontext()


class FakeResult:
    def __init__(self, text, stop_reason="end_turn"):
        self.text = text
        self.stop_reason = stop_reason

    def __str__(self):
        return self.text


class FakeAgent:
    """Proposes deleting this conversation's reports whenever asked to delete."""

    def __init__(self, deletions):
        self.deletions = deletions
        self.messages = []
        self.error = None
        self.stop_reason = "end_turn"

    def __call__(self, message, limits=None):
        self.messages.append(message)
        if self.error:
            raise self.error
        if "delete the reports" in message:
            self.deletions.propose(this_conversation=True)
        return FakeResult("ok", self.stop_reason)


@pytest.fixture
def make_app(policy, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "build_coordinator", lambda access, store, deletions: FakeAgent(deletions))

    def make(answers, reports=1):
        settings = Settings(gcp_project="p", gemini_api_key="k", coordinator_model="m", analyst_model="m", report_model="m")
        session = SessionContext("ana", "Ana", Role.VIEWER, DataScope(all_data=True))
        store = ReportStore(tmp_path / "reports.db")
        for n in range(reports):
            store.save("ana", session.thread_id, f"Report {n}", "Summary.", "Body.", ["q1"])
        access = DataAccess(settings, policy, session, None, Pseudonymizer(b"k"))
        return cli.ChatApp(access, store, FakeConsole(answers))

    return make


def test_cli_imports():
    assert cli.main


def test_yes_deletes_and_the_agent_hears_about_it(make_app):
    app = make_app(["yes"])
    app.dispatch("delete the reports from this conversation")
    assert app.store.list("ana") == []
    app.dispatch("thanks")
    assert app.agent.messages[-1].startswith("[App note: Deleted 1 report(s)")


def test_bulk_deletion_needs_the_typed_count(make_app):
    app = make_app(["yes", "6"], reports=6)
    app.dispatch("delete the reports from this conversation")
    assert len(app.store.list("ana")) == 6
    app.dispatch("delete the reports from this conversation")
    assert app.store.list("ana") == []


def test_other_input_cancels_and_is_handled_as_a_message(make_app):
    app = make_app(["what was Q1 revenue?"])
    app.dispatch("delete the reports from this conversation")
    assert len(app.store.list("ana")) == 1
    assert app.agent.messages[-1] == "[App note: Deletion cancelled. Nothing was deleted.]\n\nwhat was Q1 revenue?"
    assert app.deletions.pending is None


def test_no_cancels_without_a_new_message(make_app):
    app = make_app(["no"])
    app.dispatch("delete the reports from this conversation")
    assert len(app.store.list("ana")) == 1
    assert len(app.agent.messages) == 1


def test_new_conversation_cancels_a_pending_deletion(make_app):
    app = make_app([])
    app.deletions.propose(this_conversation=True)
    app.dispatch("/new")
    assert app.deletions.pending is None
    assert app.store.audit_events("ana")[-1][0] == "deletion_cancelled"


def test_model_outage_gets_its_own_message(make_app):
    app = make_app([])
    app.agent.error = wrapped(ModelThrottledException("429"))
    app.dispatch("revenue?")
    assert app.console.printed[-1].endswith(f"{cli.MODEL_DOWN_MESSAGE}[/red]")


def test_rejected_model_request_points_to_the_config(make_app):
    app = make_app([])
    app.agent.error = wrapped(errors.ClientError(401, {}))
    app.dispatch("revenue?")
    assert cli.MODEL_REJECTED_MESSAGE in app.console.printed[-1]


def test_unexpected_error_gets_the_generic_message_with_a_reference(make_app):
    app = make_app([])
    app.agent.error = RuntimeError("bug")
    app.dispatch("revenue?")
    assert "something went wrong" in app.console.printed[-1]
    assert "(ref " in app.console.printed[-1]


def test_every_turn_gets_its_own_log_reference(make_app):
    app = make_app([])
    with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
        app.dispatch("revenue?")
        app.dispatch("and costs?")
    turns = [(e["event"], e["turn_id"]) for e in logs if e["event"] in ("turn_started", "turn_completed")]
    assert [event for event, _ in turns] == ["turn_started", "turn_completed"] * 2
    assert turns[0][1] == turns[1][1] != turns[2][1] == turns[3][1]
    assert logs[-1]["answer_chars"] == 2 and "answer" not in logs[-1]


def test_logged_messages_are_redacted(make_app):
    app = make_app([])
    with capture_logs() as logs:
        app.dispatch("compare spend of jane.doe@example.com")
    started = next(e for e in logs if e["event"] == "turn_started")
    assert started["message"] == "compare spend of [REDACTED]"



def test_turn_budget_stops_the_answer(make_app):
    app = make_app([])
    app.agent.stop_reason = "limit_turns"
    app.dispatch("revenue?")
    assert cli.BUDGET_MESSAGE in app.console.printed[-1]


def test_metrics_command_shows_completed_turns(make_app):
    app = make_app([])
    app.dispatch("revenue?")
    app.dispatch("/metrics")
    assert "Turns: 1" in app.console.printed[-1]
