"""``run_doctor`` offline and online, through ``Probes`` (no network, sockets blocked)."""

import json
import os
import sqlite3
import sys
import types
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from pydantic import SecretStr

from earmark.config import Settings
from earmark.db import MIGRATIONS, SCHEMA_VERSION, open_database
from earmark.doctor import ONLINE_TIMEOUT_S, Probes, run_doctor
from earmark.doctor.report import CheckResult, exit_code, render, summary
from earmark.fetch import GuardedFetcher
from earmark.index import embed as embed_module
from earmark.index.embed import ModelInfo
from earmark.index.lancedb_index import META_DIM, META_MODEL, META_VERSION, LanceRuntime
from earmark.jobs import open_job_store
from earmark.transcribe.compat import Installed
from tests.anthropic_stub import KEY, AnthropicStub, error_reply, model_info_body
from tests.http_helpers import ScriptedNetwork, StaticResolver
from tests.index_helpers import BGE, stub_extra_missing, stub_fastembed_runtime
from tests.ntfy_stub import (
    ACCOUNT_ANON,
    ACCOUNT_EXHAUSTED,
    HEALTH_OK,
    TOKEN,
    TOPIC,
    TOPIC_URL,
    UNAUTHORIZED,
    json_reply,
)

TOKEN_OK = "t" * 40
ASR_URL = "http://asr.test:8000/v1"
LLM_URL = "http://llm.test:8000/v1"
NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")

FAKES: dict[str, Any] = {
    "resolver_backend": "fake",
    "fetch_backend": "fake",
    "asr_backend": "fake",
    "llm_provider": "fake",
    "store_backend": "fake",
    "index_backend": "none",
    "notify_backend": "none",
}


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    (tmp_path / "vault").mkdir(exist_ok=True)
    values: dict[str, Any] = {
        **FAKES,
        "api_token": SecretStr(TOKEN_OK),
        "data_dir": tmp_path / "data",
        "vault_dir": tmp_path / "vault",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def check(
    settings: Settings, *, online: bool = False, probes: Probes | None = None
) -> dict[str, CheckResult]:
    results = run_doctor(settings, online=online, probes=probes)
    named = {r.name: r for r in results}
    assert len(named) == len(results), "check names must be unique"
    return named


def status(settings: Settings, name: str, **kw: Any) -> CheckResult:
    return check(settings, **kw)[name]


def tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


# -- the api token ------------------------------------------------------------------------------


def test_doctor_token_missing_fails(tmp_path: Path) -> None:
    result = status(make_settings(tmp_path, api_token=None), "api token")
    assert result.status == "fail"
    assert "EARMARK_API_TOKEN is not set" in result.detail


def test_doctor_token_short_warns(tmp_path: Path) -> None:
    result = status(make_settings(tmp_path, api_token=SecretStr("short")), "api token")
    assert result.status == "warn"
    assert "shorter than 32 characters" in result.detail
    assert "token_urlsafe" in result.detail
    assert "short" not in result.detail.replace("shorter", "")


def test_doctor_token_non_ascii_fails(tmp_path: Path) -> None:
    result = status(make_settings(tmp_path, api_token=SecretStr("tökén" * 10)), "api token")
    assert result.status == "fail"
    assert "printable ASCII" in result.detail


def test_doctor_token_ok_prints_no_value(tmp_path: Path) -> None:
    result = status(make_settings(tmp_path), "api token")
    assert result == CheckResult("api token", "ok", "set")


# -- the data directory ---------------------------------------------------------------------------


def test_doctor_data_dir_missing_ok_creates_nothing(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    result = status(settings, "data dir")
    assert result.status == "ok"
    assert "does not exist yet" in result.detail
    assert not settings.data_dir.exists()


def test_doctor_data_dir_is_file_fails(tmp_path: Path) -> None:
    (tmp_path / "data").write_text("x")
    result = status(make_settings(tmp_path), "data dir")
    assert (result.status, result.detail.endswith("is not a directory")) == ("fail", True)


@NOT_ROOT
def test_doctor_data_dir_readonly_fails(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    data.chmod(0o500)
    try:
        result = status(make_settings(tmp_path), "data dir")
    finally:
        data.chmod(0o700)
    assert result.status == "fail"
    assert "EACCES" in result.detail


@NOT_ROOT
def test_doctor_data_dir_missing_under_readonly_parent_fails(tmp_path: Path) -> None:
    parent = tmp_path / "ro"
    parent.mkdir()
    parent.chmod(0o500)
    try:
        result = status(make_settings(tmp_path, data_dir=parent / "data"), "data dir")
    finally:
        parent.chmod(0o700)
    assert result.status == "fail"


def test_doctor_data_dir_probe_leaves_nothing(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    settings = make_settings(tmp_path)
    before = tree(tmp_path)
    result = status(settings, "data dir")
    assert result.status == "ok"
    assert tree(tmp_path) == before


# -- the vault ------------------------------------------------------------------------------------


def vault_settings(tmp_path: Path, **overrides: Any) -> Settings:
    return make_settings(tmp_path, store_backend="vault", **overrides)


def test_doctor_vault_unset_fails(tmp_path: Path) -> None:
    result = status(vault_settings(tmp_path, vault_dir=None), "vault")
    assert result.status == "fail"
    assert "EARMARK_VAULT_DIR: required" in result.detail


def test_doctor_vault_missing_fails(tmp_path: Path) -> None:
    result = status(vault_settings(tmp_path, vault_dir=tmp_path / "nope"), "vault")
    assert result.status == "fail"
    assert "does not exist" in result.detail


def test_doctor_vault_subdir_missing_ok(tmp_path: Path) -> None:
    settings = vault_settings(tmp_path)
    result = status(settings, "vault")
    assert result.status == "ok"
    assert "does not exist yet" in result.detail
    assert list((tmp_path / "vault").iterdir()) == []


def test_doctor_vault_subdir_symlink_fails(tmp_path: Path) -> None:
    (tmp_path / "vault").mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "vault" / "Earmark").symlink_to(tmp_path / "elsewhere")
    result = status(vault_settings(tmp_path), "vault")
    assert result.status == "fail"
    assert "symlink" in result.detail
    assert list((tmp_path / "elsewhere").iterdir()) == []


@NOT_ROOT
def test_doctor_vault_readonly_fails(tmp_path: Path) -> None:
    subdir = tmp_path / "vault" / "Earmark"
    subdir.mkdir(parents=True)
    subdir.chmod(0o500)
    try:
        existing = status(vault_settings(tmp_path), "vault")
    finally:
        subdir.chmod(0o700)
    assert existing.status == "fail"
    assert "is not writable" in existing.detail
    subdir.rmdir()
    (tmp_path / "vault").chmod(0o500)
    try:
        missing = status(vault_settings(tmp_path), "vault")
    finally:
        (tmp_path / "vault").chmod(0o700)
    assert (missing.status, missing.detail) == ("fail", "EARMARK_VAULT_DIR is not writable")


def test_doctor_vault_probe_leaves_nothing(tmp_path: Path) -> None:
    subdir = tmp_path / "vault" / "Earmark"
    subdir.mkdir(parents=True)
    before = tree(tmp_path)
    result = status(vault_settings(tmp_path), "vault")
    assert result.status == "ok"
    assert "is writable" in result.detail
    assert tree(tmp_path) == before


def test_doctor_fake_store_warns(tmp_path: Path) -> None:
    assert status(make_settings(tmp_path), "vault").status == "warn"


# -- the database ---------------------------------------------------------------------------------


def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "earmark.db"


def test_doctor_database_missing_not_created(tmp_path: Path) -> None:
    result = status(make_settings(tmp_path), "database")
    assert result.status == "ok"
    assert "not created yet" in result.detail
    assert not db_path(tmp_path).exists()
    assert not (tmp_path / "data").exists()


def test_doctor_database_ok_counts_jobs(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = open_job_store(settings)
    store.enqueue("https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000802907981")
    store.enqueue("https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000802907982")
    result = status(settings, "database")
    assert result.status == "ok"
    assert result.detail == (f"schema {SCHEMA_VERSION}, 2 jobs (queued 2, running 0, failed 0)")


def test_doctor_database_newer_fails(tmp_path: Path) -> None:
    open_database(db_path(tmp_path))
    with closing(sqlite3.connect(db_path(tmp_path))) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    result = status(make_settings(tmp_path), "database")
    assert result.status == "fail"
    assert "newer than this earmark" in result.detail
    assert "upgrade earmark" in result.detail


def test_doctor_database_older_warns_and_is_not_migrated(tmp_path: Path) -> None:
    open_database(db_path(tmp_path), migrations=MIGRATIONS[:1])
    result = status(make_settings(tmp_path), "database")
    assert result.status == "warn"
    assert "schema version 1" in result.detail
    with closing(sqlite3.connect(db_path(tmp_path))) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_doctor_database_not_wal_warns(tmp_path: Path) -> None:
    db_path(tmp_path).parent.mkdir()
    with closing(sqlite3.connect(db_path(tmp_path))) as conn:
        for migration in MIGRATIONS:
            for statement in migration.statements:
                conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    result = status(make_settings(tmp_path), "database")
    assert result.status == "warn"
    assert "not WAL" in result.detail


def test_doctor_database_corrupt_fails(tmp_path: Path) -> None:
    db_path(tmp_path).parent.mkdir()
    db_path(tmp_path).write_bytes(os.urandom(4096))
    result = status(make_settings(tmp_path), "database")
    assert result.status == "fail"
    assert result.detail.startswith("DatabaseError: ")


# -- extras of the selected backends --------------------------------------------------------------


class WhisperStubs:
    """Stand-ins for faster_whisper, its utils module, and ctranslate2."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, cuda: int = 0) -> None:
        self.downloads: list[dict[str, Any]] = []
        self.models_built = 0
        self.behaviour: Any = "/cache/snapshot"
        fw = types.ModuleType("faster_whisper")
        utils = types.ModuleType("faster_whisper.utils")
        ct2 = types.ModuleType("ctranslate2")

        def download_model(name: str, **kwargs: Any) -> str:
            self.downloads.append({"name": name, **kwargs})
            if isinstance(self.behaviour, Exception):
                raise self.behaviour
            return str(self.behaviour)

        def whisper_model(*args: Any, **kwargs: Any) -> None:
            self.models_built += 1

        utils.download_model = download_model  # type: ignore[attr-defined]  # stub modules
        fw.WhisperModel = whisper_model  # type: ignore[attr-defined]
        ct2.get_cuda_device_count = lambda: cuda  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "faster_whisper", fw)
        monkeypatch.setitem(sys.modules, "faster_whisper.utils", utils)
        monkeypatch.setitem(sys.modules, "ctranslate2", ct2)
        av = types.ModuleType("av")
        av_error = types.ModuleType("av.error")
        av.__dict__["error"] = av_error
        monkeypatch.setitem(sys.modules, "av", av)
        monkeypatch.setitem(sys.modules, "av.error", av_error)


def whisper_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "asr_backend": "faster_whisper",
        "asr_device": "cpu",
        "asr_model": "small",
    }
    values.update(overrides)
    return make_settings(tmp_path, **values)


def test_doctor_whisper_extra_missing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    results = check(whisper_settings(tmp_path))
    assert results["asr"].status == "fail"
    assert "uv sync --extra transcribe" in results["asr"].detail
    assert "asr model" not in results


def test_doctor_whisper_ok_with_stub_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stubs = WhisperStubs(monkeypatch)
    settings = whisper_settings(tmp_path)
    results = check(settings)
    assert results["asr"] == CheckResult(
        "asr", "ok", "faster_whisper, model small, device cpu, compute auto"
    )
    assert results["asr model"] == CheckResult("asr model", "ok", "downloaded")
    assert [d["local_files_only"] for d in stubs.downloads] == [True]
    assert stubs.models_built == 0

    stubs.behaviour = FileNotFoundError("not cached")
    missing = check(settings)["asr model"]
    assert missing.status == "warn"
    assert "not downloaded yet" in missing.detail

    stubs.behaviour = ValueError("Invalid model size")
    unknown = check(settings)["asr model"]
    assert unknown.status == "fail"
    assert "EARMARK_ASR_MODEL" in unknown.detail
    assert stubs.models_built == 0


def _lookup(fw: str | None, av: str | None) -> Callable[[str], Installed | None]:
    versions = {"faster-whisper": fw, "av": av}

    def lookup(name: str) -> Installed | None:
        version = versions[name]
        return None if version is None else Installed(version, from_source=False)

    return lookup


def test_doctor_asr_libraries_incompatible_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stubs = WhisperStubs(monkeypatch)
    probes = Probes(installed=_lookup("1.2.1", "19.0.2"))
    settings = whisper_settings(tmp_path)
    results = run_doctor(settings, probes=probes)
    names = [r.name for r in results]
    assert names.index("asr") + 1 == names.index("asr libraries")
    assert names.index("asr libraries") + 1 == names.index("asr model")
    libs = {r.name: r for r in results}["asr libraries"]
    assert libs.status == "warn"
    assert "known-incompatible" in libs.detail
    assert stubs.models_built == 0
    assert exit_code(results) == 0


def test_doctor_asr_libraries_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    WhisperStubs(monkeypatch)
    results = check(whisper_settings(tmp_path), probes=Probes(installed=_lookup("1.2.1", "18.1.0")))
    assert results["asr libraries"] == CheckResult(
        "asr libraries", "ok", "faster-whisper 1.2.1, av 18.1.0"
    )


def test_doctor_asr_libraries_unknown_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    WhisperStubs(monkeypatch)
    results = check(whisper_settings(tmp_path), probes=Probes(installed=_lookup(None, None)))
    assert results["asr libraries"].status == "warn"


@pytest.mark.parametrize("case", ["extra-missing", "remote", "fake"])
def test_doctor_asr_libraries_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    probes = Probes(installed=_lookup("1.2.1", "19.0.2"))
    if case == "extra-missing":
        monkeypatch.setitem(sys.modules, "faster_whisper", None)
        settings = whisper_settings(tmp_path)
    elif case == "remote":
        settings = make_settings(tmp_path, asr_backend="remote", asr_base_url=ASR_URL)
    else:
        settings = make_settings(tmp_path)
    assert "asr libraries" not in check(settings, probes=probes)


def test_doctor_whisper_local_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    WhisperStubs(monkeypatch)
    model = tmp_path / "model"
    model.mkdir()
    settings = whisper_settings(tmp_path, asr_model=str(model))
    assert status(settings, "asr model").status == "fail"
    (model / "model.bin").write_bytes(b"x")
    assert status(settings, "asr model") == CheckResult("asr model", "ok", "local directory")


def test_doctor_cuda_unavailable_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    WhisperStubs(monkeypatch, cuda=0)
    results = check(whisper_settings(tmp_path, asr_device="cuda"))
    assert results["asr"].status == "fail"
    assert "no CUDA device" in results["asr"].detail
    assert "asr model" not in results


def test_doctor_index_extra_missing_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_extra_missing(monkeypatch, "lancedb")
    results = check(make_settings(tmp_path, index_backend="lancedb"))
    assert results["index"].status == "fail"
    assert "uv sync --extra index" in results["index"].detail
    assert "embed model" not in results
    assert "search index" not in results


def test_doctor_unselected_backends_not_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    stub_extra_missing(monkeypatch, "lancedb", "fastembed", "faster_whisper")
    settings = make_settings(
        tmp_path, asr_backend="remote", asr_base_url=ASR_URL, index_backend="none"
    )
    results = run_doctor(settings)
    assert not [r for r in results if r.status == "fail" and r.name != "api token"]
    assert next(r for r in results if r.name == "index").detail.startswith("search is off")


class MemoryTable:
    def __init__(self, meta: Mapping[str, str], dim: int) -> None:
        self._meta, self._dim = dict(meta), dim

    def metadata(self) -> Mapping[str, str]:
        return self._meta

    def vector_dim(self) -> int | None:
        return self._dim


class MemoryDb:
    def __init__(self, table: MemoryTable | None) -> None:
        self.table = table

    def has_table(self) -> bool:
        return self.table is not None

    def open_table(self) -> MemoryTable | None:
        return self.table


def index_settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path, index_backend="lancedb")


def stub_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    table: MemoryTable | None,
    cached: object,
) -> None:
    info = ModelInfo(BGE, 384, "Qdrant/bge-small", "model.onnx")
    stub_fastembed_runtime(monkeypatch, [info])
    runtime = LanceRuntime(connect=lambda path: cast(Any, MemoryDb(table)))
    monkeypatch.setattr("earmark.index.lancedb_index.load_lance_runtime", lambda: runtime)
    hub = types.ModuleType("huggingface_hub")
    hub.try_to_load_from_cache = lambda *a, **k: cached  # type: ignore[attr-defined]  # stub
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    (tmp_path / "data" / "lancedb").mkdir(parents=True)


def test_doctor_index_rows_with_stub_runtimes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = {META_VERSION: "1", META_MODEL: BGE, META_DIM: "384"}
    stub_index(monkeypatch, tmp_path, table=MemoryTable(meta, 384), cached="/c/model.onnx")
    results = check(index_settings(tmp_path))
    assert results["index"].detail == f"lancedb, embedding model {BGE} (384 dimensions)"
    assert results["embed model"] == CheckResult("embed model", "ok", "downloaded")
    assert results["search index"].status == "ok"
    assert results["search index"].detail == f"built with {BGE}"


def test_doctor_index_empty_and_model_not_downloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_index(monkeypatch, tmp_path, table=None, cached=None)
    results = check(index_settings(tmp_path))
    assert results["embed model"].status == "warn"
    assert "GET /search answers 503" in results["embed model"].detail
    assert results["search index"].status == "ok"
    assert results["search index"].detail.startswith("empty")


def test_doctor_index_mismatch_fails_naming_reindex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = {META_VERSION: "1", META_MODEL: "other-model", META_DIM: "384"}
    stub_index(monkeypatch, tmp_path, table=MemoryTable(meta, 384), cached="/c")
    result = check(index_settings(tmp_path))["search index"]
    assert result.status == "fail"
    assert "earmark reindex" in result.detail


def test_doctor_embed_model_unknown_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_index(monkeypatch, tmp_path, table=None, cached=None)
    result = status(
        make_settings(tmp_path, index_backend="lancedb", embed_model="nope-model"), "index"
    )
    assert result.status == "fail"
    assert "EARMARK_EMBED_MODEL" in result.detail
    assert embed_module.load_fastembed_runtime is not None


# -- credentials and the other backends -----------------------------------------------------------


def test_doctor_anthropic_no_credential_fails(tmp_path: Path) -> None:
    from earmark.analyze.llm.anthropic import NO_CREDENTIALS

    result = status(make_settings(tmp_path, llm_provider="anthropic"), "llm")
    assert result == CheckResult("llm", "fail", NO_CREDENTIALS)


def test_doctor_anthropic_key_from_setting(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, llm_provider="anthropic", llm_api_key=SecretStr(KEY))
    result = status(settings, "llm")
    assert result.status == "ok"
    assert result.detail == (
        "anthropic, model claude-opus-5-5, effort medium, credential from EARMARK_LLM_API_KEY"
    )
    assert KEY not in result.detail


def test_doctor_anthropic_key_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-sentinel")
    result = status(make_settings(tmp_path, llm_provider="anthropic"), "llm")
    assert result.status == "ok"
    assert "credential from ANTHROPIC_API_KEY" in result.detail
    assert "sentinel" not in result.detail


def test_doctor_openai_compat_missing_base_url_fails(tmp_path: Path) -> None:
    result = status(make_settings(tmp_path, llm_provider="openai_compat"), "llm")
    assert result.status == "fail"
    assert "EARMARK_LLM_BASE_URL" in result.detail


def test_doctor_openai_compat_ok(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        llm_provider="openai_compat",
        llm_base_url=LLM_URL,
        llm_model="qwen",
        llm_context_tokens=32768,
    )
    result = status(settings, "llm")
    assert result == CheckResult(
        "llm", "ok", "openai_compat at llm.test:8000, model qwen, context 32768 tokens, no API key"
    )


def test_doctor_remote_asr_missing_base_url_fails(tmp_path: Path) -> None:
    result = status(make_settings(tmp_path, asr_backend="remote"), "asr")
    assert result.status == "fail"
    assert "EARMARK_ASR_BASE_URL" in result.detail


def test_doctor_remote_asr_ok_key_state(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path, asr_backend="remote", asr_base_url=ASR_URL, asr_api_key=SecretStr("k-asr-1")
    )
    result = status(settings, "asr")
    assert result.detail.endswith("remote server at asr.test:8000, model large-v3, API key set")
    assert "k-asr-1" not in result.detail


def test_doctor_ntfy_unset_warns(tmp_path: Path) -> None:
    result = status(make_settings(tmp_path, notify_backend="ntfy"), "notify")
    assert result.status == "warn"
    assert "EARMARK_NTFY_URL is not set" in result.detail
    assert "obsidian link" not in check(make_settings(tmp_path, notify_backend="ntfy"))


def test_doctor_ntfy_token_without_url_warns(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, notify_backend="ntfy", ntfy_token=SecretStr(TOKEN))
    result = status(settings, "notify")
    assert result.status == "warn"
    assert result.detail.endswith("EARMARK_NTFY_TOKEN is set but unused")
    assert TOKEN not in result.detail


def test_doctor_ntfy_ok_shows_host_only(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path, notify_backend="ntfy", ntfy_url=SecretStr(TOPIC_URL), ntfy_token=SecretStr(TOKEN)
    )
    result = status(settings, "notify")
    assert result == CheckResult("notify", "ok", "ntfy at ntfy.test, token set")
    assert TOPIC not in result.detail


def test_doctor_notify_none_and_fake(tmp_path: Path) -> None:
    assert status(make_settings(tmp_path), "notify") == CheckResult("notify", "ok", "off")
    assert status(make_settings(tmp_path, notify_backend="fake"), "notify").status == "warn"


def ntfy_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {"notify_backend": "ntfy", "ntfy_url": SecretStr(TOPIC_URL)}
    values.update(overrides)
    return make_settings(tmp_path, **values)


def test_doctor_obsidian_name_states(tmp_path: Path) -> None:
    unset = status(ntfy_settings(tmp_path), "obsidian link")
    assert unset.status == "warn"
    assert "EARMARK_OBSIDIAN_VAULT_NAME is not set" in unset.detail
    wrong = status(ntfy_settings(tmp_path, obsidian_vault_name="Other"), "obsidian link")
    assert wrong.status == "warn"
    assert "EARMARK_VAULT_DIR is 'vault'" in wrong.detail
    right = status(ntfy_settings(tmp_path, obsidian_vault_name="vault"), "obsidian link")
    assert right == CheckResult("obsidian link", "ok", "links open vault 'vault'")


def test_doctor_obsidian_name_mismatch_warns(tmp_path: Path) -> None:
    result = status(ntfy_settings(tmp_path, obsidian_vault_name="My Vault"), "obsidian link")
    assert result.status == "warn"
    assert "Obsidian names vaults after their folder" in result.detail


def test_doctor_fakes_and_http_fetch(tmp_path: Path) -> None:
    results = check(make_settings(tmp_path))
    for name in ("resolver", "fetch", "asr", "llm", "vault", "notify"):
        assert results[name].status in ("warn", "ok")
    assert results["resolver"].status == "warn"
    real = check(make_settings(tmp_path, resolver_backend="apple", fetch_backend="http"))
    assert real["resolver"].status == "ok"
    assert real["fetch"].status == "ok"
    private = check(make_settings(tmp_path, fetch_backend="http", allow_private_fetch=True))[
        "fetch"
    ]
    assert private.status == "warn"
    assert "private-address guard is off" in private.detail


def test_doctor_unexpected_error_becomes_fail_and_others_still_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: object) -> None:
        raise RuntimeError("kaboom\nline two")

    monkeypatch.setattr("earmark.doctor.checks.OfflineChecks.database", boom)
    results = check(make_settings(tmp_path))
    assert results["database"] == CheckResult(
        "database", "fail", "unexpected error: RuntimeError: kaboom line two"
    )
    assert results["notify"].status == "ok"


# -- offline makes no requests and creates nothing ------------------------------------------------


def test_offline_makes_no_requests(tmp_path: Path) -> None:
    hits: list[str] = []

    def refuse(request: httpx2.Request) -> httpx2.Response:
        hits.append(f"{request.method} {request.url}")
        raise AssertionError("an offline doctor must not send a request")

    probes = Probes(
        http_transport=httpx2.MockTransport(refuse),
        anthropic_http_client=httpx2.Client(transport=httpx2.MockTransport(refuse)),
        fetcher=GuardedFetcher(transport=httpx2.MockTransport(refuse)),
    )
    settings = make_settings(
        tmp_path,
        resolver_backend="apple",
        fetch_backend="http",
        asr_backend="remote",
        asr_base_url=ASR_URL,
        llm_provider="anthropic",
        llm_api_key=SecretStr(KEY),
        notify_backend="ntfy",
        ntfy_url=SecretStr(TOPIC_URL),
    )
    results = run_doctor(settings, online=False, probes=probes)
    assert hits == []
    assert {r.name for r in results} >= {"asr", "llm", "notify", "resolver"}
    assert not [r for r in results if r.name.endswith(("server", "api", "lookup"))]


def test_offline_creates_nothing(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "vault" / "Earmark").mkdir(parents=True)
    before = tree(tmp_path)
    settings = make_settings(tmp_path, store_backend="vault")
    run_doctor(settings)
    assert tree(tmp_path) == before


# -- online ------------------------------------------------------------------------------------

Handler = Callable[[httpx2.Request], httpx2.Response]


def routed(
    routes: Mapping[tuple[str, str], Callable[[httpx2.Request], httpx2.Response]],
) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        route = routes.get((request.url.host, request.url.path))
        if route is None:
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        return route(request)

    return handler


def probes_for(handler: Handler) -> Probes:
    return Probes(http_transport=httpx2.MockTransport(handler))


def test_online_ntfy_health_ok(tmp_path: Path) -> None:
    handler = routed({("ntfy.test", "/v1/health"): lambda r: json_reply(200, HEALTH_OK)})
    results = check(ntfy_settings(tmp_path), online=True, probes=probes_for(handler))
    assert results["ntfy server"] == CheckResult(
        "ntfy server", "ok", "ntfy at ntfy.test is reachable"
    )
    assert "ntfy token" not in results


def test_online_ntfy_unreachable_fails(tmp_path: Path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("boom")

    results = check(ntfy_settings(tmp_path), online=True, probes=probes_for(handler))
    assert results["ntfy server"].status == "fail"
    assert "could not reach ntfy at ntfy.test" in results["ntfy server"].detail


def test_online_ntfy_redirect_fails(tmp_path: Path) -> None:
    handler = routed(
        {("ntfy.test", "/v1/health"): lambda r: httpx2.Response(301, headers={"Location": "/x"})}
    )
    result = status(ntfy_settings(tmp_path), "ntfy server", online=True, probes=probes_for(handler))
    assert result.status == "fail"
    assert "redirect" in result.detail


@pytest.mark.parametrize(
    ("reply", "needle"),
    [
        pytest.param(lambda r: json_reply(404, b"{}"), "no /v1/health", id="404"),
        pytest.param(lambda r: json_reply(503, b"{}"), "unhealthy", id="503"),
        pytest.param(lambda r: json_reply(200, b'{"healthy":false}'), "unhealthy", id="false"),
        pytest.param(lambda r: json_reply(418, b"{}"), "HTTP 418", id="418"),
    ],
)
def test_online_ntfy_health_warnings(
    tmp_path: Path, reply: Callable[[httpx2.Request], httpx2.Response], needle: str
) -> None:
    handler = routed({("ntfy.test", "/v1/health"): reply})
    result = status(ntfy_settings(tmp_path), "ntfy server", online=True, probes=probes_for(handler))
    assert result.status == "warn"
    assert needle in result.detail


def ntfy_with_token(tmp_path: Path) -> Settings:
    return ntfy_settings(tmp_path, ntfy_token=SecretStr(TOKEN))


def test_online_ntfy_token_accepted(tmp_path: Path) -> None:
    seen: list[httpx2.Request] = []

    def account(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return json_reply(200, ACCOUNT_ANON)

    handler = routed(
        {
            ("ntfy.test", "/v1/health"): lambda r: json_reply(200, HEALTH_OK),
            ("ntfy.test", "/v1/account"): account,
        }
    )
    result = status(
        ntfy_with_token(tmp_path), "ntfy token", online=True, probes=probes_for(handler)
    )
    assert result == CheckResult("ntfy token", "ok", "token accepted")
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"


def test_online_ntfy_token_rejected_fails(tmp_path: Path) -> None:
    handler = routed(
        {
            ("ntfy.test", "/v1/health"): lambda r: json_reply(200, HEALTH_OK),
            ("ntfy.test", "/v1/account"): lambda r: json_reply(401, UNAUTHORIZED),
        }
    )
    result = status(
        ntfy_with_token(tmp_path), "ntfy token", online=True, probes=probes_for(handler)
    )
    assert result == CheckResult(
        "ntfy token", "fail", "ntfy at ntfy.test rejected EARMARK_NTFY_TOKEN"
    )


def test_online_ntfy_token_quota_warns(tmp_path: Path) -> None:
    handler = routed(
        {
            ("ntfy.test", "/v1/health"): lambda r: json_reply(200, HEALTH_OK),
            ("ntfy.test", "/v1/account"): lambda r: json_reply(200, ACCOUNT_EXHAUSTED),
        }
    )
    result = status(
        ntfy_with_token(tmp_path), "ntfy token", online=True, probes=probes_for(handler)
    )
    assert result.status == "warn"
    assert "quota is used up" in result.detail


def test_online_ntfy_token_not_checked_when_server_failed(tmp_path: Path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("boom")

    results = check(ntfy_with_token(tmp_path), online=True, probes=probes_for(handler))
    assert "ntfy token" not in results


def test_online_ntfy_never_publishes(tmp_path: Path) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.path == "/v1/account":
            return json_reply(200, ACCOUNT_ANON)
        return json_reply(200, HEALTH_OK)

    check(ntfy_with_token(tmp_path), online=True, probes=probes_for(handler))
    assert {r.method for r in seen} == {"GET"}
    assert "/" not in {r.url.path for r in seen}
    assert {r.url.path for r in seen} == {"/v1/health", "/v1/account"}


def models_body(*ids: str) -> bytes:
    return json.dumps({"data": [{"id": i, "object": "model"} for i in ids]}).encode()


def server_settings(tmp_path: Path, kind: str, **overrides: Any) -> tuple[Settings, str, str]:
    if kind == "asr":
        values: dict[str, Any] = {
            "asr_backend": "remote",
            "asr_base_url": ASR_URL,
            "asr_model": "whisper-1",
        }
        return make_settings(tmp_path, **{**values, **overrides}), "asr.test", "asr server"
    values = {
        "llm_provider": "openai_compat",
        "llm_base_url": LLM_URL,
        "llm_model": "qwen",
        "llm_context_tokens": 32768,
    }
    return make_settings(tmp_path, **{**values, **overrides}), "llm.test", "llm server"


@pytest.fixture(params=["asr", "llm"])
def kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def test_online_models_listed_ok(tmp_path: Path, kind: str) -> None:
    settings, host, name = server_settings(tmp_path, kind)
    model = "whisper-1" if kind == "asr" else "qwen"
    handler = routed({(host, "/v1/models"): lambda r: json_reply(200, models_body(model, "z"))})
    result = status(settings, name, online=True, probes=probes_for(handler))
    assert result.status == "ok"
    assert result.detail == f"{host}:8000 serves model {model}"


def test_online_models_missing_model_warns(tmp_path: Path, kind: str) -> None:
    settings, host, name = server_settings(tmp_path, kind)
    ids = [f"m{i}" for i in range(8)]
    handler = routed({(host, "/v1/models"): lambda r: json_reply(200, models_body(*ids))})
    result = status(settings, name, online=True, probes=probes_for(handler))
    assert result.status == "warn"
    assert "does not list model" in result.detail
    assert "m4" in result.detail
    assert "m5" not in result.detail  # at most five ids


def test_online_models_odd_body_warns(tmp_path: Path, kind: str) -> None:
    settings, host, name = server_settings(tmp_path, kind)
    handler = routed({(host, "/v1/models"): lambda r: json_reply(200, b'{"data": 3}')})
    result = status(settings, name, online=True, probes=probes_for(handler))
    assert (result.status, "not with a model list" in result.detail) == ("warn", True)


def test_online_models_404_warns(tmp_path: Path, kind: str) -> None:
    settings, host, name = server_settings(tmp_path, kind)
    handler = routed({(host, "/v1/models"): lambda r: httpx2.Response(404)})
    result = status(settings, name, online=True, probes=probes_for(handler))
    assert result.status == "warn"
    assert "no /models endpoint" in result.detail


def test_online_models_401_fails_and_key_is_sent_only_when_set(tmp_path: Path, kind: str) -> None:
    seen: list[httpx2.Request] = []

    def deny(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return json_reply(401, b'{"error": "bad key sentinel-model-key"}')

    key_name = "asr_api_key" if kind == "asr" else "llm_api_key"
    settings, host, name = server_settings(
        tmp_path, kind, **{key_name: SecretStr("sentinel-model-key")}
    )
    handler = routed({(host, "/v1/models"): deny})
    result = status(settings, name, online=True, probes=probes_for(handler))
    assert result.status == "fail"
    assert "HTTP 401" in result.detail
    assert "sentinel-model-key" not in result.detail
    assert seen[0].headers["Authorization"] == "Bearer sentinel-model-key"
    seen.clear()
    open_settings, _, _ = server_settings(tmp_path, kind)
    status(open_settings, name, online=True, probes=probes_for(handler))
    assert "authorization" not in {k.lower() for k in seen[0].headers}


def test_online_models_server_errors_and_redirects(tmp_path: Path, kind: str) -> None:
    settings, host, name = server_settings(tmp_path, kind)
    boom = routed({(host, "/v1/models"): lambda r: json_reply(500, b'{"error": "gpu on fire"}')})
    result = status(settings, name, online=True, probes=probes_for(boom))
    assert result.status == "fail"
    assert "HTTP 500: gpu on fire" in result.detail
    moved = routed(
        {(host, "/v1/models"): lambda r: httpx2.Response(302, headers={"Location": "/"})}
    )
    redirect = status(settings, name, online=True, probes=probes_for(moved))
    assert redirect.status == "fail"
    assert "redirect" in redirect.detail


def test_online_models_unreachable_and_timeout(tmp_path: Path, kind: str) -> None:
    settings, _, name = server_settings(tmp_path, kind)

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route")

    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow")

    down = status(settings, name, online=True, probes=probes_for(refuse))
    assert (down.status, "could not reach" in down.detail) == ("fail", True)
    late = status(settings, name, online=True, probes=probes_for(slow))
    assert (late.status, "did not answer within 10 s" in late.detail) == ("fail", True)


def anthropic_probes(stub: AnthropicStub) -> Probes:
    return Probes(anthropic_http_client=httpx2.Client(transport=httpx2.MockTransport(stub)))


def anthropic_settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path, llm_provider="anthropic", llm_api_key=SecretStr(KEY))


def test_online_anthropic_active_ok(tmp_path: Path) -> None:
    stub = AnthropicStub()
    result = status(
        anthropic_settings(tmp_path), "anthropic api", online=True, probes=anthropic_probes(stub)
    )
    assert result == CheckResult(
        "anthropic api", "ok", "claude-opus-5-5 (Claude Opus 5.5) is available"
    )
    [request] = stub.requests
    assert (request.method, request.url.path) == ("GET", "/v1/models/claude-opus-5-5")
    assert stub.message_requests() == []
    assert stub.count_requests() == []


def test_online_anthropic_deprecated_warns_and_retired_fails(tmp_path: Path) -> None:
    stub = AnthropicStub()
    stub.model_reply = httpx2.Response(
        200, json=model_info_body(lifecycle="deprecated", retires_at="2026-12-31T00:00:00Z")
    )
    warn = status(
        anthropic_settings(tmp_path), "anthropic api", online=True, probes=anthropic_probes(stub)
    )
    assert warn.status == "warn"
    assert warn.detail == "claude-opus-5-5 is deprecated; retires 2026-12-31"
    stub.model_reply = httpx2.Response(200, json=model_info_body(lifecycle="retired"))
    fail = status(
        anthropic_settings(tmp_path), "anthropic api", online=True, probes=anthropic_probes(stub)
    )
    assert fail.status == "fail"
    assert "is retired; set EARMARK_LLM_MODEL" in fail.detail


@pytest.mark.parametrize(
    ("code", "kind_", "needle"),
    [
        (404, "not_found_error", "EARMARK_LLM_MODEL"),
        (401, "authentication_error", "EARMARK_LLM_API_KEY"),
    ],
)
def test_online_anthropic_errors_fail(tmp_path: Path, code: int, kind_: str, needle: str) -> None:
    stub = AnthropicStub()
    stub.model_reply = error_reply(code, kind_, "nope")
    result = status(
        anthropic_settings(tmp_path), "anthropic api", online=True, probes=anthropic_probes(stub)
    )
    assert result.status == "fail"
    assert needle in result.detail


def test_online_anthropic_no_retry_on_500(tmp_path: Path) -> None:
    stub = AnthropicStub()
    stub.model_reply = error_reply(500, "api_error", "boom")
    result = status(
        anthropic_settings(tmp_path), "anthropic api", online=True, probes=anthropic_probes(stub)
    )
    assert result.status == "fail"
    assert len(stub.requests) == 1


def test_online_anthropic_skipped_without_credential(tmp_path: Path) -> None:
    stub = AnthropicStub()
    results = check(
        make_settings(tmp_path, llm_provider="anthropic"),
        online=True,
        probes=anthropic_probes(stub),
    )
    assert results["llm"].status == "fail"
    assert results["anthropic api"] == CheckResult("anthropic api", "skip", "offline check failed")
    assert stub.requests == []


def apple_settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path, resolver_backend="apple")


def test_online_apple_ok(tmp_path: Path) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            content=b'{"resultCount":0,"results":[]}',
            headers={"Content-Type": "text/javascript; charset=utf-8"},
        )

    probes = Probes(fetcher=GuardedFetcher(transport=httpx2.MockTransport(handler)))
    result = status(apple_settings(tmp_path), "apple lookup", online=True, probes=probes)
    assert result == CheckResult("apple lookup", "ok", "itunes.apple.com is reachable")
    assert seen[0].url.params["id"] == "1"
    assert seen[0].url.params["entity"] == "podcast"


def test_online_apple_unexpected_body_warns(tmp_path: Path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"<html>captive portal</html>")

    probes = Probes(fetcher=GuardedFetcher(transport=httpx2.MockTransport(handler)))
    assert (
        status(apple_settings(tmp_path), "apple lookup", online=True, probes=probes).status
        == "warn"
    )


def test_online_apple_blocked_fails(tmp_path: Path) -> None:
    fetcher = GuardedFetcher(
        resolve=StaticResolver({"itunes.apple.com": ["10.0.0.1"]}), network=ScriptedNetwork()
    )
    result = status(
        apple_settings(tmp_path), "apple lookup", online=True, probes=Probes(fetcher=fetcher)
    )
    assert result.status == "fail"
    assert "EARMARK_ALLOW_PRIVATE_FETCH" in result.detail


def test_online_timeouts_are_10s(tmp_path: Path) -> None:
    assert ONLINE_TIMEOUT_S == 10.0
    timeouts: dict[str, dict[str, float]] = {}

    def record(host: str) -> Callable[[httpx2.Request], httpx2.Response]:
        def reply(request: httpx2.Request) -> httpx2.Response:
            timeouts[request.url.path] = dict(request.extensions["timeout"])
            if request.url.path.endswith("health"):
                return json_reply(200, HEALTH_OK)
            return json_reply(200, models_body("whisper-1", "qwen"))

        return reply

    handler = routed(
        {
            ("ntfy.test", "/v1/health"): record("ntfy.test"),
            ("asr.test", "/v1/models"): record("asr.test"),
            ("llm.test", "/v1/models"): record("llm.test"),
        }
    )
    settings = make_settings(
        tmp_path,
        asr_backend="remote",
        asr_base_url=ASR_URL,
        asr_model="whisper-1",
        llm_provider="openai_compat",
        llm_base_url=LLM_URL,
        llm_model="qwen",
        llm_context_tokens=32768,
        notify_backend="ntfy",
        ntfy_url=SecretStr(TOPIC_URL),
    )
    results = check(settings, online=True, probes=probes_for(handler))
    assert {results[n].status for n in ("asr server", "llm server", "ntfy server")} == {"ok"}
    for path in ("/v1/health", "/v1/models"):
        assert timeouts[path]["connect"] == 5.0
        assert timeouts[path]["read"] == 10.0

    stub = AnthropicStub()
    status(
        anthropic_settings(tmp_path), "anthropic api", online=True, probes=anthropic_probes(stub)
    )
    assert stub.requests[0].extensions["timeout"]["read"] == 10.0


def test_online_skipped_after_offline_failure(tmp_path: Path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("a skipped check must not send a request")

    settings = make_settings(
        tmp_path, llm_provider="openai_compat", asr_backend="remote", resolver_backend="apple"
    )
    results = check(settings, online=True, probes=probes_for(handler))
    assert results["llm server"] == CheckResult("llm server", "skip", "offline check failed")
    assert results["asr server"].status == "skip"
    assert results["apple lookup"].status in ("skip", "fail", "ok", "warn")


def test_online_checks_only_for_selected_backends(tmp_path: Path) -> None:
    results = check(make_settings(tmp_path), online=True, probes=Probes())
    online_names = {"apple lookup", "asr server", "llm server", "anthropic api", "ntfy server"}
    assert not online_names & set(results)


# -- reports ---------------------------------------------------------------------------------------


def test_render_summary_and_exit_code() -> None:
    results = [
        CheckResult("ok one", "ok", "fine"),
        CheckResult("warned", "warn", "hmm"),
        CheckResult("warned two", "warn", "hmm"),
        CheckResult("skipped", "skip", "offline check failed"),
    ]
    assert summary([results[0]]) == "doctor: all checks passed"
    assert summary(results) == "doctor: all checks passed, 2 warnings"
    assert summary(results[:2]) == "doctor: all checks passed, 1 warning"
    failed = [*results, CheckResult("bad", "fail", "no")]
    assert summary(failed) == "doctor: 1 check failed, 2 warnings"
    assert summary([CheckResult("a", "fail", "x"), CheckResult("b", "fail", "x")]) == (
        "doctor: 2 checks failed"
    )
    assert (exit_code(results), exit_code(failed)) == (0, 1)
    lines = render(failed, [])
    assert lines[0] == "ok    ok one         fine"
    assert lines[-1].startswith("FAIL  bad ")


def test_render_redacts_cleans_and_caps() -> None:
    result = CheckResult("x", "warn", "key sentinel-r1 \x1b[31m " + "y" * 400)
    [line] = render([result], ["sentinel-r1"])
    assert "sentinel-r1" not in line
    assert "**********" in line
    assert "\x1b" not in line
    assert len(line.split(" ", 2)[2]) <= 300 + 20
    assert line.endswith("…")
