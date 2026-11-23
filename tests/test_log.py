import io
import logging
import re
import subprocess
import sys
from pathlib import Path

import httpx2
import pytest

import earmark
from earmark.config import REDACTED, Settings
from earmark.log import (
    HANDLER_NAME,
    LIBRARY_HANDLER_LOGGERS,
    QUIET_LOGGERS,
    adopt_library_loggers,
    configure_logging,
    is_own_logger,
    is_quiet_logger,
    redact,
    scrub_urls,
)
from tests.uds_http import UdsHttpServer, UdsReply

pytestmark = pytest.mark.usefixtures("restore_root_logging", "restore_library_loggers")

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


# -- scrubbing and gating ------------------------------------------------------------------

SECRET_URL = "https://x.example/secret/path?sid=1"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            'GET https://huggingface.co/api/resolve-cache/m/x?%2Fa=&etag=%22c "HTTP/1.1 200 OK"',
            'GET https://huggingface.co/… "HTTP/1.1 200 OK"',
        ),
        (
            "Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)",
            "Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)",
        ),
        (
            '127.0.0.1:5 - "GET /jobs?status=failed&limit=5 HTTP/1.1" 200',
            '127.0.0.1:5 - "GET /jobs HTTP/1.1" 200',
        ),
        ('"GET /jobs?status=a"b&limit=5 HTTP/1.1" 200', '"GET /jobs HTTP/1.1" 200'),
        ('"GET /healthz HTTP/1.1" 200', '"GET /healthz HTTP/1.1" 200'),
        (
            "for url 'https://user:pw@h.example:8443/p?q=1#f'",
            "for url 'https://h.example:8443/…'",
        ),
        ("https://[::1]:8000/x", "https://[::1]:8000/…"),
        ("file:///tmp/a", "file:///…"),
        ("https://h.example", "https://h.example"),
        ("URL('https://h.example/a/b')", "URL('https://h.example/…')"),
    ],
)
def test_scrub_urls_examples(text: str, expected: str) -> None:
    assert scrub_urls(text) == expected


def test_logger_name_predicates() -> None:
    assert is_own_logger("earmark")
    assert is_own_logger("earmark.fetch")
    assert not is_own_logger("earmarker")
    assert not is_own_logger("root")
    assert is_quiet_logger("httpx")
    assert is_quiet_logger("huggingface_hub.utils._http")
    assert is_quiet_logger("httpx2")
    assert not is_quiet_logger("httpxy")
    assert not is_quiet_logger("uvicorn.access")


@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize("name", QUIET_LOGGERS)
def test_quiet_loggers_gated_even_when_unquieted(name: str, child: bool) -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    logger = logging.getLogger(name + ".child" if child else name)
    logging.getLogger(name).setLevel(logging.DEBUG)  # what a library's debug switch does
    logger.debug("debug %s", SECRET_URL)
    logger.info("info %s", SECRET_URL)
    logger.debug("header %r", b"/secret/path?sid=1")
    logger.info("header %r", b"/secret/path?sid=1")
    assert stream.getvalue() == ""
    logger.warning("retrying %s later", SECRET_URL)
    out = stream.getvalue()
    assert "retrying https://x.example/… later" in out
    assert "secret/path" not in out
    assert "sid=" not in out


@pytest.mark.parametrize("name", ["uvicorn.error", "root", "somelib.sub"])
def test_third_party_info_is_scrubbed(name: str) -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    logger = logging.getLogger() if name == "root" else logging.getLogger(name)
    logger.info("fetching %s", SECRET_URL)
    out = stream.getvalue()
    assert "fetching https://x.example/…" in out
    assert "secret/path" not in out


def test_own_messages_unchanged() -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    apple = "https://podcasts.apple.com/us/podcast/id1?i=2"
    logging.getLogger("earmark.submission").info("accepted %s", apple)
    logging.getLogger("earmark.fetch").debug("GET feeds.example (hop 0)")
    out = stream.getvalue()
    assert f"accepted {apple}" in out
    assert "GET feeds.example (hop 0)" in out


def test_own_traceback_scrubbed() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    try:
        raise ValueError("for url 'https://h.example/p?sid=1'")
    except ValueError:
        logging.getLogger("earmark.x").exception("failed for https://own.example/keep")
    out = stream.getvalue()
    assert "failed for https://own.example/keep" in out
    assert "for url 'https://h.example/…'" in out
    assert "sid=1" not in out


def test_cached_exc_text_scrubbed() -> None:
    try:
        raise ValueError("for url 'https://h.example/p?sid=1'")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "earmark.x", logging.ERROR, __file__, 1, "boom", None, sys.exc_info()
        )
    logging.Formatter().format(record)  # caches exc_text, unscrubbed
    assert record.exc_text is not None
    assert "sid=1" in record.exc_text
    stream = io.StringIO()
    handler = configure_logging("INFO", stream=stream)
    assert handler.formatter is not None
    out = handler.formatter.format(record)
    assert "sid=1" not in out
    assert "https://h.example/…" in out


def test_hf_retry_warning_kept_scrubbed() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    logging.getLogger("huggingface_hub.utils._http").warning(
        "'ConnectError' thrown while requesting GET "
        "https://cas-bridge.xethub.hf.co/xet-bridge-us/abc?X-Amz-Signature=dead"
    )
    assert (
        "'ConnectError' thrown while requesting GET https://cas-bridge.xethub.hf.co/…"
        in stream.getvalue()
    )
    assert "dead" not in stream.getvalue()


def test_httpx2_httpcore2_over_uds_log_no_paths() -> None:
    def reply(method: str, target: str) -> UdsReply:
        if target.startswith("/start"):
            return UdsReply(307, (("Location", "/next?sid=s2"),))
        return UdsReply(200, body=b"ok")

    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    for name in ("httpx2", "httpcore2"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    with (
        UdsHttpServer(reply) as server,
        httpx2.Client(
            transport=httpx2.HTTPTransport(uds=str(server.socket_path)),
            base_url="http://models.example",
            follow_redirects=True,
        ) as client,
    ):
        assert client.get("/start?sid=s1").status_code == 200
    assert [target for _, target in server.requests] == ["/start?sid=s1", "/next?sid=s2"]
    out = stream.getvalue()
    for leaked in ("sid=", "/start", "/next"):
        assert leaked not in out


def test_adopt_removes_library_handlers() -> None:
    other = io.StringIO()
    library = logging.getLogger(LIBRARY_HANDLER_LOGGERS[0])
    stray = logging.StreamHandler(other)
    null = logging.NullHandler()
    library.addHandler(stray)
    library.addHandler(null)
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    assert stray not in library.handlers
    assert null in library.handlers
    logging.getLogger("huggingface_hub.utils._http").warning("hint once")
    assert stream.getvalue().count("hint once") == 1
    assert other.getvalue() == ""
    adopt_library_loggers()
    assert library.handlers.count(null) == 1


def test_earmark_never_reads_hf_token() -> None:
    forbidden = (
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "use_auth_token",
        "huggingface_hub.login",
    )
    root = Path(earmark.__file__).parent
    for path in root.rglob("*.py"):
        text = path.read_text()
        for word in forbidden:
            assert word not in text, f"{path} mentions {word}"
        assert not re.search(r"\bget_token\b", text), f"{path} mentions get_token"
    assert not [name for name in Settings.model_fields if "hf" in name.lower()]


def test_adopt_does_not_import_huggingface_hub() -> None:
    code = (
        "import sys, earmark.log as log\n"
        "log.configure_logging()\n"
        "log.adopt_library_loggers()\n"
        "print('huggingface_hub' in sys.modules)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "False"
