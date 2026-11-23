"""Response models for the HTTP API."""

from typing import Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from earmark.log import scrub_urls
from earmark.models import (
    DeliveryState,
    EpisodeKey,
    Job,
    JobId,
    JobResult,
    JobStatus,
    Source,
    Stage,
)
from earmark.sources import display_url

VIEW_INSTRUCTIONS_CHARS: Final = 200


class ApiModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TranscriptionResponse(ApiModel):
    job_id: JobId
    status: JobStatus
    created: bool


class JobView(ApiModel):
    """What the API (and ``earmark jobs``) shows of a job. Never the raw ``Job``."""

    id: JobId
    status: JobStatus
    stage: Stage | None
    source: Source
    url: str | None  # apple: canonical; url: display_url(job.url); upload: None
    to: str | None  # in full
    instructions: str  # at most 200 characters
    title: str | None
    episode_key: EpisodeKey | None
    attempts: int
    run_after: AwareDatetime
    error: str | None  # scrub_urls(job.error)
    warnings: tuple[str, ...]  # scrub_urls(each)
    result: JobResult | None
    delivery_state: DeliveryState | None
    sent_at: AwareDatetime | None
    shared_at: AwareDatetime
    created_at: AwareDatetime
    updated_at: AwareDatetime


def _shorten_instructions(text: str) -> str:
    if len(text) <= VIEW_INSTRUCTIONS_CHARS:
        return text
    return text[: VIEW_INSTRUCTIONS_CHARS - 1] + "…"


def job_view(job: Job) -> JobView:
    url: str | None
    if job.url is None:
        url = None
    elif job.source == "url":
        url = display_url(job.url)
    else:
        url = job.url
    return JobView(
        id=job.id,
        status=job.status,
        stage=job.stage,
        source=job.source,
        url=url,
        to=job.to,
        instructions=_shorten_instructions(job.instructions),
        title=job.title,
        episode_key=job.episode_key,
        attempts=job.attempts,
        run_after=job.run_after,
        error=None if job.error is None else scrub_urls(job.error),
        warnings=tuple(scrub_urls(w) for w in job.warnings),
        result=job.result,
        delivery_state=job.delivery_state,
        sent_at=job.sent_at,
        shared_at=job.shared_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


class JobList(ApiModel):
    jobs: tuple[JobView, ...]


class Health(ApiModel):
    status: Literal["ok"]
