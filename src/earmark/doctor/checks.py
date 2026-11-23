"""The offline doctor checks, in the order they are reported.

Each check runs inside ``guarded``: an unexpected exception becomes a ``fail`` and never
stops the other checks. Nothing here makes a network call, loads a model, downloads a
file, migrates the database, or creates a directory.
"""

import errno
import logging
import os
import secrets
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from earmark.artifacts import ArtifactStore
from earmark.config import ConfigError, Settings
from earmark.db import DB_FILENAME, SCHEMA_VERSION, Database, identity_error
from earmark.doctor import Probes
from earmark.doctor.report import CheckResult
from earmark.models import utc_now

logger = logging.getLogger(__name__)

MIN_TOKEN_CHARS: Final = 32
GENERATE_HINT: Final = "python -c 'import secrets; print(secrets.token_urlsafe(32))'"


def collapse(text: object) -> str:
    return " ".join(str(text).split())


def guarded(name: str, fn: Callable[[], CheckResult]) -> CheckResult:
    try:
        return fn()
    except Exception as exc:
        logger.debug("doctor check %s raised", name, exc_info=exc)
        return CheckResult(name, "fail", f"unexpected error: {type(exc).__name__}: {collapse(exc)}")


def config_text(exc: ConfigError) -> str:
    return "; ".join(exc.errors)


def errno_name(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno or 0, type(exc).__name__)


def close_quietly(obj: object) -> None:
    close = getattr(obj, "close", None)
    if callable(close):
        try:
            close()
        except Exception as exc:  # a failing close must not hide the check's result
            logger.debug("close failed", exc_info=exc)


def _probe_file(directory: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    path = directory / f".earmark-doctor-{secrets.token_hex(8)}.tmp"
    fd = os.open(path, flags, 0o600)
    try:
        try:
            os.write(fd, b"ok")
        finally:
            os.close(fd)
    finally:
        path.unlink(missing_ok=True)


class OfflineChecks:
    """Runs the offline checks and keeps what the online checks reuse."""

    def __init__(self, settings: Settings, probes: Probes) -> None:
        self.settings = settings
        self.probes = probes
        self._closers: list[object] = []

    def close(self) -> None:
        for obj in self._closers:
            close_quietly(obj)
        self._closers.clear()

    def run(self) -> list[CheckResult]:
        s = self.settings
        results = [
            guarded("api token", self.api_token),
            guarded("data dir", self.data_dir),
            guarded("database", self.database),
        ]
        results.append(guarded("resolver", self.resolver))
        results.append(guarded("fetch", self.fetch))
        asr = guarded("asr", self.asr)
        results.append(asr)
        if s.asr_backend == "faster_whisper" and asr.status == "ok":
            results.append(guarded("asr libraries", self.asr_libraries))
            results.append(guarded("asr model", self.asr_model))
        results.append(guarded("notify", self.notify))
        return results

    # -- local state ------------------------------------------------------------------------

    def api_token(self) -> CheckResult:
        from earmark.api.auth import ApiTokenError, require_api_token

        try:
            token = require_api_token(self.settings)
        except ApiTokenError as exc:
            return CheckResult("api token", "fail", str(exc))
        if len(token.get_secret_value()) < MIN_TOKEN_CHARS:
            return CheckResult(
                "api token",
                "warn",
                f"shorter than {MIN_TOKEN_CHARS} characters; generate one with {GENERATE_HINT}",
            )
        return CheckResult("api token", "ok", "set")

    def data_dir(self) -> CheckResult:
        path = self.settings.data_dir
        if path.exists():
            if not path.is_dir():
                return CheckResult("data dir", "fail", f"{path} is not a directory")
            try:
                _probe_file(path)
            except OSError as exc:
                return CheckResult(
                    "data dir", "fail", f"{path} is not writable ({errno_name(exc)})"
                )
            return CheckResult("data dir", "ok", f"{path} is writable")
        ancestor = path.absolute().parent
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        if ancestor.is_dir() and os.access(ancestor, os.W_OK | os.X_OK):
            return CheckResult(
                "data dir", "ok", f"{path} does not exist yet; it is created on first use"
            )
        return CheckResult(
            "data dir", "fail", f"{path} does not exist and {ancestor} is not a writable directory"
        )

    def database(self) -> CheckResult:
        path = self.settings.data_dir / DB_FILENAME
        if not path.exists():
            return CheckResult(
                "database",
                "ok",
                "not created yet; `earmark serve` or `earmark worker` creates it",
            )
        try:
            with Database(path, busy_timeout_s=5.0).connect() as conn:
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                app_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
                journal = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
                counts: dict[str, int] | None
                try:
                    rows = conn.execute("SELECT status, count(*) FROM jobs GROUP BY status")
                    counts = {str(r[0]): int(r[1]) for r in rows}
                except sqlite3.Error:
                    counts = None
        except sqlite3.Error as exc:
            return CheckResult("database", "fail", f"{type(exc).__name__}: {collapse(exc)}")
        identity = identity_error(path, app_id, version)
        if identity is not None:
            return CheckResult("database", "fail", str(identity))
        if version > SCHEMA_VERSION:
            return CheckResult(
                "database",
                "fail",
                f"schema version {version} is newer than this earmark ({SCHEMA_VERSION}); "
                "upgrade earmark",
            )
        if check != "ok":
            return CheckResult("database", "fail", f"integrity check failed: {check[:100]}")
        if version < SCHEMA_VERSION:
            return CheckResult(
                "database",
                "warn",
                f"schema version {version}; the next `earmark serve` or `earmark worker` "
                f"migrates it to {SCHEMA_VERSION}",
            )
        if journal != "wal":
            return CheckResult(
                "database",
                "warn",
                f"journal mode is {journal}, not WAL (the next start switches it; "
                "WAL needs a local filesystem)",
            )
        detail = f"schema {version}"
        if counts is not None:
            detail += (
                f", {sum(counts.values())} jobs (queued {counts.get('queued', 0)}, "
                f"running {counts.get('running', 0)}, failed {counts.get('failed', 0)})"
            )
        return CheckResult("database", "ok", detail)

    # -- backends ---------------------------------------------------------------------------

    def _context(self) -> Any:
        from earmark.pipeline.factory import BuildContext

        s = self.settings
        return BuildContext(settings=s, artifacts=ArtifactStore(s.data_dir), clock=utc_now)

    def resolver(self) -> CheckResult:
        from earmark.pipeline.factory import RESOLVERS

        name = self.settings.resolver_backend
        built = RESOLVERS[name](self._context())
        close_quietly(built)
        if name == "fake":
            return CheckResult("resolver", "warn", "fake resolver (for tests)")
        return CheckResult("resolver", "ok", "apple (iTunes lookup, then the RSS feed)")

    def fetch(self) -> CheckResult:
        from earmark.pipeline.factory import FETCHERS

        s = self.settings
        built = FETCHERS[s.fetch_backend](self._context())
        close_quietly(built)
        if s.fetch_backend == "fake":
            return CheckResult("fetch", "warn", "fake fetcher (for tests)")
        if s.allow_private_fetch:
            return CheckResult(
                "fetch",
                "warn",
                "EARMARK_ALLOW_PRIVATE_FETCH=true: the private-address guard is off",
            )
        return CheckResult("fetch", "ok", "http, private addresses are refused")

    def asr(self) -> CheckResult:
        from earmark.pipeline.factory import TRANSCRIBERS

        s = self.settings
        try:
            built = TRANSCRIBERS[s.asr_backend](self._context())
        except ConfigError as exc:
            return CheckResult("asr", "fail", config_text(exc))
        close_quietly(built)
        if s.asr_backend == "fake":
            return CheckResult("asr", "warn", "fake transcriber (for tests)")
        if s.asr_backend == "remote":
            from earmark.transcribe.remote import RemoteOptions

            ro = RemoteOptions.from_settings(s)
            key = "API key set" if ro.api_key is not None else "no API key"
            return CheckResult("asr", "ok", f"remote server at {ro.host}, model {ro.model}, {key}")
        return CheckResult(
            "asr",
            "ok",
            f"faster_whisper, model {s.asr_model}, device {s.asr_device}, "
            f"compute {s.asr_compute_type}",
        )

    def asr_libraries(self) -> CheckResult:
        from earmark.transcribe.compat import check_transcribe_packages

        result = check_transcribe_packages(self.probes.installed)
        if result.status == "ok":
            return CheckResult("asr libraries", "ok", result.detail)
        return CheckResult("asr libraries", "warn", result.detail)

    def asr_model(self) -> CheckResult:
        from earmark.transcribe.faster_whisper import WhisperOptions, model_cache_status

        options = WhisperOptions.from_settings(self.settings)
        try:
            status = model_cache_status(options)
        except ConfigError as exc:
            return CheckResult("asr model", "fail", config_text(exc))
        if status == "local":
            return CheckResult("asr model", "ok", "local directory")
        if status == "cached":
            return CheckResult("asr model", "ok", "downloaded")
        return CheckResult(
            "asr model",
            "warn",
            f"{options.model} is not downloaded yet; the worker downloads it on the first "
            "transcription (into EARMARK_ASR_MODEL_DIR or the Hugging Face cache)",
        )

    def notify(self) -> CheckResult:
        from earmark.notify.ntfy import NtfyOptions

        s = self.settings
        if s.notify_backend == "fake":
            return CheckResult("notify", "warn", "fake notifier (for tests)")
        if s.notify_backend == "none":
            return CheckResult("notify", "ok", "off")
        options = NtfyOptions.from_settings(s)
        if options is None:
            detail = "EARMARK_NTFY_URL is not set; no notifications are sent"
            if s.ntfy_token is not None:
                detail += "; EARMARK_NTFY_TOKEN is set but unused"
            return CheckResult("notify", "warn", detail)
        token = "token set" if options.token is not None else "no token"
        return CheckResult("notify", "ok", f"ntfy at {options.host}, {token}")
