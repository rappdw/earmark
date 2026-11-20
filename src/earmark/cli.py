"""Command line interface."""

import sqlite3
from typing import Annotated

import typer

from earmark import __version__
from earmark.config import ConfigError, Settings, load_settings
from earmark.db import DatabaseSetupError
from earmark.log import configure_logging

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
