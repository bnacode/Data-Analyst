"""Plain-text logs: [timestamp] bound context, level, logger, event, then the event's own fields."""

from __future__ import annotations

import logging
from pathlib import Path

import structlog

NOISY_LOGGERS = ("httpx", "httpcore", "google", "google_genai", "urllib3", "strands")


def key_values(fields: dict) -> list[str]:
    return [f"{key}={value}" for key, value in fields.items()]


def render_line(_, __, event_dict: dict) -> str:
    timestamp = event_dict.pop("timestamp")
    head = f"{event_dict.pop('level').upper()} {event_dict.pop('logger', '-')} {event_dict.pop('event')}"
    exception = event_dict.pop("exception", None)
    bound = {k: event_dict.pop(k) for k in structlog.contextvars.get_contextvars() if k in event_dict}
    line = " ".join([f"[{timestamp}]", *key_values(bound), head, *key_values(event_dict)])
    return f"{line}\n{exception}" if exception else line


def not_closed_loop_noise(record: logging.LogRecord) -> bool:
    """genai clients close themselves when garbage collected, sometimes after Strands closed their event loop. Harmless."""
    exc = record.exc_info[1] if record.exc_info else None
    return not (isinstance(exc, RuntimeError) and str(exc) == "Event loop is closed")


def setup_logging(log_file: Path, *extra_processors, level: int = logging.INFO) -> None:
    """extra_processors see only the app's own events, e.g. the metrics counter."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    structlog.configure(
        processors=[*shared, *extra_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    # Library logs (stdlib logging) go through the same pipeline, so every line has the same format
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                # Plain traceback without local variables: they could hold query results
                structlog.processors.format_exc_info,
                render_line,
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("asyncio").addFilter(not_closed_loop_noise)
    # Strands logs every failed cycle at ERROR, then re-raises it; we log each failure ourselves, with its cause
    logging.getLogger("strands.event_loop.event_loop").setLevel(logging.CRITICAL)
