# Step 06: Guarded fetch and audio download

## 1. Outcome

`earmark/fetch.py` is the one way earmark talks to third-party URLs. `GuardedFetcher`
does a guarded GET for small documents (`get`, used by step 07 for the iTunes lookup and
RSS feeds) and a streaming download for media (`download`). Every connection goes
through a custom httpcore2 network backend that resolves the host itself, refuses the
connection if any address is loopback, private, link-local, shared, metadata, or
otherwise non-public, and then connects to the exact address it checked. So the check
holds on every redirect hop and DNS rebinding cannot slip a different address in
between check and connect. `EARMARK_ALLOW_PRIVATE_FETCH=true` removes the address
check, and nothing else.

`earmark/audio.py` holds `HttpAudioFetcher`, the `http` `AudioFetcher` backend. It
returns a cached `audio.<ext>` without a request, and otherwise streams the enclosure to
a `.part` temp file next to the final path and renames it on completion. The factory
registers it under `EARMARK_FETCH_BACKEND=http`, the default. HTTP status codes and
transport failures map to `TransientError` or `PermanentError` subclasses that carry the
host and status. Requests send `User-Agent: earmark/<version>` and nothing personal.

## 2. Findings

Checked on 2026-11-20 against the installed packages (`uv pip list`): httpx2 2.16.2,
httpcore2 2.16.2, h11 0.16.0, truststore 0.10.5, pytest-socket 0.8.1, Python 3.13.5.
Probes were scratch files outside the repository, run with
`uv run pytest -c pyproject.toml --rootdir .` (so `--disable-socket
--allow-unix-socket` and `filterwarnings = error` applied), plus two runs outside pytest
against live hosts. The probe files have been deleted.

### 2.1 httpx2 does not expose a network backend; httpcore2 does

- `httpx2.HTTPTransport.__init__` (in `httpx2/_transports/default.py`) takes `verify`,
  `cert`, `trust_env`, `http1`, `http2`, `limits`, `proxy`, `uds`, `local_address`,
  `retries`, `socket_options`. It has **no `network_backend` argument**, and it builds
  its pool in the private attribute `_pool`.
- `httpcore2.ConnectionPool.__init__` does take `network_backend: NetworkBackend | None`.
  `httpcore2.NetworkBackend` (public) has `connect_tcp(host, port, timeout=None,
  local_address=None, socket_options=None) -> NetworkStream`, `connect_unix_socket(...)`,
  and `sleep(seconds)`. `httpcore2.SyncBackend.connect_tcp` calls
  `socket.create_connection((host, port), timeout, source_address=...)` at call time.
- `httpcore2.HTTPConnection._connect` passes the origin host to `connect_tcp`, and then
  calls `stream.start_tls(ssl_context, server_hostname=sni_hostname or origin.host)`.
  So if the backend connects to an IP address, TLS SNI and certificate verification
  still use the hostname from the URL. Pinning does not break HTTPS.
- **Decision:** a small public-API transport, `NetworkTransport(httpx2.BaseTransport)`,
  wraps `httpcore2.ConnectionPool(ssl_context=httpx2.create_ssl_context(),
  network_backend=...)`, and copies the 25 lines of `HTTPTransport.handle_request`
  (build an `httpcore2.Request` from `request.url.raw_scheme/raw_host/port/raw_path`,
  return an `httpx2.Response` wrapping the httpcore stream). It maps httpcore2
  exceptions to the httpx2 classes of the same name with a local table, both when the
  request is sent and while the body is iterated. **Do not** subclass `HTTPTransport`
  and overwrite `_pool`: it works, but depends on a private attribute.
- A sketch of this transport and backend passed `mypy --strict` and ruff (apart from
  line length) in the probe. `resp.stream` is typed `Iterable[bytes] |
  AsyncIterable[bytes]` and `request.stream` as `SyncByteStream | AsyncByteStream`;
  narrow both with `isinstance` and raise `TypeError` instead of using `assert` (ruff
  `S101` applies to `src/`).

### 2.2 Behaviour of the guarded stack, observed with sockets blocked

The probe ran `httpx2.Client(transport=NetworkTransport(GuardedNetworkBackend(resolve,
inner=MockBackendSubclass)))` under pytest-socket:

- An exception that is not an httpcore2 exception, raised from `connect_tcp`, passes
  through `ConnectionPool.handle_request`, the transport's mapping, and
  `Client.send` unchanged (also with `follow_redirects=True`). So the backend can raise
  `BlockedAddressError` (a `PermanentError`) directly.
- `https://` works over `httpcore2.MockStream`: its `start_tls` returns itself, and
  `httpx2.create_ssl_context()` builds a real context without touching the network.
- The pool calls `connect_tcp` once per new connection, with `port` 80 or 443 from the
  URL. A canned response needs `Connection: close` so the next request to the same
  origin opens (and so resolves and checks) a new connection.
- With real h11 framing, a body shorter than `Content-Length` raises
  `httpx2.RemoteProtocolError("peer closed connection without sending complete message
  body (received 5 bytes, expected 10)")` while iterating.
- `httpcore2.MockBackend.connect_tcp` ignores `host`, so tests use a subclass that
  records `(host, port)` and serves per-host canned bytes.

### 2.3 httpx2 client behaviour the design relies on

- With `follow_redirects=False`, `Client.send` sets `response.next_request` for a
  redirect, built by `_build_redirect_request`: relative `Location` resolved against the
  request URL, method rewritten for 301/302/303 as browsers do, `Authorization` dropped
  across origins, cookies from the client jar. `response.has_redirect_location` is true
  for 301, 302, 303, 307, 308 with a `Location` header. A malformed `Location`
  (`http://[::1`) gives `has_redirect_location == True` and `next_request is None`.
- The client does **not** check schemes for us: `MockTransport` happily answered
  `ftp://a.example/`, and `next_request` for `Location: ftp://x.example/c` is an `ftp`
  request. `build_request("GET", "notaurl")` returns a relative URL `/notaurl` without
  raising. `build_request("GET", "http://[::1")` raises `httpx2.InvalidURL`, which is a
  plain `Exception`, not an `httpx2.HTTPError`.
- Exception tree: `ConnectTimeout`, `ReadTimeout`, `WriteTimeout`, `PoolTimeout` <
  `TimeoutException` < `TransportError` < `RequestError` < `HTTPError`. `ConnectError`,
  `ReadError`, `RemoteProtocolError`, `UnsupportedProtocol` are `TransportError`s.
  `DecodingError` and `TooManyRedirects` are `RequestError`s but not `TransportError`s.
- The default headers are `accept: */*`, `accept-encoding: gzip, deflate` (only the
  installed decoders; brotli and zstd are not installed), `connection: keep-alive`,
  `user-agent: python-httpx2/2.16.2`. Client-level `headers={"User-Agent": ...}`
  replaces the user agent, and redirect requests copy the original request's headers.
- `Client(transport=...)` ignores proxy environment variables
  (`allow_env_proxies = trust_env and transport is None`). Pass `trust_env=False` as
  well, so that stays true if the transport argument is ever dropped. httpx2 reads no
  `.netrc` unless `auth=httpx2.NetRCAuth()` is passed explicitly (no other `netrc`
  reference in `_client.py`), so there is nothing else to switch off.
- `MockTransport` with `content=` leaves `response.num_bytes_downloaded` at 0. Do not
  base any logic on it; count bytes yourself.
- `httpx2.URL("http://0x7f000001/").host == "0x7f000001"`: httpx2 does not parse
  numeric shorthand hosts. glibc's `getaddrinfo` does: `0x7f000001`, `2130706433`,
  `127.1`, and `0177.0.0.1` all resolve to `127.0.0.1` with no DNS query. Checking the
  **resolved** addresses (not the hostname string) is what catches these.
- `httpx2.URL("https://bücher.example/").raw_host == b"xn--bcher-kva.example"`, so the
  backend receives ASCII hostnames. `.host` is the Unicode form.
- No `__del__` or `ResourceWarning` in `httpx2/*.py` or `httpcore2/_sync/*.py`, so an
  unclosed client is not a test failure, but tests still close what they open.
- `httpcore2.default_ssl_context()` on Linux loads the compiled-in OpenSSL CA paths
  (with fallbacks to common bundle files), honouring `SSL_CERT_FILE`/`SSL_CERT_DIR`
  when `trust_env` is true. The container (step 16) needs a CA bundle.

### 2.4 `ipaddress` on Python 3.13: `is_global` alone is not enough

Probed `ipaddress.ip_address(a).is_global` and friends:

| Address | `is_global` | Note |
|---|---|---|
| `10.0.0.1`, `127.0.0.1`, `169.254.169.254`, `0.0.0.0`, `255.255.255.255`, `192.0.2.1`, `198.18.0.1` | False | private, loopback, link-local (AWS/GCP/Azure metadata), unspecified, broadcast, documentation, benchmarking |
| `100.64.0.1`, `100.100.100.200` | False | shared address space (CGNAT, Tailscale, Alibaba metadata) |
| `::1`, `::`, `fe80::1`, `fd00:ec2::254`, `2001:db8::1`, `2002:a00:1::`, `2001::1`, `64:ff9b:1::a00:1` | False | including the AWS IPv6 metadata address and 6to4 |
| `::ffff:127.0.0.1` | False | `ipv4_mapped` is `127.0.0.1` |
| `224.0.0.1`, `ff02::1` | **True** | multicast: must be blocked separately |
| `64:ff9b::a00:1` | **True** | NAT64 well-known prefix embedding `10.0.0.1` |
| `::127.0.0.1` | **True** | deprecated IPv4-compatible `::/96` |
| `::ffff:0:a00:1` | **True** | IPv4-translated `::ffff:0:0:0/96` |
| `fec0::1` | **True** | deprecated site-local (`is_site_local`) |
| `8.8.8.8`, `93.184.215.14`, `2606:4700::1111`, `::ffff:8.8.8.8`, `64:ff9b::808:808` | True | public |

So the rule (section 5.2) is `is_global`, minus multicast and site-local, with
IPv4-mapped and NAT64 addresses judged by the IPv4 address they embed, and the
IPv4-compatible and IPv4-translated ranges refused outright. Test fixtures must use
genuinely public addresses such as `8.8.8.8` and `1.1.1.1`: the documentation ranges
(`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`, `2001:db8::/32`) are not global
and are refused.

`getaddrinfo` returns IPv6 link-local scope separately (`('fe80::1', 80, 0, 1)` for
`fe80::1%lo`), and `ipaddress.ip_address("fe80::1%eth0")` keeps a `scope_id`. Strip any
`%scope` before parsing.

### 2.5 pytest-socket and the default resolver

pytest-socket's `--disable-socket` replaces `socket.socket` **and** `socket.getaddrinfo`
at test setup (step 01, section 2.4). If `system_resolve` bound `socket.getaddrinfo` as
a default argument at import time, a test calling it would capture the real function
and do live DNS in the default suite. `system_resolve` must look up
`socket.getaddrinfo` when it is called. A test proves the default suite still raises
`SocketBlockedError` through it.

### 2.6 Live checks (generic User-Agent `earmark/0.1.0`, a public show)

- `https://itunes.apple.com/lookup?id=1200361736` returns the show with
  `feedUrl: https://feeds.simplecast.com/Sl5CSM3S`. The feed serves
  `content-type: application/xml`, about 566 kB uncompressed, and gzip when asked.
- The first enclosure is
  `https://dts.podtrac.com/redirect.mp3/pdst.fm/e/pfx.vpixl.com/6qj4J/pscrb.fm/rss/p/tracking.swap.fm/track/<id>/nyt.simplecastaudio.com/<uuid>/episodes/<uuid>/audio/128/default.mp3?aid=rss_feed&...`.
  Following it takes **6 redirects** across 6 hosts (`dts.podtrac.com` → `pdst.fm` →
  `pfx.vpixl.com` → `pscrb.fm` → `tracking.swap.fm` → `nyt.simplecastaudio.com` →
  `nyt.simplecastaudio.com`), then `200`, `content-type: audio/mpeg`,
  `content-length: 31097151`, body starting `ID3`. The intermediate 302s carry
  `content-type: text/html` or `audio/mpeg` with tiny bodies, so content-type rules
  apply only to the final response. The final URL's query has a
  `listeningSessionID`: query strings must not reach logs or error messages.
- The probe transport from 2.1, with a `getaddrinfo` resolver and `SyncBackend`,
  followed that chain with real TLS and real SNI while connecting to IP addresses, and
  read the first bytes. Pinning works against real hosts.
- **This sandbox intercepts DNS.** Every name, including `example.com`,
  `itunes.apple.com`, and even `fake.invalid`, resolves to `10.200.9.2`
  (`/etc/resolv.conf` points at `127.0.0.11`; no proxy variables are set). With the
  guard on, every real fetch here is refused as non-public, which is correct
  behaviour. The `network` tests detect this and fall back to `allow_private=True` for
  the transfer checks, and skip the one test that needs public DNS (section 6). The
  live smoke in section 7 uses a loopback server and expects the refusal.

### 2.7 DNS rebinding: pin, do not document

The brief asks whether to pin or document the gap. Pinning costs one class (section
4.3), because httpcore2 lets the backend choose the address and still verifies TLS
against the hostname. The check and the connect use the same resolution, so a
rebinding answer cannot change the address between them; a second connection resolves
and checks again. Remaining limits, documented in the architecture: the guard trusts
the system resolver's answer (it cannot know what a public name *should* resolve to),
and `ALLOW_PRIVATE_FETCH=true` turns all of it off. No proxy is ever used.

### 2.8 Markdown formatting

`make lint` runs `ruff format --check .`, which formats the Python blocks in `docs/`.
The snippets here were checked with `uv run ruff format --check docs/`.

## 3. Files

| File | Change |
|---|---|
| `src/earmark/fetch.py` | New. Address policy, `system_resolve`, `GuardedNetworkBackend`, `NetworkTransport`, `GuardedFetcher` (`get`, `download`), error classes, extension and `Retry-After` helpers. Imports `httpx2` and `httpcore2` at module top; nothing light imports this module. |
| `src/earmark/audio.py` | New. `HttpAudioFetcher` (the `http` `AudioFetcher`), `build_http_fetcher`. |
| `src/earmark/pipeline/factory.py` | Register `"http"` in `FETCHERS` with a builder that imports `earmark.audio` inside the function. |
| `tests/http_helpers.py` | New. `StaticResolver`, `ScriptedNetwork`, `raw_response`, `ChunkStream`, `FakeMonotonic`, `mock_fetcher`. |
| `tests/test_fetch.py` | New. Policy, resolver, backend, transport, `get`, `download`, `network` tests. |
| `tests/test_audio.py` | New. `HttpAudioFetcher`, factory wiring, pipeline integration. |
| `tests/test_factory.py` | Adjust the two tests that assume every backend is unbuilt (section 6, B1). |
| `docs/ARCHITECTURE.md` | Module layout (`audio.py`), a "Fetching" subsection under Stage contracts, Security "Outbound fetches", Data layout (`.part`), Configuration (MiB), Known risks 5 and 6. |

No new dependency: httpx2 and httpcore2 are already locked (httpcore2 comes with
httpx2). Import `httpcore2` directly; it is a declared dependency of httpx2 and its
`NetworkBackend` API is public. Add `"httpcore2>=2.16.2"` to `[project.dependencies]`
so a direct import is backed by a direct requirement (run `uv lock`; the lock should
change only in the `earmark` package's requirement list). No new settings, so
`.env.example` and `Settings` are unchanged.

## 4. Interfaces

### 4.1 Constants and types (`earmark.fetch`)

```python
MIB: Final = 1024 * 1024
USER_AGENT: Final = f"earmark/{__version__}"
MAX_REDIRECTS: Final = 20
CHUNK_SIZE: Final = 64 * 1024
DEFAULT_TIMEOUT: Final = httpx2.Timeout(30.0, connect=10.0, pool=10.0)
DOCUMENT_DEADLINE_S: Final = 120.0
DOWNLOAD_DEADLINE_S: Final = 3600.0
ALLOWED_SCHEMES: Final = frozenset({"http", "https"})

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
type Resolve = Callable[[str, int], Sequence[IPAddress]]
type Monotonic = Callable[[], float]
```

`__version__` comes from `earmark`. `DEFAULT_TIMEOUT` is 30 s read and write, 10 s
connect and pool. The deadlines bound a whole call (all hops plus the body), so a server
that trickles bytes cannot hold the worker forever.

### 4.2 Errors

```python
class PermanentFetchError(PermanentError):
    def __init__(
        self, message: str, *, host: str | None = None, status: int | None = None
    ) -> None: ...


class TransientFetchError(TransientError):
    def __init__(
        self,
        message: str,
        *,
        host: str | None = None,
        status: int | None = None,
        retry_after_s: float | None = None,
    ) -> None: ...


class BlockedAddressError(PermanentFetchError):
    """The host is, or resolves to, an address the policy refuses."""
```

Both store `host` and `status` as attributes so step 07 can catch, for example, a
`PermanentFetchError` with `status == 404` and say "show not found". Messages follow
`errors.py`: short, no URL path or query, host shortened to at most 100 characters.

### 4.3 Address policy, resolver, backend, transport

```python
def is_public_address(address: IPAddress) -> bool: ...


def system_resolve(host: str, port: int) -> list[IPAddress]:
    """getaddrinfo(host, port, type=SOCK_STREAM), looked up at call time.

    Keeps AF_INET and AF_INET6 results, strips any %scope, parses with ipaddress,
    de-duplicates preserving order. Raises OSError (socket.gaierror) on failure.
    """
    ...


class GuardedNetworkBackend(httpcore2.NetworkBackend):
    def __init__(
        self, resolve: Resolve = system_resolve, *, inner: httpcore2.NetworkBackend | None = None
    ) -> None: ...  # inner defaults to httpcore2.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.NetworkStream: ...

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.NetworkStream: ...  # always raises PermanentFetchError

    def sleep(self, seconds: float) -> None: ...  # delegates to inner


class NetworkTransport(httpx2.BaseTransport):
    """httpx2 transport over an httpcore2 ConnectionPool with the given network backend."""

    def __init__(self, network: httpcore2.NetworkBackend) -> None: ...

    def handle_request(self, request: httpx2.Request) -> httpx2.Response: ...

    def close(self) -> None: ...
```

`NetworkTransport` builds `httpcore2.ConnectionPool(ssl_context=httpx2.create_ssl_context(),
network_backend=network)` with httpcore2's default limits, HTTP/1.1 only. Its exception
table maps, most specific first: `ConnectTimeout`, `ReadTimeout`, `WriteTimeout`,
`PoolTimeout`, `TimeoutException`, `ConnectError`, `ReadError`, `WriteError`,
`NetworkError`, `RemoteProtocolError`, `LocalProtocolError`, `ProtocolError`,
`UnsupportedProtocol`, `ProxyError` (httpcore2 class → httpx2 class of the same name,
`raise mapped(str(exc)) from exc`). Anything else is re-raised unchanged. The body
wrapper (`httpx2.SyncByteStream` subclass) applies the same mapping while iterating and
forwards `close()`.

### 4.4 `GuardedFetcher`

```python
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
    ) -> None: ...

    @property
    def allow_private(self) -> bool: ...

    def get(
        self,
        url: str,
        *,
        max_bytes: int,
        purpose: str = "document",
        accept: str | None = None,
        params: Mapping[str, str] | None = None,
        deadline_s: float = DOCUMENT_DEADLINE_S,
    ) -> Document: ...

    def download(
        self,
        url: str,
        destination: Callable[[str], Path],
        *,
        max_bytes: int,
        purpose: str = "audio",
        limit_name: str | None = None,
        deadline_s: float = DOWNLOAD_DEADLINE_S,
    ) -> Downloaded: ...

    def close(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, *exc_info: object) -> None: ...


def guarded_fetcher(settings: Settings) -> GuardedFetcher:
    return GuardedFetcher(allow_private=settings.allow_private_fetch)
```

- Transport selection: `transport` if given (tests only; the guard is then whatever that
  transport does). Otherwise `NetworkTransport(backend)` where `backend` is `network`
  (default `httpcore2.SyncBackend()`), wrapped in `GuardedNetworkBackend(resolve,
  inner=backend)` unless `allow_private`. With `allow_private=True` the host is passed
  to the inner backend unresolved, so IPv6 scopes and `/etc/hosts` entries behave as
  usual.
- The `httpx2.Client` is created lazily on first use with `transport=...`,
  `trust_env=False`, `follow_redirects=False`, `timeout=timeout`, and
  `headers={"User-Agent": USER_AGENT}`. `close()` closes it if created; safe to call
  twice. Building a `GuardedFetcher` does no I/O and loads no CA store.
- `destination(ext)` is called once, after the final response's headers pass every
  check and before any body byte is read. It returns the final path; the fetcher writes
  through `artifacts.atomic_writer(path, temp_suffix=".part")`.
- `purpose` appears in messages ("audio", "feed", "lookup"). `limit_name`, when given,
  is appended to the size-limit message, e.g. `(EARMARK_MAX_AUDIO_MB)`.
- Not thread-safe. Each worker process builds its own.

### 4.5 Helpers (`earmark.fetch`)

```python
def parse_retry_after(value: str | None, now: datetime) -> float | None: ...


def media_type(content_type: str | None) -> str | None: ...


def pick_extension(final_path: str, content_type: str | None, original_path: str) -> str: ...
```

`AUDIO_EXTENSIONS` and `CONTENT_TYPE_EXTENSIONS` are module constants (section 5.6).

### 4.6 `earmark.audio`

```python
STALE_PART_AGE_S: Final = 2 * DOWNLOAD_DEADLINE_S


class HttpAudioFetcher:
    def __init__(
        self, artifacts: ArtifactStore, fetcher: GuardedFetcher, *, max_bytes: int
    ) -> None: ...

    @property
    def fetcher(self) -> GuardedFetcher: ...

    @property
    def max_bytes(self) -> int: ...

    def fetch(self, episode: Episode, /) -> Path: ...

    def close(self) -> None: ...


def build_http_fetcher(settings: Settings, artifacts: ArtifactStore) -> HttpAudioFetcher:
    return HttpAudioFetcher(
        artifacts, guarded_fetcher(settings), max_bytes=settings.max_audio_mb * MIB
    )
```

### 4.7 Factory

```python
def _http_fetcher(ctx: BuildContext) -> AudioFetcher:
    from earmark.audio import build_http_fetcher

    return build_http_fetcher(ctx.settings, ctx.artifacts)


FETCHERS: Final[Mapping[str, Builder[AudioFetcher]]] = {
    "fake": lambda ctx: FakeAudioFetcher(ctx.artifacts),
    "http": _http_fetcher,
}
```

## 5. Behaviour and edge cases

### 5.1 URL checks (every hop, before any request is sent)

`_check_url(url: httpx2.URL, purpose, *, redirect: bool)` raises
`PermanentFetchError` when:

- the scheme is not `http` or `https` (case-insensitive; httpx2 lower-cases it). This
  covers `ftp:`, `file:`, `data:`, `javascript:`, `gopher:`, a missing scheme
  (`example.com/a.mp3`, `/notaurl`), and a scheme-relative `//host/a.mp3`. Message:
  `refusing {purpose} URL with scheme 'ftp'` (scheme shortened to 20 characters; empty
  shown as `''`), or `refusing to follow a redirect to a URL with scheme 'ftp'` on a
  hop;
- the host is empty;
- the URL has userinfo (`https://user:pw@host/`): `refusing {purpose} URL with
  credentials`. Public feeds never need them, and credentials would otherwise end up in
  logs and requests.

`build_request` raising `httpx2.InvalidURL` becomes `PermanentFetchError("invalid
{purpose} URL")`. The address policy is not checked here; it runs at connect time
(5.2), so it is applied to the address actually used.

### 5.2 Address policy and pinning

`is_public_address(a)`:

1. IPv6 with `ipv4_mapped` → judge the mapped IPv4 address.
2. IPv6 in `64:ff9b::/96` (NAT64) → judge the IPv4 address in the low 32 bits.
3. IPv6 in `::/96` (IPv4-compatible) or `::ffff:0:0:0/96` (IPv4-translated), or
   `is_site_local` → not public.
4. Otherwise public iff `a.is_global and not a.is_multicast`.

`GuardedNetworkBackend.connect_tcp(host, port, ...)`:

1. If `host` parses as an IP literal (after stripping `[`/`]` and `%scope`), the
   candidate list is that address and `resolve` is not called.
2. Else `addresses = list(resolve(host, port))`. `OSError` →
   `TransientFetchError(f"could not resolve {host}", host=host)` (DNS outages are worth
   retrying; a host that never resolves fails after `JOB_MAX_ATTEMPTS`). An empty list,
   the same.
3. If **any** candidate is not public → `BlockedAddressError(f"refusing to connect to
   {host}: it resolves to a non-public address ({addr}); see
   EARMARK_ALLOW_PRIVATE_FETCH", host=host)` (for a literal: `... {host}: not a public
   address; see ...`). Refusing the whole host, rather than filtering, keeps a
   half-private answer from being a way in. No connection is attempted.
4. Try the candidates in order: `inner.connect_tcp(str(addr), port, timeout=...,
   local_address=..., socket_options=...)`. Return the first stream. On
   `httpcore2.ConnectError` or `httpcore2.ConnectTimeout` try the next; if all fail,
   re-raise the last one (the transport maps it to httpx2, `GuardedFetcher` to
   `TransientFetchError`).

Each new connection resolves once and connects to what it checked. Keep-alive reuse is
per origin, so a reused connection was checked when it opened. `resolve` exceptions
other than `OSError` propagate (in the default suite that is pytest-socket's
`SocketBlockedError`, a `RuntimeError`).

### 5.3 Redirects

`_open(...)` drives the hops by hand with `client.send(request, stream=True)`:

```python
redirects = 0
while True:
    _check_url(request.url, purpose, redirect=redirects > 0)
    self._check_deadline(...)
    response = self._send(request, purpose)  # maps httpx2 errors, see 5.5
    if not response.has_redirect_location:
        break
    next_request = response.next_request
    response.close()
    if next_request is None:
        raise PermanentFetchError(f"{purpose} redirect from {host} has an invalid Location")
    redirects += 1
    if redirects > self._max_redirects:
        raise PermanentFetchError(f"too many redirects for {purpose} (more than {limit})")
    request = next_request
```

With `MAX_REDIRECTS = 20`, a chain of 20 redirects then `200` succeeds; the 21st
redirect response raises. Redirect bodies are never read and their content types are
ignored. The client's cookie jar is cleared at the start of every `get` and `download`,
so cookies flow within one chain (as trackers expect) but never between fetches.

### 5.4 Status codes (final response)

| Status | Result |
|---|---|
| 200, 203 | success |
| other 2xx (201, 202, 204, 205, 206, …) | `PermanentFetchError("{purpose} request to {host} returned unexpected HTTP {status}")` |
| 3xx not followed (300, 304, 305, 3xx without `Location`) | same, permanent |
| 408, 429, 500–599 | `TransientFetchError("{purpose} request to {host} failed: HTTP {status}", status=..., retry_after_s=parse_retry_after(...))` |
| other 4xx | `PermanentFetchError("{purpose} request to {host} failed: HTTP {status}", status=...)` |
| anything else | permanent, as for other 2xx |

The response is closed before raising. `parse_retry_after`: `None` or blank → `None`;
1 to 10 ASCII digits → that many seconds, capped at 86400; otherwise an HTTP-date via
`email.utils.parsedate_to_datetime` (naive results taken as UTC) → seconds from `now`
(the injected `clock()`), clamped to `[0, 86400]`; anything unparseable → `None`. The
job store caps it again at 6 h.

### 5.5 Transport failures and deadlines

Around `client.send` and around body iteration:

- `httpx2.TimeoutException` → `TransientFetchError(f"{purpose} request to {host} timed
  out")`.
- any other `httpx2.TransportError`, or `httpx2.DecodingError` →
  `TransientFetchError(f"{purpose} request to {host} failed: {type(exc).__name__}")`.
  The exception text is not copied in (it can be long and may echo the URL). Chain
  with `from exc`.
- `UnsupportedProtocol` cannot happen after 5.1, but would map as above.
- `PermanentFetchError`, `TransientFetchError`, and `BlockedAddressError` raised by the
  backend pass through untouched.
- Deadline: `start = monotonic()` at the beginning of the call. Before each hop and
  after each body chunk, if `monotonic() - start > deadline_s` →
  `TransientFetchError(f"{purpose} request to {host} took longer than {deadline_s:g}
  s")`. A single blocked read is still bounded by the read timeout.
- `OSError` while writing the file (disk full) is not caught: the runner treats it as
  an unexpected, transient error with a traceback.

### 5.6 `download`

1. `_open` with headers `Accept-Encoding: identity` (audio is already compressed, and
   identity makes `Content-Length` the file size).
2. `media_type(response.headers.get("content-type"))`: text before `;`, stripped,
   lower-cased, `None` if empty. If it starts with `text/` or is one of
   `application/json`, `application/xml`, `application/xhtml+xml`,
   `application/rss+xml`, `application/atom+xml` → `PermanentFetchError(f"{purpose} URL
   on {host} returned {media_type}, not audio")`. A login page or error page served
   with `200` must not be cached as audio.
3. `Content-Length`: parsed only if it is 1 to 19 ASCII digits, otherwise ignored. If
   larger than `max_bytes` → `PermanentFetchError(f"{purpose} is larger than the {limit}
   limit{hint}")` before reading the body, where `limit` is `N MiB` when `max_bytes` is
   a whole number of MiB, else `N bytes`, and `hint` is ` (EARMARK_MAX_AUDIO_MB)` when
   `limit_name` is given. Nothing is written and `destination` is not called.
4. `ext = pick_extension(response.url.path, content_type, original_url.path)`;
   `path = destination(ext)`.
5. `with atomic_writer(path, temp_suffix=".part") as f:` iterate
   `response.iter_bytes(CHUNK_SIZE)`; add each chunk's length to `size`; if `size >
   max_bytes` raise the size error from step 3 (the writer removes the temp file);
   write; check the deadline. Exactly `max_bytes` is allowed.
6. After the loop, still inside the writer: if `size == 0` → `TransientFetchError(f"{purpose}
   from {host} was empty")`. If the response has no `Content-Encoding` (or `identity`)
   and a parsed `Content-Length` differs from `size` → `TransientFetchError(f"{purpose}
   download from {host} was incomplete ({size} of {length} bytes)")`. Real h11 raises
   first on a short body (2.2); this check covers transports that do not.
7. The response is closed in a `finally`. Any exception, including a `BaseException`
   such as the worker's `Shutdown`, leaves the final path untouched (absent) and no
   `.part` file, because `atomic_writer` unlinks its temp file on `BaseException`.
8. Return `Downloaded(path, str(response.url), size, content_type, redirects)`.

`pick_extension`:

1. The suffix of the last segment of `final_path` (percent-decoded with
   `urllib.parse.unquote`, lower-cased, without the dot), if in `AUDIO_EXTENSIONS`.
2. Else `CONTENT_TYPE_EXTENSIONS.get(media_type(content_type))`.
3. Else the suffix of `original_path`, if in `AUDIO_EXTENSIONS`.
4. Else `"bin"`.

The query string is never consulted. The final URL comes first because tracker
prefixes decorate the original (`redirect.mp3/...`) while the CDN path names the real
file; the server's content type beats the original URL's decoration.

```python
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
```

Every value matches `ArtifactStore.audio_path`'s `[a-z0-9]{1,5}`.

### 5.7 `get`

Same `_open` (default `Accept-Encoding`, so gzip and deflate are accepted; `Accept:
{accept}` when given; `params` passed to `build_request`). Same status handling.
`Content-Length` over `max_bytes` → the size error before reading. Then
`iter_bytes(CHUNK_SIZE)` into a list, counting **decoded** bytes against `max_bytes`, so
a gzip bomb stops at the cap (at most one chunk's expansion is held beyond it). No
content-type refusal and no empty-body check: step 07 decides what a valid feed is.
Returns `Document(str(response.url), b"".join(chunks), media_type(...), redirects)`.

### 5.8 `HttpAudioFetcher.fetch`

1. `existing = artifacts.find_audio(episode.key)`; if not `None`, return it. No client
   is created and no request is made.
2. Remove stale temp files: in `artifacts.episode_dir(key)` (if it exists), unlink each
   `.audio.*.part` whose `st_mtime` is older than `STALE_PART_AGE_S` (2 h). Younger
   ones may belong to a concurrent worker and are left alone.
3. Empty `episode.audio_url` → `PermanentFetchError("episode has no audio URL")`.
4. `downloaded = fetcher.download(episode.audio_url, lambda ext:
   artifacts.audio_path(episode.key, ext), max_bytes=..., purpose="audio",
   limit_name="EARMARK_MAX_AUDIO_MB")`.
5. Log at INFO: `downloaded audio for %s: %d bytes from %s after %d redirects` (key,
   size, final host, redirects). Return `downloaded.path`.

`close()` closes the `GuardedFetcher`. The worker never calls it; the process exit
does. Audio deletion after transcription is step 08's.

### 5.9 Logging and messages

- Never log or put in a message a URL path or query; hosts only. DEBUG logs one line
  per hop: `GET %s (hop %d)` with the host. Feed and enclosure URLs can carry session
  ids or tokens.
- `USER_AGENT` is `earmark/<version>`: no repository URL, contact address, or host
  name. Requests carry only `Host`, `Accept`, `Accept-Encoding`, `Connection`,
  `User-Agent` (and `Cookie` within a chain if a hop set one).

## 6. Tests

### Fixtures and helpers (`tests/http_helpers.py`)

No recorded binary fixtures are needed; audio bodies are short byte strings starting
with `b"ID3"`. The live hostnames in 2.6 are used only as names.

- `StaticResolver(table: Mapping[str, Sequence[str]])`: callable `(host, port) ->
  list[IPAddress]`; records `queries: list[str]`; raises `socket.gaierror` for an
  unknown host. Optional per-host sequence of answers (`answers: Mapping[str,
  Sequence[Sequence[str]]]`) returning the next answer on each call, for the rebinding
  test.
- `ScriptedNetwork(httpcore2.NetworkBackend)`: `responses: dict[str, list[bytes]]`
  keyed by the host string it receives (an IP when guarded, the name when not); each
  `connect_tcp` pops the next response for that host into an `httpcore2.MockStream`,
  records `(host, port)` in `connects`, and raises `httpcore2.ConnectError` for an
  unscripted host. Optional `fail: Mapping[str, Exception]` raised on connect. A
  `RecordingStream(MockStream)` subclass keeps written bytes so tests can read the
  request line and headers on the wire.
- `raw_response(status, headers=(), body=b"") -> bytes`: an HTTP/1.1 response with
  `Content-Length` (unless headers already set it, or `chunked`) and `Connection:
  close`.
- `ChunkStream(httpx2.SyncByteStream)`: yields given chunks, records how many were
  consumed, optionally raises a given `BaseException` after `n` chunks, optionally runs
  a callback before each chunk (to inspect the directory mid-download).
- `FakeMonotonic`: `__call__` returns `now`; `advance(s)`; optional auto-step per call.
- `mock_fetcher(handler, **kwargs) -> GuardedFetcher`: `transport=httpx2.MockTransport(handler)`.
  `guarded_fetcher_for(network, resolver, **kwargs) -> GuardedFetcher`:
  `GuardedFetcher(network=network, resolve=resolver, **kwargs)`; tests use it in a
  `with` block.
- Public test addresses: `8.8.8.8`, `1.1.1.1`, `9.9.9.9`, `2606:4700::1111`. Never
  documentation ranges (2.4).

### Brief criteria

| # | Criterion | Tests |
|---|---|---|
| A1 | Non-http(s) schemes are refused | `test_fetch.py::test_refuses_non_http_schemes`, parametrized over `ftp://a.example/x.mp3`, `file:///etc/passwd`, `data:audio/mpeg;base64,SUQz`, `javascript:alert(1)`, `gopher://a.example/`, `example.com/a.mp3`, `//a.example/a.mp3`: both `get` and `download` raise `PermanentFetchError` whose message contains `scheme`, and the `MockTransport` handler was never called. `test_redirect_to_other_scheme_refused` (parametrized `ftp://x.example/c`, `file:///etc/passwd`): a 302 to it raises, the handler saw exactly one request, and the message says `redirect`. `test_refuses_credentials_in_url` (`https://u:p@a.example/x.mp3`, no request; the message does not contain `p@`). `test_invalid_url_permanent` (`http://[::1`). |
| A2 | Loopback, private, link-local, and metadata hosts are refused, also via redirect; DNS injectable; `ALLOW_PRIVATE_FETCH` lifts it | `test_is_public_address` (parametrized over every row of the 2.4 table, plus `172.16.0.1`, `192.168.1.1`, `fc00::1`, `::ffff:10.0.0.1`, `64:ff9b::7f00:1`, `::ffff:0:a00:1`; expected values as in 5.2). `test_guard_refuses_private_name` (resolver `internal.example → 10.0.0.5`: `BlockedAddressError` with `host == "internal.example"`, message names `EARMARK_ALLOW_PRIVATE_FETCH`, `network.connects == []`). `test_guard_refuses_ip_literals` (parametrized `http://127.0.0.1/`, `http://[::1]:8080/`, `http://169.254.169.254/latest/meta-data/`, `http://[fd00:ec2::254]/`, `http://100.100.100.200/`: blocked, resolver never queried, no connect). `test_guard_refuses_mixed_answer` (`8.8.8.8` and `10.0.0.1`: blocked, no connect). `test_guard_refuses_metadata_name` (`metadata.google.internal → 169.254.169.254`). `test_redirect_to_private_refused`: `https://feed.example → 8.8.8.8` answers 302 to `http://internal.example/x`; `BlockedAddressError`; `connects == [("8.8.8.8", 443)]`. `test_redirect_to_literal_metadata_refused` (302 to `http://169.254.169.254/`). `test_allow_private_lifts_guard`: `allow_private=True`, `http://127.0.0.1:8080/x` and `http://internal.example/x` succeed, `connects == [("127.0.0.1", 8080), ("internal.example", 80)]`, resolver never queried. `test_dns_rebinding_pinned`: resolver answers `8.8.8.8` then `127.0.0.1` for `rebind.example`; the first `get` succeeds with `connects == [("8.8.8.8", 443)]` and one query; a second `get` (new connection, `Connection: close`) raises `BlockedAddressError` with no new connect. `test_resolution_failure_transient` (unknown host: `TransientFetchError`, message `could not resolve`), `test_empty_resolution_transient`. `test_connect_falls_back_to_next_address` (two public addresses, the first raises `httpcore2.ConnectError`: success via the second, both in `connects`). `test_unix_socket_refused` (direct call on the backend). `test_system_resolve_parses_getaddrinfo` (monkeypatch `socket.getaddrinfo` to return v4, v6 with a scope, a duplicate, and an `AF_UNIX`-like entry: result is the de-duplicated v4 and v6 addresses in order, no scope). `test_system_resolve_propagates_oserror`. `test_system_resolve_is_blocked_in_default_suite` (no monkeypatch: `pytest.raises(SocketBlockedError)`, proving it is looked up at call time, 2.5). |
| A3 | Redirects followed up to a cap of at least 10 | `test_max_redirects_at_least_ten` (`MAX_REDIRECTS >= 10`). `test_follows_twenty_redirects` (20 relative and absolute redirects across hosts then 200: success, `Document.redirects == 20`, `Document.url` is the last URL). `test_twenty_first_redirect_refused` (21: `PermanentFetchError`, `too many redirects`, 21 requests made, no 22nd). `test_tracker_chain_through_guard`: the 6-hop chain of 2.6 (hostnames only) over `ScriptedNetwork`, each host mapped to a distinct public IP, `https`; succeeds; `connects` lists each IP on port 443 in order; `Downloaded.redirects == 6`; intermediate 302s with `Content-Type: text/html` bodies are ignored. `test_redirect_without_valid_location` (`Location: http://[::1`: permanent, `invalid Location`). `test_303_becomes_get` is not needed (we only GET). |
| A4 | Streams to `.part`, renamed on completion; interruption leaves nothing | `test_download_writes_part_then_renames`: a `ChunkStream` callback before the second chunk asserts the final path does not exist and exactly one file matching `.audio.mp3.*.part` exists in the episode dir; afterwards the final file has the full bytes, mode `0o644`, and no `.part` remains. `test_interrupted_download_leaves_nothing`: the stream raises `httpx2.ReadError` after one chunk: `TransientFetchError`; no `audio.*` and no `.part`; a second attempt with a good stream succeeds. `test_shutdown_mid_download_leaves_nothing`: raises `earmark.pipeline.worker.Shutdown` after one chunk: `Shutdown` propagates unchanged; nothing left. `test_truncated_body_transient`: over `ScriptedNetwork`, `Content-Length: 10` with 5 bytes: `TransientFetchError`; nothing left. `test_short_body_without_framing_error` (MockTransport, `Content-Length: 10`, 5 bytes delivered cleanly: `TransientFetchError`, `incomplete`; nothing left). `test_empty_body_transient`. |
| A5 | Size cap against `Content-Length` and bytes read | `test_download_content_length_over_cap`: `Content-Length: 11`, `max_bytes=10`: `PermanentFetchError` mentioning `10 bytes`; `ChunkStream.consumed == 0`; `destination` never called. `test_download_bytes_over_cap_without_length`: no `Content-Length`, chunks of 4 bytes, `max_bytes=10`: raises after the third chunk (`consumed == 3`), nothing left. `test_download_lying_content_length` (`Content-Length: 5`, 12 bytes streamed, `max_bytes=10`: size error, not "incomplete"). `test_download_exact_cap_allowed`. `test_size_message_names_limit` (`max_bytes=2 * MIB`, `limit_name="EARMARK_MAX_AUDIO_MB"` → `2 MiB limit (EARMARK_MAX_AUDIO_MB)`). `test_get_content_length_over_cap`. `test_get_decoded_bytes_over_cap`: a gzip body of 200 000 zero bytes (compressed to under 1 kB) with `Content-Encoding: gzip`, `max_bytes=100_000`: `PermanentFetchError`. `test_get_gzip_within_cap` (decoded content returned). |
| A6 | Timeouts, 5xx, 408, 429 → `TransientError`; other 4xx → `PermanentError` | `test_status_mapping`, parametrized: 408, 429, 500, 502, 503, 504, 599 → `TransientFetchError` with `.status` set and `isinstance(exc, TransientError)`; 400, 401, 403, 404, 410, 451 → `PermanentFetchError` with `.status`, `isinstance(exc, PermanentError)`; for both `get` and `download`, and on a final response reached through a redirect. `test_unexpected_status_permanent` (204, 206, 304, 300). `test_timeouts_transient`, parametrized over `httpx2.ConnectTimeout`, `ReadTimeout`, `WriteTimeout`, `PoolTimeout` raised by the handler: `TransientFetchError`, `timed out`. `test_transport_errors_transient` (`ConnectError`, `ReadError`, `RemoteProtocolError`, `DecodingError`: message ends with the class name and does not contain the exception text). `test_read_timeout_mid_body_transient` (download; nothing left). `test_httpcore_errors_mapped_by_transport` (over `ScriptedNetwork`: `fail={"8.8.8.8": httpcore2.ConnectTimeout("x")}` → `TransientFetchError`, `timed out`; `httpcore2.ConnectError` on the only address → `TransientFetchError`). `test_retry_after_seconds` (`429`, `Retry-After: 120` → `retry_after_s == 120`). `test_retry_after_http_date` (`503`, a date 90 s after the injected clock → 90). `test_parse_retry_after` table (blank, garbage, negative, `-5`, past date → 0, huge digits → 86400, 11 digits → `None`). `test_deadline_exceeded_transient` (`FakeMonotonic` stepping 50 s per call, `deadline_s=120`: `TransientFetchError`, `took longer than 120 s`; nothing left). |
| A7 | Existing audio file → no request | `test_audio.py::test_existing_audio_no_request`: pre-create `audio.m4a`; `HttpAudioFetcher` over a `mock_fetcher` whose handler fails the test if called; `fetch` returns that path; handler calls `== 0`. `test_existing_audio_no_connection`: the same with a `GuardedFetcher(network=ScriptedNetwork({}), resolve=StaticResolver({}))`: no connect, no query. |
| A8 | Generic `User-Agent`, no personal details | `test_user_agent_on_every_hop`: a 2-hop redirect then 200; for every request the handler saw, `User-Agent == USER_AGENT == f"earmark/{earmark.__version__}"`, it matches `^earmark/[0-9][0-9A-Za-z.+-]*$`, and the header names are a subset of `{host, accept, accept-encoding, connection, user-agent, cookie}` (no `from`, `referer`, `authorization`). `test_user_agent_on_the_wire`: over `ScriptedNetwork` with `RecordingStream`, the written request has the header line `User-Agent: earmark/<version>` exactly, and `Accept-Encoding: identity` for `download`. The exact-equality assertions are what rule out personal details: the value is the product name and version and nothing else. |
| A9 | Extension from URL path or `Content-Type` | `test_pick_extension`, parametrized: `/a/b.mp3` → mp3; `/a/B.MP3` → mp3; `/ep.m4a` with `audio/mpeg` → m4a (path wins); `/stream` with `audio/mp4` → m4a; `/stream` with `audio/mpeg; charset=binary` → mp3; final `/stream`, `application/octet-stream`, original `/redirect.mp3/x` → mp3; final `/x/file.m4a`, original `/redirect.mp3/...` → m4a; `/ep%2Em4a` → m4a; `/play?file=x.mp3` (path `/play`) with no type → bin; `/ep.php` with `audio/ogg` → ogg; `/a.mp3/` → falls through; nothing → bin. `test_download_names_file_by_extension` (`audio/x-m4a` at `/stream`: `audio.m4a` exists). `test_download_refuses_text_content` (parametrized `text/html; charset=utf-8`, `application/json`, `application/rss+xml`: `PermanentFetchError`, nothing written, `destination` not called). |

### Added criteria

| # | Criterion | Tests |
|---|---|---|
| B1 | `http` is registered and built from settings; factory rules hold | `test_audio.py::test_factory_builds_http_fetcher` (`fetch_backend="http"`, `max_audio_mb=3`, `allow_private_fetch=True`: `isinstance(b.fetcher, HttpAudioFetcher)`, its max bytes `== 3 * MIB`, its fetcher's `allow_private` is true; expose both as read-only properties `max_bytes` and `allow_private`). In `test_factory.py`: `test_unbuilt_backend_names_setting` is parametrized only over fields whose `Literal` still has an unbuilt value (compute the list at collection; `fetch_backend` drops out), and `test_default_settings_list_all_unbuilt` expects 6 lines and none starting with `EARMARK_FETCH_BACKEND`. `test_pipeline_modules_import_light` stays as is and must still pass (the builder imports lazily). New `test_fetch_module_not_imported_by_factory` (subprocess: `import earmark.pipeline.factory`; `earmark.fetch` and `earmark.audio` not in `sys.modules`). |
| B2 | Messages carry host and status, never path or query | `test_messages_omit_path_and_query`: URL `https://cdn.example/secret-path/ep.mp3?token=tok-77f1`, responses 404, 503, timeout, size cap, text/html, blocked redirect: each message contains `cdn.example` (or the redirect host) and neither `tok-77f1` nor `secret-path`. `test_debug_log_has_hosts_only` (`caplog` at DEBUG for `earmark.fetch` and `earmark.audio`: no record contains `tok-77f1` or `secret-path`). |
| B3 | Cookies do not persist between fetches | `test_cookies_within_chain_not_across`: hop 1 sets `Set-Cookie: s=1`, hop 2 sees `Cookie: s=1`; a later `get` to the same host sends no `Cookie`. |
| B4 | `HttpAudioFetcher` conforms and works in the pipeline | `test_audio.py::test_http_fetcher_satisfies_protocol` (annotated `fetcher: AudioFetcher = HttpAudioFetcher(...)`; mypy is the proof). `test_pipeline_with_http_fetcher`: `make_env` with `dataclasses.replace(fakes.backends(), fetcher=HttpAudioFetcher(artifacts, mock_fetcher(handler), max_bytes=MIB))`, the handler serving `b"ID3" + bytes(100)` as `audio/mpeg` for any `https://fake.invalid/audio/*.mp3`; one job runs to `done`, `audio.mp3` exists, the fake transcriber was called with that path; a second job with a new prompt makes no request (the transcript is cached). `test_pipeline_http_404_fails_job` (handler 404: job `failed`, `stage == "fetch"`, `error` contains `HTTP 404` and `fake.invalid`). `test_pipeline_http_503_requeues` (job `queued`, `attempts == 1`, `run_after` honours `Retry-After: 600`). |
| B5 | Stale `.part` files are cleaned, fresh ones kept | `test_audio.py::test_stale_part_removed_fresh_kept` (create `.audio.mp3.aaaa.part` with `os.utime` 3 h old and `.audio.mp3.bbbb.part` now; after a successful fetch the old one is gone and the fresh one remains). |
| B6 | No proxy from the environment | `test_ignores_proxy_env`: `monkeypatch.setenv` `HTTPS_PROXY`, `HTTP_PROXY`, and `ALL_PROXY` to `http://10.9.9.9:3128`; over `ScriptedNetwork`, the only connect is to the resolved public address of `feed.example` (never `10.9.9.9`) and the request line is origin-form (`GET /x HTTP/1.1`, not an absolute URL). |
| B7 | Live: the real chain and size cap work; the guard passes public hosts | `@pytest.mark.network` in `test_fetch.py`. Helper `_dns_intercepted()`: `not all(is_public_address(a) for a in system_resolve("itunes.apple.com", 443))`. `test_live_lookup_feed_and_enclosure`: `GuardedFetcher(allow_private=_dns_intercepted())`; `get("https://itunes.apple.com/lookup", params={"id": "1200361736"}, max_bytes=MIB, purpose="lookup")` → JSON with a `feedUrl`; `get(feedUrl, max_bytes=20 * MIB, purpose="feed")` → bytes containing `<rss`; first `<enclosure ... url="...">` by regex (unescape `&amp;`); `download(enclosure, dest, max_bytes=256 * 1024)` → `PermanentFetchError` with `larger than`, no file in `tmp_path`. `test_live_guard_allows_public_dns`: skipped with a reason when `_dns_intercepted()`; else `GuardedFetcher()` `get`s the lookup successfully. `test_live_guard_refuses_intercepted_dns`: skipped unless `_dns_intercepted()`; `GuardedFetcher().get(lookup)` raises `BlockedAddressError`. |

## 7. Verification

```bash
cd /home/sandy/dev/earmark
make check
uv run pytest tests/test_fetch.py tests/test_audio.py tests/test_factory.py -q
uv run ruff format --check docs/
uv run pytest -m network tests/test_fetch.py -v -rs   # in this sandbox: 2 pass, 1 skipped (public DNS)
uv lock --check
git diff --stat uv.lock   # only earmark's own requirement list changes, if at all

# Light imports still hold
uv run python -c 'import sys, earmark.pipeline.factory; print(sorted(m for m in ("httpx2", "httpcore2", "earmark.fetch", "earmark.audio") if m in sys.modules))'
# expect: []

# fetch.py is the only module that imports httpx2/httpcore2 for outbound requests
grep -rn "import httpx2\|import httpcore2\|from httpx2\|from httpcore2" src/earmark
# expect: src/earmark/fetch.py only (audio.py imports earmark.fetch)
```

Live smoke 1, loopback server (no internet). Run from a scratch directory, with the
scratch path in `$SCRATCH`:

```bash
cd "$SCRATCH" && mkdir -p www && head -c 300000 /dev/urandom > www/ep.mp3
uv run --project /home/sandy/dev/earmark python -m http.server 18799 --bind 127.0.0.1 \
  --directory www > http.log 2>&1 &
HTTP_PID=$!
for i in $(seq 50); do curl -sf -o /dev/null http://127.0.0.1:18799/ep.mp3 && break; sleep 0.1; done
: > http.log   # forget the readiness probe
cat > smoke_fetch.py <<'EOF'
import sys
from pathlib import Path

from earmark.artifacts import ArtifactStore
from earmark.audio import HttpAudioFetcher
from earmark.errors import PermanentError
from earmark.fakes import FakeResolver
from earmark.fetch import GuardedFetcher

data = Path(sys.argv[1])
canon = "https://podcasts.apple.com/us/podcast/id1200361736?i=1000802907981"
url = "http://127.0.0.1:18799/ep.mp3"
episode = FakeResolver().resolve(canon).model_copy(update={"audio_url": url})
store = ArtifactStore(data)
try:
    HttpAudioFetcher(store, GuardedFetcher(), max_bytes=1 << 20).fetch(episode)
except PermanentError as exc:
    print("guarded:", type(exc).__name__, exc.message)
fetcher = HttpAudioFetcher(store, GuardedFetcher(allow_private=True), max_bytes=1 << 20)
path = fetcher.fetch(episode)
print("downloaded:", path.name, path.stat().st_size)
print("cached:", fetcher.fetch(episode) == path)
small = HttpAudioFetcher(ArtifactStore(data / "small"), GuardedFetcher(allow_private=True), max_bytes=1000)
try:
    small.fetch(episode)
except PermanentError as exc:
    print("capped:", exc.message)
print("leftovers:", sorted(p.name for p in data.rglob("*.part")))
EOF
uv run --project /home/sandy/dev/earmark python smoke_fetch.py "$SCRATCH/data"
# guarded: BlockedAddressError refusing to connect to 127.0.0.1: not a public address; see EARMARK_ALLOW_PRIVATE_FETCH
# downloaded: audio.mp3 300000
# cached: True
# capped: audio is larger than the 1000 bytes limit (EARMARK_MAX_AUDIO_MB)
# leftovers: []
grep -c '"GET /ep.mp3' http.log   # 2: the download and the capped attempt; none for guarded or cached
kill $HTTP_PID
```

Live smoke 2, the worker with the real fetcher and the fake resolver (whose audio URL is
`https://fake.invalid/...`):

```bash
cd "$SCRATCH"
E=/home/sandy/dev/earmark/.venv/bin/earmark
export EARMARK_DATA_DIR="$SCRATCH/data2" EARMARK_VAULT_DIR="$SCRATCH/vault2"
for v in RESOLVER_BACKEND ASR_BACKEND LLM_PROVIDER STORE_BACKEND INDEX_BACKEND \
  NOTIFY_BACKEND; do export "EARMARK_$v=fake"; done
export EARMARK_FETCH_BACKEND=http
$E submit 'https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000802907981'
$E worker --once
$E jobs --json
# In this sandbox (DNS answers 10.200.9.2 for every name): status "failed", stage "fetch",
# error "refusing to connect to fake.invalid: it resolves to a non-public address
# (10.200.9.2); see EARMARK_ALLOW_PRIVATE_FETCH".
# On a machine with normal DNS: status "queued", attempts 1, error "could not resolve fake.invalid".
ls "$SCRATCH"/data2/episodes/*/   # episode.json shared.json, no audio.*, no .part
```

Also check:

- `git status` lists only the files in section 3 and this plan.
- No `# type: ignore` or `noqa` in new code, no loosened lint, type, or test settings,
  and no `enable_socket` outside the `network` tests.
- `docs/ARCHITECTURE.md` matches section 8.

Not verifiable in the sandbox: the address guard against real public DNS (the
`network` test `test_live_guard_allows_public_dns` runs on CI's scheduled network
workflow from step 07, and on the homelab), and real IPv6 connectivity.

## 8. Deviations

1. **DNS rebinding is closed by pinning, not documented as a gap** (the brief's
   research question). The check runs inside the connection's network backend and
   connects to the address it checked; section 2.7.
2. **`MAX_REDIRECTS = 20`**, above the brief's minimum of 10. It matches browsers and
   httpx2's own default; the live Daily chain uses 6.
3. **New module `earmark/audio.py`** for the `http` backend, so `fetch.py` stays a
   generic guarded client that step 07 reuses. Added to the architecture's module
   layout.
4. **Direct dependency on `httpcore2`**, already installed with httpx2, because httpx2
   exposes no way to choose the network backend (2.1). No other package considered:
   writing a resolver hook any other way would mean monkeypatching `socket`.
5. **Additions beyond the brief**, each small and tested: URLs with credentials are
   refused; textual content types and empty bodies are not saved as audio; a whole-call
   deadline (2 min documents, 1 h downloads); cookies are cleared between fetches;
   stale `.part` files older than 2 h are removed; `Retry-After` is honoured; error
   classes carry `host` and `status` for step 07.
6. **Sizes are MiB.** `EARMARK_MAX_AUDIO_MB` and `EARMARK_MAX_FEED_MB` are multiplied by
   1024 × 1024. The architecture's configuration table now says so.
7. **The address policy is broader than the brief's list.** Anything not globally
   routable is refused (shared CGNAT space, documentation, benchmarking, multicast,
   reserved, NAT64 or mapped forms of those). Tailnet addresses (`100.64.0.0/10`) are
   therefore refused unless `ALLOW_PRIVATE_FETCH` is set.
8. **`test_factory.py` changes**: two tests assumed every backend name was unbuilt; they
   now compute which fields still have an unbuilt name.
9. **`network` tests adapt to intercepting DNS** (2.6): the transfer test runs with
   `allow_private=True` when public names resolve to non-public addresses, and the
   guard tests skip or assert the refusal accordingly.
10. **Architecture edits** made with this plan: module layout (`fetch.py` contents,
    `audio.py`); a "Fetching" subsection under Stage contracts; Security "Outbound
    fetches" (policy, pinning, schemes, credentials, redirects, cookies, proxies,
    User-Agent); Data layout (`.part` temp files); Configuration (MiB); Known risks 5
    (sandbox DNS) and 6 (filtering resolvers that answer `0.0.0.0`).

## 9. Out of scope

- Resolving Apple URLs, the iTunes lookup, RSS parsing and its XML safety: step 07
  (it calls `GuardedFetcher.get` with `MAX_FEED_MB`).
- Deleting audio after transcription (`KEEP_AUDIO`): step 08.
- Resuming partial downloads with `Range`, parallel downloads, HTTP/2, and a per-host
  rate limit.
- `earmark doctor` checks for DNS and outbound reachability: step 15.
- CA bundle in the container image: step 16.
