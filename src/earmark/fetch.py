"""The one way earmark talks to third-party URLs: guarded GET and streaming download.

Feeds, enclosure URLs, and everything behind them are untrusted. Every connection goes
through ``GuardedNetworkBackend``, which resolves the host itself, refuses the connection
if any address is not public, and connects to the exact address it checked. So the check
holds on every redirect hop and a DNS answer cannot change between check and connect.

Messages name the host and status, never the URL path or query (they can carry tokens).
"""

import contextlib
import email.utils
import ipaddress
import logging
import re
import socket
import time
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Self

import httpcore2
import httpx2

from earmark import __version__
from earmark.artifacts import atomic_writer
from earmark.config import Settings
from earmark.errors import PermanentError, TransientError
from earmark.models import Clock, utc_now

logger = logging.getLogger(__name__)

MIB: Final = 1024 * 1024
USER_AGENT: Final = f"earmark/{__version__}"
MAX_REDIRECTS: Final = 20
DEFAULT_TIMEOUT: Final = httpx2.Timeout(30.0, connect=10.0, pool=10.0)
DOCUMENT_DEADLINE_S: Final = 120.0
DOWNLOAD_DEADLINE_S: Final = 3600.0
ALLOWED_SCHEMES: Final = frozenset({"http", "https"})
RETRY_AFTER_CAP_S: Final = 86400.0

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
type Resolve = Callable[[str, int], Sequence[IPAddress]]
type Monotonic = Callable[[], float]

AUDIO_EXTENSIONS: Final = frozenset(
    {"mp3", "m4a", "m4b", "mp4", "m4v", "aac", "ogg", "oga", "opus", "wav", "flac"}
    | {"webm", "mov", "mka", "aiff"}
)
CONTENT_TYPE_EXTENSIONS: Final[Mapping[str, str]] = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mpeg3": "mp3",
    "audio/x-mpeg": "mp3",
    "audio/x-mp3": "mp3",
    "audio/mpg": "mp3",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/mp4a-latm": "m4a",
    "audio/x-m4b": "m4b",
    "audio/aac": "aac",
    "audio/aacp": "aac",
    "audio/x-aac": "aac",
    "audio/ogg": "ogg",
    "application/ogg": "ogg",
    "audio/opus": "opus",
    "audio/wav": "wav",
    "audio/wave": "wav",
    "audio/x-wav": "wav",
    "audio/vnd.wave": "wav",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/webm": "webm",
    "video/webm": "webm",
    "video/mp4": "mp4",
    "video/x-m4v": "m4v",
    "video/quicktime": "mov",
    "audio/x-matroska": "mka",
    "audio/aiff": "aiff",
    "audio/x-aiff": "aiff",
}
_TEXTUAL_TYPES: Final = frozenset(
    {
        "application/json",
        "application/xml",
        "application/xhtml+xml",
        "application/rss+xml",
        "application/atom+xml",
    }
)

_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_V4_COMPATIBLE = ipaddress.IPv6Network("::/96")
_V4_TRANSLATED = ipaddress.IPv6Network("::ffff:0:0:0/96")
_DIGITS = re.compile(r"[0-9]{1,10}")
_LENGTH = re.compile(r"[0-9]{1,19}")


# -- errors -------------------------------------------------------------------------------


def _shorten(host: str | None, limit: int = 100) -> str | None:
    if host is None:
        return None
    return host if len(host) <= limit else host[: limit - 1] + "…"


class PermanentFetchError(PermanentError):
    def __init__(self, message: str, *, host: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.host = _shorten(host)
        self.status = status


class TransientFetchError(TransientError):
    def __init__(
        self,
        message: str,
        *,
        host: str | None = None,
        status: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message, retry_after_s=retry_after_s)
        self.host = _shorten(host)
        self.status = status


class BlockedAddressError(PermanentFetchError):
    """The host is, or resolves to, an address the policy refuses."""


# -- address policy and resolver ------------------------------------------------------------


def is_public_address(address: IPAddress) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return is_public_address(address.ipv4_mapped)
        if address in _NAT64:
            return is_public_address(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
        if address in _V4_COMPATIBLE or address in _V4_TRANSLATED or address.is_site_local:
            return False
    return address.is_global and not address.is_multicast


def system_resolve(host: str, port: int) -> list[IPAddress]:
    """``getaddrinfo`` looked up at call time (so a socket-blocking test harness applies).

    Raises OSError (``socket.gaierror``) on failure.
    """
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    found: list[IPAddress] = []
    for family, _, _, _, sockaddr in infos:
        if family in (socket.AF_INET, socket.AF_INET6):
            address = ipaddress.ip_address(str(sockaddr[0]).partition("%")[0])
            if address not in found:
                found.append(address)
    return found


def _parse_literal(host: str) -> IPAddress | None:
    text = host.strip("[]").partition("%")[0]
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


class GuardedNetworkBackend(httpcore2.NetworkBackend):
    """Resolves, checks, and connects to the address it checked."""

    def __init__(
        self, resolve: Resolve = system_resolve, *, inner: httpcore2.NetworkBackend | None = None
    ) -> None:
        self._resolve = resolve
        self._inner: httpcore2.NetworkBackend = inner or httpcore2.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.NetworkStream:
        literal = _parse_literal(host)
        candidates: list[IPAddress]
        if literal is not None:
            candidates = [literal]
            if not is_public_address(literal):
                raise BlockedAddressError(
                    f"refusing to connect to {_shorten(host)}: not a public address; "
                    "see EARMARK_ALLOW_PRIVATE_FETCH",
                    host=host,
                )
        else:
            try:
                candidates = list(self._resolve(host, port))
            except OSError as exc:
                raise TransientFetchError(f"could not resolve {_shorten(host)}", host=host) from exc
            if not candidates:
                raise TransientFetchError(f"could not resolve {_shorten(host)}", host=host)
            for address in candidates:
                if not is_public_address(address):
                    raise BlockedAddressError(
                        f"refusing to connect to {_shorten(host)}: it resolves to a "
                        f"non-public address ({address}); see EARMARK_ALLOW_PRIVATE_FETCH",
                        host=host,
                    )
        options = list(socket_options) if socket_options is not None else None
        last: Exception = TransientFetchError(f"could not connect to {_shorten(host)}", host=host)
        for address in candidates:
            try:
                return self._inner.connect_tcp(
                    str(address),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=options,
                )
            except (httpcore2.ConnectError, httpcore2.ConnectTimeout) as exc:
                last = exc
        raise last

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.NetworkStream:
        raise PermanentFetchError("refusing to connect to a unix socket")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


# -- transport --------------------------------------------------------------------------------

_EXC_TABLE: Final[tuple[tuple[type[Exception], type[httpx2.HTTPError]], ...]] = (
    (httpcore2.ConnectTimeout, httpx2.ConnectTimeout),
    (httpcore2.ReadTimeout, httpx2.ReadTimeout),
    (httpcore2.WriteTimeout, httpx2.WriteTimeout),
    (httpcore2.PoolTimeout, httpx2.PoolTimeout),
    (httpcore2.TimeoutException, httpx2.TimeoutException),
    (httpcore2.ConnectError, httpx2.ConnectError),
    (httpcore2.ReadError, httpx2.ReadError),
    (httpcore2.WriteError, httpx2.WriteError),
    (httpcore2.NetworkError, httpx2.NetworkError),
    (httpcore2.RemoteProtocolError, httpx2.RemoteProtocolError),
    (httpcore2.LocalProtocolError, httpx2.LocalProtocolError),
    (httpcore2.ProtocolError, httpx2.ProtocolError),
    (httpcore2.UnsupportedProtocol, httpx2.UnsupportedProtocol),
    (httpcore2.ProxyError, httpx2.ProxyError),
)


@contextlib.contextmanager
def _map_httpcore_errors() -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        for source, target in _EXC_TABLE:
            if isinstance(exc, source):
                raise target(str(exc)) from exc
        raise


class _ResponseBody(httpx2.SyncByteStream):
    def __init__(self, stream: Iterable[bytes]) -> None:
        self._stream = stream

    def __iter__(self) -> Iterator[bytes]:
        with _map_httpcore_errors():
            yield from self._stream

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if callable(close):
            close()


class NetworkTransport(httpx2.BaseTransport):
    """httpx2 transport over an httpcore2 ConnectionPool with the given network backend."""

    def __init__(self, network: httpcore2.NetworkBackend) -> None:
        self._pool = httpcore2.ConnectionPool(
            ssl_context=httpx2.create_ssl_context(), network_backend=network
        )

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        if not isinstance(request.stream, httpx2.SyncByteStream):
            raise TypeError("NetworkTransport needs a synchronous request body")
        req = httpcore2.Request(
            method=request.method,
            url=httpcore2.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _map_httpcore_errors():
            resp = self._pool.handle_request(req)
        if not isinstance(resp.stream, Iterable):
            raise TypeError("httpcore2 returned an asynchronous body")
        return httpx2.Response(
            status_code=resp.status,
            headers=resp.headers,
            stream=_ResponseBody(resp.stream),
            extensions=resp.extensions,
        )

    def close(self) -> None:
        self._pool.close()


# -- helpers ------------------------------------------------------------------------------------


def parse_retry_after(value: str | None, now: datetime) -> float | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    if _DIGITS.fullmatch(text):
        return float(min(int(text), RETRY_AFTER_CAP_S))
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return min(max((when - now).total_seconds(), 0.0), RETRY_AFTER_CAP_S)


def media_type(content_type: str | None) -> str | None:
    if content_type is None:
        return None
    return content_type.partition(";")[0].strip().lower() or None


def _extension_of(path: str, *, any_segment: bool = False) -> str | None:
    segments = urllib.parse.unquote(path).split("/")
    for segment in reversed(segments if any_segment else segments[-1:]):
        if "." in segment:
            ext = segment.rsplit(".", 1)[-1].lower()
            if ext in AUDIO_EXTENSIONS:
                return ext
    return None


def pick_extension(final_path: str, content_type: str | None, original_path: str) -> str:
    return (
        _extension_of(final_path)
        or CONTENT_TYPE_EXTENSIONS.get(media_type(content_type) or "")
        or _extension_of(original_path, any_segment=True)
        or "bin"
    )


def limit_text(max_bytes: int) -> str:
    if max_bytes % MIB == 0:
        return f"{max_bytes // MIB} MiB"
    return f"{max_bytes} bytes"


def _content_length(response: httpx2.Response) -> int | None:
    raw = response.headers.get("content-length", "").strip()
    return int(raw) if _LENGTH.fullmatch(raw) else None


def _is_identity(response: httpx2.Response) -> bool:
    return response.headers.get("content-encoding", "identity").strip().lower() in ("", "identity")


# -- results and fetcher -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Document:
    url: str  # final URL after redirects
    content: bytes  # decoded (gzip/deflate removed)
    content_type: str | None  # media type, lower-case, parameters dropped
    redirects: int


@dataclass(frozen=True, slots=True)
class Downloaded:
    path: Path
    url: str
    size: int
    content_type: str | None
    redirects: int


class GuardedFetcher:
    """Not thread-safe. The httpx2 client is created on first use."""

    def __init__(
        self,
        *,
        allow_private: bool = False,
        resolve: Resolve = system_resolve,
        network: httpcore2.NetworkBackend | None = None,
        transport: httpx2.BaseTransport | None = None,
        timeout: httpx2.Timeout = DEFAULT_TIMEOUT,
        max_redirects: int = MAX_REDIRECTS,
        clock: Clock = utc_now,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        self._allow_private = allow_private
        self._resolve = resolve
        self._network = network
        self._transport = transport
        self._timeout = timeout
        self._max_redirects = max_redirects
        self._clock = clock
        self._monotonic = monotonic
        self._client: httpx2.Client | None = None

    @property
    def allow_private(self) -> bool:
        return self._allow_private

    def _get_client(self) -> httpx2.Client:
        if self._client is None:
            transport = self._transport
            if transport is None:
                backend: httpcore2.NetworkBackend = self._network or httpcore2.SyncBackend()
                if not self._allow_private:
                    backend = GuardedNetworkBackend(self._resolve, inner=backend)
                transport = NetworkTransport(backend)
            self._client = httpx2.Client(
                transport=transport,
                trust_env=False,
                follow_redirects=False,
                timeout=self._timeout,
                headers={"User-Agent": USER_AGENT},
            )
        return self._client

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- request plumbing --------------------------------------------------------------------

    @contextlib.contextmanager
    def _transport_errors(self, purpose: str, host: str) -> Iterator[None]:
        try:
            yield
        except (PermanentFetchError, TransientFetchError):
            raise
        except httpx2.TimeoutException as exc:
            raise TransientFetchError(
                f"{purpose} request to {_shorten(host)} timed out", host=host
            ) from exc
        except (httpx2.TransportError, httpx2.DecodingError) as exc:
            raise TransientFetchError(
                f"{purpose} request to {_shorten(host)} failed: {type(exc).__name__}", host=host
            ) from exc

    def _check_deadline(self, start: float, deadline_s: float, purpose: str, host: str) -> None:
        if self._monotonic() - start > deadline_s:
            raise TransientFetchError(
                f"{purpose} request to {_shorten(host)} took longer than {deadline_s:g} s",
                host=host,
            )

    @staticmethod
    def _check_url(url: httpx2.URL, purpose: str, *, redirect: bool) -> None:
        scheme = url.scheme
        if scheme not in ALLOWED_SCHEMES:
            shown = repr(scheme[:20]) if scheme else "''"
            if redirect:
                raise PermanentFetchError(
                    f"refusing to follow a redirect to a URL with scheme {shown}"
                )
            raise PermanentFetchError(f"refusing {purpose} URL with scheme {shown}")
        if not url.host:
            raise PermanentFetchError(f"refusing {purpose} URL without a host")
        if url.userinfo:
            what = "a redirect to a URL" if redirect else f"{purpose} URL"
            raise PermanentFetchError(f"refusing {what} with credentials", host=url.host)

    def _check_status(self, response: httpx2.Response, purpose: str, host: str) -> None:
        status = response.status_code
        if status in (200, 203):
            return
        retry_after = response.headers.get("retry-after")
        response.close()
        label = _shorten(host)
        if status in (408, 429) or 500 <= status <= 599:
            raise TransientFetchError(
                f"{purpose} request to {label} failed: HTTP {status}",
                host=host,
                status=status,
                retry_after_s=parse_retry_after(retry_after, self._clock()),
            )
        if 400 <= status <= 499:
            raise PermanentFetchError(
                f"{purpose} request to {label} failed: HTTP {status}", host=host, status=status
            )
        raise PermanentFetchError(
            f"{purpose} request to {label} returned unexpected HTTP {status}",
            host=host,
            status=status,
        )

    def _open(
        self,
        url: str,
        purpose: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str] | None,
        start: float,
        deadline_s: float,
    ) -> tuple[httpx2.Response, int, str]:
        """Follow redirects by hand. Returns the open final response, hop count, original path."""
        client = self._get_client()
        client.cookies.clear()
        try:
            request = client.build_request("GET", url, headers=dict(headers), params=params)
        except httpx2.InvalidURL as exc:
            raise PermanentFetchError(f"invalid {purpose} URL") from exc
        original_path = request.url.path
        redirects = 0
        while True:
            self._check_url(request.url, purpose, redirect=redirects > 0)
            host = request.url.host
            self._check_deadline(start, deadline_s, purpose, host)
            logger.debug("GET %s (hop %d)", _shorten(host), redirects)
            with self._transport_errors(purpose, host):
                response = client.send(request, stream=True)
            if not response.has_redirect_location:
                break
            next_request = response.next_request
            response.close()
            if next_request is None:
                raise PermanentFetchError(
                    f"{purpose} redirect from {_shorten(host)} has an invalid Location",
                    host=host,
                )
            redirects += 1
            if redirects > self._max_redirects:
                raise PermanentFetchError(
                    f"too many redirects for {purpose} (more than {self._max_redirects})",
                    host=host,
                )
            request = next_request
        self._check_status(response, purpose, host)
        return response, redirects, original_path

    @staticmethod
    def _too_large(purpose: str, max_bytes: int, limit_name: str | None) -> PermanentFetchError:
        hint = f" ({limit_name})" if limit_name else ""
        return PermanentFetchError(
            f"{purpose} is larger than the {limit_text(max_bytes)} limit{hint}"
        )

    # -- public API --------------------------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        max_bytes: int,
        purpose: str = "document",
        accept: str | None = None,
        params: Mapping[str, str] | None = None,
        deadline_s: float = DOCUMENT_DEADLINE_S,
    ) -> Document:
        start = self._monotonic()
        headers = {"Accept": accept} if accept else {}
        response, redirects, _ = self._open(
            url, purpose, headers=headers, params=params, start=start, deadline_s=deadline_s
        )
        host = response.url.host
        try:
            length = _content_length(response)
            if length is not None and length > max_bytes:
                raise self._too_large(purpose, max_bytes, None)
            chunks: list[bytes] = []
            size = 0
            with self._transport_errors(purpose, host):
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise self._too_large(purpose, max_bytes, None)
                    chunks.append(chunk)
                    self._check_deadline(start, deadline_s, purpose, host)
            return Document(
                url=str(response.url),
                content=b"".join(chunks),
                content_type=media_type(response.headers.get("content-type")),
                redirects=redirects,
            )
        finally:
            response.close()

    def download(
        self,
        url: str,
        destination: Callable[[str], Path],
        *,
        max_bytes: int,
        purpose: str = "audio",
        limit_name: str | None = None,
        deadline_s: float = DOWNLOAD_DEADLINE_S,
    ) -> Downloaded:
        start = self._monotonic()
        response, redirects, original_path = self._open(
            url,
            purpose,
            headers={"Accept-Encoding": "identity"},
            params=None,
            start=start,
            deadline_s=deadline_s,
        )
        host = response.url.host
        try:
            content_type = media_type(response.headers.get("content-type"))
            if content_type is not None and (
                content_type.startswith("text/") or content_type in _TEXTUAL_TYPES
            ):
                raise PermanentFetchError(
                    f"{purpose} URL on {_shorten(host)} returned {content_type}, not audio",
                    host=host,
                )
            length = _content_length(response)
            if length is not None and length > max_bytes:
                raise self._too_large(purpose, max_bytes, limit_name)
            ext = pick_extension(response.url.path, content_type, original_path)
            path = destination(ext)
            size = 0
            with (
                self._transport_errors(purpose, host),
                atomic_writer(path, temp_suffix=".part") as out,
            ):
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise self._too_large(purpose, max_bytes, limit_name)
                    out.write(chunk)
                    self._check_deadline(start, deadline_s, purpose, host)
                if size == 0:
                    raise TransientFetchError(
                        f"{purpose} from {_shorten(host)} was empty", host=host
                    )
                if _is_identity(response) and length is not None and length != size:
                    raise TransientFetchError(
                        f"{purpose} download from {_shorten(host)} was incomplete "
                        f"({size} of {length} bytes)",
                        host=host,
                    )
            return Downloaded(
                path=path,
                url=str(response.url),
                size=size,
                content_type=content_type,
                redirects=redirects,
            )
        finally:
            response.close()


def guarded_fetcher(settings: Settings) -> GuardedFetcher:
    return GuardedFetcher(allow_private=settings.allow_private_fetch)
