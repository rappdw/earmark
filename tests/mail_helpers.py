"""Parse a composed message the way a receiver would, for the delivery tests."""

import email
import email.policy
import hashlib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, cast

import yaml


@dataclass(frozen=True)
class ParsedMail:
    message: EmailMessage
    note_name: str
    note: bytes
    instructions_name: str
    instructions: bytes

    @property
    def text_part(self) -> str:
        body = self.message.get_body(preferencelist=("plain",))
        assert body is not None
        return cast(str, body.get_content()).replace("\r\n", "\n")


def parse_mail(data: bytes) -> ParsedMail:
    message = email.message_from_bytes(data, policy=email.policy.default)
    attachments = list(message.iter_attachments())
    assert len(attachments) == 2
    note, instructions = attachments
    for part in (note, instructions):
        assert part.get_content_type() == "text/markdown"
        assert part.get_content_charset() == "utf-8"
        assert part.get_content_disposition() == "attachment"
    return ParsedMail(
        message=message,
        note_name=note.get_filename() or "",
        note=cast(bytes, note.get_payload(decode=True)),
        instructions_name=instructions.get_filename() or "",
        instructions=cast(bytes, instructions.get_payload(decode=True)),
    )


def split_file(raw: bytes) -> tuple[dict[str, Any], bytes]:
    """The parsed frontmatter and the exact bytes after the closing ``---`` line."""
    assert raw.startswith(b"---\n")
    end = raw.index(b"\n---\n", 3)
    front = yaml.safe_load(raw[4 : end + 1])
    return front, raw[end + 5 :]


def assert_note_hash_verifies(raw: bytes) -> dict[str, Any]:
    front, body = split_file(raw)
    assert front["transcript_sha256"] == hashlib.sha256(body).hexdigest()
    return front
