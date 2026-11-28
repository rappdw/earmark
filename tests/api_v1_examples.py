"""The /v1 responses of contract/api-v1.md, built from fixed inputs by the real app.

``build_examples`` is the one source of the contract's examples: the contract test compares
each fenced block to what it returns. To change an example, call it from a scratch test and
paste the bodies into the contract.
"""

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from earmark.models import Segment
from earmark.pipeline.stages import Backends
from tests.multipart_helpers import audio
from tests.pipeline_env import BlockingTranscriber, PipelineEnv
from tests.v1_env import v1_env

EXAMPLE_NAMES = (
    "accepted",
    "job-queued",
    "job-done",
    "transcript-json",
    "transcript-text",
    "deleted",
    "error",
)
START = datetime(2026, 11, 28, 9, 0, tzinfo=UTC)
JOB_ID = "3f9c2a7e5b1d4c8e9a6f0b2d4e6a8c01"
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
        return replace(backends, transcriber=blocking)

    out: dict[str, str] = {}
    with v1_env(tmp_path, segments=SEGMENTS, backends_hook=hook) as v1:
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
    assert set(out) == set(EXAMPLE_NAMES)
    return out
