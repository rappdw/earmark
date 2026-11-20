"""Helpers shared by the clients of operator-configured endpoints (remote ASR, local LLM).

Standard library plus ``earmark.log`` only, so it is safe to import anywhere.
"""

import json
import urllib.parse
from collections.abc import Sequence
from typing import Final

from earmark.log import redact

MAX_DETAIL_CHARS: Final = 200
MAX_HOST_CHARS: Final = 100


def host_label(base_url: str) -> str:
    """'host' or 'host:port' (IPv6 in brackets) for messages, capped at 100 characters."""
    parts = urllib.parse.urlsplit(base_url)
    name = parts.hostname or "?"
    if ":" in name:
        name = f"[{name}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    shown = name if port is None else f"{name}:{port}"
    return shown if len(shown) <= MAX_HOST_CHARS else shown[: MAX_HOST_CHARS - 1] + "…"


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _media_type(content_type: str | None) -> str | None:
    if content_type is None:
        return None
    return content_type.split(";")[0].strip().lower() or None


def _first_string(*candidates: object) -> str | None:
    for item in candidates:
        if isinstance(item, str):
            return item
    return None


def _json_detail(data: object) -> str | None:
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    detail = data.get("detail")
    detail_msg: object = None
    if isinstance(detail, list) and detail and isinstance(detail[0], dict):
        detail_msg = detail[0].get("msg")
    return _first_string(
        error.get("message") if isinstance(error, dict) else None,
        error,
        detail,
        detail_msg,
        data.get("message"),
    )


def error_detail(body: bytes, content_type: str | None, secrets: Sequence[str]) -> str | None:
    """A short, sanitised message from a server error body, or ``None``."""
    text = body.decode("utf-8", errors="replace")
    kind = _media_type(content_type)
    found: str | None = None
    if (kind is not None and "json" in kind) or text.lstrip().startswith("{"):
        try:
            found = _json_detail(json.loads(text))
        except ValueError:
            found = None
    elif kind == "text/plain":
        found = text
    if found is None:
        return None
    cleaned = _collapse("".join(c if c.isprintable() else " " for c in found))
    cleaned = redact(cleaned, secrets)[:MAX_DETAIL_CHARS]
    return cleaned or None
