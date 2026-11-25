"""GET /jobs/{id}/transcript: the cached transcript as the mailed note, text or JSON."""

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import SecretStr

from earmark.api.app import create_app
from earmark.config import Settings
from earmark.errors import PermanentError, SkipEpisode, TransientError
from earmark.fakes import FakeBackends, FakeResolver, FakeTranscriber, make_fake_backends
from earmark.models import Job, Segment
from earmark.transcripts import (
    FAILED_BEFORE,
    GONE,
    NOT_YET,
    NOT_YET_AT_STAGE,
    SKIPPED,
    UNREADABLE,
)
from tests.factories import TO, make_episode
from tests.mail_helpers import parse_mail, split_file
from tests.pipeline_env import CANON, PipelineEnv, make_env

TOKEN = "transcript-token-4d2e"
SHARE = "https://podcasts.apple.com/us/podcast/did-a-i-break-the-internet/id1200361736?i=1000802907981&uo=4"
DIRECT = "https://audio.example.net/shows/clip.mp3?sig=abc"
YOUTUBE = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
SENTINEL = "SPOKEN-TOKEN-6e1f"
NOTE_TYPE = "text/markdown; charset=utf-8"
BAD_FORMAT = "format: Input should be 'note', 'text' or 'json'"


@pytest.fixture
def env(tmp_path: Path) -> PipelineEnv:
    return make_env(tmp_path)


def _client(env: PipelineEnv, tmp_path: Path) -> Iterator[TestClient]:
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


@pytest.fixture
def client(env: PipelineEnv, tmp_path: Path) -> Iterator[TestClient]:
    yield from _client(env, tmp_path)


def _post(client: TestClient, url: str = SHARE, **body: Any) -> str:
    posted = client.post("/transcriptions", json={"url": url, "to": TO, **body})
    assert posted.status_code == 202, posted.text
    return str(posted.json()["job_id"])


def _run(env: PipelineEnv) -> Job:
    job = env.worker.run_once()
    assert job is not None
    return job


def _done(client: TestClient, env: PipelineEnv, url: str = SHARE, **body: Any) -> Job:
    job_id = _post(client, url, **body)
    job = _run(env)
    assert (job.id, job.status) == (job_id, "done")
    return job


def _get(client: TestClient, job_id: str, fmt: str | None = None) -> Response:
    suffix = "" if fmt is None else f"?format={fmt}"
    return client.get(f"/jobs/{job_id}/transcript{suffix}")


def _episode_dir(tmp_path: Path, job: Job) -> Path:
    assert job.episode_key is not None
    return tmp_path / "data" / "episodes" / job.episode_key


def _mailed(env: PipelineEnv, index: int = -1) -> Any:
    return parse_mail(env.fakes.mailer.calls[index].data)


# -- the formats ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["apple_podcasts", "youtube", "url", "upload"])
def test_note_equals_mailed_attachment(client: TestClient, env: PipelineEnv, kind: str) -> None:
    if kind == "upload":
        posted = client.post(
            "/transcriptions",
            files={"file": ("memo.m4a", b"ID3" + bytes(2000), "audio/mp4")},
            data={"to": TO, "instructions": "Summarise", "title": "Memo"},
        )
        assert posted.status_code == 202
    else:
        _post(client, {"apple_podcasts": SHARE, "youtube": YOUTUBE, "url": DIRECT}[kind])
    job = _run(env)
    assert job.status == "done"
    mailed = _mailed(env)
    got = _get(client, job.id)
    assert got.status_code == 200
    assert got.content == mailed.note
    assert got.headers["content-disposition"] == f'attachment; filename="{mailed.note_name}"'
    assert got.headers["content-type"] == NOTE_TYPE


def test_note_is_the_default_format(client: TestClient, env: PipelineEnv) -> None:
    job = _done(client, env)
    assert _get(client, job.id).content == _get(client, job.id, "note").content


def test_url_notes_follow_their_own_job(client: TestClient, env: PipelineEnv) -> None:
    first = _done(client, env, DIRECT, title="First title")
    second = _done(client, env, DIRECT + "&x=1", title="Second title")
    assert first.episode_key == second.episode_key
    assert _get(client, first.id).content == _mailed(env, 0).note
    assert _get(client, second.id).content == _mailed(env, 1).note
    assert b"First title" in _get(client, first.id).content
    assert b"Second title" not in _get(client, first.id).content


def test_text_is_transcript_txt(client: TestClient, env: PipelineEnv, tmp_path: Path) -> None:
    job = _done(client, env)
    got = _get(client, job.id, "text")
    assert got.status_code == 200
    assert got.content == (_episode_dir(tmp_path, job) / "transcript.txt").read_bytes()
    assert got.headers["content-type"] == "text/plain; charset=utf-8"
    assert "content-disposition" not in got.headers


def test_json_view_shape(client: TestClient, env: PipelineEnv, tmp_path: Path) -> None:
    job = _done(client, env)
    got = _get(client, job.id, "json")
    assert got.status_code == 200
    assert got.headers["content-type"] == "application/json"
    assert got.content.endswith(b"\n")
    body = json.loads(got.content)
    cached = env.fakes.artifacts.read_transcript(job.episode_key or "")
    assert cached is not None
    assert set(body) == {"job_id", "episode_key", "language", "duration_s", "segments", "warnings"}
    assert body["job_id"] == job.id
    assert body["episode_key"] == cached.episode_key
    assert body["language"] == cached.language
    assert body["duration_s"] == cached.duration_s
    assert body["warnings"] == list(cached.warnings)
    assert body["segments"] == [
        {"start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker}
        for s in cached.segments
    ]
    assert "model" not in body
    assert "asr_profile" not in body


def test_bodies_are_utf8(tmp_path: Path) -> None:
    fakes = _fakes(
        tmp_path,
        resolver=FakeResolver({CANON: make_episode(title="Grüße")}),
        segments=[Segment(start=0, end=5, text="Grüße")],
    )
    env = make_env(tmp_path, fakes=fakes)
    for client in _client(env, tmp_path):
        job = _done(client, env)
        for fmt in ("note", "text", "json"):
            assert "Grüße".encode() in _get(client, job.id, fmt).content
        assert b"\\u00fc" not in _get(client, job.id, "json").content


def _fakes(
    tmp_path: Path, *, resolver: FakeResolver | None = None, segments: list[Segment] | None = None
) -> FakeBackends:
    fakes = make_fake_backends(tmp_path / "data")
    if resolver is not None:
        fakes.resolver = resolver
    if segments is not None:
        fakes.transcriber = FakeTranscriber(fakes.artifacts, segments=segments)
    return fakes


@pytest.mark.parametrize("fmt", ["note", "text", "json"])
def test_every_200_is_no_store(client: TestClient, env: PipelineEnv, fmt: str) -> None:
    job = _done(client, env)
    assert _get(client, job.id, fmt).headers["cache-control"] == "no-store"


def test_note_is_deterministic(client: TestClient, env: PipelineEnv) -> None:
    job = _done(client, env)
    before = _get(client, job.id).content
    env.clock.advance(3600)
    assert _get(client, job.id).content == before


# -- the states ----------------------------------------------------------------------------


@pytest.mark.parametrize("job_id", ["a" * 32, "not-hex", "A" * 32])
def test_unknown_job_404(client: TestClient, job_id: str) -> None:
    got = _get(client, job_id)
    assert (got.status_code, got.json()) == (404, {"detail": "job not found"})


def test_queued_job_409(client: TestClient) -> None:
    got = _get(client, _post(client))
    assert got.status_code == 409
    assert got.json() == {"detail": NOT_YET.format(status="queued")}


def test_running_job_before_deliver_409(client: TestClient, env: PipelineEnv) -> None:
    job_id = _post(client)
    claim = env.jobs.claim_next()
    assert claim is not None
    env.jobs.set_stage(claim, "fetch")
    got = _get(client, job_id)
    assert got.status_code == 409
    assert got.json() == {"detail": NOT_YET_AT_STAGE.format(status="running", stage="fetch")}


def test_running_job_at_deliver_is_served(client: TestClient, env: PipelineEnv) -> None:
    first = _done(client, env, instructions="first")
    second_id = _post(client, instructions="second")
    claim = env.jobs.claim_next()
    assert claim is not None
    env.jobs.set_episode_key(claim, first.episode_key or "")
    env.jobs.set_stage(claim, "deliver")
    got = _get(client, second_id)
    assert got.status_code == 200
    assert got.content == _mailed(env, 0).note


def test_queued_after_transient_delivery_failure_is_served(
    client: TestClient, env: PipelineEnv
) -> None:
    env.fakes.mailer.fail_next(TransientError("smtp down"))
    job_id = _post(client)
    job = _run(env)
    assert (job.status, job.stage) == ("queued", "deliver")
    served = _get(client, job_id)
    assert served.status_code == 200
    env.clock.advance(7200)
    assert _run(env).status == "done"
    assert _mailed(env).note == served.content


def test_failed_before_transcript_409(client: TestClient, env: PipelineEnv) -> None:
    env.fakes.resolver.fail_next(PermanentError("gone"))
    job_id = _post(client)
    job = _run(env)
    assert (job.status, job.stage) == ("failed", "resolve")
    got = _get(client, job_id)
    assert (got.status_code, got.json()) == (409, {"detail": FAILED_BEFORE})


def test_failed_job_409_even_when_episode_cached_since(
    client: TestClient, env: PipelineEnv
) -> None:
    _done(client, env, instructions="first")
    env.fakes.resolver.fail_next(PermanentError("gone"))
    failed_id = _post(client, instructions="second")
    assert _run(env).status == "failed"
    got = _get(client, failed_id)
    assert (got.status_code, got.json()) == (409, {"detail": FAILED_BEFORE})


def test_skipped_job_409(client: TestClient, env: PipelineEnv) -> None:
    env.fakes.resolver.fail_next(SkipEpisode("subscriber-only"))
    job_id = _post(client)
    assert _run(env).status == "skipped"
    got = _get(client, job_id)
    assert (got.status_code, got.json()) == (409, {"detail": SKIPPED})


@pytest.mark.parametrize("fmt", ["note", "text", "json"])
def test_failed_at_delivery_is_served(client: TestClient, env: PipelineEnv, fmt: str) -> None:
    env.fakes.mailer.fail_next(PermanentError("rejected"))
    job_id = _post(client)
    job = _run(env)
    assert (job.status, job.stage) == ("failed", "deliver")
    assert _get(client, job_id, fmt).status_code == 200


def test_attachment_cap_failure_is_served(tmp_path: Path) -> None:
    env = make_env(tmp_path, max_attachment_bytes=100)
    for client in _client(env, tmp_path):
        job_id = _post(client)
        job = _run(env)
        assert job.status == "failed"
        assert "EARMARK_MAX_ATTACHMENT_MB" in (job.error or "")
        got = _get(client, job_id)
        assert got.status_code == 200
        assert len(got.content) > 100


@pytest.mark.parametrize("fmt", ["note", "text", "json"])
def test_missing_transcript_json_410(
    client: TestClient, env: PipelineEnv, tmp_path: Path, fmt: str
) -> None:
    job = _done(client, env)
    (_episode_dir(tmp_path, job) / "transcript.json").unlink()
    got = _get(client, job.id, fmt)
    assert got.status_code == 410
    assert got.json() == {"detail": GONE.format(key=job.episode_key, file="transcript.json")}


def test_missing_transcript_txt_410_for_text_only(
    client: TestClient, env: PipelineEnv, tmp_path: Path
) -> None:
    job = _done(client, env)
    (_episode_dir(tmp_path, job) / "transcript.txt").unlink()
    got = _get(client, job.id, "text")
    assert got.status_code == 410
    assert got.json() == {"detail": GONE.format(key=job.episode_key, file="transcript.txt")}
    assert _get(client, job.id, "note").status_code == 200
    assert _get(client, job.id, "json").status_code == 200


@pytest.mark.parametrize("url", [SHARE, YOUTUBE, DIRECT])
def test_missing_episode_json_410_for_note_only(
    client: TestClient, env: PipelineEnv, tmp_path: Path, url: str
) -> None:
    job = _done(client, env, url)
    (_episode_dir(tmp_path, job) / "episode.json").unlink()
    got = _get(client, job.id, "note")
    assert got.status_code == 410
    assert got.json() == {"detail": GONE.format(key=job.episode_key, file="episode.json")}
    assert _get(client, job.id, "text").status_code == 200
    assert _get(client, job.id, "json").status_code == 200


def test_upload_note_needs_no_episode_json(
    client: TestClient, env: PipelineEnv, tmp_path: Path
) -> None:
    posted = client.post(
        "/transcriptions",
        files={"file": ("memo.m4a", b"ID3" + bytes(500), "audio/mp4")},
        data={"to": TO},
    )
    job = _run(env)
    assert (posted.status_code, job.status) == (202, "done")
    (_episode_dir(tmp_path, job) / "episode.json").unlink()
    assert _get(client, job.id).content == _mailed(env).note


def test_missing_shared_json_410_for_note(
    client: TestClient, env: PipelineEnv, tmp_path: Path
) -> None:
    job = _done(client, env)
    (_episode_dir(tmp_path, job) / "shared.json").unlink()
    got = _get(client, job.id)
    assert got.status_code == 410
    assert got.json() == {"detail": GONE.format(key=job.episode_key, file="shared.json")}
    assert _get(client, job.id, "text").status_code == 200


@pytest.mark.parametrize("fmt", ["note", "text", "json"])
def test_whole_episode_dir_deleted_410(
    client: TestClient, env: PipelineEnv, tmp_path: Path, fmt: str
) -> None:
    job = _done(client, env)
    shutil.rmtree(_episode_dir(tmp_path, job))
    assert _get(client, job.id, fmt).status_code == 410


def test_corrupt_transcript_json_500(client: TestClient, env: PipelineEnv, tmp_path: Path) -> None:
    job = _done(client, env)
    directory = _episode_dir(tmp_path, job)
    (directory / "transcript.json").write_bytes(b'{"%s": 1}' % SENTINEL.encode())
    text = (directory / "transcript.txt").read_bytes()
    expected = {"detail": UNREADABLE.format(key=job.episode_key, file="transcript.json")}
    for fmt in ("note", "json"):
        got = _get(client, job.id, fmt)
        assert (got.status_code, got.json()) == (500, expected)
    served = _get(client, job.id, "text")
    assert (served.status_code, served.content) == (200, text)


def test_corrupt_episode_json_500_for_note(
    client: TestClient, env: PipelineEnv, tmp_path: Path
) -> None:
    job = _done(client, env)
    (_episode_dir(tmp_path, job) / "episode.json").write_bytes(b"{}")
    got = _get(client, job.id)
    assert got.status_code == 500
    assert got.json() == {"detail": UNREADABLE.format(key=job.episode_key, file="episode.json")}
    assert _get(client, job.id, "json").status_code == 200


@pytest.mark.parametrize("fmt", ["xml", "NOTE", ""])
def test_unknown_format_422(client: TestClient, env: PipelineEnv, fmt: str) -> None:
    job = _done(client, env)
    got = _get(client, job.id, fmt)
    assert (got.status_code, got.json()) == (422, {"detail": BAD_FORMAT})
    if fmt == "xml":
        assert "xml" not in got.text


# -- it reads and nothing else -------------------------------------------------------------


def _snapshot(root: Path) -> set[tuple[str, int, int]]:
    return {
        (str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file() and not p.name.startswith("earmark.db")
    }


def _calls(env: PipelineEnv) -> list[int]:
    f = env.fakes
    return [
        f.resolver.call_count,
        f.fetcher.call_count,
        f.youtube.call_count,
        f.transcriber.call_count,
        f.mailer.call_count,
        f.notifier.call_count,
    ]


def test_route_starts_no_work_for_a_queued_job(
    client: TestClient, env: PipelineEnv, tmp_path: Path
) -> None:
    job_id = _post(client)
    before = env.jobs.get(job_id)
    calls = _calls(env)
    for fmt in ("note", "text", "json"):
        assert _get(client, job_id, fmt).status_code == 409
    assert not (tmp_path / "data" / "episodes").exists()
    assert _calls(env) == calls
    assert env.jobs.get(job_id) == before


def test_route_starts_no_work_for_a_done_job(
    client: TestClient, env: PipelineEnv, tmp_path: Path
) -> None:
    job = _done(client, env)
    snapshot = _snapshot(tmp_path / "data")
    before = env.jobs.get(job.id)
    calls = _calls(env)
    for fmt in ("note", "text", "json"):
        assert _get(client, job.id, fmt).status_code == 200
    assert _snapshot(tmp_path / "data") == snapshot
    assert env.jobs.get(job.id) == before
    assert _calls(env) == calls


# -- the token -----------------------------------------------------------------------------


def test_transcript_wrong_token_401(client: TestClient, env: PipelineEnv) -> None:
    job = _done(client, env)
    got = client.get(f"/jobs/{job.id}/transcript", headers={"Authorization": "Bearer nope"})
    assert got.status_code == 401
    assert b"Welcome" not in got.content


# -- a re-share ----------------------------------------------------------------------------


def _reshare(client: TestClient, env: PipelineEnv, second_ts: str) -> tuple[Job, Job]:
    first = _done(client, env, instructions="first", timestamp="2026-01-01T09:00:00-08:00")
    second = _done(client, env, instructions="second", timestamp=second_ts)
    return first, second


def test_reshare_serves_the_same_transcript(client: TestClient, env: PipelineEnv) -> None:
    a, b = _reshare(client, env, "2026-01-01T10:00:00-08:00")
    assert env.fakes.transcriber.computed == 1
    note_b = _get(client, b.id).content
    assert note_b == _mailed(env, 1).note
    assert note_b == _get(client, a.id).content
    assert _get(client, b.id, "text").content == _get(client, a.id, "text").content
    json_a = json.loads(_get(client, a.id, "json").content)
    json_b = json.loads(_get(client, b.id, "json").content)
    assert (json_a["job_id"], json_b["job_id"]) == (a.id, b.id)
    assert json_a["segments"] == json_b["segments"]
    assert _get(client, a.id).content == _mailed(env, 0).note


def test_earlier_timestamp_reshare_moves_the_note(client: TestClient, env: PipelineEnv) -> None:
    a, _ = _reshare(client, env, "2026-01-01T08:00:00-08:00")
    served_a = _get(client, a.id).content
    assert served_a != _mailed(env, 0).note
    assert served_a == _mailed(env, 1).note
    served_front, served_body = split_file(served_a)
    mailed_front, mailed_body = split_file(_mailed(env, 0).note)
    assert served_body == mailed_body
    changed = {k for k in served_front if served_front[k] != mailed_front.get(k)}
    assert changed == {"shared_at"}
