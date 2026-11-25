"""Notifications through ntfy: one JSON publish to the server root per finished job.

The ntfy server comes only from operator configuration (it may be public ntfy.sh or a
self-hosted server on a private address), so this client has its own small httpx2 client
and no private-address guard, like the remote ASR client. It never follows
redirects, ignores proxy environment variables, keeps no cookies, caps the response, and
sends the token only as a per-request bearer header. Everything that varies (title,
message, tags, priority, link) travels in the JSON body; no untrusted text reaches a
header, the URL, or a query string (docs/steps/15-notify-doctor.md 2.1, 5.2).

Import this module only as ``earmark.notify.ntfy`` (absolute), and only from the factory's
builder and from doctor: it loads httpx2.
"""

import json
import logging
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final, Self
from urllib.parse import urlsplit

import httpx2
from pydantic import SecretStr

from earmark.config import NTFY_TOPIC_PATTERN, Settings
from earmark.endpoint import error_detail, host_label
from earmark.errors import PermanentError, TransientError
from earmark.fetch import USER_AGENT, parse_retry_after
from earmark.log import redact, scrub_urls
from earmark.models import Episode, Job, JobStatus, format_hms, utc_now
from earmark.recipients import mask_address
from earmark.sanitize import clean_unicode, fit_bytes
from earmark.sources import url_host

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S: Final = 5.0
TIMEOUT_S: Final = 10.0  # read, write, pool
MAX_RESPONSE_BYTES: Final = 64 * 1024
TITLE_MAX_BYTES: Final = 250
NAME_MAX_BYTES: Final = 200  # the episode-title part of a title
MESSAGE_MAX_BYTES: Final = 2000
LINE_MAX_BYTES: Final = 600  # any one message line (reason, warning)
PRIORITY_DEFAULT: Final = 3
PRIORITY_HIGH: Final = 4
TAGS: Final[Mapping[JobStatus, str]] = {
    "done": "white_check_mark",
    "failed": "x",
    "skipped": "fast_forward",
}
TITLE_PREFIX: Final[Mapping[JobStatus, str]] = {
    "done": "Sent",
    "failed": "Failed",
    "skipped": "Skipped",
}
UNKNOWN_EPISODE: Final = "podcast episode"
UNKNOWN_UPLOAD: Final = "uploaded audio"
UPLOAD_LINE: Final = "Uploaded audio"
UNKNOWN_SHOW: Final = "Apple Podcasts episode"
YOUTUBE_LINE: Final = "YouTube video"
_JOINERS: Final = frozenset({"‌", "‍"})
_DROPPED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Co", "Cn", "Zl", "Zp"})


class NtfyAuthError(PermanentError):
    """The server refused the token (HTTP 401 or 403)."""


def split_topic_url(url: str) -> tuple[str, str]:
    """(root URL ending in '/', topic) of a validated topic URL; ValueError otherwise."""
    parts = urlsplit(url)
    path = parts.path[:-1] if parts.path.endswith("/") else parts.path
    prefix, _, topic = path.rpartition("/")
    if not parts.scheme or not parts.netloc or not NTFY_TOPIC_PATTERN.fullmatch(topic):
        raise ValueError("not an ntfy topic URL")
    return f"{parts.scheme}://{parts.netloc}{prefix}/", topic


def clean_field(text: str, max_bytes: int) -> str:
    """One line of untrusted text, safe to show: see docs/steps/15-notify-doctor.md 5.2."""
    kept: list[str] = []
    for ch in clean_unicode(text):
        if ch.isspace():
            kept.append(" ")  # CR, LF, TAB, NEL, and the separators read as a space
        elif unicodedata.category(ch) in _DROPPED_CATEGORIES and ch not in _JOINERS:
            continue
        else:
            kept.append(ch)
    collapsed = " ".join("".join(kept).split())
    # ntfy RFC-2047-decodes titles even for JSON publishes, so no encoded-word may survive.
    return fit_bytes(collapsed.replace("=?", "= ?"), max_bytes)


@dataclass(frozen=True, slots=True)
class Notification:
    title: str
    message: str
    tags: tuple[str, ...]
    priority: int

    def payload(self, topic: str) -> dict[str, object]:
        """The JSON publish body: topic, title, message, tags and priority."""
        body: dict[str, object] = {
            "topic": topic,
            "title": self.title,
            "message": self.message,
            "tags": list(self.tags),
            "priority": self.priority,
        }
        return body


def _label(job: Job, episode: Episode | None, field: Callable[[str, int], str]) -> tuple[str, str]:
    """(first message line, the name that stands for the episode in the title)."""
    if job.source == "upload":
        stem = PurePosixPath(job.upload_name).stem if job.upload_name else ""
        for candidate in (job.title or "", episode.title if episode else "", stem):
            name = field(candidate, NAME_MAX_BYTES)
            if name:
                return UPLOAD_LINE, name
        return UPLOAD_LINE, UNKNOWN_UPLOAD
    if job.source == "url":
        host = field(url_host(job.url or ""), LINE_MAX_BYTES)
        name = field(job.title or "", NAME_MAX_BYTES)
        return f"Audio from {host}", name or f"audio from {host}"
    if job.source == "youtube":
        channel = field(episode.show_title, LINE_MAX_BYTES) if episode is not None else ""
        name = field(episode.title, NAME_MAX_BYTES) if episode is not None else ""
        return (
            f"YouTube: {channel}" if channel else YOUTUBE_LINE,
            name or YOUTUBE_LINE,
        )
    name = field(episode.title, NAME_MAX_BYTES) if episode is not None else ""
    show = field(episode.show_title, LINE_MAX_BYTES) if episode is not None else ""
    return show or UNKNOWN_SHOW, name or UNKNOWN_EPISODE


def compose(
    job: Job, episode: Episode | None, *, secrets: Sequence[str] = ()
) -> Notification | None:
    """The notification for a terminal job; None for queued or running.

    It never carries a URL path or query, instructions, a destination address or a file path.
    """
    status = job.status
    if status not in TAGS:
        return None

    def field(text: str, limit: int) -> str:
        return clean_field(redact(text, secrets), limit)

    first_line, name = _label(job, episode, field)
    title = fit_bytes(f"{TITLE_PREFIX[status]}: {name}", TITLE_MAX_BYTES)

    lines: list[str] = [first_line]
    if job.to is not None and status in ("done", "failed"):
        lines.append(f"To: {field(mask_address(job.to), LINE_MAX_BYTES)}")
    if status == "done":
        if job.result is not None:
            lines.append(f"Duration: {format_hms(job.result.duration_s)}")
        if job.resent:
            lines.append("Resent after an interruption.")
        if job.warnings:
            first = field(scrub_urls(job.warnings[0]), LINE_MAX_BYTES)
            count = len(job.warnings)
            lines.append(f"{count} warning{'' if count == 1 else 's'}: {first}")
    else:
        if status == "failed" and job.stage:
            lines.append(f"Stage: {field(job.stage, LINE_MAX_BYTES)}")
        reason = field(scrub_urls(job.error or ""), LINE_MAX_BYTES)
        lines.append(f"Reason: {reason or 'no reason recorded'}")
        if status == "failed" and job.attempts > 1:
            lines.append(f"Gave up after {job.attempts} attempts.")
        if status == "failed" and job.delivery_state == "sending":
            lines.append("The email may already have been delivered.")

    message = fit_bytes("\n".join(lines), MESSAGE_MAX_BYTES)
    return Notification(
        title=redact(title, secrets),
        message=redact(message, secrets),
        tags=(TAGS[status],),
        priority=PRIORITY_HIGH if status == "failed" else PRIORITY_DEFAULT,
    )


@dataclass(frozen=True, slots=True)
class NtfyOptions:
    root_url: str  # ends in "/"
    topic: str
    token: SecretStr | None
    secrets: tuple[str, ...]  # Settings.secret_values(), for redaction

    @property
    def host(self) -> str:
        return host_label(self.root_url)

    @classmethod
    def from_settings(cls, settings: Settings) -> Self | None:
        """None when EARMARK_NTFY_URL is unset."""
        if settings.ntfy_url is None:
            return None
        root, topic = split_topic_url(settings.ntfy_url.get_secret_value())
        return cls(
            root_url=root,
            topic=topic,
            token=settings.ntfy_token,
            secrets=tuple(settings.secret_values()),
        )


@dataclass(frozen=True, slots=True)
class NtfyHealth:
    status: int  # HTTP status of GET <root>v1/health
    healthy: bool | None  # the "healthy" field when the body is the expected JSON


@dataclass(frozen=True, slots=True)
class NtfyAccount:
    username: str | None
    messages_remaining: int | None


@dataclass(frozen=True, slots=True)
class _Reply:
    status: int
    headers: httpx2.Headers
    body: bytes


class NtfyClient:
    """Not thread-safe. The httpx2 client is created on first use."""

    def __init__(
        self,
        options: NtfyOptions,
        *,
        transport: httpx2.BaseTransport | None = None,
        timeout_s: float = TIMEOUT_S,
    ) -> None:
        self._options = options
        self._transport = transport
        self._timeout_s = timeout_s
        self._client: httpx2.Client | None = None
        secrets = list(options.secrets)
        if options.token is not None:
            secrets.append(options.token.get_secret_value())
        secrets.extend((options.topic, options.root_url + options.topic))
        self._secrets = tuple(s for s in secrets if s)

    @property
    def options(self) -> NtfyOptions:
        return self._options

    def _get_client(self) -> httpx2.Client:
        if self._client is None:
            self._client = httpx2.Client(
                transport=self._transport or httpx2.HTTPTransport(),
                trust_env=False,
                follow_redirects=False,
                timeout=httpx2.Timeout(
                    self._timeout_s, connect=min(CONNECT_TIMEOUT_S, self._timeout_s)
                ),
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        return self._client

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            client.close()

    def _request(
        self, method: str, url: str, *, with_token: bool, body: bytes | None = None
    ) -> _Reply:
        host = self._options.host
        headers: dict[str, str] = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        token = self._options.token
        if with_token and token is not None:
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        client = self._get_client()
        client.cookies.clear()
        try:
            with client.stream(method, url, content=body, headers=headers) as response:
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= MAX_RESPONSE_BYTES:
                        break
                data = b"".join(chunks)[:MAX_RESPONSE_BYTES]
                return _Reply(response.status_code, response.headers, data)
        except httpx2.TimeoutException as exc:
            raise TransientError(
                f"ntfy at {host} did not answer within {self._timeout_s:g} s"
            ) from exc
        except (httpx2.TransportError, httpx2.DecodingError) as exc:
            raise TransientError(f"could not reach ntfy at {host}: {type(exc).__name__}") from exc
        except httpx2.InvalidURL as exc:
            raise PermanentError("invalid EARMARK_NTFY_URL") from exc

    def _detail(self, reply: _Reply) -> str:
        detail = error_detail(reply.body, reply.headers.get("Content-Type"), self._secrets)
        return "" if detail is None else f": {detail}"

    def _redirect_error(self, status: int) -> PermanentError:
        return PermanentError(
            f"ntfy at {self._options.host} answered with a redirect (HTTP {status}); "
            "redirects are not followed, check EARMARK_NTFY_URL"
        )

    def _status_error(self, reply: _Reply, verb: str) -> PermanentError | TransientError:
        status = reply.status
        host = self._options.host
        if 300 <= status < 400:
            return self._redirect_error(status)
        detail = self._detail(reply)
        if status in (401, 403):
            return NtfyAuthError(
                f"ntfy at {host} refused the {verb}: HTTP {status} "
                f"(check EARMARK_NTFY_TOKEN){detail}"
            )
        if status == 404:
            return PermanentError(f"ntfy at {host}: HTTP 404 (check EARMARK_NTFY_URL){detail}")
        if status in (408, 429) or status >= 500:
            return TransientError(
                f"ntfy at {host} failed: HTTP {status}{detail}",
                retry_after_s=parse_retry_after(reply.headers.get("Retry-After"), utc_now()),
            )
        return PermanentError(f"ntfy at {host} rejected the {verb}: HTTP {status}{detail}")

    def publish(self, notification: Notification) -> None:
        payload = notification.payload(self._options.topic)
        body = json.dumps(payload, ensure_ascii=False).encode()
        reply = self._request("POST", self._options.root_url, with_token=True, body=body)
        if reply.status != 200:
            raise self._status_error(reply, "notification")

    def health(self) -> NtfyHealth:
        reply = self._request("GET", self._options.root_url + "v1/health", with_token=False)
        if 300 <= reply.status < 400:
            raise self._redirect_error(reply.status)
        healthy: bool | None = None
        if reply.status == 200:
            try:
                data = json.loads(reply.body)
            except ValueError:
                data = None
            flag = data.get("healthy") if isinstance(data, dict) else None
            healthy = flag if isinstance(flag, bool) else None
        return NtfyHealth(reply.status, healthy)

    def account(self) -> NtfyAccount:
        reply = self._request("GET", self._options.root_url + "v1/account", with_token=True)
        if reply.status != 200:
            raise self._status_error(reply, "request")
        try:
            data = json.loads(reply.body)
        except ValueError:
            data = None
        username: str | None = None
        remaining: int | None = None
        if isinstance(data, dict):
            name = data.get("username")
            username = name if isinstance(name, str) else None
            stats = data.get("stats")
            left = stats.get("messages_remaining") if isinstance(stats, dict) else None
            if isinstance(left, int) and not isinstance(left, bool):
                remaining = left
        return NtfyAccount(username, remaining)


class NtfyNotifier:
    """The `ntfy` Notifier: one publish per terminal job, no retry."""

    def __init__(self, client: NtfyClient) -> None:
        self._client = client

    def notify(self, job: Job, episode: Episode | None, /) -> None:
        options = self._client.options
        notification = compose(job, episode, secrets=options.secrets)
        if notification is None:
            logger.debug("job %s is %s; not notifying", job.id, job.status)
            return
        self._client.publish(notification)
        logger.info("notified job %s via ntfy at %s", job.id, options.host)

    def close(self) -> None:
        self._client.close()


def build_ntfy_notifier(
    settings: Settings, *, transport: httpx2.BaseTransport | None = None
) -> NtfyNotifier | None:
    """None (and one INFO log) without EARMARK_NTFY_URL. Makes no request."""
    options = NtfyOptions.from_settings(settings)
    if options is None:
        logger.info("notifications are off: EARMARK_NTFY_URL is not set")
        return None
    logger.info("ntfy notifications to %s", options.host)
    return NtfyNotifier(NtfyClient(options, transport=transport))
