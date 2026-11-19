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
    assert s.llm_base_url is None
    assert s.llm_api_key is None
    assert s.llm_context_tokens is None
    assert s.embed_model == "BAAI/bge-small-en-v1.5"
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
