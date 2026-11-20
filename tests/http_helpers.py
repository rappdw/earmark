"""Scripted network, resolver, and stream helpers for the fetch and audio tests."""

import ipaddress
import socket
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Any

import httpcore2
import httpx2

from earmark.fetch import GuardedFetcher, IPAddress


class StaticResolver:
    """A ``Resolve`` over a fixed table; ``answers`` gives a different answer per call."""

    def __init__(
        self,
        table: Mapping[str, Sequence[str]] | None = None,
        *,
        answers: Mapping[str, Sequence[Sequence[str]]] | None = None,
    ) -> None:
        self._table = dict(table or {})
        self._answers = {host: list(seq) for host, seq in (answers or {}).items()}
        self.queries: list[str] = []

    def __call__(self, host: str, port: int) -> list[IPAddress]:
        self.queries.append(host)
        if self._answers.get(host):
            found = self._answers[host].pop(0)
        elif host in self._table:
            found = self._table[host]
        else:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [ipaddress.ip_address(a) for a in found]


class RecordingStream(httpcore2.MockStream):
    def __init__(self, buffer: list[bytes]) -> None:
        super().__init__(buffer)
        self.written = b""

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.written += buffer


class ScriptedNetwork(httpcore2.NetworkBackend):
    """Serves canned bytes per connection, keyed by the host string it is asked to connect to."""

    def __init__(
        self,
        responses: Mapping[str, Sequence[bytes]] | None = None,
        *,
        fail: Mapping[str, Exception] | None = None,
    ) -> None:
        self.responses = {host: list(seq) for host, seq in (responses or {}).items()}
        self.fail = dict(fail or {})
        self.connects: list[tuple[str, int]] = []
        self.streams: list[RecordingStream] = []

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.NetworkStream:
        self.connects.append((host, port))
        if host in self.fail:
            raise self.fail[host]
        queue = self.responses.get(host)
        if not queue:
            raise httpcore2.ConnectError(f"unscripted host {host}")
        stream = RecordingStream([queue.pop(0)])
        self.streams.append(stream)
        return stream

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.NetworkStream:
        raise httpcore2.ConnectError("no unix sockets")

    def sleep(self, seconds: float) -> None:
        pass


def raw_response(status: int, headers: Sequence[tuple[str, str]] = (), body: bytes = b"") -> bytes:
    names = {k.lower() for k, _ in headers}
    lines = [f"HTTP/1.1 {status} X"]
    lines += [f"{k}: {v}" for k, v in headers]
    if "content-length" not in names and "transfer-encoding" not in names:
        lines.append(f"Content-Length: {len(body)}")
    lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


class ChunkStream(httpx2.SyncByteStream):
    """Yields ``chunks``; can run a callback before each chunk or raise after ``n`` chunks."""

    def __init__(
        self,
        chunks: Sequence[bytes],
        *,
        raise_after: tuple[int, BaseException] | None = None,
        before_chunk: Callable[[int], None] | None = None,
    ) -> None:
        self.chunks = list(chunks)
        self.consumed = 0
        self._raise_after = raise_after
        self._before = before_chunk

    def __iter__(self) -> Iterator[bytes]:
        for index, chunk in enumerate(self.chunks):
            if self._raise_after is not None and index == self._raise_after[0]:
                raise self._raise_after[1]
            if self._before is not None:
                self._before(index)
            self.consumed += 1
            yield chunk
        if self._raise_after is not None and self._raise_after[0] >= len(self.chunks):
            raise self._raise_after[1]


class FakeMonotonic:
    def __init__(self, now: float = 0.0, *, step: float = 0.0) -> None:
        self.now = now
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value

    def advance(self, seconds: float) -> None:
        self.now += seconds


def mock_fetcher(
    handler: Callable[[httpx2.Request], httpx2.Response], **kwargs: Any
) -> GuardedFetcher:
    return GuardedFetcher(transport=httpx2.MockTransport(handler), **kwargs)


def guarded_fetcher_for(
    network: httpcore2.NetworkBackend, resolver: StaticResolver, **kwargs: Any
) -> GuardedFetcher:
    return GuardedFetcher(network=network, resolve=resolver, **kwargs)
