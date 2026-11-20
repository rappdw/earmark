import subprocess
import sys
import typing
from pathlib import Path

import pytest
from pydantic import SecretStr

from earmark import config
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
from earmark.ingest import enqueue_ingest
from earmark.models import IngestRequest
from earmark.pipeline import factory
from tests.factories import MutableClock
from tests.index_helpers import stub_extra_missing

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
    assert "choose one of: " in line


def test_default_settings_list_all_unbuilt(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    expected = {
        field for field, (_, registry) in FIELDS.items() if getattr(settings, field) not in registry
    }
    assert expected == {"notify_backend"}
    with pytest.raises(ConfigError) as info:
        factory.build_backends(settings, ArtifactStore(tmp_path / "data"))
    errors = info.value.errors
    assert len(errors) == len(expected) == 1
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


def test_vault_store_requires_vault_dir(tmp_path: Path) -> None:
    values: dict[str, typing.Any] = dict.fromkeys(FIELDS, "fake")
    values["store_backend"] = "vault"
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", **values)
    with pytest.raises(ConfigError) as info:
        factory.build_backends(settings, ArtifactStore(tmp_path / "data"))
    [line] = info.value.errors
    assert line.startswith("EARMARK_VAULT_DIR: required when EARMARK_STORE_BACKEND=vault")


def test_vault_store_dir_must_exist(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "data")
    missing = _fake_settings(tmp_path, store_backend="vault")
    with pytest.raises(ConfigError) as info:
        factory.build_backends(missing, artifacts)
    [line] = info.value.errors
    assert line.startswith("EARMARK_VAULT_DIR: ")
    assert line.endswith("does not exist")
    assert not (tmp_path / "vault").exists()
    (tmp_path / "vault").write_text("a file")
    with pytest.raises(ConfigError) as info:
        factory.build_backends(missing, artifacts)
    assert info.value.errors[0].endswith("is not a directory")


def test_vault_store_built(tmp_path: Path) -> None:
    from zoneinfo import ZoneInfo

    from earmark.vault.notes import VaultStore

    (tmp_path / "vault").mkdir()
    settings = _fake_settings(tmp_path, store_backend="vault", tz="Europe/Paris")
    b = factory.build_backends(settings, ArtifactStore(tmp_path / "data"))
    assert isinstance(b.store, VaultStore)
    assert list((tmp_path / "vault").iterdir()) == []
    assert not (tmp_path / "data" / "vault.lock").exists()
    assert b.store._tz == ZoneInfo("Europe/Paris")  # no public accessor


def test_build_worker_runs_a_job_with_vault(tmp_path: Path) -> None:
    (tmp_path / "vault").mkdir()
    settings = _fake_settings(tmp_path, store_backend="vault")
    worker = factory.build_worker(settings)
    from earmark.jobs import open_job_store

    enqueue_ingest(open_job_store(settings), IngestRequest(url=SHARE))
    job = worker.run_once()
    assert job is not None
    assert job.status == "done"
    notes = list((tmp_path / "vault" / "Earmark" / "Podcasts" / "Fake Show").glob("*.md"))
    assert len(notes) == 1
    assert job.result is not None
    assert job.result.note_path == f"Earmark/Podcasts/Fake Show/{notes[0].name}"


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


def test_anthropic_analyzer_built(tmp_path: Path) -> None:
    from earmark.analyze.analyzer import LlmAnalyzer
    from earmark.analyze.llm.anthropic import AnthropicClient

    settings = _fake_settings(
        tmp_path,
        llm_provider="anthropic",
        llm_api_key=SecretStr("sk-x"),
        llm_model="claude-x",
        llm_effort="low",
        llm_fallbacks=False,
    )
    analyzer = factory.build_analyzer(settings, clock=MutableClock())
    assert isinstance(analyzer, LlmAnalyzer)
    client = analyzer.client
    assert isinstance(client, AnthropicClient)
    try:
        assert (client.model, client.options.effort, client.options.fallbacks) == (
            "claude-x",
            "low",
            False,
        )
        assert client.context_tokens == 1_000_000
    finally:
        analyzer.close()
    small = factory.build_analyzer(
        _fake_settings(
            tmp_path,
            llm_provider="anthropic",
            llm_api_key=SecretStr("sk-x"),
            llm_context_tokens=200_000,
        )
    )
    assert isinstance(small, LlmAnalyzer)
    try:
        assert small.client.context_tokens == 200_000
    finally:
        small.close()


def test_anthropic_without_credentials_touches_nothing(tmp_path: Path) -> None:
    settings = _fake_settings(tmp_path, llm_provider="anthropic")
    with pytest.raises(ConfigError) as info:
        factory.build_worker(settings)
    from earmark.analyze.llm.anthropic import NO_CREDENTIALS

    assert info.value.errors == [NO_CREDENTIALS]
    assert not (tmp_path / "data").exists()


def test_build_analyzer_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", llm_provider="fake")
    analyzer = factory.build_analyzer(settings)
    assert isinstance(analyzer, FakeAnalyzer)
    unbuilt = Settings(_env_file=None, data_dir=tmp_path / "data", llm_provider="anthropic")
    monkeypatch.setattr(factory, "ANALYZERS", {"fake": factory.ANALYZERS["fake"]})
    with pytest.raises(ConfigError) as info:
        factory.build_analyzer(unbuilt)
    assert info.value.errors[0].startswith("EARMARK_LLM_PROVIDER: ")


def test_pipeline_modules_import_light() -> None:
    code = (
        "import sys\n"
        "import earmark.pipeline.runner, earmark.pipeline.worker, earmark.pipeline.factory\n"
        "heavy = {'fastapi', 'starlette', 'uvicorn', 'httpx2', 'anthropic',\n"
        "         'faster_whisper', 'lancedb', 'fastembed', 'pyarrow', 'onnxruntime', 'numpy',\n"
        "         'loguru'}\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in heavy))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "[]"


def test_lancedb_indexer_without_extra(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for missing in ("lancedb", "fastembed"):
        with monkeypatch.context() as m:
            stub_extra_missing(m, missing)
            settings = _fake_settings(tmp_path, index_backend="lancedb")
            with pytest.raises(ConfigError) as info:
                factory.build_backends(settings, ArtifactStore(tmp_path / "data"))
            [line] = info.value.errors
            assert line.startswith("EARMARK_INDEX_BACKEND: ")
            assert "uv sync --extra index" in line
    assert not (tmp_path / "data").exists()


def _stub_lance(monkeypatch: pytest.MonkeyPatch) -> None:
    from earmark.index.lancedb_index import LanceRuntime

    def connect(path: Path) -> typing.NoReturn:
        raise AssertionError("building must not connect")

    monkeypatch.setattr(
        "earmark.index.lancedb_index.load_lance_runtime", lambda: LanceRuntime(connect)
    )


def test_lancedb_indexer_downloads_and_searcher_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from earmark.fakes import FakeEmbedder
    from earmark.index.lancedb_index import LanceIndex

    seen: list[bool] = []

    def fake_build(settings: Settings, *, allow_download: bool) -> FakeEmbedder:
        seen.append(allow_download)
        return FakeEmbedder()

    monkeypatch.setattr("earmark.index.embed.build_embedder", fake_build)
    _stub_lance(monkeypatch)
    settings = _fake_settings(tmp_path, index_backend="lancedb")
    b = factory.build_backends(settings, ArtifactStore(tmp_path / "data"))
    assert isinstance(b.indexer, LanceIndex)
    searcher = factory.build_searcher(settings)
    assert isinstance(searcher, LanceIndex)
    assert seen == [True, False]
    assert not (tmp_path / "data").exists()


def test_searchers_match_indexers() -> None:
    assert set(factory.SEARCHERS) == set(factory.INDEXERS)
    assert set(factory.SEARCHERS) == set(typing.get_args(config.IndexBackend))


def test_build_searcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(factory.build_searcher(_fake_settings(tmp_path)), FakeSearcher)
    assert factory.build_searcher(_fake_settings(tmp_path, index_backend="none")) is None
    monkeypatch.setattr(factory, "SEARCHERS", {"fake": factory.SEARCHERS["fake"]})
    with pytest.raises(ConfigError, match="EARMARK_INDEX_BACKEND"):
        factory.build_searcher(_fake_settings(tmp_path, index_backend="lancedb"))


def test_build_searcher_without_extra(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_extra_missing(monkeypatch, "lancedb")
    with pytest.raises(ConfigError, match="uv sync --extra index"):
        factory.build_searcher(_fake_settings(tmp_path, index_backend="lancedb"))
    assert not (tmp_path / "data").exists()


def test_build_note_locator(tmp_path: Path) -> None:
    fake = factory.build_note_locator(_fake_settings(tmp_path))
    assert fake(["a" * 20])["a" * 20].note_path == f"Earmark/Podcasts/{'a' * 20}.md"
    (tmp_path / "vault").mkdir()
    vault = factory.build_note_locator(_fake_settings(tmp_path, store_backend="vault"))
    assert vault(["a" * 20]) == {}
    with pytest.raises(ConfigError, match="EARMARK_VAULT_DIR"):
        factory.build_note_locator(
            Settings(_env_file=None, data_dir=tmp_path / "d", store_backend="vault")
        )


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


def test_openai_compat_analyzer_built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx2

    from earmark.analyze.analyzer import LlmAnalyzer
    from earmark.analyze.llm.openai_compat import OpenAICompatClient

    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(500)

    monkeypatch.setattr(httpx2, "HTTPTransport", lambda *a, **k: httpx2.MockTransport(handler))
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        llm_provider="openai_compat",
        llm_base_url="http://spark:8000/v1",
        llm_model="local-model",
        llm_context_tokens=32768,
    )
    analyzer = factory.build_analyzer(settings)
    assert isinstance(analyzer, LlmAnalyzer)
    assert isinstance(analyzer.client, OpenAICompatClient)
    assert analyzer.repairs_invalid_json is True
    assert seen == []
    assert not (tmp_path / "data").exists()


def test_openai_compat_config_error_touches_nothing(tmp_path: Path) -> None:
    settings = _fake_settings(tmp_path, llm_provider="openai_compat")
    with pytest.raises(ConfigError) as info:
        factory.build_worker(settings)
    assert [line.split(":")[0] for line in info.value.errors] == [
        "EARMARK_LLM_BASE_URL",
        "EARMARK_LLM_CONTEXT_TOKENS",
        "EARMARK_LLM_MODEL",
    ]
    assert not (tmp_path / "data").exists()
