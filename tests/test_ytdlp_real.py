"""The real yt-dlp library, offline. Opt-in: ``pytest -m slow`` with the ``youtube`` extra.

Fails (does not skip) without the extra, like the transcribe ``slow`` tests. Sockets are blocked
here: nothing in this module reaches YouTube.
"""

import importlib
import io
import subprocess
import sysconfig
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_socket

from earmark.errors import PermanentError, TransientError
from earmark.youtube.ytdlp import (
    REFUSALS,
    MissingYoutubeExtraError,
    YtDlpLog,
    YtDlpRuntime,
    classify_error,
    load_runtime,
    ytdlp_params,
)

pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def _no_sockets() -> Iterator[None]:
    pytest_socket.disable_socket(allow_unix_socket=True)
    yield
    pytest_socket.enable_socket()


@pytest.fixture(scope="module")
def runtime() -> YtDlpRuntime:
    try:
        return load_runtime()
    except MissingYoutubeExtraError as exc:
        pytest.fail(f"{exc} (the slow tests need the youtube extra)")


def test_real_runtime_loads(runtime: YtDlpRuntime) -> None:
    assert runtime.version != "unknown"
    assert runtime.ejs_version is not None
    assert runtime.deno_version is not None
    assert runtime.deno_path.is_file()
    assert runtime.deno_path.parent == Path(sysconfig.get_path("scripts"))


def test_real_deno_runs(runtime: YtDlpRuntime) -> None:
    result = subprocess.run(
        [str(runtime.deno_path), "--version"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("deno ")
    assert runtime.deno_version is not None
    assert runtime.deno_version in result.stdout


def test_real_plugins_disabled(runtime: YtDlpRuntime) -> None:
    yt_globals = importlib.import_module("yt_dlp.globals")
    assert yt_globals.plugin_dirs.value == []


def test_real_params_accepted(runtime: YtDlpRuntime) -> None:
    session: Any = runtime.open_session(
        ytdlp_params(
            deno_path=runtime.deno_path,
            logger=YtDlpLog(),
            progress_hook=None,
            outtmpl="/tmp/x/%%(id)s",  # never written: the session only constructs
        )
    )
    try:
        assert list(session._ies) == ["Youtube"]  # the loaded extractors
        assert session.proxies == {"all": "__noproxy__"}
        assert session.params["js_runtimes"] == {"deno": {"path": str(runtime.deno_path)}}
    finally:
        session.close()


def test_real_errors_classify(runtime: YtDlpRuntime) -> None:
    # yt-dlp is untyped and optional, so it is imported by name (as earmark itself does).
    networking = importlib.import_module("yt_dlp.networking")
    net = importlib.import_module("yt_dlp.networking.exceptions")
    utils = importlib.import_module("yt_dlp.utils")

    errors = runtime.errors

    def http(status: int) -> Exception:
        response = networking.Response(
            io.BytesIO(b""), "https://example.invalid/", {}, status=status
        )
        error: Exception = net.HTTPError(response)
        return error

    def wrapped(exc: BaseException) -> Exception:
        error: Exception = utils.DownloadError("ERROR: x", exc_info=(type(exc), exc, None))
        return error

    for status, retry in ((503, True), (429, True), (408, True), (404, False)):
        mapped = classify_error(wrapped(http(status)), errors, [])
        assert isinstance(mapped, TransientError if retry else PermanentError), status
    forbidden = classify_error(wrapped(http(403)), errors, [])
    assert isinstance(forbidden, PermanentError)
    assert forbidden.message == REFUSALS["http_403"]

    mapped = classify_error(wrapped(net.TransportError("boom")), errors, [])
    assert isinstance(mapped, TransientError)
    mapped = classify_error(wrapped(utils.ContentTooShortError(1, 2)), errors, [])
    assert isinstance(mapped, TransientError)

    mapped = classify_error(wrapped(utils.GeoRestrictedError("x", countries=["US"])), errors, [])
    assert isinstance(mapped, PermanentError)
    assert mapped.message == REFUSALS["geo"]

    expected = utils.ExtractorError("Private video. Sign in", expected=True)
    mapped = classify_error(wrapped(expected), errors, [])
    assert isinstance(mapped, PermanentError)
    assert mapped.message == REFUSALS["private"]

    unexpected = utils.ExtractorError("Unable to extract something")
    mapped = classify_error(wrapped(unexpected), errors, [])
    assert isinstance(mapped, PermanentError)
    assert mapped.message == REFUSALS["out_of_date"]

    assert classify_error(wrapped(OSError(28, "full")), errors, []) is None
    bare = utils.DownloadError("ERROR: Private video")
    mapped = classify_error(bare, errors, [])
    assert isinstance(mapped, PermanentError)
    assert mapped.message == REFUSALS["private"]
