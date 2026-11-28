"""Deterministic, call-counting, failure-injectable fakes for every pipeline stage.

The cache-aware fakes really write their artifacts through ArtifactStore. Conformance to
the stage Protocols is structural and is checked in ``tests/test_stages.py``.
"""

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from earmark.artifacts import ArtifactStore, atomic_write_bytes
from earmark.errors import PermanentError
from earmark.models import (
    Clock,
    DecodeOptions,
    Episode,
    Job,
    Segment,
    SummaryInput,
    SummaryResult,
    Transcript,
    episode_key,
)
from earmark.pipeline.stages import Backends, OutgoingMail
from earmark.transcribe.base import asr_profile, decoded_key_for, with_decode_options
from earmark.youtube.links import youtube_episode

FAKE_NOW: Final = datetime(2026, 1, 1, tzinfo=UTC)
FAKE_SENDER: Final = "earmark@example.org"  # the fake sends nothing; never used by smtp


def fixed_clock(at: datetime = FAKE_NOW) -> Clock:
    def clock() -> datetime:
        return at

    return clock


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
class DownloadCall:
    url: str


@dataclass(frozen=True)
class YoutubeResolveCall:
    video_id: str


@dataclass(frozen=True)
class YoutubeDownloadCall:
    video_id: str


@dataclass(frozen=True)
class TranscribeCall:
    episode: Episode
    audio: Path
    options: DecodeOptions | None = None


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
            feed_guid=guid,
        )


class FakeAudioFetcher(FakeStage[FetchCall | DownloadCall]):
    def __init__(
        self,
        artifacts: ArtifactStore,
        payload: bytes = b"fake audio\n",
        payloads: Mapping[str, bytes] | None = None,
    ) -> None:
        super().__init__()
        self._artifacts = artifacts
        self._payload = payload
        self._payloads = dict(payloads or {})
        self.computed = 0
        self.downloads = 0

    def download(self, url: str, destination: Callable[[str], Path]) -> Path:
        self._enter(DownloadCall(url))
        path = destination("mp3")
        atomic_write_bytes(path, self._payloads.get(url, self._payload))
        self.downloads += 1
        return path

    def fetch(self, episode: Episode) -> Path:
        self._enter(FetchCall(episode))
        existing = self._artifacts.find_audio(episode.key)
        if existing is not None:
            return existing
        path = self._artifacts.audio_path(episode.key, "mp3")
        atomic_write_bytes(path, self._payload)
        self.computed += 1
        return path


class FakeYoutubeSource(FakeStage[YoutubeResolveCall | YoutubeDownloadCall]):
    def __init__(
        self,
        payload: bytes = b"fake youtube audio\n",
        payloads: Mapping[str, bytes] | None = None,
        episodes: Mapping[str, Episode] | None = None,
    ) -> None:
        super().__init__()
        self._payload = payload
        self._payloads = dict(payloads or {})
        self._episodes = dict(episodes or {})
        self.downloads = 0

    def resolve(self, video_id: str) -> Episode:
        self._enter(YoutubeResolveCall(video_id))
        if video_id in self._episodes:
            return self._episodes[video_id]
        return youtube_episode(
            video_id,
            title=f"Fake video {video_id}",
            channel="Fake Channel",
            published_at=FAKE_NOW,
            published_date_only=False,
            duration_s=600.0,
        )

    def download(self, video_id: str, destination: Callable[[str], Path]) -> Path:
        self._enter(YoutubeDownloadCall(video_id))
        path = destination("webm")
        atomic_write_bytes(path, self._payloads.get(video_id, self._payload))
        self.downloads += 1
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

    @property
    def profile(self) -> str:
        return self.profile_for(None)

    def profile_for(self, options: DecodeOptions | None) -> str:
        return asr_profile(
            "fake",
            with_decode_options({"model": self.model, "language": self.language}, options),
        )

    def cached(self, episode: Episode, options: DecodeOptions | None = None) -> Transcript | None:
        if options is None:
            stored = self._artifacts.read_transcript(episode.key)
        else:
            stored = self._artifacts.read_decoded(decoded_key_for(episode, options), episode.key)
        if stored is None or stored.asr_profile != self.profile_for(options):
            return None
        return stored

    def transcribe(
        self, episode: Episode, audio: Path, options: DecodeOptions | None = None
    ) -> Transcript:
        self._enter(TranscribeCall(episode, audio, options))
        cached = self.cached(episode, options)
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
            if options is not None and options.initial_prompt is not None:
                h = hashlib.sha256(options.initial_prompt.encode("utf-8")).hexdigest()[:12]
                segments = (*segments, Segment(start=15.0, end=20.0, text=f"Prompt {h}."))
        last_end = segments[-1].end if segments else 0.0
        language = self.language if options is None else (options.language or self.language)
        transcript = Transcript(
            episode_key=episode.key,
            language=language,
            duration_s=max(episode.duration_s or 0.0, last_end, 0.0),
            model=self.model,
            segments=segments,
            warnings=self.warnings,
            asr_profile=self.profile_for(options),
        )
        if options is None:
            self._artifacts.write_transcript(transcript)
        else:
            self._artifacts.write_decoded(decoded_key_for(episode, options), transcript)
        self.computed += 1
        return transcript


class FakeMailer(FakeStage[OutgoingMail]):
    """Records each message; with ``outbox`` set also writes ``<job_id>.eml`` (mode 0600)."""

    def __init__(self, sender: str = FAKE_SENDER, outbox: Path | None = None) -> None:
        super().__init__()
        self.sender = sender
        self._outbox = outbox

    def send(self, mail: OutgoingMail) -> None:
        self._enter(mail)
        if self._outbox is not None:
            atomic_write_bytes(self._outbox / f"{mail.job_id}.eml", mail.data, mode=0o600)


class FakeNotifier(FakeStage[NotifyCall]):
    def notify(self, job: Job, episode: Episode | None) -> None:
        self._enter(NotifyCall(job, episode))


@dataclass(frozen=True)
class SummarizeCall:
    transcript_chars: int
    instructions_chars: int


class FakeSummarizer(FakeStage[SummarizeCall]):
    """Returns scripted text, or a line that never contains the input."""

    backend = "fake"

    def __init__(
        self,
        *,
        text: str | None = None,
        model: str = "fake-llm",
        finish_reason: str | None = "stop",
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        max_output_tokens: int = 8192,
    ) -> None:
        super().__init__()
        self._text = text
        self.model = model
        self._finish_reason = finish_reason
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens
        self.max_output_tokens = max_output_tokens
        self.computed = 0

    def summarize(self, request: SummaryInput) -> SummaryResult:
        self._enter(SummarizeCall(len(request.transcript), len(request.instructions)))
        self.computed += 1
        text = self._text
        if text is None:
            text = f"Summary of {len(request.transcript)} characters.\n"
        return SummaryResult(
            text=text,
            model=self.model,
            finish_reason=self._finish_reason,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
        )


@dataclass
class FakeBackends:
    artifacts: ArtifactStore
    resolver: FakeResolver
    fetcher: FakeAudioFetcher
    youtube: FakeYoutubeSource
    transcriber: FakeTranscriber
    mailer: FakeMailer
    notifier: FakeNotifier
    summarizer: FakeSummarizer

    def backends(self) -> Backends:
        return Backends(
            resolver=self.resolver,
            fetcher=self.fetcher,
            youtube=self.youtube,
            transcriber=self.transcriber,
            mailer=self.mailer,
            notifier=self.notifier,
            summarizer=self.summarizer,
        )


def make_fake_backends(data_dir: Path) -> FakeBackends:
    artifacts = ArtifactStore(data_dir)
    return FakeBackends(
        artifacts=artifacts,
        resolver=FakeResolver(),
        fetcher=FakeAudioFetcher(artifacts),
        youtube=FakeYoutubeSource(),
        transcriber=FakeTranscriber(artifacts),
        mailer=FakeMailer(),
        notifier=FakeNotifier(),
        summarizer=FakeSummarizer(),
    )
