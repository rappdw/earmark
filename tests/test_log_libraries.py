"""Real huggingface_hub and httpx/httpcore logging (``-m slow``, needs the transcribe extra).

Every request is answered in-process (a mock transport or a unix-socket server), so nothing
leaves the machine. The point: with earmark's handler installed, no library prints a URL
path or query, and nothing but earmark's handler prints.
"""

import importlib
import io
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from earmark.log import QUIET_LOGGERS, adopt_library_loggers, configure_logging
from tests.uds_http import UdsHttpServer, UdsReply

pytestmark = [
    pytest.mark.slow,
    pytest.mark.usefixtures("restore_root_logging", "restore_library_loggers"),
]

SHA = "a" * 40
HINT = (
    "unauth; Warning: You are sending unauthenticated requests to the HF Hub. "
    "Please set a HF_TOKEN to enable higher rate limits and faster downloads."
)
HINT_TEXT = "You are sending unauthenticated requests to the HF Hub"


def _hub_handler(requests: list[tuple[str, str]], httpx: Any) -> Callable[[Any], Any]:
    files = {"config.json": b"{}", "other.json": b"{}"}

    def handler(request: Any) -> Any:
        path = request.url.path
        requests.append((request.method, request.url.raw_path.decode()))
        headers = {"X-HF-Warning": HINT}
        for name in files:
            if path == f"/org/repo/resolve/{SHA}/{name}":
                location = (
                    f"/api/resolve-cache/models/org/repo/{SHA}/{name}"
                    f"?%2Forg%2Frepo%2Fresolve%2F{SHA}%2F{name}=&etag=%22abc%22"
                )
                return httpx.Response(307, headers={**headers, "Location": location})
            if path == f"/api/resolve-cache/models/org/repo/{SHA}/{name}":
                return httpx.Response(
                    200,
                    headers={
                        **headers,
                        "X-Repo-Commit": SHA,
                        "ETag": '"abc"',
                        "Content-Length": "2",
                    },
                    content=files[name] if request.method == "GET" else b"",
                )
        return httpx.Response(404, headers=headers)

    return handler


@pytest.fixture
def hub_downloads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Callable[[str], Path]]:
    httpx = importlib.import_module("httpx")
    hub = importlib.import_module("huggingface_hub")
    http = importlib.import_module("huggingface_hub.utils._http")
    constants = importlib.import_module("huggingface_hub.constants")
    monkeypatch.setattr(constants, "HF_HUB_OFFLINE", False)
    monkeypatch.setattr(http, "_WARNED_TOPICS", set())
    seen: list[tuple[str, str]] = []
    hook = http.hf_request_event_hook
    transport = httpx.MockTransport(_hub_handler(seen, httpx))
    hub.set_client_factory(
        lambda: httpx.Client(
            transport=transport, event_hooks={"request": [hook]}, follow_redirects=True
        )
    )

    def download(name: str) -> Path:
        found = hub.hf_hub_download("org/repo", name, revision=SHA, cache_dir=tmp_path / "hf")
        return Path(found)

    download.requests = seen  # type: ignore[attr-defined]  # exposed for the assertions
    try:
        yield download
    finally:
        hub.set_client_factory(http.default_client_factory)
        hub.close_session()


def test_hf_download_logs_no_urls(
    hub_downloads: Callable[[str], Path],
) -> None:
    stream = io.StringIO()
    adopt_library_loggers()  # imports huggingface_hub.utils.logging and strips its handler
    configure_logging("DEBUG", stream=stream)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
    assert hub_downloads("config.json").read_bytes() == b"{}"
    seen = hub_downloads.requests  # type: ignore[attr-defined]
    assert any("/resolve/" in target for _, target in seen)
    assert any(m == "HEAD" and "resolve-cache" in t for m, t in seen)
    assert any(m == "GET" and "resolve-cache" in t for m, t in seen)
    out = stream.getvalue()
    for leaked in ("resolve", "etag", "%2F", "HTTP Request:"):
        assert leaked not in out


def test_hf_hint_logged_once(hub_downloads: Callable[[str], Path]) -> None:
    stream = io.StringIO()
    adopt_library_loggers()
    configure_logging("INFO", stream=stream)
    hub_downloads("config.json")
    hub_downloads("other.json")
    assert stream.getvalue().count(HINT_TEXT) == 1
    handlers = logging.getLogger("huggingface_hub").handlers
    assert all(isinstance(h, logging.NullHandler) for h in handlers)


def _redirect_reply(method: str, target: str) -> UdsReply:
    if target.startswith("/start"):
        return UdsReply(307, (("Location", "/next?sid=s2"),))
    return UdsReply(200, body=b"ok")


def test_httpx_httpcore_over_uds_log_no_paths() -> None:
    httpx = importlib.import_module("httpx")
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    with (
        UdsHttpServer(_redirect_reply) as server,
        httpx.Client(
            transport=httpx.HTTPTransport(uds=str(server.socket_path)),
            base_url="http://models.example",
            follow_redirects=True,
        ) as client,
    ):
        assert client.get("/start?sid=s1").status_code == 200
    assert [t for _, t in server.requests] == ["/start?sid=s1", "/next?sid=s2"]
    out = stream.getvalue()
    for leaked in ("sid=", "/start", "/next"):
        assert leaked not in out
