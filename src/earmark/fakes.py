"""Deterministic, call-counting, failure-injectable fakes for every pipeline stage.

The cache-aware fakes really write their artifacts through ArtifactStore. Conformance to
the stage Protocols is structural and is checked in ``tests/test_stages.py``.
"""

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final, Literal

from earmark.artifacts import ArtifactStore, atomic_write_bytes, atomic_write_text
from earmark.digest.model import (
    DigestNote,
    DigestWriteResult,
    ThemeEntry,
    ThemeRecord,
    ThemeRequest,
)
from earmark.digest.prompts import DIGEST_SCHEMA_VERSION
from earmark.errors import PermanentError
from earmark.index.chunks import NotePaths
from earmark.index.search import SearchHit, SearchQuery, SearchResults
from earmark.models import (
    ANALYSIS_SCHEMA_VERSION,
    Analysis,
    Claim,
    Clock,
    Entity,
    Episode,
    Job,
    ResearchLead,
    Segment,
    StoreResult,
    Transcript,
    episode_key,
    prompt_hash,
)
from earmark.pipeline.stages import Backends
from earmark.vault.document import new_note

FAKE_NOW: Final = datetime(2026, 1, 1, tzinfo=UTC)


def fixed_clock(at: datetime = FAKE_NOW) -> Clock:
    def clock() -> datetime:
        return at

    return clock


_DEFAULT_CLOCK: Final = fixed_clock()


class FakeStage[C]:
    """Records every call, then raises the next queued failure, if any."""

    def __init__(self) -> None:
        self.calls: list[C] = []
        self._failures: list[Exception] = []

    def fail_next(self, exc: Exception, times: int = 1) -> None:
        if times < 1:
            raise ValueError("times must be >= 1")
        self._failures.extend([exc] * times)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def _enter(self, call: C) -> None:
        self.calls.append(call)
        if self._failures:
            raise self._failures.pop(0)


@dataclass(frozen=True)
class ResolveCall:
    url: str


@dataclass(frozen=True)
class FetchCall:
    episode: Episode


@dataclass(frozen=True)
class TranscribeCall:
    episode: Episode
    audio: Path


@dataclass(frozen=True)
class AnalyzeCall:
    episode: Episode
    transcript: Transcript
    prompt: str | None


@dataclass(frozen=True)
class StoreCall:
    episode: Episode
    transcript: Transcript
    analyses: tuple[Analysis, ...]
    shared_at: datetime


@dataclass(frozen=True)
class IndexCall:
    episode: Episode
    transcript: Transcript
    analyses: tuple[Analysis, ...]
    stored: StoreResult


@dataclass(frozen=True)
class NotifyCall:
    job: Job
    episode: Episode | None


class FakeResolver(FakeStage[ResolveCall]):
    def __init__(self, episodes: Mapping[str, Episode] | None = None) -> None:
        super().__init__()
        self._episodes = dict(episodes or {})

    def resolve(self, url: str) -> Episode:
        self._enter(ResolveCall(url))
        if url in self._episodes:
            return self._episodes[url]
        digest = hashlib.sha256(url.encode()).hexdigest()[:12]
        guid = f"urn:earmark-fake:{digest}"
        return Episode(
            key=episode_key("apple_podcasts", "1000000001", guid),
            source="apple_podcasts",
            show_id="1000000001",
            episode_id=digest,
            guid=guid,
            title=f"Fake episode {digest[:6]}",
            show_title="Fake Show",
            published_at=FAKE_NOW,
            duration_s=900.0,
            audio_url=f"https://fake.invalid/audio/{digest}.mp3",
            feed_url="https://fake.invalid/feed.xml",
            url=url,
            description="A synthetic episode for tests.",
        )


class FakeAudioFetcher(FakeStage[FetchCall]):
    def __init__(self, artifacts: ArtifactStore, payload: bytes = b"fake audio\n") -> None:
        super().__init__()
        self._artifacts = artifacts
        self._payload = payload
        self.computed = 0

    def fetch(self, episode: Episode) -> Path:
        self._enter(FetchCall(episode))
        existing = self._artifacts.find_audio(episode.key)
        if existing is not None:
            return existing
        path = self._artifacts.audio_path(episode.key, "mp3")
        atomic_write_bytes(path, self._payload)
        self.computed += 1
        return path


class FakeTranscriber(FakeStage[TranscribeCall]):
    def __init__(
        self,
        artifacts: ArtifactStore,
        segments: Sequence[Segment] | None = None,
        *,
        model: str = "fake-asr",
        language: str = "en",
        warnings: Sequence[str] = (),
    ) -> None:
        super().__init__()
        self._artifacts = artifacts
        self._segments = None if segments is None else tuple(segments)
        self.model = model
        self.language = language
        self.warnings = tuple(warnings)
        self.computed = 0

    def transcribe(self, episode: Episode, audio: Path) -> Transcript:
        self._enter(TranscribeCall(episode, audio))
        cached = self._artifacts.read_transcript(episode.key)
        if cached is not None:
            return cached
        if not audio.is_file():
            raise PermanentError(f"audio file missing: {audio.name}")
        segments = self._segments
        if segments is None:
            segments = (
                Segment(start=0.0, end=5.0, text=f"Welcome to {episode.show_title}."),
                Segment(start=5.0, end=10.0, text=f"Today: {episode.title}."),
                Segment(start=10.0, end=15.0, text="Thanks for listening."),
            )
        last_end = segments[-1].end if segments else 0.0
        transcript = Transcript(
            episode_key=episode.key,
            language=self.language,
            duration_s=max(episode.duration_s or 0.0, last_end, 0.0),
            model=self.model,
            segments=segments,
            warnings=self.warnings,
        )
        self._artifacts.write_transcript(transcript)
        self.computed += 1
        return transcript


class FakeAnalyzer(FakeStage[AnalyzeCall]):
    def __init__(
        self,
        artifacts: ArtifactStore,
        *,
        model: str = "fake-llm",
        clock: Clock = _DEFAULT_CLOCK,
        warnings: Sequence[str] = (),
    ) -> None:
        super().__init__()
        self._artifacts = artifacts
        self.model = model
        self.clock = clock
        self.warnings = tuple(warnings)
        self.computed = 0

    def analyze(self, episode: Episode, transcript: Transcript, prompt: str | None) -> Analysis:
        self._enter(AnalyzeCall(episode, transcript, prompt))
        h = prompt_hash(prompt, self.model)
        cached = self._artifacts.read_analysis(episode.key, h)
        if cached is not None:
            return cached
        t0 = transcript.segments[0].start if transcript.segments else None
        ts = (t0,) if t0 is not None else ()
        context = "Mentioned as an example."

        def entity(name: str) -> tuple[Entity, ...]:
            return (Entity(name=name, context=context, timestamps_s=ts),)

        analysis = Analysis(
            episode_key=episode.key,
            prompt=prompt,
            prompt_hash=h,
            model=self.model,
            schema_version=ANALYSIS_SCHEMA_VERSION,
            created_at=self.clock(),
            summary=f"Fake summary of {episode.title}.",
            key_claims=(Claim(text="The fake episode makes a claim.", timestamp_s=t0),),
            people=entity("Ada Lovelace"),
            papers=entity("On Computable Numbers"),
            companies=entity("Acme Corp"),
            tools=entity("Fake Tool"),
            topics=entity("Testing"),
            open_questions=("What did the fake episode leave out?",),
            research_leads=(
                ResearchLead(
                    text="Follow up on the fake claim.", rationale="It is a test.", timestamp_s=t0
                ),
            ),
            prompt_response=f"Fake response to: {prompt}" if prompt is not None else None,
            warnings=self.warnings,
        )
        self._artifacts.write_analysis(analysis)
        self.computed += 1
        return analysis


class FakeStore(FakeStage[StoreCall]):
    def __init__(self, vault_dir: Path | None = None, subdir: str = "Earmark") -> None:
        super().__init__()
        pure = PurePosixPath(subdir)
        if not subdir or pure.is_absolute() or "\\" in subdir or ".." in pure.parts:
            raise ValueError("subdir must be a relative POSIX path without '..'")
        self.vault_dir = vault_dir
        self.subdir = subdir
        self.digest_calls: list[DigestNote] = []

    def write_digest(self, note: DigestNote, /) -> DigestWriteResult:
        """Records the call and, with ``vault_dir``, writes the digest note whole."""
        self.digest_calls.append(note)
        path = f"{self.subdir}/Digests/{note.file_stem}.md"
        status: Literal["created", "updated", "unchanged"] = "unchanged"
        if self.vault_dir is not None:
            paths = {
                key: NotePaths(found.note_path, None)
                for key, found in self.locate_notes(note.episode_keys).items()
                if found.note_path is not None and (self.vault_dir / found.note_path).is_file()
            }
            target = self.vault_dir / path
            text = new_note(note.owned, note.seeds, note.render(paths))
            try:
                before: str | None = target.read_text(encoding="utf-8")
            except FileNotFoundError:
                before = None
            if before != text:
                atomic_write_text(target, text)
                status = "created" if before is None else "updated"
        return DigestWriteResult(note_path=path, status=status)

    def store(
        self,
        episode: Episode,
        transcript: Transcript,
        analyses: Sequence[Analysis],
        shared_at: datetime,
    ) -> StoreResult:
        self._enter(StoreCall(episode, transcript, tuple(analyses), shared_at))
        result = StoreResult(
            note_path=f"{self.subdir}/Podcasts/{episode.key}.md", transcript_note_path=None
        )
        if self.vault_dir is not None:
            newest_first = sorted(
                analyses, key=lambda a: (a.created_at, a.prompt_hash), reverse=True
            )
            parts = [f"# {episode.title}\n"]
            parts.extend(
                f"\n## {a.prompt or 'Default analysis'}\n\n{a.summary}\n" for a in newest_first
            )
            atomic_write_text(self.vault_dir / result.note_path, "".join(parts))
        return result

    def locate_notes(self, keys: Sequence[str], /) -> dict[str, NotePaths]:
        """The paths ``store`` returns, for every key."""
        return {k: NotePaths(f"{self.subdir}/Podcasts/{k}.md", None) for k in keys}


class FakeThemer(FakeStage[ThemeRequest]):
    """Satisfies ``Themer``: one theme per show, in order of the show's first lead."""

    model = "fake-themes"

    def themes(self, request: ThemeRequest, /) -> ThemeRecord:
        self._enter(request)
        by_show: dict[str, list[str]] = {}
        for lead in request.leads:
            by_show.setdefault(request.episodes[lead.episode_key].show_title, []).append(
                lead.lead_id
            )
        themes = tuple(
            ThemeEntry(title=show, summary=f"Research leads from {show}.", leads=tuple(ids))
            for show, ids in by_show.items()
        )
        return ThemeRecord(
            version=DIGEST_SCHEMA_VERSION,
            input_hash=request.input_hash,
            model=self.model,
            themes=themes,
            requests=0,
        )

    def close(self) -> None:
        return None


class FakeIndexer(FakeStage[IndexCall]):
    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, int] = {}

    def index(
        self,
        episode: Episode,
        transcript: Transcript,
        analyses: Sequence[Analysis],
        stored: StoreResult,
    ) -> None:
        self._enter(IndexCall(episode, transcript, tuple(analyses), stored))
        self.rows[episode.key] = 1 + len(analyses)


class FakeEmbedder:
    """Deterministic embeddings that rank by shared words. Satisfies ``Embedder``.

    Each token (``\\w+``, casefolded) adds +-1 at an index derived from its sha256; the
    vector is L2-normalised, and a text with no tokens is the unit vector on index 0.
    """

    def __init__(
        self, *, model: str = "fake-embed-v1", dim: int = 64, max_chars: int = 1000
    ) -> None:
        if dim < 1:
            raise ValueError("dim must be >= 1")
        self._model = model
        self._dim = dim
        self._max_chars = max_chars
        self.documents: list[str] = []  # every text embed_documents received
        self.queries: list[str] = []
        self._failures: list[Exception] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    def fail_next(self, exc: Exception, times: int = 1) -> None:
        if times < 1:
            raise ValueError("times must be >= 1")
        self._failures.extend([exc] * times)

    def fits(self, text: str, /) -> bool:
        return len(text) <= self._max_chars

    def _vector(self, text: str) -> list[float]:
        values = [0.0] * self._dim
        for token in re.findall(r"\w+", text.casefold()):
            digest = hashlib.sha256(token.encode()).digest()
            values[int.from_bytes(digest[:4]) % self._dim] += 1.0 if digest[4] & 1 == 0 else -1.0
        norm = math.sqrt(sum(v * v for v in values))
        if norm == 0.0:
            values[0] = 1.0
            return values
        return [v / norm for v in values]

    def _maybe_fail(self) -> None:
        if self._failures:
            raise self._failures.pop(0)

    def embed_documents(self, texts: Sequence[str], /) -> list[list[float]]:
        self._maybe_fail()
        self.documents.extend(texts)
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str, /) -> list[float]:
        self._maybe_fail()
        self.queries.append(text)
        return self._vector(text)


class FakeSearcher(FakeStage[SearchQuery]):
    def __init__(self, hits: Sequence[SearchHit] = (), *, index_exists: bool = True) -> None:
        super().__init__()
        self._hits = tuple(hits)
        self._index_exists = index_exists

    def search(self, query: SearchQuery, /) -> SearchResults:
        self._enter(query)
        return SearchResults(hits=self._hits[: query.limit], index_exists=self._index_exists)


class FakeNotifier(FakeStage[NotifyCall]):
    def notify(self, job: Job, episode: Episode | None) -> None:
        self._enter(NotifyCall(job, episode))


@dataclass
class FakeBackends:
    artifacts: ArtifactStore
    resolver: FakeResolver
    fetcher: FakeAudioFetcher
    transcriber: FakeTranscriber
    analyzer: FakeAnalyzer
    store: FakeStore
    indexer: FakeIndexer
    notifier: FakeNotifier

    def backends(self) -> Backends:
        return Backends(
            resolver=self.resolver,
            fetcher=self.fetcher,
            transcriber=self.transcriber,
            analyzer=self.analyzer,
            store=self.store,
            indexer=self.indexer,
            notifier=self.notifier,
        )


def make_fake_backends(
    data_dir: Path,
    *,
    vault_dir: Path | None = None,
    subdir: str = "Earmark",
    clock: Clock = _DEFAULT_CLOCK,
) -> FakeBackends:
    artifacts = ArtifactStore(data_dir)
    return FakeBackends(
        artifacts=artifacts,
        resolver=FakeResolver(),
        fetcher=FakeAudioFetcher(artifacts),
        transcriber=FakeTranscriber(artifacts),
        analyzer=FakeAnalyzer(artifacts, clock=clock),
        store=FakeStore(vault_dir, subdir),
        indexer=FakeIndexer(),
        notifier=FakeNotifier(),
    )
