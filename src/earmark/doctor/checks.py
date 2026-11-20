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
from earmark.db import DB_FILENAME, SCHEMA_VERSION, Database
from earmark.doctor import Probes
from earmark.doctor.report import CheckResult
from earmark.errors import EarmarkError
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
        self.anthropic: Any = None  # AnthropicClient, kept for the online check
        self._index: Any = None  # LanceIndex
        self._index_info: Any = None  # ModelInfo
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
            guarded("vault", self.vault),
        ]
        if s.notify_backend == "ntfy" and s.ntfy_url is not None:
            results.append(guarded("obsidian link", self.obsidian_link))
        results.append(guarded("resolver", self.resolver))
        results.append(guarded("fetch", self.fetch))
        asr = guarded("asr", self.asr)
        results.append(asr)
        if s.asr_backend == "faster_whisper" and asr.status == "ok":
            results.append(guarded("asr model", self.asr_model))
        results.append(guarded("llm", self.llm))
        index = guarded("index", self.index)
        results.append(index)
        if s.index_backend == "lancedb" and index.status == "ok":
            results.append(guarded("embed model", self.embed_model))
            results.append(guarded("search index", self.search_index))
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

    def vault(self) -> CheckResult:
        from earmark.vault.fs import SymlinkRefused, Vault
        from earmark.vault.notes import build_vault_store

        s = self.settings
        if s.store_backend == "fake":
            return CheckResult("vault", "warn", "fake store (for tests)")
        try:
            build_vault_store(s)
        except ConfigError as exc:
            return CheckResult("vault", "fail", config_text(exc))
        vault_dir = (s.vault_dir or Path()).absolute()  # set: build_vault_store checked it
        try:
            opened = Vault(vault_dir, s.vault_subdir).open_existing_subdir()
        except SymlinkRefused as exc:
            return CheckResult("vault", "fail", exc.message)
        except OSError as exc:
            return CheckResult(
                "vault", "fail", f"{s.vault_subdir} is not readable ({errno_name(exc)})"
            )
        if opened is None:
            if os.access(vault_dir, os.W_OK | os.X_OK):
                return CheckResult(
                    "vault",
                    "ok",
                    f"{s.vault_subdir} does not exist yet; it is created on the first store",
                )
            return CheckResult("vault", "fail", "EARMARK_VAULT_DIR is not writable")
        with opened:
            try:
                opened.probe_write()
            except OSError as exc:
                return CheckResult(
                    "vault", "fail", f"{s.vault_subdir} is not writable ({errno_name(exc)})"
                )
        return CheckResult("vault", "ok", f"{vault_dir}/{s.vault_subdir} is writable")

    def obsidian_link(self) -> CheckResult:
        s = self.settings
        name = s.obsidian_vault_name
        if name is None:
            return CheckResult(
                "obsidian link",
                "warn",
                "EARMARK_OBSIDIAN_VAULT_NAME is not set; done notifications show the note "
                "path instead of a link",
            )
        if s.vault_dir is not None and s.vault_dir.absolute().name != name:
            return CheckResult(
                "obsidian link",
                "warn",
                "Obsidian names vaults after their folder; EARMARK_VAULT_DIR is "
                f"'{s.vault_dir.absolute().name}'",
            )
        return CheckResult("obsidian link", "ok", f"links open vault '{name}'")

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

    def llm(self) -> CheckResult:
        s = self.settings
        if s.llm_provider == "fake":
            return CheckResult("llm", "warn", "fake analyzer (for tests)")
        try:
            if s.llm_provider == "anthropic":
                from earmark.analyze.llm.anthropic import AnthropicClient, AnthropicOptions

                options = AnthropicOptions.from_settings(s)
                client = AnthropicClient(
                    options, http_client=self.probes.anthropic_http_client, max_retries=0
                )
                self.anthropic = client
                self._closers.append(client)
                return CheckResult(
                    "llm",
                    "ok",
                    f"anthropic, model {options.model}, effort {options.effort}, "
                    f"credential from {client.credential_source}",
                )
            from earmark.analyze.llm.openai_compat import OpenAICompatOptions

            oc = OpenAICompatOptions.from_settings(s)
        except ConfigError as exc:
            return CheckResult("llm", "fail", config_text(exc))
        key = "API key set" if oc.api_key is not None else "no API key"
        return CheckResult(
            "llm",
            "ok",
            f"openai_compat at {oc.host}, model {oc.model}, context {oc.context_tokens} "
            f"tokens, {key}",
        )

    def index(self) -> CheckResult:
        from earmark.index import embed
        from earmark.index.lancedb_index import build_lancedb_index

        s = self.settings
        if s.index_backend == "none":
            return CheckResult("index", "ok", "search is off (EARMARK_INDEX_BACKEND=none)")
        if s.index_backend == "fake":
            return CheckResult("index", "warn", "fake index (for tests)")
        try:
            built = build_lancedb_index(s, allow_download=False)
            info = embed.find_model(s, embed.load_fastembed_runtime())
        except ConfigError as exc:
            return CheckResult("index", "fail", config_text(exc))
        self._index = built
        self._index_info = info
        return CheckResult(
            "index", "ok", f"lancedb, embedding model {info.name} ({info.dim} dimensions)"
        )

    def embed_model(self) -> CheckResult:
        from earmark.index.embed import default_model_dir, model_cached

        info = self._index_info
        cached = model_cached(info, default_model_dir(self.settings))
        if cached is True:
            return CheckResult("embed model", "ok", "downloaded")
        if cached is False:
            return CheckResult(
                "embed model",
                "warn",
                f"{info.name} is not downloaded yet; the worker or `earmark reindex` downloads "
                "it; until then GET /search answers 503",
            )
        return CheckResult("embed model", "warn", f"cannot tell whether {info.name} is downloaded")

    def search_index(self) -> CheckResult:
        try:
            exists = self._index.check_existing()
        except EarmarkError as exc:  # IndexMismatchError and TransientError
            return CheckResult("search index", "fail", exc.message)
        if not exists:
            return CheckResult(
                "search index",
                "ok",
                "empty; filled as episodes are indexed (or by `earmark reindex`)",
            )
        return CheckResult("search index", "ok", f"built with {self._index_info.name}")

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
