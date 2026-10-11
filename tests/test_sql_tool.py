import pandas as pd
import pytest
from google.api_core.exceptions import BadRequest, ServiceUnavailable
from structlog.testing import capture_logs

from analyst_agent.config import Settings
from analyst_agent.safety.masker import Pseudonymizer
from analyst_agent.session import DataScope, Role, SessionContext
from analyst_agent.tools.sql import EMPTY_HINT, DataAccess, QueryResult, execute_sql, make_run_sql

TOP_CUSTOMERS = "SELECT user_id, SUM(sale_price) AS total FROM order_items GROUP BY user_id"


class FakeRunner:
    def __init__(self, result=None):
        self.result = result if result is not None else pd.DataFrame({"n": [1]})
        self.queries = []

    def run(self, sql):
        self.queries.append(sql)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def make_access(policy):
    def make(result=None, scope=DataScope(all_data=True)):
        settings = Settings(gcp_project="p", gemini_api_key="k", coordinator_model="m", analyst_model="m", report_model="m")
        session = SessionContext("ceo", "CEO", Role.ADMIN, scope)
        return DataAccess(settings, policy, session, FakeRunner(result), Pseudonymizer(b"k"))

    return make


def text(result):
    """run_sql's output as the model reads it: plain text, or the text of an error result."""
    return result if isinstance(result, str) else result["content"][0]["text"]


def test_rejected_sql_never_reaches_bigquery(make_access):
    access = make_access()
    result = execute_sql("SELECT email FROM users", access)
    assert result.failed and result.text.startswith("Rejected:")
    assert access.runner.queries == []


def test_results_are_masked_csv(make_access):
    access = make_access(pd.DataFrame({"user_id": [10121, 42], "total": [1306.987, 99.5]}))
    result = execute_sql(TOP_CUSTOMERS, access)
    assert not result.failed and result.text.startswith("2 rows:")
    assert "Customer #" in result.text and "10121" not in result.text
    assert "1306.99" in result.text


def test_empty_result_gets_a_hint_and_is_not_a_failure(make_access):
    result = execute_sql("SELECT brand FROM products", make_access(pd.DataFrame({"brand": []})))
    assert result == QueryResult(EMPTY_HINT)


def test_empty_result_reminds_scoped_users_of_their_access(make_access):
    access = make_access(pd.DataFrame({"brand": []}), DataScope(filters={"brand": ("MG", "Nike")}))
    output = execute_sql("SELECT brand FROM products", access).text
    assert "only sees brand in (MG, Nike)" in output and "outside their access" in output


def test_bigquery_errors_are_returned_for_repair(make_access):
    error = BadRequest("bad", errors=[{"message": "Unrecognized name: revenue"}])
    result = execute_sql("SELECT brand FROM products", make_access(error))
    assert result == QueryResult("BigQuery error: Unrecognized name: revenue", failed=True)


def test_outage_tells_the_agent_to_stop(make_access):
    result = execute_sql("SELECT brand FROM products", make_access(ServiceUnavailable("down")))
    assert result.failed and "unavailable" in result.text and "Stop" in result.text


def test_failed_queries_are_tool_errors(make_access):
    run_sql = make_run_sql(make_access())
    assert run_sql(sql="SELECT email FROM users")["status"] == "error"
    assert isinstance(run_sql(sql="SELECT brand FROM products"), str)
    assert run_sql(sql="SELECT  brand\n FROM products")["status"] == "error"


def test_repeated_sql_is_refused(make_access):
    access = make_access()
    run_sql = make_run_sql(access)
    run_sql(sql="SELECT brand FROM products")
    assert "already ran" in text(run_sql(sql="SELECT  brand\n FROM products"))
    assert len(access.runner.queries) == 1


def test_failures_end_the_question_early(make_access):
    access = make_access()
    run_sql = make_run_sql(access)
    for column in ("email", "first_name", "last_name"):
        run_sql(sql=f"SELECT {column} FROM users")
    assert "budget used up" in text(run_sql(sql="SELECT brand FROM products"))
    assert access.runner.queries == []


def test_good_queries_have_a_larger_budget(make_access):
    access = make_access()
    run_sql = make_run_sql(access)
    columns = ["brand", "category", "department", "name", "sku"]
    assert all(isinstance(run_sql(sql=f"SELECT {c} FROM products"), str) for c in columns)
    assert "budget used up" in text(run_sql(sql="SELECT cost FROM products"))
    assert len(access.runner.queries) == access.settings.max_sql_queries


def test_each_question_gets_a_fresh_budget(make_access):
    access = make_access()
    first, second = make_run_sql(access), make_run_sql(access)
    for column in ("email", "first_name", "last_name"):
        first(sql=f"SELECT {column} FROM users")
    assert isinstance(second(sql="SELECT brand FROM products"), str)


def test_every_query_outcome_logs_the_sql(make_access):
    error = BadRequest("bad", errors=[{"message": "Unrecognized name: revenue"}])
    with capture_logs() as logs:
        execute_sql("SELECT email\n  FROM users", make_access())
        execute_sql("SELECT brand FROM products", make_access(error))
        execute_sql("SELECT category FROM products", make_access())
    rejected, failed, executed = (e for e in logs if e["event"].startswith("sql_"))
    assert rejected["sql"] == "SELECT email FROM users"
    assert failed["sql"] == "SELECT brand FROM products" and failed["error"] == "Unrecognized name: revenue"
    assert "thelook_ecommerce" in failed["ran"]
    assert executed["sql"] == "SELECT category FROM products"
