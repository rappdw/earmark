"""``run_doctor`` offline and online, through ``Probes`` (no network, sockets blocked)."""

import json
import os
import sqlite3
import sys
import types
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from earmark.config import Settings
from earmark.db import APPLICATION_ID, MIGRATIONS, SCHEMA_VERSION, open_database
from earmark.doctor import ONLINE_TIMEOUT_S, Probes, run_doctor
from earmark.doctor.report import CheckResult, Status, exit_code, render, summary
from earmark.fetch import GuardedFetcher
from earmark.jobs import open_job_store
from earmark.transcribe.compat import Installed
from tests.factories import new_podcast_job
from tests.http_helpers import ScriptedNetwork, StaticResolver
from tests.legacy_db import V010_SCHEMA_VERSION, make_v010_database
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
from tests.smtp_stub import REPLY_SENTINEL, SmtpStub, stub_factory, trusted_test_context

make_stub = pytest.fixture(stub_factory)
TOKEN_OK = "t" * 40
ASR_URL = "http://asr.test:8000/v1"
NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")

FAKES: dict[str, Any] = {
    "resolver_backend": "fake",
    "fetch_backend": "fake",
    "youtube_backend": "fake",
    "asr_backend": "fake",
    "notify_backend": "none",
    "mail_backend": "fake",
}


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        **FAKES,
        "api_token": SecretStr(TOKEN_OK),
        "data_dir": tmp_path / "data",
        "allowed_recipients": ("you@example.org",),
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
    store.enqueue(
        new_podcast_job("https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000802907981")
    )
    store.enqueue(
        new_podcast_job("https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000802907982")
    )
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
    open_database(db_path(tmp_path), migrations=())
    result = status(make_settings(tmp_path), "database")
    assert result.status == "warn"
    assert "schema version 0" in result.detail
    with closing(sqlite3.connect(db_path(tmp_path))) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0


def test_doctor_database_not_wal_warns(tmp_path: Path) -> None:
    db_path(tmp_path).parent.mkdir()
    with closing(sqlite3.connect(db_path(tmp_path), autocommit=True)) as conn:
        for migration in MIGRATIONS:
            for statement in migration.statements:
                conn.execute(statement)
        conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
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

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        cuda: int = 0,
        cuda_types: tuple[str, ...] = (),
        types_error: Exception | None = None,
    ) -> None:
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

        def supported_compute_types(device: str) -> list[str]:
            if types_error is not None:
                raise types_error
            return list(cuda_types)

        ct2.get_supported_compute_types = supported_compute_types  # type: ignore[attr-defined]
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


CUDA_CASES = [
    # (device, compute, devices, types, error, status, detail)
    ("cpu", "auto", 0, (), None, "ok", "no CUDA device (EARMARK_ASR_DEVICE=cpu)"),
    ("auto", "auto", 0, (), None, "ok", "no CUDA device; transcribing on CPU"),
    (
        "cpu",
        "auto",
        1,
        ("float16",),
        None,
        "ok",
        "1 CUDA device, float16 supported; EARMARK_ASR_DEVICE=cpu leaves them unused",
    ),
    ("auto", "auto", 2, ("float16", "int8"), None, "ok", "2 CUDA devices, float16 supported"),
    ("cuda", "float16", 1, ("float16",), None, "ok", "1 CUDA device, float16 supported"),
    (
        "cuda",
        "auto",
        1,
        (),
        ValueError("x"),
        "fail",
        "1 CUDA device; CTranslate2 could not list the CUDA compute types (ValueError)",
    ),
    (
        "cpu",
        "auto",
        1,
        (),
        ValueError("x"),
        "warn",
        "1 CUDA device; CTranslate2 could not list the CUDA compute types (ValueError)",
    ),
    (
        "auto",
        "auto",
        1,
        ("int8", "float32"),
        None,
        "warn",
        "1 CUDA device, float16 not supported (CUDA compute types: float32, int8)",
    ),
    (
        "cuda",
        "float16",
        1,
        ("int8", "float32"),
        None,
        "fail",
        "1 CUDA device, float16 not supported (CUDA compute types: float32, int8); "
        "set EARMARK_ASR_COMPUTE_TYPE to one of them",
    ),
    (
        "cpu",
        "float16",
        1,
        ("int8",),
        None,
        "warn",
        "1 CUDA device, float16 not supported (CUDA compute types: int8)",
    ),
]


@pytest.mark.parametrize(
    ("device", "compute", "devices", "types", "error", "status", "detail"), CUDA_CASES
)
def test_doctor_asr_cuda(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    device: str,
    compute: str,
    devices: int,
    types: tuple[str, ...],
    error: Exception | None,
    status: Status,
    detail: str,
) -> None:
    WhisperStubs(monkeypatch, cuda=devices, cuda_types=types, types_error=error)
    settings = whisper_settings(tmp_path, asr_device=device, asr_compute_type=compute)
    results = check(settings)
    if device == "cuda" and devices == 0:
        pytest.skip("asr fails first")
    assert results["asr cuda"] == CheckResult("asr cuda", status, detail)


def test_doctor_asr_cuda_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    WhisperStubs(monkeypatch)
    names = [r.name for r in run_doctor(whisper_settings(tmp_path))]
    start = names.index("asr")
    assert names[start : start + 4] == ["asr", "asr libraries", "asr model", "asr cuda"]


def test_doctor_asr_cuda_absent_when_asr_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    WhisperStubs(monkeypatch, cuda=0)
    results = check(whisper_settings(tmp_path, asr_device="cuda"))
    assert results["asr"].status == "fail"
    assert "asr cuda" not in results


def test_doctor_asr_detail_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    WhisperStubs(monkeypatch)
    results = check(whisper_settings(tmp_path, asr_batch_size=8))
    assert results["asr"].detail == (
        "faster_whisper, model small, device cpu, compute auto, batch size 8"
    )


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


def test_doctor_unselected_backends_not_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    settings = make_settings(tmp_path, asr_backend="remote", asr_base_url=ASR_URL)
    results = run_doctor(settings)
    assert not [r for r in results if r.status == "fail" and r.name != "api token"]


# -- credentials and the other backends -----------------------------------------------------------


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


def test_llm_off(tmp_path: Path) -> None:
    result = status(make_settings(tmp_path), "llm")
    assert result.status == "ok"
    assert result.detail.startswith("off")
    assert "503" in result.detail


def test_llm_fake_warns(tmp_path: Path) -> None:
    result = status(make_settings(tmp_path, llm_backend="fake"), "llm")
    assert result.status == "warn"
    assert "fake summarizer" in result.detail


@pytest.mark.parametrize("backend", ["ollama", "llama_cpp", "vllm"])
def test_llm_real_backend_fails_until_step_37b(tmp_path: Path, backend: str) -> None:
    result = status(make_settings(tmp_path, llm_backend=backend), "llm")
    assert result.status == "fail"
    assert "EARMARK_LLM_BACKEND" in result.detail
    assert "step 37b" in result.detail


def test_llm_check_makes_no_request_and_names_no_secret(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        llm_backend="ollama",
        llm_base_url="http://ollama:11434",
        llm_api_key=SecretStr("sentinel-llm-key-77"),
    )
    result = status(settings, "llm")
    assert "sentinel-llm-key-77" not in result.detail
    assert "ollama:11434" not in result.detail


def ntfy_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {"notify_backend": "ntfy", "ntfy_url": SecretStr(TOPIC_URL)}
    values.update(overrides)
    return make_settings(tmp_path, **values)


def test_doctor_fakes_and_http_fetch(tmp_path: Path) -> None:
    results = check(make_settings(tmp_path))
    for name in ("resolver", "fetch", "asr", "notify"):
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
        fetcher=GuardedFetcher(transport=httpx2.MockTransport(refuse)),
    )
    settings = make_settings(
        tmp_path,
        resolver_backend="apple",
        fetch_backend="http",
        asr_backend="remote",
        asr_base_url=ASR_URL,
        notify_backend="ntfy",
        ntfy_url=SecretStr(TOPIC_URL),
    )
    results = run_doctor(settings, online=False, probes=probes)
    assert hits == []
    assert {r.name for r in results} >= {"asr", "notify", "resolver"}
    assert not [r for r in results if r.name.endswith(("server", "api", "lookup"))]


def test_offline_creates_nothing(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    before = tree(tmp_path)
    run_doctor(make_settings(tmp_path))
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
    assert kind == "asr"
    values: dict[str, Any] = {
        "asr_backend": "remote",
        "asr_base_url": ASR_URL,
        "asr_model": "whisper-1",
    }
    return make_settings(tmp_path, **{**values, **overrides}), "asr.test", "asr server"


@pytest.fixture(params=["asr"])
def kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def test_online_models_listed_ok(tmp_path: Path, kind: str) -> None:
    settings, host, name = server_settings(tmp_path, kind)
    model = "whisper-1"
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

    key_name = "asr_api_key"
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
            return json_reply(200, models_body("whisper-1"))

        return reply

    handler = routed(
        {
            ("ntfy.test", "/v1/health"): record("ntfy.test"),
            ("asr.test", "/v1/models"): record("asr.test"),
        }
    )
    settings = make_settings(
        tmp_path,
        asr_backend="remote",
        asr_base_url=ASR_URL,
        asr_model="whisper-1",
        notify_backend="ntfy",
        ntfy_url=SecretStr(TOPIC_URL),
    )
    results = check(settings, online=True, probes=probes_for(handler))
    assert {results[n].status for n in ("asr server", "ntfy server")} == {"ok"}
    for path in ("/v1/health", "/v1/models"):
        assert timeouts[path]["connect"] == 5.0
        assert timeouts[path]["read"] == 10.0


def test_online_skipped_after_offline_failure(tmp_path: Path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("a skipped check must not send a request")

    settings = make_settings(tmp_path, asr_backend="remote", resolver_backend="apple")
    results = check(settings, online=True, probes=probes_for(handler))
    assert results["asr server"] == CheckResult("asr server", "skip", "offline check failed")
    assert results["apple lookup"].status in ("skip", "fail", "ok", "warn")


def test_online_checks_only_for_selected_backends(tmp_path: Path) -> None:
    results = check(make_settings(tmp_path), online=True, probes=Probes())
    online_names = {"apple lookup", "asr server", "ntfy server"}
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


def test_doctor_check_names_in_order(tmp_path: Path) -> None:
    results = run_doctor(make_settings(tmp_path))
    assert [r.name for r in results] == [
        "api token",
        "recipients",
        "data dir",
        "database",
        "resolver",
        "fetch",
        "youtube",
        "asr",
        "mail",
        "notify",
        "llm",
        "client mode",
    ]


def test_doctor_database_v010_fails(tmp_path: Path) -> None:
    db_path(tmp_path).parent.mkdir()
    path = make_v010_database(db_path(tmp_path))
    result = status(make_settings(tmp_path), "database")
    assert result.status == "fail"
    assert "Move it aside" in result.detail
    assert "0.1.0" in result.detail
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == V010_SCHEMA_VERSION
        assert conn.execute("PRAGMA application_id").fetchone()[0] == 0


# -- recipients ---------------------------------------------------------------------------------


def test_doctor_recipients_unset_fails(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, allowed_recipients=())
    result = status(settings, "recipients")
    assert result.status == "fail"
    assert result.detail == (
        "EARMARK_ALLOWED_RECIPIENTS is not set: every request is refused. Set it to the "
        "destination addresses, comma-separated, or to all."
    )
    assert exit_code(run_doctor(settings)) == 1


def test_doctor_recipients_list_ok_masked(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path, allowed_recipients=("zelda.secret@example.org", "bob@example.org")
    )
    result = status(settings, "recipients")
    assert result.status == "ok"
    assert result.detail == "2 allowed: b…@example.org, z…@example.org"
    assert "zelda" not in result.detail


def test_doctor_recipients_all_warns(tmp_path: Path) -> None:
    result = status(make_settings(tmp_path, allowed_recipients=("all",)), "recipients")
    assert result.status == "warn"
    assert result.detail.startswith("all: any address is accepted.")
    assert "Anyone with the API token" in result.detail


def test_doctor_recipients_listed_right_after_api_token(tmp_path: Path) -> None:
    names = [r.name for r in run_doctor(make_settings(tmp_path))]
    assert names[:2] == ["api token", "recipients"]


# -- the mail checks (step 25) -----------------------------------------------------------

SMTP_PASSWORD = "abcd efgh ijkl mnop"


def smtp_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "mail_backend": "smtp",
        "smtp_host": "smtp.test",
        "smtp_username": "mailer-user",
        "smtp_password": SecretStr(SMTP_PASSWORD),
        "smtp_from": "earmark@example.org",
    }
    values.update(overrides)
    return make_settings(tmp_path, **values)


def smtp_probes(stub: SmtpStub) -> Probes:
    return Probes(
        smtp_socket_factory=stub.socket_factory,
        smtp_context_factory=trusted_test_context,
        timeout_s=5.0,
    )


def test_mail_check_fake_warns(tmp_path: Path) -> None:
    result = status(make_settings(tmp_path), "mail")
    assert result == CheckResult("mail", "warn", "fake mailer (for tests): nothing is sent")


def test_mail_check_smtp_missing_fails(tmp_path: Path) -> None:
    result = status(make_settings(tmp_path, mail_backend="smtp"), "mail")
    assert result.status == "fail"
    for name in ("HOST", "FROM", "USERNAME", "PASSWORD"):
        assert f"EARMARK_SMTP_{name}" in result.detail


def test_mail_check_smtp_ok_names_host_not_secrets(tmp_path: Path) -> None:
    result = status(smtp_settings(tmp_path), "mail")
    assert result == CheckResult(
        "mail", "ok", "smtp to smtp.test:587 (starttls), from @example.org, timeout 60 s"
    )
    tls = status(smtp_settings(tmp_path, smtp_security="tls", smtp_timeout_s=12.5), "mail")
    assert tls.detail == "smtp to smtp.test:465 (tls), from @example.org, timeout 12.5 s"
    for text in (result.detail, tls.detail):
        assert SMTP_PASSWORD not in text
        assert "mailer-user" not in text


def test_mail_check_makes_no_connection(tmp_path: Path) -> None:
    check(smtp_settings(tmp_path))  # sockets are blocked: a connect would be a failure


def test_online_smtp_login_sends_nothing(
    tmp_path: Path, make_stub: Callable[..., SmtpStub]
) -> None:
    stub = make_stub()
    results = check(smtp_settings(tmp_path), online=True, probes=smtp_probes(stub))
    result = results["smtp"]
    assert result.status == "ok"
    assert result.detail.startswith("smtp.test:587: TLSv1.")
    assert result.detail.endswith(", login accepted, nothing sent")
    stub.join()
    verbs = {verb.upper() for verb in stub.verbs}
    assert verbs == {"EHLO", "STARTTLS", "AUTH", "QUIT"}
    assert stub.messages == []
    assert stub.mail_from == [] == stub.rcpt_to
    for text in (result.detail, *(r.detail for r in results.values())):
        assert SMTP_PASSWORD not in text
        assert "mailer-user" not in text
        assert REPLY_SENTINEL not in text


def test_online_smtp_auth_failure_fails_with_settings(
    tmp_path: Path, make_stub: Callable[..., SmtpStub]
) -> None:
    stub = make_stub(auth_reply=f"535 5.7.8 bad {{user}} {{password}} {REPLY_SENTINEL}")
    result = status(smtp_settings(tmp_path), "smtp", online=True, probes=smtp_probes(stub))
    assert result.status == "fail"
    assert "EARMARK_SMTP_USERNAME" in result.detail
    assert "EARMARK_SMTP_PASSWORD" in result.detail
    assert "reply 535, 5.7.8" in result.detail
    for text in (SMTP_PASSWORD, "mailer-user", REPLY_SENTINEL):
        assert text not in result.detail


def test_online_smtp_unverified_certificate_fails(
    tmp_path: Path, make_stub: Callable[..., SmtpStub]
) -> None:
    stub = make_stub()
    probes = Probes(smtp_socket_factory=stub.socket_factory, timeout_s=5.0)  # system trust store
    result = status(smtp_settings(tmp_path), "smtp", online=True, probes=probes)
    assert result.status == "fail"
    assert "did not verify" in result.detail
    assert "EARMARK_SMTP_HOST" in result.detail


def test_online_smtp_skipped_when_offline_mail_failed(tmp_path: Path) -> None:
    results = check(make_settings(tmp_path, mail_backend="smtp"), online=True)
    assert results["mail"].status == "fail"
    assert results["smtp"] == CheckResult("smtp", "skip", "offline check failed")


def test_online_smtp_not_listed_for_the_fake_backend(tmp_path: Path) -> None:
    assert "smtp" not in check(make_settings(tmp_path), online=True)


# -- YouTube ----------------------------------------------------------------------------------


def test_youtube_fake_warns(tmp_path: Path) -> None:
    result = status(make_settings(tmp_path), "youtube")
    assert result.status == "warn"
    assert "fake YouTube source" in result.detail


def test_youtube_ok_versions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from earmark.youtube import ytdlp
    from tests.ytdlp_fakes import fake_runtime

    fake = fake_runtime(tmp_path)
    monkeypatch.setattr(ytdlp, "load_runtime", lambda: fake.runtime)
    result = status(make_settings(tmp_path, youtube_backend="ytdlp"), "youtube")
    assert result.status == "ok"
    assert result.detail == "yt-dlp 2026.11.23, yt-dlp-ejs 0.8.0, deno 2.9.8"
    assert not fake.sessions  # no request, no process


def test_youtube_unknown_versions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    from earmark.youtube import ytdlp
    from tests.ytdlp_fakes import fake_runtime

    runtime = replace(fake_runtime(tmp_path).runtime, ejs_version=None, deno_version=None)
    monkeypatch.setattr(ytdlp, "load_runtime", lambda: runtime)
    result = status(make_settings(tmp_path, youtube_backend="ytdlp"), "youtube")
    assert result.detail == "yt-dlp 2026.11.23, yt-dlp-ejs unknown, deno unknown"


def test_youtube_missing_extra_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "yt_dlp", None)
    result = status(make_settings(tmp_path, youtube_backend="ytdlp"), "youtube")
    assert result.status == "fail"
    assert "uv sync --extra youtube" in result.detail


# -- client mode and the api server (step 34) ----------------------------------------------------

API = "https://earmark.example.com"


def test_doctor_client_mode_off(tmp_path: Path) -> None:
    result = status(make_settings(tmp_path), "client mode")
    assert result.status == "ok"
    assert result.detail == "off: jobs, submit and transcript read EARMARK_DATA_DIR"


def test_doctor_client_mode_on(tmp_path: Path) -> None:
    result = status(make_settings(tmp_path, api_url=API + ":8443"), "client mode")
    assert result.status == "ok"
    assert result.detail == (
        "on: jobs, submit and transcript call the API at earmark.example.com:8443"
    )
    assert "https" not in result.detail


def test_doctor_client_mode_without_token_fails(tmp_path: Path) -> None:
    result = status(make_settings(tmp_path, api_url=API, api_token=None), "client mode")
    assert result.status == "fail"
    assert "EARMARK_API_TOKEN is not set" in result.detail


def test_doctor_client_mode_bad_token_fails(tmp_path: Path) -> None:
    from earmark.api_client import BAD_TOKEN

    settings = make_settings(tmp_path, api_url=API, api_token=SecretStr("tök" * 20))
    result = status(settings, "client mode")
    assert (result.status, result.detail) == ("fail", BAD_TOKEN)


def _health_probes(reply: Callable[[httpx2.Request], httpx2.Response]) -> Probes:
    return Probes(http_transport=httpx2.MockTransport(reply))


def test_doctor_online_api_server_ok(tmp_path: Path) -> None:
    seen: list[httpx2.Request] = []

    def reply(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"status": "ok"})

    result = status(
        make_settings(tmp_path, api_url=API),
        "api server",
        online=True,
        probes=_health_probes(reply),
    )
    assert (result.status, result.detail) == ("ok", "earmark.example.com answers /healthz")
    [request] = seen
    assert request.url.path == "/healthz"
    assert "authorization" not in request.headers


def _unreachable(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("boom", request=request)


def _bad_certificate(request: httpx2.Request) -> httpx2.Response:
    import ssl

    raise httpx2.ConnectError("boom", request=request) from ssl.SSLCertVerificationError(
        1, "certificate verify failed"
    )


@pytest.mark.parametrize(
    ("reply", "level", "needle"),
    [
        (
            lambda r: httpx2.Response(302, headers={"Location": "http://x.example.net/"}),
            "fail",
            "redirect",
        ),
        (lambda r: httpx2.Response(503), "fail", "answered HTTP 503 on /healthz"),
        (_unreachable, "fail", "could not reach the API at earmark.example.com (ConnectError)"),
        (_bad_certificate, "fail", "certificate of earmark.example.com did not verify"),
        (lambda r: httpx2.Response(200, text="<html>"), "warn", "but not as earmark does"),
    ],
    ids=["redirect", "503", "unreachable", "certificate", "not earmark"],
)
def test_doctor_online_api_server_failures(
    tmp_path: Path, reply: Callable[[httpx2.Request], httpx2.Response], level: str, needle: str
) -> None:
    result = status(
        make_settings(tmp_path, api_url=API),
        "api server",
        online=True,
        probes=_health_probes(reply),
    )
    assert result.status == level
    assert needle in result.detail


def test_doctor_online_api_server_skipped_without_token(tmp_path: Path) -> None:
    def reply(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("no request expected")

    settings = make_settings(tmp_path, api_url=API, api_token=None)
    result = status(settings, "api server", online=True, probes=_health_probes(reply))
    assert result.status == "skip"


def test_doctor_online_no_api_server_line_when_off(tmp_path: Path) -> None:
    results = check(make_settings(tmp_path), online=True, probes=_health_probes(_unreachable))
    assert "api server" not in results
