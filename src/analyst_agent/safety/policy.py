"""Allowlist of tables and columns the agent may touch. Anything unlisted is blocked."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from analyst_agent.config import ConfigError


class Access(str, Enum):
    """How a column may be used: freely, as a pseudonymized person ID, or only in aggregates."""

    ALLOW = "allow"
    PSEUDONYMIZE = "pseudonymize"
    QUASI_IDENTIFIER = "quasi_identifier"


class ColumnPolicy(BaseModel):
    type: str
    access: Access
    description: str = ""


class OwnerLink(BaseModel):
    """A row belongs to the user when `column` matches `via` (table.column) on one of their rows."""

    column: str
    via: str = Field(pattern=r"^\w+\.\w+$")

    @property
    def table(self) -> str:
        return self.via.split(".")[0]

    @property
    def key(self) -> str:
        return self.via.split(".")[1]


class TablePolicy(BaseModel):
    description: str = ""
    columns: dict[str, ColumnPolicy]
    blocked: list[str] = []
    belongs_via: OwnerLink | None = None
    describes_customers: bool = False  # its columns (except the person ID) are about the customers themselves


class DataPolicy(BaseModel):
    """Allowed tables and columns, and the table that user scopes filter on."""

    dataset: str
    scope_table: str
    tables: dict[str, TablePolicy]

    def person_column(self, table: str) -> str | None:
        """The column linking a table's rows to a customer, if any."""
        columns = self.tables[table].columns
        return next((name for name, col in columns.items() if col.access is Access.PSEUDONYMIZE), None)

    def blocked_columns(self) -> set[str]:
        return {name for table in self.tables.values() for name in table.blocked}

    def reaches_scope_table(self, table: str) -> bool:
        for _ in self.tables:  # a longer path would be a cycle
            if table == self.scope_table:
                return True
            link = self.tables[table].belongs_via
            parent = self.tables.get(link.table) if link else None
            if parent is None or link.key not in parent.columns:
                return False
            table = link.table
        return False


def load_policy(path: Path) -> DataPolicy:
    """Loads and validates data_policy.yaml; raises ConfigError if it is invalid."""
    try:
        policy = DataPolicy.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"Invalid data policy {path}: {exc}") from exc
    if policy.scope_table not in policy.tables:
        raise ConfigError(f"Unknown scope table {policy.scope_table}")
    for name, table in policy.tables.items():
        if overlap := set(table.columns) & set(table.blocked):
            raise ConfigError(f"Columns both allowed and blocked in {name}: {sorted(overlap)}")
        if table.belongs_via and not policy.reaches_scope_table(name):
            raise ConfigError(f"belongs_via of {name} does not lead to {policy.scope_table}")
    return policy
