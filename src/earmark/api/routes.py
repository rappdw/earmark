"""The ingest API's routes."""

from typing import Annotated, Final

from fastapi import APIRouter, HTTPException, Query, Response

from earmark.api.schemas import Health, IngestResponse, JobList, SearchResponse
from earmark.artifacts import ArtifactStore
from earmark.episodes import (
    EPISODES_LIMIT_DEFAULT,
    EPISODES_LIMIT_MAX,
    EPISODES_OFFSET_MAX,
    EpisodeNotFoundError,
    EpisodePage,
    TranscriptMissingError,
    enqueue_reanalyze,
    list_episodes,
)
from earmark.index.search import DEFAULT_SEARCH_LIMIT, Searcher, parse_query
from earmark.ingest import enqueue_ingest
from earmark.jobs import LIST_LIMIT_MAX, JobStore
from earmark.models import IngestRequest, Job, JobStatus, ReanalyzeRequest
from earmark.resolve.apple import UnsupportedUrlError

# 4000 prompt chars at up to 12 bytes each, 2048 URL chars at up to 6, and a timestamp.
MAX_BODY_BYTES: Final = 64 * 1024


def build_router(store: JobStore, searcher: Searcher, artifacts: ArtifactStore) -> APIRouter:
    router = APIRouter()

    @router.post("/ingest", status_code=202, max_body_size=MAX_BODY_BYTES)
    def ingest(body: IngestRequest, response: Response) -> IngestResponse:
        try:
            enqueued = enqueue_ingest(store, body)
        except UnsupportedUrlError as exc:
            raise HTTPException(status_code=422, detail=exc.message) from None
        job = enqueued.job
        response.headers["Location"] = f"/jobs/{job.id}"
        return IngestResponse(job_id=job.id, status=job.status, created=enqueued.created)

    @router.get("/episodes")
    def list_stored_episodes(
        limit: Annotated[int, Query(ge=1, le=EPISODES_LIMIT_MAX)] = EPISODES_LIMIT_DEFAULT,
        offset: Annotated[int, Query(ge=0, le=EPISODES_OFFSET_MAX)] = 0,
    ) -> EpisodePage:
        return list_episodes(artifacts, limit=limit, offset=offset)

    @router.post("/episodes/{key}/analyze", status_code=202, max_body_size=MAX_BODY_BYTES)
    def reanalyze(key: str, body: ReanalyzeRequest, response: Response) -> IngestResponse:
        try:
            enqueued = enqueue_reanalyze(store, artifacts, key, body)
        except EpisodeNotFoundError as exc:
            raise HTTPException(status_code=404, detail=exc.message) from None
        except TranscriptMissingError as exc:
            raise HTTPException(status_code=409, detail=exc.message) from None
        job = enqueued.job
        response.headers["Location"] = f"/jobs/{job.id}"
        return IngestResponse(job_id=job.id, status=job.status, created=enqueued.created)

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str) -> Job:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @router.get("/jobs")
    def list_jobs(
        limit: Annotated[int, Query(ge=1, le=LIST_LIMIT_MAX)] = 20,
        status: JobStatus | None = None,
    ) -> JobList:
        return JobList(jobs=tuple(store.list_recent(limit, status=status)))

    @router.get("/search")
    def search(
        q: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
        kind: Annotated[list[str] | None, Query()] = None,
        episode: str | None = None,
    ) -> SearchResponse:
        query = parse_query(q, limit=limit, kinds=kind or (), episode_key=episode)
        return SearchResponse(hits=searcher.search(query).hits)

    @router.api_route("/healthz", methods=["GET", "HEAD"])
    def healthz() -> Health:
        return Health(status="ok")

    return router
