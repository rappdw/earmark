"""POST /v1/summaries and the summary job, end to end over the fake summarizer."""

import json
import socket
from pathlib import Path
from typing import Any

import httpx2
import pytest

from earmark.errors import PermanentError, TransientError
from earmark.v1_jobs import (
    NOT_A_SUMMARY,
    NOT_A_TRANSCRIPTION,
    SUMMARIES_OFF,
    SUMMARY_BODY_TOO_LARGE,
    SUMMARY_GONE,
    SUMMARY_MISSING_BODY,
    SUMMARY_ONLY_JSON,
    SUMMARY_PURGED,
    SUMMARY_UNREADABLE,
    TRANSCRIPT_TOO_LONG,
)
from tests.asgi_helpers import call_asgi
from tests.summary_env import INSTRUCTIONS, TRANSCRIPT, SummaryEnv, summary_env, summary_fakes
from tests.v1_env import AUTH, CalledError

TRICKY = '## Notes\r\n\n- café \U0001f600 <think>x</think>\n```\n{"a": 1}\n```\n  '


def _status(v1: SummaryEnv, job_id: str) -> dict[str, Any]:
    response = v1.client.get(f"/v1/jobs/{job_id}")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


# -- end to end ----------------------------------------------------------------------------


def test_summary_end_to_end(tmp_path: Path) -> None:
    fakes = summary_fakes(tmp_path, text=TRICKY)
    transcript = "  [00:00:00] **Alice:** Hello \u2028 world\r\n  "
    instructions = "\tSummarise.  \n"
    with summary_env(tmp_path, fakes=fakes) as v1:
        accepted = v1.post_summary(transcript=transcript, instructions=instructions)
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        assert accepted.json() == {"job_id": job_id, "status": "queued"}
        assert accepted.headers["Location"] == f"/v1/jobs/{job_id}"
        assert v1.run().id == job_id
        shown = _status(v1, job_id)
        assert shown["kind"] == "summary"
        assert shown["status"] == "done"
        assert shown["stage"] == "summarize"
        assert shown["language"] is None
        assert shown["duration_s"] is None
        assert shown["error"] is None
        summary = v1.summary(job_id)
        assert summary.status_code == 200
        assert summary.headers["Cache-Control"] == "no-store"
        assert summary.headers["content-type"] == "application/json"
        body = summary.json()
        assert body == {"job_id": job_id, "model": "fake-llm", "text": TRICKY, "warnings": []}
        assert summary.text.endswith("}\n")
        assert [(c.transcript_chars, c.instructions_chars) for c in fakes.summarizer.calls] == [
            (len(transcript), len(instructions))
        ]
        # the mailer and the notifier are ForbiddenMailer and ForbiddenNotifier
        assert not (v1.data_dir / "outbox").exists()


def test_summary_text_is_served_byte_for_byte_with_lone_surrogates_replaced(
    tmp_path: Path,
) -> None:
    fakes = summary_fakes(tmp_path, text="a\ud800b")
    with summary_env(tmp_path, fakes=fakes) as v1:
        job_id = v1.submit_summary()
        v1.run()
        assert v1.summary(job_id).json()["text"] == "a�b"


@pytest.mark.parametrize("failure", ["permanent", "transient", "stale"])
def test_summary_failure_never_mails_or_pushes(tmp_path: Path, failure: str) -> None:
    with summary_env(tmp_path, max_attempts=1) as v1:
        job_id = v1.submit_summary()
        if failure == "permanent":
            v1.fakes.summarizer.fail_next(PermanentError("nope"))
            assert v1.run().status == "failed"
        elif failure == "transient":
            v1.fakes.summarizer.fail_next(TransientError("later"))
            assert v1.run().status == "failed"  # max_attempts=1
        else:
            claim = v1.jobs.claim_next()
            assert claim is not None
            v1.clock.advance(901)
            for job in v1.jobs.requeue_stale():
                v1.env.pipeline.finish(job, None)
        shown = _status(v1, job_id)
        assert shown["status"] == "failed"
        assert shown["error"]
        assert v1.summary(job_id).status_code == 409
        assert not (v1.data_dir / "outbox").exists()


@pytest.mark.parametrize("outcome", ["done", "failed"])
def test_summary_input_deleted_when_job_ends(tmp_path: Path, outcome: str) -> None:
    with summary_env(tmp_path) as v1:
        job_id = v1.submit_summary()
        assert len(v1.uploads) == 1
        if outcome == "failed":
            v1.fakes.summarizer.fail_next(PermanentError("nope"))
        v1.run()
        assert v1.uploads == []
        assert (v1.data_dir / "summaries" / job_id / "summary.json").is_file() == (
            outcome == "done"
        )


def test_summary_transient_failure_keeps_the_input_for_the_retry(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        job_id = v1.submit_summary()
        v1.fakes.summarizer.fail_next(TransientError("later"))
        assert v1.run().status == "queued"
        assert len(v1.uploads) == 1
        v1.clock.advance(3600)
        assert v1.run().status == "done"
        assert v1.summary(job_id).status_code == 200
        assert v1.uploads == []


def test_summary_input_file_is_private(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        v1.submit_summary()
        [name] = v1.uploads
        assert name.endswith(".json")
        assert (v1.data_dir / "uploads" / name).stat().st_mode & 0o777 == 0o600


def test_summary_written_with_private_modes(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        job_id = v1.submit_summary()
        v1.run()
        directory = v1.data_dir / "summaries" / job_id
        assert directory.stat().st_mode & 0o777 == 0o700
        assert (directory / "summary.json").stat().st_mode & 0o777 == 0o600


# -- caps and the handler ------------------------------------------------------------------


def test_transcript_over_cap_is_422(tmp_path: Path) -> None:
    with summary_env(tmp_path, settings_overrides={"llm_max_transcript_chars": 1000}) as v1:
        ok = v1.post_summary(transcript="é" * 1000)
        assert ok.status_code == 202
        too_long = v1.post_summary(transcript="é" * 1001)
        assert too_long.status_code == 422
        assert too_long.json() == {"detail": TRANSCRIPT_TOO_LONG.format(n=1000)}
        assert "at most 1000 characters" in too_long.json()["detail"]
        assert len(v1.uploads) == 1  # only the accepted one


def test_transcript_cap_default_is_400000(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        response = v1.post_summary(transcript="x" * 400_001)
        assert response.status_code == 422
        assert "at most 400000 characters" in response.json()["detail"]
        assert v1.uploads == []


def test_summary_handler_makes_no_outbound_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hits: list[str] = []

    def refuse(*args: object, **kwargs: object) -> None:
        hits.append("called")
        raise CalledError("an outbound request was attempted")

    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    with summary_env(tmp_path, settings_overrides={"llm_max_transcript_chars": 1000}) as v1:
        assert v1.post_summary(transcript="x" * 1001).status_code == 422
        assert v1.post_summary().status_code == 202
    assert hits == []


def test_instructions_cap(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        assert v1.post_summary(instructions="é" * 20_000).status_code == 202
        refused = v1.post_summary(instructions="é" * 20_001)
        assert refused.status_code == 422
        assert refused.json() == {"detail": "instructions is too long (at most 20000 characters)"}


def _body_limit(chars: int) -> int:
    return 12 * (chars + 20_000) + 64 * 1024


def test_summary_body_too_large_is_413(tmp_path: Path) -> None:
    limit = _body_limit(1000)
    detail = SUMMARY_BODY_TOO_LARGE.format(limit=f"{limit} bytes")
    with summary_env(tmp_path, settings_overrides={"llm_max_transcript_chars": 1000}) as v1:
        headers = {**AUTH, "Content-Type": "application/json"}
        declared = call_asgi(
            v1.client.app,
            path="/v1/summaries",
            headers={**headers, "Content-Length": str(limit + 1)},
            chunks=[b"{"],
        )
        assert declared.status == 413
        assert declared.json() == {"detail": detail}
        assert declared.receive_calls == 0
        streamed = call_asgi(
            v1.client.app,
            path="/v1/summaries",
            headers=headers,
            chunks=[b"x" * limit, b"x"],
        )
        assert streamed.status == 413
        assert streamed.json() == {"detail": detail}
        assert v1.uploads == []


def _raw(v1: SummaryEnv, body: bytes, content_type: str = "application/json") -> Any:
    return v1.client.post("/v1/summaries", content=body, headers={"Content-Type": content_type})


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        (
            {"transcript": "t", "instructions": "i", "to": "a@example.org"},
            422,
            "unknown field 'to' (allowed: transcript, instructions)",
        ),
        (
            {"transcript": "t", "instructions": "i", "model": "x"},
            422,
            "unknown field 'model' (allowed: transcript, instructions)",
        ),
        (
            {"transcript": "t", "instructions": "i", "max_tokens": 5},
            422,
            "unknown field 'max_tokens' (allowed: transcript, instructions)",
        ),
        ({"instructions": "i"}, 422, "transcript is required"),
        ({"transcript": "t"}, 422, "instructions is required"),
        ({"transcript": "  \n", "instructions": "i"}, 422, "transcript must not be empty"),
        ({"transcript": "t", "instructions": ""}, 422, "instructions must not be empty"),
        (
            {"transcript": 5, "instructions": "i"},
            422,
            "transcript: Input should be a valid string",
        ),
        (
            {"transcript": "t", "instructions": None},
            422,
            "instructions: Input should be a valid string",
        ),
    ],
)
def test_summary_rejects_fields(
    tmp_path: Path, body: dict[str, Any], status: int, detail: str
) -> None:
    with summary_env(tmp_path) as v1:
        response = _raw(v1, json.dumps(body).encode())
        assert (response.status_code, response.json()) == (status, {"detail": detail})
        assert v1.uploads == []


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (b"{", "request body is not valid JSON"),
        (b"[]", "request body must be a JSON object sent with Content-Type: application/json"),
        (b'"text"', "request body must be a JSON object sent with Content-Type: application/json"),
        (b"", SUMMARY_MISSING_BODY),
    ],
)
def test_summary_rejects_bodies(tmp_path: Path, body: bytes, detail: str) -> None:
    with summary_env(tmp_path) as v1:
        response = _raw(v1, body)
        assert (response.status_code, response.json()) == (422, {"detail": detail})


@pytest.mark.parametrize("content_type", ["text/plain", "multipart/form-data", ""])
def test_summary_requires_json_media_type(tmp_path: Path, content_type: str) -> None:
    with summary_env(tmp_path) as v1:
        response = v1.client.post(
            "/v1/summaries",
            content=json.dumps({"transcript": "t", "instructions": "i"}).encode(),
            headers={"Content-Type": content_type},
        )
        assert (response.status_code, response.json()) == (415, {"detail": SUMMARY_ONLY_JSON})
        assert v1.uploads == []


def test_summaries_off_is_503(tmp_path: Path) -> None:
    with summary_env(tmp_path, backend="none") as v1:
        headers = {**AUTH, "Content-Type": "application/json"}
        result = call_asgi(
            v1.client.app, path="/v1/summaries", headers=headers, chunks=[b'{"a": 1}']
        )
        assert result.status == 503
        assert result.json() == {"detail": SUMMARIES_OFF}
        assert result.receive_calls == 0  # the body was never read
        assert v1.uploads == []


def test_summary_needs_the_token(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        response = v1.client.post(
            "/v1/summaries",
            content=b"{}",
            headers={"Content-Type": "application/json", "Authorization": "Bearer wrong"},
        )
        assert response.status_code == 401


def test_summary_disconnect_is_400_without_a_job(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        result = call_asgi(
            v1.client.app,
            path="/v1/summaries",
            headers={**AUTH, "Content-Type": "application/json"},
            chunks=[b'{"transcript": "t", ', b'"instructions": "i"}'],
            disconnect_after=1,
        )
        assert result.status == 400
        assert v1.uploads == []
        assert v1.run_all() == []


# -- dedupe --------------------------------------------------------------------------------


def test_summary_identical_within_window_same_job(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        first = v1.post_summary().json()["job_id"]
        second = v1.post_summary().json()["job_id"]
        assert first == second
        assert len(v1.uploads) == 1  # the second staged copy is deleted at once
        v1.run()
        assert v1.post_summary().json()["job_id"] == first  # done, within the window
        assert v1.uploads == []


def test_summary_different_input_is_another_job(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        a = v1.submit_summary()
        b = v1.submit_summary(instructions=INSTRUCTIONS + " ")
        c = v1.submit_summary(transcript=TRANSCRIPT + " ")
        assert len({a, b, c}) == 3


def test_summary_after_window_new_job(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        first = v1.submit_summary()
        v1.run()
        v1.clock.advance(601)
        assert v1.submit_summary() != first


def test_summary_failed_not_deduped(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        first = v1.submit_summary()
        v1.fakes.summarizer.fail_next(PermanentError("nope"))
        v1.run()
        assert v1.submit_summary() != first


def test_summary_dedupe_key_distinct_from_mail_and_transcription(tmp_path: Path) -> None:
    from earmark.jobs import _dedupe_key, _summary_dedupe_key, _v1_dedupe_key

    sha = "ab" * 32
    keys = {
        _summary_dedupe_key(sha),
        _v1_dedupe_key(sha, None, None),
        _dedupe_key("ingest", "upload", "sha256:" + sha, "a@example.org", ""),
    }
    assert len(keys) == 3


# -- kind ----------------------------------------------------------------------------------


def test_kind_in_v1_view(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        audio_job = v1.submit()
        summary_job = v1.submit_summary()
        assert _status(v1, audio_job)["kind"] == "transcription"
        assert _status(v1, summary_job)["kind"] == "summary"
        v1.run_all()
        assert _status(v1, audio_job)["kind"] == "transcription"
        assert _status(v1, summary_job)["kind"] == "summary"
        for job_id in (audio_job, summary_job):
            assert v1.client.delete(f"/v1/jobs/{job_id}").status_code == 200
        assert _status(v1, audio_job)["kind"] == "transcription"
        shown = _status(v1, summary_job)
        assert shown["kind"] == "summary"
        assert shown["purged_at"] is not None


def test_kind_in_unversioned_view(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        audio_job = v1.submit()
        summary_job = v1.submit_summary()
        listed = {j["id"]: j for j in v1.client.get("/jobs").json()["jobs"]}
        assert listed[audio_job]["kind"] == "ingest"
        assert listed[summary_job]["kind"] == "summary"
        assert listed[summary_job]["source"] == "upload"
        assert listed[summary_job]["to"] is None
        assert listed[summary_job]["instructions"] == ""


def test_summary_route_on_transcription_job(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        job_id = v1.submit()
        for _ in range(2):  # before and after the job is done
            response = v1.summary(job_id)
            assert (response.status_code, response.json()) == (409, {"detail": NOT_A_SUMMARY})
            v1.run_all()
        assert "{job_id}" in NOT_A_SUMMARY  # the text names the route with a literal


@pytest.mark.parametrize("fmt", ["json", "text"])
@pytest.mark.parametrize("purged", [False, True])
def test_transcript_route_on_summary_job(tmp_path: Path, fmt: str, purged: bool) -> None:
    with summary_env(tmp_path) as v1:
        job_id = v1.submit_summary()
        v1.run()
        if purged:
            assert v1.client.delete(f"/v1/jobs/{job_id}").status_code == 200
        response = v1.transcript(job_id, fmt)
        assert (response.status_code, response.json()) == (
            409,
            {"detail": NOT_A_TRANSCRIPTION},
        )


def test_transcript_route_on_queued_summary_job_is_409_wrong_resource(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        job_id = v1.submit_summary()
        assert v1.transcript(job_id).json() == {"detail": NOT_A_TRANSCRIPTION}


def test_unversioned_transcript_refuses_summary(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        job_id = v1.submit_summary()
        v1.run()
        for fmt in ("note", "text", "json"):
            response = v1.client.get(f"/jobs/{job_id}/transcript?format={fmt}")
            assert response.status_code == 409
            assert response.json() == {
                "detail": "this job is a summary; fetch it from /v1/jobs/{id}/summary"
            }


def test_mail_job_hidden_from_summary_route(tmp_path: Path) -> None:
    from earmark.jobs import NewJob

    with summary_env(tmp_path) as v1:
        job = v1.jobs.enqueue(
            NewJob(
                source="apple_podcasts", to="you@example.org", url="https://podcasts.apple.com/x"
            )
        ).job
        response = v1.summary(job.id)
        assert (response.status_code, response.json()) == (404, {"detail": "job not found"})
        assert v1.summary("0" * 32).status_code == 404


def test_summary_states(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        job_id = v1.submit_summary()
        queued = v1.summary(job_id)
        assert (queued.status_code, queued.json()) == (
            409,
            {"detail": "this job has no summary yet; it is queued"},
        )
        claim = v1.jobs.claim_next()
        assert claim is not None
        v1.jobs.set_stage(claim, "summarize")
        running = v1.summary(job_id)
        assert running.status_code == 409
        assert running.json() == {
            "detail": "this job has no summary yet; it is running (stage: summarize)"
        }
        v1.jobs.release(claim)
        v1.run()
        assert v1.summary(job_id).status_code == 200
        path = v1.data_dir / "summaries" / job_id / "summary.json"
        path.write_text("{ not json")
        corrupt = v1.summary(job_id)
        assert (corrupt.status_code, corrupt.json()) == (500, {"detail": SUMMARY_UNREADABLE})
        path.unlink()
        gone = v1.summary(job_id)
        assert (gone.status_code, gone.json()) == (410, {"detail": SUMMARY_GONE})
        v1.clock.advance(601)
        other = v1.submit_summary()
        v1.fakes.summarizer.fail_next(PermanentError("nope"))
        v1.run()
        failed = v1.summary(other)
        assert (failed.status_code, failed.json()) == (
            409,
            {"detail": "this job has no summary; it failed"},
        )
        assert v1.client.delete(f"/v1/jobs/{job_id}").status_code == 200
        purged = v1.summary(job_id)
        assert (purged.status_code, purged.json()) == (410, {"detail": SUMMARY_PURGED})


def test_summary_skipped_state(tmp_path: Path) -> None:
    from earmark.errors import SkipEpisode

    with summary_env(tmp_path) as v1:
        job_id = v1.submit_summary()
        v1.fakes.summarizer.fail_next(SkipEpisode("not today"))
        assert v1.run().status == "skipped"
        response = v1.summary(job_id)
        assert (response.status_code, response.json()) == (
            409,
            {"detail": "this job has no summary; it was skipped"},
        )


def test_summary_view_shape(tmp_path: Path) -> None:
    with summary_env(tmp_path) as v1:
        job_id = v1.submit_summary()
        v1.run()
        assert list(v1.summary(job_id).json()) == ["job_id", "model", "text", "warnings"]
        assert list(_status(v1, job_id)) == [
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


def test_summary_failed_job_error_is_earmarks_own_text(tmp_path: Path) -> None:
    with summary_env(tmp_path, fakes=summary_fakes(tmp_path, finish_reason="length")) as v1:
        job_id = v1.submit_summary()
        v1.run()
        shown = _status(v1, job_id)
        assert shown["status"] == "failed"
        assert shown["error"].startswith("the summary was cut off")
        assert "EARMARK_LLM_MAX_OUTPUT_TOKENS" in shown["error"]
        assert not (v1.data_dir / "summaries" / job_id).exists()
