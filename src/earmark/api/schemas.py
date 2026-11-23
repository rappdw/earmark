"""Response models for the HTTP API."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from earmark.models import Job, JobId, JobStatus


class ApiModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class IngestResponse(ApiModel):
    job_id: JobId
    status: JobStatus
    created: bool


class JobList(ApiModel):
    jobs: tuple[Job, ...]


class Health(ApiModel):
    status: Literal["ok"]
