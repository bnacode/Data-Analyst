import pytest

from analyst_agent.agents.report_writer import DEFAULT_PERSONA, load_persona
from analyst_agent.config import ROOT
from analyst_agent.reports.store import ReportStore
from analyst_agent.session import DataScope, Role, SessionContext
from analyst_agent.tools.reports import make_report_tools


@pytest.fixture
def store(tmp_path):
    return ReportStore(tmp_path / "reports.db")


def save(store, owner="ana", thread="t1", title="Q1 Sales Review", body="Revenue grew 12%.", tags=("q1", "revenue")):
    return store.save(owner, thread, title, "Headline.", body, list(tags))


def test_save_and_read_back(store):
    saved = save(store)
    assert store.get("ana", saved.id) == saved
    assert saved.tags == ("q1", "revenue")
    assert "# Q1 Sales Review" in saved.markdown()


def test_saved_report_is_written_to_a_markdown_file(store):
    saved = save(store)
    assert store.file(saved.id).read_text(encoding="utf-8") == saved.markdown()


def test_customers_are_numbered_per_report(store):
    body = "Customer #3F9A1C2E spent most, then Customer #00AB12CD. Customer #3F9A1C2E also returned most."
    saved = save(store, title="Top spender Customer #00AB12CD", body=body)
    assert saved.title == "Top spender Customer 1"
    assert saved.body == "Customer 2 spent most, then Customer 1. Customer 2 also returned most."


def test_users_only_see_their_own_reports(store):
    saved = save(store, owner="ana")
    assert store.get("marko", saved.id) is None
    assert store.list("marko") == []
    assert store.list("ana") == [saved]


def test_pii_is_redacted_before_saving(store):
    saved = save(store, body="Contact jane@example.com or 555-123-4567.")
    assert "jane@example.com" not in saved.body and "555-123-4567" not in saved.body
    assert saved.body.count("[REDACTED]") == 2


def test_keyword_search_covers_text_and_tags(store):
    nike = save(store, title="Nike vs MG", tags=("brands",))
    q1 = save(store, tags=("q1", "Spend"))
    assert store.list("ana", "NIKE") == [nike]
    assert store.list("ana", "spend") == [q1]
    assert store.list("ana") == [q1, nike]  # newest first


def test_report_tools_are_bound_to_the_session_user(store):
    saved = save(store, owner="ana")
    marko = SessionContext("marko", "Marko", Role.MAINTAINER, DataScope(filters={"department": ("Women",)}))
    list_reports, read_report = make_report_tools(store, marko)
    assert list_reports() == "No saved reports yet."
    assert "no report" in read_report(report_id=saved.id)


def test_new_thread_clears_the_notebook():
    session = SessionContext("ana", "Ana", Role.VIEWER, DataScope(all_data=True))
    session.notebook.append("Question: revenue?")
    session.new_thread()
    assert session.notebook == []


def test_persona_is_loaded_and_falls_back_when_broken(tmp_path):
    assert load_persona(ROOT / "config" / "persona.yaml") != DEFAULT_PERSONA
    broken = tmp_path / "persona.yaml"
    broken.write_text("instructions: [unclosed")
    assert load_persona(broken) == DEFAULT_PERSONA
    assert load_persona(tmp_path / "missing.yaml") == DEFAULT_PERSONA
