"""The /v1 routes: transcription for programs, with no delivery.

``build_router`` includes this router, so every path is behind the bearer middleware. A
/v1 job never sends mail or a push, and /v1 shows only its own jobs: a mail job's id is 404.
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Final

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from earmark.api.schemas import V1Accepted, V1JobView, v1_job_view
from earmark.api.upload import UploadRejected, log_rejection, receive_upload
from earmark.artifacts import ArtifactStore
from earmark.fetch import media_type
from earmark.jobs import Enqueued, JobActiveError, JobNotFoundError
from earmark.models import V1_FORM_FIELDS, V1UploadFields
from earmark.staging import Staging
from earmark.submission import Intake
from earmark.transcripts import JOB_NOT_FOUND, TranscriptUnavailable
from earmark.v1_jobs import (
    DELETE_FAILED,
    STILL_ACTIVE,
    V1_ONLY_MULTIPART,
    V1TranscriptFormat,
    purge_job,
    read_v1_transcript,
    v1_job,
)

logger = logging.getLogger(__name__)

_UNAVAILABLE_STATUS: Final = {"not_ready": 409, "gone": 410, "unreadable": 500}
_NO_STORE: Final = {"Cache-Control": "no-store"}


def _validation_detail(exc: ValidationError, body_fields: Sequence[str]) -> str:
    from earmark.api.app import describe_validation_errors

    errors: list[Mapping[str, Any]] = [
        {**e, "loc": ("body", *e.get("loc", ()))}
        for e in exc.errors(include_input=False, include_url=False)
    ]
    return describe_validation_errors(errors, body_fields=body_fields)


def build_v1_router(
    intake: Intake, *, artifacts: ArtifactStore, max_upload_bytes: int
) -> APIRouter:
    """A standalone router holding the /v1 routes (full paths, ``/v1/...``)."""
    router = APIRouter()
    add_v1_routes(router, intake, artifacts=artifacts, max_upload_bytes=max_upload_bytes)
    return router


def add_v1_routes(
    router: APIRouter, intake: Intake, *, artifacts: ArtifactStore, max_upload_bytes: int
) -> None:
    """Register the /v1 routes on ``router`` itself. FastAPI's ``include_router`` keeps a
    nested router as one lazy entry, which hides the routes from ``router.routes``."""
    store = intake.store
    staging: Staging = intake.staging

    async def submit(request: Request) -> Enqueued:
        try:
            received = await receive_upload(
                request.headers,
                request.stream(),
                staging,
                max_file_bytes=max_upload_bytes,
                text_fields=V1_FORM_FIELDS,
            )
        except UploadRejected as exc:
            log_rejection(exc)
            raise HTTPException(status_code=exc.status, detail=exc.detail) from None
        writer = received.writer
        try:
            try:
                fields = V1UploadFields.model_validate(received.fields)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=422, detail=_validation_detail(exc, ("file", *V1_FORM_FIELDS))
                ) from None
            staged = await run_in_threadpool(writer.commit)
        except BaseException:
            await run_in_threadpool(writer.abort)
            raise
        return await run_in_threadpool(intake.submit_v1, fields, staged)

    @router.post("/v1/transcriptions", status_code=202)
    async def transcriptions(request: Request) -> Response:
        if media_type(request.headers.get("content-type")) != "multipart/form-data":
            raise HTTPException(status_code=415, detail=V1_ONLY_MULTIPART)
        try:
            enqueued = await submit(request)
        except ClientDisconnect:
            logger.info("client disconnected during the request")
            return Response(status_code=400)  # nobody is listening
        job = enqueued.job
        body = V1Accepted(job_id=job.id, status=job.status)
        return JSONResponse(
            body.model_dump(mode="json"),
            status_code=202,
            headers={"Location": f"/v1/jobs/{job.id}"},
        )

    @router.get("/v1/jobs/{job_id}")
    def get_job(job_id: str) -> JSONResponse:
        job = v1_job(store, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=JOB_NOT_FOUND)
        return JSONResponse(v1_job_view(job).model_dump(mode="json"), headers=_NO_STORE)

    @router.get("/v1/jobs/{job_id}/transcript")
    def get_transcript(
        job_id: str,
        fmt: Annotated[V1TranscriptFormat, Query(alias="format")] = "json",
    ) -> Response:
        job = v1_job(store, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=JOB_NOT_FOUND)
        try:
            found = read_v1_transcript(job, artifacts, fmt)
        except TranscriptUnavailable as exc:
            logger.info("job %s: no /v1 transcript as %s: %s", job.id, fmt, exc.reason)
            raise HTTPException(
                status_code=_UNAVAILABLE_STATUS[exc.reason], detail=exc.detail
            ) from None
        logger.info("job %s: /v1 transcript served as %s", job.id, fmt)
        return Response(content=found.data, media_type=found.media_type, headers=_NO_STORE)

    @router.delete("/v1/jobs/{job_id}")
    def delete_job(job_id: str) -> JSONResponse:
        try:
            purged = purge_job(store, artifacts, staging, job_id)
        except JobNotFoundError:
            raise HTTPException(status_code=404, detail=JOB_NOT_FOUND) from None
        except JobActiveError as exc:
            raise HTTPException(
                status_code=409, detail=STILL_ACTIVE.format(status=exc.job.status)
            ) from None
        except OSError as exc:
            logger.error(
                "job %s: could not delete the transcript files: %s", job_id, type(exc).__name__
            )
            raise HTTPException(status_code=500, detail=DELETE_FAILED) from None
        view: V1JobView = v1_job_view(purged)
        return JSONResponse(view.model_dump(mode="json"), headers=_NO_STORE)
