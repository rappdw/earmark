import gzip
import ipaddress
import itertools
import re
import socket
from collections.abc import Callable
from datetime import datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpcore2
import httpx2
import pytest
from pytest_socket import SocketBlockedError

import earmark
from earmark.errors import PermanentError, TransientError
from earmark.fetch import (
    MAX_REDIRECTS,
    MIB,
    USER_AGENT,
    BlockedAddressError,
    GuardedFetcher,
    GuardedNetworkBackend,
    PermanentFetchError,
    TransientFetchError,
    is_public_address,
    media_type,
    parse_retry_after,
    pick_extension,
    system_resolve,
)
from earmark.pipeline.worker import Shutdown
from tests.factories import T0
from tests.http_helpers import (
    ChunkStream,
    FakeMonotonic,
    ScriptedNetwork,
    StaticResolver,
    guarded_fetcher_for,
    mock_fetcher,
    raw_response,
)

Handler = Callable[[httpx2.Request], httpx2.Response]
AUDIO = b"ID3" + bytes(40)


def _ok(
    body: bytes = AUDIO, content_type: str | None = "audio/mpeg", **headers: str
) -> httpx2.Response:
    all_headers = dict(headers)
    if content_type is not None:
        all_headers["Content-Type"] = content_type
    return httpx2.Response(200, headers=all_headers, content=body)


def _redirect(location: str, status: int = 302) -> httpx2.Response:
    return httpx2.Response(status, headers={"Location": location})


class Counter:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.handler(request)


def _dest(root: Path) -> Callable[[str], Path]:
    def dest(ext: str) -> Path:
        return root / "ep" / f"audio.{ext}"

    return dest


def _leftovers(root: Path) -> list[str]:
    directory = root / "ep"
    return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


# -- A1: schemes and URLs -------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://a.example/x.mp3",
        "file:///etc/passwd",
        "data:audio/mpeg;base64,SUQz",
        "javascript:alert(1)",
        "gopher://a.example/",
        "example.com/a.mp3",
        "//a.example/a.mp3",
    ],
)
def test_refuses_non_http_schemes(tmp_path: Path, url: str) -> None:
    handler = Counter(lambda r: _ok())
    with mock_fetcher(handler) as fetcher:
        with pytest.raises(PermanentFetchError, match="scheme"):
            fetcher.get(url, max_bytes=MIB)
        with pytest.raises(PermanentFetchError, match="scheme"):
            fetcher.download(url, _dest(tmp_path), max_bytes=MIB)
    assert handler.requests == []


@pytest.mark.parametrize("target", ["ftp://x.example/c", "file:///etc/passwd"])
def test_redirect_to_other_scheme_refused(tmp_path: Path, target: str) -> None:
    handler = Counter(lambda r: _redirect(target))
    with mock_fetcher(handler) as fetcher:
        with pytest.raises(PermanentFetchError, match="redirect"):
            fetcher.get("https://a.example/x", max_bytes=MIB)
        with pytest.raises(PermanentFetchError, match="redirect"):
            fetcher.download("https://a.example/x", _dest(tmp_path), max_bytes=MIB)
    assert len(handler.requests) == 2


def test_refuses_credentials_in_url() -> None:
    handler = Counter(lambda r: _ok())
    with mock_fetcher(handler) as fetcher, pytest.raises(PermanentFetchError) as info:
        fetcher.get("https://u:p@a.example/x.mp3", max_bytes=MIB)
    assert "credentials" in info.value.message
    assert "p@" not in info.value.message
    assert handler.requests == []


def test_invalid_url_permanent() -> None:
    with (
        mock_fetcher(lambda r: _ok()) as fetcher,
        pytest.raises(PermanentFetchError, match="invalid document URL"),
    ):
        fetcher.get("http://[::1", max_bytes=MIB)


# -- A2: address policy -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("10.0.0.1", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("0.0.0.0", False),  # noqa: S104  # an address under test, not a bind
        ("255.255.255.255", False),
        ("192.0.2.1", False),
        ("198.18.0.1", False),
        ("100.64.0.1", False),
        ("100.100.100.200", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("::1", False),
        ("::", False),
        ("fe80::1", False),
        ("fd00:ec2::254", False),
        ("fc00::1", False),
        ("2001:db8::1", False),
        ("2002:a00:1::", False),
        ("2001::1", False),
        ("64:ff9b:1::a00:1", False),
        ("::ffff:127.0.0.1", False),
        ("::ffff:10.0.0.1", False),
        ("224.0.0.1", False),
        ("ff02::1", False),
        ("64:ff9b::a00:1", False),
        ("64:ff9b::7f00:1", False),
        ("::127.0.0.1", False),
        ("::ffff:0:a00:1", False),
        ("fec0::1", False),
        ("8.8.8.8", True),
        ("1.1.1.1", True),
        ("93.184.215.14", True),
        ("2606:4700::1111", True),
        ("::ffff:8.8.8.8", True),
        ("64:ff9b::808:808", True),
    ],
)
def test_is_public_address(address: str, public: bool) -> None:
    assert is_public_address(ipaddress.ip_address(address)) is public


def _guard_env(
    table: dict[str, list[str]], responses: dict[str, list[bytes]] | None = None, **kwargs: Any
) -> tuple[GuardedFetcher, ScriptedNetwork, StaticResolver]:
    network = ScriptedNetwork(responses or {})
    resolver = StaticResolver(table)
    return guarded_fetcher_for(network, resolver, **kwargs), network, resolver


def test_guard_refuses_private_name() -> None:
    fetcher, network, _ = _guard_env({"internal.example": ["10.0.0.5"]})
    with fetcher, pytest.raises(BlockedAddressError) as info:
        fetcher.get("http://internal.example/x", max_bytes=MIB)
    assert info.value.host == "internal.example"
    assert "EARMARK_ALLOW_PRIVATE_FETCH" in info.value.message
    assert network.connects == []


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]:8080/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[fd00:ec2::254]/",
        "http://100.100.100.200/",
    ],
)
def test_guard_refuses_ip_literals(url: str) -> None:
    fetcher, network, resolver = _guard_env({})
    with fetcher, pytest.raises(BlockedAddressError, match="not a public address"):
        fetcher.get(url, max_bytes=MIB)
    assert resolver.queries == []
    assert network.connects == []


def test_guard_refuses_mixed_answer() -> None:
    fetcher, network, _ = _guard_env({"mixed.example": ["8.8.8.8", "10.0.0.1"]})
    with fetcher, pytest.raises(BlockedAddressError):
        fetcher.get("http://mixed.example/", max_bytes=MIB)
    assert network.connects == []


def test_guard_refuses_metadata_name() -> None:
    fetcher, network, _ = _guard_env({"metadata.google.internal": ["169.254.169.254"]})
    with fetcher, pytest.raises(BlockedAddressError, match=r"metadata\.google\.internal"):
        fetcher.get("http://metadata.google.internal/computeMetadata/v1/", max_bytes=MIB)
    assert network.connects == []


def test_redirect_to_private_refused() -> None:
    fetcher, network, _ = _guard_env(
        {"feed.example": ["8.8.8.8"], "internal.example": ["10.0.0.5"]},
        {"8.8.8.8": [raw_response(302, [("Location", "http://internal.example/x")])]},
    )
    with fetcher, pytest.raises(BlockedAddressError):
        fetcher.get("https://feed.example/x", max_bytes=MIB)
    assert network.connects == [("8.8.8.8", 443)]


def test_redirect_to_literal_metadata_refused() -> None:
    fetcher, network, _ = _guard_env(
        {"feed.example": ["8.8.8.8"]},
        {"8.8.8.8": [raw_response(302, [("Location", "http://169.254.169.254/")])]},
    )
    with fetcher, pytest.raises(BlockedAddressError):
        fetcher.get("https://feed.example/x", max_bytes=MIB)
    assert network.connects == [("8.8.8.8", 443)]


def test_allow_private_lifts_guard() -> None:
    network = ScriptedNetwork(
        {"127.0.0.1": [raw_response(200, body=b"a")], "internal.example": [raw_response(200)]}
    )
    resolver = StaticResolver({})
    with guarded_fetcher_for(network, resolver, allow_private=True) as fetcher:
        assert fetcher.allow_private
        fetcher.get("http://127.0.0.1:8080/x", max_bytes=MIB)
        fetcher.get("http://internal.example/x", max_bytes=MIB)
    assert network.connects == [("127.0.0.1", 8080), ("internal.example", 80)]
    assert resolver.queries == []


def test_dns_rebinding_pinned() -> None:
    network = ScriptedNetwork({"8.8.8.8": [raw_response(200, body=b"ok")]})
    resolver = StaticResolver(answers={"rebind.example": [["8.8.8.8"], ["127.0.0.1"]]})
    with guarded_fetcher_for(network, resolver) as fetcher:
        assert fetcher.get("https://rebind.example/a", max_bytes=MIB).content == b"ok"
        assert network.connects == [("8.8.8.8", 443)]
        assert resolver.queries == ["rebind.example"]
        with pytest.raises(BlockedAddressError):
            fetcher.get("https://rebind.example/b", max_bytes=MIB)
    assert network.connects == [("8.8.8.8", 443)]
    assert resolver.queries == ["rebind.example"] * 2


def test_resolution_failure_transient() -> None:
    fetcher, network, _ = _guard_env({})
    with fetcher, pytest.raises(TransientFetchError, match="could not resolve") as info:
        fetcher.get("http://nope.example/", max_bytes=MIB)
    assert info.value.host == "nope.example"
    assert network.connects == []


def test_empty_resolution_transient() -> None:
    fetcher, _, _ = _guard_env({"empty.example": []})
    with fetcher, pytest.raises(TransientFetchError, match="could not resolve"):
        fetcher.get("http://empty.example/", max_bytes=MIB)


def test_connect_falls_back_to_next_address() -> None:
    network = ScriptedNetwork(
        {"1.1.1.1": [raw_response(200, body=b"ok")]},
        fail={"8.8.8.8": httpcore2.ConnectError("refused")},
    )
    resolver = StaticResolver({"two.example": ["8.8.8.8", "1.1.1.1"]})
    with guarded_fetcher_for(network, resolver) as fetcher:
        assert fetcher.get("http://two.example/", max_bytes=MIB).content == b"ok"
    assert network.connects == [("8.8.8.8", 80), ("1.1.1.1", 80)]


def test_unix_socket_refused() -> None:
    backend = GuardedNetworkBackend(StaticResolver({}), inner=ScriptedNetwork())
    with pytest.raises(PermanentFetchError):
        backend.connect_unix_socket("/run/docker.sock")


def test_system_resolve_parses_getaddrinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(*args: Any, **kwargs: Any) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1111%eth0", 80, 0, 2)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
            (socket.AF_UNIX, socket.SOCK_STREAM, 0, "", "/tmp/x"),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    assert system_resolve("x.example", 80) == [
        ipaddress.ip_address("8.8.8.8"),
        ipaddress.ip_address("2606:4700::1111"),
    ]


def test_system_resolve_propagates_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(*args: Any, **kwargs: Any) -> list[Any]:
        raise socket.gaierror(socket.EAI_NONAME, "nope")

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    with pytest.raises(OSError, match="nope"):
        system_resolve("x.example", 80)


def test_system_resolve_is_blocked_in_default_suite() -> None:
    with pytest.raises(SocketBlockedError):
        system_resolve("example.com", 80)


# -- A3: redirects ----------------------------------------------------------------------------


def test_max_redirects_at_least_ten() -> None:
    assert MAX_REDIRECTS >= 10


def _chain(total: int) -> Counter:
    def handler(request: httpx2.Request) -> httpx2.Response:
        n = int(request.url.path.removeprefix("/hop") or 0)
        if n >= total:
            return _ok()
        nxt = f"/hop{n + 1}" if n % 2 else f"https://h{n + 1}.example/hop{n + 1}"
        return _redirect(nxt)

    return Counter(handler)


def test_follows_twenty_redirects() -> None:
    handler = _chain(20)
    with mock_fetcher(handler) as fetcher:
        doc = fetcher.get("https://h0.example/hop0", max_bytes=MIB)
    assert doc.redirects == 20
    assert doc.url == "https://h19.example/hop20"
    assert len(handler.requests) == 21


def test_twenty_first_redirect_refused() -> None:
    handler = _chain(21)
    with (
        mock_fetcher(handler) as fetcher,
        pytest.raises(PermanentFetchError, match="too many redirects"),
    ):
        fetcher.get("https://h0.example/hop0", max_bytes=MIB)
    assert len(handler.requests) == 21


def test_tracker_chain_through_guard(tmp_path: Path) -> None:
    hosts = [
        ("dts.podtrac.com", "1.1.1.1"),
        ("pdst.fm", "1.0.0.1"),
        ("pfx.vpixl.com", "8.8.8.8"),
        ("pscrb.fm", "8.8.4.4"),
        ("tracking.swap.fm", "9.9.9.9"),
        ("nyt.simplecastaudio.com", "149.112.112.112"),
    ]
    responses: dict[str, list[bytes]] = {}
    for (_, ip), (nxt, _) in itertools.pairwise(hosts):
        responses[ip] = [
            raw_response(
                302,
                [("Location", f"https://{nxt}/p"), ("Content-Type", "text/html")],
                b"<html>moved</html>",
            )
        ]
    last_ip = hosts[-1][1]
    responses[last_ip] = [
        raw_response(302, [("Location", "/audio/128/default.mp3?aid=rss")], b"x"),
        raw_response(200, [("Content-Type", "audio/mpeg")], AUDIO),
    ]
    network = ScriptedNetwork(responses)
    resolver = StaticResolver({host: [ip] for host, ip in hosts})
    with guarded_fetcher_for(network, resolver) as fetcher:
        result = fetcher.download(
            f"https://{hosts[0][0]}/redirect.mp3/x", _dest(tmp_path), max_bytes=MIB
        )
    assert result.redirects == 6
    assert result.path.read_bytes() == AUDIO
    assert result.path.name == "audio.mp3"
    assert network.connects == [(ip, 443) for _, ip in hosts] + [(last_ip, 443)]


def test_redirect_without_valid_location() -> None:
    with (
        mock_fetcher(lambda r: _redirect("http://[::1")) as fetcher,
        pytest.raises(PermanentFetchError, match="invalid Location"),
    ):
        fetcher.get("https://a.example/x", max_bytes=MIB)


# -- A4: streaming to .part ------------------------------------------------------------------


def _stream_response(stream: ChunkStream, length: int | None, **headers: str) -> httpx2.Response:
    all_headers = {"Content-Type": "audio/mpeg", **headers}
    if length is not None:
        all_headers["Content-Length"] = str(length)
    return httpx2.Response(200, headers=all_headers, stream=stream)


def test_download_writes_part_then_renames(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def before(index: int) -> None:
        if index == 1:
            assert not (tmp_path / "ep" / "audio.mp3").exists()
            parts = sorted(p.name for p in (tmp_path / "ep").glob(".audio.mp3.*.part"))
            seen.append(parts)

    stream = ChunkStream([b"ID3a", b"bcde"], before_chunk=before)
    with mock_fetcher(lambda r: _stream_response(stream, 8)) as fetcher:
        result = fetcher.download("https://a.example/x.mp3", _dest(tmp_path), max_bytes=MIB)
    assert len(seen) == 1
    assert len(seen[0]) == 1
    final = tmp_path / "ep" / "audio.mp3"
    assert result.path == final
    assert final.read_bytes() == b"ID3abcde"
    assert final.stat().st_mode & 0o777 == 0o644
    assert result.size == 8
    assert _leftovers(tmp_path) == ["audio.mp3"]


def test_interrupted_download_leaves_nothing(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            stream = ChunkStream([b"ID3a", b"bcde"], raise_after=(1, httpx2.ReadError("boom")))
            return _stream_response(stream, 8)
        return _stream_response(ChunkStream([b"ID3abcde"]), 8)

    with mock_fetcher(handler) as fetcher:
        with pytest.raises(TransientFetchError):
            fetcher.download("https://a.example/x.mp3", _dest(tmp_path), max_bytes=MIB)
        assert _leftovers(tmp_path) == []
        fetcher.download("https://a.example/x.mp3", _dest(tmp_path), max_bytes=MIB)
    assert _leftovers(tmp_path) == ["audio.mp3"]


def test_shutdown_mid_download_leaves_nothing(tmp_path: Path) -> None:
    stream = ChunkStream([b"ID3a", b"bcde"], raise_after=(1, Shutdown()))
    with mock_fetcher(lambda r: _stream_response(stream, 8)) as fetcher, pytest.raises(Shutdown):
        fetcher.download("https://a.example/x.mp3", _dest(tmp_path), max_bytes=MIB)
    assert _leftovers(tmp_path) == []


def test_truncated_body_transient(tmp_path: Path) -> None:
    network = ScriptedNetwork(
        {"8.8.8.8": [raw_response(200, [("Content-Length", "10")], b"12345")]}
    )
    resolver = StaticResolver({"cdn.example": ["8.8.8.8"]})
    with guarded_fetcher_for(network, resolver) as fetcher, pytest.raises(TransientFetchError):
        fetcher.download("https://cdn.example/x.mp3", _dest(tmp_path), max_bytes=MIB)
    assert _leftovers(tmp_path) == []


def test_short_body_without_framing_error(tmp_path: Path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"Content-Length": "10"}, content=b"12345")

    with mock_fetcher(handler) as fetcher, pytest.raises(TransientFetchError, match="incomplete"):
        fetcher.download("https://a.example/x.mp3", _dest(tmp_path), max_bytes=MIB)
    assert _leftovers(tmp_path) == []


def test_empty_body_transient(tmp_path: Path) -> None:
    with (
        mock_fetcher(lambda r: _ok(b"")) as fetcher,
        pytest.raises(TransientFetchError, match="empty"),
    ):
        fetcher.download("https://a.example/x.mp3", _dest(tmp_path), max_bytes=MIB)
    assert _leftovers(tmp_path) == []


# -- A5: size caps ---------------------------------------------------------------------------


def test_download_content_length_over_cap(tmp_path: Path) -> None:
    stream = ChunkStream([b"x" * 11])
    destination_calls: list[str] = []

    def dest(ext: str) -> Path:
        destination_calls.append(ext)
        return tmp_path / "ep" / f"audio.{ext}"

    with (
        mock_fetcher(lambda r: _stream_response(stream, 11)) as fetcher,
        pytest.raises(PermanentFetchError, match="10 bytes"),
    ):
        fetcher.download("https://a.example/x.mp3", dest, max_bytes=10)
    assert stream.consumed == 0
    assert destination_calls == []
    assert _leftovers(tmp_path) == []


def test_download_bytes_over_cap_without_length(tmp_path: Path) -> None:
    stream = ChunkStream([b"abcd"] * 5)
    with (
        mock_fetcher(lambda r: _stream_response(stream, None)) as fetcher,
        pytest.raises(PermanentFetchError, match="larger than"),
    ):
        fetcher.download("https://a.example/x.mp3", _dest(tmp_path), max_bytes=10)
    assert stream.consumed == 3
    assert _leftovers(tmp_path) == []


def test_download_lying_content_length(tmp_path: Path) -> None:
    stream = ChunkStream([b"abcd"] * 3)
    with (
        mock_fetcher(lambda r: _stream_response(stream, 5)) as fetcher,
        pytest.raises(PermanentFetchError, match="larger than"),
    ):
        fetcher.download("https://a.example/x.mp3", _dest(tmp_path), max_bytes=10)
    assert _leftovers(tmp_path) == []


def test_download_exact_cap_allowed(tmp_path: Path) -> None:
    stream = ChunkStream([b"abcde", b"fghij"])
    with mock_fetcher(lambda r: _stream_response(stream, 10)) as fetcher:
        result = fetcher.download("https://a.example/x.mp3", _dest(tmp_path), max_bytes=10)
    assert result.size == 10


def test_size_message_names_limit(tmp_path: Path) -> None:
    stream = ChunkStream([b"x"])
    with (
        mock_fetcher(lambda r: _stream_response(stream, 2 * MIB + 1)) as fetcher,
        pytest.raises(PermanentFetchError) as info,
    ):
        fetcher.download(
            "https://a.example/x.mp3",
            _dest(tmp_path),
            max_bytes=2 * MIB,
            limit_name="EARMARK_MAX_AUDIO_MB",
        )
    assert "2 MiB limit (EARMARK_MAX_AUDIO_MB)" in info.value.message


def test_get_content_length_over_cap() -> None:
    stream = ChunkStream([b"x" * 20])
    with (
        mock_fetcher(lambda r: _stream_response(stream, 20)) as fetcher,
        pytest.raises(PermanentFetchError, match="larger than"),
    ):
        fetcher.get("https://a.example/feed", max_bytes=10)
    assert stream.consumed == 0


def test_get_decoded_bytes_over_cap() -> None:
    body = gzip.compress(bytes(200_000))
    assert len(body) < 1000

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"Content-Encoding": "gzip"}, content=body)

    with mock_fetcher(handler) as fetcher, pytest.raises(PermanentFetchError, match="larger than"):
        fetcher.get("https://a.example/feed", max_bytes=100_000)


def test_get_gzip_within_cap() -> None:
    body = gzip.compress(b"<rss/>" * 10)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"Content-Encoding": "gzip", "Content-Type": "Application/XML; x=y"},
            content=body,
        )

    with mock_fetcher(handler) as fetcher:
        doc = fetcher.get("https://a.example/feed", max_bytes=1000, accept="application/xml")
    assert doc.content == b"<rss/>" * 10
    assert doc.content_type == "application/xml"


# -- A6: statuses, timeouts, deadlines ---------------------------------------------------------

TRANSIENT_STATUSES = [408, 429, 500, 502, 503, 504, 599]
PERMANENT_STATUSES = [400, 401, 403, 404, 410, 451]


def _status_handler(status: int, *, via_redirect: bool) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if via_redirect and request.url.path == "/start":
            return _redirect("https://b.example/final")
        return httpx2.Response(status)

    return handler


@pytest.mark.parametrize("via_redirect", [False, True])
@pytest.mark.parametrize("status", TRANSIENT_STATUSES + PERMANENT_STATUSES)
def test_status_mapping(tmp_path: Path, status: int, via_redirect: bool) -> None:
    expected: type[Exception] = (
        TransientFetchError if status in TRANSIENT_STATUSES else PermanentFetchError
    )
    url = "https://a.example/start"
    calls: list[Callable[[GuardedFetcher], object]] = [
        lambda f: f.get(url, max_bytes=MIB),
        lambda f: f.download(url, _dest(tmp_path), max_bytes=MIB),
    ]
    for call in calls:
        with (
            mock_fetcher(_status_handler(status, via_redirect=via_redirect)) as fetcher,
            pytest.raises(expected) as info,
        ):
            call(fetcher)
        exc = info.value
        assert isinstance(exc, TransientFetchError | PermanentFetchError)
        assert exc.status == status
        assert f"HTTP {status}" in exc.message
        assert isinstance(exc, TransientError if status in TRANSIENT_STATUSES else PermanentError)
        assert exc.host == ("b.example" if via_redirect else "a.example")


@pytest.mark.parametrize("status", [204, 206, 304, 300])
def test_unexpected_status_permanent(status: int) -> None:
    with (
        mock_fetcher(lambda r: httpx2.Response(status)) as fetcher,
        pytest.raises(PermanentFetchError, match="unexpected HTTP") as info,
    ):
        fetcher.get("https://a.example/x", max_bytes=MIB)
    assert info.value.status == status


@pytest.mark.parametrize(
    "exc_type",
    [httpx2.ConnectTimeout, httpx2.ReadTimeout, httpx2.WriteTimeout, httpx2.PoolTimeout],
)
def test_timeouts_transient(exc_type: type[httpx2.TimeoutException]) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise exc_type("secret detail")

    with (
        mock_fetcher(handler) as fetcher,
        pytest.raises(TransientFetchError, match="timed out") as info,
    ):
        fetcher.get("https://a.example/x", max_bytes=MIB)
    assert "secret detail" not in info.value.message


@pytest.mark.parametrize(
    "exc_type",
    [httpx2.ConnectError, httpx2.ReadError, httpx2.RemoteProtocolError, httpx2.DecodingError],
)
def test_transport_errors_transient(exc_type: type[httpx2.HTTPError]) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise exc_type("secret detail")

    with mock_fetcher(handler) as fetcher, pytest.raises(TransientFetchError) as info:
        fetcher.get("https://a.example/x", max_bytes=MIB)
    assert info.value.message.endswith(exc_type.__name__)
    assert "secret detail" not in info.value.message


def test_read_timeout_mid_body_transient(tmp_path: Path) -> None:
    stream = ChunkStream([b"ID3a", b"bcde"], raise_after=(1, httpx2.ReadTimeout("slow")))
    with (
        mock_fetcher(lambda r: _stream_response(stream, 8)) as fetcher,
        pytest.raises(TransientFetchError, match="timed out"),
    ):
        fetcher.download("https://a.example/x.mp3", _dest(tmp_path), max_bytes=MIB)
    assert _leftovers(tmp_path) == []


def test_httpcore_errors_mapped_by_transport() -> None:
    resolver = StaticResolver({"feed.example": ["8.8.8.8"]})
    timeout = ScriptedNetwork(fail={"8.8.8.8": httpcore2.ConnectTimeout("x")})
    with (
        guarded_fetcher_for(timeout, resolver) as fetcher,
        pytest.raises(TransientFetchError, match="timed out"),
    ):
        fetcher.get("https://feed.example/x", max_bytes=MIB)
    refused = ScriptedNetwork(fail={"8.8.8.8": httpcore2.ConnectError("x")})
    with (
        guarded_fetcher_for(refused, resolver) as fetcher,
        pytest.raises(TransientFetchError, match="ConnectError"),
    ):
        fetcher.get("https://feed.example/x", max_bytes=MIB)


def test_retry_after_seconds() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, headers={"Retry-After": "120"})

    with mock_fetcher(handler) as fetcher, pytest.raises(TransientFetchError) as info:
        fetcher.get("https://a.example/x", max_bytes=MIB)
    assert info.value.retry_after_s == 120


def test_retry_after_http_date() -> None:
    when = format_datetime(T0 + timedelta(seconds=90), usegmt=True)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503, headers={"Retry-After": when})

    with (
        mock_fetcher(handler, clock=lambda: T0) as fetcher,
        pytest.raises(TransientFetchError) as info,
    ):
        fetcher.get("https://a.example/x", max_bytes=MIB)
    assert info.value.retry_after_s == 90


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("garbage", None),
        ("-5", None),
        ("120", 120.0),
        (" 7 ", 7.0),
        ("9999999999", 86400.0),
        ("99999999999", None),
        (format_datetime(T0 - timedelta(hours=1), usegmt=True), 0.0),
        (format_datetime(T0 + timedelta(days=30), usegmt=True), 86400.0),
    ],
)
def test_parse_retry_after(value: str | None, expected: float | None) -> None:
    now: datetime = T0
    assert parse_retry_after(value, now) == expected


@pytest.mark.parametrize("kind", ["get", "download"])
def test_deadline_exceeded_transient(tmp_path: Path, kind: str) -> None:
    stream = ChunkStream([b"abcd"] * 5)
    clock = FakeMonotonic(step=50)
    fetcher = mock_fetcher(lambda r: _stream_response(stream, None), monotonic=clock)

    def call() -> object:
        if kind == "get":
            return fetcher.get("https://a.example/x", max_bytes=MIB, deadline_s=120)
        return fetcher.download(
            "https://a.example/x.mp3", _dest(tmp_path), max_bytes=MIB, deadline_s=120
        )

    with pytest.raises(TransientFetchError, match="took longer than 120 s"):
        call()
    fetcher.close()
    assert _leftovers(tmp_path) == []


# -- A8: user agent ---------------------------------------------------------------------------


def test_user_agent_on_every_hop() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/a":
            return _redirect("https://b.example/b")
        if request.url.path == "/b":
            return _redirect("/c")
        return _ok()

    counter = Counter(handler)
    with mock_fetcher(counter) as fetcher:
        fetcher.get("https://a.example/a", max_bytes=MIB)
    assert len(counter.requests) == 3
    assert f"earmark/{earmark.__version__}" == USER_AGENT
    assert re.fullmatch(r"earmark/[0-9][0-9A-Za-z.+-]*", USER_AGENT)
    allowed = {"host", "accept", "accept-encoding", "connection", "user-agent", "cookie"}
    for request in counter.requests:
        assert request.headers["User-Agent"] == USER_AGENT
        assert {k.lower() for k in request.headers} <= allowed


def _wire_headers(raw: bytes) -> dict[str, str]:
    lines = raw.decode("latin-1").split("\r\n\r\n")[0].split("\r\n")[1:]
    return {k.lower(): v for k, _, v in (line.partition(": ") for line in lines)}


def test_user_agent_on_the_wire(tmp_path: Path) -> None:
    network = ScriptedNetwork(
        {"8.8.8.8": [raw_response(200, [("Content-Type", "audio/mpeg")], AUDIO)] * 2}
    )
    resolver = StaticResolver({"cdn.example": ["8.8.8.8"]})
    with guarded_fetcher_for(network, resolver) as fetcher:
        fetcher.get("https://cdn.example/feed", max_bytes=MIB)
        fetcher.download("https://cdn.example/x.mp3", _dest(tmp_path), max_bytes=MIB)
    get_headers = _wire_headers(network.streams[0].written)
    assert get_headers["user-agent"] == USER_AGENT
    assert get_headers["accept-encoding"] != "identity"
    assert set(get_headers) <= {"host", "accept", "accept-encoding", "connection", "user-agent"}
    download_headers = _wire_headers(network.streams[1].written)
    assert download_headers["user-agent"] == USER_AGENT
    assert download_headers["accept-encoding"] == "identity"


# -- A9: extensions ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("final", "ctype", "original", "expected"),
    [
        ("/a/b.mp3", None, "/x", "mp3"),
        ("/a/B.MP3", None, "/x", "mp3"),
        ("/ep.m4a", "audio/mpeg", "/x", "m4a"),
        ("/stream", "audio/mp4", "/x", "m4a"),
        ("/stream", "audio/mpeg; charset=binary", "/x", "mp3"),
        ("/stream", "application/octet-stream", "/redirect.mp3/x", "mp3"),
        ("/x/file.m4a", "audio/mpeg", "/redirect.mp3/y", "m4a"),
        ("/ep%2Em4a", None, "/x", "m4a"),
        ("/play", None, "/x", "bin"),
        ("/ep.php", "audio/ogg", "/x", "ogg"),
        ("/a.mp3/", None, "/x", "bin"),
        ("/", None, "/", "bin"),
    ],
)
def test_pick_extension(final: str, ctype: str | None, original: str, expected: str) -> None:
    assert pick_extension(final, ctype, original) == expected


def test_media_type() -> None:
    assert media_type("Audio/MPEG ; q=1") == "audio/mpeg"
    assert media_type("  ") is None
    assert media_type(None) is None


def test_download_names_file_by_extension(tmp_path: Path) -> None:
    with mock_fetcher(lambda r: _ok(content_type="audio/x-m4a")) as fetcher:
        result = fetcher.download(
            "https://a.example/stream?file=x.mp3", _dest(tmp_path), max_bytes=MIB
        )
    assert result.path == tmp_path / "ep" / "audio.m4a"
    assert result.content_type == "audio/x-m4a"
    assert result.path.exists()


@pytest.mark.parametrize(
    "ctype", ["text/html; charset=utf-8", "application/json", "application/rss+xml"]
)
def test_download_refuses_text_content(tmp_path: Path, ctype: str) -> None:
    destination_calls: list[str] = []

    def dest(ext: str) -> Path:
        destination_calls.append(ext)
        return tmp_path / "ep" / f"audio.{ext}"

    with (
        mock_fetcher(lambda r: _ok(b"<html>login</html>", ctype)) as fetcher,
        pytest.raises(PermanentFetchError, match="not audio"),
    ):
        fetcher.download("https://a.example/x.mp3", dest, max_bytes=MIB)
    assert destination_calls == []
    assert _leftovers(tmp_path) == []


def test_get_does_not_refuse_text() -> None:
    with mock_fetcher(lambda r: _ok(b"<rss/>", "text/html")) as fetcher:
        assert fetcher.get("https://a.example/x", max_bytes=MIB).content == b"<rss/>"


# -- B2, B3, B6 -------------------------------------------------------------------------------

SECRET_URL = "https://cdn.example/secret-path/ep.mp3?token=tok-77f1"


def _assert_clean(message: str) -> None:
    assert "tok-77f1" not in message
    assert "secret-path" not in message


def test_messages_omit_path_and_query(tmp_path: Path) -> None:
    messages: list[tuple[str, str]] = []

    def run(handler: Handler, host: str, *, max_bytes: int = MIB) -> None:
        with mock_fetcher(handler) as fetcher:
            try:
                fetcher.download(SECRET_URL, _dest(tmp_path), max_bytes=max_bytes)
            except (PermanentFetchError, TransientFetchError) as exc:
                messages.append((host, exc.message))
            else:  # pragma: no cover
                raise AssertionError("expected an error")

    def timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("x")

    run(lambda r: httpx2.Response(404), "cdn.example")
    run(lambda r: httpx2.Response(503), "cdn.example")
    run(timeout, "cdn.example")
    run(lambda r: _ok(b"x" * 20), "", max_bytes=10)
    run(lambda r: _ok(b"<html/>", "text/html"), "cdn.example")
    run(lambda r: _redirect("ftp://other.example/secret-path?token=tok-77f1"), "")
    fetcher, _, _ = _guard_env(
        {"cdn.example": ["8.8.8.8"], "internal.example": ["10.0.0.5"]},
        {
            "8.8.8.8": [
                raw_response(
                    302, [("Location", "http://internal.example/secret-path?token=tok-77f1")]
                )
            ]
        },
    )
    with fetcher, pytest.raises(BlockedAddressError) as info:
        fetcher.get(SECRET_URL, max_bytes=MIB)
    messages.append(("internal.example", info.value.message))
    assert len(messages) == 7
    for host, message in messages:
        _assert_clean(message)
        if host:
            assert host in message


def test_cookies_within_chain_not_across() -> None:
    seen: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("cookie"))
        if request.url.path == "/a":
            return httpx2.Response(302, headers={"Location": "/b", "Set-Cookie": "s=1; Path=/"})
        return _ok()

    with mock_fetcher(handler) as fetcher:
        fetcher.get("https://a.example/a", max_bytes=MIB)
        fetcher.get("https://a.example/c", max_bytes=MIB)
    assert seen == [None, "s=1", None]


def test_ignores_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://10.9.9.9:3128")
    fetcher, network, _ = _guard_env(
        {"feed.example": ["8.8.8.8"]}, {"8.8.8.8": [raw_response(200, body=b"ok")]}
    )
    with fetcher:
        fetcher.get("http://feed.example/x", max_bytes=MIB)
    assert network.connects == [("8.8.8.8", 80)]
    assert network.streams[0].written.startswith(b"GET /x HTTP/1.1\r\n")


# -- B7: live ---------------------------------------------------------------------------------

LOOKUP = "https://itunes.apple.com/lookup"


def _dns_intercepted() -> bool:
    return not all(is_public_address(a) for a in system_resolve("itunes.apple.com", 443))


@pytest.mark.network
def test_live_lookup_feed_and_enclosure(tmp_path: Path) -> None:
    import json
    from html import unescape

    with GuardedFetcher(allow_private=_dns_intercepted()) as fetcher:
        lookup = fetcher.get(LOOKUP, params={"id": "1200361736"}, max_bytes=MIB, purpose="lookup")
        feed_url = json.loads(lookup.content)["results"][0]["feedUrl"]
        feed = fetcher.get(feed_url, max_bytes=20 * MIB, purpose="feed")
        assert b"<rss" in feed.content
        match = re.search(rb'<enclosure[^>]*\burl="([^"]+)"', feed.content)
        assert match is not None
        enclosure = unescape(match.group(1).decode())
        with pytest.raises(PermanentFetchError, match="larger than"):
            fetcher.download(enclosure, _dest(tmp_path), max_bytes=256 * 1024)
    assert _leftovers(tmp_path) == []


@pytest.mark.network
def test_live_guard_allows_public_dns() -> None:
    if _dns_intercepted():
        pytest.skip("this network's DNS answers non-public addresses for public names")
    with GuardedFetcher() as fetcher:
        doc = fetcher.get(LOOKUP, params={"id": "1200361736"}, max_bytes=MIB, purpose="lookup")
    assert b"feedUrl" in doc.content


@pytest.mark.network
def test_live_guard_refuses_intercepted_dns() -> None:
    if not _dns_intercepted():
        pytest.skip("DNS is not intercepted here")
    with GuardedFetcher() as fetcher, pytest.raises(BlockedAddressError):
        fetcher.get(LOOKUP, params={"id": "1200361736"}, max_bytes=MIB, purpose="lookup")
