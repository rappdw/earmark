"""Settings: EARMARK_* environment variables, then .env, then defaults."""

import ipaddress
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Final, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from earmark.recipients import ALL, RecipientPolicy, normalize_address

ResolverBackend = Literal["apple", "fake"]
FetchBackend = Literal["http", "fake"]
YoutubeBackend = Literal["ytdlp", "fake"]
AsrBackend = Literal["faster_whisper", "remote", "fake"]
NotifyBackend = Literal["ntfy", "fake", "none"]
MailBackend = Literal["smtp", "fake"]
SmtpSecurity = Literal["starttls", "tls"]
LlmBackend = Literal["none", "ollama", "llama_cpp", "vllm", "fake"]
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

SMTP_DEFAULT_PORTS: Final[Mapping[str, int]] = {"starttls": 587, "tls": 465}
SMTP_TIMEOUT_MAX_S: Final = 90.0
ASR_BATCH_SIZE_MAX: Final = 64
# The worker's stop grace (compose stop_grace_period, systemd TimeoutStopSec) must be at least
# SMTP_TIMEOUT_MAX_S + WORKER_STOP_MARGIN_S. The margin covers the push (ntfy: 5 s connect,
# 10 s write, 10 s read) and the job-store writes. tests/test_deploy.py pins this.
WORKER_STOP_MARGIN_S: Final = 30.0

LLM_MIN_CONTEXT_TOKENS: Final = 8192
LLM_MAX_OUTPUT_TOKENS_LIMIT: Final = 131_072
LLM_MAX_TRANSCRIPT_CHARS_LIMIT: Final = 10_000_000

ENV_PREFIX: Final = "EARMARK_"
REDACTED: Final = "**********"
NTFY_TOPIC_PATTERN: Final = re.compile(r"[-_A-Za-z0-9]{1,64}")
# A shorter bare topic is not redacted on its own: masking a common word would corrupt logs.
NTFY_TOPIC_SECRET_MIN_CHARS: Final = 12

API_URL_SHAPE: Final = (
    "must be the API's root URL, such as https://earmark.example.com or http://127.0.0.1:8787"
)
API_URL_CREDENTIALS: Final = "must not contain credentials; use EARMARK_API_TOKEN"
API_URL_QUERY: Final = "must not contain a query or fragment"
API_URL_PATH: Final = "must be the API's root URL, without a path"
API_URL_PLAIN_HTTP: Final = (
    "plain http is allowed only to a loopback address such as http://127.0.0.1:8787; "
    "use https, or the API token would cross the network unencrypted"
)

LLM_URL_SHAPE: Final = (
    "must be the model server's root URL, such as http://127.0.0.1:11434 or http://ollama:11434"
)
LLM_URL_CREDENTIALS: Final = "must not contain credentials; use EARMARK_LLM_API_KEY"
LLM_URL_QUERY: Final = "must not contain a query or fragment"
LLM_URL_PATH: Final = (
    "must be the server's root URL, without a path (earmark adds /api/chat or /v1/chat/completions)"
)
LLM_URL_NOT_LOCAL: Final = (
    "must name a loopback or private-network address or a local host name (localhost, a "
    "single-label name such as ollama, or a name under .internal, .local, .lan or "
    ".home.arpa); earmark sends transcripts only to a model server on this machine or its "
    "private network"
)
_LOCAL_NETWORKS: Final = tuple(
    ipaddress.ip_network(n)
    for n in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7")
)
_DNS_LABEL: Final = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_LOCAL_SUFFIXES: Final = (".internal", ".local", ".lan", ".home.arpa")
LLM_MODEL_PATTERN: Final = re.compile(r"[^\s\x00-\x1f\x7f]{1,200}")


class ConfigError(Exception):
    """Settings could not be loaded. str(exc) is safe to show: it never contains input values."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("invalid configuration:\n  " + "\n  ".join(errors))


def env_var(field_name: str) -> str:
    return ENV_PREFIX + field_name.upper()


def is_loopback_ip(host: str) -> bool:
    """True for an IP literal in 127/8, ::1 or ::ffff:127.*; False for names and others."""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_api_url(url: str) -> str:
    """The URL as 'scheme://netloc' (scheme lower-cased); ValueError with one of the
    API_URL_* messages otherwise. Never puts the input in the message."""
    text = url.strip()
    try:
        parts = urlsplit(text)
        hostname = parts.hostname
        _ = parts.port
    except ValueError:
        raise ValueError(API_URL_SHAPE) from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not hostname:
        raise ValueError(API_URL_SHAPE)
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError(API_URL_CREDENTIALS)
    if parts.query or parts.fragment or "?" in text or "#" in text:
        raise ValueError(API_URL_QUERY)
    if parts.path not in ("", "/"):
        raise ValueError(API_URL_PATH)
    if scheme == "http" and not is_loopback_ip(hostname):
        raise ValueError(API_URL_PLAIN_HTTP)
    return f"{scheme}://{parts.netloc}"


def is_local_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for 127/8, 10/8, 172.16/12, 192.168/16, ::1 and fc00::/7 (an IPv4-mapped IPv6
    address is checked as its IPv4 address); false for everything else."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in net for net in _LOCAL_NETWORKS if net.version == address.version)


def is_local_host_name(name: str) -> bool:
    """True for localhost, a name under .localhost, a single label, and names under
    .internal, .local, .lan and .home.arpa; false otherwise (IP literals included)."""
    name = name.lower().removesuffix(".")
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        return False
    labels = name.split(".")
    if not all(_DNS_LABEL.fullmatch(label) for label in labels):
        return False
    if len(labels) == 1 or name == "localhost" or name.endswith(".localhost"):
        return True
    return name.endswith(_LOCAL_SUFFIXES)


def check_local_base_url(url: str) -> str:
    """The URL as 'scheme://netloc'; ValueError with one of the LLM_URL_* messages
    otherwise. Never puts the input in the message."""
    text = url.strip()
    try:
        parts = urlsplit(text)
        hostname = parts.hostname
        _ = parts.port
    except ValueError:
        raise ValueError(LLM_URL_SHAPE) from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not hostname:
        raise ValueError(LLM_URL_SHAPE)
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError(LLM_URL_CREDENTIALS)
    if parts.query or parts.fragment or "?" in text or "#" in text:
        raise ValueError(LLM_URL_QUERY)
    if parts.path not in ("", "/"):
        raise ValueError(LLM_URL_PATH)
    try:
        local = is_local_ip(ipaddress.ip_address(hostname))
    except ValueError:
        local = is_local_host_name(hostname)
    if not local:
        raise ValueError(LLM_URL_NOT_LOCAL)
    return f"{scheme}://{parts.netloc}"


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
    api_url: str | None = Field(
        None, description="The running API's URL; jobs, submit and transcript then call it."
    )
    host: str = Field("127.0.0.1", description="Address the API binds to.")
    port: int = Field(8787, ge=1, le=65535, description="Port the API binds to.")
    data_dir: Path = Field(Path("./data"), description="Working data (jobs DB, episode cache).")
    resolver_backend: ResolverBackend = Field("apple", description="Episode URL resolver.")
    fetch_backend: FetchBackend = Field("http", description="Audio/feed fetcher.")
    youtube_backend: YoutubeBackend = Field("ytdlp", description="YouTube source.")
    asr_backend: AsrBackend = Field("faster_whisper", description="Transcription backend.")
    notify_backend: NotifyBackend = Field("ntfy", description="Notifier.")
    mail_backend: MailBackend = Field("smtp", description="Mail sender.")
    asr_model: str = Field("large-v3", description="Whisper model name.")
    asr_device: AsrDevice = Field("auto", description="Whisper device.")
    asr_compute_type: AsrComputeType = Field("auto", description="Whisper compute type.")
    asr_batch_size: int = Field(
        0,
        ge=0,
        le=ASR_BATCH_SIZE_MAX,
        description="Batched faster-whisper decoding: chunks per batch (0 is off).",
    )
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
    allowed_recipients: Annotated[tuple[str, ...], NoDecode] = Field(
        (), description="Who may receive transcripts: comma-separated addresses, or all."
    )
    max_upload_mb: int = Field(1024, gt=0, description="Upload size cap (MiB).")
    dedupe_window_s: int = Field(600, ge=0, description="Seconds a finished job answers repeats.")
    ntfy_url: SecretStr | None = Field(None, description="ntfy topic URL (secret).")
    ntfy_token: SecretStr | None = Field(None, description="ntfy access token.")
    max_audio_mb: int = Field(1024, gt=0, description="Audio download size cap.")
    max_feed_mb: int = Field(20, gt=0, description="Feed download size cap.")
    allow_private_fetch: bool = Field(False, description="Allow private-address fetches.")
    job_max_attempts: int = Field(4, ge=1, description="Attempts before a job fails.")
    job_lease_seconds: int = Field(900, gt=0, description="Job lease duration.")
    worker_poll_seconds: float = Field(2.0, gt=0, description="Worker poll interval.")
    smtp_host: str | None = Field(None, description="SMTP submission server host name.")
    smtp_port: int | None = Field(
        None, ge=1, le=65535, description="SMTP port (default 587, or 465 for tls)."
    )
    smtp_security: SmtpSecurity = Field("starttls", description="starttls or tls (implicit).")
    smtp_username: str | None = Field(None, description="SMTP login name.")
    smtp_password: SecretStr | None = Field(None, description="SMTP login password.")
    smtp_from: str | None = Field(None, description="The sender address (the From header).")
    smtp_timeout_s: float = Field(
        60.0, gt=0, le=SMTP_TIMEOUT_MAX_S, description="Whole-session SMTP deadline (seconds)."
    )
    max_attachment_mb: int = Field(
        10, gt=0, description="Cap on the transcript and instructions together (MiB)."
    )
    llm_backend: LlmBackend = Field(
        "none",
        description="Summaries (POST /v1/summaries): none (off), ollama, llama_cpp, vllm or fake.",
    )
    llm_base_url: str | None = Field(
        None,
        description="The local model server's root URL (loopback, private or a local name).",
    )
    llm_model: str | None = Field(None, description="The model name the server serves.")
    llm_api_key: SecretStr | None = Field(None, description="The model server's API key.")
    llm_context_tokens: int | None = Field(
        None,
        ge=LLM_MIN_CONTEXT_TOKENS,
        description="ollama only: the context window earmark asks for (num_ctx).",
    )
    llm_max_output_tokens: int = Field(
        8192,
        ge=256,
        le=LLM_MAX_OUTPUT_TOKENS_LIMIT,
        description="The most tokens a summary may have.",
    )
    llm_timeout_seconds: float = Field(
        3600.0, gt=0, description="How long to wait for the model server's answer."
    )
    llm_max_transcript_chars: int = Field(
        400_000,
        ge=1000,
        le=LLM_MAX_TRANSCRIPT_CHARS_LIMIT,
        description="Longest transcript POST /v1/summaries accepts (characters).",
    )
    log_level: LogLevel = Field("INFO", description="Log level.")

    @field_validator("llm_backend", mode="before")
    @classmethod
    def _lower_llm_backend(cls, v: object) -> object:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("llm_base_url")
    @classmethod
    def _check_llm_base_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return check_local_base_url(v) if v else None

    @field_validator("llm_model")
    @classmethod
    def _check_llm_model(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if LLM_MODEL_PATTERN.fullmatch(v) is None:
            raise ValueError(
                "must be a model name of at most 200 characters, without spaces or control "
                "characters"
            )
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v: object) -> object:
        return v.upper() if isinstance(v, str) else v

    @field_validator("allowed_recipients", mode="before")
    @classmethod
    def _split_recipients(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.split(",")
        if not isinstance(v, list | tuple):
            return v
        entries = [e.strip() if isinstance(e, str) else e for e in v]
        entries = [e for e in entries if e != ""]
        if any(isinstance(e, str) and e.lower() == ALL for e in entries):
            if len(entries) != 1:
                raise ValueError("all cannot be combined with addresses")
            return (ALL,)
        out: list[str] = []
        for number, entry in enumerate(entries, start=1):
            try:
                address = normalize_address(entry) if isinstance(entry, str) else None
            except ValueError:
                address = None
            if address is None:
                raise ValueError(f"entry {number} is not an email address")
            if address not in out:
                out.append(address)
        return tuple(out)

    @field_validator("api_url")
    @classmethod
    def _check_api_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return check_api_url(v) if v else None

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

    @field_validator("smtp_security", mode="before")
    @classmethod
    def _lower_smtp_security(cls, v: object) -> object:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("smtp_host")
    @classmethod
    def _check_smtp_host(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v or any(c.isspace() for c in v) or "/" in v or "@" in v or "://" in v:
            raise ValueError("must be a host name such as smtp.example.org")
        return v

    @field_validator("smtp_username")
    @classmethod
    def _check_smtp_username(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if not all("!" <= c <= "~" for c in v):
            raise ValueError("must be printable ASCII without spaces")
        return v

    @field_validator("smtp_password")
    @classmethod
    def _check_smtp_password(cls, v: SecretStr | None) -> SecretStr | None:
        if v is None:
            return None
        stripped = v.get_secret_value().strip()
        if not stripped:
            return None
        if not all(" " <= c <= "~" for c in stripped):
            raise ValueError("must be printable ASCII (spaces allowed), without control characters")
        return SecretStr(stripped)

    @field_validator("smtp_from")
    @classmethod
    def _check_smtp_from(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            return normalize_address(v)
        except ValueError:
            raise ValueError("must be one email address") from None

    @field_validator("asr_api_key", "ntfy_token", "llm_api_key")
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

    @property
    def smtp_port_effective(self) -> int:
        return self.smtp_port or SMTP_DEFAULT_PORTS[self.smtp_security]

    def recipient_policy(self) -> RecipientPolicy:
        return RecipientPolicy.from_setting(self.allowed_recipients)

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
            elif name == "allowed_recipients":
                display = self.recipient_policy().describe()
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
