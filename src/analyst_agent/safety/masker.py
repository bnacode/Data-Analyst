"""Cleans query results before the model sees them."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field

import pandas as pd

from analyst_agent.safety.sql_guard import GROUP_SIZE, GuardedSql

PSEUDONYM = re.compile(r"Customer #[0-9A-F]{8}")


class Pseudonymizer:
    """Stable labels for customer IDs, keyed so they can't be reversed by hashing guesses."""

    def __init__(self, key: bytes | None = None) -> None:
        self._key = key or secrets.token_bytes(32)

    def label(self, customer_id: object) -> str | None:
        """Pseudonym like 'Customer #3F9A1C2E', the same for the same ID within a session."""
        if pd.isna(customer_id):
            return None
        if isinstance(customer_id, float) and customer_id.is_integer():
            customer_id = int(customer_id)  # IDs come back as floats when the column has nulls
        digest = hmac.new(self._key, str(customer_id).encode(), hashlib.sha256).hexdigest()
        return f"Customer #{digest[:8].upper()}"


@dataclass
class MaskedResult:
    frame: pd.DataFrame
    notes: list[str] = field(default_factory=list)


def mask_result(
    frame: pd.DataFrame,
    guarded: GuardedSql,
    pseudonymizer: Pseudonymizer,
    blocked_columns: set[str],
    min_group_size: int,
) -> MaskedResult:
    """Drops blocked columns, pseudonymizes customer IDs and hides groups below the minimum size."""
    notes = []
    # Second line of defence behind the guard
    frame = frame.drop(columns=[col for col in frame.columns if col.lower() in blocked_columns])
    for col in guarded.person_columns & set(frame.columns):
        frame[col] = frame[col].map(pseudonymizer.label)
    if guarded.has_group_size and GROUP_SIZE in frame.columns:
        small = frame[GROUP_SIZE] < min_group_size
        if small.any():
            notes.append(f"{small.sum()} group(s) with fewer than {min_group_size} customers hidden for privacy.")
        frame = frame[~small].drop(columns=GROUP_SIZE)
    return MaskedResult(frame.reset_index(drop=True), notes)
