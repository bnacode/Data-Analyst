from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors
from strands.types.exceptions import EventLoopException, ModelThrottledException
from structlog.testing import capture_logs

from analyst_agent.agents import data_analyst
from analyst_agent.agents.models import MODEL_DOWN, error_cause, model_rejected, model_unavailable
from analyst_agent.tools.result import tool_error
from analyst_agent.config import Settings
from analyst_agent.safety.masker import Pseudonymizer
from analyst_agent.session import DataScope, Role, SessionContext
from analyst_agent.tools.sql import DataAccess


def wrapped(error):
    """How Strands raises a model error: EventLoopException(...) from error."""
    exc = EventLoopException(error)
    exc.__cause__ = error
    return exc


@pytest.mark.parametrize(
    "error",
    [
        ModelThrottledException("429"),
        errors.ServerError(503, {}),
        httpx.ConnectError("offline"),
        wrapped(ModelThrottledException("429")),
    ],
)
def test_model_outages_are_recognised_even_when_wrapped(error):
    assert model_unavailable(error)


def test_other_errors_are_not_outages():
    assert not model_unavailable(wrapped(ValueError("bug")))
    assert not model_rejected(wrapped(ValueError("bug")))


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_refused_requests_are_rejections_not_outages(code):
    error = wrapped(errors.ClientError(code, {}))
    assert model_rejected(error)
    assert not model_unavailable(error)


def test_rate_limits_are_outages_not_rejections():
    error = wrapped(errors.ClientError(429, {}))
    assert model_unavailable(error)
    assert not model_rejected(error)


def test_error_cause_names_the_root_error():
    assert error_cause(wrapped(errors.ClientError(429, {"error": {"status": "RESOURCE_EXHAUSTED"}}))) == "429 RESOURCE_EXHAUSTED"
    assert error_cause(wrapped(httpx.ConnectError("offline"))) == "ConnectError"


def test_error_cause_names_the_quota():
    body = {"error": {"status": "RESOURCE_EXHAUSTED", "details": [{"violations": [{"quotaId": "PerDay-FreeTier"}]}]}}
    assert error_cause(wrapped(errors.ClientError(429, body))) == "429 RESOURCE_EXHAUSTED PerDay-FreeTier"


@pytest.fixture
def ask_analyst(policy, monkeypatch):
    """ask_data_analyst with a fake analyst agent that returns `outcome` (or raises it)."""

    def make(outcome):
        class FakeAnalyst:
            def __init__(self, **kwargs):
                pass

            def __call__(self, *args, **kwargs):
                if isinstance(outcome, Exception):
                    raise outcome
                return SimpleNamespace(structured_output=outcome)

        monkeypatch.setattr(data_analyst, "Agent", FakeAnalyst)
        monkeypatch.setattr(data_analyst, "gemini_model", lambda *args: None)
        settings = Settings(gcp_project="p", gemini_api_key="k", coordinator_model="m", analyst_model="m", report_model="m")
        session = SessionContext("ceo", "CEO", Role.ADMIN, DataScope(all_data=True))
        access = DataAccess(settings, policy, session, None, Pseudonymizer(b"k"))
        return data_analyst.make_ask_data_analyst(access), session

    return make


def test_analyst_stops_cleanly_when_gemini_is_down(ask_analyst):
    ask, session = ask_analyst(wrapped(errors.ServerError(503, {})))
    assert ask("revenue?") == tool_error(MODEL_DOWN)
    assert session.notebook == []


def test_findings_are_logged_and_kept(ask_analyst):
    ask, session = ask_analyst(data_analyst.Findings(answer="Revenue was $10.", caveats=["Excludes returns."]))
    with capture_logs() as logs:
        assert "Revenue was $10." in ask("revenue?")
    assert logs[-1] == {"event": "findings", "log_level": "info", "question": "revenue?",
                        "answer": "Revenue was $10.", "caveats": ["Excludes returns."]}
    assert len(session.notebook) == 1


def test_incomplete_analysis_is_a_tool_error(ask_analyst):
    ask, session = ask_analyst(None)
    assert ask("revenue?") == tool_error(data_analyst.INCOMPLETE.model_dump_json())
    assert session.notebook == []
