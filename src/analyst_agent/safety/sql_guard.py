"""SQL guard: every query is parsed, checked against the allowlist and rewritten before it runs."""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

from analyst_agent.safety.policy import Access, DataPolicy
from analyst_agent.session import DataScope

GROUP_SIZE = "_group_size"


class SqlRejected(ValueError):
    """The query is not allowed; the message tells the model why."""


@dataclass(frozen=True)
class GuardedSql:
    sql: str
    person_columns: frozenset[str]  # output columns holding customer IDs, to pseudonymize
    has_group_size: bool  # GROUP_SIZE column added, for the small-group rule


def guard_sql(sql: str, policy: DataPolicy, scope: DataScope, max_rows: int, min_group_size: int) -> GuardedSql:
    """Validates a query against the policy and rewrites it with scope filters and a row cap."""
    if not (scope.all_data or scope.filters):
        raise SqlRejected("You don't have access to any data.")
    tree = _parse(sql)
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    _check_tables(tree, policy, ctes)
    tree = _qualify(tree, policy)
    _check_scope_values(tree, policy, scope)
    person_columns = _check_person_ids(tree, policy)
    _check_demographics(tree, policy)
    has_group_size = _protect_small_groups(tree, policy, min_group_size)
    _bind_tables(tree, policy, scope, ctes)
    tree.set("limit", exp.Limit(expression=exp.Literal.number(_row_cap(tree, max_rows))))
    return GuardedSql(tree.sql(dialect="bigquery"), frozenset(person_columns), has_group_size)


def _parse(sql: str) -> exp.Query:
    try:
        statements = [s for s in sqlglot.parse(sql, read="bigquery") if s is not None]
    except ParseError as exc:
        raise SqlRejected(f"SQL syntax error: {str(exc).splitlines()[0]}") from exc
    if len(statements) != 1:
        raise SqlRejected("Send exactly one SQL statement.")
    if not isinstance(statements[0], exp.Query):
        raise SqlRejected("Only SELECT queries are allowed.")
    return statements[0]


def _is_cte(table: exp.Table, ctes: set[str]) -> bool:
    return not table.db and table.name.lower() in ctes


def _check_tables(tree: exp.Query, policy: DataPolicy, ctes: set[str]) -> None:
    # A CTE named like a table would hide that table from the scope rewrite
    if shadowed := ctes & set(policy.tables):
        raise SqlRejected(f"Give CTEs names that differ from table names: {', '.join(sorted(shadowed))}.")
    catalog, dataset = policy.dataset.split(".")
    for table in tree.find_all(exp.Table):
        if _is_cte(table, ctes):
            continue
        in_dataset = not table.db or (table.db == dataset and table.catalog in ("", catalog))
        if table.name.lower() not in policy.tables or not in_dataset:
            raise SqlRejected(f"Table {table.name} is not available. Use: {', '.join(policy.tables)}.")
        table.set("catalog", None)
        table.set("db", None)


def _qualify(tree: exp.Query, policy: DataPolicy) -> exp.Query:
    # The schema holds only allowed columns, so any blocked column fails to resolve
    schema = {name: {col: c.type for col, c in table.columns.items()} for name, table in policy.tables.items()}
    try:
        return qualify(tree, schema=schema, dialect="bigquery", identify=False, validate_qualify_columns=True)
    except OptimizeError as exc:
        raise SqlRejected(f"{exc}. Only columns listed by describe_schema are available.") from exc


def _check_scope_values(tree: exp.Query, policy: DataPolicy, scope: DataScope) -> None:
    """Filtering on a value outside the user's scope would quietly return zero, so say it's off limits instead."""
    if scope.all_data:
        return
    for root in traverse_scope(tree):
        for column in root.columns:
            allowed = scope.filters.get(column.name.lower())
            source = root.sources.get(column.table)
            if allowed is None or not (isinstance(source, exp.Table) and source.name.lower() == policy.scope_table):
                continue
            inside = {value.lower() for value in allowed}
            if outside := [value for value in _compared_values(column) if value.lower() not in inside]:
                raise SqlRejected(
                    f"{', '.join(outside)} is outside this user's data access ({scope.describe()}). "
                    "Do not retry without the filter and do not report zero: tell the user it is outside their access."
                )


def _compared_values(column: exp.Column) -> list[str]:
    """String literals a column is compared to with = or IN."""
    parent = column.parent
    if isinstance(parent, exp.EQ):
        values = [parent.expression if parent.this is column else parent.this]
    elif isinstance(parent, exp.In) and parent.this is column:
        values = parent.expressions
    else:
        return []
    return [value.this for value in values if isinstance(value, exp.Literal) and value.is_string]


def _check_person_ids(tree: exp.Query, policy: DataPolicy) -> set[str]:
    """Customer IDs may be joined, filtered and counted, but only selected as-is (then pseudonymized)."""
    root = None
    for root in traverse_scope(tree):
        for column in root.columns:
            if _is_person_id(column, root, policy) and not _is_safe_person_use(column):
                raise SqlRejected("Customer IDs can only be selected as-is or counted, not transformed.")
    return {
        projection.alias_or_name
        for projection in root.expression.selects
        if isinstance(projection.unalias(), exp.Column) and _is_person_id(projection.unalias(), root, policy)
    }


def _column_access(column: exp.Column, scope: Scope, policy: DataPolicy) -> Access | None:
    """Access of the table column this column passes on unchanged, followed through CTEs and subqueries."""
    source = scope.sources.get(column.table)
    if isinstance(source, exp.Table):
        allowed = policy.tables[source.name.lower()].columns.get(column.name.lower())
        return allowed.access if allowed else None
    if isinstance(source, Scope) and isinstance(source.expression, exp.Select):
        for projection in source.expression.selects:
            if projection.alias_or_name == column.name and isinstance(projection.unalias(), exp.Column):
                return _column_access(projection.unalias(), source, policy)
    return None


def _is_person_id(column: exp.Column, scope: Scope, policy: DataPolicy) -> bool:
    return _column_access(column, scope, policy) is Access.PSEUDONYMIZE


def _check_demographics(tree: exp.Query, policy: DataPolicy) -> None:
    """Age, gender and the like may describe groups of customers, never one customer."""
    for scope in _output_scopes(traverse_scope(tree)[-1]):
        if not _is_group_level(scope, policy) and any(_shows_demographics(p, scope, policy) for p in scope.expression.selects):
            raise SqlRejected(
                "Columns marked as demographic in the schema can only be shown for groups of customers "
                "(GROUP BY), never per customer or next to a customer ID."
            )


def _output_scopes(scope: Scope) -> list[Scope]:
    """The SELECTs whose rows are returned: the query itself, or each branch of a UNION."""
    return [leaf for branch in scope.set_operation_scopes for leaf in _output_scopes(branch)] or [scope]


def _shows_demographics(expression: exp.Expression, scope: Scope, policy: DataPolicy) -> bool:
    """Whether a demographic column feeds this expression, also through CTEs and subqueries with a row per customer."""
    for column in expression.find_all(exp.Column):
        source = scope.sources.get(column.table)
        if isinstance(source, exp.Table) and _column_access(column, scope, policy) is Access.QUASI_IDENTIFIER:
            return True
        if isinstance(source, Scope) and isinstance(source.expression, exp.Select) and not _is_group_level(source, policy):
            if any(p.alias_or_name == column.name and _shows_demographics(p, source, policy) for p in source.expression.selects):
                return True
    return False


def _is_safe_person_use(column: exp.Column) -> bool:
    node = column
    while node.parent is not None and not isinstance(node.parent, exp.Select):
        node = node.parent
    if node.arg_key != "expressions":  # used in WHERE, JOIN, GROUP BY, ORDER BY: never output
        return True
    parent = column.parent.parent if isinstance(column.parent, exp.Distinct) else column.parent
    return isinstance(parent, exp.Count) or (isinstance(parent, exp.Alias) and isinstance(parent.parent, exp.Select))


def _protect_small_groups(tree: exp.Query, policy: DataPolicy, min_size: int) -> bool:
    """Aggregates describing customers need enough of them: the outer query reports group sizes, inner ones drop small groups."""
    if not _describes_customers(tree, policy):
        return False  # sales by brand, category or month say nothing about a person, however few bought
    scopes = traverse_scope(tree)
    for scope in scopes:
        if not _is_group_level(scope, policy) or not _reads_customers(scope, policy):
            continue
        customers = _count_customers(scope, policy)
        if customers is None:
            raise SqlRejected(
                "Can't check group sizes: aggregate customer data straight from the tables, "
                "or select the customer ID in the subquery or CTE."
            )
        if scope is scopes[-1]:
            scope.expression.select(customers.as_(GROUP_SIZE), copy=False)
            return True
        scope.expression.having(exp.GTE(this=customers, expression=exp.Literal.number(min_size)), copy=False)
    return False


def _describes_customers(tree: exp.Query, policy: DataPolicy) -> bool:
    """Whether the query uses who customers are (demographics, location), not just what they bought."""
    for scope in traverse_scope(tree):
        for column in scope.columns:
            source = scope.sources.get(column.table)
            if not isinstance(source, exp.Table):
                continue
            access = _column_access(column, scope, policy)
            if access is Access.QUASI_IDENTIFIER:
                return True
            if policy.tables[source.name.lower()].describes_customers and access is not Access.PSEUDONYMIZE:
                return True
    return False


def _is_group_level(scope: Scope, policy: DataPolicy) -> bool:
    """Aggregates rows, but not one group per customer."""
    select = scope.expression
    if not isinstance(select, exp.Select):
        return False
    group = select.args.get("group")
    if group:
        return not any(isinstance(e, exp.Column) and _is_person_id(e, scope, policy) for e in group.expressions)
    return any(not isinstance(agg.parent, exp.Window) for agg in scope.find_all(exp.AggFunc))


def _reads_customers(scope: Scope, policy: DataPolicy) -> bool:
    """Whether rows of individual customers flow into this scope."""
    sources = [source for _, source in scope.selected_sources.values()]
    return any(_exposes_customers(s, policy) for s in [*sources, *scope.set_operation_scopes, *scope.subquery_scopes])


def _exposes_customers(source: exp.Table | Scope, policy: DataPolicy) -> bool:
    if isinstance(source, exp.Table):
        return policy.person_column(source.name.lower()) is not None
    return not _is_group_level(source, policy) and _reads_customers(source, policy)


def _count_customers(scope: Scope, policy: DataPolicy) -> exp.Count | None:
    for alias, (_, source) in scope.selected_sources.items():
        if column := _person_column_of(source, policy):
            return exp.Count(this=exp.Distinct(expressions=[exp.column(column, table=alias)]))
    return None


def _person_column_of(source: exp.Table | Scope, policy: DataPolicy) -> str | None:
    if isinstance(source, exp.Table):
        return policy.person_column(source.name.lower())
    if not isinstance(source.expression, exp.Select):
        return None
    return next(
        (
            p.alias_or_name
            for p in source.expression.selects
            if isinstance(p.unalias(), exp.Column) and _is_person_id(p.unalias(), source, policy)
        ),
        None,
    )


def _bind_tables(tree: exp.Query, policy: DataPolicy, scope: DataScope, ctes: set[str]) -> None:
    """Point every table at the dataset, wrapped in the user's scope filter."""
    for table in list(tree.find_all(exp.Table)):
        if _is_cte(table, ctes):
            continue
        name, alias = table.name.lower(), table.alias_or_name
        if scope.all_data:
            table.replace(_full_table(name, policy).as_(alias))
        else:
            rows = exp.select("*").from_(_full_table(name, policy)).where(_scope_filter(name, policy, scope))
            table.replace(rows.subquery(alias))


def _scope_filter(name: str, policy: DataPolicy, scope: DataScope) -> exp.Expression:
    """Rows of the scope table matching the user's filters, or rows linked to those."""
    if name == policy.scope_table:
        return exp.and_(*(exp.column(col).isin(*values) for col, values in scope.filters.items()))
    link = policy.tables[name].belongs_via
    if link is None:
        raise SqlRejected(f"Table {name} is not available within your data scope.")
    linked = exp.select(link.key).from_(_full_table(link.table, policy)).where(_scope_filter(link.table, policy, scope))
    return exp.column(link.column).isin(query=linked)


def _full_table(name: str, policy: DataPolicy) -> exp.Table:
    catalog, dataset = policy.dataset.split(".")
    return exp.table_(name, db=dataset, catalog=catalog, quoted=True)


def _row_cap(tree: exp.Query, max_rows: int) -> int:
    limit = tree.args.get("limit")
    current = limit.expression if limit else None
    if isinstance(current, exp.Literal) and current.is_int:
        return min(int(current.this), max_rows)
    return max_rows
