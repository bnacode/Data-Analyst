import pytest

from analyst_agent.safety.sql_guard import GROUP_SIZE, SqlRejected, guard_sql
from analyst_agent.session import DataScope

FULL = "`bigquery-public-data`.`thelook_ecommerce`"


def guard(sql, policy, scope, max_rows=200):
    return guard_sql(sql, policy, scope, max_rows, min_group_size=5)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT email FROM users",
        "SELECT state FROM users WHERE email LIKE '%@gmail.com'",
        "SELECT o.status FROM orders o JOIN users u ON u.email = CAST(o.order_id AS STRING)",
        "SELECT state FROM (SELECT state, first_name FROM users)",
        "WITH t AS (SELECT id, street_address FROM users) SELECT id FROM t",
        "SELECT latitude, longitude FROM users",
        "SELECT made_up_column FROM orders",
    ],
)
def test_blocked_columns_are_rejected_anywhere(policy, all_scope, sql):
    with pytest.raises(SqlRejected, match="could not be resolved|Unknown column"):
        guard(sql, policy, all_scope)


@pytest.mark.parametrize(
    "sql, reason",
    [
        ("DROP TABLE users", "Only SELECT"),
        ("DELETE FROM orders WHERE TRUE", "Only SELECT"),
        ("INSERT INTO orders (order_id) VALUES (1)", "Only SELECT"),
        ("SELECT 1; DROP TABLE users", "exactly one"),
        ("SELECT * FROM inventory_items", "not available"),
        ("SELECT * FROM thelook_ecommerce.INFORMATION_SCHEMA.COLUMNS", "not available"),
        ("SELECT * FROM `other-project.other_dataset.orders`", "not available"),
        ("SELEC brand FROM products", "syntax error"),
    ],
)
def test_non_select_and_unknown_tables_are_rejected(policy, all_scope, sql, reason):
    with pytest.raises(SqlRejected, match=reason):
        guard(sql, policy, all_scope)


def test_select_star_expands_to_allowed_columns_only(policy, all_scope):
    sql = guard("SELECT state, COUNT(*) AS n FROM (SELECT * FROM users) GROUP BY state", policy, all_scope).sql
    assert "users.state" in sql
    assert "email" not in sql and "first_name" not in sql


def test_fully_qualified_table_names_are_accepted(policy, all_scope):
    sql = guard("SELECT brand FROM `bigquery-public-data.thelook_ecommerce.products`", policy, all_scope).sql
    assert f"FROM {FULL}.`products`" in sql


def test_no_scope_filter_for_all_data(policy, all_scope):
    sql = guard("SELECT brand, COUNT(*) AS n FROM products GROUP BY brand", policy, all_scope).sql
    assert "WHERE" not in sql


def test_scope_filters_every_table(policy, brand_scope):
    sql = guard(
        "SELECT u.state, SUM(oi.sale_price) AS revenue FROM order_items oi "
        "JOIN users u ON u.id = oi.user_id JOIN orders o ON o.order_id = oi.order_id GROUP BY u.state",
        policy,
        brand_scope,
    ).sql
    in_scope = f"SELECT id FROM {FULL}.`products` WHERE brand IN ('MG', 'Nike')"
    assert f"FROM {FULL}.`order_items` WHERE product_id IN ({in_scope})) AS oi" in sql
    assert f"FROM {FULL}.`users` WHERE id IN (SELECT user_id FROM {FULL}.`order_items`" in sql
    assert f"FROM {FULL}.`orders` WHERE order_id IN (SELECT order_id FROM {FULL}.`order_items`" in sql


def test_scope_applies_inside_ctes_and_subqueries(policy, brand_scope):
    sql = guard(
        "WITH b AS (SELECT brand FROM products) SELECT brand FROM b UNION ALL SELECT brand FROM (SELECT brand FROM products)",
        policy,
        brand_scope,
    ).sql
    assert sql.count("WHERE brand IN ('MG', 'Nike')") == 2


@pytest.mark.parametrize(
    "sql",
    [
        "WITH products AS (SELECT * FROM `bigquery-public-data.thelook_ecommerce.products`) SELECT brand FROM products",
        "WITH orders AS (SELECT order_id FROM orders) SELECT COUNT(*) AS n FROM orders",
    ],
)
def test_ctes_named_like_tables_are_rejected(policy, brand_scope, sql):
    with pytest.raises(SqlRejected, match="CTEs"):
        guard(sql, policy, brand_scope)


def test_scope_values_are_quoted_safely(policy):
    sneaky = DataScope(filters={"brand": ("x') OR TRUE --",)})
    sql = guard("SELECT brand FROM products", policy, sneaky).sql
    assert r"brand IN ('x\') OR TRUE --')" in sql


def test_empty_scope_has_no_access(policy):
    with pytest.raises(SqlRejected, match="access"):
        guard("SELECT brand FROM products", policy, DataScope())


def test_customer_ids_are_marked_for_pseudonyms(policy, all_scope):
    guarded = guard("SELECT user_id, SUM(sale_price) AS total FROM order_items GROUP BY user_id", policy, all_scope)
    assert guarded.person_columns == {"user_id"}
    assert not guarded.has_group_size


def test_customer_ids_are_traced_through_ctes(policy, all_scope):
    sql = "WITH t AS (SELECT user_id AS cid FROM orders) SELECT cid AS customer FROM t"
    assert guard(sql, policy, all_scope).person_columns == {"customer"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT CAST(user_id AS STRING) AS x FROM orders",
        "SELECT user_id + 0 AS x FROM orders",
        "SELECT MAX(user_id) AS x FROM orders",
        "SELECT TO_HEX(SHA256(CAST(id AS STRING))) AS x FROM users",
    ],
)
def test_transformed_customer_ids_are_rejected(policy, all_scope, sql):
    with pytest.raises(SqlRejected, match="Customer IDs"):
        guard(sql, policy, all_scope)


def test_customer_ids_can_be_counted_and_joined(policy, all_scope):
    sql = "SELECT u.state, COUNT(DISTINCT o.user_id) AS buyers FROM orders o JOIN users u ON u.id = o.user_id GROUP BY u.state"
    assert guard(sql, policy, all_scope).person_columns == frozenset()


def test_group_size_added_to_customer_aggregates(policy, all_scope):
    guarded = guard("SELECT state, AVG(age) AS avg_age FROM users GROUP BY state", policy, all_scope)
    assert guarded.has_group_size
    assert f"COUNT(DISTINCT users.id) AS {GROUP_SIZE}" in guarded.sql


def test_no_group_size_for_product_only_aggregates(policy, all_scope):
    assert not guard("SELECT brand, COUNT(*) AS n FROM products GROUP BY brand", policy, all_scope).has_group_size


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT p.brand, SUM(oi.sale_price) AS revenue FROM order_items oi JOIN products p ON p.id = oi.product_id GROUP BY p.brand",
        "SELECT o.status, COUNT(DISTINCT o.user_id) AS buyers FROM orders o GROUP BY o.status",
        "SELECT oi.status, SUM(oi.sale_price) AS r FROM order_items oi JOIN users u ON u.id = oi.user_id GROUP BY oi.status",
    ],
)
def test_sales_figures_are_never_hidden_however_few_bought(policy, all_scope, sql):
    assert not guard(sql, policy, all_scope).has_group_size


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT u.state, SUM(oi.sale_price) AS revenue FROM order_items oi JOIN users u ON u.id = oi.user_id GROUP BY u.state",
        "SELECT SUM(oi.sale_price) AS revenue FROM order_items oi JOIN users u ON u.id = oi.user_id WHERE u.country = 'Brasil'",
        "SELECT o.gender, COUNT(*) AS n FROM orders o GROUP BY o.gender",
    ],
)
def test_aggregates_about_who_customers_are_report_group_sizes(policy, all_scope, sql):
    assert guard(sql, policy, all_scope).has_group_size


@pytest.mark.parametrize("sql, limit", [("SELECT brand FROM products", 200), ("SELECT brand FROM products LIMIT 10", 10), ("SELECT brand FROM products LIMIT 5000", 200)])
def test_row_cap(policy, all_scope, sql, limit):
    assert guard(sql, policy, all_scope).sql.endswith(f"LIMIT {limit}")


def test_inner_customer_aggregates_drop_small_groups(policy, all_scope):
    sql = "WITH s AS (SELECT state, COUNT(*) AS n FROM users GROUP BY state) SELECT * FROM s"
    guarded = guard(sql, policy, all_scope)
    assert "HAVING COUNT(DISTINCT users.id) >= 5" in guarded.sql
    assert not guarded.has_group_size


def test_union_branches_drop_small_groups(policy, all_scope):
    sql = "SELECT state, AVG(age) AS a FROM users GROUP BY state UNION ALL SELECT 'all', AVG(age) FROM users"
    assert guard(sql, policy, all_scope).sql.count("HAVING COUNT(DISTINCT users.id) >= 5") == 2


def test_group_size_traced_through_per_customer_ctes(policy, all_scope):
    sql = (
        "WITH c AS (SELECT oi.user_id, SUM(oi.sale_price) AS spend FROM order_items oi "
        "JOIN users u ON u.id = oi.user_id WHERE u.state = 'Texas' GROUP BY oi.user_id) "
        "SELECT AVG(spend) AS avg_spend FROM c"
    )
    guarded = guard(sql, policy, all_scope)
    assert guarded.has_group_size
    assert f"COUNT(DISTINCT c.user_id) AS {GROUP_SIZE}" in guarded.sql


def test_aggregates_over_already_grouped_rows_need_no_group_size(policy, all_scope):
    sql = "WITH s AS (SELECT state, COUNT(*) AS n FROM users GROUP BY state) SELECT AVG(n) AS a FROM s"
    assert not guard(sql, policy, all_scope).has_group_size


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT state, AVG(age) AS a FROM (SELECT state, age FROM users) GROUP BY state",
        "WITH t AS (SELECT state, age FROM users) SELECT state, AVG(age) AS a FROM t GROUP BY state",
        "SELECT category, COUNT(*) AS n FROM products WHERE id IN "
        "(SELECT product_id FROM order_items WHERE user_id IN (SELECT id FROM users WHERE city = 'Austin')) GROUP BY category",
    ],
)
def test_customer_aggregates_without_traceable_ids_are_rejected(policy, all_scope, sql):
    with pytest.raises(SqlRejected, match="group sizes"):
        guard(sql, policy, all_scope)


def test_tables_without_belongs_via_are_off_limits_to_scoped_users(policy, brand_scope):
    unlinked = policy.model_copy(deep=True)
    unlinked.tables["orders"].belongs_via = None
    with pytest.raises(SqlRejected, match="data scope"):
        guard("SELECT status FROM orders", unlinked, brand_scope)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SUM(sale_price) FROM order_items oi JOIN products p ON p.id = oi.product_id WHERE p.brand = 'Adidas'",
        "SELECT COUNT(*) FROM products WHERE brand IN ('Nike', 'Adidas')",
        "SELECT COUNT(*) FROM products WHERE 'Adidas' = brand",
        "WITH t AS (SELECT id FROM products WHERE brand = 'Adidas') SELECT COUNT(*) FROM t",
    ],
)
def test_filtering_outside_the_scope_is_rejected_not_zero(policy, brand_scope, sql):
    with pytest.raises(SqlRejected, match="Adidas is outside this user's data access"):
        guard(sql, policy, brand_scope)


@pytest.mark.parametrize(
    "sql, scope",
    [
        ("SELECT COUNT(*) FROM products WHERE brand IN ('nike', 'MG')", DataScope(filters={"brand": ("MG", "Nike")})),
        ("SELECT COUNT(*) FROM products WHERE brand = 'Adidas'", DataScope(all_data=True)),
        ("SELECT COUNT(*) FROM products WHERE brand = 'Adidas'", DataScope(filters={"department": ("Women",)})),
    ],
)
def test_filters_inside_the_scope_or_on_other_columns_pass(policy, sql, scope):
    guard(sql, policy, scope)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT u.id, u.gender, u.age, SUM(oi.sale_price) AS spend FROM order_items oi JOIN users u ON oi.user_id = u.id GROUP BY 1, 2, 3",
        "SELECT age, gender FROM users",
        "WITH t AS (SELECT id, age + 0 AS a FROM users) SELECT id, a FROM t",
        "SELECT state, COUNT(*) AS n FROM users GROUP BY state UNION ALL SELECT city, 1 FROM users",
    ],
)
def test_demographics_are_never_shown_per_customer(policy, all_scope, sql):
    with pytest.raises(SqlRejected, match="groups of customers"):
        guard(sql, policy, all_scope)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT state, AVG(age) AS a FROM users GROUP BY state",
        "WITH s AS (SELECT gender, COUNT(*) AS n FROM users GROUP BY gender) SELECT gender, n FROM s",
        "SELECT u.id, u.country, SUM(oi.sale_price) AS spend FROM order_items oi JOIN users u ON oi.user_id = u.id GROUP BY 1, 2",
        "SELECT oi.user_id, SUM(oi.sale_price) AS s FROM order_items oi JOIN users u ON u.id = oi.user_id WHERE u.age > 30 GROUP BY 1",
    ],
)
def test_demographics_of_groups_and_other_customer_columns_pass(policy, all_scope, sql):
    guard(sql, policy, all_scope)
