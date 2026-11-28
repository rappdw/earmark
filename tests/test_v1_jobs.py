"""The /v1 job and transcript routes, and DELETE (the purge)."""

import json
import sqlite3
from pathlib import Path

import pytest

from earmark.artifacts import ArtifactStore
from earmark.db import Database
from earmark.errors import PermanentError
from earmark.models import Segment
from earmark.v1_jobs import PURGED, SUMMARY_PURGED, V1_FAILED, V1_GONE, V1_UNREADABLE
from tests.factories import new_upload_job
from tests.multipart_helpers import audio
from tests.summary_env import SummaryEnv, summary_env
from tests.v1_env import V1Env, v1_env

SEGMENTS = (
    Segment(start=0.0, end=4.2, text="Good morning, everyone."),
    Segment(start=4.2, end=9.8, text="Let's start with the roadmap."),
)
DONE_TEXT = "[00:00:00] Good morning, everyone.\n[00:00:04] Let's start with the roadmap.\n"


def _prompt_in_db(v1: V1Env, job_id: str) -> str | None:
    conn = sqlite3.connect(v1.data_dir / "earmark.db")
    try:
        row = conn.execute("SELECT initial_prompt FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def _files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def _decoded(v1: V1Env) -> list[Path]:
    root = v1.data_dir / "transcripts"
    return sorted(root.rglob("*")) if root.is_dir() else []


# -- purge ---------------------------------------------------------------------------------


def test_purge_removes_files_and_prompt(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        job_id = v1.submit(initial_prompt="PROMPT-TEXT-77")
        v1.run()
        assert _prompt_in_db(v1, job_id) == "PROMPT-TEXT-77"
        assert [p for p in _decoded(v1) if p.is_file()]
        deleted = v1.client.delete(f"/v1/jobs/{job_id}")
        assert deleted.status_code == 200
        assert deleted.json()["purged_at"] is not None
        assert not [p for p in _decoded(v1) if p.is_file()]
        assert v1.uploads == []
        assert _prompt_in_db(v1, job_id) is None
        gone = v1.transcript(job_id)
        assert (gone.status_code, gone.json()) == (410, {"detail": PURGED})
        shown = v1.client.get(f"/v1/jobs/{job_id}")
        assert shown.status_code == 200
        assert shown.json()["purged_at"] == deleted.json()["purged_at"]


def test_purge_leaves_only_the_row(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        job_id = v1.submit(initial_prompt="PROMPT-TEXT-77")
        v1.run()
        assert v1.client.delete(f"/v1/jobs/{job_id}").status_code == 200
        assert set(_files(v1.data_dir)) <= {"earmark.db", "earmark.db-wal", "earmark.db-shm"}
        assert "earmark.db" in _files(v1.data_dir)
        assert not (v1.data_dir / "episodes").exists()


@pytest.mark.parametrize("state", ["queued", "running"])
def test_purge_refuses_active(tmp_path: Path, state: str) -> None:
    with v1_env(tmp_path) as v1:
        job_id = v1.submit(initial_prompt="keep me")
        if state == "running":
            assert v1.jobs.claim_next() is not None
        denied = v1.client.delete(f"/v1/jobs/{job_id}")
        assert denied.status_code == 409
        assert denied.json() == {
            "detail": f"this job is still {state}; delete it once it has finished"
        }
        assert len(v1.uploads) == 1
        assert _prompt_in_db(v1, job_id) == "keep me"


def test_purge_is_repeatable(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        job_id = v1.submit()
        v1.run()
        first = v1.client.delete(f"/v1/jobs/{job_id}")
        second = v1.client.delete(f"/v1/jobs/{job_id}")
        assert (first.status_code, second.status_code) == (200, 200)
        assert first.json() == second.json()


def test_purge_marks_finished_siblings(tmp_path: Path) -> None:
    with v1_env(tmp_path, no_dedupe=True) as v1:
        a = v1.submit(initial_prompt="shared")
        v1.run()
        b = v1.submit(initial_prompt="shared")
        v1.run()
        c = v1.submit(initial_prompt="shared")
        assert len({a, b, c}) == 3
        assert v1.client.delete(f"/v1/jobs/{a}").status_code == 200
        assert v1.client.get(f"/v1/jobs/{b}").json()["purged_at"] is not None
        assert _prompt_in_db(v1, b) is None
        queued = v1.client.get(f"/v1/jobs/{c}").json()
        assert queued["purged_at"] is None
        assert queued["status"] == "queued"
        assert _prompt_in_db(v1, c) == "shared"
        assert v1.run().id == c
        served = v1.transcript(c)
        assert served.status_code == 200
        assert v1.fakes.transcriber.computed == 2  # the purge removed the cache


def test_purge_files_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with v1_env(tmp_path) as v1:
        job_id = v1.submit(initial_prompt="still here")
        v1.run()
        real = ArtifactStore.remove_decoded

        def broken(self: ArtifactStore, dkey: str) -> int:
            raise OSError("disk says no")

        monkeypatch.setattr(ArtifactStore, "remove_decoded", broken)
        failed = v1.client.delete(f"/v1/jobs/{job_id}")
        assert failed.status_code == 500
        assert failed.json() == {"detail": "could not delete the transcript files; try again"}
        assert _prompt_in_db(v1, job_id) == "still here"
        assert v1.client.get(f"/v1/jobs/{job_id}").json()["purged_at"] is None
        monkeypatch.setattr(ArtifactStore, "remove_decoded", real)
        assert v1.client.delete(f"/v1/jobs/{job_id}").status_code == 200


def test_purge_unknown_and_mail_job_404(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        mail = v1.jobs.enqueue(new_upload_job()).job
        for job_id in ("f" * 32, mail.id, "not-an-id"):
            r = v1.client.delete(f"/v1/jobs/{job_id}")
            assert (r.status_code, r.json()) == (404, {"detail": "job not found"})


def test_purge_checkpoint_busy_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with v1_env(tmp_path) as v1:
        job_id = v1.submit()
        v1.run()
        monkeypatch.setattr(Database, "checkpoint", lambda self: False)
        assert v1.client.delete(f"/v1/jobs/{job_id}").status_code == 200


# -- the transcript route --------------------------------------------------------------------


def _done_env(v1: V1Env) -> str:
    job_id = v1.submit(language="en")
    v1.run()
    return job_id


def test_v1_transcript_states(tmp_path: Path) -> None:
    with v1_env(tmp_path, segments=SEGMENTS) as v1:
        queued = v1.submit(payload=audio(10))
        r = v1.transcript(queued)
        assert (r.status_code, r.json()) == (
            409,
            {"detail": "this job has no transcript yet; it is queued"},
        )
        claim = v1.jobs.claim_next()
        assert claim is not None
        v1.jobs.set_stage(claim, "transcribe")
        r = v1.transcript(queued)
        assert r.status_code == 409
        assert r.json() == {
            "detail": "this job has no transcript yet; it is running (stage: transcribe)"
        }
        v1.jobs.release(claim)
        v1.run()
        assert v1.transcript(queued).status_code == 200

        failed = v1.submit(payload=audio(11))
        v1.fakes.transcriber.fail_next(PermanentError("x"))
        v1.run()
        r = v1.transcript(failed)
        assert (r.status_code, r.json()) == (409, {"detail": V1_FAILED})

        done = queued
        for path in (v1.data_dir / "transcripts").rglob("transcript.txt"):
            path.unlink()
        assert v1.transcript(done, "text").status_code == 410
        assert v1.transcript(done, "text").json() == {"detail": V1_GONE}
        for path in (v1.data_dir / "transcripts").rglob("transcript.json"):
            path.write_text("{not json")
        r = v1.transcript(done)
        assert (r.status_code, r.json()) == (500, {"detail": V1_UNREADABLE})
        for path in (v1.data_dir / "transcripts").rglob("transcript.json"):
            path.unlink()
        r = v1.transcript(done)
        assert (r.status_code, r.json()) == (410, {"detail": V1_GONE})

        bad = v1.transcript(done, "note")
        assert bad.status_code == 422
        assert bad.json() == {"detail": "format: Input should be 'json' or 'text'"}


def test_v1_transcript_json_shape(tmp_path: Path) -> None:
    with v1_env(tmp_path, segments=SEGMENTS) as v1:
        job_id = _done_env(v1)
        body = v1.transcript(job_id).json()
        assert list(body) == ["job_id", "language", "duration_s", "segments", "warnings"]
        assert body["job_id"] == job_id
        assert body["segments"] == [
            {"start": 0.0, "end": 4.2, "text": "Good morning, everyone."},
            {"start": 4.2, "end": 9.8, "text": "Let's start with the roadmap."},
        ]
        assert body["warnings"] == []
        explicit = v1.transcript(job_id, "json")
        assert explicit.json() == body
        assert explicit.headers["content-type"] == "application/json"
        assert "model" not in explicit.text
        assert "fake-asr" not in explicit.text


def test_v1_transcript_text_bytes(tmp_path: Path) -> None:
    with v1_env(tmp_path, segments=SEGMENTS) as v1:
        job_id = _done_env(v1)
        r = v1.transcript(job_id, "text")
        assert r.content == DONE_TEXT.encode()
        [stored] = (v1.data_dir / "transcripts").rglob("transcript.txt")
        assert r.content == stored.read_bytes()
        assert r.headers["content-type"] == "text/plain; charset=utf-8"


def test_v1_transcript_no_store(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        job_id = _done_env(v1)
        assert v1.transcript(job_id).headers["cache-control"] == "no-store"
        assert v1.transcript(job_id, "text").headers["cache-control"] == "no-store"
        assert v1.client.get(f"/v1/jobs/{job_id}").headers["cache-control"] == "no-store"


# -- visibility ------------------------------------------------------------------------------


def test_v1_routes_hide_mail_jobs(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        mail = v1.jobs.enqueue(new_upload_job()).job
        for call in (
            v1.client.get(f"/v1/jobs/{mail.id}"),
            v1.client.get(f"/v1/jobs/{mail.id}/transcript"),
            v1.client.delete(f"/v1/jobs/{mail.id}"),
        ):
            assert (call.status_code, call.json()) == (404, {"detail": "job not found"})


def test_unversioned_transcript_refuses_v1_job(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        job_id = _done_env(v1)
        for fmt in ("note", "text", "json"):
            r = v1.client.get(f"/jobs/{job_id}/transcript?format={fmt}")
            assert r.status_code == 409
            assert r.json() == {
                "detail": "this job was sent to /v1; fetch its transcript from "
                "/v1/jobs/{id}/transcript"
            }


def test_v1_job_view_fields(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        job_id = v1.submit(language="de")
        queued = v1.client.get(f"/v1/jobs/{job_id}").json()
        assert list(queued) == [
            "job_id",
            "kind",
            "status",
            "stage",
            "error",
            "created_at",
            "updated_at",
            "language",
            "duration_s",
            "purged_at",
        ]
        assert queued["language"] == "de"
        assert queued["duration_s"] is None
        assert queued["status"] == "queued"
        v1.run()
        done = v1.client.get(f"/v1/jobs/{job_id}").json()
        assert done["language"] == "de"  # the fake reports the requested language
        assert done["duration_s"] == 15.0
        assert done["status"] == "done"
        auto = v1.submit(payload=audio(9))
        v1.run()
        assert v1.client.get(f"/v1/jobs/{auto}").json()["language"] == "en"  # the fake's own
        assert json.dumps(done)  # serialisable


def test_v1_job_view_hides_what_it_does_not_name(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        job_id = v1.submit(initial_prompt="SECRET-PROMPT-1")
        assert "SECRET-PROMPT-1" not in v1.client.get(f"/v1/jobs/{job_id}").text


# -- summary jobs (step 37) ----------------------------------------------------------------


def _summary_done(v1: SummaryEnv, **kwargs: str) -> str:
    job_id = v1.submit_summary(**kwargs)
    v1.run()
    return job_id


def test_purge_summary_job(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        job_id = _summary_done(v1)
        assert (v1.data_dir / "summaries" / job_id / "summary.json").is_file()
        deleted = v1.client.delete(f"/v1/jobs/{job_id}")
        assert deleted.status_code == 200
        body = deleted.json()
        assert body["purged_at"] is not None
        assert body["kind"] == "summary"
        assert not (v1.data_dir / "summaries" / job_id).exists()
        assert v1.uploads == []
        gone = v1.summary(job_id)
        assert (gone.status_code, gone.json()) == (410, {"detail": SUMMARY_PURGED})
        again = v1.client.delete(f"/v1/jobs/{job_id}")
        assert (again.status_code, again.json()) == (200, body)


@pytest.mark.parametrize("state", ["queued", "running"])
def test_purge_summary_refuses_active(tmp_path: Path, state: str) -> None:
    with summary_env(tmp_path) as v1:
        job_id = v1.submit_summary()
        if state == "running":
            assert v1.jobs.claim_next() is not None
        denied = v1.client.delete(f"/v1/jobs/{job_id}")
        assert denied.status_code == 409
        assert denied.json() == {
            "detail": f"this job is still {state}; delete it once it has finished"
        }
        assert len(v1.uploads) == 1
        assert v1.client.get(f"/v1/jobs/{job_id}").json()["purged_at"] is None


def test_purge_summary_only_that_job(tmp_path: Path) -> None:
    with summary_env(tmp_path, no_dedupe=True) as v1:
        a = _summary_done(v1)
        b = _summary_done(v1)
        assert a != b  # the same input, window 0
        assert v1.client.delete(f"/v1/jobs/{a}").status_code == 200
        assert not (v1.data_dir / "summaries" / a).exists()
        assert (v1.data_dir / "summaries" / b / "summary.json").is_file()
        assert v1.client.get(f"/v1/jobs/{b}").json()["purged_at"] is None
        assert v1.summary(b).status_code == 200


def test_purge_summary_files_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with summary_env(tmp_path) as v1:
        job_id = _summary_done(v1)
        real = ArtifactStore.remove_summary

        def broken(self: ArtifactStore, job: str) -> int:
            raise OSError("disk says no")

        monkeypatch.setattr(ArtifactStore, "remove_summary", broken)
        failed = v1.client.delete(f"/v1/jobs/{job_id}")
        assert failed.status_code == 500
        assert failed.json() == {"detail": "could not delete the summary files; try again"}
        assert v1.client.get(f"/v1/jobs/{job_id}").json()["purged_at"] is None
        assert v1.summary(job_id).status_code == 200
        monkeypatch.setattr(ArtifactStore, "remove_summary", real)
        assert v1.client.delete(f"/v1/jobs/{job_id}").status_code == 200


def test_purge_summary_leaves_only_the_row(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        job_id = _summary_done(v1)
        assert v1.client.delete(f"/v1/jobs/{job_id}").status_code == 200
        assert v1.files() == []
        assert set(_files(v1.data_dir)) <= {"earmark.db", "earmark.db-wal", "earmark.db-shm"}
        assert "earmark.db" in _files(v1.data_dir)


def test_purge_failed_summary_job(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        job_id = v1.submit_summary()
        v1.fakes.summarizer.fail_next(PermanentError("nope"))
        v1.run()
        deleted = v1.client.delete(f"/v1/jobs/{job_id}")
        assert deleted.status_code == 200
        assert deleted.json()["status"] == "failed"
        assert deleted.json()["purged_at"] is not None


def test_purge_leaves_a_transcription_untouched_by_a_summary_purge(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        audio_job = v1.submit()
        v1.run()
        summary_job = _summary_done(v1)
        assert v1.client.delete(f"/v1/jobs/{summary_job}").status_code == 200
        assert v1.transcript(audio_job).status_code == 200
