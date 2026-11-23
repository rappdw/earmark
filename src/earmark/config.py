"""Settings: EARMARK_* environment variables, then .env, then defaults."""

import re
from pathlib import Path
from typing import Final, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ResolverBackend = Literal["apple", "fake"]
FetchBackend = Literal["http", "fake"]
AsrBackend = Literal["faster_whisper", "remote", "fake"]
NotifyBackend = Literal["ntfy", "fake", "none"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
AsrDevice = Literal["auto", "cpu", "cuda"]
AsrComputeType = Literal[
    "auto",
    "default",
    "int8",
    "int8_float32",
    "int8_float16",
    "int8_bfloat16",
    "int16",
    "float16",
    "bfloat16",
    "float32",
]

ENV_PREFIX: Final = "EARMARK_"
REDACTED: Final = "**********"
NTFY_TOPIC_PATTERN: Final = re.compile(r"[-_A-Za-z0-9]{1,64}")
# A shorter bare topic is not redacted on its own: masking a common word would corrupt logs.
NTFY_TOPIC_SECRET_MIN_CHARS: Final = 12


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
    resolver_backend: ResolverBackend = Field("apple", description="Episode URL resolver.")
    fetch_backend: FetchBackend = Field("http", description="Audio/feed fetcher.")
    asr_backend: AsrBackend = Field("faster_whisper", description="Transcription backend.")
    notify_backend: NotifyBackend = Field("ntfy", description="Notifier.")
    asr_model: str = Field("large-v3", description="Whisper model name.")
    asr_device: AsrDevice = Field("auto", description="Whisper device.")
    asr_compute_type: AsrComputeType = Field("auto", description="Whisper compute type.")
    asr_language: str | None = Field(None, description="Force a transcription language.")
    asr_model_dir: Path | None = Field(
        None, description="Where Whisper models are downloaded (default: the Hugging Face cache)."
    )
    asr_base_url: str | None = Field(
        None, description="OpenAI-compatible ASR base URL, ending in /v1."
    )
    asr_api_key: SecretStr | None = Field(None, description="Remote ASR API key.")
    asr_max_upload_mb: int | None = Field(
        None, gt=0, description="Refuse remote uploads larger than this (MiB)."
    )
    asr_timeout_seconds: float = Field(
        10800.0, gt=0, description="How long to wait for the remote ASR server's answer."
    )
    keep_audio: bool = Field(False, description="Keep audio after transcription.")
    ntfy_url: SecretStr | None = Field(None, description="ntfy topic URL (secret).")
    ntfy_token: SecretStr | None = Field(None, description="ntfy access token.")
    max_audio_mb: int = Field(1024, gt=0, description="Audio download size cap.")
    max_feed_mb: int = Field(20, gt=0, description="Feed download size cap.")
    allow_private_fetch: bool = Field(False, description="Allow private-address fetches.")
    job_max_attempts: int = Field(4, ge=1, description="Attempts before a job fails.")
    job_lease_seconds: int = Field(900, gt=0, description="Job lease duration.")
    worker_poll_seconds: float = Field(2.0, gt=0, description="Worker poll interval.")
    log_level: LogLevel = Field("INFO", description="Log level.")

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v: object) -> object:
        return v.upper() if isinstance(v, str) else v

    @field_validator("asr_model")
    @classmethod
    def _check_model_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("asr_device", "asr_compute_type", mode="before")
    @classmethod
    def _lower_asr_choice(cls, v: object) -> object:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("asr_language")
    @classmethod
    def _check_asr_language(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if v == "auto":
            return None
        if not re.fullmatch("[a-z]{2,3}", v):
            raise ValueError("must be a language code such as 'en', or 'auto'")
        return v

    @field_validator("asr_base_url")
    @classmethod
    def _check_base_url(cls, v: str | None, info: ValidationInfo) -> str | None:
        if v is None:
            return None
        v = v.strip()
        shape = "must be an http(s) URL with a host, such as http://gpu-box:8000/v1"
        try:
            parts = urlsplit(v)
            hostname = parts.hostname
            _ = parts.port
        except ValueError:
            raise ValueError(shape) from None
        if parts.scheme.lower() not in ("http", "https") or not hostname:
            raise ValueError(shape)
        if parts.username is not None or parts.password is not None:
            key_setting = env_var((info.field_name or "").replace("_base_url", "_api_key"))
            raise ValueError(f"must not contain credentials; use {key_setting}")
        if parts.query or parts.fragment:
            raise ValueError("must not contain a query or fragment")
        return v.rstrip("/")

    @field_validator("ntfy_url")
    @classmethod
    def _check_ntfy_url(cls, v: SecretStr | None) -> SecretStr | None:
        if v is None:
            return None
        url = v.get_secret_value().strip()
        if not url:
            return None
        shape = "must be an http(s) topic URL such as https://ntfy.sh/my-topic"
        try:
            parts = urlsplit(url)
            hostname = parts.hostname
            _ = parts.port
        except ValueError:
            raise ValueError(shape) from None
        if parts.scheme.lower() not in ("http", "https") or not hostname:
            raise ValueError(shape)
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise ValueError("must not contain credentials; use EARMARK_NTFY_TOKEN")
        if parts.query or parts.fragment or "?" in url or "#" in url:
            raise ValueError("must not contain a query or fragment")
        path = parts.path[:-1] if parts.path.endswith("/") else parts.path
        topic = path.rpartition("/")[2]
        if not NTFY_TOPIC_PATTERN.fullmatch(topic):
            raise ValueError(
                "must end in the topic name (letters, digits, '-' and '_', at most 64 "
                "characters), such as https://ntfy.sh/my-topic"
            )
        return SecretStr(url[:-1] if url.endswith("/") else url)

    @field_validator("asr_api_key", "ntfy_token")
    @classmethod
    def _check_api_key(cls, v: SecretStr | None) -> SecretStr | None:
        if v is None:
            return None
        stripped = v.get_secret_value().strip()
        if not stripped:
            return None
        if not all("!" <= c <= "~" for c in stripped):
            raise ValueError("must be printable ASCII without spaces")
        return SecretStr(stripped)

    def secret_values(self) -> list[str]:
        """Every set secret, plus the bare ntfy topic when it is at least 12 characters."""
        values: list[str] = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr) and value.get_secret_value():
                values.append(value.get_secret_value())
        if self.ntfy_url is not None:
            topic = urlsplit(self.ntfy_url.get_secret_value()).path.rpartition("/")[2]
            if len(topic) >= NTFY_TOPIC_SECRET_MIN_CHARS and topic not in values:
                values.append(topic)
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
