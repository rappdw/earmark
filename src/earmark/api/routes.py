"""The API's routes."""

import logging
import re
from collections.abc import Mapping, Sequence
from functools import partial
from typing import Annotated, Any, Final

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from earmark.api.schemas import Health, JobList, JobView, TranscriptionResponse, job_view
from earmark.api.upload import UploadRejected, log_rejection, receive_upload
from earmark.api.v1 import add_v1_routes
from earmark.artifacts import ArtifactStore
from earmark.fetch import media_type
from earmark.jobs import LIST_LIMIT_MAX, Enqueued
from earmark.models import (
    FORM_FIELDS,
    JSON_FIELDS,
    JobStatus,
    TranscriptFormat,
    TranscriptionRequest,
    UploadFields,
)
from earmark.recipients import RecipientRefused
from earmark.resolve.apple import UnsupportedUrlError
from earmark.submission import Intake
from earmark.transcripts import JOB_NOT_FOUND, TranscriptUnavailable, read_transcript_file
from earmark.v1_jobs import SummaryLimits

logger = logging.getLogger(__name__)

# 4000 instruction chars at up to 12 JSON bytes each, 2048 URL chars at up to 6, a 200 char
# title at 12, an address and a timestamp come to about 53 KB.
MAX_JSON_BODY_BYTES: Final = 64 * 1024
JSON_TOO_LARGE: Final = "request body is larger than 64 KiB"
UNSUPPORTED_TYPE: Final = (
    "send JSON (Content-Type: application/json) or a multipart form "
    "(Content-Type: multipart/form-data)"
)
_FILENAME_OK: Final = re.compile(r"[0-9a-z][0-9a-z.-]*\.md")
_UNAVAILABLE_STATUS: Final = {"not_ready": 409, "gone": 410, "unreadable": 500}
MISSING_BODY: Final = "request body is missing; send a JSON object with url and to"


def _prefixed(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{**e, "loc": ("body", *e.get("loc", ()))} for e in errors]


def _validation_detail(exc: ValidationError, body_fields: Sequence[str]) -> str:
    from earmark.api.app import describe_validation_errors

    errors = exc.errors(include_input=False, include_url=False)
    return describe_validation_errors(_prefixed(errors), body_fields=body_fields)


def _accepted(enqueued: Enqueued) -> JSONResponse:
    job = enqueued.job
    body = TranscriptionResponse(job_id=job.id, status=job.status, created=enqueued.created)
    return JSONResponse(
        body.model_dump(mode="json"), status_code=202, headers={"Location": f"/jobs/{job.id}"}
    )


def build_router(
    intake: Intake,
    *,
    artifacts: ArtifactStore,
    max_upload_bytes: int,
    summaries: SummaryLimits | None = None,
) -> APIRouter:
    router = APIRouter()
    store = intake.store

    async def read_json(request: Request) -> TranscriptionRequest:
        declared = request.headers.get("content-length", "").strip()
        if declared.isascii() and declared.isdigit() and int(declared) > MAX_JSON_BODY_BYTES:
            raise HTTPException(status_code=413, detail=JSON_TOO_LARGE)
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_JSON_BODY_BYTES:
                raise HTTPException(status_code=413, detail=JSON_TOO_LARGE)
        if not body:
            raise HTTPException(status_code=422, detail=MISSING_BODY)
        try:
            return TranscriptionRequest.model_validate_json(bytes(body))
        except ValidationError as exc:
            raise HTTPException(
                status_code=422, detail=_validation_detail(exc, JSON_FIELDS)
            ) from None

    async def submit_json(request: Request) -> Enqueued:
        parsed = await read_json(request)
        try:
            return await run_in_threadpool(intake.submit_url, parsed)
        except (RecipientRefused, UnsupportedUrlError) as exc:
            raise HTTPException(status_code=422, detail=exc.message) from None

    async def submit_multipart(request: Request) -> Enqueued:
        try:
            received = await receive_upload(
                request.headers, request.stream(), intake.staging, max_file_bytes=max_upload_bytes
            )
        except UploadRejected as exc:
            log_rejection(exc)
            raise HTTPException(status_code=exc.status, detail=exc.detail) from None
        writer = received.writer
        try:
            try:
                fields = UploadFields.model_validate(received.fields)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=422, detail=_validation_detail(exc, ("file", *FORM_FIELDS))
                ) from None
            staged = await run_in_threadpool(writer.commit)
        except BaseException:
            await run_in_threadpool(writer.abort)
            raise
        try:
            return await run_in_threadpool(
                partial(intake.submit_upload, fields, staged, upload_name=received.filename)
            )
        except RecipientRefused as exc:
            raise HTTPException(status_code=422, detail=exc.message) from None

    @router.post("/transcriptions", status_code=202)
    async def transcriptions(request: Request) -> Response:
        kind = media_type(request.headers.get("content-type"))
        try:
            if kind == "application/json":
                return _accepted(await submit_json(request))
            if kind == "multipart/form-data":
                return _accepted(await submit_multipart(request))
        except ClientDisconnect:
            logger.info("client disconnected during the request")
            return Response(status_code=400)  # nobody is listening
        raise HTTPException(status_code=415, detail=UNSUPPORTED_TYPE)

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str) -> JobView:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=JOB_NOT_FOUND)
        return job_view(job)

    @router.get("/jobs/{job_id}/transcript")
    def get_transcript(
        job_id: str,
        fmt: Annotated[TranscriptFormat, Query(alias="format")] = "note",
    ) -> Response:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=JOB_NOT_FOUND)
        try:
            found = read_transcript_file(job, artifacts, fmt)
        except TranscriptUnavailable as exc:
            if exc.reason == "unreadable":
                logger.warning(
                    "job %s: no transcript as %s: %s: %s", job.id, fmt, exc.reason, exc.detail
                )
            else:
                logger.info("job %s: no transcript as %s: %s", job.id, fmt, exc.reason)
            raise HTTPException(
                status_code=_UNAVAILABLE_STATUS[exc.reason], detail=exc.detail
            ) from None
        headers = {"Cache-Control": "no-store"}
        if found.filename is not None:
            if not _FILENAME_OK.fullmatch(found.filename):
                raise ValueError("the note's file name is not a plain slug")
            headers["Content-Disposition"] = f'attachment; filename="{found.filename}"'
        logger.info("job %s: transcript served as %s", job.id, fmt)
        return Response(content=found.data, media_type=found.media_type, headers=headers)

    @router.get("/jobs")
    def list_jobs(
        limit: Annotated[int, Query(ge=1, le=LIST_LIMIT_MAX)] = 20,
        status: JobStatus | None = None,
    ) -> JobList:
        return JobList(jobs=tuple(job_view(j) for j in store.list_recent(limit, status=status)))

    @router.api_route("/healthz", methods=["GET", "HEAD"])
    def healthz() -> Health:
        return Health(status="ok")

    add_v1_routes(
        router, intake, artifacts=artifacts, max_upload_bytes=max_upload_bytes, summaries=summaries
    )
    return router
