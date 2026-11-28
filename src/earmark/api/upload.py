"""The streaming multipart reader behind ``POST /transcriptions`` uploads.

Starlette's form parser spools every file part through a temporary file (memory, then the
system temp dir), which breaks the rule that an upload goes straight to ``data/uploads``.
So the route reads the request stream itself and feeds python-multipart's push parser,
which holds no part data beyond its boundary look-behind. Received file data
is hashed and appended to a ``Staging`` writer chunk by chunk.

Every failure path aborts the writer, so a rejected or interrupted upload leaves nothing in
``uploads/``. Messages never echo field contents, other than the first 40 characters of an
unknown field's name.
"""

import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers

from earmark.fetch import limit_text
from earmark.models import FORM_FIELDS, MAX_INSTRUCTIONS_CHARS
from earmark.sources import clean_upload_name, upload_extension
from earmark.staging import Staging, UploadWriter

if TYPE_CHECKING:
    from python_multipart.multipart import MultipartCallbacks

logger = logging.getLogger(__name__)

MULTIPART_OVERHEAD_BYTES: Final = 128 * 1024  # text fields, part headers, boundaries
MAX_FIELD_BYTES: Final = 4 * MAX_INSTRUCTIONS_CHARS + 64
MAX_PARTS: Final = 8
FILE_FIELD: Final = "file"
TEXT_FIELDS: Final = FORM_FIELDS
ALLOWED_FIELDS: Final = ", ".join((FILE_FIELD, *TEXT_FIELDS))
_DIGITS: Final = re.compile(r"[0-9]{1,19}")

MALFORMED: Final = "the multipart form is malformed"
NOT_AUDIO: Final = (
    "the file does not look like audio: send it with an audio content type such as "
    "audio/mpeg, or a file name ending in an audio extension such as .mp3 or .m4a"
)
ONE_FILE: Final = "the form must have exactly one file field named 'file'"
NO_BOUNDARY: Final = "the multipart form has no boundary"
INCOMPLETE: Final = "the multipart form is incomplete"
TOO_MANY_PARTS: Final = "the form has too many parts"
EMPTY_FILE: Final = "the uploaded file is empty"
FIELD_NOT_TEXT: Final = "field '{name}' must be a text field"
FIELD_REPEATED: Final = "field '{name}' is given more than once"
FIELD_TOO_LONG: Final = "field '{name}' is too long"
FIELD_NOT_UTF8: Final = "field '{name}' is not valid UTF-8"
UNKNOWN_FIELD: Final = "unknown field '{name}' (allowed: {allowed})"


class UploadRejected(Exception):  # noqa: N818  # a rejection, named for what it says
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ReceivedUpload:
    fields: dict[str, str]  # decoded text parts, at most one per name
    writer: UploadWriter  # complete but not committed; the caller commits or aborts
    filename: str | None  # cleaned (clean_upload_name)


def too_large(max_file_bytes: int) -> UploadRejected:
    return UploadRejected(
        413,
        f"the upload is larger than the {limit_text(max_file_bytes)} limit (EARMARK_MAX_UPLOAD_MB)",
    )


@dataclass
class _State:
    """Everything the parser callbacks collect. Callbacks run inside ``parser.write``."""

    staging: Staging
    text_fields: tuple[str, ...] = FORM_FIELDS
    parts: int = 0
    ended: bool = False
    writer: UploadWriter | None = None
    filename: str | None = None
    texts: dict[str, bytearray] = field(default_factory=dict)
    pending: list[bytes] = field(default_factory=list)  # file data from the current write()
    # per-part state
    kind: str | None = None  # "file", "text" or None
    text_name: str | None = None
    header_name: bytearray = field(default_factory=bytearray)
    header_value: bytearray = field(default_factory=bytearray)
    headers: dict[bytes, bytes] = field(default_factory=dict)

    # -- callbacks --------------------------------------------------------------------

    def on_part_begin(self) -> None:
        self.parts += 1
        if self.parts > MAX_PARTS:
            raise UploadRejected(422, TOO_MANY_PARTS)
        self.kind = None
        self.text_name = None
        self.headers = {}
        self.header_name.clear()
        self.header_value.clear()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self.header_name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self.header_value += data[start:end]

    def on_header_end(self) -> None:
        self.headers[bytes(self.header_name).strip().lower()] = bytes(self.header_value).strip()
        self.header_name.clear()
        self.header_value.clear()

    def on_headers_finished(self) -> None:
        disposition = self.headers.get(b"content-disposition")
        if disposition is None:
            raise UploadRejected(422, MALFORMED)
        kind, params = parse_options_header(disposition)
        raw_name = params.get(b"name")
        if kind.lower() != b"form-data" or raw_name is None:
            raise UploadRejected(422, MALFORMED)
        name = raw_name.decode("utf-8", "replace")
        if name == FILE_FIELD:
            self._begin_file(params.get(b"filename"))
        elif name in self.text_fields:
            if b"filename" in params:
                raise UploadRejected(422, FIELD_NOT_TEXT.format(name=name))
            if name in self.texts:
                raise UploadRejected(422, FIELD_REPEATED.format(name=name))
            self.kind = "text"
            self.text_name = name
            self.texts[name] = bytearray()
        else:
            allowed = ", ".join((FILE_FIELD, *self.text_fields))
            raise UploadRejected(422, UNKNOWN_FIELD.format(name=name[:40], allowed=allowed))

    def _begin_file(self, raw_filename: bytes | None) -> None:
        if self.writer is not None:
            raise UploadRejected(422, ONE_FILE)
        filename = None if raw_filename is None else raw_filename.decode("utf-8", "replace")
        content_type = self.headers.get(b"content-type", b"").decode("latin-1") or None
        ext = upload_extension(filename, content_type)
        if ext is None:
            raise UploadRejected(415, NOT_AUDIO)
        self.filename = clean_upload_name(filename)
        self.kind = "file"
        # One sync os.open in the event loop, once per upload.
        self.writer = self.staging.begin_upload(ext)

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self.kind == "file":
            self.pending.append(bytes(data[start:end]))
        elif self.kind == "text" and self.text_name is not None:
            buffer = self.texts[self.text_name]
            buffer += data[start:end]
            if len(buffer) > MAX_FIELD_BYTES:
                raise UploadRejected(422, FIELD_TOO_LONG.format(name=self.text_name))

    def on_end(self) -> None:
        self.ended = True

    def callbacks(self) -> "MultipartCallbacks":
        return {
            "on_part_begin": self.on_part_begin,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_part_data": self.on_part_data,
            "on_end": self.on_end,
        }


def _write_all(writer: UploadWriter, chunks: list[bytes]) -> None:
    for chunk in chunks:
        writer.write(chunk)


def _boundary(headers: Headers) -> bytes:
    _, params = parse_options_header(headers.get("content-type"))
    boundary = params.get(b"boundary")
    if not boundary:
        raise UploadRejected(422, NO_BOUNDARY)
    return boundary


async def receive_upload(
    headers: Headers,
    stream: AsyncIterator[bytes],
    staging: Staging,
    *,
    max_file_bytes: int,
    text_fields: tuple[str, ...] = FORM_FIELDS,
) -> ReceivedUpload:
    """Read a multipart body into a staged, uncommitted upload.

    Raises ``UploadRejected``. Whatever it staged is removed before any exception leaves.
    """
    state = _State(staging, text_fields)
    try:
        return await _receive(headers, stream, state, max_file_bytes)
    except BaseException:
        if state.writer is not None:
            state.writer.abort()
        raise


async def _receive(
    headers: Headers, stream: AsyncIterator[bytes], state: _State, max_file_bytes: int
) -> ReceivedUpload:
    body_limit = max_file_bytes + MULTIPART_OVERHEAD_BYTES
    declared = headers.get("content-length", "").strip()
    if _DIGITS.fullmatch(declared) and int(declared) > body_limit:
        raise too_large(max_file_bytes)
    boundary = _boundary(headers)
    try:
        parser = MultipartParser(boundary, state.callbacks())
    except FormParserError:
        raise UploadRejected(422, MALFORMED) from None

    total = 0
    async for chunk in stream:
        total += len(chunk)
        if total > body_limit:
            raise too_large(max_file_bytes)
        try:
            consumed = parser.write(chunk)
        except FormParserError:
            raise UploadRejected(422, MALFORMED) from None
        if consumed != len(chunk):
            raise UploadRejected(422, MALFORMED)
        if state.pending:
            chunks, state.pending = state.pending, []
            writer = state.writer
            if writer is None:  # pragma: no cover - data only arrives after a file part opened
                raise UploadRejected(422, MALFORMED)
            if writer.size + sum(len(c) for c in chunks) > max_file_bytes:
                raise too_large(max_file_bytes)
            await run_in_threadpool(_write_all, writer, chunks)
    try:
        parser.finalize()
    except FormParserError:
        raise UploadRejected(422, MALFORMED) from None
    if not state.ended:
        raise UploadRejected(422, INCOMPLETE)

    writer = state.writer
    if writer is None:
        raise UploadRejected(422, ONE_FILE)
    if writer.size == 0:
        raise UploadRejected(422, EMPTY_FILE)
    fields: dict[str, str] = {}
    for name, raw in state.texts.items():
        try:
            fields[name] = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            raise UploadRejected(422, FIELD_NOT_UTF8.format(name=name)) from None
    return ReceivedUpload(fields=fields, writer=writer, filename=state.filename)


def log_rejection(exc: UploadRejected) -> None:
    logger.info("upload rejected with status %d", exc.status)
