from pathlib import Path

import pytest

from analyst_agent.config import ROOT, ConfigError
from analyst_agent.safety.policy import load_policy
from analyst_agent.session import AccessDenied, Role, create_session, load_access
from analyst_agent.tools.schema import render_schema

CONFIG = ROOT / "config"
PII_COLUMNS = ["first_name", "last_name", "email", "street_address", "postal_code", "latitude", "longitude", "user_geom"]


@pytest.mark.parametrize("column", PII_COLUMNS)
def test_pii_columns_are_blocked(policy, column):
    assert column not in policy.tables["users"].columns


@pytest.mark.parametrize("table, column", [("users", "id"), ("orders", "user_id"), ("order_items", "user_id")])
def test_person_ids_are_pseudonymized(policy, table, column):
    assert policy.person_column(table) == column


def test_schema_text_never_names_pii_columns(policy):
    text = render_schema(policy)
    for column in PII_COLUMNS:
        assert f"- {column} (" not in text


def test_schema_text_marks_demographics(policy):
    assert "- age (INTEGER): Customer age in years. [demographic:" in render_schema(policy)


def test_policy_rejects_column_both_allowed_and_blocked(tmp_path: Path):
    bad = tmp_path / "policy.yaml"
    bad.write_text(
        "dataset: d\nscope_table: users\ntables:\n  users:\n    columns:\n      email: {type: STRING, access: allow}\n    blocked: [email]\n"
    )
    with pytest.raises(ConfigError):
        load_policy(bad)


def test_sessions_from_access_config(policy):
    ana = create_session("ana", CONFIG / "access.yaml", policy)
    assert ana.role is Role.VIEWER
    assert ana.scope.filters == {"brand": ("MG", "Nike")}
    assert not ana.scope.all_data

    ceo = create_session("ceo", CONFIG / "access.yaml", policy)
    assert ceo.scope.all_data
    assert ceo.scope.describe() == "all products"


def test_unknown_user_is_denied(policy):
    with pytest.raises(AccessDenied):
        create_session("mallory", CONFIG / "access.yaml", policy)


def test_empty_scope_means_no_data(policy, tmp_path: Path):
    access = tmp_path / "access.yaml"
    access.write_text("groups:\n  nobody: {scope: {}}\nusers:\n  bob: {name: Bob, role: viewer, group: nobody}\n")
    assert create_session("bob", access, policy).scope.describe() == "no data"


def test_access_config_rejects_unknown_group(policy, tmp_path: Path):
    access = tmp_path / "access.yaml"
    access.write_text("groups: {}\nusers:\n  bob: {name: Bob, role: viewer, group: ghost}\n")
    with pytest.raises(ConfigError):
        load_access(access, policy)


def test_access_config_rejects_unsupported_scope_column(policy, tmp_path: Path):
    access = tmp_path / "access.yaml"
    access.write_text("groups:\n  g: {scope: {email: [x]}}\nusers: {}\n")
    with pytest.raises(ConfigError):
        load_access(access, policy)


@pytest.mark.parametrize(
    "link",
    [
        "{column: id, via: ghost.id}",
        "{column: id, via: products.ghost}",
        "{column: id, via: orders.id}",  # orders links back to users: a cycle
    ],
)
def test_policy_rejects_broken_belongs_via_links(tmp_path: Path, link):
    bad = tmp_path / "policy.yaml"
    bad.write_text(
        "dataset: d\nscope_table: products\ntables:\n"
        "  products: {columns: {id: {type: INTEGER, access: allow}}}\n"
        f"  users: {{columns: {{id: {{type: INTEGER, access: allow}}}}, belongs_via: {link}}}\n"
        "  orders: {columns: {id: {type: INTEGER, access: allow}}, belongs_via: {column: id, via: users.id}}\n"
    )
    with pytest.raises(ConfigError, match="belongs_via"):
        load_policy(bad)
