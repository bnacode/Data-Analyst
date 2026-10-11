"""CLI chat loop. Errors from a turn never reach the terminal."""

from __future__ import annotations

import argparse
import sys
import time
import uuid

import structlog
from google.auth.exceptions import DefaultCredentialsError
from rich.console import Console
from rich.markdown import Markdown

from analyst_agent.agents.coordinator import build_coordinator
from analyst_agent.agents.models import error_cause, model_rejected, model_unavailable
from analyst_agent.config import MODEL_PRICES, ConfigError, load_settings
from analyst_agent.data.bq_client import BigQueryRunner
from analyst_agent.observability.log import setup_logging
from analyst_agent.observability.metrics import SessionMetrics
from analyst_agent.reports.deletion import YES, Deletions
from analyst_agent.reports.store import ReportStore
from analyst_agent.safety.masker import Pseudonymizer
from analyst_agent.safety.output_scan import redact_pii
from analyst_agent.safety.policy import load_policy
from analyst_agent.session import AccessDenied, create_session
from analyst_agent.tools.sql import DataAccess

HELP = """\
Commands:
  /whoami   show your user, role and data scope
  /reports  list your saved reports
  /metrics  show usage, cost and events for this session
  /new      start a new conversation
  /help     show this help
  /exit     quit"""
MODEL_DOWN_MESSAGE = "The AI service is busy or unavailable right now. Please try again in a minute."
MODEL_REJECTED_MESSAGE = "The AI service rejected the request. Check GEMINI_API_KEY and the model names in .env."
BUDGET_MESSAGE = "That took more steps than one message allows. Try a narrower question or split it into parts."

log = structlog.get_logger("analyst_agent.cli")


class ChatApp:
    """Terminal chat loop: commands, messages, and deletion confirmations."""

    def __init__(self, access: DataAccess, store: ReportStore, console: Console) -> None:
        self.access = access
        self.store = store
        self.settings = access.settings
        self.session = access.session
        self.console = console
        self.deletions = Deletions(store, self.session)
        self.note = ""  # outcome of a confirmation, passed to the agent with the next message
        self.start_thread()

    def start_thread(self) -> None:
        self.agent = build_coordinator(self.access, self.store, self.deletions)
        structlog.contextvars.bind_contextvars(user_id=self.session.user_id, thread_id=self.session.thread_id)
        log.info("thread_started")

    def run(self) -> None:
        self.console.print(f"[bold]Analyst assistant[/bold] - signed in as {self.session.name}. Type /help for commands.")
        while True:
            try:
                message = self.console.input(f"\n[bold cyan]{self.session.user_id}>[/bold cyan] ").strip()
            except (EOFError, KeyboardInterrupt):
                self.deletions.cancel()
                self.console.print("\nBye.")
                return
            if not self.dispatch(message):
                return

    def dispatch(self, message: str) -> bool:
        """Returns False when the app should exit."""
        if not message:
            return True
        if message.startswith("/"):
            return self.handle_command(message)
        self.handle_message(message)
        if self.deletions.pending:
            return self.dispatch(self.confirm_deletion())
        return True

    def handle_command(self, command: str) -> bool:
        """Returns False when the app should exit."""
        name = command.split()[0].lower()
        if name in ("/exit", "/quit"):
            self.deletions.cancel()
            self.console.print("Bye.")
            return False
        if name == "/whoami":
            s = self.session
            self.console.print(f"{s.name} ({s.user_id}), {s.role.value}, scope: {s.scope.describe()}, thread {s.thread_id}")
        elif name == "/reports":
            reports = self.store.list(self.session.user_id)
            self.console.print("\n".join(r.line() for r in reports) or "No saved reports yet.", markup=False)
        elif name == "/metrics":
            self.console.print(self.access.metrics.report(MODEL_PRICES), markup=False)
        elif name == "/new":
            self.deletions.cancel()
            self.note = ""
            self.session.new_thread()
            self.start_thread()
            self.console.print(f"New conversation started (thread {self.session.thread_id}).")
        elif name == "/help":
            self.console.print(HELP)
        else:
            self.console.print(f"Unknown command {name}. Type /help.")
        return True

    def handle_message(self, message: str) -> None:
        if len(message) > self.settings.max_message_chars:
            self.console.print(f"[yellow]That message is too long (max {self.settings.max_message_chars} characters). Please shorten it.[/yellow]")
            return
        turn_id = uuid.uuid4().hex[:8]
        # Every log line of this turn, including those from tools and subagents, carries the turn_id
        with structlog.contextvars.bound_contextvars(turn_id=turn_id):
            self.answer(message, turn_id)

    def answer(self, message: str, turn_id: str) -> None:
        """Runs one agent turn and prints the redacted answer, turning errors into friendly messages."""
        log.info("turn_started", message=redact_pii(message))
        started = time.perf_counter()
        try:
            prompt, self.note = (f"{self.note}\n\n{message}" if self.note else message), ""
            with self.console.status("Thinking..."):
                result = self.agent(prompt, limits={"turns": self.settings.max_turns})
            if result.stop_reason == "limit_turns":
                log.warning("turn_budget_exhausted")
                self.console.print(f"[yellow]{BUDGET_MESSAGE}[/yellow]")
                return
            answer = redact_pii(str(result).strip())
            self.console.print(Markdown(answer))
            duration_ms = round((time.perf_counter() - started) * 1000)
            self.access.metrics.turn_ms.append(duration_ms)
            log.info("turn_completed", duration_ms=duration_ms, answer_chars=len(answer))
        except KeyboardInterrupt:
            log.info("turn_cancelled")
            self.console.print("[yellow]Cancelled.[/yellow]")
        except Exception as exc:
            if model_unavailable(exc):
                log.warning("model_unavailable", agent="coordinator", error=error_cause(exc))
                self.console.print(f"[red]{MODEL_DOWN_MESSAGE}[/red]")
            elif model_rejected(exc):
                log.exception("model_rejected", agent="coordinator", error=error_cause(exc))
                self.console.print(f"[red]{MODEL_REJECTED_MESSAGE}[/red]")
            else:
                log.exception("turn_failed")
                self.console.print(f"[red]Sorry, something went wrong while answering. Please try again or rephrase. (ref {turn_id})[/red]")

    def confirm_deletion(self) -> str:
        """Asks the real user, outside the LLM. Returns any other input, to be handled as a new message."""
        pending = self.deletions.pending
        count = len(pending.ids)
        self.console.print(f"\n[bold yellow]About to delete {count} report(s):[/bold yellow]")
        self.console.print("\n".join(pending.lines), markup=False)
        question = f"Type {count} to delete them, anything else cancels: " if pending.needs_count else "Delete them? (yes/no): "
        try:
            answer = self.console.input(question).strip()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        confirmed = pending.confirmed_by(answer)
        outcome = self.deletions.confirm() if confirmed else self.deletions.cancel()
        self.console.print(outcome)
        self.note = f"[App note: {outcome}]"
        is_reply = confirmed or answer.lower() in (*YES, "no", "n")
        return "" if is_reply else answer


def main(argv: list[str] | None = None) -> int:
    """Entry point: loads config, signs the user in and starts the chat."""
    parser = argparse.ArgumentParser(prog="analyst", description="Data analysis chat assistant")
    parser.add_argument("--user", required=True, help="user id from config/access.yaml (prototype stand-in for SSO)")
    args = parser.parse_args(argv)

    # Windows consoles may default to cp1252, which can't print the spinner or model output
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    console = Console()
    try:
        settings = load_settings()
        metrics = SessionMetrics()
        setup_logging(settings.var_dir / "logs" / "agent.log", metrics.count_event)
        policy = load_policy(settings.config_dir / "data_policy.yaml")
        session = create_session(args.user, settings.config_dir / "access.yaml", policy)
        runner = BigQueryRunner(settings.gcp_project, settings.max_bytes_billed, settings.query_timeout_s)
        store = ReportStore(settings.reports_db)
    except (ConfigError, AccessDenied) as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    except DefaultCredentialsError:
        console.print("[red]No Google Cloud credentials. Run: gcloud auth application-default login[/red]")
        return 1
    access = DataAccess(settings, policy, session, runner, Pseudonymizer(settings.pseudonym_key), metrics)
    ChatApp(access, store, console).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
