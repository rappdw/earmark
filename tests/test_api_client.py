"""ApiClient against the real app in-process, and against stubs."""

import inspect
import json
import ssl
from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest
from pydantic import SecretStr

from earmark import api_client
from earmark.api.app import create_app
from earmark.api_client import (
    BAD_TOKEN,
    CANNOT_READ_FILE,
    CERTIFICATE,
    NO_TOKEN,
    REDIRECT,
    TIMEOUT,
    TOO_LARGE,
    UNREACHABLE,
    UNREADABLE,
    ApiClient,
    ApiError,
    ClientSetupError,
    default_transport,
)
from earmark.config import Settings
from earmark.models import FORM_FIELDS, TranscriptionRequest, UploadFields
from tests.api_client_helpers import (
    API_URL,
    CLIENT_TOKEN,
    RecordingTransport,
    api_settings,
    in_process_transport,
)
from tests.factories import TO

SHARE = "https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000802907981&uo=4"
HOST = "earmark.example.com"


def stub(handler: Callable[[httpx2.Request], httpx2.Response]) -> ApiClient:
    return ApiClient(API_URL, SecretStr(CLIENT_TOKEN), transport=httpx2.MockTransport(handler))


def recording(tmp_path: Path) -> tuple[ApiClient, RecordingTransport]:
    app = create_app(api_settings(tmp_path / "data"))
    transport = RecordingTransport(in_process_transport(app))
    return ApiClient(API_URL, SecretStr(CLIENT_TOKEN), transport=transport), transport


def test_rejects_plain_http_to_non_loopback() -> None:
    with pytest.raises(ClientSetupError) as caught:
        ApiClient("http://192.168.1.10:8787", SecretStr("t"))
    assert str(caught.value).startswith("EARMARK_API_URL: plain http is allowed only")
    assert "192.168.1.10" not in str(caught.value)


@pytest.mark.parametrize(
    ("token", "message"),
    [("", NO_TOKEN), ("  ", NO_TOKEN), ("tok en", BAD_TOKEN), ("tök", BAD_TOKEN)],
)
def test_token_rules(token: str, message: str) -> None:
    with pytest.raises(ClientSetupError) as caught:
        ApiClient(API_URL, SecretStr(token))
    assert str(caught.value) == message


def test_from_settings_needs_url_and_token(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="EARMARK_API_URL"):
        ApiClient.from_settings(api_settings(tmp_path))
    with pytest.raises(ClientSetupError) as caught:
        ApiClient.from_settings(Settings(_env_file=None, api_url=API_URL, data_dir=tmp_path))
    assert str(caught.value) == NO_TOKEN


def test_sends_bearer_header_only(tmp_path: Path) -> None:
    client, transport = recording(tmp_path)
    with client:
        response = client.submit_url(TranscriptionRequest(url=SHARE, to=TO))
        client.get_job(response.job_id)
        client.list_jobs(limit=5, status=None)
        with pytest.raises(ApiError):
            client.transcript(response.job_id, "note")  # 409: not ready
    assert len(transport.requests) == 4
    for _method, url, headers, _body in transport.requests:
        assert headers["authorization"] == f"Bearer {CLIENT_TOKEN}"
        assert CLIENT_TOKEN not in url
        assert "cookie" not in headers


def test_submit_url_body(tmp_path: Path) -> None:
    client, transport = recording(tmp_path)
    with client:
        client.submit_url(TranscriptionRequest(url=SHARE, to=TO, instructions="q"))
        client.submit_url(TranscriptionRequest(url=SHARE + "&z=1", to=TO, title="T"))
    first, second = (json.loads(r[3]) for r in transport.requests)
    assert set(first) == {"url", "to", "instructions"}
    assert set(second) == {"url", "to", "instructions", "title"}


def test_submit_file_form(tmp_path: Path) -> None:
    audio = tmp_path / "My Clip.m4a"
    audio.write_bytes(b"ID3" + bytes(5000))
    client, transport = recording(tmp_path)
    with client:
        answer = client.submit_file(audio, UploadFields(to=TO, instructions="q", title="T"))
    [(_, _, headers, body)] = transport.requests
    assert headers["content-type"].startswith("multipart/form-data")
    import re

    names = set(re.findall(rb'name="([a-z]+)"', body))
    assert names <= {b"file", *(f.encode() for f in FORM_FIELDS)}
    assert b"file" in names
    assert b'filename="My Clip.m4a"' in body
    assert len(answer.job_id) == 32


def test_redirect_not_followed() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(302, headers={"Location": "http://evil.example.net/"})

    with pytest.raises(ApiError) as caught, stub(handler) as client:
        client.list_jobs(limit=1, status=None)
    assert caught.value.message == REDIRECT.format(host=HOST, status=302)
    assert caught.value.status == 302
    assert len(seen) == 1


def test_error_detail_shown_cleaned_and_capped() -> None:
    detail = "\x1b[2J" + "bad\nline " + "x" * 2000

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500, json={"detail": detail})

    with pytest.raises(ApiError) as caught, stub(handler) as client:
        client.list_jobs(limit=1, status=None)
    message = caught.value.message
    assert len(message) == 500
    assert message.endswith("…")
    assert message.startswith("?[2Jbad?line ")
    assert all(c.isprintable() for c in message)
    assert caught.value.status == 500


def test_error_without_detail() -> None:
    with (
        pytest.raises(ApiError) as caught,
        stub(lambda r: httpx2.Response(502, html="<html>bad gateway</html>")) as client,
    ):
        client.list_jobs(limit=1, status=None)
    assert caught.value.message == f"the API at {HOST} answered HTTP 502"


def test_unauthorized_adds_the_hint() -> None:
    with (
        pytest.raises(ApiError) as caught,
        stub(lambda r: httpx2.Response(401, json={"detail": "nope"})) as client,
    ):
        client.list_jobs(limit=1, status=None)
    assert caught.value.message == "nope (check EARMARK_API_TOKEN)"
    with pytest.raises(ApiError) as caught, stub(lambda r: httpx2.Response(401)) as client:
        client.list_jobs(limit=1, status=None)
    assert caught.value.message == "missing or invalid API token (check EARMARK_API_TOKEN)"


def test_unreadable_answer() -> None:
    with (
        pytest.raises(ApiError) as caught,
        stub(lambda r: httpx2.Response(200, json={"hello": 1})) as client,
    ):
        client.list_jobs(limit=1, status=None)
    assert caught.value.message == UNREADABLE.format(host=HOST)
    assert caught.value.status == 200


def test_answer_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_client, "MAX_RESPONSE_BYTES", 1000)
    with (
        pytest.raises(ApiError) as caught,
        stub(lambda r: httpx2.Response(200, content=b"x" * 2000)) as client,
    ):
        client.transcript("a" * 32, "note")
    assert caught.value.message == TOO_LARGE.format(limit="1000 bytes")
    assert caught.value.status is None


def _raise(exc: Exception) -> Callable[[httpx2.Request], httpx2.Response]:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise exc

    return handler


def _cert_error(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("boom") from ssl.SSLCertVerificationError(
        1, "certificate verify failed"
    )


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (_raise(httpx2.ReadTimeout("slow")), TIMEOUT.format(host=HOST, seconds=60)),
        (
            _raise(httpx2.ConnectError("refused")),
            UNREACHABLE.format(host=HOST, error="ConnectError"),
        ),
        (_cert_error, CERTIFICATE.format(host=HOST)),
    ],
    ids=["timeout", "unreachable", "certificate"],
)
def test_transport_error_messages(
    handler: Callable[[httpx2.Request], httpx2.Response], expected: str
) -> None:
    with pytest.raises(ApiError) as caught, stub(handler) as client:
        client.list_jobs(limit=1, status=None)
    assert caught.value.message == expected
    assert caught.value.status is None


def test_unreadable_file(tmp_path: Path) -> None:
    folder = tmp_path / "dir.m4a"
    folder.mkdir()
    with (
        pytest.raises(ApiError) as caught,
        stub(lambda r: pytest.fail("no request expected")) as client,
    ):
        client.submit_file(folder, UploadFields(to=TO))
    assert caught.value.message == CANNOT_READ_FILE.format(name="dir.m4a", reason="Is a directory")
    assert caught.value.status is None


@pytest.mark.parametrize("bad", ["../healthz", "A" * 32, "a" * 31, "a/b", ""])
def test_malformed_job_id_makes_no_request(bad: str) -> None:
    with stub(lambda r: pytest.fail("no request expected")) as client:
        for call in (client.get_job, lambda j: client.transcript(j, "note")):
            with pytest.raises(ApiError) as caught:
                call(bad)
            assert (caught.value.message, caught.value.status) == ("job not found", 404)


def test_default_transport_verifies() -> None:
    transport = default_transport()
    context = transport._pool._ssl_context  # type: ignore[attr-defined]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_no_verification_switch() -> None:
    for name in Settings.model_fields:
        assert not any(bad in name for bad in ("verify", "insecure", "cert", "ssl")), name
    assert "verify" not in inspect.signature(ApiClient).parameters
    assert "verify" not in inspect.signature(default_transport).parameters
