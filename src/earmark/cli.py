"""Command line interface."""

import json
import sqlite3
from typing import Annotated

import typer

from earmark import __version__
from earmark.config import ConfigError, Settings, load_settings
from earmark.db import DatabaseSetupError
from earmark.jobs import LIST_LIMIT_MAX
from earmark.log import configure_logging
from earmark.models import Job, JobStatus

app = typer.Typer(
    name="earmark",
    no_args_is_help=True,
    add_completion=False,
    help="Share a podcast episode, get a research note.",
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
    """Share a podcast episode, get a research note."""


def _load_or_exit() -> Settings:
    try:
        return load_settings()
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None


@app.command("config")
def config_command() -> None:
    """Show the effective configuration with secrets redacted."""
    settings = _load_or_exit()
    configure_logging(settings.log_level, settings.secret_values())
    for name, display in settings.redacted_items():
        typer.echo(f"{name}={display}")


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
    url: Annotated[str, typer.Argument(help="Apple Podcasts episode URL.")],
    prompt: Annotated[str | None, typer.Option("--prompt", help="Analysis prompt.")] = None,
) -> None:
    """Enqueue an episode, the same way POST /ingest does."""
    settings = _load_or_exit()
    configure_logging(settings.log_level, settings.secret_values())
    from pydantic import ValidationError

    from earmark.ingest import enqueue_ingest
    from earmark.jobs import open_job_store
    from earmark.models import IngestRequest
    from earmark.resolve.apple import UnsupportedUrlError

    try:
        request = IngestRequest(url=url, prompt=prompt)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False)
        typer.echo("earmark submit: " + "; ".join(str(e["msg"]) for e in errors), err=True)
        raise typer.Exit(code=2) from None
    try:
        store = open_job_store(settings)
        enqueued = enqueue_ingest(store, request)
    except UnsupportedUrlError as exc:
        typer.echo(f"earmark submit: {exc.message}", err=True)
        raise typer.Exit(code=2) from None
    except (DatabaseSetupError, sqlite3.Error, OSError) as exc:
        typer.echo(f"earmark submit: cannot open the job store: {exc}", err=True)
        raise typer.Exit(code=1) from None
    suffix = "" if enqueued.created else " (existing job)"
    typer.echo(f"{enqueued.job.id} {enqueued.job.status}{suffix}")


def _clean(value: object) -> str:
    """Replace non-printable characters so stored text cannot drive the terminal."""
    return "".join(c if c.isprintable() else "?" for c in str(value))


def _job_lines(job: Job) -> list[str]:
    from datetime import UTC

    created = job.created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    subject = job.url or job.episode_key or "-"
    fields = (job.id, job.status, job.stage or "-", job.attempts, created, subject)
    lines = ["  ".join(_clean(f) for f in fields)]
    if job.error:
        lines.append(f"  error: {_clean(job.error)}")
    lines.extend(f"  warning: {_clean(w)}" for w in job.warnings)
    return lines


@app.command("jobs")
def jobs_command(
    limit: Annotated[
        int, typer.Option("--limit", min=1, max=LIST_LIMIT_MAX, help="How many jobs to show.")
    ] = 20,
    status: Annotated[JobStatus | None, typer.Option("--status", help="Only this status.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """List recent jobs, newest first."""
    settings = _load_or_exit()
    configure_logging(settings.log_level, settings.secret_values())
    from earmark.jobs import open_job_store

    try:
        jobs = open_job_store(settings).list_recent(limit, status=status)
    except (DatabaseSetupError, sqlite3.Error, OSError) as exc:
        typer.echo(f"earmark jobs: cannot open the job store: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if as_json:
        payload = {"jobs": [j.model_dump(mode="json") for j in jobs]}
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    if not jobs:
        typer.echo("no jobs")
        return
    for job in jobs:
        for line in _job_lines(job):
            typer.echo(line)
