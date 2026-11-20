"""Command line interface."""

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
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


@app.command("analyze")
def analyze_command(
    episode_key: Annotated[str, typer.Argument(help="Episode key (see data/episodes/).")],
    prompt: Annotated[str | None, typer.Option("--prompt", help="Analysis prompt.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the Analysis as JSON.")] = False,
) -> None:
    """Analyze an episode's stored transcript and print the analysis.

    Writes `analyses/<hash>.json` (or reuses it); does not update the vault note.
    """
    settings = _load_or_exit()
    configure_logging(settings.log_level, settings.secret_values())
    from earmark.analyze.analyzer import LlmAnalyzer
    from earmark.analyze.render import render_analysis
    from earmark.artifacts import ArtifactStore
    from earmark.errors import PermanentError, TransientError
    from earmark.models import is_episode_key, normalize_prompt
    from earmark.pipeline.factory import build_analyzer

    if not is_episode_key(episode_key):
        typer.echo(f"earmark analyze: not an episode key: {_clean(episode_key)}", err=True)
        raise typer.Exit(code=2)
    try:
        normalised = normalize_prompt(prompt)
    except ValueError as exc:
        typer.echo(f"earmark analyze: {exc}", err=True)
        raise typer.Exit(code=2) from None
    try:
        analyzer = build_analyzer(settings)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    try:
        artifacts = ArtifactStore(settings.data_dir)
        episode = artifacts.read_episode(episode_key)
        transcript = artifacts.read_transcript(episode_key)
        if episode is None or transcript is None:
            what = "episode" if episode is None else "transcript"
            typer.echo(
                f"earmark analyze: no {what} for {episode_key} under EARMARK_DATA_DIR; "
                "share the episode first",
                err=True,
            )
            raise typer.Exit(code=1)
        analysis = analyzer.analyze(episode, transcript, normalised)
    except TransientError as exc:
        typer.echo(f"earmark analyze: {_clean(exc.message)} (temporary; try again later)", err=True)
        raise typer.Exit(code=1) from None
    except PermanentError as exc:
        typer.echo(f"earmark analyze: {_clean(exc.message)}", err=True)
        raise typer.Exit(code=1) from None
    finally:
        if isinstance(analyzer, LlmAnalyzer):
            analyzer.close()
    for warning in analysis.warnings:
        typer.echo(f"earmark analyze: warning: {_clean(warning)}", err=True)
    if as_json:
        typer.echo(analysis.model_dump_json(indent=2))
        return
    for line in render_analysis(analysis).splitlines():
        typer.echo(_clean(line))
