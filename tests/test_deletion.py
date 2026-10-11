from datetime import timedelta

import pytest

from analyst_agent.reports.deletion import Deletions
from analyst_agent.reports.store import ReportStore
from analyst_agent.session import DataScope, Role, SessionContext
from analyst_agent.tools.reports import make_propose_deletion


@pytest.fixture
def store(tmp_path):
    return ReportStore(tmp_path / "reports.db")


@pytest.fixture
def ana():
    return SessionContext("ana", "Ana", Role.VIEWER, DataScope(all_data=True), thread_id="t1")


def save(store, owner="ana", thread="t1", title="Q1 Sales Review", body="Revenue grew 12%."):
    return store.save(owner, thread, title, "Headline.", body, ["q1"])


def ids(store, owner="ana"):
    return [report.id for report in store.list(owner)]


def test_confirm_deletes_only_the_stored_ids_and_their_files(store, ana):
    first, second = save(store), save(store)
    deletions = Deletions(store, ana)
    deletions.propose(this_conversation=True)
    late = save(store)  # matches the filter, but the user never saw it
    assert "Deleted 2" in deletions.confirm()
    assert ids(store) == [late.id]
    assert not store.file(first.id).exists() and not store.file(second.id).exists()
    assert deletions.pending is None


def test_cancel_deletes_nothing(store, ana):
    saved = save(store)
    deletions = Deletions(store, ana)
    deletions.propose(ids=[saved.id])
    assert "Nothing was deleted" in deletions.cancel()
    assert ids(store) == [saved.id]


def test_expired_proposal_deletes_nothing(store, ana):
    saved = save(store)
    deletions = Deletions(store, ana, ttl=timedelta(seconds=-1))
    deletions.propose(ids=[saved.id])
    assert "expired" in deletions.confirm()
    assert ids(store) == [saved.id]


def test_other_users_reports_never_match(store, ana):
    marko_report = save(store, owner="marko", thread="t1")
    deletions = Deletions(store, ana)
    assert deletions.propose(ids=[marko_report.id]) is None
    assert deletions.propose(this_conversation=True) is None
    assert store.delete("ana", [marko_report.id]) == []
    assert ids(store, "marko") == [marko_report.id]


def test_term_matches_whole_words_only(store, ana):
    mg = save(store, title="MG brand review")
    save(store, body="Image quality and management notes.")
    assert [r.id for r in store.find("ana", term="mg")] == [mg.id]


def test_filters_combine(store, ana):
    save(store, thread="t0", title="Nike old")
    nike_now = save(store, title="Nike now")
    save(store, title="MG now")
    assert [r.id for r in store.find("ana", term="nike", thread_id="t1")] == [nike_now.id]


def test_new_proposal_replaces_the_old_one(store, ana):
    first, second = save(store), save(store)
    deletions = Deletions(store, ana)
    deletions.propose(ids=[first.id])
    deletions.propose(ids=[second.id])
    deletions.confirm()
    assert ids(store) == [first.id]


def test_audit_log_records_every_step_without_content(store, ana):
    saved = save(store, body="Secret strategy")
    deletions = Deletions(store, ana)
    deletions.propose(ids=[saved.id])
    deletions.cancel()
    deletions.propose(ids=[saved.id])
    deletions.confirm()
    events = store.audit_events("ana")
    assert [event for event, _ in events] == ["deletion_proposed", "deletion_cancelled", "deletion_proposed", "deletion_executed"]
    assert all(report_ids == str(saved.id) for _, report_ids in events)


def test_confirmation_answers_are_strict(store, ana):
    deletions = Deletions(store, ana)
    for _ in range(6):
        save(store)
    small = deletions.propose(ids=ids(store)[:2])
    assert small.confirmed_by(" YES ") and small.confirmed_by("y")
    assert not small.confirmed_by("yes please delete") and not small.confirmed_by("sure")
    bulk = deletions.propose(this_conversation=True)
    assert bulk.confirmed_by("6") and not bulk.confirmed_by("yes")


def test_tool_never_claims_deletion(store, ana):
    save(store)
    propose = make_propose_deletion(Deletions(store, ana))
    assert "at least one filter" in propose()
    assert "NOT deleted" in propose(this_conversation=True)
    assert "No saved reports match" in propose(term="nothing-like-this")
    assert len(ids(store)) == 1


def test_confirming_reports_that_are_already_gone_says_nothing_was_deleted(store, ana):
    saved = save(store)
    deletions = Deletions(store, ana)
    deletions.propose(ids=[saved.id])
    store.delete("ana", [saved.id])
    assert deletions.confirm() == "Nothing was deleted: those reports no longer exist."
