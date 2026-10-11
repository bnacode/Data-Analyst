"""Runtime settings, loaded from environment variables and the .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_ENV = ("GOOGLE_CLOUD_PROJECT", "GEMINI_API_KEY")
DEFAULT_MODEL = "gemini-3.5-flash-lite"
# USD per 1M tokens (input, output), for the /metrics cost estimate. Check https://ai.google.dev/pricing
MODEL_PRICES = {
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.5-flash": (1.50, 9.00),
}


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    """App settings and safety limits."""

    gcp_project: str
    gemini_api_key: str
    coordinator_model: str  # routing and chat: every message, so cheap and fast
    analyst_model: str  # writes and repairs SQL
    report_model: str  # long-form reports
    config_dir: Path = ROOT / "config"
    var_dir: Path = ROOT / "var"
    max_message_chars: int = 2000
    max_result_rows: int = 200
    min_group_size: int = 5
    max_bytes_billed: int = 10**9
    query_timeout_s: int = 60
    max_turns: int = 8  # coordinator steps per user message
    max_sql_queries: int = 5  # per question, all queries
    max_sql_failures: int = 3  # per question: failed queries (first try + 2 repairs) before giving up
    max_report_analyses: int = 4  # extra analyst calls per report
    pseudonym_key: bytes | None = None  # random per run when unset

    @property
    def reports_db(self) -> Path:
        return self.var_dir / "reports.db"


def load_dotenv(path: Path) -> None:
    """Minimal .env loader; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_settings() -> Settings:
    """Settings from the environment and .env; raises ConfigError if required values are missing."""
    load_dotenv(ROOT / ".env")
    missing = [key for key in REQUIRED_ENV if not os.environ.get(key)]
    if missing:
        raise ConfigError(
            f"Missing required environment variables: {', '.join(missing)}. "
            "Copy .env.example to .env and fill them in."
        )
    coordinator_model = os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
    return Settings(
        gcp_project=os.environ["GOOGLE_CLOUD_PROJECT"],
        gemini_api_key=os.environ["GEMINI_API_KEY"],
        coordinator_model=coordinator_model,
        analyst_model=os.environ.get("GEMINI_ANALYST_MODEL") or coordinator_model,
        report_model=os.environ.get("GEMINI_REPORT_MODEL") or coordinator_model,
        pseudonym_key=os.environ.get("PSEUDONYM_KEY", "").encode() or None,
    )
