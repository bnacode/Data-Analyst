"""Gemini model factory shared by the coordinator and subagents."""

from __future__ import annotations

import httpx
from google.genai import errors, types
from strands.models.gemini import GeminiModel
from strands.types.exceptions import ModelThrottledException

from analyst_agent.config import Settings

# The only retry layer: agents are built with retry_strategy=None so Strands doesn't retry on top of it
_RETRY = types.HttpRetryOptions(attempts=4, initial_delay=2, http_status_codes=[429, 500, 503, 504])

MODEL_DOWN = "The AI service is unavailable right now. Stop and tell the user to try again shortly."


def gemini_model(settings: Settings, model_id: str, temperature: float = 0.2) -> GeminiModel:
    """Gemini client with HTTP retries on rate limits and server errors."""
    return GeminiModel(
        client_args={
            "api_key": settings.gemini_api_key,
            "http_options": types.HttpOptions(retry_options=_RETRY),
        },
        model_id=model_id,
        params={"temperature": temperature},
    )


def _find(exc: BaseException | None, matches) -> bool:
    """Strands wraps model errors, so look down the cause chain."""
    while exc is not None:
        if matches(exc):
            return True
        exc = exc.__cause__
    return False


def _is_outage(exc: BaseException) -> bool:
    if isinstance(exc, errors.APIError):
        return exc.code == 429 or isinstance(exc, errors.ServerError)
    return isinstance(exc, (ModelThrottledException, httpx.TransportError))


def model_unavailable(exc: BaseException | None) -> bool:
    """Gemini still failing after the retries: rate limited, down, or unreachable. Worth trying again later."""
    return _find(exc, _is_outage)


def error_cause(exc: BaseException) -> str:
    """The root cause for logs, e.g. "429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier"."""
    while exc.__cause__ is not None:
        exc = exc.__cause__
    if not isinstance(exc, errors.APIError):
        return type(exc).__name__
    details = exc.details.get("error", {}).get("details", []) if isinstance(exc.details, dict) else []
    quotas = [v["quotaId"] for d in details for v in d.get("violations", []) if "quotaId" in v]
    return " ".join([f"{exc.code} {exc.status}", *quotas])


def model_rejected(exc: BaseException | None) -> bool:
    """Gemini refused the request itself (bad API key, unknown model): retrying won't help."""
    return not model_unavailable(exc) and _find(exc, lambda e: isinstance(e, errors.ClientError))
