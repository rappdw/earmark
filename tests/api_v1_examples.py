"""The /v1 responses of contract/api-v1.md, built from fixed inputs by the real app.

``build_examples`` is the one source of the contract's examples: the contract test compares
each fenced block to what it returns. To change an example, call it from a scratch test and
paste the bodies into the contract.
"""

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from earmark.fakes import FakeSummarizer
from earmark.models import Segment
from earmark.pipeline.stages import Backends
from tests.multipart_helpers import audio
from tests.pipeline_env import BlockingTranscriber, PipelineEnv
from tests.summary_env import CallbackSummarizer, SummaryEnv, summary_env

EXAMPLE_NAMES = (
    "accepted",
    "job-queued",
    "job-done",
    "transcript-json",
    "transcript-text",
    "deleted",
    "error",
    "summary-accepted",
    "summary-error",
    "summary-job-done",
    "summary",
)
START = datetime(2026, 11, 28, 9, 0, tzinfo=UTC)
JOB_ID = "3f9c2a7e5b1d4c8e9a6f0b2d4e6a8c01"
SUMMARY_JOB_ID = "7b1e4d9a2c3f4e5a8b6c0d1e2f3a4b5c"
SUMMARY_START = datetime(2026, 11, 28, 10, 0, tzinfo=UTC)
SUMMARY_TEXT = "## Decisions\n\n- Ship the roadmap in March.\n"
SEGMENTS = (
    Segment(start=0.0, end=4.2, text="Good morning, everyone."),
    Segment(start=4.2, end=9.8, text="Let's start with the roadmap."),
)


def build_examples(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """name -> the response body, exactly as the app sent it."""
    monkeypatch.setattr("earmark.jobs.uuid.uuid4", lambda: uuid.UUID(JOB_ID))
    holder: list[PipelineEnv] = []

    def hook(backends: Backends) -> Backends:
        def advance() -> None:  # the env is built after this hook runs, so look it up late
            holder[0].clock.advance(600)

        blocking = BlockingTranscriber(backends.transcriber, advance)
        summarizer = FakeSummarizer(text=SUMMARY_TEXT, model="example-model")
        timed = CallbackSummarizer(summarizer, lambda: holder[0].clock.advance(120))
        return replace(backends, transcriber=blocking, summarizer=timed)

    out: dict[str, str] = {}
    with summary_env(tmp_path, segments=SEGMENTS, backends_hook=hook) as v1:
        holder.append(v1.env)
        v1.clock.now = START
        accepted = v1.post(payload=audio(64), language="en")
        assert accepted.status_code == 202
        out["accepted"] = accepted.text
        out["job-queued"] = v1.client.get(f"/v1/jobs/{JOB_ID}").text
        early = v1.transcript(JOB_ID)
        assert early.status_code == 409
        out["error"] = early.text
        v1.clock.advance(5)
        assert v1.run().status == "done"
        out["job-done"] = v1.client.get(f"/v1/jobs/{JOB_ID}").text
        out["transcript-json"] = v1.transcript(JOB_ID, "json").text
        out["transcript-text"] = v1.transcript(JOB_ID, "text").text
        v1.clock.advance(60)
        deleted = v1.client.delete(f"/v1/jobs/{JOB_ID}")
        assert deleted.status_code == 200
        out["deleted"] = deleted.text
        _summary_examples(v1, monkeypatch, out)
    assert set(out) == set(EXAMPLE_NAMES)
    return out


def _summary_examples(v1: SummaryEnv, monkeypatch: pytest.MonkeyPatch, out: dict[str, str]) -> None:
    monkeypatch.setattr("earmark.jobs.uuid.uuid4", lambda: uuid.UUID(SUMMARY_JOB_ID))
    v1.clock.now = SUMMARY_START
    accepted = v1.post_summary()
    assert accepted.status_code == 202
    out["summary-accepted"] = accepted.text
    early = v1.summary(SUMMARY_JOB_ID)
    assert early.status_code == 409
    out["summary-error"] = early.text
    v1.clock.advance(5)
    assert v1.run().status == "done"
    out["summary-job-done"] = v1.client.get(f"/v1/jobs/{SUMMARY_JOB_ID}").text
    out["summary"] = v1.summary(SUMMARY_JOB_ID).text


# The seven bodies of contract/api-v1.md as step 36 shipped it (version 1.0), frozen: JSON
# bodies parsed, the text body verbatim. The test of version 1.1 checks that each still holds,
# apart from the added `kind`.
STEP36_EXAMPLES: Final[dict[str, Any]] = {
    "accepted": {"job_id": "3f9c2a7e5b1d4c8e9a6f0b2d4e6a8c01", "status": "queued"},
    "job-queued": {
        "job_id": "3f9c2a7e5b1d4c8e9a6f0b2d4e6a8c01",
        "status": "queued",
        "stage": None,
        "error": None,
        "created_at": "2026-11-28T09:00:00Z",
        "updated_at": "2026-11-28T09:00:00Z",
        "language": "en",
        "duration_s": None,
        "purged_at": None,
    },
    "error": {"detail": "this job has no transcript yet; it is queued"},
    "job-done": {
        "job_id": "3f9c2a7e5b1d4c8e9a6f0b2d4e6a8c01",
        "status": "done",
        "stage": "transcribe",
        "error": None,
        "created_at": "2026-11-28T09:00:00Z",
        "updated_at": "2026-11-28T09:10:05Z",
        "language": "en",
        "duration_s": 9.8,
        "purged_at": None,
    },
    "transcript-json": {
        "job_id": "3f9c2a7e5b1d4c8e9a6f0b2d4e6a8c01",
        "language": "en",
        "duration_s": 9.8,
        "segments": [
            {"start": 0.0, "end": 4.2, "text": "Good morning, everyone."},
            {"start": 4.2, "end": 9.8, "text": "Let's start with the roadmap."},
        ],
        "warnings": [],
    },
    "transcript-text": (
        "[00:00:00] Good morning, everyone.\n[00:00:04] Let's start with the roadmap.\n"
    ),
    "deleted": {
        "job_id": "3f9c2a7e5b1d4c8e9a6f0b2d4e6a8c01",
        "status": "done",
        "stage": "transcribe",
        "error": None,
        "created_at": "2026-11-28T09:00:00Z",
        "updated_at": "2026-11-28T09:11:05Z",
        "language": "en",
        "duration_s": 9.8,
        "purged_at": "2026-11-28T09:11:05Z",
    },
}
