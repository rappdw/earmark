"""Logging setup: one stderr handler that gates, scrubs, and redacts.

Earmark logs hosts only. Third-party libraries do not: HTTP clients log full request
URLs (paths and query strings that can carry session ids), uvicorn logs the query of
every request. So the one handler does three things on top of secret redaction:

- It drops records below WARNING from the wire loggers in ``QUIET_LOGGERS``, whatever
  level a library or an environment variable (``HF_HUB_VERBOSITY``) gave them. The price is
  that ``LOG_LEVEL=DEBUG`` does not show their DEBUG output.
- It rewrites URLs in third-party records, and in every traceback, to
  ``scheme://host[:port]/...``, and drops the query from HTTP request lines.
- It is the only handler: ``huggingface_hub``'s own stderr handler is removed.

Not covered: hf_xet writes its own log files (see docs/deploy.md).
"""

import contextlib
import importlib
import logging
import re
import sys
from collections.abc import Iterable
from typing import Final, TextIO

from earmark.config import REDACTED

HANDLER_NAME: Final = "earmark"
# Wire loggers: HTTP clients print every request URL at INFO and response headers at
# DEBUG; the huggingface_hub client logs request URLs. Their levels are set to WARNING, and the
# handler's filter drops what is below WARNING even if a library resets the level.
QUIET_LOGGERS: Final = (
    "httpx",
    "httpcore",
    "httpx2",
    "httpcore2",
    "urllib3",
    "huggingface_hub",
    "yt_dlp",
)
LIBRARY_HANDLER_LOGGERS: Final = ("huggingface_hub",)
OWN_LOGGER: Final = "earmark"
URL_ELISION: Final = "/…"
LOG_FORMAT: Final = "%(asctime)s %(levelname)s %(name)s: %(message)s"


_URL: Final = re.compile(
    r"\b(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)"
    r"(?:[^\s/?#@'\"<>`]*@)?"
    r"(?P<host>[^\s/?#'\"<>`]*)"
    r"(?P<rest>[/?#][^\s'\"<>`]*)?"
)
_REQUEST_QUERY: Final = re.compile(r'("[A-Z]+ [^\s?"]*)\?\S*?( HTTP/\d(?:\.\d)?")')


def scrub_urls(text: str) -> str:
    """Reduce every URL to ``scheme://host[:port]/…`` and drop queries from request lines."""

    def elide(match: re.Match[str]) -> str:
        return match["scheme"] + match["host"] + (URL_ELISION if match["rest"] else "")

    text = _URL.sub(elide, text)
    return _REQUEST_QUERY.sub(r"\1\2", text)


def is_own_logger(name: str) -> bool:
    return name == OWN_LOGGER or name.startswith(OWN_LOGGER + ".")


def is_quiet_logger(name: str) -> bool:
    return any(name == entry or name.startswith(entry + ".") for entry in QUIET_LOGGERS)


class QuietLoggerFilter(logging.Filter):
    """Drop records below WARNING from the wire loggers, whatever their logger level."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING or not is_quiet_logger(record.name)


def adopt_library_loggers() -> None:
    """Remove handlers libraries install on their own loggers, so only ours prints.

    ``huggingface_hub`` adds a bare stderr handler when ``huggingface_hub.utils.logging``
    is first imported, and its records also propagate to the root handler. Never imports
    ``huggingface_hub`` itself; idempotent.
    """
    if sys.modules.get("huggingface_hub") is not None:
        with contextlib.suppress(ImportError):
            importlib.import_module("huggingface_hub.utils.logging")
    for name in LIBRARY_HANDLER_LOGGERS:
        library = logging.getLogger(name)
        for handler in list(library.handlers):
            if not isinstance(handler, logging.NullHandler):
                library.removeHandler(handler)


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every non-empty secret in ``text`` with ``REDACTED``, longest first."""
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


class RedactingFormatter(logging.Formatter):
    def __init__(
        self, secrets: Iterable[str], fmt: str | None = None, datefmt: str | None = None
    ) -> None:
        super().__init__(fmt, datefmt)
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if is_own_logger(record.name):
            # Own messages are host-only by contract; scrub only the traceback and stack.
            head = self.formatMessage(record)
            if text.startswith(head):
                text = head + scrub_urls(text[len(head) :])
            else:
                text = scrub_urls(text)
        else:
            text = scrub_urls(text)
        return redact(text, self._secrets)


def configure_logging(
    level: str = "INFO", secrets: Iterable[str] = (), stream: TextIO | None = None
) -> logging.Handler:
    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() == HANDLER_NAME:
            root.removeHandler(existing)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.set_name(HANDLER_NAME)
    handler.setFormatter(RedactingFormatter(secrets, LOG_FORMAT))
    handler.addFilter(QuietLoggerFilter())
    root.addHandler(handler)
    root.setLevel(level)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(logging.WARNING, root.level))
    adopt_library_loggers()
    return handler
