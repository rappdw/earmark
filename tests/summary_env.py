"""A wired /v1 app and worker with summaries on, over the fake summarizer."""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from earmark.fakes import FakeBackends, FakeSummarizer, make_fake_backends
from earmark.jobs import JobStore, NewJob
from earmark.models import Job, SummaryFields, SummaryInput, SummaryResult, summary_input_bytes
from earmark.staging import Staging
from tests.v1_env import V1Env, v1_env

TRANSCRIPT = "[00:00:00] **Alice:** Good morning, everyone.\n"
INSTRUCTIONS = "Summarise the decisions as a Markdown list."


class SummaryEnv(V1Env):
    """A V1Env with ``post_summary`` and ``summary`` helpers."""

    def post_summary(
        self,
        *,
        transcript: str = TRANSCRIPT,
        instructions: str = INSTRUCTIONS,
        **extra: Any,
    ) -> Any:
        body = json.dumps(
            {"transcript": transcript, "instructions": instructions, **extra}, ensure_ascii=False
        )
        return self.client.post(
            "/v1/summaries",
            content=body.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    def submit_summary(self, **kwargs: Any) -> str:
        response = self.post_summary(**kwargs)
        assert response.status_code == 202, response.text
        return str(response.json()["job_id"])

    def summary(self, job_id: str) -> Any:
        return self.client.get(f"/v1/jobs/{job_id}/summary")

    def files(self) -> list[str]:
        """Every file under the data directory except the database, as relative paths."""
        return sorted(
            str(p.relative_to(self.data_dir))
            for p in self.data_dir.rglob("*")
            if p.is_file() and not p.name.startswith("earmark.db")
        )


def summary_input(transcript: str = TRANSCRIPT, instructions: str = INSTRUCTIONS) -> SummaryInput:
    return SummaryInput(transcript=transcript, instructions=instructions)


@contextmanager
def summary_env(tmp_path: Path, *, backend: str = "fake", **kwargs: Any) -> Iterator[SummaryEnv]:
    overrides: dict[str, Any] = {"llm_backend": backend, "llm_model": "example-model"}
    overrides.update(kwargs.pop("settings_overrides", None) or {})
    with v1_env(tmp_path, settings_overrides=overrides, **kwargs) as base:
        yield SummaryEnv(base.env, base.client, base.data_dir)


def summary_fakes(tmp_path: Path, **summarizer: Any) -> FakeBackends:
    """The fakes of ``tmp_path/data`` with a scripted ``FakeSummarizer``."""
    fakes = make_fake_backends(tmp_path / "data")
    fakes.summarizer = FakeSummarizer(**summarizer)
    return fakes


class CallbackSummarizer:
    """Runs ``callback`` before delegating, so a test can act in the middle of the call."""

    backend = "fake"

    def __init__(self, inner: FakeSummarizer, callback: Callable[[], None]) -> None:
        self.inner = inner
        self.callback = callback
        self.model = inner.model
        self.max_output_tokens = inner.max_output_tokens

    def summarize(self, request: SummaryInput, /) -> SummaryResult:
        self.callback()
        return self.inner.summarize(request)


def stage_summary_job(
    jobs: JobStore,
    staging: Staging,
    *,
    transcript: str = TRANSCRIPT,
    instructions: str = INSTRUCTIONS,
) -> Job:
    """Stage a summary input and enqueue its job, as the API does."""
    staged = staging.stage_document(
        summary_input_bytes(SummaryFields(transcript=transcript, instructions=instructions)),
        "json",
    )
    return jobs.enqueue(
        NewJob(
            kind="summary",
            source="upload",
            to=None,
            upload_file=staged.name,
            content_sha256=staged.sha256,
            delivery="none",
        )
    ).job
