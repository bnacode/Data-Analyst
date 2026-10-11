"""Saved reports in SQLite plus a markdown file each. Every query is filtered by owner, so users only ever see their own."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from analyst_agent.safety.masker import PSEUDONYM
from analyst_agent.safety.output_scan import redact_pii

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,  -- ids are never reused, so a stale id can't hit a new report
    owner_id   TEXT NOT NULL,
    thread_id  TEXT NOT NULL,
    title      TEXT NOT NULL,
    summary    TEXT NOT NULL,
    body       TEXT NOT NULL,
    tags       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reports_owner ON reports (owner_id);
CREATE TABLE IF NOT EXISTS audit_events (  -- metadata only, never report content
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    event      TEXT NOT NULL,
    report_ids TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""
COLUMNS = "id, thread_id, title, summary, body, tags, created_at"
SEARCHABLE = "lower(title || ' ' || summary || ' ' || body || ' ' || tags)"


@dataclass(frozen=True)
class SavedReport:
    """A report as stored in the library."""

    id: int
    thread_id: str
    title: str
    summary: str
    body: str
    tags: tuple[str, ...]
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> SavedReport:
        return cls(**{**dict(row), "tags": tuple(filter(None, row["tags"].split(",")))})

    def text(self) -> str:
        return " ".join([self.title, self.summary, self.body, *self.tags])

    def line(self) -> str:
        return f"#{self.id} ({self.created_at[:10]}) {self.title}: {self.summary}"

    def markdown(self) -> str:
        return f"# {self.title}\n\n{self.summary}\n\n{self.body}\n\nReport #{self.id} | {self.created_at[:10]} | tags: {', '.join(self.tags)}"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def number_customers(texts: list[str]) -> list[str]:
    """Pseudonyms change every run, so a saved report numbers its customers instead: Customer 1, 2, ..."""
    numbers: dict[str, str] = {}

    def label(match: re.Match) -> str:
        return numbers.setdefault(match.group(), f"Customer {len(numbers) + 1}")

    return [PSEUDONYM.sub(label, text) for text in texts]


class ReportStore:
    """SQLite library of saved reports. Every query is filtered by owner."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.files = path.parent / "reports"
        self.files.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        # A connection per call: tools may run on worker threads
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, owner_id: str, thread_id: str, title: str, summary: str, body: str, tags: list[str]) -> SavedReport:
        """Redacts PII, numbers customers, stores the report and writes its markdown file."""
        clean_tags = ",".join(redact_pii(tag.strip().lower().replace(",", " ")) for tag in tags if tag.strip())
        texts = number_customers([redact_pii(text) for text in (title, summary, body)])
        with self._db() as db:
            cursor = db.execute(
                "INSERT INTO reports (owner_id, thread_id, title, summary, body, tags, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (owner_id, thread_id, *texts, clean_tags, now()),
            )
        report = self.get(owner_id, cursor.lastrowid)
        self.file(report.id).write_text(report.markdown(), encoding="utf-8")
        return report

    def file(self, report_id: int) -> Path:
        return self.files / f"report-{report_id}.md"

    def get(self, owner_id: str, report_id: int) -> SavedReport | None:
        with self._db() as db:
            row = db.execute(f"SELECT {COLUMNS} FROM reports WHERE owner_id = ? AND id = ?", (owner_id, report_id)).fetchone()
        return SavedReport.from_row(row) if row else None

    def list(self, owner_id: str, term: str = "") -> list[SavedReport]:
        """Newest first; `term` is a case-insensitive keyword match on all text fields."""
        with self._db() as db:
            rows = db.execute(
                f"SELECT {COLUMNS} FROM reports WHERE owner_id = ? AND instr({SEARCHABLE}, lower(?)) > 0 ORDER BY id DESC",
                (owner_id, term.strip()),
            ).fetchall()
        return [SavedReport.from_row(row) for row in rows]

    def find(self, owner_id: str, ids: Collection[int] = (), term: str = "", thread_id: str | None = None) -> list[SavedReport]:
        """Reports matching every given filter. `term` matches whole words only, so "MG" never hits "img"."""
        word = re.compile(rf"(?<!\w){re.escape(term.strip())}(?!\w)", re.IGNORECASE)
        return [
            report
            for report in self.list(owner_id, term)
            if (not ids or report.id in ids)
            and (thread_id is None or report.thread_id == thread_id)
            and (not term.strip() or word.search(report.text()))
        ]

    def delete(self, owner_id: str, ids: Collection[int]) -> list[int]:
        """Rows go first, in one transaction; a leftover file is harmless, a row without its file is not."""
        if not ids:
            return []
        marks = ", ".join("?" * len(ids))
        with self._db() as db:
            rows = db.execute(
                f"DELETE FROM reports WHERE owner_id = ? AND id IN ({marks}) RETURNING id", (owner_id, *ids)
            ).fetchall()
        deleted = sorted(row["id"] for row in rows)
        for report_id in deleted:
            self.file(report_id).unlink(missing_ok=True)
        return deleted

    def audit(self, user_id: str, event: str, report_ids: Collection[int]) -> None:
        with self._db() as db:
            db.execute(
                "INSERT INTO audit_events (user_id, event, report_ids, created_at) VALUES (?, ?, ?, ?)",
                (user_id, event, ",".join(map(str, report_ids)), now()),
            )

    def audit_events(self, user_id: str) -> list[tuple[str, str]]:
        with self._db() as db:
            rows = db.execute("SELECT event, report_ids FROM audit_events WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
        return [(row["event"], row["report_ids"]) for row in rows]
