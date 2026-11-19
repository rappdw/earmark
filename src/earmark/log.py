"""Logging setup with a formatter that redacts secret values."""

import logging
import sys
from collections.abc import Iterable
from typing import Final, TextIO

from earmark.config import REDACTED

HANDLER_NAME: Final = "earmark"
LOG_FORMAT: Final = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class RedactingFormatter(logging.Formatter):
    def __init__(
        self, secrets: Iterable[str], fmt: str | None = None, datefmt: str | None = None
    ) -> None:
        super().__init__(fmt, datefmt)
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text


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
    root.addHandler(handler)
    root.setLevel(level)
    return handler
