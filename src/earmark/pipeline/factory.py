"""Composition root: the only place that constructs backends from Settings.

Each registry maps a backend name (a value of the matching ``Literal`` in
``earmark.config``) to a builder. Later steps add their backend by adding one entry; a
builder imports its heavy modules inside the function, never at module top.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from earmark.artifacts import ArtifactStore
from earmark.config import ConfigError, Settings, env_var
from earmark.fakes import (
    FAKE_SENDER,
    FakeAudioFetcher,
    FakeMailer,
    FakeNotifier,
    FakeResolver,
    FakeTranscriber,
)
from earmark.jobs import JobStore, open_job_store
from earmark.models import Clock, utc_now
from earmark.pipeline.runner import Pipeline
from earmark.pipeline.stages import (
    AudioFetcher,
    Backends,
    Mailer,
    Notifier,
    Resolver,
    Transcriber,
)
from earmark.pipeline.worker import Worker, heartbeat_interval
from earmark.staging import Staging

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BuildContext:
    settings: Settings
    artifacts: ArtifactStore
    clock: Clock


type Builder[T] = Callable[[BuildContext], T]


def _http_fetcher(ctx: BuildContext) -> AudioFetcher:
    from earmark.audio import build_http_fetcher

    return build_http_fetcher(ctx.settings, ctx.artifacts)


def _apple_resolver(ctx: BuildContext) -> Resolver:
    from earmark.resolve.resolver import build_apple_resolver

    return build_apple_resolver(ctx.settings)


def _faster_whisper_transcriber(ctx: BuildContext) -> Transcriber:
    from earmark.transcribe.faster_whisper import build_faster_whisper_transcriber

    return build_faster_whisper_transcriber(ctx.settings, ctx.artifacts)


def _remote_transcriber(ctx: BuildContext) -> Transcriber:
    from earmark.transcribe.remote import build_remote_transcriber

    return build_remote_transcriber(ctx.settings, ctx.artifacts)


def _smtp_mailer(ctx: BuildContext) -> Mailer:
    from earmark.mail.smtp import build_smtp_mailer

    return build_smtp_mailer(ctx.settings)


def _ntfy_notifier(ctx: BuildContext) -> Notifier | None:
    from earmark.notify.ntfy import build_ntfy_notifier

    return build_ntfy_notifier(ctx.settings)


RESOLVERS: Final[Mapping[str, Builder[Resolver]]] = {
    "apple": _apple_resolver,
    "fake": lambda ctx: FakeResolver(),
}
FETCHERS: Final[Mapping[str, Builder[AudioFetcher]]] = {
    "fake": lambda ctx: FakeAudioFetcher(ctx.artifacts),
    "http": _http_fetcher,
}
TRANSCRIBERS: Final[Mapping[str, Builder[Transcriber]]] = {
    "fake": lambda ctx: FakeTranscriber(ctx.artifacts),
    "faster_whisper": _faster_whisper_transcriber,
    "remote": _remote_transcriber,
}
MAILERS: Final[Mapping[str, Builder[Mailer]]] = {
    "fake": lambda ctx: FakeMailer(
        sender=ctx.settings.smtp_from or FAKE_SENDER, outbox=ctx.settings.data_dir / "outbox"
    ),
    "smtp": _smtp_mailer,
}
NOTIFIERS: Final[Mapping[str, Builder[Notifier | None]]] = {
    "fake": lambda ctx: FakeNotifier(),
    "none": lambda ctx: None,
    "ntfy": _ntfy_notifier,
}


def _lookup[T](
    registry: Mapping[str, Builder[T]], field: str, name: str, problems: list[str]
) -> Builder[T] | None:
    builder = registry.get(name)
    if builder is None:
        choices = ", ".join(sorted(registry))
        problems.append(
            f"{env_var(field)}: backend {name!r} is not built into this version of earmark "
            f"(choose one of: {choices})"
        )
    return builder


def build_backends(
    settings: Settings, artifacts: ArtifactStore, *, clock: Clock = utc_now
) -> Backends:
    problems: list[str] = []
    resolver = _lookup(RESOLVERS, "resolver_backend", settings.resolver_backend, problems)
    fetcher = _lookup(FETCHERS, "fetch_backend", settings.fetch_backend, problems)
    transcriber = _lookup(TRANSCRIBERS, "asr_backend", settings.asr_backend, problems)
    mailer = _lookup(MAILERS, "mail_backend", settings.mail_backend, problems)
    notifier = _lookup(NOTIFIERS, "notify_backend", settings.notify_backend, problems)
    if (
        problems
        or resolver is None
        or fetcher is None
        or transcriber is None
        or mailer is None
        or notifier is None
    ):
        raise ConfigError(problems)
    ctx = BuildContext(settings=settings, artifacts=artifacts, clock=clock)
    return Backends(
        resolver=resolver(ctx),
        fetcher=fetcher(ctx),
        transcriber=transcriber(ctx),
        mailer=mailer(ctx),
        notifier=notifier(ctx),
    )


def build_resolver(settings: Settings, *, clock: Clock = utc_now) -> Resolver:
    """Build only the resolver (for ``earmark resolve``); no other backend is built or checked."""
    problems: list[str] = []
    builder = _lookup(RESOLVERS, "resolver_backend", settings.resolver_backend, problems)
    if builder is None:
        raise ConfigError(problems)
    ctx = BuildContext(settings=settings, artifacts=ArtifactStore(settings.data_dir), clock=clock)
    return builder(ctx)


def build_worker(
    settings: Settings, *, jobs: JobStore | None = None, clock: Clock = utc_now
) -> Worker:
    artifacts = ArtifactStore(settings.data_dir)
    backends = build_backends(settings, artifacts, clock=clock)
    jobs = jobs or open_job_store(settings, clock=clock)
    staging = Staging(settings.data_dir)
    # A crashed job is still "running" here, so its partial download is kept for the retry.
    staging.sweep_downloads(jobs.running_job_ids())
    pipeline = Pipeline(
        backends,
        artifacts,
        jobs,
        staging=staging,
        secrets=settings.secret_values(),
        clock=clock,
        max_attachment_bytes=settings.max_attachment_mb * 1024 * 1024,
        recipient_policy=settings.recipient_policy(),
    )
    logger.info(
        "backends: resolver=%s fetch=%s asr=%s mail=%s notify=%s",
        settings.resolver_backend,
        settings.fetch_backend,
        settings.asr_backend,
        settings.mail_backend,
        settings.notify_backend,
    )
    return Worker(
        jobs,
        pipeline,
        poll_seconds=settings.worker_poll_seconds,
        heartbeat_seconds=heartbeat_interval(settings.job_lease_seconds),
    )
