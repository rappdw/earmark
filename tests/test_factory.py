import subprocess
import sys
import typing
from pathlib import Path

import pytest

from earmark import config
from earmark.artifacts import ArtifactStore
from earmark.config import ConfigError, Settings, env_var
from earmark.fakes import (
    FakeAnalyzer,
    FakeAudioFetcher,
    FakeIndexer,
    FakeNotifier,
    FakeResolver,
    FakeStore,
    FakeTranscriber,
)
from earmark.ingest import enqueue_ingest
from earmark.models import IngestRequest
from earmark.pipeline import factory
from tests.factories import MutableClock

SHARE = "https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000802907981&uo=4"

FIELDS: dict[str, tuple[typing.Any, typing.Mapping[str, object]]] = {
    "resolver_backend": (config.ResolverBackend, factory.RESOLVERS),
    "fetch_backend": (config.FetchBackend, factory.FETCHERS),
    "asr_backend": (config.AsrBackend, factory.TRANSCRIBERS),
    "llm_provider": (config.LlmProvider, factory.ANALYZERS),
    "store_backend": (config.StoreBackend, factory.STORES),
    "index_backend": (config.IndexBackend, factory.INDEXERS),
    "notify_backend": (config.NotifyBackend, factory.NOTIFIERS),
}


def _fake_settings(tmp_path: Path, **overrides: typing.Any) -> Settings:
    values: dict[str, typing.Any] = dict.fromkeys(FIELDS, "fake")
    values.update(overrides)
    return Settings(
        _env_file=None, data_dir=tmp_path / "data", vault_dir=tmp_path / "vault", **values
    )


def _unbuilt_names(field: str) -> list[str]:
    literal, registry = FIELDS[field]
    return [v for v in typing.get_args(literal) if v not in registry]


UNBUILT_FIELDS = [field for field in FIELDS if _unbuilt_names(field)]


@pytest.mark.parametrize("field", UNBUILT_FIELDS)
def test_unbuilt_backend_names_setting(tmp_path: Path, field: str) -> None:
    _, registry = FIELDS[field]
    name = _unbuilt_names(field)[0]
    settings = _fake_settings(tmp_path, **{field: name})
    with pytest.raises(ConfigError) as info:
        factory.build_backends(settings, ArtifactStore(tmp_path / "data"))
    [line] = info.value.errors
    assert line.startswith(f"{env_var(field)}: ")
    assert repr(name) in line
    choices = ", ".join(sorted(registry))
    assert line.endswith(f"(choose one of: {choices})")
    assert "choose one of: fake" in line


def test_default_settings_list_all_unbuilt(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    expected = {
        field for field, (_, registry) in FIELDS.items() if getattr(settings, field) not in registry
    }
    assert expected == {"llm_provider", "store_backend", "index_backend", "notify_backend"}
    with pytest.raises(ConfigError) as info:
        factory.build_backends(settings, ArtifactStore(tmp_path / "data"))
    errors = info.value.errors
    assert len(errors) == len(expected) == 4
    for field in FIELDS:
        count = 1 if field in expected else 0
        assert sum(line.startswith(f"{env_var(field)}: ") for line in errors) == count
    assert not any(line.startswith("EARMARK_FETCH_BACKEND") for line in errors)
    assert not any(line.startswith("EARMARK_RESOLVER_BACKEND") for line in errors)
    assert not any(line.startswith("EARMARK_ASR_BACKEND") for line in errors)


def test_all_fake_builds_fakes(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "data")
    b = factory.build_backends(_fake_settings(tmp_path), artifacts)
    assert isinstance(b.resolver, FakeResolver)
    assert isinstance(b.fetcher, FakeAudioFetcher)
    assert isinstance(b.transcriber, FakeTranscriber)
    assert isinstance(b.analyzer, FakeAnalyzer)
    assert isinstance(b.store, FakeStore)
    assert isinstance(b.indexer, FakeIndexer)
    assert isinstance(b.notifier, FakeNotifier)
    assert b.store.vault_dir == tmp_path / "vault"
    none = factory.build_backends(
        _fake_settings(tmp_path, index_backend="none", notify_backend="none"), artifacts
    )
    assert none.indexer is None
    assert none.notifier is None


def test_registries_match_settings() -> None:
    for literal, registry in FIELDS.values():
        assert set(registry) <= set(typing.get_args(literal))
        assert "fake" in registry


def test_fake_analyzer_uses_real_clock(tmp_path: Path) -> None:
    clock = MutableClock()
    b = factory.build_backends(
        _fake_settings(tmp_path), ArtifactStore(tmp_path / "data"), clock=clock
    )
    assert isinstance(b.analyzer, FakeAnalyzer)
    assert b.analyzer.clock is clock


def test_build_worker_runs_a_job(tmp_path: Path) -> None:
    settings = _fake_settings(tmp_path)
    worker = factory.build_worker(settings)
    from earmark.jobs import open_job_store

    enqueue_ingest(open_job_store(settings), IngestRequest(url=SHARE))
    job = worker.run_once()
    assert job is not None
    assert job.status == "done"
    assert list((tmp_path / "vault" / "Earmark" / "Podcasts").glob("*.md"))


def test_build_worker_config_error_touches_nothing(tmp_path: Path) -> None:
    settings = _fake_settings(tmp_path, asr_backend="remote")
    with pytest.raises(ConfigError) as info:
        factory.build_worker(settings)
    [line] = info.value.errors
    assert line.startswith("EARMARK_ASR_BASE_URL: required when EARMARK_ASR_BACKEND=remote")
    assert not (tmp_path / "data").exists()


def test_pipeline_modules_import_light() -> None:
    code = (
        "import sys\n"
        "import earmark.pipeline.runner, earmark.pipeline.worker, earmark.pipeline.factory\n"
        "heavy = {'fastapi', 'starlette', 'uvicorn', 'httpx2', 'anthropic',\n"
        "         'faster_whisper', 'lancedb', 'fastembed'}\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in heavy))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "[]"


def test_fetch_module_not_imported_by_factory() -> None:
    code = (
        "import sys\n"
        "import earmark.pipeline.factory\n"
        "print(sorted(m for m in ('httpx2', 'httpcore2', 'earmark.fetch', 'earmark.audio')\n"
        "             if m in sys.modules))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "[]"


def test_factory_builds_apple_resolver(tmp_path: Path) -> None:
    from earmark.fetch import MIB
    from earmark.resolve.resolver import AppleResolver

    settings = _fake_settings(
        tmp_path, resolver_backend="apple", allow_private_fetch=True, max_feed_mb=3
    )
    resolver = factory.build_resolver(settings)
    assert isinstance(resolver, AppleResolver)
    assert resolver.max_bytes == 3 * MIB
    assert resolver.fetcher.allow_private is True
    default = factory.build_resolver(_fake_settings(tmp_path, resolver_backend="apple"))
    assert isinstance(default, AppleResolver)
    assert default.fetcher.allow_private is False


def test_build_backends_uses_apple_resolver(tmp_path: Path) -> None:
    from earmark.resolve.resolver import AppleResolver

    b = factory.build_backends(
        _fake_settings(tmp_path, resolver_backend="apple"), ArtifactStore(tmp_path / "data")
    )
    assert isinstance(b.resolver, AppleResolver)


def test_resolver_modules_not_imported_by_factory() -> None:
    code = (
        "import sys\n"
        "import earmark.pipeline.factory\n"
        "names = ('earmark.resolve.resolver', 'earmark.resolve.rss', 'earmark.resolve.itunes',\n"
        "         'earmark.resolve.apple_page', 'defusedxml')\n"
        "print(sorted(m for m in names if m in sys.modules))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "[]"


def test_build_resolver_only_builds_resolver(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        resolver_backend="fake",
        asr_backend="faster_whisper",
    )
    assert isinstance(factory.build_resolver(settings), FakeResolver)
    assert not (tmp_path / "data").exists()


def test_build_resolver_unbuilt_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(factory, "RESOLVERS", {"fake": factory.RESOLVERS["fake"]})
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    with pytest.raises(ConfigError) as info:
        factory.build_resolver(settings)
    [line] = info.value.errors
    assert line.startswith("EARMARK_RESOLVER_BACKEND: ")
