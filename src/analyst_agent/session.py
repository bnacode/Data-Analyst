"""The signed-in user. Built by code from config, never from the LLM."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ValidationError

from analyst_agent.config import ConfigError
from analyst_agent.safety.policy import DataPolicy


class Role(str, Enum):
    VIEWER = "viewer"
    MAINTAINER = "maintainer"
    ADMIN = "admin"


class AccessDenied(Exception):
    pass


@dataclass(frozen=True)
class DataScope:
    """Products a user may analyze. No filters and not all_data means no data."""

    filters: dict[str, tuple[str, ...]] = field(default_factory=dict)
    all_data: bool = False

    def describe(self) -> str:
        if self.all_data:
            return "all products"
        if not self.filters:
            return "no data"
        return " and ".join(f"{col} in ({', '.join(values)})" for col, values in self.filters.items())


@dataclass
class SessionContext:
    """The signed-in user and the current conversation."""

    user_id: str
    name: str
    role: Role
    scope: DataScope
    thread_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    notebook: list[str] = field(default_factory=list)  # masked findings gathered in this thread

    def new_thread(self) -> None:
        self.thread_id = uuid.uuid4().hex[:12]
        self.notebook.clear()


class Group(BaseModel):
    scope: Literal["all"] | dict[str, list[str]]


class User(BaseModel):
    name: str
    role: Role
    group: str


class AccessConfig(BaseModel):
    groups: dict[str, Group]
    users: dict[str, User]


def load_access(path: Path, policy: DataPolicy) -> AccessConfig:
    """Loads and validates access.yaml; raises ConfigError if it is invalid."""
    try:
        config = AccessConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"Invalid access config {path}: {exc}") from exc
    for user_id, user in config.users.items():
        if user.group not in config.groups:
            raise ConfigError(f"User {user_id!r} references unknown group {user.group!r}")
    scope_columns = policy.tables[policy.scope_table].columns
    for name, group in config.groups.items():
        if group.scope != "all" and (unknown := set(group.scope) - set(scope_columns)):
            raise ConfigError(f"Group {name!r} scopes on unknown {policy.scope_table} columns: {sorted(unknown)}")
    return config


def create_session(user_id: str, access_path: Path, policy: DataPolicy) -> SessionContext:
    """Session for a known user with their group's data scope; raises AccessDenied otherwise."""
    config = load_access(access_path, policy)
    user = config.users.get(user_id)
    if user is None:
        raise AccessDenied(f"Unknown user {user_id!r}. Known users: {', '.join(sorted(config.users))}")
    scope = config.groups[user.group].scope
    if scope == "all":
        data_scope = DataScope(all_data=True)
    else:
        data_scope = DataScope(filters={col: tuple(values) for col, values in scope.items() if values})
    return SessionContext(user_id, user.name, user.role, data_scope)
