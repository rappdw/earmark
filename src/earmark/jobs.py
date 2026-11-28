"""Durable job queue on SQLite.

Imports the standard library, ``earmark.models``, ``earmark.db`` and ``earmark.config``
(for ``open_job_store``'s type only). A ``JobStore`` opens one short-lived connection per
method, so it may be shared by threads; separate processes each build their own.

Claiming is one ``UPDATE ... RETURNING`` inside ``BEGIN IMMEDIATE``. The row is selected
with a subquery, not ``UPDATE ... ORDER BY ... LIMIT``, which the SQLite build that CI
uses does not support. A claim carries a private lease token; every worker-side method is
fenced on it and raises ``LeaseLostError`` when the job was requeued or finished.
"""

import hashlib
import json
import math
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from earmark.config import Settings
from earmark.db import Database, MissingDatabaseError, database_path, open_database
from earmark.models import (
    MAX_UTC_OFFSET_MIN,
    Clock,
    Delivery,
    Job,
    JobKind,
    JobResult,
    JobStatus,
    Source,
    Stage,
    is_episode_key,
    utc_now,
)

RETRY_BASE_S: Final = 60.0
RETRY_FACTOR: Final = 4
RETRY_MAX_S: Final = 6 * 3600.0
LIST_LIMIT_MAX: Final = 500
_MAX_EXPONENT: Final = 16
NO_DATA_DIR: Final = "EARMARK_DATA_DIR ({path}) does not exist"
NO_DATABASE: Final = "there is no job database in EARMARK_DATA_DIR ({path})"
_JOB_ID_RE: Final = re.compile(r"[0-9a-f]{32}")


class LeaseLostError(RuntimeError):
    """The claim no longer holds the job (requeued, released, or finished)."""


class JobNotFoundError(LookupError): ...


class JobActiveError(RuntimeError):
    """The job is queued or running; it cannot be purged."""

    def __init__(self, job: Job) -> None:
        super().__init__(job.id)
        self.job = job


@dataclass(frozen=True, slots=True)
class Claim:
    job: Job  # the job as claimed: status "running", attempts already incremented
    token: str  # uuid4().hex, private to this claim; never shown in the API


@dataclass(frozen=True, slots=True)
class NewJob:
    """What ``JobStore.enqueue`` needs to create one job."""

    source: Source
    to: str | None  # mail: normalised, non-empty; delivery "none": None
    instructions: str = ""
    title: str | None = None
    url: str | None = None  # apple_podcasts: canonical; url: direct
    upload_file: str | None = None
    upload_name: str | None = None
    content_sha256: str | None = None
    shared_at: datetime | None = None
    delivery: Delivery = "mail"
    initial_prompt: str | None = None
    language: str | None = None


@dataclass(frozen=True, slots=True)
class Enqueued:
    job: Job
    created: bool  # False when an identical active job was returned instead


def retry_delay(attempts: int, retry_after_s: float | None = None) -> timedelta:
    """Backoff before the next attempt: 60 s * 4^(attempts-1), capped at 6 h, and at
    least ``retry_after_s`` (itself capped)."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    if retry_after_s is not None and (not math.isfinite(retry_after_s) or retry_after_s < 0):
        raise ValueError("retry_after_s must be finite and non-negative")
    exponent = min(attempts - 1, _MAX_EXPONENT)
    seconds = min(RETRY_BASE_S * RETRY_FACTOR**exponent, RETRY_MAX_S)
    if retry_after_s is not None:
        seconds = max(seconds, min(retry_after_s, RETRY_MAX_S))
    return timedelta(seconds=seconds)


def _check_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("datetimes must be timezone-aware")
    return dt


def _to_db(dt: datetime) -> str:
    return _check_aware(dt).astimezone(UTC).isoformat(timespec="microseconds")


def _from_db(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _opt_from_db(value: str | None) -> datetime | None:
    return None if value is None else _from_db(value)


def _offset_minutes(dt: datetime) -> int:
    """The whole-minute UTC offset of an aware datetime; ValueError outside +-1439."""
    offset = _check_aware(dt).utcoffset()
    seconds = 0.0 if offset is None else offset.total_seconds()  # _check_aware: never None
    if seconds % 60 != 0 or abs(seconds) > MAX_UTC_OFFSET_MIN * 60:
        raise ValueError("the UTC offset must be a whole number of minutes within 23:59")
    return int(seconds) // 60


def _require_text(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


def _dedupe_key(kind: JobKind, source: Source, subject: str, to: str, instructions: str) -> str:
    payload = json.dumps(
        [kind, source, subject, to, instructions], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _v1_dedupe_key(content_sha256: str, initial_prompt: str | None, language: str | None) -> str:
    """A list of another length than a mail key, so the two can never be equal."""
    payload = json.dumps(
        ["ingest", "upload", "none", "sha256:" + content_sha256, initial_prompt, language],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _row_to_job(row: sqlite3.Row) -> Job:
    result_text: str | None = row["result"]
    return Job(
        id=row["id"],
        kind=row["kind"],
        status=row["status"],
        stage=row["stage"],
        source=row["source"],
        url=row["url"],
        to=row["recipient"],
        instructions=row["instructions"],
        title=row["title"],
        upload_file=row["upload_file"],
        upload_name=row["upload_name"],
        content_sha256=row["content_sha256"],
        episode_key=row["episode_key"],
        attempts=row["attempts"],
        run_after=_from_db(row["run_after"]),
        error=row["error"],
        warnings=tuple(json.loads(row["warnings"])),
        result=None if result_text is None else JobResult.model_validate_json(result_text),
        shared_at=_from_db(row["shared_at"]),
        created_at=_from_db(row["created_at"]),
        updated_at=_from_db(row["updated_at"]),
        heartbeat_at=_opt_from_db(row["heartbeat_at"]),
        utc_offset_min=row["utc_offset_min"],
        delivery_state=row["delivery_state"],
        message_id=row["message_id"],
        sent_at=_opt_from_db(row["sent_at"]),
        resent=bool(row["resent"]),
        delivery=row["delivery"],
        initial_prompt=row["initial_prompt"],
        language=row["language"],
        purged_at=_opt_from_db(row["purged_at"]),
    )


class JobStore:
    def __init__(
        self,
        db: Database,
        *,
        clock: Clock = utc_now,
        max_attempts: int = 4,
        lease: timedelta = timedelta(seconds=900),
        dedupe_window: timedelta = timedelta(seconds=600),
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if lease <= timedelta(0):
            raise ValueError("lease must be > 0")
        if dedupe_window < timedelta(0):
            raise ValueError("dedupe_window must be >= 0")
        self._db = db
        self._clock = clock
        self._max_attempts = max_attempts
        self._lease = lease
        self._dedupe_window = dedupe_window

    def _now(self) -> datetime:
        return _check_aware(self._clock())

    # -- enqueue ----------------------------------------------------------------------

    def enqueue(self, new: NewJob, /) -> Enqueued:
        """Create a job, or return the active (or recently done) one for the same request."""
        subject = self._validate(new)
        now = self._now()
        shared = now if new.shared_at is None else _check_aware(new.shared_at)
        offset_min = None if new.shared_at is None else _offset_minutes(new.shared_at)
        job = Job(
            id=uuid.uuid4().hex,
            kind="ingest",
            status="queued",
            source=new.source,
            url=new.url,
            to=new.to,
            instructions=new.instructions,
            title=new.title,
            upload_file=new.upload_file,
            upload_name=new.upload_name,
            content_sha256=new.content_sha256,
            run_after=now,
            shared_at=shared,
            created_at=now,
            updated_at=now,
            utc_offset_min=offset_min,
            delivery=new.delivery,
            initial_prompt=new.initial_prompt,
            language=new.language,
        )
        if new.delivery == "none" and new.content_sha256 is not None:
            key = _v1_dedupe_key(new.content_sha256, new.initial_prompt, new.language)
        elif new.to is not None:
            key = _dedupe_key("ingest", new.source, subject, new.to, new.instructions)
        else:  # pragma: no cover - _validate refuses it
            raise ValueError("a job needs a recipient or no delivery")
        cutoff = _to_db(now - self._dedupe_window)
        with self._db.transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE dedupe_key = :key
                  AND (status IN ('queued', 'running')
                       OR (status = 'done' AND updated_at > :cutoff))
                ORDER BY CASE WHEN status IN ('queued', 'running') THEN 0 ELSE 1 END,
                         updated_at DESC, seq DESC
                LIMIT 1
                """,
                {"key": key, "cutoff": cutoff},
            ).fetchone()
            if row is not None:
                return Enqueued(_row_to_job(row), False)
            conn.execute(
                """
                INSERT INTO jobs (id, kind, status, source, url, recipient, instructions,
                                  title, upload_file, upload_name, content_sha256, dedupe_key,
                                  attempts, run_after, warnings, shared_at, created_at,
                                  updated_at, utc_offset_min, delivery, initial_prompt,
                                  language)
                VALUES (:id, 'ingest', 'queued', :source, :url, :to, :instructions, :title,
                        :upload_file, :upload_name, :content_sha256, :key, 0, :now, '[]',
                        :shared, :now, :now, :offset_min, :delivery, :initial_prompt,
                        :language)
                """,
                {
                    "id": job.id,
                    "source": new.source,
                    "url": new.url,
                    "to": new.to,
                    "instructions": new.instructions,
                    "title": new.title,
                    "upload_file": new.upload_file,
                    "upload_name": new.upload_name,
                    "content_sha256": new.content_sha256,
                    "key": key,
                    "now": _to_db(now),
                    "shared": _to_db(shared),
                    "offset_min": offset_min,
                    "delivery": new.delivery,
                    "initial_prompt": new.initial_prompt,
                    "language": new.language,
                },
            )
        return Enqueued(job, True)

    @staticmethod
    def _validate(new: NewJob) -> str:
        """The dedupe subject of ``new``; ValueError if it breaks the source rules."""
        if new.delivery == "none":
            if (
                new.source != "upload"
                or new.to is not None
                or new.instructions != ""
                or new.title is not None
                or new.upload_name is not None
                or new.url is not None
                or not new.upload_file
                or not new.content_sha256
            ):
                raise ValueError(
                    "a delivery-free job is an upload with upload_file and content_sha256, "
                    "and no recipient, instructions, title, name or url"
                )
            return "sha256:" + new.content_sha256
        if new.to is None:
            raise ValueError("to must not be empty")
        _require_text(new.to, "to")
        if new.initial_prompt is not None or new.language is not None:
            raise ValueError("only a delivery-free job has decode options")
        if new.source == "upload":
            if new.url is not None or not new.upload_file or not new.content_sha256:
                raise ValueError("an upload job needs upload_file and content_sha256, not a url")
            return "sha256:" + new.content_sha256
        if new.url is None or not new.url.strip():
            raise ValueError("url must not be empty")
        if new.upload_file is not None:
            raise ValueError("only an upload job has upload_file")
        return new.url

    def mark_purged(self, job_id: str) -> list[Job]:
        """Mark a finished /v1 job, and every finished one sharing its transcript, purged.

        Raises JobNotFoundError for an unknown or mail job and JobActiveError while the job
        is queued or running. Returns the named job first, then the others by seq.
        """
        if _JOB_ID_RE.fullmatch(job_id) is None:
            raise JobNotFoundError(job_id)
        now = _to_db(self._now())
        with self._db.transaction() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None or row["delivery"] != "none":
                raise JobNotFoundError(job_id)
            job = _row_to_job(row)
            if job.purged_at is not None:
                return [job]
            if job.status in ("queued", "running"):
                raise JobActiveError(job)
            rows = conn.execute(
                """
                UPDATE jobs
                SET purged_at = :now, initial_prompt = NULL, dedupe_key = 'purged:' || id,
                    updated_at = :now
                WHERE delivery = 'none' AND dedupe_key = :key
                  AND status IN ('done', 'failed', 'skipped') AND purged_at IS NULL
                RETURNING *
                """,
                {"now": now, "key": row["dedupe_key"]},
            ).fetchall()
        ordered = sorted(rows, key=lambda r: (r["id"] != job_id, r["seq"]))
        return [_row_to_job(r) for r in ordered]

    def checkpoint(self) -> bool:
        return self._db.checkpoint()

    # -- claim and lease --------------------------------------------------------------

    def claim_next(self) -> Claim | None:
        now = _to_db(self._now())
        token = uuid.uuid4().hex
        with self._db.transaction() as conn:
            row = conn.execute(
                """
                UPDATE jobs
                SET status = 'running', attempts = attempts + 1, lease_token = :token,
                    heartbeat_at = :now, updated_at = :now
                WHERE seq = (
                    SELECT seq FROM jobs
                    WHERE status = 'queued' AND run_after <= :now
                    ORDER BY created_at, seq
                    LIMIT 1
                )
                RETURNING *
                """,
                {"token": token, "now": now},
            ).fetchone()
        return None if row is None else Claim(_row_to_job(row), token)

    def _fenced(self, claim: Claim, sql: str, params: dict[str, object]) -> Job:
        """One fenced UPDATE ... RETURNING; raises LeaseLostError if it matched no row.

        Every ``sql`` is ``UPDATE jobs SET ... WHERE id = :id AND status = 'running' AND
        lease_token = :token RETURNING *``; ``:now`` is bound here.
        """
        now = _to_db(self._now())
        with self._db.transaction() as conn:
            row = conn.execute(
                sql, {"id": claim.job.id, "token": claim.token, "now": now, **params}
            ).fetchone()
        if row is None:
            raise LeaseLostError(claim.job.id)
        return _row_to_job(row)

    def heartbeat(self, claim: Claim) -> None:
        self._fenced(
            claim,
            """
            UPDATE jobs SET heartbeat_at = :now
            WHERE id = :id AND status = 'running' AND lease_token = :token
            RETURNING *
            """,
            {},
        )

    def set_stage(self, claim: Claim, stage: Stage) -> None:
        self._fenced(
            claim,
            """
            UPDATE jobs SET stage = :stage, heartbeat_at = :now, updated_at = :now
            WHERE id = :id AND status = 'running' AND lease_token = :token
            RETURNING *
            """,
            {"stage": stage},
        )

    def set_episode_key(self, claim: Claim, episode_key: str) -> None:
        if not is_episode_key(episode_key):
            raise ValueError("episode_key must be 20 lowercase hex characters")
        self._fenced(
            claim,
            """
            UPDATE jobs SET episode_key = :key, updated_at = :now
            WHERE id = :id AND status = 'running' AND lease_token = :token
            RETURNING *
            """,
            {"key": episode_key},
        )

    def complete(self, claim: Claim, result: JobResult | None = None) -> Job:
        return self._fenced(
            claim,
            """
            UPDATE jobs
            SET status = 'done', error = NULL, result = :result, lease_token = NULL,
                updated_at = :now
            WHERE id = :id AND status = 'running' AND lease_token = :token
            RETURNING *
            """,
            {"result": None if result is None else result.model_dump_json()},
        )

    def mark_sending(self, claim: Claim, message_id: str) -> Job:
        """Record that the SMTP conversation is about to start (before the first command).

        A second ``mark_sending`` on a job still ``sending`` means an earlier attempt was
        interrupted, so ``resent`` is set.
        """
        _require_text(message_id, "message_id")
        return self._fenced(
            claim,
            """
            UPDATE jobs
            SET delivery_state = 'sending', message_id = :mid,
                resent = CASE WHEN delivery_state = 'sending' THEN 1 ELSE resent END,
                heartbeat_at = :now, updated_at = :now
            WHERE id = :id AND status = 'running' AND lease_token = :token
              AND (delivery_state IS NULL OR delivery_state = 'sending')
            RETURNING *
            """,
            {"mid": message_id},
        )

    def clear_sending(self, claim: Claim) -> Job:
        """A definite delivery failure: the message was not accepted."""
        return self._fenced(
            claim,
            """
            UPDATE jobs
            SET delivery_state = CASE WHEN delivery_state = 'sending' THEN NULL
                                      ELSE delivery_state END,
                updated_at = :now
            WHERE id = :id AND status = 'running' AND lease_token = :token
            RETURNING *
            """,
            {},
        )

    def complete_sent(self, claim: Claim, result: JobResult) -> Job:
        """The server accepted the message: ``sent`` and ``done`` in one write."""
        return self._fenced(
            claim,
            """
            UPDATE jobs
            SET status = 'done', error = NULL, result = :result, delivery_state = 'sent',
                sent_at = :now, lease_token = NULL, updated_at = :now
            WHERE id = :id AND status = 'running' AND lease_token = :token
              AND delivery_state = 'sending'
            RETURNING *
            """,
            {"result": result.model_dump_json()},
        )

    def skip(self, claim: Claim, reason: str) -> Job:
        _require_text(reason, "reason")
        return self._fenced(
            claim,
            """
            UPDATE jobs
            SET status = 'skipped', error = :reason, lease_token = NULL, updated_at = :now
            WHERE id = :id AND status = 'running' AND lease_token = :token
            RETURNING *
            """,
            {"reason": reason},
        )

    def release(self, claim: Claim) -> Job:
        return self._fenced(
            claim,
            """
            UPDATE jobs
            SET status = 'queued', attempts = attempts - 1, run_after = :now,
                lease_token = NULL, updated_at = :now
            WHERE id = :id AND status = 'running' AND lease_token = :token
            RETURNING *
            """,
            {},
        )

    def fail(
        self,
        claim: Claim,
        error: str,
        *,
        transient: bool,
        retry_after_s: float | None = None,
    ) -> Job:
        _require_text(error, "error")
        now = self._now()
        with self._db.transaction() as conn:
            current = conn.execute(
                "SELECT attempts FROM jobs WHERE id = ? AND status = 'running' AND lease_token = ?",
                (claim.job.id, claim.token),
            ).fetchone()
            if current is None:
                raise LeaseLostError(claim.job.id)
            attempts = int(current["attempts"])
            if transient and attempts < self._max_attempts:
                run_after = _to_db(now + retry_delay(attempts, retry_after_s))
                new_status = "queued"
            else:
                run_after = None
                new_status = "failed"
            row = conn.execute(
                """
                UPDATE jobs
                SET status = :status, error = :error,
                    run_after = COALESCE(:run_after, run_after),
                    lease_token = NULL, updated_at = :now
                WHERE id = :id AND status = 'running' AND lease_token = :token
                RETURNING *
                """,
                {
                    "status": new_status,
                    "error": error,
                    "run_after": run_after,
                    "now": _to_db(now),
                    "id": claim.job.id,
                    "token": claim.token,
                },
            ).fetchone()
        if row is None:  # pragma: no cover - the write lock is held since BEGIN IMMEDIATE
            raise LeaseLostError(claim.job.id)
        return _row_to_job(row)

    def add_warning(self, job_id: str, message: str) -> None:
        _require_text(message, "message")
        now = _to_db(self._now())
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET warnings = json_insert(warnings, '$[#]', :message), updated_at = :now
                WHERE id = :id
                  AND NOT EXISTS (SELECT 1 FROM json_each(jobs.warnings) WHERE value = :message)
                """,
                {"message": message, "now": now, "id": job_id},
            )
            if cursor.rowcount == 1:
                return
            exists = conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if exists is None:
                raise JobNotFoundError(job_id)

    def requeue_stale(self) -> list[Job]:
        now = self._now()
        cutoff = _to_db(now - self._lease)
        reason = f"worker stopped responding (no heartbeat for {self._lease.total_seconds():g} s)"
        updated: list[Job] = []
        with self._db.transaction() as conn:
            stale = conn.execute(
                """
                SELECT seq, attempts FROM jobs
                WHERE status = 'running' AND heartbeat_at < ?
                ORDER BY created_at, seq
                """,
                (cutoff,),
            ).fetchall()
            for entry in stale:
                if entry["attempts"] >= self._max_attempts:
                    status, run_after = "failed", None
                else:
                    status, run_after = "queued", _to_db(now)
                row = conn.execute(
                    """
                    UPDATE jobs
                    SET status = :status, error = :error,
                        run_after = COALESCE(:run_after, run_after),
                        lease_token = NULL, updated_at = :now
                    WHERE seq = :seq
                    RETURNING *
                    """,
                    {
                        "status": status,
                        "error": reason,
                        "run_after": run_after,
                        "now": _to_db(now),
                        "seq": entry["seq"],
                    },
                ).fetchone()
                updated.append(_row_to_job(row))
        return updated

    # -- reads ------------------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        if _JOB_ID_RE.fullmatch(job_id) is None:
            return None
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return None if row is None else _row_to_job(row)

    def active_upload_files(self) -> set[str]:
        """The staged file names of queued and running upload jobs."""
        with self._db.connect() as conn:
            rows = conn.execute(
                """
                SELECT upload_file FROM jobs
                WHERE upload_file IS NOT NULL AND status IN ('queued', 'running')
                """
            ).fetchall()
        return {str(r["upload_file"]) for r in rows}

    def running_job_ids(self) -> set[str]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT id FROM jobs WHERE status = 'running'").fetchall()
        return {str(r["id"]) for r in rows}

    def list_recent(self, limit: int = 20, *, status: JobStatus | None = None) -> list[Job]:
        if not 1 <= limit <= LIST_LIMIT_MAX:
            raise ValueError(f"limit must be between 1 and {LIST_LIMIT_MAX}")
        with self._db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE :status IS NULL OR status = :status
                ORDER BY created_at DESC, seq DESC
                LIMIT :limit
                """,
                {"status": status, "limit": limit},
            ).fetchall()
        return [_row_to_job(row) for row in rows]


def open_job_store(settings: Settings, *, clock: Clock = utc_now) -> JobStore:
    db = open_database(database_path(settings.data_dir))
    return JobStore(
        db,
        clock=clock,
        max_attempts=settings.job_max_attempts,
        lease=timedelta(seconds=settings.job_lease_seconds),
        dedupe_window=timedelta(seconds=settings.dedupe_window_s),
    )


def open_existing_job_store(settings: Settings, *, clock: Clock = utc_now) -> JobStore:
    """open_job_store for read-only commands: never creates the data directory or the
    database. Raises MissingDatabaseError when either is missing."""
    shown = settings.data_dir.absolute()
    if not settings.data_dir.exists():
        raise MissingDatabaseError(NO_DATA_DIR.format(path=shown))
    if not database_path(settings.data_dir).is_file():
        raise MissingDatabaseError(NO_DATABASE.format(path=shown))
    return open_job_store(settings, clock=clock)
