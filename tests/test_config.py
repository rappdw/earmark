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
    assert s.vault_dir is None
    assert s.vault_subdir == "Earmark"
    assert s.obsidian_vault_name is None
    assert s.resolver_backend == "apple"
    assert s.fetch_backend == "http"
    assert s.asr_backend == "faster_whisper"
    assert s.llm_provider == "anthropic"
    assert s.store_backend == "vault"
    assert s.index_backend == "lancedb"
    assert s.notify_backend == "ntfy"
    assert (s.asr_model, s.asr_device, s.asr_compute_type) == ("large-v3", "auto", "auto")
    assert s.asr_language is None
    assert s.asr_base_url is None
    assert s.asr_api_key is None
    assert s.asr_max_upload_mb is None
    assert s.keep_audio is False
    assert s.llm_model == "claude-opus-5-5"
    assert s.llm_effort == "medium"
    assert s.llm_fallbacks is True
    assert s.llm_base_url is None
    assert s.llm_api_key is None
    assert s.llm_context_tokens is None
    assert s.llm_timeout_seconds == 3600.0
    assert s.llm_response_format == "json_schema"
    assert s.embed_model == "BAAI/bge-small-en-v1.5"
    assert s.embed_model_dir is None
    assert s.ntfy_url is None
    assert s.ntfy_token is None
    assert s.max_audio_mb == 1024
    assert s.max_feed_mb == 20
    assert s.allow_private_fetch is False
    assert s.job_max_attempts == 4
    assert s.job_lease_seconds == 900
    assert s.worker_poll_seconds == 2.0
    assert s.tz is None
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
    monkeypatch.setenv("TZ", "Mars/Base")
    s = load_settings(env_file=None)
    assert s.port == 8787
    assert s.tz is None


def test_unknown_earmark_key_ignored(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("EARMARK_PROT=1\n")
    assert load_settings().port == 8787


def test_empty_values_are_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_API_TOKEN", "")
    monkeypatch.setenv("EARMARK_ASR_LANGUAGE", "")
    monkeypatch.setenv("EARMARK_VAULT_SUBDIR", "")
    s = load_settings(env_file=None)
    assert s.api_token is None
    assert s.asr_language is None
    assert s.vault_subdir == "Earmark"


def test_secret_fields_are_secretstr() -> None:
    names = {
        n for n in Settings.model_fields if n.endswith(("_token", "_key", "_secret", "_password"))
    }
    for n in names:
        assert Settings.model_fields[n].annotation == SecretStr | None, n
    assert names == {"api_token", "asr_api_key", "llm_api_key", "ntfy_token"}


_SECRETS = {
    "EARMARK_API_TOKEN": "sentinel-a1",
    "EARMARK_ASR_API_KEY": "sentinel-b2",
    "EARMARK_LLM_API_KEY": "sentinel-c3",
    "EARMARK_NTFY_TOKEN": "sentinel-d4",
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
    monkeypatch.setenv("EARMARK_LLM_API_KEY", "only-one")
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
        ("EARMARK_INDEX_BACKEND", "chroma-sentinel", True),
        ("EARMARK_TZ", "Mars/Base", True),
        ("EARMARK_LOG_LEVEL", "verbose-sentinel", True),
        ("EARMARK_VAULT_SUBDIR", "../x", True),
        ("EARMARK_VAULT_SUBDIR", "/abs", True),
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
        ("EARMARK_LLM_BASE_URL", "ftp://sentinel-host/v1", True),
        ("EARMARK_LLM_BASE_URL", "sentinel-nohost", True),
        ("EARMARK_LLM_BASE_URL", "http://user:sentinel-pw@h/v1", True),
        ("EARMARK_LLM_BASE_URL", "http://h/v1?k=sentinel-q", True),
        ("EARMARK_LLM_BASE_URL", "http://h:99999/v1", False),
        ("EARMARK_LLM_TIMEOUT_SECONDS", "0", False),
        ("EARMARK_LLM_RESPONSE_FORMAT", "xml-sentinel", True),
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


def test_backend_none_allowed_for_index_and_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_INDEX_BACKEND", "none")
    monkeypatch.setenv("EARMARK_NOTIFY_BACKEND", "none")
    s = load_settings(env_file=None)
    assert (s.index_backend, s.notify_backend) == ("none", "none")


def test_all_backends_accept_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "RESOLVER_BACKEND",
        "FETCH_BACKEND",
        "ASR_BACKEND",
        "LLM_PROVIDER",
        "STORE_BACKEND",
        "INDEX_BACKEND",
        "NOTIFY_BACKEND",
    ):
        monkeypatch.setenv(f"EARMARK_{name}", "fake")
    s = load_settings(env_file=None)
    assert s.resolver_backend == s.fetch_backend == s.asr_backend == "fake"
    assert s.llm_provider == s.store_backend == s.index_backend == s.notify_backend == "fake"


def test_log_level_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_LOG_LEVEL", "debug")
    assert load_settings(env_file=None).log_level == "DEBUG"


@pytest.mark.parametrize("tz", ["Europe/Oslo", "UTC"])
def test_valid_tz_accepted(monkeypatch: pytest.MonkeyPatch, tz: str) -> None:
    monkeypatch.setenv("EARMARK_TZ", tz)
    assert load_settings(env_file=None).tz == tz


def test_nested_vault_subdir_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_VAULT_SUBDIR", "Notes/Earmark")
    assert load_settings(env_file=None).vault_subdir == "Notes/Earmark"


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


def test_llm_settings_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_LLM_EFFORT", " HIGH ")
    monkeypatch.setenv("EARMARK_LLM_MODEL", " claude-x ")
    monkeypatch.setenv("EARMARK_LLM_API_KEY", " sk-x\n")
    monkeypatch.setenv("EARMARK_LLM_FALLBACKS", "false")
    s = load_settings(env_file=None)
    assert (s.llm_effort, s.llm_model, s.llm_fallbacks) == ("high", "claude-x", False)
    assert s.llm_api_key is not None
    assert s.llm_api_key.get_secret_value() == "sk-x"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("EARMARK_LLM_EFFORT", "extreme-sentinel"),
        ("EARMARK_LLM_MODEL", "   "),
        ("EARMARK_LLM_API_KEY", "a b-sentinel"),
    ],
)
def test_llm_settings_rejected(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)
    [line] = info.value.errors
    assert line.startswith(f"{name}: ")
    assert "sentinel" not in str(info.value)
    if name == "EARMARK_LLM_API_KEY":
        assert "printable ASCII without spaces" in line


def test_llm_api_key_blank_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_LLM_API_KEY", "   ")
    s = load_settings(env_file=None)
    assert s.llm_api_key is None
    assert s.secret_values() == []


def test_llm_base_url_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_LLM_BASE_URL", "http://user:pw@h/v1")
    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)
    assert info.value.errors == [
        "EARMARK_LLM_BASE_URL: Value error, must not contain credentials; use EARMARK_LLM_API_KEY"
    ]
    monkeypatch.setenv("EARMARK_ASR_BASE_URL", "http://user:pw@h/v1")
    monkeypatch.delenv("EARMARK_LLM_BASE_URL")
    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)
    assert "use EARMARK_ASR_API_KEY" in info.value.errors[0]
    monkeypatch.delenv("EARMARK_ASR_BASE_URL")
    assert Settings(_env_file=None, llm_base_url=" http://h:8000/v1/ ").llm_base_url == (
        "http://h:8000/v1"
    )


def test_llm_response_format_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_LLM_RESPONSE_FORMAT", " JSON_OBJECT ")
    assert load_settings(env_file=None).llm_response_format == "json_object"


def test_embed_model_stripped_and_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_EMBED_MODEL", "  BAAI/bge-base-en-v1.5 ")
    assert load_settings(env_file=None).embed_model == "BAAI/bge-base-en-v1.5"
    monkeypatch.setenv("EARMARK_EMBED_MODEL", "\t")
    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)
    assert "EARMARK_EMBED_MODEL" in str(info.value)


def test_embed_model_dir_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_EMBED_MODEL_DIR", "/models/embed")
    assert load_settings(env_file=None).embed_model_dir == Path("/models/embed")


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


def test_obsidian_vault_name_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_OBSIDIAN_VAULT_NAME", "  My Vault é ")
    assert load_settings(env_file=None).obsidian_vault_name == "My Vault é"
    monkeypatch.setenv("EARMARK_OBSIDIAN_VAULT_NAME", "   ")
    assert load_settings(env_file=None).obsidian_vault_name is None
    for bad, message in (("a\nb", "control characters"), ("a‮b", "control characters")):
        monkeypatch.setenv("EARMARK_OBSIDIAN_VAULT_NAME", bad)
        with pytest.raises(ConfigError) as info:
            load_settings(env_file=None)
        assert message in info.value.errors[0]
    monkeypatch.setenv("EARMARK_OBSIDIAN_VAULT_NAME", "v" * 201)
    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)
    assert "at most 200 characters" in info.value.errors[0]
