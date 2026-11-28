"""One Protocol per pipeline stage.

Every parameter is positional-only, so implementations may name them freely and the
runner must call stages positionally.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from earmark.models import DecodeOptions, Episode, Job, SummaryInput, SummaryResult, Transcript


class Resolver(Protocol):
    def resolve(self, url: str, /) -> Episode:
        """Turn a shared URL into an Episode. Not cached.

        Raises SkipEpisode when there is no public feed or the episode is subscriber-only.
        """
        ...


class AudioFetcher(Protocol):
    def fetch(self, episode: Episode, /) -> Path:
        """Return the local audio path under ``ArtifactStore.audio_path(episode.key, ext)``.

        If ``find_audio`` already finds a file, return it without a request.
        """
        ...

    def download(self, url: str, destination: Callable[[str], Path], /) -> Path:
        """Download a client-supplied direct audio URL to ``destination(ext)``; return the path.

        Always downloads (no cache). Errors follow ``fetch.py``.
        """
        ...


class YoutubeSource(Protocol):
    def resolve(self, video_id: str, /) -> Episode:
        """The video's metadata as an Episode. Raises PermanentError for a video earmark
        refuses (live, not yet aired, private, removed, members-only, age-gated), and
        Transient/PermanentError as fetching does."""
        ...

    def download(self, video_id: str, destination: Callable[[str], Path], /) -> Path:
        """Download an audio-only stream to destination(ext) and return the path. Reuses
        the extraction of the resolve just before it. Never caches."""
        ...


class Transcriber(Protocol):
    def cached(
        self, episode: Episode, options: DecodeOptions | None = None, /
    ) -> Transcript | None:
        """The cached transcript if one exists for this transcriber's profile.

        With ``options`` (a /v1 job) the transcript is looked up per audio and options.
        Never loads a model.
        """
        ...

    def transcribe(
        self, episode: Episode, audio: Path, options: DecodeOptions | None = None, /
    ) -> Transcript:
        """Return the cached transcript if ``cached`` has one (loading no model).

        Otherwise transcribe ``audio`` and write both transcript files through
        ``ArtifactStore.write_transcript`` (``write_decoded`` with ``options``).
        ``options=None`` is the mail path, unchanged.
        """
        ...


class Notifier(Protocol):
    def notify(self, job: Job, episode: Episode | None, /) -> None:
        """Called with the job in a terminal state, and the episode if resolution got that far.

        Must not raise for delivery problems; the runner also catches anything it does raise.
        """
        ...


class Summarizer(Protocol):
    @property
    def backend(self) -> str:
        """``ollama``, ``llama_cpp``, ``vllm`` or ``fake``."""
        ...

    @property
    def model(self) -> str: ...

    @property
    def max_output_tokens(self) -> int: ...

    def summarize(self, request: SummaryInput, /) -> SummaryResult:
        """One model call. Raises PermanentError (context exceeded, refused, malformed
        answer) or TransientError (unreachable, HTTP 5xx, 408, 429). Never puts server
        text, the transcript, the instructions or the output into a message."""
        ...


@dataclass(frozen=True, slots=True)
class OutgoingMail:
    """One composed message, ready to hand to a Mailer."""

    job_id: str
    message_id: str  # "<earmark.<job_id>@<from domain>>"
    sender: str  # the envelope MAIL FROM and the From header
    recipient: str  # the one envelope RCPT TO and the To header: the job's validated `to`
    data: bytes  # the whole message, ASCII, CRLF line endings
    note_filename: str
    instructions_filename: str


class Mailer(Protocol):
    @property
    def sender(self) -> str:
        """The From address. The smtp backend's is EARMARK_SMTP_FROM."""
        ...

    def send(self, mail: OutgoingMail, /) -> None:
        """Deliver ``mail`` or raise.

        Raises TransientError, PermanentError, or MaybeDelivered when the outcome is unknown.
        """
        ...


@dataclass(frozen=True, slots=True)
class Backends:
    """The stage implementations the runner drives."""

    resolver: Resolver
    fetcher: AudioFetcher
    youtube: YoutubeSource
    transcriber: Transcriber
    mailer: Mailer
    notifier: Notifier | None  # None when EARMARK_NOTIFY_BACKEND=none
    summarizer: Summarizer | None = None  # None when EARMARK_LLM_BACKEND=none
