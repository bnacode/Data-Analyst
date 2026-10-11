"""Last check before text is shown or saved: redact emails and phone numbers."""

from __future__ import annotations

import re

import structlog

log = structlog.get_logger(__name__)

PATTERNS = {
    "email": re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    "phone": re.compile(r"(?<![\w.])(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\w|\.\d)"),
}
REDACTED = "[REDACTED]"


def redact_pii(text: str) -> str:
    """Replaces emails and phone numbers with [REDACTED] and logs how many were found."""
    hits = {}
    for kind, pattern in PATTERNS.items():
        text, count = pattern.subn(REDACTED, text)
        if count:
            hits[kind] = count
    if hits:
        log.warning("pii_redacted", **hits)
    return text
