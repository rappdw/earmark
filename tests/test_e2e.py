from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from earmark.api.app import create_app
from earmark.config import Settings
from earmark.fakes import ResolveCall
from earmark.models import Job, JobResult
from earmark.notify.ntfy import compose
from tests.pipeline_env import CANON, make_env

TOKEN = "e2e-token-1a2b3c"
SHARE = "https://podcasts.apple.com/us/podcast/did-a-i-break-the-internet/id1200361736?i=1000802907981&uo=4"


def test_ingest_then_run_once(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    settings = Settings(_env_file=None, api_token=SecretStr(TOKEN), data_dir=tmp_path / "data")
    prompt = "What papers are cited?"
    with TestClient(
        create_app(settings, store=env.jobs), headers={"Authorization": f"Bearer {TOKEN}"}
    ) as client:
        posted = client.post("/ingest", json={"url": SHARE, "prompt": prompt})
        assert posted.status_code == 202
        job_id = posted.json()["job_id"]
        ran = env.worker.run_once()
        assert ran is not None
        assert (ran.id, ran.status) == (job_id, "done")
        job = Job.model_validate(client.get(f"/jobs/{job_id}").json())

    key = job.episode_key
    assert key is not None
    assert job.status == "done"
    assert job.stage == "transcribe"
    assert job.error is None
    assert job.result == JobResult(duration_s=900.0)
    episode_dir = tmp_path / "data" / "episodes" / key
    for name in ("episode.json", "shared.json", "transcript.json", "transcript.txt"):
        assert (episode_dir / name).is_file(), name
    assert sorted(p.name for p in episode_dir.iterdir()) == [
        "audio.mp3",
        "episode.json",
        "shared.json",
        "transcript.json",
        "transcript.txt",
    ]

    episode = env.fakes.artifacts.read_episode(key)
    assert episode is not None
    assert env.fakes.resolver.calls == [ResolveCall(CANON)]
    assert env.fakes.fetcher.computed == 1
    assert env.fakes.transcriber.computed == 1
    [notify_call] = env.fakes.notifier.calls
    assert notify_call.job.status == "done"
    assert notify_call.episode == episode

    note = compose(notify_call.job, notify_call.episode)
    assert note is not None
    assert note.title == "Transcribed: " + episode.title
    assert "Duration: 00:15:00" in note.message
    for text in (note.title, note.message):
        assert "://" not in text
        assert "podcasts.apple.com" not in text
        assert prompt not in text


def test_run_once_empty_queue(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    assert env.worker.run_once() is None
    assert env.fakes.resolver.call_count == 0
    assert env.fakes.notifier.call_count == 0
