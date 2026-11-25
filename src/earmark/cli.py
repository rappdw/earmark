"""Command line interface."""

import json
import logging
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, NoReturn

import typer

from earmark import __version__
from earmark.config import ConfigError, Settings, load_settings
from earmark.db import DatabaseSetupError
from earmark.jobs import LIST_LIMIT_MAX
from earmark.log import configure_logging
from earmark.models import JobStatus, TranscriptFormat

if TYPE_CHECKING:
    from earmark.api.schemas import JobView
    from earmark.api_client import ApiClient, ApiError
    from earmark.jobs import Enqueued, JobStore
    from earmark.models import TranscriptionRequest, UploadFields
    from earmark.submission import Intake

logger = logging.getLogger(__name__)

NO_DATABASE_HINT: Final = (
    "  To see the running service's jobs, set EARMARK_API_URL (with EARMARK_API_TOKEN) to "
    "call its API, or run the command in its container: "
    "docker compose exec api earmark {command} ..."
)
SUBMIT_REFUSED_STATUSES: Final = frozenset({413, 415, 422})
DEFAULT_LIST_LIMIT: Final = 20

app = typer.Typer(
    name="earmark",
    no_args_is_help=True,
    add_completion=False,
    help="Share a podcast episode, get its transcript.",
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"earmark {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            is_eager=True,
            callback=_version_callback,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Share a podcast episode, get its transcript."""


def _load_or_exit() -> Settings:
    try:
        return load_settings()
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None


def _api_client_or_exit(settings: Settings, command: str) -> "ApiClient":
    from earmark.api_client import ApiClient, ClientSetupError

    try:
        return ApiClient.from_settings(settings)
    except ClientSetupError as exc:
        typer.echo(f"earmark {command}: {exc}", err=True)
        raise typer.Exit(code=2) from None


def _api_error_exit(
    command: str, exc: "ApiError", *, usage: frozenset[int] = frozenset()
) -> NoReturn:
    typer.echo(f"earmark {command}: {exc.message}", err=True)
    raise typer.Exit(code=2 if exc.status in usage else 1)


def _existing_store_or_exit(settings: Settings, command: str) -> "JobStore":
    from earmark.db import MissingDatabaseError
    from earmark.jobs import open_existing_job_store

    try:
        return open_existing_job_store(settings)
    except MissingDatabaseError as exc:
        typer.echo(f"earmark {command}: {exc}; nothing was created.", err=True)
        typer.echo(NO_DATABASE_HINT.format(command=command), err=True)
        raise typer.Exit(code=1) from None
    except (DatabaseSetupError, sqlite3.Error, OSError) as exc:
        typer.echo(f"earmark {command}: cannot open the job store: {exc}", err=True)
        raise typer.Exit(code=1) from None


@app.command("config")
def config_command() -> None:
    """Show the effective configuration with secrets redacted."""
    settings = _load_or_exit()
    configure_logging(settings.log_level, settings.secret_values())
    for name, display in settings.redacted_items():
        typer.echo(f"{name}={display}")


@app.command("doctor")
def doctor_command(
    online: Annotated[
        bool, typer.Option("--online", help="Also check that servers are reachable.")
    ] = False,
) -> None:
    """Check the configuration, directories, database, backends, and credentials."""
    try:
        settings = load_settings()
    except ConfigError as exc:
        for line in exc.errors:
            typer.echo(f"FAIL  {'config':<14} {_clean(line)}"[:400])
        raise typer.Exit(code=2) from None
    from earmark.doctor.report import doctor_secrets

    secrets = doctor_secrets(settings)
    # Builders log at INFO; keep that off the doctor's output.
    configure_logging("ERROR" if settings.log_level == "ERROR" else "WARNING", secrets)
    from earmark.doctor import run_doctor
    from earmark.doctor.report import exit_code, render, summary

    results = run_doctor(settings, online=online)
    for line in render(results, secrets):
        typer.echo(line)
    typer.echo(summary(results))
    raise typer.Exit(code=exit_code(results))


@app.command("serve")
def serve_command() -> None:
    """Run the ingest API on EARMARK_HOST:EARMARK_PORT."""
    settings = _load_or_exit()
    configure_logging(settings.log_level, settings.secret_values())
    from earmark.api.app import create_app
    from earmark.api.auth import ApiTokenError

    try:
        api = create_app(settings)
    except ApiTokenError as exc:
        typer.echo(f"earmark serve: {exc}", err=True)
        raise typer.Exit(code=2) from None
    except (DatabaseSetupError, sqlite3.Error, OSError) as exc:
        typer.echo(f"earmark serve: cannot open the job store: {exc}", err=True)
        raise typer.Exit(code=1) from None
    import uvicorn

    uvicorn.run(api, host=settings.host, port=settings.port, log_config=None, server_header=False)


@app.command("worker")
def worker_command(
    once: Annotated[
        bool, typer.Option("--once", help="Process at most one job, then exit.")
    ] = False,
) -> None:
    """Run the job worker."""
    settings = _load_or_exit()
    configure_logging(settings.log_level, settings.secret_values())
    from earmark.pipeline.factory import build_worker

    try:
        worker = build_worker(settings)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    except (DatabaseSetupError, sqlite3.Error, OSError) as exc:
        typer.echo(f"earmark worker: cannot open the job store: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if not once:
        worker.run_forever()
        return
    try:
        with worker.handle_signals():
            job = worker.run_once()
    except sqlite3.Error as exc:
        typer.echo(f"earmark worker: job store error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo("no job ready" if job is None else f"job {job.id} {job.status}")


@app.command("submit")
def submit_command(
    source: Annotated[
        str,
        typer.Argument(
            help="An Apple Podcasts episode link, a YouTube video link, a direct audio URL, "
            "or a local audio file."
        ),
    ],
    to: Annotated[
        str, typer.Option("--to", help="Destination address (it must be allowed on the server).")
    ],
    instructions: Annotated[
        str | None,
        typer.Option(
            "--instructions", help="Instructions for the PKA; they go to it with the transcript."
        ),
    ] = None,
    title: Annotated[str | None, typer.Option("--title", help="A title for the audio.")] = None,
) -> None:
    """Enqueue a transcription, the same way POST /transcriptions does.

    Calls the API when EARMARK_API_URL is set.
    """
    settings = _load_or_exit()
    configure_logging(settings.log_level, settings.secret_values())
    from pydantic import ValidationError

    from earmark.models import TranscriptionRequest, UploadFields

    path = Path(source)
    fields: UploadFields | None = None
    request: TranscriptionRequest | None = None
    try:
        if path.is_file():
            fields = UploadFields(to=to, instructions=instructions or "", title=title)
        else:
            request = TranscriptionRequest(
                url=source, to=to, instructions=instructions or "", title=title
            )
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False)
        typer.echo("earmark submit: " + "; ".join(str(e["msg"]) for e in errors), err=True)
        raise typer.Exit(code=2) from None
    if settings.api_url is not None:
        _submit_via_api(settings, path, fields, request)
        return
    from earmark.jobs import open_job_store
    from earmark.recipients import RecipientRefused
    from earmark.resolve.apple import UnsupportedUrlError
    from earmark.staging import Staging
    from earmark.submission import Intake

    enqueued: Enqueued
    try:
        intake = Intake(
            open_job_store(settings), settings.recipient_policy(), Staging(settings.data_dir)
        )
        if fields is not None:
            intake.check_recipient(fields.to)
            enqueued = _submit_file(intake, settings, path, fields)
        elif request is not None:
            enqueued = intake.submit_url(request)
        else:  # pragma: no cover - one of the two was built above
            raise typer.Exit(code=2)
    except (UnsupportedUrlError, RecipientRefused) as exc:
        typer.echo(f"earmark submit: {exc.message}", err=True)
        raise typer.Exit(code=2) from None
    except _SubmitRefused as exc:
        typer.echo(f"earmark submit: {exc}", err=True)
        raise typer.Exit(code=2) from None
    except (DatabaseSetupError, sqlite3.Error, OSError) as exc:
        typer.echo(f"earmark submit: cannot open the job store: {exc}", err=True)
        raise typer.Exit(code=1) from None
    _echo_submitted(enqueued.job.id, enqueued.job.status, enqueued.created)


def _echo_submitted(job_id: str, status: str, created: bool) -> None:
    suffix = "" if created else " (existing job)"
    typer.echo(f"{job_id} {status}{suffix}")


def _submit_via_api(
    settings: Settings,
    path: Path,
    fields: "UploadFields | None",
    request: "TranscriptionRequest | None",
) -> None:
    from earmark.api_client import ApiError

    with _api_client_or_exit(settings, "submit") as client:
        try:
            if fields is not None:
                try:
                    _check_upload(path)
                except _SubmitRefused as exc:
                    typer.echo(f"earmark submit: {exc}", err=True)
                    raise typer.Exit(code=2) from None
                answer = client.submit_file(path, fields)
            elif request is not None:
                answer = client.submit_url(request)
            else:  # pragma: no cover - one of the two was built by the caller
                raise typer.Exit(code=2)
        except ApiError as exc:
            _api_error_exit("submit", exc, usage=SUBMIT_REFUSED_STATUSES)
    _echo_submitted(answer.job_id, answer.status, answer.created)


class _SubmitRefused(Exception):  # noqa: N818  # a usage problem with its own message
    """The input cannot be submitted; str(exc) is the message to show."""


def _check_extension(path: Path) -> str:
    from earmark.sources import upload_extension

    ext = upload_extension(path.name, None)
    if ext is None:
        raise _SubmitRefused(
            f"{_clean(path.name)} does not look like audio "
            "(use an audio file extension such as .mp3 or .m4a)"
        )
    return ext


def _empty_message(path: Path) -> str:
    return f"{_clean(path.name)} is empty"


def _check_upload(path: Path) -> None:
    """The refusals that need no server: not an audio name, or an empty file."""
    _check_extension(path)
    if path.stat().st_size == 0:
        raise _SubmitRefused(_empty_message(path))


def _submit_file(
    intake: "Intake", settings: Settings, path: Path, fields: "UploadFields"
) -> "Enqueued":
    from earmark.fetch import MIB
    from earmark.sources import clean_upload_name

    ext = _check_extension(path)
    limit = settings.max_upload_mb * MIB
    with intake.staging.begin_upload(ext) as writer, path.open("rb") as source:
        while chunk := source.read(MIB):
            writer.write(chunk)
            if writer.size > limit:
                raise _SubmitRefused(
                    f"the file is larger than the {settings.max_upload_mb} MiB limit "
                    "(EARMARK_MAX_UPLOAD_MB)"
                )
        if writer.size == 0:
            raise _SubmitRefused(_empty_message(path))
        staged = writer.commit()
    return intake.submit_upload(fields, staged, upload_name=clean_upload_name(path.name))


def _clean(value: object) -> str:
    """Replace non-printable characters so stored text cannot drive the terminal."""
    return "".join(c if c.isprintable() else "?" for c in str(value))


def _job_lines(view: "JobView") -> list[str]:
    from datetime import UTC

    created = view.created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    subject = view.url or ("upload" if view.source == "upload" else "-")
    fields = (view.id, view.status, view.stage or "-", view.attempts, created, subject)
    lines = ["  ".join(_clean(f) for f in fields)]
    if view.error:
        lines.append(f"  error: {_clean(view.error)}")
    lines.extend(f"  warning: {_clean(w)}" for w in view.warnings)
    return lines


@app.command("jobs")
def jobs_command(
    job_id: Annotated[
        str | None, typer.Argument(help="Show this job only.", show_default=False)
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit", min=1, max=LIST_LIMIT_MAX, help="How many jobs to show (default 20)."
        ),
    ] = None,
    status: Annotated[JobStatus | None, typer.Option("--status", help="Only this status.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """List recent jobs, newest first, or show one job.

    Calls the API when EARMARK_API_URL is set.
    """
    settings = _load_or_exit()
    configure_logging(settings.log_level, settings.secret_values())
    if job_id is not None and (limit is not None or status is not None):
        typer.echo(
            "earmark jobs: --limit and --status are for the list; leave them out to show one job",
            err=True,
        )
        raise typer.Exit(code=2)
    shown = limit or DEFAULT_LIST_LIMIT
    one: JobView | None = None
    jobs: list[JobView] = []
    if settings.api_url is not None:
        from earmark.api_client import ApiError

        with _api_client_or_exit(settings, "jobs") as client:
            try:
                if job_id is not None:
                    one = client.get_job(job_id)
                else:
                    jobs = list(client.list_jobs(limit=shown, status=status))
            except ApiError as exc:
                _api_error_exit("jobs", exc)
    else:
        from earmark.api.schemas import job_view

        store = _existing_store_or_exit(settings, "jobs")
        try:
            if job_id is not None:
                found = store.get(job_id)
                if found is None:
                    typer.echo("earmark jobs: job not found", err=True)
                    raise typer.Exit(code=1)
                one = job_view(found)
            else:
                jobs = [job_view(j) for j in store.list_recent(shown, status=status)]
        except (DatabaseSetupError, sqlite3.Error, OSError) as exc:
            typer.echo(f"earmark jobs: cannot open the job store: {exc}", err=True)
            raise typer.Exit(code=1) from None
    if one is not None:
        if as_json:
            typer.echo(json.dumps(one.model_dump(mode="json"), ensure_ascii=False))
        else:
            for line in _job_lines(one):
                typer.echo(line)
        return
    if as_json:
        payload = {"jobs": [view.model_dump(mode="json") for view in jobs]}
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    if not jobs:
        typer.echo("no jobs")
        return
    for job in jobs:
        for line in _job_lines(job):
            typer.echo(line)


def terminal_text(data: bytes) -> str:
    """UTF-8 bytes as text that cannot drive a terminal: every character that is not
    printable, except newline and tab, becomes '?'. Undecodable bytes become U+FFFD."""
    text = data.decode("utf-8", errors="replace")
    return "".join(c if c in "\n\t" or c.isprintable() else "?" for c in text)


def _stdout_is_terminal() -> bool:  # a seam for tests
    return sys.stdout.isatty()


def _write_transcript(data: bytes) -> None:
    """The output step of both modes: the terminal filter, or the exact bytes."""
    if _stdout_is_terminal():
        typer.echo(terminal_text(data), nl=False)
        return
    sys.stdout.flush()
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


@app.command("transcript")
def transcript_command(
    job_id: Annotated[str, typer.Argument(help="The job id that POST /transcriptions returned.")],
    fmt: Annotated[
        TranscriptFormat,
        typer.Option("--format", help="note (the mailed note; the default), text or json."),
    ] = "note",
) -> None:
    """Print a job's transcript, as GET /jobs/{id}/transcript serves it.

    Calls the API when EARMARK_API_URL is set.
    """
    settings = _load_or_exit()
    configure_logging(settings.log_level, settings.secret_values())
    if settings.api_url is not None:
        from earmark.api_client import ApiError

        with _api_client_or_exit(settings, "transcript") as client:
            try:
                data = client.transcript(job_id, fmt)
            except ApiError as exc:
                _api_error_exit("transcript", exc)
        _write_transcript(data)
        return
    from earmark.artifacts import ArtifactStore
    from earmark.transcripts import JOB_NOT_FOUND, TranscriptUnavailable, read_transcript_file

    store = _existing_store_or_exit(settings, "transcript")
    try:
        job = store.get(job_id)
    except (DatabaseSetupError, sqlite3.Error, OSError) as exc:
        typer.echo(f"earmark transcript: cannot open the job store: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if job is None:
        typer.echo(f"earmark transcript: {JOB_NOT_FOUND}", err=True)
        raise typer.Exit(code=1)
    try:
        found = read_transcript_file(job, ArtifactStore(settings.data_dir), fmt)
    except TranscriptUnavailable as exc:
        typer.echo(f"earmark transcript: {exc.detail}", err=True)
        raise typer.Exit(code=1) from None
    _write_transcript(found.data)


@app.command("resolve")
def resolve_command(
    url: Annotated[str, typer.Argument(help="Apple Podcasts episode URL.")],
) -> None:
    """Resolve an episode URL to its feed, GUID, and audio URL, and print it as JSON."""
    settings = _load_or_exit()
    configure_logging(settings.log_level, settings.secret_values())
    from earmark.errors import PermanentError, SkipEpisode, TransientError
    from earmark.pipeline.factory import build_resolver
    from earmark.resolve.apple import UnsupportedUrlError, parse_apple_url
    from earmark.resolve.resolver import AppleResolver

    try:
        canonical = parse_apple_url(url).url
    except UnsupportedUrlError as exc:
        typer.echo(f"earmark resolve: {_clean(exc.message)}", err=True)
        raise typer.Exit(code=2) from None
    try:
        resolver = build_resolver(settings)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    try:
        if isinstance(resolver, AppleResolver):
            resolution = resolver.resolve_details(canonical)
            episode = resolution.episode
            how = (
                f"feed match: {resolution.match}"
                if resolution.match
                else "the feed does not list it, audio URL from Apple"
            )
            note = f"resolved from the {resolution.source}; {how}"
        else:
            episode = resolver.resolve(canonical)
            note = None
    except UnsupportedUrlError as exc:
        typer.echo(f"earmark resolve: {_clean(exc.message)}", err=True)
        raise typer.Exit(code=2) from None
    except SkipEpisode as exc:
        typer.echo(f"earmark resolve: skipped: {_clean(exc.reason)}", err=True)
        raise typer.Exit(code=3) from None
    except PermanentError as exc:
        typer.echo(f"earmark resolve: {_clean(exc.message)}", err=True)
        raise typer.Exit(code=1) from None
    except TransientError as exc:
        typer.echo(f"earmark resolve: {_clean(exc.message)} (temporary; try again later)", err=True)
        raise typer.Exit(code=1) from None
    finally:
        if isinstance(resolver, AppleResolver):
            resolver.close()
    if note is not None:
        typer.echo(note, err=True)
    typer.echo(episode.model_dump_json(indent=2))


@app.command("transcribe")
def transcribe_command(
    audio: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, help="Local audio file."),
    ],
    as_json: Annotated[bool, typer.Option("--json", help="Print the Transcript as JSON.")] = False,
) -> None:
    """Transcribe a local audio file and print the result.

    Uses the remote ASR server when EARMARK_ASR_BACKEND=remote, otherwise the local
    faster-whisper engine; writes nothing under EARMARK_DATA_DIR and leaves the input
    file alone.
    """
    settings = _load_or_exit()
    configure_logging(settings.log_level, settings.secret_values())
    from earmark.errors import PermanentError, TransientError
    from earmark.transcribe import faster_whisper as fw
    from earmark.transcribe.base import TranscriptionEngine

    engine: TranscriptionEngine
    close: Callable[[], None] | None = None
    try:
        if settings.asr_backend == "remote":
            from earmark.transcribe import remote

            remote_engine = remote.RemoteAsrEngine(remote.RemoteOptions.from_settings(settings))
            engine, close = remote_engine, remote_engine.close
        else:
            options = fw.WhisperOptions.from_settings(settings)
            runtime = fw.load_runtime()
            fw.check_device(options, runtime)
            engine = fw.FasterWhisperEngine(options, runtime)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    try:
        transcript = engine.transcribe_file(audio, fw.file_key(audio))
    except TransientError as exc:
        typer.echo(
            f"earmark transcribe: {_clean(exc.message)} (temporary; try again later)", err=True
        )
        raise typer.Exit(code=1) from None
    except PermanentError as exc:
        typer.echo(f"earmark transcribe: {_clean(exc.message)}", err=True)
        raise typer.Exit(code=1) from None
    except Exception as exc:
        from earmark.pipeline.runner import describe_error

        logger.error("earmark transcribe failed", exc_info=exc)
        text = describe_error(exc, settings.secret_values())
        typer.echo(
            f"earmark transcribe: {_clean(text)} (not a problem with the audio file; "
            "`earmark doctor` checks the installed libraries)",
            err=True,
        )
        raise typer.Exit(code=1) from None
    finally:
        if close is not None:
            close()
    for warning in transcript.warnings:
        typer.echo(f"earmark transcribe: warning: {_clean(warning)}", err=True)
    if as_json:
        typer.echo(transcript.model_dump_json(indent=2))
        return
    for line in transcript.to_text().splitlines():
        typer.echo(_clean(line))
