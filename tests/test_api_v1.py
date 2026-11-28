"""POST /v1/transcriptions and the jobs it makes, over the real app and the fakes."""

import json
from pathlib import Path
from typing import Any

import pytest

from earmark.errors import PermanentError, SkipEpisode, TransientError
from tests.asgi_helpers import call_asgi
from tests.multipart_helpers import Part, audio, content_type, multipart_body
from tests.v1_env import V1Env, v1_env

# -- 6.1: nothing leaves the Spark -----------------------------------------------------


def test_v1_job_done_without_mail_or_push(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        job_id = v1.submit(language="en")
        job = v1.run()
        assert (job.id, job.status, job.stage) == (job_id, "done", "transcribe")
        assert job.result is not None
        assert job.result.language == "en"
        assert job.result.duration_s > 0
        assert job.delivery_state is None
        assert v1.fakes.mailer.calls == []
        assert v1.fakes.notifier.calls == []
        assert not (v1.data_dir / "outbox").exists()
        assert v1.uploads == []
        shown = v1.client.get(f"/v1/jobs/{job_id}").json()
        assert shown["status"] == "done"
        assert shown["duration_s"] == job.result.duration_s


def test_v1_job_failed_without_mail_or_push(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        v1.fakes.transcriber.fail_next(PermanentError("no good"))
        job_id = v1.submit()
        job = v1.run()
        assert (job.id, job.status, job.error) == (job_id, "failed", "no good")
        assert v1.fakes.mailer.calls == []
        assert v1.uploads == []
        assert v1.transcript(job_id).json() == {"detail": "this job has no transcript; it failed"}


def test_v1_job_transient_exhausted_without_mail_or_push(tmp_path: Path) -> None:
    with v1_env(tmp_path, max_attempts=1) as v1:
        v1.fakes.transcriber.fail_next(TransientError("try later"))
        v1.submit()
        job = v1.run()
        assert job.status == "failed"
        assert v1.uploads == []


def test_v1_job_skipped_without_mail_or_push(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        v1.fakes.transcriber.fail_next(SkipEpisode("not for us"))
        job_id = v1.submit()
        job = v1.run()
        assert job.status == "skipped"
        assert v1.transcript(job_id).status_code == 409


def test_v1_ignores_recipient_allowlist(tmp_path: Path) -> None:
    with v1_env(tmp_path, forbid_delivery=False) as v1:
        assert v1.post().status_code == 202
        refused = v1.client.post(
            "/transcriptions",
            files={"file": ("a.flac", audio(), "audio/flac")},
            data={"to": "you@example.org"},
        )
        assert refused.status_code == 422


# -- 6.2: validation -------------------------------------------------------------------


@pytest.mark.parametrize("name", ["to", "title", "instructions", "timestamp", "other"])
def test_v1_rejects_field(tmp_path: Path, name: str) -> None:
    with v1_env(tmp_path) as v1:
        extra: dict[str, Any] = {name: "x"}
        r = v1.post(**extra)
        assert r.status_code == 422
        assert r.json() == {
            "detail": f"unknown field '{name}' (allowed: file, initial_prompt, language)"
        }
        assert v1.uploads == []


def test_v1_requires_file(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        body = multipart_body([Part("language", "en")])
        r = v1.client.post(
            "/v1/transcriptions", content=body, headers={"Content-Type": content_type()}
        )
        assert r.status_code == 422
        assert r.json() == {"detail": "the form must have exactly one file field named 'file'"}


def test_v1_rejects_two_files(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        parts = [Part("file", audio(), "a.flac", "audio/flac")] * 2
        r = v1.client.post(
            "/v1/transcriptions",
            content=multipart_body(parts),
            headers={"Content-Type": content_type()},
        )
        assert r.status_code == 422
        assert r.json() == {"detail": "the form must have exactly one file field named 'file'"}
        assert v1.uploads == []


def test_v1_rejects_empty_file(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        r = v1.post(payload=b"")
        assert r.status_code == 422
        assert r.json() == {"detail": "the uploaded file is empty"}
        assert v1.uploads == []


def test_v1_initial_prompt_cap(tmp_path: Path) -> None:
    with v1_env(tmp_path, no_dedupe=True) as v1:
        ok = v1.post(initial_prompt=" " + "a" * 1000 + " ")
        assert ok.status_code == 202
        stored = v1.jobs.get(ok.json()["job_id"])
        assert stored is not None
        assert stored.initial_prompt == "a" * 1000
        accents = v1.post(initial_prompt="é" * 1000)
        assert accents.status_code == 202
        too_long = v1.post(initial_prompt="a" * 1001)
        assert too_long.status_code == 422
        assert too_long.json() == {"detail": "initial_prompt is too long (at most 1000 characters)"}
        assert len(v1.uploads) == 2


def test_v1_language_rules(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        for given, stored in (("en", "en"), ("EN", "en"), (" yue ", "yue"), ("", None)):
            job_id = v1.submit(language=given, payload=audio(10 + len(given)))
            job = v1.jobs.get(job_id)
            assert job is not None
            assert job.language == stored
        for bad in ("xx", "english", "zh-CN", "auto"):
            r = v1.post(language=bad)
            assert r.status_code == 422
            assert r.json() == {
                "detail": "language must be a Whisper language code such as 'en'; "
                "leave it out to detect the language"
            }


def test_v1_rejects_json(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        r = v1.client.post("/v1/transcriptions", json={"url": "https://example.org/a.mp3"})
        assert r.status_code == 415
        assert r.json() == {
            "detail": "send a multipart form (Content-Type: multipart/form-data) with the "
            "audio in a field named 'file'"
        }


def test_v1_rejects_non_audio(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        r = v1.post(filename="notes.txt", content_type="text/plain")
        assert r.status_code == 415
        assert v1.uploads == []


def test_v1_upload_too_large(tmp_path: Path) -> None:
    with v1_env(tmp_path, max_upload_bytes=100) as v1:
        r = v1.post(payload=audio(500))
        assert r.status_code == 413
        assert "EARMARK_MAX_UPLOAD_MB" in r.json()["detail"]
        assert v1.uploads == []


V1_ROUTES = [
    ("POST", "/v1/transcriptions"),
    ("GET", f"/v1/jobs/{'a' * 32}"),
    ("GET", f"/v1/jobs/{'a' * 32}/transcript"),
    ("DELETE", f"/v1/jobs/{'a' * 32}"),
]


@pytest.mark.parametrize(("method", "path"), V1_ROUTES)
def test_v1_routes_need_token(tmp_path: Path, method: str, path: str) -> None:
    with v1_env(tmp_path) as v1:
        body = multipart_body([Part("file", audio(), "a.flac", "audio/flac")])
        for headers in ({}, {"Authorization": "Bearer wrong-token"}):
            result = call_asgi(
                v1.client.app,
                method=method,
                path=path,
                headers={"Content-Type": content_type(), **headers},
                chunks=[body],
            )
            assert result.status == 401
            assert result.receive_calls == 0  # the body is never read
        assert v1.uploads == []


def test_v1_accepted_shape(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        r = v1.post()
        assert r.status_code == 202
        body = r.json()
        assert set(body) == {"job_id", "status"}
        assert body["status"] == "queued"
        assert r.headers["Location"] == f"/v1/jobs/{body['job_id']}"


# -- 6.4: per-options transcripts --------------------------------------------------------


def _decoded_dirs(v1: V1Env) -> list[str]:
    root = v1.data_dir / "transcripts"
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


def test_v1_prompts_get_their_own_transcripts(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        a = v1.submit(initial_prompt="Alice and Bob")
        b = v1.submit(initial_prompt="Kubernetes")
        v1.run_all()
        ta = v1.transcript(a).json()
        tb = v1.transcript(b).json()
        prompt_a = [s["text"] for s in ta["segments"] if s["text"].startswith("Prompt ")]
        prompt_b = [s["text"] for s in tb["segments"] if s["text"].startswith("Prompt ")]
        assert len(prompt_a) == len(prompt_b) == 1
        assert prompt_a != prompt_b
        assert len(_decoded_dirs(v1)) == 2
        assert v1.fakes.transcriber.computed == 2
        assert "Alice" not in json.dumps(ta)


def test_v1_language_gets_its_own_transcript(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        plain = v1.submit()
        german = v1.submit(language="de")
        v1.run_all()
        assert len(_decoded_dirs(v1)) == 2
        assert v1.transcript(german).json()["language"] == "de"
        assert v1.transcript(plain).json()["language"] == "en"  # the fake's default


def test_v1_identical_resend_reuses_cache(tmp_path: Path) -> None:
    with v1_env(tmp_path, no_dedupe=True) as v1:
        first = v1.submit(initial_prompt="Alice")
        v1.run()
        second = v1.submit(initial_prompt="Alice")
        assert second != first
        assert v1.run().status == "done"
        assert v1.fakes.transcriber.computed == 1
        one = v1.transcript(first).content
        two = v1.transcript(second).content
        assert one.replace(first.encode(), b"") == two.replace(second.encode(), b"")


def test_v1_identical_resend_within_window_returns_same_job(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        first = v1.submit(initial_prompt="Alice")
        v1.run()
        again = v1.submit(initial_prompt="Alice")
        assert again == first
        assert v1.uploads == []


def test_v1_cache_isolated_from_mail_cache(tmp_path: Path) -> None:
    with v1_env(tmp_path, forbid_delivery=False, allowed_recipients=("you@example.org",)) as v1:
        mail = v1.client.post(
            "/transcriptions",
            files={"file": ("a.flac", audio(64), "audio/flac")},
            data={"to": "you@example.org"},
        )
        assert mail.status_code == 202
        v1.run()
        mail_files = list((v1.data_dir / "episodes").glob("*/transcript.json"))
        assert len(mail_files) == 1
        before = mail_files[0].read_bytes()
        v1.submit()
        assert v1.run().status == "done"
        assert v1.fakes.transcriber.computed == 2
        assert mail_files[0].read_bytes() == before
        assert len(_decoded_dirs(v1)) == 1


def test_v1_job_listed_by_unversioned_api(tmp_path: Path) -> None:
    with v1_env(tmp_path) as v1:
        job_id = v1.submit()
        listed = v1.client.get("/jobs").json()["jobs"]
        assert [j["id"] for j in listed] == [job_id]
        shown = v1.client.get(f"/jobs/{job_id}").json()
        assert (shown["source"], shown["to"], shown["title"]) == ("upload", None, None)
