"""The receiving skill's deterministic half: recognise, check, de-duplicate and file earmark mail.

``receive.py`` is the launcher; this module holds the work. It is standard library only and
written to Python 3.9, and it imports the contract checker that sits beside it. It reads only
the paths it is given, starts one process (``pka-tools version``), and writes only the note it
was asked about and ``.pka/earmark/``. Every result is one JSON line on stdout that never quotes
text from the mail.

Exit codes follow pka-tools: 0 ok, 1 refused or failed, 2 a prerequisite is missing, 3 the
workspace is not ready, 64 usage error.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, NoReturn

import earmark_mail_contract as contract

EXIT_OK, EXIT_REFUSED, EXIT_PREREQUISITE, EXIT_NOT_READY, EXIT_USAGE = 0, 1, 2, 3, 64
RECEIVER: Final = "earmark-receive"
MIN_PKA_TOOLS: Final = (1, 8, 0)
MAX_BYTES: Final = 16 * 1024 * 1024
HEAD_BYTES: Final = 64 * 1024
RECORD_VERSION: Final = 1
SKIPPED_TOP: Final = frozenset({"team-inbox", "owner-inbox"})
INSTRUCTIONS_SUFFIX: Final = ".instructions.md"
COMMANDS: Final = ("preflight", "classify", "file", "rewrite", "move", "annotate", "record")

_NAME = re.compile(r"[a-z0-9][a-z0-9.-]{0,119}")
_INBOX_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_CONTRACT_LINE = re.compile(r"contract: [0-9]+")
_TAG = re.compile(r"[a-z0-9][a-z0-9_/-]{0,62}")
_TIME = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]+))?(Z|[+-][0-9]{2}:[0-9]{2})"
)
_SAFE_PATH = re.compile(r"[A-Za-z0-9._/-]+")
_OUTCOMES_OK: Final = ("new", "duplicate", "reshare", "retranscription")
_OUTCOMES_REFUSED: Final = ("not-earmark", "unknown-contract", "invalid", "ambiguous")

MESSAGES: Final = {
    "internal": "The receiver failed unexpectedly.",
    "prerequisite": "A prerequisite is missing or an input file cannot be read.",
    "not-ready": "The workspace is not ready for the receiver.",
    "not-earmark": "The message is not shaped like earmark mail.",
    "unknown-contract": "The message uses a contract version this receiver does not know.",
    "invalid": "The message breaks the mail contract.",
    "ambiguous": "More than one note in the workspace has this guid.",
    "not-new": "The message is not new; nothing was filed.",
    "not-retranscription": "The message is not a re-transcription.",
    "not-filed": "The note is not filed yet.",
    "path-mismatch": "The path is not the filed note of this message.",
    "dest-refused": "The destination folder is not allowed.",
    "exists": "A file of that name already exists there.",
    "order-unknown": "The order of the two requests is unknown; the owner decides.",
    "older-body": "The filed body is from a newer request; it was kept.",
    "cr-in-note": "The filed note holds a carriage return and was not rewritten.",
    "no-sha-line": "The filed note has no single transcript hash line and was not rewritten.",
    "tags-not-a-list": "The note's tags are not a list of items.",
    "bad-tag": "A tag does not match the allowed pattern.",
    "bad-summary": "The summary is empty, too long or unreadable.",
    "nothing-to-do": "This delivery is already handled.",
}


class _Stop(Exception):  # noqa: N818 -- control flow, not an error condition
    """End the command with a result the caller prints."""

    def __init__(self, exit_code: int, error: str, extra: dict[str, Any] | None = None) -> None:
        super().__init__(error)
        self.exit_code = exit_code
        self.error = error
        self.extra = extra or {}


class _Usage(Exception):  # noqa: N818 -- control flow, not an error condition
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        sys.stderr.write(self.format_usage())
        sys.stderr.write(self.prog + ": error: " + message + "\n")
        sys.exit(EXIT_USAGE)


# ---------------------------------------------------------------------------------------
# Reading inputs


@dataclasses.dataclass
class _Input:
    names: list[str]
    datas: list[bytes]
    dispositions: list[str]
    oversize: list[bool]
    subject: str | None
    body: str | None
    inbox_id: str | None
    mail: contract.Mail | None  # --eml only


def _read_regular(path: str) -> bytes:
    target = Path(path)
    try:
        if not target.is_file() or target.stat().st_size > MAX_BYTES:
            raise _Stop(EXIT_PREREQUISITE, "prerequisite")
        return target.read_bytes()
    except OSError:
        raise _Stop(EXIT_PREREQUISITE, "prerequisite") from None


def _read_attachment(path: str) -> tuple[bytes, bool]:
    """The file's bytes, or (empty, True) when it is over the size cap."""
    target = Path(path)
    try:
        if not target.is_file():
            raise _Stop(EXIT_PREREQUISITE, "prerequisite")
        if target.stat().st_size > MAX_BYTES:
            return b"", True
        return target.read_bytes(), False
    except OSError:
        raise _Stop(EXIT_PREREQUISITE, "prerequisite") from None


def _check_usage(args: argparse.Namespace) -> None:
    if (args.eml is None) == (not args.attachment):
        raise _Usage("give either --eml or --attachment (once per attachment), not both")
    if args.command == "annotate" and not args.tag and args.summary_file is None:
        raise _Usage("annotate needs --tag or --summary-file")


def _load_input(args: argparse.Namespace) -> _Input:
    attachments: list[list[str]] | None = args.attachment
    if args.eml is not None:
        mail = contract.read_message(_read_regular(args.eml))
        return _Input(
            names=[a.filename for a in mail.attachments],
            datas=[a.data for a in mail.attachments],
            dispositions=["clean"] * len(mail.attachments),
            oversize=[len(a.data) > MAX_BYTES for a in mail.attachments],
            subject=mail.subject,
            body=mail.body,
            inbox_id=args.inbox_id,
            mail=mail,
        )
    names: list[str] = []
    datas: list[bytes] = []
    dispositions: list[str] = []
    oversize: list[bool] = []
    for name, path, disposition in attachments or []:
        data, big = _read_attachment(path)
        names.append(name)
        datas.append(data)
        dispositions.append(disposition)
        oversize.append(big)
    subject: str | None = None
    body: str | None = None
    if args.subject_file is not None:
        subject = _read_regular(args.subject_file).decode("utf-8", "replace")
        if subject.endswith("\n"):
            subject = subject[:-1]
    if args.body_file is not None:
        body = _read_regular(args.body_file).decode("utf-8", "replace")
    return _Input(names, datas, dispositions, oversize, subject, body, args.inbox_id, None)


# ---------------------------------------------------------------------------------------
# Recognising


def _front(text: str) -> list[str] | None:
    """The lines between the opening and the first closing ``---`` line, or None."""
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index] == "---":
            return lines[1:index]
    return None


def _front_bytes(data: bytes) -> tuple[list[bytes], int] | None:
    """The frontmatter lines of ``data`` and the index of its closing line, or None."""
    lines = data.split(b"\n")
    if not lines or lines[0] != b"---":
        return None
    for index in range(1, len(lines)):
        if lines[index] == b"---":
            return lines, index
    return None


def _only(front: list[str], prefix: str) -> str | None:
    found = [line for line in front if line.startswith(prefix)]
    return found[0] if len(found) == 1 else None


@dataclasses.dataclass
class _Recognised:
    note_index: int
    instructions_index: int
    note_front: list[str]
    instructions_front: list[str]
    instructions_text: str
    contract: int


def _recognise(inp: _Input) -> tuple[list[str], _Recognised | None]:
    reasons: list[str] = []
    count = len(inp.names)
    if count != 2:
        reasons.append("attachments.count")
    if any(d != "clean" for d in inp.dispositions):
        reasons.append("attachments.disposition")
    if any(inp.oversize):
        reasons.append("attachments.size")
    if count != 2:
        return reasons, None
    instructions_at = [i for i, n in enumerate(inp.names) if n.endswith(INSTRUCTIONS_SUFFIX)]
    instr = instructions_at[0] if len(instructions_at) == 1 else 0
    note = 1 - instr
    names_ok = len(instructions_at) == 1 and all(_NAME.fullmatch(n) for n in inp.names)
    names_ok = names_ok and inp.names[note].endswith(".md")
    names_ok = names_ok and inp.names[instr] == inp.names[note][:-3] + INSTRUCTIONS_SUFFIX
    if not names_ok:
        reasons.append("attachments.names")
    if reasons:
        return reasons, None
    return _markers(inp, note, instr)


def _decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _markers(inp: _Input, note: int, instr: int) -> tuple[list[str], _Recognised | None]:
    note_text = _decode(inp.datas[note])
    instr_text = _decode(inp.datas[instr])
    note_front = None if note_text is None else _front(note_text)
    instr_front = None if instr_text is None else _front(instr_text)
    note_contract = note_guid = instr_contract = instr_guid = None
    note_ok = note_front is not None and 'source: "earmark"' in note_front
    if note_front is not None:
        note_contract = _contract_line(note_front)
        note_guid = _only(note_front, "guid:")
    note_ok = note_ok and note_contract is not None and note_guid is not None
    instr_ok = instr_front is not None and 'type: "earmark-instructions"' in instr_front
    if instr_front is not None:
        instr_contract = _contract_line(instr_front)
        instr_guid = _only(instr_front, "guid:")
    instr_ok = instr_ok and instr_contract is not None and instr_guid is not None
    if note_ok:
        instr_ok = instr_ok and instr_contract == note_contract and instr_guid == note_guid
    reasons = (["note.marker"] if not note_ok else []) + (
        ["instructions.marker"] if not instr_ok else []
    )
    if reasons or note_front is None or instr_front is None or note_contract is None:
        return reasons, None
    return reasons, _Recognised(
        note_index=note,
        instructions_index=instr,
        note_front=note_front,
        instructions_front=instr_front,
        instructions_text=instr_text or "",
        contract=int(note_contract.split(" ")[1]),
    )


def _contract_line(front: list[str]) -> str | None:
    found = [line for line in front if _CONTRACT_LINE.fullmatch(line)]
    return found[0] if len(found) == 1 else None


# ---------------------------------------------------------------------------------------
# The workspace and its record


def _workspace(args: argparse.Namespace, *, record: bool = True) -> Path:
    ws = Path(args.workspace)
    if not (ws / ".pka").is_dir():
        raise _Stop(EXIT_NOT_READY, "not-ready")
    if record:
        folder = ws / ".pka" / "earmark"
        file = folder / "received.jsonl"
        if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
            raise _Stop(EXIT_NOT_READY, "not-ready")
        if file.is_symlink() or (file.exists() and not file.is_file()):
            raise _Stop(EXIT_NOT_READY, "not-ready")
    return ws


def _record_file(ws: Path) -> Path:
    return ws / ".pka" / "earmark" / "received.jsonl"


def _read_record(ws: Path) -> tuple[list[dict[str, Any]], int]:
    file = _record_file(ws)
    if not file.exists():
        return [], 0
    readable: list[dict[str, Any]] = []
    unreadable = 0
    for raw in file.read_bytes().split(b"\n"):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw.decode("utf-8"))
        except ValueError:
            unreadable += 1
            continue
        keys = ("guid", "job_id", "event", "path")
        if isinstance(obj, dict) and all(isinstance(obj.get(k), str) for k in keys):
            readable.append(obj)
        else:
            unreadable += 1
    return readable, unreadable


def _safe_rel(text: str) -> bool:
    if not text or text.startswith("/") or "\\" in text:
        return False
    if any(unicodedata.category(c) == "Cc" for c in text):
        return False
    return all(part not in ("", ".", "..") for part in text.split("/"))


def _head(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(HEAD_BYTES)


def _is_earmark_note(head: bytes, guid_line: str) -> bool:
    front = _front(head.decode("utf-8", "replace"))
    return front is not None and 'source: "earmark"' in front and guid_line in front


def _verified(ws: Path, rel: str, guid_line: str) -> bool:
    if not _safe_rel(rel):
        return False
    target = ws / rel
    try:
        if not stat.S_ISREG(target.lstat().st_mode):
            return False
        target.resolve().relative_to(ws.resolve())
        return _is_earmark_note(_head(target), guid_line)
    except (OSError, ValueError):
        return False


def _scan(ws: Path, guid_line: str) -> list[str]:
    """Workspace-relative paths of every earmark note with this guid line, sorted."""
    root = str(ws)
    found: list[str] = []
    for folder, dirs, files in os.walk(root, followlinks=False):
        top = folder == root
        dirs[:] = [d for d in dirs if not d.startswith(".") and not (top and d in SKIPPED_TOP)]
        for name in files:
            if not name.endswith(".md"):
                continue
            path = Path(folder) / name
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    continue
                if _is_earmark_note(_head(path), guid_line):
                    found.append(path.relative_to(ws).as_posix())
            except OSError:
                continue
    return sorted(found)


def _instant(text: str) -> dt.datetime | None:
    match = _TIME.fullmatch(text)
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(match.group(n)) for n in range(1, 7))
    micro = int((match.group(7) or "0")[:6].ljust(6, "0"))
    offset = match.group(8)
    try:
        if offset == "Z":
            zone = dt.timezone.utc
        else:
            sign = -1 if offset[0] == "-" else 1
            zone = dt.timezone(
                sign * dt.timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6]))
            )
        return dt.datetime(year, month, day, hour, minute, second, micro, tzinfo=zone)
    except ValueError:
        return None


def _later(newer: str, older: str) -> bool | None:
    a, b = _instant(newer), _instant(older)
    if a is None or b is None:
        return None
    return a > b


# ---------------------------------------------------------------------------------------
# Classifying


@dataclasses.dataclass
class _Verdict:
    outcome: str
    doc: dict[str, Any]
    note: bytes = b""
    guid: str = ""
    job_id: str = ""
    shared_at: str = ""
    sha: str = ""
    path: str | None = None
    rewrite_body: bool | None = None


def _classify(ws: Path, inp: _Input) -> _Verdict:
    reasons, found = _recognise(inp)
    if found is None:
        return _Verdict("not-earmark", {"outcome": "not-earmark", "reasons": reasons})
    if found.contract != 1:
        return _Verdict(
            "unknown-contract", {"outcome": "unknown-contract", "contract": found.contract}
        )
    if inp.mail is not None:
        mail = inp.mail
    else:
        attachments = tuple(contract.Attachment(n, d, None) for n, d in zip(inp.names, inp.datas))
        mail = contract.Mail(attachments, inp.subject, inp.body, None, None)
    report = contract.check(mail)
    base: dict[str, Any] = {
        "contract": 1,
        "failures": sorted({f.rule for f in report.failures}),
        "skipped": sorted(report.skipped),
    }
    if report.failures:
        return _Verdict("invalid", dict(base, outcome="invalid"))
    summary = report.summary
    guid, job_id, sha = summary["guid"], summary["job_id"], summary["transcript_sha256"]
    note_name, instr_name = summary["note"], summary["instructions"]
    if not (guid and job_id and sha and note_name and instr_name):
        raise _Stop(EXIT_REFUSED, "internal")
    shared = [line for line in found.instructions_front if line.startswith("shared_at: ")]
    shared_at = shared[0][len("shared_at: ") :] if len(shared) == 1 else ""
    body = found.instructions_text.split("\n")
    closing = body.index("---", 1)
    empty = "\n".join(body[closing + 1 :]).strip() == ""
    guid_line = _only(found.note_front, "guid:") or ""
    sha_line = _only(found.note_front, "transcript_sha256:") or ""
    records, unreadable = _read_record(ws)
    mine = [r for r in records if r["guid"] == guid]
    verdict = _Verdict(
        "new",
        dict(
            base,
            type=summary["type"],
            guid=guid,
            job_id=job_id,
            transcript_sha256=sha,
            shared_at=shared_at,
            note=note_name,
            instructions=instr_name,
            instructions_empty=empty,
            path=None,
            previously_filed=None,
            rewrite_body=None,
            matches=[],
            record={"lines": len(records), "unreadable": unreadable},
        ),
        note=inp.datas[found.note_index],
        guid=guid,
        job_id=job_id,
        shared_at=shared_at,
        sha=sha,
    )
    _decide(ws, verdict, mine, guid_line, sha_line)
    verdict.doc["outcome"] = verdict.outcome
    verdict.doc["path"] = verdict.path
    verdict.doc["rewrite_body"] = verdict.rewrite_body
    return verdict


def _decide(
    ws: Path, verdict: _Verdict, mine: list[dict[str, Any]], guid_line: str, sha_line: str
) -> None:
    last_path = mine[-1]["path"] if mine else None
    if any(r["event"] == "done" and r["job_id"] == verdict.job_id for r in mine):
        verdict.outcome = "duplicate"
        if last_path is not None and _verified(ws, last_path, guid_line):
            verdict.path = last_path
        return
    path: str | None = None
    if last_path is not None and _verified(ws, last_path, guid_line):
        path = last_path
    else:
        matches = _scan(ws, guid_line)
        if len(matches) > 1:
            verdict.outcome = "ambiguous"
            verdict.doc["matches"] = matches
            return
        path = matches[0] if matches else None
    if path is None:
        if last_path is not None:
            verdict.doc["previously_filed"] = (
                last_path if _SAFE_PATH.fullmatch(last_path) else "unreadable"
            )
        return
    verdict.path = path
    filed = (ws / path).read_bytes().decode("utf-8", "replace")
    filed_front = _front(filed) or []
    if _only(filed_front, "transcript_sha256:") == sha_line:
        verdict.outcome = "reshare"
        return
    verdict.outcome = "retranscription"
    bodies = [r for r in mine if r["event"] in ("filed", "rewritten")]
    stamp = bodies[-1].get("shared_at") if bodies else None
    if isinstance(stamp, str):
        verdict.rewrite_body = _later(verdict.shared_at, stamp)


# ---------------------------------------------------------------------------------------
# Output


def _emit(command: str, exit_code: int, doc: dict[str, Any]) -> int:
    out: dict[str, Any] = {"receiver": RECEIVER, "command": command, "ok": exit_code == 0}
    out.update(doc)
    if exit_code != 0 and "message" not in out:
        out["message"] = MESSAGES.get(str(out.get("error")), MESSAGES["internal"])
    sys.stdout.write(json.dumps(out, ensure_ascii=True, sort_keys=True) + "\n")
    return exit_code


# ---------------------------------------------------------------------------------------
# preflight


def _preflight(args: argparse.Namespace) -> dict[str, Any]:
    ws = _workspace(args, record=False)
    tool = ws / ".pka" / "bin" / "pka-tools"
    if not tool.is_file() or not os.access(str(tool), os.X_OK):
        raise _Stop(EXIT_PREREQUISITE, "prerequisite")
    try:
        done = subprocess.run(  # noqa: S603 -- a fixed argument list; no shell
            [str(tool), "version", "--json"],
            cwd=str(ws),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise _Stop(EXIT_PREREQUISITE, "prerequisite") from None
    if done.returncode == 3:
        raise _Stop(EXIT_NOT_READY, "not-ready")
    if done.returncode != 0:
        raise _Stop(EXIT_PREREQUISITE, "prerequisite")
    try:
        version = json.loads(done.stdout).get("version")
    except (ValueError, AttributeError):
        raise _Stop(EXIT_PREREQUISITE, "prerequisite") from None
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise _Stop(EXIT_PREREQUISITE, "prerequisite")
    numbers = tuple(int(n) for n in version.split("."))
    if numbers < MIN_PKA_TOOLS:
        raise _Stop(EXIT_PREREQUISITE, "prerequisite")
    return {
        "pka_tools": version,
        "minimum": ".".join(str(n) for n in MIN_PKA_TOOLS),
        "python": ".".join(str(n) for n in sys.version_info[:3]),
    }


# ---------------------------------------------------------------------------------------
# Writing


def _check_dest(ws: Path, dest: str) -> Path:
    parts = dest.split("/")
    refused = _Stop(EXIT_REFUSED, "dest-refused")
    if not _safe_rel(dest) or any(p.startswith(".") for p in parts) or parts[0] in SKIPPED_TOP:
        raise refused
    current = ws
    try:
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise refused
            if current.exists() and (not current.is_dir() or (current / "meeting.json").exists()):
                raise refused
        current.resolve().relative_to(ws.resolve())
    except (OSError, ValueError):
        raise refused from None
    return current


def _atomic_write(target: Path, data: bytes) -> None:
    mode = stat.S_IMODE(target.stat().st_mode)
    handle, name = tempfile.mkstemp(dir=str(target.parent), prefix=".earmark-")
    temp = Path(name)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        temp.chmod(mode)
        temp.replace(target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _append_record(
    ws: Path, event: str, verdict: _Verdict, inbox_id: str | None, path: str
) -> None:
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    obj = {
        "v": RECORD_VERSION,
        "at": now,
        "event": event,
        "guid": verdict.guid,
        "job_id": verdict.job_id,
        "message_id": inbox_id,
        "path": path,
        "shared_at": verdict.shared_at,
        "transcript_sha256": verdict.sha,
    }
    line = json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    file = _record_file(ws)
    file.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(file), flags, 0o644)
    try:
        os.write(fd, line.encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _gate(verdict: _Verdict, allowed: tuple[str, ...], wrong: str, path: str) -> None:
    outcome = verdict.outcome
    if outcome in _OUTCOMES_REFUSED:
        raise _Stop(EXIT_REFUSED, outcome)
    if outcome == "new":
        raise _Stop(EXIT_REFUSED, "not-filed")
    if outcome == "duplicate":
        raise _Stop(EXIT_REFUSED, "nothing-to-do")
    if verdict.path != path:
        raise _Stop(EXIT_REFUSED, "path-mismatch")
    if outcome not in allowed:
        raise _Stop(EXIT_REFUSED, wrong)


def _file(args: argparse.Namespace, ws: Path, inp: _Input, verdict: _Verdict) -> dict[str, Any]:
    if verdict.outcome != "new":
        raise _Stop(EXIT_REFUSED, "not-new")
    folder = _check_dest(ws, args.dest)
    name = verdict.doc["note"]
    target = folder / name
    rel = args.dest + "/" + name
    if target.exists() or target.is_symlink():
        raise _Stop(EXIT_REFUSED, "exists")
    if not args.dry_run:
        folder.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(
                str(target),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o644,
            )
        except FileExistsError:
            raise _Stop(EXIT_REFUSED, "exists") from None
        with os.fdopen(fd, "wb") as out:
            out.write(verdict.note)
            out.flush()
            os.fsync(out.fileno())
        _append_record(ws, "filed", verdict, inp.inbox_id, rel)
    return {"path": rel, "dry_run": bool(args.dry_run)}


def _rewrite(args: argparse.Namespace, ws: Path, inp: _Input, verdict: _Verdict) -> dict[str, Any]:
    _gate(verdict, ("retranscription",), "not-retranscription", args.path)
    if verdict.rewrite_body is None and not args.owner_approved:
        raise _Stop(EXIT_REFUSED, "order-unknown")
    if verdict.rewrite_body is False:
        raise _Stop(EXIT_REFUSED, "older-body")
    target = ws / args.path
    current = target.read_bytes()
    if b"\r" in current:
        raise _Stop(EXIT_REFUSED, "cr-in-note")
    filed = _front_bytes(current)
    incoming = _front_bytes(verdict.note)
    if filed is None or incoming is None:
        raise _Stop(EXIT_REFUSED, "no-sha-line")
    lines, close = filed
    marks = [i for i in range(1, close) if lines[i].startswith(b"transcript_sha256: ")]
    new_lines, new_close = incoming
    new_marks = [i for i in range(1, new_close) if new_lines[i].startswith(b"transcript_sha256: ")]
    if len(marks) != 1 or len(new_marks) != 1:
        raise _Stop(EXIT_REFUSED, "no-sha-line")
    head = list(lines[: close + 1])
    head[marks[0]] = new_lines[new_marks[0]]
    data = b"\n".join(head) + b"\n" + b"\n".join(new_lines[new_close + 1 :])
    if not args.dry_run:
        _atomic_write(target, data)
        _append_record(ws, "rewritten", verdict, inp.inbox_id, args.path)
    return {"path": args.path, "dry_run": bool(args.dry_run)}


def _move(args: argparse.Namespace, ws: Path, inp: _Input, verdict: _Verdict) -> dict[str, Any]:
    _gate(verdict, ("reshare", "retranscription"), "not-filed", args.path)
    folder = _check_dest(ws, args.dest)
    name = args.path.rsplit("/", 1)[-1]
    target = folder / name
    if target.exists() or target.is_symlink():
        raise _Stop(EXIT_REFUSED, "exists")
    rel = args.dest + "/" + name
    if not args.dry_run:
        folder.mkdir(parents=True, exist_ok=True)
        (ws / args.path).rename(target)
        _append_record(ws, "moved", verdict, inp.inbox_id, rel)
    return {"path": rel, "from": args.path, "dry_run": bool(args.dry_run)}


def _summary_text(path: str) -> str:
    try:
        text = _read_regular(path).decode("utf-8")
    except UnicodeDecodeError:
        raise _Stop(EXIT_REFUSED, "bad-summary") from None
    text = re.sub(r"\s+", " ", text)
    text = "".join(c for c in text if unicodedata.category(c) not in ("Cc", "Zl", "Zp"))
    text = text.strip()
    if not 1 <= len(text) <= 300:
        raise _Stop(EXIT_REFUSED, "bad-summary")
    return text


def _annotate(args: argparse.Namespace, ws: Path, inp: _Input, verdict: _Verdict) -> dict[str, Any]:
    _gate(verdict, ("reshare", "retranscription"), "not-filed", args.path)
    tags: list[str] = args.tag or []
    if any(_TAG.fullmatch(t) is None for t in tags):
        raise _Stop(EXIT_REFUSED, "bad-tag")
    summary = None if args.summary_file is None else _summary_text(args.summary_file)
    target = ws / args.path
    text = target.read_bytes().decode("utf-8")
    lines = text.split("\n")
    close = lines.index("---", 1)
    if tags:
        lines = _add_tags(lines, close, tags)
        close = lines.index("---", 1)
    if summary is not None:
        escaped = summary.replace("\\", "\\\\").replace('"', '\\"')
        entry = 'summary: "' + escaped + '"'
        at = [i for i in range(1, close) if lines[i].startswith("summary:")]
        if at:
            lines[at[0]] = entry
        else:
            lines.insert(close, entry)
    if not args.dry_run:
        _atomic_write(target, "\n".join(lines).encode("utf-8"))
    return {"path": args.path, "dry_run": bool(args.dry_run)}


def _add_tags(lines: list[str], close: int, tags: list[str]) -> list[str]:
    at = [i for i in range(1, close) if lines[i] == "tags:"]
    if len(at) != 1:
        raise _Stop(EXIT_REFUSED, "tags-not-a-list")
    last = at[0]
    items: list[str] = []
    while last + 1 < close and lines[last + 1].startswith("  - "):
        last += 1
        items.append(lines[last][4:].strip().strip('"'))
    if not items:
        raise _Stop(EXIT_REFUSED, "tags-not-a-list")
    out = list(lines)
    for tag in tags:
        if tag in items:
            continue
        items.append(tag)
        out.insert(last + 1, '  - "' + tag + '"')
        last += 1
    return out


def _record(args: argparse.Namespace, ws: Path, inp: _Input, verdict: _Verdict) -> dict[str, Any]:
    _gate(verdict, ("reshare", "retranscription"), "not-filed", args.path)
    if not args.dry_run:
        _append_record(ws, "done", verdict, inp.inbox_id, args.path)
    return {"path": args.path, "dry_run": bool(args.dry_run)}


_WRITES: Final[dict[str, Callable[..., dict[str, Any]]]] = {
    "file": _file,
    "rewrite": _rewrite,
    "move": _move,
    "annotate": _annotate,
    "record": _record,
}


# ---------------------------------------------------------------------------------------
# The command line


def _inbox_id(text: str) -> str:
    if _INBOX_ID.fullmatch(text) is None:
        raise argparse.ArgumentTypeError("not a valid inbox id")
    return text


def _parser() -> argparse.ArgumentParser:
    root = _Parser(prog="receive.py", description="Receive an earmark email.", allow_abbrev=False)
    sub = root.add_subparsers(dest="command", required=True, metavar="|".join(COMMANDS))
    helps = {
        "preflight": "check .pka/bin/pka-tools (needs 1.8.0 or newer)",
        "classify": "recognise, check and de-duplicate a message",
        "file": "write the note of a new message into a folder",
        "rewrite": "replace the body of a filed note by a re-transcription",
        "move": "move the filed note to another folder",
        "annotate": "add tags or a summary to the filed note",
        "record": "close this delivery in the record",
    }
    for name in COMMANDS:
        cmd = sub.add_parser(name, help=helps[name], allow_abbrev=False)
        cmd.add_argument("--workspace", required=True)
        if name == "preflight":
            continue
        cmd.add_argument("--attachment", nargs=3, action="append", metavar=("NAME", "FILE", "DISP"))
        cmd.add_argument("--eml")
        cmd.add_argument("--subject-file")
        cmd.add_argument("--body-file")
        cmd.add_argument("--inbox-id", type=_inbox_id)
        if name == "classify":
            continue
        cmd.add_argument("--dry-run", action="store_true")
        if name == "file":
            cmd.add_argument("--dest", required=True)
            continue
        cmd.add_argument("--path", required=True)
        if name == "rewrite":
            cmd.add_argument("--owner-approved", action="store_true")
        if name == "move":
            cmd.add_argument("--dest", required=True)
        if name == "annotate":
            cmd.add_argument("--tag", action="append")
            cmd.add_argument("--summary-file")
    return root


def _run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    if args.command == "preflight":
        return EXIT_OK, _preflight(args)
    _check_usage(args)
    ws = _workspace(args)
    inp = _load_input(args)
    verdict = _classify(ws, inp)
    if args.command == "classify":
        return (EXIT_OK if verdict.outcome in _OUTCOMES_OK else EXIT_REFUSED), dict(
            verdict.doc, error=None if verdict.outcome in _OUTCOMES_OK else verdict.outcome
        )
    return EXIT_OK, _WRITES[args.command](args, ws, inp, verdict)


def main(argv: list[str]) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        code, doc = _run(args)
    except _Usage as exc:
        parser.error(str(exc))
    except _Stop as stop:
        return _emit(args.command, stop.exit_code, dict(stop.extra, error=stop.error))
    except Exception:  # noqa: BLE001 -- a receiver bug must never look like a handled message
        return _emit(args.command, EXIT_REFUSED, {"error": "internal"})
    if doc.get("error") is None:
        doc.pop("error", None)
    return _emit(args.command, code, doc)
