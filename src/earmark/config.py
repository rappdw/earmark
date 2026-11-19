"""Settings: EARMARK_* environment variables, then .env, then defaults."""

import zoneinfo
from pathlib import Path, PurePosixPath
from typing import Final, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ResolverBackend = Literal["apple", "fake"]
FetchBackend = Literal["http", "fake"]
AsrBackend = Literal["faster_whisper", "remote", "fake"]
LlmProvider = Literal["anthropic", "openai_compat", "fake"]
StoreBackend = Literal["vault", "fake"]
IndexBackend = Literal["lancedb", "fake", "none"]
NotifyBackend = Literal["ntfy", "fake", "none"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

ENV_PREFIX: Final = "EARMARK_"
REDACTED: Final = "**********"


class ConfigError(Exception):
    """Settings could not be loaded. str(exc) is safe to show: it never contains input values."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("invalid configuration:\n  " + "\n  ".join(errors))


def env_var(field_name: str) -> str:
    return ENV_PREFIX + field_name.upper()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
        frozen=True,
    )

    api_token: SecretStr | None = Field(None, description="Bearer token for the ingest API.")
    host: str = Field("127.0.0.1", description="Address the API binds to.")
    port: int = Field(8787, ge=1, le=65535, description="Port the API binds to.")
    data_dir: Path = Field(Path("./data"), description="Working data (jobs DB, episode cache).")
    vault_dir: Path | None = Field(None, description="Root of the Markdown vault.")
    vault_subdir: str = Field("Earmark", description="Folder inside the vault earmark owns.")
    obsidian_vault_name: str | None = Field(None, description="Vault name for obsidian:// links.")
    resolver_backend: ResolverBackend = Field("apple", description="Episode URL resolver.")
    fetch_backend: FetchBackend = Field("http", description="Audio/feed fetcher.")
    asr_backend: AsrBackend = Field("faster_whisper", description="Transcription backend.")
    llm_provider: LlmProvider = Field("anthropic", description="Analysis model provider.")
    store_backend: StoreBackend = Field("vault", description="Note store.")
    index_backend: IndexBackend = Field("lancedb", description="Search index.")
    notify_backend: NotifyBackend = Field("ntfy", description="Notifier.")
    asr_model: str = Field("large-v3", description="Whisper model name.")
    asr_device: str = Field("auto", description="Whisper device.")
    asr_compute_type: str = Field("auto", description="Whisper compute type.")
    asr_language: str | None = Field(None, description="Force a transcription language.")
    asr_base_url: str | None = Field(None, description="Remote ASR endpoint.")
    asr_api_key: SecretStr | None = Field(None, description="Remote ASR API key.")
    asr_max_upload_mb: int | None = Field(None, gt=0, description="Remote ASR upload cap.")
    keep_audio: bool = Field(False, description="Keep audio after transcription.")
    llm_model: str = Field("claude-opus-5-5", description="Analysis model id.")
    llm_base_url: str | None = Field(None, description="OpenAI-compatible endpoint.")
    llm_api_key: SecretStr | None = Field(None, description="Analysis API key.")
    llm_context_tokens: int | None = Field(None, gt=0, description="Model context window.")
    embed_model: str = Field("BAAI/bge-small-en-v1.5", description="Embedding model.")
    ntfy_url: str | None = Field(None, description="ntfy topic URL.")
    ntfy_token: SecretStr | None = Field(None, description="ntfy access token.")
    max_audio_mb: int = Field(1024, gt=0, description="Audio download size cap.")
    max_feed_mb: int = Field(20, gt=0, description="Feed download size cap.")
    allow_private_fetch: bool = Field(False, description="Allow private-address fetches.")
    job_max_attempts: int = Field(4, ge=1, description="Attempts before a job fails.")
    job_lease_seconds: int = Field(900, gt=0, description="Job lease duration.")
    worker_poll_seconds: float = Field(2.0, gt=0, description="Worker poll interval.")
    tz: str | None = Field(None, description="IANA time zone for dates.")
    log_level: LogLevel = Field("INFO", description="Log level.")

    @field_validator("vault_subdir")
    @classmethod
    def _check_vault_subdir(cls, v: str) -> str:
        path = PurePosixPath(v)
        if not v.strip() or path.is_absolute() or ".." in path.parts or "\\" in v:
            raise ValueError("must be a relative path inside the vault without '..'")
        return v

    @field_validator("tz")
    @classmethod
    def _check_tz(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                zoneinfo.ZoneInfo(v)
            except (zoneinfo.ZoneInfoNotFoundError, ValueError):
                raise ValueError("unknown time zone") from None
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v: object) -> object:
        return v.upper() if isinstance(v, str) else v

    def secret_values(self) -> list[str]:
        values: list[str] = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr) and value.get_secret_value():
                values.append(value.get_secret_value())
        return values

    def redacted_items(self) -> list[tuple[str, str]]:
        items: list[tuple[str, str]] = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if value is None:
                display = ""
            elif isinstance(value, SecretStr):
                display = REDACTED if value.get_secret_value() else ""
            elif name.endswith("_url"):
                display = redact_url(str(value))
            elif isinstance(value, bool):
                display = "true" if value else "false"
            else:
                display = str(value)
            items.append((env_var(name), display))
        return items


def redact_url(url: str) -> str:
    """Mask a password in URL userinfo; return anything else unchanged."""
    try:
        parts = urlsplit(url)
        password = parts.password
        if password is None:
            return url
        netloc = parts.netloc
        userinfo, _, hostport = netloc.rpartition("@")
        username = userinfo.split(":", 1)[0]
        return parts._replace(netloc=f"{username}:****@{hostport}").geturl()
    except ValueError:
        return url


def load_settings(*, env_file: Path | str | None = ".env") -> Settings:
    try:
        return Settings(_env_file=env_file)
    except ValidationError as exc:
        lines: list[str] = []
        for err in exc.errors(include_input=False, include_url=False):
            loc = err["loc"]
            prefix = env_var(str(loc[0])) if loc else "EARMARK"
            lines.append(f"{prefix}: {err['msg']}")
        raise ConfigError(lines) from None
