"""Run the contract checker as a PKA would, and take sample emails apart and rebuild them."""

import base64
import dataclasses
import email
import email.policy
import functools
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Final, cast

ROOT: Final = Path(__file__).resolve().parents[1]
CHECKER: Final = ROOT / "plugins/earmark-pka/skills/earmark-receive/scripts/check_mail.py"
SCRIPTS: Final = CHECKER.parent
RECEIVER: Final = SCRIPTS / "receive.py"
_BOUNDARY: Final = "=_earmark_checker_test"


@dataclass(frozen=True)
class Interpreter:
    path: str
    version: tuple[int, int]

    @property
    def id(self) -> str:
        return f"py{self.version[0]}.{self.version[1]}"


@functools.cache
def _probe(path: str) -> Interpreter:
    code = "import sys; sys.stdout.write('%d.%d' % sys.version_info[:2])"
    out = subprocess.run(
        [path, "-I", "-c", code], capture_output=True, text=True, timeout=60, check=True
    ).stdout
    major, minor = out.split(".")
    return Interpreter(path, (int(major), int(minor)))


def interpreters() -> list[Interpreter]:
    """``sys.executable``, then the ``MAIL_CHECKER_PYTHONS`` interpreters, in order."""
    found = [_probe(sys.executable)]
    found.extend(
        _probe(path) for path in os.environ.get("MAIL_CHECKER_PYTHONS", "").split(":") if path
    )
    return found


def floor_interpreters() -> list[Interpreter]:
    return [i for i in interpreters() if i.version >= (3, 9)]


def old_interpreters() -> list[Interpreter]:
    return [i for i in interpreters() if i.version < (3, 9)]


def run_checker(python: Interpreter, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [python.path, "-I", str(CHECKER), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


def run_receiver(python: Interpreter, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [python.path, "-I", str(RECEIVER), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


def report(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    document = json.loads(lines[0])
    assert isinstance(document, dict)
    return cast(dict[str, Any], document)


@dataclass(frozen=True)
class Parts:
    """A message taken apart with the standard library's email package."""

    subject: str
    body: str
    note_name: str
    note: bytes
    instructions_name: str
    instructions: bytes
    note_media_type: str = "text/markdown; charset=utf-8"
    instructions_media_type: str = "text/markdown; charset=utf-8"
    headers: tuple[tuple[str, str], ...] = ()

    def with_note(self, change: Callable[[bytes], bytes]) -> "Parts":
        return dataclasses.replace(self, note=change(self.note))

    def with_instructions(self, change: Callable[[bytes], bytes]) -> "Parts":
        return dataclasses.replace(self, instructions=change(self.instructions))


def _media_type(part: EmailMessage) -> str:
    charset = part.get_content_charset()
    return part.get_content_type() + (f"; charset={charset}" if charset else "")


def split(data: bytes) -> Parts:
    msg = email.message_from_bytes(data, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)
    attachments = list(msg.iter_attachments())
    assert len(attachments) == 2
    note, instructions = attachments
    assert isinstance(note, EmailMessage)
    assert isinstance(instructions, EmailMessage)
    body = msg.get_body(preferencelist=("plain",))
    assert body is not None
    headers = tuple((name, str(msg[name])) for name in ("From", "To", "Date", "Message-ID"))
    return Parts(
        subject=str(msg["Subject"]),
        body=cast(str, body.get_content()).replace("\r\n", "\n"),
        note_name=note.get_filename() or "",
        note=cast(bytes, note.get_payload(decode=True)),
        instructions_name=instructions.get_filename() or "",
        instructions=cast(bytes, instructions.get_payload(decode=True)),
        note_media_type=_media_type(note),
        instructions_media_type=_media_type(instructions),
        headers=headers,
    )


def _attach(msg: EmailMessage, data: bytes, name: str, media_type: str) -> None:
    kind, _, rest = media_type.partition(";")
    maintype, _, subtype = kind.strip().partition("/")
    charset = rest.partition("=")[2].strip().strip('"')
    msg.add_attachment(
        data,
        maintype=maintype,
        subtype=subtype,
        filename=name,
        params={"charset": charset} if charset else None,
    )


def rebuild(
    parts: Parts,
    *,
    extra_attachments: tuple[tuple[str, bytes], ...] = (),
    extra_html: bool = False,
) -> bytes:
    """The message again, built as ``compose_mail`` builds it; ``rebuild(split(x))`` passes."""
    msg = EmailMessage(policy=email.policy.SMTP)
    for name, value in parts.headers:
        msg[name] = value
    msg["Subject"] = parts.subject
    msg.set_content(parts.body, cte="7bit")
    _attach(msg, parts.note, parts.note_name, parts.note_media_type)
    _attach(msg, parts.instructions, parts.instructions_name, parts.instructions_media_type)
    for name, data in extra_attachments:
        _attach(msg, data, name, "text/markdown; charset=utf-8")
    if extra_html:
        msg.add_attachment(b"<p>x</p>", maintype="text", subtype="html", disposition="inline")
    msg.set_boundary(_BOUNDARY)
    return msg.as_bytes(policy=email.policy.SMTP)


def write_parts(parts: Parts, directory: Path) -> list[str]:
    """Files-mode arguments for ``parts``, with every optional input."""
    note, instructions = directory / "note", directory / "instructions"
    subject, body = directory / "subject", directory / "body"
    note.write_bytes(parts.note)
    instructions.write_bytes(parts.instructions)
    subject.write_bytes(parts.subject.encode("utf-8") + b"\n")
    body.write_bytes(parts.body.encode("utf-8"))
    return [
        "--note",
        str(note),
        "--note-name",
        parts.note_name,
        "--instructions",
        str(instructions),
        "--instructions-name",
        parts.instructions_name,
        "--note-media-type",
        parts.note_media_type,
        "--instructions-media-type",
        parts.instructions_media_type,
        "--subject-file",
        str(subject),
        "--body-file",
        str(body),
    ]


def set_front(raw: bytes, key: str, line: str | None) -> bytes:
    """Replace the frontmatter entry for ``key`` by ``line``, add it, or drop it (None)."""
    head, sep, rest = raw.partition(b"\n---\n")
    assert sep, "no closing fence"
    lines = head.decode("utf-8").split("\n")
    out: list[str] = []
    replaced = False
    skipping = False
    for text in lines:
        if skipping and text.startswith("  - "):
            continue
        skipping = False
        if text.startswith(f"{key}:"):
            skipping = True
            if line is not None:
                out.append(line)
            replaced = True
            continue
        out.append(text)
    if not replaced and line is not None:
        out.append(line)
    return "\n".join(out).encode("utf-8") + sep + rest


def front_value(raw: bytes, key: str) -> str:
    match = re.search(rf"^{key}: (.*)$", raw.decode("utf-8"), re.MULTILINE)
    assert match is not None, key
    return match.group(1)


def body_of(note: bytes) -> bytes:
    return note.partition(b"\n---\n")[2]


def rehash(note: bytes) -> bytes:
    """Rewrite ``transcript_sha256`` to match the body."""
    digest = hashlib.sha256(body_of(note)).hexdigest()
    return set_front(note, "transcript_sha256", f'transcript_sha256: "{digest}"')


def change_body(note: bytes, old: str, new: str) -> bytes:
    """Replace text in the note's body and rehash."""
    head, sep, body = note.partition(b"\n---\n")
    assert old.encode() in body, old
    return rehash(head + sep + body.replace(old.encode(), new.encode(), 1))


def rename_note(parts: Parts, name: str) -> Parts:
    """A new note name, with the instructions name, ``for`` and the body following it."""
    stem = name[:-3] if name.endswith(".md") else name
    new_instructions = stem + ".instructions.md"
    instructions = set_front(parts.instructions, "for", f'for: "{name}"')
    body = parts.body.replace(parts.instructions_name, new_instructions)
    body = body.replace(parts.note_name, name)
    return dataclasses.replace(
        parts,
        note_name=name,
        instructions_name=new_instructions,
        instructions=instructions,
        body=body,
    )


def rename_instructions(parts: Parts, name: str) -> Parts:
    body = parts.body.replace(parts.instructions_name, name)
    return dataclasses.replace(parts, instructions_name=name, body=body)


def set_guid(parts: Parts, guid: str) -> Parts:
    """The same guid in the note and the instructions."""
    line = f'guid: "{guid}"'
    return dataclasses.replace(
        parts,
        note=set_front(parts.note, "guid", line),
        instructions=set_front(parts.instructions, "guid", line),
    )


def b64_corrupt(raw: bytes) -> bytes:
    """Three characters of the first base64 body replaced with ``@@@``."""
    start = raw.index(b"Content-Transfer-Encoding: base64")
    body = raw.index(b"\r\n\r\n", start) + 4
    return raw[: body + 10] + b"@@@" + raw[body + 13 :]


def encode_word(text: str) -> str:
    return "=?utf-8?b?" + base64.b64encode(text.encode()).decode() + "?="
