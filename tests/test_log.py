import io
import logging

import pytest

from earmark.config import REDACTED
from earmark.log import HANDLER_NAME, configure_logging, redact

pytestmark = pytest.mark.usefixtures("restore_root_logging")

log = logging.getLogger("earmark.test")


def test_redacts_secret_in_message_and_args() -> None:
    stream = io.StringIO()
    configure_logging("INFO", ["s3cret-value"], stream)
    log.info("token s3cret-value here")
    log.info("arg %s", "s3cret-value")
    out = stream.getvalue()
    assert "s3cret-value" not in out
    assert out.count(REDACTED) == 2


def test_redacts_secret_in_traceback() -> None:
    stream = io.StringIO()
    configure_logging("INFO", ["tb-secret-9"], stream)
    try:
        raise ValueError("failed with tb-secret-9")
    except ValueError:
        log.exception("boom")
    out = stream.getvalue()
    assert "Traceback" in out
    assert "tb-secret-9" not in out
    assert REDACTED in out


def test_longer_secret_redacted_first() -> None:
    stream = io.StringIO()
    configure_logging("INFO", ["abc", "abcdef"], stream)
    log.info("abcdef")
    out = stream.getvalue()
    assert "def" not in out.split(": ", 1)[1]
    assert out.rstrip().endswith(f"earmark.test: {REDACTED}")


def test_empty_secret_ignored() -> None:
    stream = io.StringIO()
    configure_logging("INFO", [""], stream)
    log.info("hello")
    assert out_message(stream) == "hello"


def out_message(stream: io.StringIO) -> str:
    return stream.getvalue().rstrip().split(": ", 1)[1]


def test_configure_twice_single_handler() -> None:
    configure_logging("INFO", (), io.StringIO())
    configure_logging("INFO", (), io.StringIO())
    named = [h for h in logging.getLogger().handlers if h.get_name() == HANDLER_NAME]
    assert len(named) == 1


def test_level_applied() -> None:
    stream = io.StringIO()
    configure_logging("WARNING", (), stream)
    assert logging.getLogger().level == logging.WARNING
    log.info("quiet")
    log.warning("loud")
    assert "quiet" not in stream.getvalue()
    assert "loud" in stream.getvalue()


def test_redact() -> None:
    assert redact("abcdef and abc", ["abc", "abcdef"]) == f"{REDACTED} and {REDACTED}"
    assert redact("unchanged", ["", "zzz"]) == "unchanged"
    assert redact("x", []) == "x"


def test_http_client_loggers_do_not_log_urls_at_info() -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    try:
        logging.getLogger("httpx2").info("HTTP Request: GET https://x.example/secret/path?sid=1")
        logging.getLogger("httpx2").warning("kept")
    finally:
        for name in ("httpx2", "httpcore2"):
            logging.getLogger(name).setLevel(logging.NOTSET)
    assert "secret/path" not in stream.getvalue()
    assert "kept" in stream.getvalue()
