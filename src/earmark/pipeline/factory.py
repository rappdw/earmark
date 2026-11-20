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
    FakeAnalyzer,
    FakeAudioFetcher,
    FakeIndexer,
    FakeNotifier,
    FakeResolver,
    FakeSearcher,
    FakeStore,
    FakeTranscriber,
)
from earmark.index.rebuild import NoteLocator
from earmark.index.search import Searcher
from earmark.jobs import JobStore, open_job_store
from earmark.models import Clock, utc_now
from earmark.pipeline.runner import Pipeline
from earmark.pipeline.stages import (
    Analyzer,
    AudioFetcher,
    Backends,
    Indexer,
    Notifier,
    Resolver,
    Store,
    Transcriber,
)
from earmark.pipeline.worker import Worker, heartbeat_interval

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


def _anthropic_analyzer(ctx: BuildContext) -> Analyzer:
    from earmark.analyze.llm.anthropic import build_anthropic_analyzer

    return build_anthropic_analyzer(ctx.settings, ctx.artifacts, clock=ctx.clock)


def _openai_compat_analyzer(ctx: BuildContext) -> Analyzer:
    from earmark.analyze.llm.openai_compat import build_openai_compat_analyzer

    return build_openai_compat_analyzer(ctx.settings, ctx.artifacts, clock=ctx.clock)


def _vault_store(ctx: BuildContext) -> Store:
    from earmark.vault.notes import build_vault_store

    return build_vault_store(ctx.settings)


def _lancedb_indexer(ctx: BuildContext) -> Indexer:
    from earmark.index.lancedb_index import build_lancedb_index

    return build_lancedb_index(ctx.settings, allow_download=True)


def _ntfy_notifier(ctx: BuildContext) -> Notifier | None:
    from earmark.notify.ntfy import build_ntfy_notifier

    return build_ntfy_notifier(ctx.settings)


def _lancedb_searcher(settings: Settings) -> Searcher:
    from earmark.index.lancedb_index import build_lancedb_index

    return build_lancedb_index(settings, allow_download=False)


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
ANALYZERS: Final[Mapping[str, Builder[Analyzer]]] = {
    "anthropic": _anthropic_analyzer,
    "openai_compat": _openai_compat_analyzer,
    "fake": lambda ctx: FakeAnalyzer(ctx.artifacts, clock=ctx.clock),
}
STORES: Final[Mapping[str, Builder[Store]]] = {
    "vault": _vault_store,
    "fake": lambda ctx: FakeStore(ctx.settings.vault_dir, ctx.settings.vault_subdir),
}
INDEXERS: Final[Mapping[str, Builder[Indexer | None]]] = {
    "fake": lambda ctx: FakeIndexer(),
    "lancedb": _lancedb_indexer,
    "none": lambda ctx: None,
}
SEARCHERS: Final[Mapping[str, Callable[[Settings], Searcher | None]]] = {
    "fake": lambda settings: FakeSearcher(),
    "lancedb": _lancedb_searcher,
    "none": lambda settings: None,
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
    analyzer = _lookup(ANALYZERS, "llm_provider", settings.llm_provider, problems)
    store = _lookup(STORES, "store_backend", settings.store_backend, problems)
    indexer = _lookup(INDEXERS, "index_backend", settings.index_backend, problems)
    notifier = _lookup(NOTIFIERS, "notify_backend", settings.notify_backend, problems)
    if (
        problems
        or resolver is None
        or fetcher is None
        or transcriber is None
        or analyzer is None
        or store is None
        or indexer is None
        or notifier is None
    ):
        raise ConfigError(problems)
    ctx = BuildContext(settings=settings, artifacts=artifacts, clock=clock)
    return Backends(
        resolver=resolver(ctx),
        fetcher=fetcher(ctx),
        transcriber=transcriber(ctx),
        analyzer=analyzer(ctx),
        store=store(ctx),
        indexer=indexer(ctx),
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


def build_analyzer(settings: Settings, *, clock: Clock = utc_now) -> Analyzer:
    """Build only the analyzer (for ``earmark analyze``); no other backend is built or checked."""
    problems: list[str] = []
    builder = _lookup(ANALYZERS, "llm_provider", settings.llm_provider, problems)
    if builder is None:
        raise ConfigError(problems)
    ctx = BuildContext(settings=settings, artifacts=ArtifactStore(settings.data_dir), clock=clock)
    return builder(ctx)


def build_searcher(settings: Settings) -> Searcher | None:
    """Build only the searcher (for ``earmark search`` and ``serve``); it never downloads a model.

    ``None`` when ``EARMARK_INDEX_BACKEND=none``. Raises ``ConfigError`` for an unbuilt
    backend, a missing extra, or an unknown embedding model.
    """
    builder = SEARCHERS.get(settings.index_backend)
    if builder is None:
        choices = ", ".join(sorted(SEARCHERS))
        raise ConfigError(
            [
                f"{env_var('index_backend')}: backend {settings.index_backend!r} is not built "
                f"into this version of earmark (choose one of: {choices})"
            ]
        )
    return builder(settings)


def build_note_locator(settings: Settings) -> NoteLocator:
    """Find existing note paths for episode keys, without writing, for ``earmark reindex``."""
    if settings.store_backend == "vault":
        from earmark.vault.notes import build_vault_store

        return build_vault_store(settings).locate_notes
    if settings.store_backend == "fake":
        return FakeStore(settings.vault_dir, settings.vault_subdir).locate_notes
    raise ConfigError(
        [f"{env_var('store_backend')}: backend {settings.store_backend!r} is not built"]
    )


def build_worker(
    settings: Settings, *, jobs: JobStore | None = None, clock: Clock = utc_now
) -> Worker:
    artifacts = ArtifactStore(settings.data_dir)
    backends = build_backends(settings, artifacts, clock=clock)
    jobs = jobs or open_job_store(settings, clock=clock)
    pipeline = Pipeline(backends, artifacts, jobs, secrets=settings.secret_values())
    logger.info(
        "backends: resolver=%s fetch=%s asr=%s llm=%s store=%s index=%s notify=%s",
        settings.resolver_backend,
        settings.fetch_backend,
        settings.asr_backend,
        settings.llm_provider,
        settings.store_backend,
        settings.index_backend,
        settings.notify_backend,
    )
    return Worker(
        jobs,
        pipeline,
        poll_seconds=settings.worker_poll_seconds,
        heartbeat_seconds=heartbeat_interval(settings.job_lease_seconds),
    )
