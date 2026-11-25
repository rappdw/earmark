"""The CLI's client mode: jobs, submit and transcript over the HTTP API.

The API is an operator endpoint (EARMARK_API_URL): no address guard, no redirects, no proxy
variables, no cookies kept, answers capped, the token only in a per-request Authorization
header, certificates always verified. It logs nothing. Import it only from the CLI's
commands and from doctor: it loads httpx2.
"""

import json
import re
import ssl
from pathlib import Path
from typing import Any, Final, Self

import httpx2
from pydantic import BaseModel, SecretStr, ValidationError

from earmark.api.schemas import JobList, JobView, TranscriptionResponse
from earmark.config import Settings, check_api_url
from earmark.endpoint import host_label
from earmark.fetch import MIB, USER_AGENT, limit_text
from earmark.log import redact
from earmark.models import JobStatus, TranscriptFormat, TranscriptionRequest, UploadFields
from earmark.transcripts import JOB_NOT_FOUND

API_TIMEOUT: Final = httpx2.Timeout(60.0, connect=10.0)
MAX_RESPONSE_BYTES: Final = 64 * MIB
MAX_DETAIL_CHARS: Final = 500
NO_TOKEN: Final = "EARMARK_API_URL is set, so this command calls the API, and that needs EARMARK_API_TOKEN (the API's token)"  # noqa: E501, S105  # a message, not a secret
BAD_TOKEN: Final = (
    "EARMARK_API_TOKEN must be printable ASCII without spaces, because it is sent in an HTTP header"  # noqa: S105  # a message, not a secret
)
UNAUTHORIZED_HINT: Final = " (check EARMARK_API_TOKEN)"
REDIRECT: Final = (
    "the API at {host} answered with a redirect (HTTP {status}); redirects are not "
    "followed, check EARMARK_API_URL"
)
HTTP_STATUS: Final = "the API at {host} answered HTTP {status}"
TIMEOUT: Final = "the API at {host} did not answer within {seconds:g} s"
UNREACHABLE: Final = (
    "could not reach the API at {host} ({error}); check EARMARK_API_URL and that the "
    "service is running"
)
CERTIFICATE: Final = (
    "the certificate of {host} did not verify; earmark always checks it, so the server "
    "needs a certificate this machine trusts, with its intermediates"
)
INVALID_URL: Final = "invalid EARMARK_API_URL"
TOO_LARGE: Final = "the API's answer is larger than {limit}"
UNREADABLE: Final = (
    "the API at {host} answered with a body earmark cannot read; is it the same version of earmark?"
)
CANNOT_READ_FILE: Final = "cannot read {name}: {reason}"
_JOB_ID: Final = re.compile(r"[0-9a-f]{32}")
_CHAIN_LIMIT: Final = 10


class ClientSetupError(Exception):
    """Client mode cannot start. str(exc) names settings, never their values."""


class ApiError(Exception):
    """A call failed. ``message`` is safe to show: cleaned, redacted of the token, capped."""

    def __init__(self, message: str, *, status: int | None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def default_transport() -> httpx2.BaseTransport:
    """httpx2.HTTPTransport() with its default, verifying TLS context. A seam for tests."""
    return httpx2.HTTPTransport()


def _has_certificate_error(exc: BaseException) -> bool:
    seen: BaseException | None = exc
    for _ in range(_CHAIN_LIMIT):
        if seen is None:
            return False
        if isinstance(seen, ssl.SSLCertVerificationError):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def transport_error_message(exc: httpx2.HTTPError, host: str, timeout_s: float) -> str:
    """TIMEOUT, CERTIFICATE (an ssl.SSLCertVerificationError anywhere in the
    __cause__/__context__ chain), INVALID_URL, or UNREACHABLE with type(exc).__name__.
    Never str(exc). Shared with doctor."""
    if isinstance(exc, httpx2.TimeoutException):
        return TIMEOUT.format(host=host, seconds=timeout_s)
    if _has_certificate_error(exc):
        return CERTIFICATE.format(host=host)
    if isinstance(exc, httpx2.InvalidURL):
        return INVALID_URL
    return UNREACHABLE.format(host=host, error=type(exc).__name__)


def check_token(token: SecretStr | None) -> str | None:
    """The reason a token cannot be used, or None when it can."""
    if token is None:
        return NO_TOKEN
    value = token.get_secret_value().strip()
    if not value:
        return NO_TOKEN
    if not all("!" <= c <= "~" for c in value):
        return BAD_TOKEN
    return None


def _printable(text: str) -> str:
    return "".join(c if c.isprintable() else "?" for c in text)


class ApiClient:
    def __init__(
        self,
        base_url: str,
        token: SecretStr,
        *,
        transport: httpx2.BaseTransport | None = None,
        timeout: httpx2.Timeout = API_TIMEOUT,
    ) -> None:
        try:
            checked = check_api_url(base_url)
        except ValueError as exc:
            raise ClientSetupError(f"EARMARK_API_URL: {exc}") from None
        problem = check_token(token)
        if problem is not None:
            raise ClientSetupError(problem)
        self._token = token.get_secret_value().strip()
        self._host = host_label(checked)
        self._timeout_s = timeout.read or 60.0
        self._client = httpx2.Client(
            base_url=checked,
            transport=transport or default_transport(),
            trust_env=False,
            follow_redirects=False,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        if settings.api_url is None:
            raise ValueError("EARMARK_API_URL is not set")
        if settings.api_token is None:
            raise ClientSetupError(NO_TOKEN)
        return cls(settings.api_url, settings.api_token)

    @property
    def host(self) -> str:
        return self._host

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- the calls ------------------------------------------------------------------------

    def list_jobs(self, *, limit: int, status: JobStatus | None) -> tuple[JobView, ...]:
        params: dict[str, Any] = {"limit": limit}
        if status is not None:
            params["status"] = status
        body = self._call("GET", "/jobs", expected=200, params=params)
        return self._parse(JobList, body, 200).jobs

    def get_job(self, job_id: str) -> JobView:
        self._check_job_id(job_id)
        body = self._call("GET", f"/jobs/{job_id}", expected=200)
        return self._parse(JobView, body, 200)

    def submit_url(self, request: TranscriptionRequest) -> TranscriptionResponse:
        body = self._call(
            "POST",
            "/transcriptions",
            expected=202,
            json=request.model_dump(mode="json", exclude_none=True),
        )
        return self._parse(TranscriptionResponse, body, 202)

    def submit_file(self, path: Path, fields: UploadFields) -> TranscriptionResponse:
        try:
            with path.open("rb") as handle:
                body = self._call(
                    "POST",
                    "/transcriptions",
                    expected=202,
                    data=fields.model_dump(mode="json", exclude_none=True),
                    files={"file": (path.name, handle)},
                )
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
            raise ApiError(
                CANNOT_READ_FILE.format(name=_printable(path.name), reason=_printable(reason)),
                status=None,
            ) from None
        return self._parse(TranscriptionResponse, body, 202)

    def transcript(self, job_id: str, fmt: TranscriptFormat) -> bytes:
        self._check_job_id(job_id)
        return self._call("GET", f"/jobs/{job_id}/transcript", expected=200, params={"format": fmt})

    # -- internals ------------------------------------------------------------------------

    @staticmethod
    def _check_job_id(job_id: str) -> None:
        if not _JOB_ID.fullmatch(job_id):
            raise ApiError(JOB_NOT_FOUND, status=404)

    def _parse[M: BaseModel](self, model: type[M], body: bytes, status: int) -> M:
        try:
            return model.model_validate_json(body)
        except (ValidationError, ValueError):
            raise ApiError(UNREADABLE.format(host=self._host), status=status) from None

    def _call(
        self,
        method: str,
        path: str,
        *,
        expected: int,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: dict[str, Any] | None = None,
        files: Any = None,
    ) -> bytes:
        self._client.cookies.clear()
        try:
            with self._client.stream(
                method,
                path,
                params=params,
                json=json,
                data=data,
                files=files,
                headers={"Authorization": f"Bearer {self._token}"},
            ) as response:
                status = response.status_code
                content_type = response.headers.get("Content-Type", "")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        raise ApiError(
                            TOO_LARGE.format(limit=limit_text(MAX_RESPONSE_BYTES)), status=None
                        )
                    chunks.append(chunk)
        except httpx2.HTTPError as exc:
            raise ApiError(
                transport_error_message(exc, self._host, self._timeout_s), status=None
            ) from None
        body = b"".join(chunks)
        if status == expected:
            return body
        if 300 <= status < 400:
            raise ApiError(REDIRECT.format(host=self._host, status=status), status=status)
        detail = self._detail(body, content_type)
        if status == 401:
            raise ApiError(
                (detail or "missing or invalid API token") + UNAUTHORIZED_HINT, status=401
            )
        raise ApiError(detail or HTTP_STATUS.format(host=self._host, status=status), status=status)

    def _detail(self, body: bytes, content_type: str) -> str | None:
        text = body.decode("utf-8", errors="replace")
        if "json" not in content_type.lower() and not text.lstrip().startswith("{"):
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
        detail = data.get("detail") if isinstance(data, dict) else None
        if not isinstance(detail, str):
            return None
        cleaned = redact(_printable(detail), [self._token])
        if len(cleaned) > MAX_DETAIL_CHARS:
            cleaned = cleaned[: MAX_DETAIL_CHARS - 1] + "…"
        return cleaned or None
