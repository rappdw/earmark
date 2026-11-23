import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from earmark.api.app import create_app
from earmark.api.schemas import JobView
from earmark.config import Settings
from earmark.fakes import ResolveCall
from earmark.models import JobResult, content_key
from earmark.notify.ntfy import compose
from tests.factories import TO
from tests.mail_helpers import assert_note_hash_verifies, parse_mail
from tests.pipeline_env import CANON, PipelineEnv, make_env

TOKEN = "e2e-token-1a2b3c"
SHARE = "https://podcasts.apple.com/us/podcast/did-a-i-break-the-internet/id1200361736?i=1000802907981&uo=4"
DIRECT = "https://audio.example.net/shows/clip.mp3?sig=abc"


@pytest.fixture
def env(tmp_path: Path) -> PipelineEnv:
    return make_env(tmp_path)


@pytest.fixture
def client(env: PipelineEnv, tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,
        api_token=SecretStr(TOKEN),
        data_dir=tmp_path / "data",
        allowed_recipients=(TO,),
    )
    with TestClient(
        create_app(settings, store=env.jobs), headers={"Authorization": f"Bearer {TOKEN}"}
    ) as c:
        yield c


def _finish(client: TestClient, env: PipelineEnv, job_id: str) -> JobView:
    ran = env.worker.run_once()
    assert ran is not None
    assert (ran.id, ran.status) == (job_id, "done")
    return JobView.model_validate(client.get(f"/jobs/{job_id}").json())


def test_podcast_request_transcribes_on_fakes(
    client: TestClient, env: PipelineEnv, tmp_path: Path
) -> None:
    instructions = "What papers are cited?"
    posted = client.post(
        "/transcriptions", json={"url": SHARE, "to": TO, "instructions": instructions}
    )
    assert posted.status_code == 202
    job = _finish(client, env, posted.json()["job_id"])

    key = job.episode_key
    assert key is not None
    assert (job.status, job.stage, job.error, job.source) == (
        "done",
        "deliver",
        None,
        "apple_podcasts",
    )
    assert job.delivery_state == "sent"
    assert job.sent_at is not None
    [mail] = env.fakes.mailer.calls
    assert mail.recipient == TO
    parsed = parse_mail(mail.data)
    front = assert_note_hash_verifies(parsed.note)
    assert front["title"] == "Fake episode " + front["guid"].rsplit(":", 1)[-1][:6]
    assert parsed.note_name == mail.note_filename
    assert parsed.instructions.endswith(b"---\n" + instructions.encode())
    assert job.result == JobResult(duration_s=900.0)
    assert job.to == TO
    assert job.instructions == instructions
    episode_dir = tmp_path / "data" / "episodes" / key
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
    assert note.title == "Sent: " + episode.title
    assert "Duration: 00:15:00" in note.message
    for text in (note.title, note.message):
        assert "://" not in text
        assert "podcasts.apple.com" not in text
        assert instructions not in text
        assert TO not in text


def test_direct_url_request_transcribes_on_fakes(
    client: TestClient, env: PipelineEnv, tmp_path: Path
) -> None:
    posted = client.post("/transcriptions", json={"url": DIRECT, "to": TO, "title": "A clip"})
    assert posted.status_code == 202
    job = _finish(client, env, posted.json()["job_id"])

    assert job.source == "url"
    assert job.url == "https://audio.example.net/shows/clip.mp3"
    assert job.result is not None
    assert job.result.duration_s == 15.0
    assert job.episode_key == content_key(hashlib.sha256(b"fake audio\n").hexdigest())
    transcript = tmp_path / "data" / "episodes" / (job.episode_key or "") / "transcript.json"
    assert transcript.is_file()
    assert env.fakes.resolver.calls == []
    assert env.fakes.fetcher.downloads == 1
    assert env.fakes.fetcher.computed == 0
    assert not (tmp_path / "data" / "downloads").exists() or not list(
        (tmp_path / "data" / "downloads").iterdir()
    )
    [call] = env.fakes.notifier.calls
    note = compose(call.job, call.episode)
    assert note is not None
    assert note.title == "Sent: A clip"
    assert (job.delivery_state, env.fakes.mailer.call_count) == ("sent", 1)
    assert_note_hash_verifies(parse_mail(env.fakes.mailer.calls[0].data).note)
    assert "audio.example.net" in note.message
    assert "/shows/" not in note.title + note.message


def test_upload_request_transcribes_on_fakes(
    client: TestClient, env: PipelineEnv, tmp_path: Path
) -> None:
    data = b"ID3" + bytes(2000)
    posted = client.post(
        "/transcriptions",
        files={"file": ("memo.m4a", data, "audio/mp4")},
        data={"to": TO, "instructions": "Summarise", "title": "Memo"},
    )
    assert posted.status_code == 202
    assert (tmp_path / "data" / "uploads").is_dir()
    job = _finish(client, env, posted.json()["job_id"])

    assert (job.source, job.url, job.title) == ("upload", None, "Memo")
    assert job.episode_key == content_key(hashlib.sha256(data).hexdigest())
    assert job.result is not None
    transcript = tmp_path / "data" / "episodes" / (job.episode_key or "") / "transcript.json"
    assert transcript.is_file()
    assert env.fakes.resolver.calls == []
    assert env.fakes.fetcher.call_count == 0
    assert env.fakes.transcriber.calls[0].audio.suffix == ".m4a"
    assert list((tmp_path / "data" / "uploads").iterdir()) == []  # retention: gone when done
    [call] = env.fakes.notifier.calls
    assert call.job.status == "done"
    assert (job.delivery_state, env.fakes.mailer.call_count) == ("sent", 1)
    assert env.fakes.mailer.calls[0].recipient == TO
    assert_note_hash_verifies(parse_mail(env.fakes.mailer.calls[0].data).note)


def test_run_once_empty_queue(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    assert env.worker.run_once() is None
    assert env.fakes.resolver.call_count == 0
    assert env.fakes.notifier.call_count == 0
