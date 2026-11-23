import logging
import os
import time
import typing
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

import httpx2
import pytest

from earmark.artifacts import ArtifactStore
from earmark.audio import STALE_PART_AGE_S, HttpAudioFetcher
from earmark.config import Settings
from earmark.fakes import make_fake_backends
from earmark.fetch import MIB, PermanentFetchError
from earmark.models import Episode, Job
from earmark.pipeline import factory
from earmark.pipeline.stages import AudioFetcher
from tests.factories import T0, make_episode, new_podcast_job
from tests.http_helpers import ScriptedNetwork, StaticResolver, guarded_fetcher_for, mock_fetcher
from tests.pipeline_env import CANON, PipelineEnv, make_env

AUDIO = b"ID3" + bytes(100)


def _episode(audio_url: str = "https://cdn.example/secret-path/ep.mp3?token=tok-77f1") -> Episode:
    return make_episode().model_copy(update={"audio_url": audio_url})


@dataclass
class PipelineRig:
    env: PipelineEnv
    calls: list[httpx2.Request]


def _serve(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, headers={"Content-Type": "audio/mpeg"}, content=AUDIO)


def test_existing_audio_no_request(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "data")
    episode = _episode()
    existing = artifacts.audio_path(episode.key, "m4a")
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"cached")
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        pytest.fail("no request expected")

    fetcher = HttpAudioFetcher(artifacts, mock_fetcher(handler), max_bytes=MIB)
    assert fetcher.fetch(episode) == existing
    assert calls == []
    fetcher.close()


def test_existing_audio_no_connection(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "data")
    episode = _episode()
    existing = artifacts.audio_path(episode.key, "mp3")
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"cached")
    network = ScriptedNetwork({})
    resolver = StaticResolver({})
    fetcher = HttpAudioFetcher(artifacts, guarded_fetcher_for(network, resolver), max_bytes=MIB)
    assert fetcher.fetch(episode) == existing
    assert network.connects == []
    assert resolver.queries == []
    fetcher.close()


def test_fetch_downloads_and_names_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    artifacts = ArtifactStore(tmp_path / "data")
    episode = _episode()
    caplog.set_level(logging.DEBUG)
    fetcher = HttpAudioFetcher(artifacts, mock_fetcher(_serve), max_bytes=MIB)
    path = fetcher.fetch(episode)
    fetcher.close()
    assert path == artifacts.audio_path(episode.key, "mp3")
    assert path.read_bytes() == AUDIO
    assert artifacts.find_audio(episode.key) == path
    messages = [r.getMessage() for r in caplog.records if r.name.startswith("earmark.")]
    assert any("downloaded audio for" in m and "cdn.example" in m for m in messages)


def test_debug_log_has_hosts_only(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "cdn.example":
            return httpx2.Response(
                302, headers={"Location": "https://other.example/secret-path/a?token=tok-77f1"}
            )
        return _serve(request)

    caplog.set_level(logging.DEBUG, logger="earmark.fetch")
    caplog.set_level(logging.DEBUG, logger="earmark.audio")
    artifacts = ArtifactStore(tmp_path / "data")
    fetcher = HttpAudioFetcher(artifacts, mock_fetcher(handler), max_bytes=MIB)
    fetcher.fetch(_episode())
    fetcher.close()
    texts = [r.getMessage() for r in caplog.records if r.name.startswith("earmark.")]
    assert texts
    assert any("hop 1" in t for t in texts)
    for text in texts:
        assert "tok-77f1" not in text
        assert "secret-path" not in text


def test_no_audio_url_permanent(tmp_path: Path) -> None:
    fetcher = HttpAudioFetcher(
        ArtifactStore(tmp_path / "data"), mock_fetcher(_serve), max_bytes=MIB
    )
    with pytest.raises(PermanentFetchError, match="no audio URL"):
        fetcher.fetch(_episode(""))


def test_stale_part_removed_fresh_kept(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "data")
    episode = _episode()
    directory = artifacts.episode_dir(episode.key)
    directory.mkdir(parents=True)
    stale = directory / ".audio.mp3.aaaa.part"
    fresh = directory / ".audio.mp3.bbbb.part"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"y")
    old = time.time() - STALE_PART_AGE_S - 3600
    os.utime(stale, (old, old))
    fetcher = HttpAudioFetcher(artifacts, mock_fetcher(_serve), max_bytes=MIB)
    fetcher.fetch(episode)
    fetcher.close()
    assert not stale.exists()
    assert fresh.exists()


def test_stale_part_age_is_two_hours_beyond_download_deadline() -> None:
    assert STALE_PART_AGE_S >= 2 * 3600


def test_http_fetcher_satisfies_protocol(tmp_path: Path) -> None:
    fetcher: AudioFetcher = HttpAudioFetcher(
        ArtifactStore(tmp_path), mock_fetcher(_serve), max_bytes=MIB
    )
    assert fetcher.fetch is not None


def _settings(tmp_path: Path, **overrides: typing.Any) -> Settings:
    values: dict[str, typing.Any] = {
        "resolver_backend": "fake",
        "asr_backend": "fake",
        "notify_backend": "fake",
        "fetch_backend": "http",
    }
    values.update(overrides)
    return Settings(_env_file=None, data_dir=tmp_path / "data", **values)


def test_factory_builds_http_fetcher(tmp_path: Path) -> None:
    settings = _settings(tmp_path, max_audio_mb=3, allow_private_fetch=True)
    backends = factory.build_backends(settings, ArtifactStore(tmp_path / "data"))
    assert isinstance(backends.fetcher, HttpAudioFetcher)
    assert backends.fetcher.max_bytes == 3 * MIB
    assert backends.fetcher.fetcher.allow_private
    default = factory.build_backends(_settings(tmp_path), ArtifactStore(tmp_path / "data"))
    assert isinstance(default.fetcher, HttpAudioFetcher)
    assert not default.fetcher.fetcher.allow_private


# -- pipeline integration ---------------------------------------------------------------------


def _pipeline(tmp_path: Path, status: int = 200, retry_after: str | None = None) -> PipelineRig:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        assert request.url.host == "fake.invalid"
        if status != 200:
            headers = {"Retry-After": retry_after} if retry_after else {}
            return httpx2.Response(status, headers=headers)
        return _serve(request)

    fakes = make_fake_backends(tmp_path / "data")
    http = HttpAudioFetcher(fakes.artifacts, mock_fetcher(handler), max_bytes=MIB)
    env = make_env(tmp_path, fakes=fakes, backends=replace(fakes.backends(), fetcher=http))
    return PipelineRig(env, calls)


def _run(rig: PipelineRig, prompt: str = "") -> Job:
    rig.env.jobs.enqueue(new_podcast_job(CANON, instructions=prompt))
    job = rig.env.worker.run_once()
    assert job is not None
    return job


def test_pipeline_with_http_fetcher(tmp_path: Path) -> None:
    rig = _pipeline(tmp_path)
    job = _run(rig)
    assert job.status == "done"
    assert job.episode_key is not None
    audio = rig.env.fakes.artifacts.audio_path(job.episode_key, "mp3")
    assert audio.read_bytes() == AUDIO
    assert rig.env.fakes.transcriber.calls[0].audio == audio
    assert len(rig.calls) == 1
    second = _run(rig, "What papers are cited?")
    assert second.status == "done"
    assert len(rig.calls) == 1


def test_pipeline_http_404_fails_job(tmp_path: Path) -> None:
    job = _run(_pipeline(tmp_path, status=404))
    assert job.status == "failed"
    assert job.stage == "fetch"
    assert job.error is not None
    assert "HTTP 404" in job.error
    assert "fake.invalid" in job.error


def test_pipeline_http_503_requeues(tmp_path: Path) -> None:
    rig = _pipeline(tmp_path, status=503, retry_after="600")
    job = _run(rig)
    assert job.status == "queued"
    assert job.attempts == 1
    assert job.run_after >= T0 + timedelta(seconds=600)


# -- direct audio URLs ----------------------------------------------------------------------

DIRECT = "https://audio.example.net/private/PATH-TOKEN-91d2.mp3?sig=QUERY-TOKEN-a77b"


def test_download_writes_destination_and_logs_host_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from earmark.staging import Staging

    caplog.set_level(logging.DEBUG)
    staging = Staging(tmp_path / "data")
    fetcher = HttpAudioFetcher(
        ArtifactStore(tmp_path / "data"), mock_fetcher(_serve), max_bytes=MIB
    )
    destination = staging.download_destination("a" * 32)
    path = fetcher.download(DIRECT, destination)
    fetcher.close()
    assert path == staging.downloads_dir / ("a" * 32 + ".mp3")
    assert path.read_bytes() == AUDIO
    texts = [r.getMessage() for r in caplog.records if r.name.startswith("earmark.")]
    assert any("downloaded direct audio" in t and "audio.example.net" in t for t in texts)
    for text in texts:
        assert "PATH-TOKEN" not in text
        assert "QUERY-TOKEN" not in text
        assert "/private/" not in text
    assert not [p for p in staging.downloads_dir.iterdir() if p.name.endswith(".part")]


def test_download_refuses_textual_type(tmp_path: Path) -> None:
    from earmark.staging import Staging

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"Content-Type": "text/html"}, content=b"<html>")

    staging = Staging(tmp_path / "data")
    fetcher = HttpAudioFetcher(
        ArtifactStore(tmp_path / "data"), mock_fetcher(handler), max_bytes=MIB
    )
    with pytest.raises(PermanentFetchError, match="not audio") as info:
        fetcher.download(DIRECT, staging.download_destination("b" * 32))
    fetcher.close()
    assert "PATH-TOKEN" not in str(info.value)
    assert not list(staging.downloads_dir.glob("*"))


def test_download_names_the_limit(tmp_path: Path) -> None:
    from earmark.staging import Staging

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"Content-Type": "audio/mpeg"}, content=bytes(MIB + 1))

    fetcher = HttpAudioFetcher(
        ArtifactStore(tmp_path / "data"), mock_fetcher(handler), max_bytes=MIB
    )
    with pytest.raises(PermanentFetchError, match="EARMARK_MAX_AUDIO_MB"):
        fetcher.download(DIRECT, Staging(tmp_path / "data").download_destination("c" * 32))
    fetcher.close()
