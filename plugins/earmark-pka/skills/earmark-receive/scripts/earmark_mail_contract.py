"""The earmark mail contract (``contract: 1``) as a checker: standard library only, Python 3.9.

``check_mail.py`` is the launcher; this module holds the rules. It reads a whole message
(``message``), or the parts a PKA's inbox tools give it (``files``), and reports every
mechanically checkable rule of the contract. Failure messages are fixed ASCII sentences and
never quote text from the mail, because the mail's text is third-party text.

Exit codes: 0 every rule that ran passed, 1 a rule failed (or the checker itself failed),
2 a prerequisite is missing, 64 usage error.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import email
import email.message
import email.policy
import hashlib
import json
import re
import sys
import unicodedata
import urllib.parse
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final, NoReturn

CONTRACT: Final = 1
EXIT_OK, EXIT_FAILED, EXIT_PREREQUISITE, EXIT_USAGE = 0, 1, 2, 64
CHECKER_NAME: Final = "earmark-mail-contract"


@dataclasses.dataclass(frozen=True)
class Rule:
    id: str
    needs: str  # "message" | "media-type" | "subject" | "body" | "attachments"
    description: str


def _rules() -> tuple[Rule, ...]:
    table = [
        ("message.defects", "message", "no header or part of the message has parser defects"),
        ("message.structure", "message", "multipart/mixed: one inline text/plain, two attachments"),
        ("attachment.media-type", "media-type", "attachments are text/markdown in UTF-8"),
        ("attachment.names", "attachments", "exactly one attachment name ends in .instructions.md"),
        ("filename.note", "attachments", "note name is YYYY-MM-DD-<slug>.md within the caps"),
        (
            "filename.instructions",
            "attachments",
            "instructions name is the note's stem plus .instructions.md",
        ),
        ("file.utf8", "attachments", "each attachment decodes as UTF-8"),
        ("file.bom", "attachments", "no byte order mark"),
        ("file.lf", "attachments", "no carriage return in an attachment"),
        ("frontmatter.fence", "attachments", "frontmatter sits between two --- lines at the top"),
        ("frontmatter.syntax", "attachments", "every frontmatter line fits the accepted grammar"),
        ("frontmatter.empty", "attachments", "no empty string, no key without a value"),
        ("string.control", "attachments", "no control or line-separator character in a string"),
        ("string.length", "attachments", "strings are at most 300 characters, title 200"),
        ("note.keys", "attachments", "the note has no key outside the contract's table"),
        ("note.required", "attachments", "every key the note's type requires is present"),
        ("note.omitted", "attachments", "keys the note's type or origin omits are absent"),
        ("note.types", "attachments", "each note key has its table type"),
        ("note.type", "attachments", "type is podcast or audio"),
        ("note.contract", "attachments", "contract is 1"),
        ("note.source", "attachments", "source is earmark"),
        ("note.origin", "attachments", "origin is apple_podcasts, upload or url, matching type"),
        ("note.date", "attachments", "date equals the filename's date"),
        ("note.filename-date", "attachments", "date is the publish date, else the shared_at date"),
        ("note.duration", "attachments", "duration is hh:mm:ss"),
        ("note.language", "attachments", "language is a BCP 47 tag"),
        ("note.source-url", "attachments", "source_url has no userinfo, query or fragment"),
        ("note.guid", "attachments", "guid has the form the note's type allows"),
        ("note.sha256-form", "attachments", "transcript_sha256 is 64 lower-case hex digits"),
        ("note.hash", "attachments", "transcript_sha256 is the sha256 of the body as sent"),
        ("note.tags", "attachments", "tags is exactly the type"),
        ("note.heading", "attachments", "the body starts with the heading and ## Transcript"),
        (
            "note.paragraphs",
            "attachments",
            "paragraphs are blank-line separated and start [hh:mm:ss]",
        ),
        (
            "instructions.keys",
            "attachments",
            "the instructions have exactly the contract's six keys",
        ),
        ("instructions.types", "attachments", "each instructions key has its type"),
        ("instructions.type", "attachments", "type is earmark-instructions"),
        ("instructions.contract", "attachments", "contract is 1"),
        ("instructions.for", "attachments", "for is the note's filename"),
        ("instructions.guid", "attachments", "guid equals the note's guid"),
        ("subject.prefix", "subject", "the subject starts with [earmark] "),
        ("subject.length", "subject", "the subject is at most 120 characters"),
        ("subject.control", "subject", "the subject has no control or line-separator character"),
        ("body.names", "body", "the body carries both attachment names"),
        ("body.job-id", "body", "the body carries the job_id"),
        ("body.third-party", "body", "the body says the subject and note hold third-party text"),
        ("body.no-guid", "body", "the guid stays out of the body"),
    ]
    return tuple(Rule(i, n, d) for i, n, d in table)


RULES: tuple[Rule, ...] = _rules()


@dataclasses.dataclass(frozen=True)
class Attachment:
    filename: str
    data: bytes
    media_type: str | None  # "text/markdown; charset=utf-8" style, or None if unknown


@dataclasses.dataclass(frozen=True)
class Mail:
    attachments: tuple[Attachment, ...]
    subject: str | None  # decoded; None: not given
    body: str | None  # the text/plain body; None: not given
    message_id: str | None  # message mode only
    raw: email.message.EmailMessage | None  # message mode only


@dataclasses.dataclass(frozen=True)
class Failure:
    rule: str
    file: str | None  # "note" | "instructions" | None
    message: str  # ASCII, never quotes text from the mail


@dataclasses.dataclass(frozen=True)
class Report:
    failures: tuple[Failure, ...]
    skipped: tuple[str, ...]  # rule ids not run because an input was not given
    summary: dict[str, str | None]


# ---------------------------------------------------------------------------------------
# Reading a message


def _decoded(part: email.message.Message) -> bytes:
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else b""


def _media_type(part: email.message.EmailMessage) -> str:
    media = part.get_content_type()
    charset = part.get_param("charset")
    if charset:
        media += "; charset=" + str(charset)
    return media


def _text_of(part: email.message.EmailMessage) -> str:
    charset = part.get_content_charset() or "ascii"
    try:
        return _decoded(part).decode(charset, "replace")
    except LookupError:
        return _decoded(part).decode("utf-8", "replace")


def read_message(data: bytes) -> Mail:
    """Parse an RFC 5322 message into the parts the rules need."""
    msg = email.message_from_bytes(data, policy=email.policy.default)
    if not isinstance(msg, email.message.EmailMessage):
        raise ValueError("the message did not parse")
    attachments: list[Attachment] = []
    body: str | None = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_content_disposition() == "attachment":
            attachments.append(
                Attachment(part.get_filename() or "", _decoded(part), _media_type(part))
            )
        elif body is None and part.get_content_type() == "text/plain":
            body = _text_of(part)
    subject = msg.get("Subject")
    message_id = msg.get("Message-ID")
    return Mail(
        attachments=tuple(attachments),
        subject="" if subject is None else str(subject),
        body="" if body is None else body,
        message_id=None if message_id is None else str(message_id),
        raw=msg,
    )


# ---------------------------------------------------------------------------------------
# The frontmatter subset earmark emits

_KEY = r"[a-z][a-z0-9_]*"
_KEY_LINE = re.compile(r"(" + _KEY + r"):(?: (.*))?")
_INT = re.compile(r"[0-9]+")
_DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_TIMESTAMP = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})"
)
_ESCAPES = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "\t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
    "L": chr(0x2028),
    "P": chr(0x2029),
}
_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}
_HEX = frozenset("0123456789abcdefABCDEF")
_STRING_CONTROL = frozenset({"Cc", "Zl", "Zp"})


def _valid_date(text: str) -> bool:
    match = _DATE.fullmatch(text)
    if match is None:
        return False
    try:
        dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return False
    return True


def _valid_timestamp(text: str) -> bool:
    match = _TIMESTAMP.fullmatch(text)
    if match is None:
        return False
    year, month, day, hour, minute, second = (int(match.group(n)) for n in range(1, 7))
    offset = match.group(7)
    try:
        dt.datetime(year, month, day, hour, minute, second, tzinfo=dt.timezone.utc)
        if offset != "Z":
            hours, minutes = int(offset[1:3]), int(offset[4:6])
            if hours > 23 or minutes > 59:
                return False
            sign = -1 if offset[0] == "-" else 1
            dt.timezone(sign * dt.timedelta(hours=hours, minutes=minutes))
    except ValueError:
        return False
    return True


def _scan_quoted(text: str) -> str | None:
    """The value of a YAML double-quoted scalar filling ``text``, or None if it is malformed."""
    if not text.startswith('"'):
        return None
    out: list[str] = []
    i = 1
    while i < len(text):
        ch = text[i]
        i += 1
        if ch == '"':
            return "".join(out) if i == len(text) else None
        if ch != "\\":
            if unicodedata.category(ch) == "Cc":
                return None
            out.append(ch)
            continue
        if i >= len(text):
            return None
        esc = text[i]
        i += 1
        if esc in _HEX_ESCAPES:
            width = _HEX_ESCAPES[esc]
            digits = text[i : i + width]
            if len(digits) != width or not all(c in _HEX for c in digits):
                return None
            i += width
            code = int(digits, 16)
            if 0xD800 <= code <= 0xDFFF or code > 0x10FFFF:
                return None
            out.append(chr(code))
        elif esc in _ESCAPES:
            out.append(_ESCAPES[esc])
        else:
            return None
    return None  # no closing quote


@dataclasses.dataclass
class _Field:
    kind: str  # string | int | date | timestamp | list | bad
    text: str = ""
    items: tuple[str, ...] = ()


@dataclasses.dataclass
class _Front:
    fields: dict[str, _Field]
    syntax_error: bool
    empty: bool


def _parse_front(lines: Sequence[str]) -> _Front:
    fields: dict[str, _Field] = {}
    syntax = False
    empty = False
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        match = _KEY_LINE.fullmatch(line)
        if match is None:
            syntax = True
            continue
        key, value = match.group(1), match.group(2)
        if key in fields:
            syntax = True
            continue
        if value is None:
            items: list[str] = []
            while i < len(lines) and lines[i].startswith("  - "):
                item = _scan_quoted(lines[i][4:])
                if item is None:
                    syntax = True
                else:
                    items.append(item)
                i += 1
            if not items:
                empty = True
            fields[key] = _Field("list", items=tuple(items))
        elif value.startswith('"'):
            text = _scan_quoted(value)
            if text is None:
                syntax = True
                fields[key] = _Field("bad")
            else:
                empty = empty or text == ""
                fields[key] = _Field("string", text)
        elif _INT.fullmatch(value):
            fields[key] = _Field("int", value)
        elif _valid_date(value):
            fields[key] = _Field("date", value)
        elif _valid_timestamp(value):
            fields[key] = _Field("timestamp", value)
        else:
            syntax = True
            fields[key] = _Field("bad")
    return _Front(fields, syntax, empty)


def _split_fence(text: str) -> tuple[list[str], str] | None:
    """The frontmatter lines and the body after the closing fence, or None."""
    lines = text.split("\n")
    if lines[0] != "---":
        return None
    for index in range(1, len(lines) - 1):
        if lines[index] == "---":
            body = "\n".join(lines[index + 1 :])
            return lines[1:index], body
    return None


# ---------------------------------------------------------------------------------------
# The rules

_NOTE_TYPES = {
    "type": "string",
    "contract": "int",
    "source": "string",
    "origin": "string",
    "title": "string",
    "show": "string",
    "date": "date",
    "published": "date",
    "duration": "string",
    "language": "string",
    "apple_url": "string",
    "source_url": "string",
    "guid": "string",
    "shared_at": "timestamp",
    "transcript_sha256": "string",
    "tags": "list",
}
_NOTE_REQUIRED = (
    "type",
    "contract",
    "source",
    "origin",
    "title",
    "date",
    "duration",
    "guid",
    "shared_at",
    "transcript_sha256",
    "tags",
)
_INSTRUCTIONS_TYPES = {
    "type": "string",
    "contract": "int",
    "for": "string",
    "guid": "string",
    "job_id": "string",
    "shared_at": "timestamp",
}
_NOTE_NAME = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md")
_INSTRUCTIONS_SUFFIX = ".instructions.md"
_DURATION = re.compile(r"[0-9]{2,}:[0-5][0-9]:[0-5][0-9]")
_LANGUAGE = re.compile(r"[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PARAGRAPH = re.compile(r"\[[0-9]{2,}:[0-5][0-9]:[0-5][0-9]\] .")
_GENERIC = frozenset(
    {
        "file.utf8",
        "frontmatter.fence",
        "frontmatter.syntax",
        "frontmatter.empty",
        "string.control",
        "string.length",
    }
)


class _Ctx:
    def __init__(self) -> None:
        self.failures: list[Failure] = []

    def fail(self, rule: str, file: str | None, message: str) -> None:
        self.failures.append(Failure(rule, file, message))


class _File:
    """One attachment, read as far as it can be read."""

    def __init__(self, role: str, attachment: Attachment) -> None:
        self.role = role
        self.name = attachment.filename
        self.data = attachment.data
        self.fields: dict[str, _Field] | None = None
        self.body: str | None = None
        self.body_bytes: bytes | None = None

    def string(self, key: str) -> str | None:
        field = self.fields.get(key) if self.fields is not None else None
        return field.text if field is not None and field.kind == "string" else None

    def kind(self, key: str) -> str | None:
        field = self.fields.get(key) if self.fields is not None else None
        return None if field is None else field.kind

    def scalar(self, key: str, kind: str) -> str | None:
        field = self.fields.get(key) if self.fields is not None else None
        return field.text if field is not None and field.kind == kind else None


def _is_control(text: str) -> bool:
    return any(unicodedata.category(ch) in _STRING_CONTROL for ch in text)


def _read_file(ctx: _Ctx, role: str, attachment: Attachment) -> _File:
    file = _File(role, attachment)
    data = attachment.data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        ctx.fail("file.utf8", role, "the file is not valid UTF-8")
        return file
    if text.startswith("﻿"):
        ctx.fail("file.bom", role, "the file starts with a byte order mark")
        text = text[1:]
    if b"\r" in data:
        ctx.fail("file.lf", role, "the file has a carriage return; line endings must be LF")
    fenced = _split_fence(text)
    if fenced is None:
        ctx.fail("frontmatter.fence", role, "the file has no frontmatter between --- lines")
        return file
    lines, body = fenced
    front = _parse_front(lines)
    file.fields = front.fields
    file.body = body
    file.body_bytes = body.encode("utf-8")
    if front.syntax_error:
        ctx.fail("frontmatter.syntax", role, "a frontmatter line is outside the accepted grammar")
    if front.empty:
        ctx.fail("frontmatter.empty", role, "a frontmatter value is empty")
    strings: list[tuple[str, str]] = []
    for key, field in front.fields.items():
        if field.kind == "string":
            strings.append((key, field.text))
        elif field.kind == "list":
            strings.extend((key, item) for item in field.items)
    if any(_is_control(value) for _, value in strings):
        ctx.fail("string.control", role, "a string holds a control or line-separator character")
    for key, value in strings:
        if len(value) > (200 if key == "title" and role == "note" else 300):
            ctx.fail("string.length", role, "a string is over its length cap: " + key[:40])
    return file


def _identify(attachments: Sequence[Attachment]) -> list[tuple[str, Attachment]]:
    """Tag the first two attachments as note and instructions, by name else by order."""
    chosen = list(attachments[:2])
    named = [a for a in attachments if a.filename.endswith(_INSTRUCTIONS_SUFFIX)]
    if len(named) == 1:
        others = [a for a in attachments if a is not named[0]]
        chosen = [*others[:1], named[0]]
    roles = ["note", "instructions"]
    return [(roles[n], a) for n, a in enumerate(chosen)]


def _note_name_parts(name: str) -> tuple[str, str] | None:
    match = _NOTE_NAME.fullmatch(name)
    if match is None or not _valid_date(match.group(1)) or len(match.group(2)) > 80:
        return None
    return match.group(1), match.group(2)


def _check_names(ctx: _Ctx, mail: Mail, note: _File | None, instr: _File | None) -> None:
    named = [a for a in mail.attachments if a.filename.endswith(_INSTRUCTIONS_SUFFIX)]
    if len(named) != 1:
        ctx.fail(
            "attachment.names", None, "exactly one attachment name must end in .instructions.md"
        )
    if note is not None and _note_name_parts(note.name) is None:
        ctx.fail("filename.note", "note", "the note's file name is not YYYY-MM-DD-<slug>.md")
    if note is not None and instr is not None:
        stem = note.name[:-3] if note.name.endswith(".md") else note.name
        if instr.name != stem + _INSTRUCTIONS_SUFFIX:
            ctx.fail(
                "filename.instructions",
                "instructions",
                "the instructions' name is not the note's stem plus .instructions.md",
            )


def _check_types(ctx: _Ctx, file: _File, table: dict[str, str], rule: str) -> bool:
    ok = True
    for key, field in (file.fields or {}).items():
        want = table.get(key)
        if want is not None and field.kind != want:
            ok = False
            ctx.fail(rule, file.role, "a key has the wrong type: " + key[:40])
    return ok


def _check_note(ctx: _Ctx, note: _File) -> None:
    if note.fields is None:
        return
    fields = note.fields
    for key in fields:
        if key not in _NOTE_TYPES:
            ctx.fail("note.keys", "note", "a key is not in the contract: " + key[:40])
    kind = note.string("type")
    for key in _NOTE_REQUIRED + (("show",) if kind == "podcast" else ()):
        if key not in fields:
            ctx.fail("note.required", "note", "a required key is missing: " + key)
    origin = note.string("origin")
    if kind == "audio":
        for key in ("show", "apple_url"):
            if key in fields:
                ctx.fail("note.omitted", "note", "audio omits this key: " + key)
    if "source_url" in fields and origin != "url":
        ctx.fail("note.omitted", "note", "source_url belongs to origin url only")
    if kind == "podcast" and "source_url" in fields and origin == "url":
        ctx.fail("note.omitted", "note", "a podcast omits source_url")
    _check_types(ctx, note, _NOTE_TYPES, "note.types")
    if kind is not None and kind not in ("podcast", "audio"):
        ctx.fail("note.type", "note", "type is neither podcast nor audio")
    if note.scalar("contract", "int") not in (None, "1"):
        ctx.fail("note.contract", "note", "contract is not 1")
    source = note.string("source")
    if source is not None and source != "earmark":
        ctx.fail("note.source", "note", "source is not earmark")
    if origin is not None:
        wrong = origin not in ("apple_podcasts", "upload", "url")
        if kind in ("podcast", "audio") and (origin == "apple_podcasts") != (kind == "podcast"):
            wrong = True
        if wrong:
            ctx.fail("note.origin", "note", "origin is not valid for this type")
    parts = _note_name_parts(note.name)
    date = note.scalar("date", "date")
    if parts is not None and date is not None and date != parts[0]:
        ctx.fail("note.date", "note", "date differs from the file name's date")
    if date is not None:
        published = note.scalar("published", "date")
        shared = note.scalar("shared_at", "timestamp")
        expected = published if published is not None else (shared[:10] if shared else None)
        if expected is not None and date != expected:
            ctx.fail("note.filename-date", "note", "date is not the publish or shared_at date")
    duration = note.string("duration")
    if duration is not None and not _DURATION.fullmatch(duration):
        ctx.fail("note.duration", "note", "duration is not hh:mm:ss")
    language = note.string("language")
    if language is not None and not _LANGUAGE.fullmatch(language):
        ctx.fail("note.language", "note", "language is not a BCP 47 tag")
    source_url = note.string("source_url")
    if source_url is not None:
        userinfo = "@" in urllib.parse.urlsplit(source_url).netloc
        if userinfo or "?" in source_url or "#" in source_url:
            ctx.fail("note.source-url", "note", "source_url has userinfo, a query or a fragment")
    guid = note.string("guid")
    if guid is not None and kind in ("podcast", "audio"):
        if kind == "podcast":
            good = (guid.startswith("feed:") and len(guid) > 5) or bool(
                re.fullmatch(r"apple:[0-9]+", guid)
            )
        else:
            good = bool(re.fullmatch(r"sha256:[0-9a-f]{64}", guid))
        if not good:
            ctx.fail("note.guid", "note", "guid does not have the form this type allows")
    digest = note.string("transcript_sha256")
    if digest is not None:
        if not _SHA256.fullmatch(digest):
            ctx.fail("note.sha256-form", "note", "transcript_sha256 is not 64 lower-case hex")
        elif note.body_bytes is not None and hashlib.sha256(note.body_bytes).hexdigest() != digest:
            ctx.fail("note.hash", "note", "transcript_sha256 does not match the note's body")
    tags = fields.get("tags")
    if tags is not None and tags.kind == "list" and kind is not None and tags.items != (kind,):
        ctx.fail("note.tags", "note", "tags is not exactly the type")
    _check_body(ctx, note, kind)


def _check_body(ctx: _Ctx, note: _File, kind: str | None) -> None:
    title = note.string("title")
    show = note.string("show")
    body = note.body
    if body is None or title is None:
        return
    if kind == "podcast":
        if show is None:
            return
        head = "# " + show + " — " + title + "\n\n## Transcript\n\n"
    else:
        head = "# " + title + "\n\n## Transcript\n\n"
    if not body.startswith(head):
        ctx.fail("note.heading", "note", "the body does not start with the contract's heading")
        return
    rest = body[len(head) :]
    if rest.endswith("\n"):
        rest = rest[:-1]
    if rest and any(_PARAGRAPH.match(block) is None for block in rest.split("\n\n")):
        ctx.fail("note.paragraphs", "note", "a paragraph is empty or lacks its [hh:mm:ss] marker")


def _check_instructions(ctx: _Ctx, instr: _File, note: _File | None) -> None:
    if instr.fields is None:
        return
    fields = instr.fields
    if set(fields) != set(_INSTRUCTIONS_TYPES):
        ctx.fail("instructions.keys", "instructions", "the keys are not exactly the contract's six")
    _check_types(ctx, instr, _INSTRUCTIONS_TYPES, "instructions.types")
    kind = instr.string("type")
    if kind is not None and kind != "earmark-instructions":
        ctx.fail("instructions.type", "instructions", "type is not earmark-instructions")
    if instr.scalar("contract", "int") not in (None, "1"):
        ctx.fail("instructions.contract", "instructions", "contract is not 1")
    if note is None:
        return
    target = instr.string("for")
    if target is not None and target != note.name:
        ctx.fail("instructions.for", "instructions", "for is not the note's file name")
    guid, note_guid = instr.string("guid"), note.string("guid")
    if guid is not None and note_guid is not None and guid != note_guid:
        ctx.fail("instructions.guid", "instructions", "guid differs from the note's guid")


def _check_media_types(ctx: _Ctx, attachments: Sequence[Attachment]) -> None:
    for attachment in attachments:
        pieces = [p.strip() for p in (attachment.media_type or "").split(";")]
        good = pieces[0].lower() == "text/markdown"
        for param in pieces[1:]:
            name, _, value = param.partition("=")
            if name.strip().lower() == "charset" and value.strip().strip('"').lower() != "utf-8":
                good = False
        if not good:
            ctx.fail("attachment.media-type", None, "an attachment is not text/markdown in UTF-8")


def _check_structure(ctx: _Ctx, raw: email.message.EmailMessage) -> None:
    parts = list(raw.walk())
    leaves = [p for p in parts if not p.is_multipart()]
    inline = [
        p
        for p in leaves
        if p.get_content_type() == "text/plain" and p.get_content_disposition() != "attachment"
    ]
    attached = [p for p in leaves if p.get_content_disposition() == "attachment"]
    good = (
        raw.get_content_type() == "multipart/mixed"
        and len(parts) == len(leaves) + 1
        and len(leaves) == 3
        and len(inline) == 1
        and len(attached) == 2
    )
    if not good:
        ctx.fail("message.structure", None, "the message is not one text body plus two attachments")


def _check_defects(ctx: _Ctx, raw: email.message.EmailMessage) -> None:
    found = False
    for part in raw.walk():
        _decoded(part)  # defects of a body appear only once it is decoded
    for part in raw.walk():
        if part.defects:
            found = True
        for value in part.values():
            if getattr(value, "defects", ()):
                found = True
    if found:
        ctx.fail("message.defects", None, "the message or one of its parts has parser defects")


def _check_text(ctx: _Ctx, mail: Mail, note: _File | None, instr: _File | None) -> None:
    subject = mail.subject
    if subject is not None:
        if not subject.startswith("[earmark] "):
            ctx.fail("subject.prefix", None, "the subject does not start with [earmark] ")
        if len(subject) > 120:
            ctx.fail("subject.length", None, "the subject is over 120 characters")
        if _is_control(subject):
            ctx.fail("subject.control", None, "the subject holds a control character")
    body = mail.body
    if body is None:
        return
    names = [a.filename for a in mail.attachments]
    if not names or any(name not in body for name in names):
        ctx.fail("body.names", None, "the body does not carry both attachment names")
    job_id = instr.string("job_id") if instr is not None else None
    if job_id and job_id not in body:
        ctx.fail("body.job-id", None, "the body does not carry the job_id")
    if "third-party" not in body.lower():
        ctx.fail("body.third-party", None, "the body does not mention third-party text")
    guid = note.string("guid") if note is not None else None
    if guid and guid in body:
        ctx.fail("body.no-guid", None, "the body carries the guid")


def _skipped(mail: Mail) -> tuple[str, ...]:
    missing = set()
    if mail.raw is None:
        missing.add("message")
    if any(a.media_type is None for a in mail.attachments):
        missing.add("media-type")
    if mail.subject is None:
        missing.add("subject")
    if mail.body is None:
        missing.add("body")
    return tuple(r.id for r in RULES if r.needs in missing)


_SUMMARY_RULES: dict[str, tuple[str, tuple[str, ...]]] = {
    "type": ("note", ("note.types", "note.type")),
    "guid": ("note", ("note.types", "note.guid", "instructions.types", "instructions.guid")),
    "transcript_sha256": ("note", ("note.types", "note.sha256-form", "note.hash")),
    "job_id": ("instructions", ("instructions.keys", "instructions.types")),
    "note": ("note", ("attachment.names", "filename.note")),
    "instructions": ("instructions", ("attachment.names", "filename.instructions")),
}


def _summary(
    mail: Mail, failures: Sequence[Failure], note: _File | None, instr: _File | None
) -> dict[str, str | None]:
    failed: set[str] = {f.rule for f in failures}
    generic_failed = {(f.file, f.rule) for f in failures if f.rule in _GENERIC}

    def clean(role: str, rules: tuple[str, ...]) -> bool:
        if any(r in failed for r in rules):
            return False
        return not any(file == role for file, _ in generic_failed)

    def both(rules: tuple[str, ...]) -> bool:
        return clean("note", rules) and clean("instructions", rules)

    values: dict[str, str | None] = {
        "type": note.string("type") if note else None,
        "guid": note.string("guid") if note else None,
        "transcript_sha256": note.string("transcript_sha256") if note else None,
        "job_id": instr.string("job_id") if instr else None,
        "note": note.name if note else None,
        "instructions": instr.name if instr else None,
    }
    out: dict[str, str | None] = {}
    for key, value in values.items():
        role, rules = _SUMMARY_RULES[key]
        ok = both(rules) if key == "guid" else clean(role, rules)
        out[key] = value if ok and value else None
    message_id = mail.message_id
    printable = message_id is not None and re.fullmatch(r"[\x21-\x7e]+", message_id) is not None
    out["message_id"] = message_id if printable else None
    return out


def check(mail: Mail) -> Report:
    """Run every rule whose inputs the mail has."""
    ctx = _Ctx()
    if mail.raw is not None:
        _check_defects(ctx, mail.raw)
        _check_structure(ctx, mail.raw)
    if all(a.media_type is not None for a in mail.attachments):
        _check_media_types(ctx, mail.attachments)
    tagged = _identify(mail.attachments)
    files = {role: _read_file(ctx, role, a) for role, a in tagged}
    note, instr = files.get("note"), files.get("instructions")
    _check_names(ctx, mail, note, instr)
    if note is not None:
        _check_note(ctx, note)
    if instr is not None:
        _check_instructions(ctx, instr, note)
    _check_text(ctx, mail, note, instr)
    failures = tuple(ctx.failures)
    return Report(failures, _skipped(mail), _summary(mail, failures, note, instr))


# ---------------------------------------------------------------------------------------
# The command line


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        sys.stderr.write("check_mail.py: error: " + message + "\n")
        sys.exit(EXIT_USAGE)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="check_mail.py", description="Check an earmark mail, contract 1.")
    sub = parser.add_subparsers(dest="command", required=True)
    message = sub.add_parser("message", help="check a whole RFC 5322 message")
    message.add_argument("--json", action="store_true", help="print one JSON object")
    message.add_argument("path")
    files = sub.add_parser("files", help="check the parts a PKA's inbox tools give")
    files.add_argument("--json", action="store_true", help="print one JSON object")
    files.add_argument("--note", required=True, help="file holding the note's bytes")
    files.add_argument("--note-name", required=True, help="the note's attachment name")
    files.add_argument("--instructions", required=True, help="file holding the instructions")
    files.add_argument("--instructions-name", required=True, help="its attachment name")
    files.add_argument("--note-media-type", default=None)
    files.add_argument("--instructions-media-type", default=None)
    files.add_argument("--subject-file", default=None, help="UTF-8, one trailing newline dropped")
    files.add_argument("--body-file", default=None, help="the text/plain body, UTF-8")
    rules = sub.add_parser("rules", help="list what the checker checks")
    rules.add_argument("--json", action="store_true", help="print a JSON list")
    return parser


class _UnreadableError(Exception):
    pass


def _read_bytes(path: str) -> bytes:
    target = Path(path)
    try:
        if not target.is_file():
            raise _UnreadableError("check_mail.py: not a readable file: " + path)
        return target.read_bytes()
    except OSError:
        raise _UnreadableError("check_mail.py: not a readable file: " + path) from None


def _write(text: str) -> None:
    sys.stdout.write(text + "\n")


def _print_rules(as_json: bool) -> None:
    if as_json:
        listing = [{"rule": r.id, "needs": r.needs, "description": r.description} for r in RULES]
        _write(json.dumps(listing, ensure_ascii=True, sort_keys=True))
    else:
        for rule in RULES:
            _write(rule.id + "\t" + rule.description)


def _print_report(report: Report, as_json: bool) -> None:
    if as_json:
        document: dict[str, Any] = {
            "checker": CHECKER_NAME,
            "contract": CONTRACT,
            "ok": not report.failures,
            "failures": [
                {"rule": f.rule, "file": f.file, "message": f.message} for f in report.failures
            ],
            "skipped": list(report.skipped),
            "summary": report.summary,
        }
        _write(json.dumps(document, ensure_ascii=True, sort_keys=True))
        return
    for failure in report.failures:
        where = "" if failure.file is None else " [" + failure.file + "]"
        _write("FAIL " + failure.rule + where + ": " + failure.message)
    if report.failures:
        _write("FAILED: " + str(len(report.failures)) + " check(s)")
    else:
        _write("OK: earmark mail, contract " + str(CONTRACT))
    if report.skipped:
        _write("not checked (input not given): " + ", ".join(report.skipped))


def _load(args: argparse.Namespace) -> Mail:
    if args.command == "message":
        return read_message(_read_bytes(args.path))
    subject: str | None = None
    body: str | None = None
    if args.subject_file is not None:
        subject = _read_bytes(args.subject_file).decode("utf-8", "replace")
        if subject.endswith("\n"):
            subject = subject[:-1]
    if args.body_file is not None:
        body = _read_bytes(args.body_file).decode("utf-8", "replace")
    return Mail(
        attachments=(
            Attachment(args.note_name, _read_bytes(args.note), args.note_media_type),
            Attachment(
                args.instructions_name,
                _read_bytes(args.instructions),
                args.instructions_media_type,
            ),
        ),
        subject=subject,
        body=body,
        message_id=None,
        raw=None,
    )


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    if args.command == "rules":
        _print_rules(args.json)
        return EXIT_OK
    try:
        mail = _load(args)
    except _UnreadableError as exc:
        sys.stderr.write(str(exc) + "\n")
        return EXIT_PREREQUISITE
    except Exception:  # noqa: BLE001 -- a checker bug must never make mail look valid
        report = Report(
            (Failure("internal", None, "the checker failed reading its input"),), (), {}
        )
        _print_report(report, args.json)
        return EXIT_FAILED
    try:
        report = check(mail)
    except Exception as exc:  # noqa: BLE001 -- a checker bug must never make mail look valid
        message = "the checker failed: " + type(exc).__name__[:60]
        report = Report((Failure("internal", None, message),), (), {})
    _print_report(report, args.json)
    return EXIT_FAILED if report.failures else EXIT_OK
