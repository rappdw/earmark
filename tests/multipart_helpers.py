"""Exact multipart/form-data bytes, in any part order."""

from collections.abc import Sequence
from dataclasses import dataclass

BOUNDARY = b"earmark-test-boundary"


@dataclass(frozen=True)
class Part:
    name: str
    data: bytes | str
    filename: str | None = None
    content_type: str | None = None


def multipart_body(
    parts: Sequence[Part], boundary: bytes = BOUNDARY, *, close: bool = True
) -> bytes:
    out = bytearray()
    for part in parts:
        disposition = f'form-data; name="{part.name}"'
        if part.filename is not None:
            disposition += f'; filename="{part.filename}"'
        out += b"--" + boundary + b"\r\n"
        out += b"Content-Disposition: " + disposition.encode("utf-8") + b"\r\n"
        if part.content_type is not None:
            out += b"Content-Type: " + part.content_type.encode("latin-1") + b"\r\n"
        out += b"\r\n"
        out += part.data.encode("utf-8") if isinstance(part.data, str) else part.data
        out += b"\r\n"
    if close:
        out += b"--" + boundary + b"--\r\n"
    return bytes(out)


def content_type(boundary: bytes = BOUNDARY) -> str:
    return "multipart/form-data; boundary=" + boundary.decode("ascii")


def audio(n: int = 64) -> bytes:
    return b"ID3" + bytes(n)


def split(data: bytes, size: int) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]
