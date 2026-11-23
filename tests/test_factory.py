import subprocess
import sys
import typing
from pathlib import Path

import pytest
from pydantic import SecretStr

from earmark import config
from earmark.artifacts import ArtifactStore
from earmark.config import ConfigError, Settings, env_var
from earmark.fakes import FakeAudioFetcher, FakeNotifier, FakeResolver, FakeTranscriber
from earmark.pipeline import factory
from tests.factories import new_podcast_job

SHARE = "https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000802907981&uo=4"

FIELDS: dict[str, tuple[typing.Any, typing.Mapping[str, object]]] = {
    "resolver_backend": (config.ResolverBackend, factory.RESOLVERS),
    "fetch_backend": (config.FetchBackend, factory.FETCHERS),
    "asr_backend": (config.AsrBackend, factory.TRANSCRIBERS),
    "notify_backend": (config.NotifyBackend, factory.NOTIFIERS),
}


def _fake_settings(tmp_path: Path, **overrides: typing.Any) -> Settings:
    values: dict[str, typing.Any] = dict.fromkeys(FIELDS, "fake")
    values.update(overrides)
    return Settings(_env_file=None, data_dir=tmp_path / "data", **values)


def test_unbuilt_backend_name_names_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name that is a valid setting but has no builder fails with a ConfigError that says so."""
    without_ntfy = {k: v for k, v in factory.NOTIFIERS.items() if k != "ntfy"}
    monkeypatch.setattr(factory, "NOTIFIERS", without_ntfy)
    settings = _fake_settings(tmp_path, notify_backend="ntfy")
    with pytest.raises(ConfigError) as info:
        factory.build_backends(settings, ArtifactStore(tmp_path / "data"))
    [line] = info.value.errors
    assert line.startswith(f"{env_var('notify_backend')}: ")
    assert repr("ntfy") in line
    assert line.endswith(f"(choose one of: {', '.join(sorted(without_ntfy))})")


def test_every_default_backend_is_built(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    expected = {
        field for field, (_, registry) in FIELDS.items() if getattr(settings, field) not in registry
    }
    assert expected == set()
    for literal, registry in FIELDS.values():
        assert set(registry) == set(typing.get_args(literal))


def test_ntfy_without_url_builds_none(tmp_path: Path) -> None:
    settings = _fake_settings(tmp_path, notify_backend="ntfy")
    backends = factory.build_backends(settings, ArtifactStore(tmp_path / "data"))
    assert backends.notifier is None


def test_ntfy_with_url_builds_notifier(tmp_path: Path) -> None:
    from earmark.notify.ntfy import NtfyNotifier

    settings = _fake_settings(
        tmp_path, notify_backend="ntfy", ntfy_url=SecretStr("https://ntfy.test/sentinel-topic")
    )
    backends = factory.build_backends(settings, ArtifactStore(tmp_path / "data"))
    assert isinstance(backends.notifier, NtfyNotifier)  # built without a request: sockets blocked


def test_all_fake_builds_fakes(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "data")
    b = factory.build_backends(_fake_settings(tmp_path), artifacts)
    assert isinstance(b.resolver, FakeResolver)
    assert isinstance(b.fetcher, FakeAudioFetcher)
    assert isinstance(b.transcriber, FakeTranscriber)
    assert isinstance(b.notifier, FakeNotifier)
    none = factory.build_backends(_fake_settings(tmp_path, notify_backend="none"), artifacts)
    assert none.notifier is None


def test_registries_match_settings() -> None:
    for literal, registry in FIELDS.values():
        assert set(registry) <= set(typing.get_args(literal))
        assert "fake" in registry


def test_build_worker_runs_a_job(tmp_path: Path) -> None:
    settings = _fake_settings(tmp_path)
    worker = factory.build_worker(settings)
    from earmark.jobs import open_job_store

    open_job_store(settings).enqueue(new_podcast_job(SHARE))
    job = worker.run_once()
    assert job is not None
    assert job.status == "done"
    assert job.episode_key is not None
    assert ArtifactStore(tmp_path / "data").has_transcript(job.episode_key)


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
        "heavy = {'fastapi', 'starlette', 'uvicorn', 'httpx2', 'faster_whisper', 'onnxruntime',\n"
        "         'numpy'}\n"
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


# -- digest builders ---------------------------------------------------------------------------


def test_build_worker_sweeps_downloads(tmp_path: Path) -> None:
    from earmark.db import open_database
    from earmark.jobs import JobStore, NewJob

    settings = _fake_settings(tmp_path)
    store = JobStore(open_database(tmp_path / "data" / "earmark.db"))
    store.enqueue(NewJob(source="url", to="you@example.org", url="https://audio.example.net/a.mp3"))
    claim = store.claim_next()  # a worker that crashed mid-download
    assert claim is not None
    running = claim.job.id
    downloads = tmp_path / "data" / "downloads"
    downloads.mkdir(parents=True)
    (downloads / f"{running}.mp3").write_bytes(b"partial of a running job")
    (downloads / f".{running}.mp3.abc.part").write_bytes(b"part")
    (downloads / f"{'2' * 32}.mp3").write_bytes(b"orphan")
    (downloads / "stray.txt").write_bytes(b"stray")
    factory.build_worker(settings)
    assert sorted(p.name for p in downloads.iterdir()) == sorted(
        [f"{running}.mp3", f".{running}.mp3.abc.part"]
    )
