"""Shared vocabulary: frozen pydantic models, key and hash functions, and type aliases.

Imports only the standard library and pydantic. Sequences are tuples (so models are
hashable and really immutable), datetimes are timezone-aware, and floats are finite.
"""

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

Source = Literal["apple_podcasts"]
JobKind = Literal["ingest", "reanalyze"]
JobStatus = Literal["queued", "running", "done", "failed", "skipped"]
Stage = Literal["resolve", "fetch", "transcribe", "analyze", "store", "index", "notify"]
Clock = Callable[[], datetime]

TERMINAL_STATUSES: Final[frozenset[JobStatus]] = frozenset({"done", "failed", "skipped"})
ANALYSIS_SCHEMA_VERSION: Final = 1
EPISODE_KEY_LENGTH: Final = 20
PROMPT_HASH_LENGTH: Final = 16

MAX_URL_CHARS: Final = 2048
MAX_PROMPT_CHARS: Final = 4000
INGEST_ERROR_TYPES: Final = frozenset(
    {"url_empty", "url_too_long", "prompt_too_long", "timestamp_out_of_range"}
)

EpisodeKey = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{20}$")]
PromptHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
JobId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]


def utc_now() -> datetime:
    """The default Clock."""
    return datetime.now(UTC)


def episode_key(source: str, show_id: str, guid: str) -> str:
    """Stable 20-hex-character key for an episode. The parts are not normalised."""
    if not source or not show_id or not guid:
        raise ValueError("source, show_id and guid must not be empty")
    if ":" in source or ":" in show_id:
        raise ValueError("source and show_id must not contain ':'")
    digest = hashlib.sha256(f"{source}:{show_id}:{guid}".encode()).hexdigest()
    return digest[:EPISODE_KEY_LENGTH]


def prompt_hash(
    prompt: str | None, model: str, schema_version: int = ANALYSIS_SCHEMA_VERSION
) -> str:
    """16-hex-character cache key of an analysis. ``None`` (default prompt) differs from ``""``."""
    if not model:
        raise ValueError("model must not be empty")
    if schema_version < 1:
        raise ValueError("schema_version must be >= 1")
    payload = json.dumps(
        {"model": model, "prompt": prompt, "schema_version": schema_version},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:PROMPT_HASH_LENGTH]


def is_episode_key(value: str) -> bool:
    return re.fullmatch("[0-9a-f]{20}", value) is not None


def is_prompt_hash(value: str) -> bool:
    return re.fullmatch("[0-9a-f]{16}", value) is not None


def format_hms(seconds: float) -> str:
    """``HH:MM:SS`` with the fraction floored and hours unbounded."""
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("seconds must be finite and non-negative")
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _check_relative_posix(path: str) -> str:
    pure = PurePosixPath(path)
    if not path or pure.is_absolute() or "\\" in path or ".." in pure.parts:
        raise ValueError("path must be a non-empty relative POSIX path without '..'")
    return path


def normalize_prompt(value: str | None) -> str | None:
    """Strip; blank -> None; raise ValueError if longer than MAX_PROMPT_CHARS."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt is too long (at most {MAX_PROMPT_CHARS} characters)")
    return value


_LONE_SURROGATE: Final = re.compile("[\ud800-\udfff]")


def scrub_surrogates(text: str) -> str:
    """Each lone surrogate (U+D800-U+DFFF) becomes U+FFFD; otherwise unchanged."""
    if _LONE_SURROGATE.search(text) is None:
        return text
    return _LONE_SURROGATE.sub("\ufffd", text)


def entity_key(name: str) -> str:
    """NFKC, casefold; '+'/'#' kept right after a letter, digit, or kept '+'/'#'; every
    other punctuation (P*) or symbol (S*) character becomes a space; whitespace collapsed."""
    text = unicodedata.normalize("NFKC", name).casefold()
    out: list[str] = []
    for c in text:
        prev = out[-1] if out else ""
        if c in "+#" and (prev.isalnum() or prev in {"+", "#"}):
            out.append(c)
        elif unicodedata.category(c)[0] in "PS":
            out.append(" ")
        else:
            out.append(c)
    return " ".join("".join(out).split())


def _scrub_value(value: object) -> object:
    if isinstance(value, str):
        return scrub_surrogates(value)
    if isinstance(value, tuple | list):
        cleaned = [_scrub_value(v) for v in value]
        if all(a is b for a, b in zip(cleaned, value, strict=True)):
            return value
        return type(value)(cleaned)
    return value


class Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def _scrub(cls, value: object) -> object:
        return _scrub_value(value)


class IngestRequest(Model):
    url: str
    prompt: str | None = None
    timestamp: AwareDatetime | None = None

    @field_validator("url", mode="after")
    @classmethod
    def _normalise_url(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise PydanticCustomError("url_empty", "url must not be empty")
        if len(v) > MAX_URL_CHARS:
            raise PydanticCustomError(
                "url_too_long", f"url is too long (at most {MAX_URL_CHARS} characters)"
            )
        return v

    @field_validator("prompt", mode="after")
    @classmethod
    def _normalise_prompt(cls, v: str | None) -> str | None:
        try:
            return normalize_prompt(v)
        except ValueError as exc:
            raise PydanticCustomError("prompt_too_long", str(exc)) from None

    @field_validator("timestamp", mode="after")
    @classmethod
    def _check_timestamp(cls, v: datetime | None) -> datetime | None:
        if v is not None:
            try:
                v.astimezone(UTC)
            except OverflowError:
                raise PydanticCustomError(
                    "timestamp_out_of_range", "timestamp is out of range"
                ) from None
        return v


class EpisodeRef(Model):
    source: Source
    show_id: str
    episode_id: str
    country: str
    url: str


class Episode(Model):
    key: EpisodeKey
    source: Source
    show_id: Annotated[str, Field(min_length=1)]
    episode_id: str
    guid: Annotated[str, Field(min_length=1)]
    title: str
    show_title: str
    published_at: AwareDatetime | None
    duration_s: Seconds | None
    audio_url: str
    feed_url: str | None
    url: str
    description: str = ""

    @field_validator("show_id")
    @classmethod
    def _no_colon(cls, value: str) -> str:
        if ":" in value:
            raise ValueError("show_id must not contain ':'")
        return value

    @model_validator(mode="after")
    def _key_matches(self) -> Self:
        if self.key != episode_key(self.source, self.show_id, self.guid):
            raise ValueError("key does not match source, show_id and guid")
        return self


class Segment(Model):
    start: Seconds
    end: Seconds
    text: str
    speaker: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end < self.start:
            raise ValueError("end must be >= start")
        return self


class Transcript(Model):
    episode_key: EpisodeKey
    language: str | None
    duration_s: Seconds
    model: str
    segments: tuple[Segment, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_text(self) -> str:
        """One ``[HH:MM:SS] text`` line per segment; the content of ``transcript.txt``."""
        lines: list[str] = []
        for seg in self.segments:
            text = " ".join(seg.text.split())
            marker = f"[{format_hms(seg.start)}]"
            lines.append(f"{marker} {text}" if text else marker)
        return "".join(line + "\n" for line in lines)


class Claim(Model):
    text: str
    timestamp_s: Seconds | None = None


class Entity(Model):
    name: Annotated[str, Field(min_length=1)]
    context: str = ""
    timestamps_s: tuple[Seconds, ...] = ()


class ResearchLead(Model):
    text: str
    rationale: str = ""
    timestamp_s: Seconds | None = None


class Analysis(Model):
    episode_key: EpisodeKey
    prompt: str | None
    prompt_hash: PromptHash
    model: str
    schema_version: Annotated[int, Field(ge=1)]
    created_at: AwareDatetime
    summary: str
    key_claims: tuple[Claim, ...] = ()
    people: tuple[Entity, ...] = ()
    papers: tuple[Entity, ...] = ()
    companies: tuple[Entity, ...] = ()
    tools: tuple[Entity, ...] = ()
    topics: tuple[Entity, ...] = ()
    open_questions: tuple[str, ...] = ()
    research_leads: tuple[ResearchLead, ...] = ()
    prompt_response: str | None = None
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _hash_matches(self) -> Self:
        if self.prompt_hash != prompt_hash(self.prompt, self.model, self.schema_version):
            raise ValueError("prompt_hash does not match prompt, model and schema_version")
        return self


class StoreResult(Model):
    note_path: str
    transcript_note_path: str | None = None
    warnings: tuple[str, ...] = ()

    @field_validator("note_path", "transcript_note_path")
    @classmethod
    def _relative(cls, value: str | None) -> str | None:
        return None if value is None else _check_relative_posix(value)


class ShareRecord(Model):
    """Content of ``shared.json``: the earliest time the episode was shared."""

    episode_key: EpisodeKey
    first_shared_at: AwareDatetime


class JobResult(Model):
    note_path: str | None = None
    transcript_note_path: str | None = None
    analysis_hash: PromptHash | None = None

    @field_validator("note_path", "transcript_note_path")
    @classmethod
    def _relative(cls, value: str | None) -> str | None:
        return None if value is None else _check_relative_posix(value)


class Job(Model):
    id: JobId
    kind: JobKind
    status: JobStatus
    stage: Stage | None = None
    url: str | None
    prompt: str | None
    episode_key: EpisodeKey | None = None
    attempts: int = Field(0, ge=0)
    run_after: AwareDatetime
    error: str | None = None
    warnings: tuple[str, ...] = ()
    result: JobResult | None = None
    shared_at: AwareDatetime
    created_at: AwareDatetime
    updated_at: AwareDatetime
    heartbeat_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _kind_requirements(self) -> Self:
        if self.kind == "ingest" and self.url is None:
            raise ValueError("an ingest job requires url")
        if self.kind == "reanalyze" and self.episode_key is None:
            raise ValueError("a reanalyze job requires episode_key")
        return self
