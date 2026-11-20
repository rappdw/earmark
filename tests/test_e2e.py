from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from earmark.api.app import create_app
from earmark.config import Settings
from earmark.fakes import ResolveCall, StoreCall
from earmark.models import Job, JobResult, StoreResult, prompt_hash
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
    assert job.stage == "index"
    assert job.error is None
    assert job.result == JobResult(
        note_path=f"Earmark/Podcasts/{key}.md",
        transcript_note_path=None,
        analysis_hash=prompt_hash(prompt, "fake-llm"),
    )
    episode_dir = tmp_path / "data" / "episodes" / key
    for name in ("episode.json", "shared.json", "transcript.json", "transcript.txt"):
        assert (episode_dir / name).is_file(), name
    assert (episode_dir / "analyses" / f"{prompt_hash(prompt, 'fake-llm')}.json").is_file()
    assert (env.vault / "Earmark" / "Podcasts" / f"{key}.md").is_file()

    artifacts = env.fakes.artifacts
    episode = artifacts.read_episode(key)
    transcript = artifacts.read_transcript(key)
    [analysis] = artifacts.list_analyses(key)
    assert episode is not None
    assert transcript is not None
    assert env.fakes.resolver.calls == [ResolveCall(CANON)]
    assert env.fakes.store.calls == [StoreCall(episode, transcript, (analysis,), job.shared_at)]
    [index_call] = env.fakes.indexer.calls
    assert index_call.stored == StoreResult(note_path=f"Earmark/Podcasts/{key}.md")
    [notify_call] = env.fakes.notifier.calls
    assert notify_call.job.status == "done"
    assert notify_call.episode == episode


def test_run_once_empty_queue(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    assert env.worker.run_once() is None
    assert env.fakes.resolver.call_count == 0
    assert env.fakes.notifier.call_count == 0
