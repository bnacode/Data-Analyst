"""Tool results that report failure: the model still reads the text, while metrics and logs count an error."""

from __future__ import annotations


def tool_error(text: str) -> dict:
    return {"status": "error", "content": [{"text": text}]}
