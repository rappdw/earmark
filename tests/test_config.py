import re
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from earmark.config import ConfigError, Settings, env_var, load_settings, redact_url

ROOT = Path(__file__).resolve().parents[1]


def test_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.api_token is None
    assert s.host == "127.0.0.1"
    assert s.port == 8787
    assert s.data_dir == Path("./data")
    assert s.resolver_backend == "apple"
    assert s.fetch_backend == "http"
    assert s.asr_backend == "faster_whisper"
    assert s.notify_backend == "ntfy"
    assert (s.asr_model, s.asr_device, s.asr_compute_type) == ("large-v3", "auto", "auto")
    assert s.asr_language is None
    assert s.asr_base_url is None
    assert s.asr_api_key is None
    assert s.asr_max_upload_mb is None
    assert s.keep_audio is False
    assert s.ntfy_url is None
    assert s.ntfy_token is None
    assert s.max_audio_mb == 1024
    assert s.max_feed_mb == 20
    assert s.allow_private_fetch is False
    assert s.job_max_attempts == 4
    assert s.job_lease_seconds == 900
    assert s.worker_poll_seconds == 2.0
    assert s.log_level == "INFO"


def test_reads_env_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_PORT", "9000")
    monkeypatch.setenv("EARMARK_KEEP_AUDIO", "true")
    s = load_settings(env_file=None)
    assert s.port == 9000
    assert s.keep_audio is True


def test_reads_dotenv_in_cwd(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("EARMARK_PORT=9001\n")
    assert load_settings().port == 9001


def test_env_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("EARMARK_PORT=9001\n")
    monkeypatch.setenv("EARMARK_PORT", "9002")
    assert load_settings().port == 9002


def test_unprefixed_vars_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "1")
    monkeypatch.setenv("HOST", "0.0.0.0")  # noqa: S104  # a test value, never bound
    s = load_settings(env_file=None)
    assert s.port == 8787
    assert s.host == "127.0.0.1"


def test_unknown_earmark_key_ignored(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("EARMARK_PROT=1\n")
    assert load_settings().port == 8787


def test_empty_values_are_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_API_TOKEN", "")
    monkeypatch.setenv("EARMARK_ASR_LANGUAGE", "")
    monkeypatch.setenv("EARMARK_HOST", "")
    s = load_settings(env_file=None)
    assert s.api_token is None
    assert s.asr_language is None
    assert s.host == "127.0.0.1"


def test_secret_fields_are_secretstr() -> None:
    names = {
        n for n in Settings.model_fields if n.endswith(("_token", "_key", "_secret", "_password"))
    }
    for n in names:
        assert Settings.model_fields[n].annotation == SecretStr | None, n
    assert names == {"api_token", "asr_api_key", "ntfy_token"}


_SECRETS = {
    "EARMARK_API_TOKEN": "sentinel-a1",
    "EARMARK_ASR_API_KEY": "sentinel-b2",
    "EARMARK_NTFY_TOKEN": "sentinel-c3",
}


def test_secrets_hidden_in_repr_and_str(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _SECRETS.items():
        monkeypatch.setenv(k, v)
    s = load_settings(env_file=None)
    for v in _SECRETS.values():
        assert v not in repr(s)
        assert v not in str(s)
    assert sorted(s.secret_values()) == sorted(_SECRETS.values())


def test_secret_values_lists_only_set_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(_env_file=None).secret_values() == []
    monkeypatch.setenv("EARMARK_ASR_API_KEY", "only-one")
    assert load_settings(env_file=None).secret_values() == ["only-one"]


@pytest.mark.parametrize(
    ("name", "value", "echo_check"),
    [
        ("EARMARK_PORT", "0", False),
        ("EARMARK_PORT", "70000", False),
        ("EARMARK_JOB_MAX_ATTEMPTS", "0", False),
        ("EARMARK_WORKER_POLL_SECONDS", "0", False),
        ("EARMARK_MAX_AUDIO_MB", "-1", False),
        ("EARMARK_ASR_BACKEND", "whisperx-sentinel", True),
        ("EARMARK_RESOLVER_BACKEND", "none", False),
        ("EARMARK_LOG_LEVEL", "verbose-sentinel", True),
        ("EARMARK_ASR_DEVICE", "gpu-sentinel", True),
        ("EARMARK_ASR_COMPUTE_TYPE", "float8-sentinel", True),
        ("EARMARK_ASR_LANGUAGE", "english-sentinel", True),
        ("EARMARK_ASR_MODEL", " ", False),
        ("EARMARK_ASR_BASE_URL", "ftp://sentinel-host/v1", True),
        ("EARMARK_ASR_BASE_URL", "sentinel-nohost", True),
        ("EARMARK_ASR_BASE_URL", "http://user:sentinel-pw@h/v1", True),
        ("EARMARK_ASR_BASE_URL", "http://h/v1?k=sentinel-q", True),
        ("EARMARK_ASR_BASE_URL", "http://h:99999/v1", False),
        ("EARMARK_ASR_API_KEY", "sentinel key", True),
        ("EARMARK_ASR_TIMEOUT_SECONDS", "0", False),
    ],
)
def test_invalid_values_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, echo_check: bool
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)
    exc = info.value
    assert any(line.startswith(f"{name}:") for line in exc.errors)
    assert "input_value" not in str(exc)
    assert exc.__cause__ is None
    assert exc.__suppress_context__
    if echo_check:
        assert value not in str(exc)
        assert value.lower() not in str(exc).lower()


def test_notify_backend_none_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_NOTIFY_BACKEND", "none")
    assert load_settings(env_file=None).notify_backend == "none"


def test_all_backends_accept_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "RESOLVER_BACKEND",
        "FETCH_BACKEND",
        "ASR_BACKEND",
        "NOTIFY_BACKEND",
    ):
        monkeypatch.setenv(f"EARMARK_{name}", "fake")
    s = load_settings(env_file=None)
    assert s.resolver_backend == s.fetch_backend == s.asr_backend == s.notify_backend == "fake"


def test_log_level_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_LOG_LEVEL", "debug")
    assert load_settings(env_file=None).log_level == "DEBUG"


def test_settings_frozen() -> None:
    s = Settings(_env_file=None)
    with pytest.raises(ValidationError):
        Settings.__setattr__(s, "port", 1)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://bob:hunter2@ntfy.example/topic", "https://bob:****@ntfy.example/topic"),
        ("https://bob@ntfy.example/topic", "https://bob@ntfy.example/topic"),
        ("https://ntfy.example/topic", "https://ntfy.example/topic"),
        ("https://bob:pw@ntfy.example:8080/t?x=1", "https://bob:****@ntfy.example:8080/t?x=1"),
        ("not a url [", "not a url ["),
    ],
)
def test_redact_url(url: str, expected: str) -> None:
    assert redact_url(url) == expected


def test_env_example_lists_every_field() -> None:
    found: list[str] = []
    for line in (ROOT / ".env.example").read_text().splitlines():
        m = re.match(r"^#?\s*(EARMARK_[A-Z0-9_]+)=", line)
        if m:
            found.append(m.group(1))
    assert set(found) == {env_var(f) for f in Settings.model_fields}
    assert len(found) == len(set(found))


def test_env_example_values_are_defaults() -> None:
    assert Settings(_env_file=ROOT / ".env.example") == Settings(_env_file=None)


def test_asr_settings_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_ASR_DEVICE", "CUDA")
    monkeypatch.setenv("EARMARK_ASR_COMPUTE_TYPE", " Int8 ")
    monkeypatch.setenv("EARMARK_ASR_MODEL", "  small ")
    monkeypatch.setenv("EARMARK_ASR_LANGUAGE", "auto")
    s = load_settings(env_file=None)
    assert (s.asr_device, s.asr_compute_type, s.asr_model, s.asr_language) == (
        "cuda",
        "int8",
        "small",
        None,
    )
    monkeypatch.setenv("EARMARK_ASR_LANGUAGE", "Haw")
    assert load_settings(env_file=None).asr_language == "haw"


def test_asr_model_dir_path(monkeypatch: pytest.MonkeyPatch) -> None:
    assert load_settings(env_file=None).asr_model_dir is None
    monkeypatch.setenv("EARMARK_ASR_MODEL_DIR", "/models")
    assert load_settings(env_file=None).asr_model_dir == Path("/models")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://ntfy.sh/topic", "https://ntfy.sh/topic"),
        ("http://ntfy.tail1234.ts.net:8080/earmark", "http://ntfy.tail1234.ts.net:8080/earmark"),
        ("https://host/prefix/topic", "https://host/prefix/topic"),
        ("https://ntfy.sh/topic/", "https://ntfy.sh/topic"),
        ("  https://ntfy.sh/a_b-C9  ", "https://ntfy.sh/a_b-C9"),
    ],
)
def test_ntfy_url_accepted(monkeypatch: pytest.MonkeyPatch, url: str, expected: str) -> None:
    monkeypatch.setenv("EARMARK_NTFY_URL", url)
    s = load_settings(env_file=None)
    assert s.ntfy_url is not None
    assert s.ntfy_url.get_secret_value() == expected


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://ntfy.sh", "must end in the topic name"),
        ("https://ntfy.sh/", "must end in the topic name"),
        ("https://ntfy.sh/sentinel.topic", "must end in the topic name"),
        ("https://ntfy.sh/" + "s" * 65, "must end in the topic name"),
        ("https://bob:sentinel-pw@ntfy.sh/topic", "EARMARK_NTFY_TOKEN"),
        ("https://ntfy.sh/topic?auth=sentinel-q", "query or fragment"),
        ("https://ntfy.sh/topic#sentinel-f", "query or fragment"),
        ("ftp://ntfy.sh/sentinel-topic", "http(s) topic URL"),
        ("sentinel-nohost", "http(s) topic URL"),
    ],
)
def test_ntfy_url_refused(monkeypatch: pytest.MonkeyPatch, url: str, message: str) -> None:
    monkeypatch.setenv("EARMARK_NTFY_URL", url)
    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)
    [line] = info.value.errors
    assert line.startswith("EARMARK_NTFY_URL: ")
    assert message in line
    assert "sentinel" not in line


def test_ntfy_url_is_secretstr(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings.model_fields["ntfy_url"].annotation == SecretStr | None
    monkeypatch.setenv("EARMARK_NTFY_URL", "https://ntfy.sh/sentinel-topic-5")
    s = load_settings(env_file=None)
    assert "https://ntfy.sh/sentinel-topic-5" in s.secret_values()
    assert "sentinel-topic-5" not in repr(s)
    assert ("EARMARK_NTFY_URL", "**********") in s.redacted_items()


def test_ntfy_token_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_NTFY_TOKEN", "  tk_abc123  ")
    token = load_settings(env_file=None).ntfy_token
    assert token is not None
    assert token.get_secret_value() == "tk_abc123"
    monkeypatch.setenv("EARMARK_NTFY_TOKEN", "sentinel token")
    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)
    [line] = info.value.errors
    assert line.startswith("EARMARK_NTFY_TOKEN: ")
    assert "printable ASCII without spaces" in line
    assert "sentinel" not in line
    monkeypatch.setenv("EARMARK_NTFY_TOKEN", "   ")
    assert load_settings(env_file=None).ntfy_token is None


def test_secret_values_include_bare_ntfy_topic() -> None:
    url = "https://ntfy.sh/sentinel-topic-5"
    s = Settings(_env_file=None, ntfy_url=SecretStr(url))
    assert s.secret_values() == [url, "sentinel-topic-5"]


def test_secret_values_skip_short_ntfy_topic() -> None:
    url = "https://ntfy.example/earmark"
    assert Settings(_env_file=None, ntfy_url=SecretStr(url)).secret_values() == [url]
    eleven = "https://ntfy.example/abcdefghijk"
    assert Settings(_env_file=None, ntfy_url=SecretStr(eleven)).secret_values() == [eleven]


def test_secret_values_topic_with_path_prefix() -> None:
    url = "https://host/ntfy/sentinel-topic-77"
    assert Settings(_env_file=None, ntfy_url=SecretStr(url)).secret_values() == [
        url,
        "sentinel-topic-77",
    ]


def test_settings_fields_are_exactly() -> None:
    assert tuple(Settings.model_fields) == (
        "api_token",
        "host",
        "port",
        "data_dir",
        "resolver_backend",
        "fetch_backend",
        "asr_backend",
        "notify_backend",
        "asr_model",
        "asr_device",
        "asr_compute_type",
        "asr_language",
        "asr_model_dir",
        "asr_base_url",
        "asr_api_key",
        "asr_max_upload_mb",
        "asr_timeout_seconds",
        "keep_audio",
        "ntfy_url",
        "ntfy_token",
        "max_audio_mb",
        "max_feed_mb",
        "allow_private_fetch",
        "job_max_attempts",
        "job_lease_seconds",
        "worker_poll_seconds",
        "log_level",
    )
