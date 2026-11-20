"""Map an ingest request to a queued job. Shared by the API and the CLI."""

import logging

from earmark.jobs import Enqueued, JobStore
from earmark.models import IngestRequest
from earmark.resolve.apple import parse_apple_url

logger = logging.getLogger(__name__)


def enqueue_ingest(store: JobStore, request: IngestRequest) -> Enqueued:
    """Parse the URL, enqueue its canonical form. Raises UnsupportedUrlError."""
    ref = parse_apple_url(request.url)
    enqueued = store.enqueue(ref.url, prompt=request.prompt, shared_at=request.timestamp)
    logger.info(
        "enqueued job %s for %s (created=%s, prompt=%s)",
        enqueued.job.id,
        ref.url,
        enqueued.created,
        "yes" if request.prompt else "no",
    )
    return enqueued
