"""Turn a validated request into a queued job. Shared by the API and the CLI.

Replaces the old ingest module. The recipient is checked before anything is enqueued, and an
upload's staged file is owned by ``submit_upload`` from the moment it is called.
"""

import logging
from dataclasses import dataclass

from earmark.jobs import Enqueued, JobStore, NewJob
from earmark.models import TranscriptionRequest, UploadFields
from earmark.recipients import RecipientPolicy, mask_address
from earmark.sources import parse_source_url, url_host
from earmark.staging import StagedUpload, Staging

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Intake:
    store: JobStore
    policy: RecipientPolicy
    staging: Staging

    def check_recipient(self, to: str, /) -> None:
        """Raise ``RecipientRefused`` unless the normalised address is allowed."""
        self.policy.check(to)

    def submit_url(self, request: TranscriptionRequest, /) -> Enqueued:
        """Check the recipient, classify the URL, enqueue. Raises ``UnsupportedUrlError``."""
        self.check_recipient(request.to)
        parsed = parse_source_url(request.url)
        enqueued = self.store.enqueue(
            NewJob(
                source=parsed.source,
                to=request.to,
                instructions=request.instructions,
                title=request.title,
                url=parsed.url,
                shared_at=request.timestamp,
            )
        )
        if parsed.source == "apple_podcasts":
            subject = parsed.url
        else:
            subject = f"audio URL on {url_host(parsed.url)}"
        self._log(enqueued, subject, request.to, request.instructions)
        return enqueued

    def submit_upload(
        self, fields: UploadFields, staged: StagedUpload, *, upload_name: str | None
    ) -> Enqueued:
        """Enqueue an upload. Deletes ``staged`` when this raises or finds an existing job."""
        try:
            self.check_recipient(fields.to)
            enqueued = self.store.enqueue(
                NewJob(
                    source="upload",
                    to=fields.to,
                    instructions=fields.instructions,
                    title=fields.title,
                    upload_file=staged.name,
                    upload_name=upload_name,
                    content_sha256=staged.sha256,
                    shared_at=fields.timestamp,
                )
            )
        except BaseException:
            self.staging.release_staged(staged)
            raise
        if not enqueued.created:
            self.staging.release_staged(staged)
        self._log(enqueued, f"upload of {staged.size} bytes", fields.to, fields.instructions)
        return enqueued

    @staticmethod
    def _log(enqueued: Enqueued, subject: str, to: str, instructions: str) -> None:
        logger.info(
            "enqueued job %s: %s, to %s, instructions %s (created=%s)",
            enqueued.job.id,
            subject,
            mask_address(to),
            "given" if instructions else "none",
            enqueued.created,
        )
