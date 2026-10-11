import pandas as pd

from analyst_agent.safety.masker import Pseudonymizer, mask_result
from analyst_agent.safety.output_scan import REDACTED, redact_pii
from analyst_agent.safety.sql_guard import GROUP_SIZE, GuardedSql


def mask(frame, person_columns=(), has_group_size=False, pseudonymizer=None):
    guarded = GuardedSql("SELECT 1", frozenset(person_columns), has_group_size)
    return mask_result(frame, guarded, pseudonymizer or Pseudonymizer(b"k"), {"email", "first_name"}, min_group_size=5)


def test_customer_ids_become_stable_pseudonyms():
    pseudonymizer = Pseudonymizer(b"k")
    frame = pd.DataFrame({"user_id": [10121, 42, 10121], "total": [1.0, 2.0, 3.0]})
    labels = mask(frame, {"user_id"}, pseudonymizer=pseudonymizer).frame["user_id"]
    assert labels[0] == labels[2] != labels[1]
    assert labels[0].startswith("Customer #")


def test_pseudonyms_depend_on_the_key():
    assert Pseudonymizer(b"a").label(1) != Pseudonymizer(b"b").label(1)


def test_small_groups_are_hidden():
    frame = pd.DataFrame({"state": ["CA", "WY", "TX"], "avg_age": [30, 41, 35], GROUP_SIZE: [120, 2, 5]})
    result = mask(frame, has_group_size=True)
    assert list(result.frame["state"]) == ["CA", "TX"]
    assert GROUP_SIZE not in result.frame.columns
    assert result.notes == ["1 group(s) with fewer than 5 customers hidden for privacy."]


def test_blocked_columns_are_dropped():
    frame = pd.DataFrame({"state": ["CA"], "email": ["a@b.com"], "First_Name": ["Ana"]})
    assert list(mask(frame).frame.columns) == ["state"]


def test_emails_and_phones_are_redacted():
    text = "Contact jane.doe+vip@example.co.uk or (555) 123-4567, or +1 555.123.4567."
    assert redact_pii(text) == f"Contact {REDACTED} or {REDACTED}, or {REDACTED}."


def test_normal_analysis_text_is_untouched():
    text = "Revenue was $1,234,567.89 in 2024-03 (up 12.5%); 3 555 orders, order 1234567890 shipped."
    assert redact_pii(text) == text


def test_float_ids_get_the_same_pseudonym():
    pseudonymizer = Pseudonymizer(b"k")
    assert pseudonymizer.label(42.0) == pseudonymizer.label(42)
