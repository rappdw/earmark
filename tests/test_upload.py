import hashlib
import sqlite3
import tempfile
import tracemalloc
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from earmark.api.app import create_app
from earmark.api.upload import (
    MAX_FIELD_BYTES,
    MAX_PARTS,
    MULTIPART_OVERHEAD_BYTES,
)
from earmark.config import Settings
from earmark.db import open_database
from earmark.jobs import JobStore
from tests.asgi_helpers import AsgiResult, call_asgi
from tests.factories import TO, MutableClock
from tests.multipart_helpers import BOUNDARY, Part, audio, content_type, multipart_body, split

TOKEN = "upload-token-3c9d"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CAP = 1000
FILE = Part("file", audio(100), "clip.mp3", "audio/mpeg")
TO_PART = Part("to", TO)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        api_token=SecretStr(TOKEN),
        data_dir=tmp_path / "data",
        allowed_recipients=(TO,),
    )


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(open_database(tmp_path / "earmark.db"), clock=MutableClock())


@pytest.fixture
def uploads(settings: Settings) -> Path:
    return settings.data_dir / "uploads"


@pytest.fixture
def client(settings: Settings, store: JobStore) -> Iterator[TestClient]:
    app = create_app(settings, store=store, max_upload_bytes=CAP)
    with TestClient(app, headers=AUTH) as c:
        yield c


def left(uploads: Path) -> list[str]:
    return sorted(p.name for p in uploads.iterdir()) if uploads.exists() else []


def post(client: TestClient, *parts: Part, close: bool = True) -> Any:
    return client.post(
        "/transcriptions",
        content=multipart_body(parts, close=close),
        headers={"Content-Type": content_type()},
    )


def asgi(
    settings: Settings,
    store: JobStore,
    chunks: list[bytes],
    *,
    cap: int = CAP,
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> AsgiResult:
    app = create_app(settings, store=store, max_upload_bytes=cap)
    base = {"authorization": f"Bearer {TOKEN}", "content-type": content_type()}
    return call_asgi(app, headers={**base, **(headers or {})}, chunks=chunks, **kwargs)


# -- success -------------------------------------------------------------------------------


def test_upload_creates_job(client: TestClient, store: JobStore, uploads: Path) -> None:
    r = post(client, TO_PART, Part("instructions", "Why?\nHow?"), Part("title", "Memo"), FILE)
    assert r.status_code == 202
    body = r.json()
    assert r.headers["Location"] == f"/jobs/{body['job_id']}"
    assert (body["status"], body["created"]) == ("queued", True)
    job = store.get(body["job_id"])
    assert job is not None
    assert (job.source, job.url, job.to) == ("upload", None, TO)
    assert (job.instructions, job.title, job.upload_name) == ("Why?\nHow?", "Memo", "clip.mp3")
    assert left(uploads) == [job.upload_file]
    assert (uploads / str(job.upload_file)).read_bytes() == FILE.data
    assert (uploads / str(job.upload_file)).stat().st_mode & 0o777 == 0o600
    assert uploads.stat().st_mode & 0o777 == 0o700


def test_upload_job_hash_matches_file(client: TestClient, store: JobStore, uploads: Path) -> None:
    data = audio(700)
    r = post(client, TO_PART, Part("file", data, "a.m4a", "application/octet-stream"))
    job = store.get(r.json()["job_id"])
    assert job is not None
    assert job.content_sha256 == hashlib.sha256(data).hexdigest()
    assert job.upload_file is not None
    assert job.upload_file.endswith(".m4a")
    assert (
        hashlib.sha256((uploads / job.upload_file).read_bytes()).hexdigest() == job.content_sha256
    )


def test_fields_after_file_accepted(client: TestClient, store: JobStore) -> None:
    assert post(client, FILE, TO_PART, Part("instructions", "after")).status_code == 202
    [job] = store.list_recent()
    assert job.instructions == "after"


def test_fields_before_file_accepted(client: TestClient) -> None:
    assert post(client, TO_PART, Part("title", "before"), FILE).status_code == 202


def test_extension_from_content_type_and_filename(client: TestClient, store: JobStore) -> None:
    r = post(client, TO_PART, Part("file", audio(), None, "audio/mpeg"))
    job = store.get(r.json()["job_id"])
    assert job is not None
    assert (job.upload_file or "").endswith(".mp3")
    assert job.upload_name is None
    r = post(client, TO_PART, Part("file", audio(20), "../../etc/PWN\x07.OGG", "text/plain"))
    other = store.get(r.json()["job_id"])
    assert other is not None
    assert (other.upload_file or "").endswith(".ogg")
    assert other.upload_name == "PWN.OGG"


def test_utf8_filename_kept(client: TestClient, store: JobStore) -> None:
    r = post(client, TO_PART, Part("file", audio(), "réunion été.m4a", "audio/mp4"))
    job = store.get(r.json()["job_id"])
    assert job is not None
    assert job.upload_name == "réunion été.m4a"


# -- A2/A5: refused uploads leave nothing --------------------------------------------------


def test_upload_to_not_allowlisted_422_and_staging_empty(
    client: TestClient, store: JobStore, uploads: Path
) -> None:
    r = post(client, Part("to", "stranger@example.com"), FILE)
    assert r.status_code == 422
    assert r.json()["detail"] == "to is not an allowed recipient (EARMARK_ALLOWED_RECIPIENTS)"
    assert left(uploads) == []
    assert store.list_recent() == []


def test_to_missing_422(client: TestClient, uploads: Path) -> None:
    r = post(client, FILE)
    assert r.status_code == 422
    assert r.json()["detail"] == "to is required"
    assert left(uploads) == []


def test_validation_failure_after_file_deletes_staged(client: TestClient, uploads: Path) -> None:
    r = post(client, FILE, Part("to", "not an address"), Part("title", "x" * 201))
    assert r.status_code == 422
    assert r.json()["detail"] == (
        "to must be one email address; title is too long (at most 200 characters)"
    )
    assert "not an address" not in r.text
    assert left(uploads) == []


def test_not_audio_415_leaves_nothing(client: TestClient, uploads: Path) -> None:
    r = post(client, TO_PART, Part("file", b"hello", "notes.txt", "text/plain"))
    assert r.status_code == 415
    assert "does not look like audio" in r.json()["detail"]
    assert left(uploads) == []
    assert post(client, TO_PART, Part("file", b"x", "blob", None)).status_code == 415
    assert left(uploads) == []


def test_store_error_503_deletes_staged(settings: Settings, tmp_path: Path, uploads: Path) -> None:
    class LockedStore(JobStore):
        def enqueue(self, *args: Any, **kwargs: Any) -> Any:
            raise sqlite3.OperationalError("database is locked")

    locked = LockedStore(open_database(tmp_path / "locked.db"))
    app = create_app(settings, store=locked, max_upload_bytes=CAP)
    with TestClient(app, headers=AUTH) as c:
        r = post(c, TO_PART, FILE)
    assert r.status_code == 503
    assert r.json() == {"detail": "the job store is unavailable; try again shortly"}
    assert left(uploads) == []


def test_dedupe_hit_deletes_second_copy(client: TestClient, store: JobStore, uploads: Path) -> None:
    first = post(client, TO_PART, FILE)
    second = post(client, TO_PART, Part("file", FILE.data, "other-name.mp3", "audio/mpeg"))
    assert first.status_code == second.status_code == 202
    assert second.json() == {**first.json(), "created": False}
    [job] = store.list_recent()
    assert left(uploads) == [job.upload_file]
    third = post(client, TO_PART, Part("instructions", "different"), FILE)
    assert third.json()["created"] is True
    assert len(left(uploads)) == 2


def test_client_disconnect_mid_file_leaves_nothing(
    settings: Settings, store: JobStore, uploads: Path
) -> None:
    # Chunks larger than the writer's buffer, so the partial file really grows on disk.
    body = multipart_body([TO_PART, Part("file", audio(100_000), "a.mp3", "audio/mpeg")])
    chunks = split(body, 20_000)
    sizes: list[int] = []

    def watch(n: int) -> None:
        sizes.append(sum(p.stat().st_size for p in uploads.iterdir()) if uploads.exists() else 0)

    result = asgi(settings, store, chunks, cap=1024 * 1024, disconnect_after=4, on_receive=watch)
    assert result.status == 400
    assert max(sizes) > 0  # the file really was being written when the client went away
    assert left(uploads) == []
    assert store.list_recent() == []


def test_truncated_body_422_leaves_nothing(client: TestClient, uploads: Path) -> None:
    r = post(client, TO_PART, FILE, close=False)
    assert r.status_code == 422
    assert r.json()["detail"] == "the multipart form is incomplete"
    assert left(uploads) == []


def test_malformed_multipart_422_leaves_nothing(client: TestClient, uploads: Path) -> None:
    r = client.post(
        "/transcriptions",
        content=b"--garbage\r\nnot a form at all",
        headers={"Content-Type": content_type()},
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "the multipart form is malformed"
    assert left(uploads) == []


def test_malformed_after_file_started_leaves_nothing(client: TestClient, uploads: Path) -> None:
    body = multipart_body([TO_PART, FILE], close=False) + b"--" + BOUNDARY + b"\rXX"
    r = client.post("/transcriptions", content=body, headers={"Content-Type": content_type()})
    assert r.status_code == 422
    assert left(uploads) == []


# -- A4: size caps -------------------------------------------------------------------------


def test_declared_length_over_cap_413_without_reading(
    settings: Settings, store: JobStore, uploads: Path
) -> None:
    declared = str(CAP + MULTIPART_OVERHEAD_BYTES + 1)
    result = asgi(settings, store, [b"x"], headers={"content-length": declared})
    assert result.status == 413
    assert result.json() == {
        "detail": "the upload is larger than the 1000 bytes limit (EARMARK_MAX_UPLOAD_MB)"
    }
    assert result.receive_calls == 0
    assert not uploads.exists()


def test_declared_limit_text_in_mib(settings: Settings, store: JobStore) -> None:
    result = asgi(
        settings,
        store,
        [b"x"],
        cap=2 * 1024 * 1024,
        headers={"content-length": str(10 * 1024 * 1024)},
    )
    assert result.json()["detail"] == (
        "the upload is larger than the 2 MiB limit (EARMARK_MAX_UPLOAD_MB)"
    )


@pytest.mark.parametrize(("size", "status"), [(CAP, 202), (CAP + 1, 413)])
def test_file_one_byte_over_cap_413(
    client: TestClient, uploads: Path, size: int, status: int
) -> None:
    r = post(client, TO_PART, Part("file", audio(size - 3), "a.mp3", "audio/mpeg"))
    assert r.status_code == status
    assert len(left(uploads)) == (1 if status == 202 else 0)


def test_lying_content_length_caught_by_bytes_read(
    settings: Settings, store: JobStore, uploads: Path
) -> None:
    body = multipart_body([TO_PART, Part("file", audio(CAP * 3), "a.mp3", "audio/mpeg")])
    result = asgi(settings, store, split(body, 512), headers={"content-length": "100"})
    assert result.status == 413
    assert left(uploads) == []


def test_chunked_upload_without_length_over_cap_413(
    settings: Settings, store: JobStore, uploads: Path
) -> None:
    body = multipart_body([TO_PART, Part("file", audio(CAP * 3), "a.mp3", "audio/mpeg")])
    result = asgi(settings, store, split(body, 700))
    assert result.status == 413
    assert left(uploads) == []


def test_total_body_over_cap_plus_overhead_413(
    settings: Settings, store: JobStore, uploads: Path
) -> None:
    junk = Part("title", "x" * 100)
    body = multipart_body([TO_PART, junk, FILE]) + b"\x00" * (MULTIPART_OVERHEAD_BYTES + CAP)
    result = asgi(settings, store, split(body, 50_000))
    assert result.status == 413
    assert left(uploads) == []


# -- A6: auth first ------------------------------------------------------------------------


def test_auth_before_body_multipart(settings: Settings, store: JobStore) -> None:
    app = create_app(settings, store=store, max_upload_bytes=CAP)
    body = multipart_body([TO_PART, FILE])
    result = call_asgi(
        app,
        headers={"content-type": content_type(), "content-length": str(len(body))},
        chunks=split(body, 100),
    )
    assert result.status == 401
    assert result.receive_calls == 0
    assert not (settings.data_dir / "uploads").exists()


# -- P1: streaming, no tempfile, bounded memory --------------------------------------------


def test_upload_streams_to_staging_while_receiving(
    settings: Settings, store: JobStore, uploads: Path
) -> None:
    data = audio(400_000)
    body = multipart_body([TO_PART, Part("file", data, "a.mp3", "audio/mpeg")])
    chunks = split(body, 32_000)
    seen: dict[int, int] = {}

    def watch(n: int) -> None:
        parts = list(uploads.glob(".*.part")) if uploads.exists() else []
        seen[n] = parts[0].stat().st_size if parts else -1

    result = asgi(settings, store, chunks, cap=1024 * 1024, on_receive=watch)
    assert result.status == 202
    sizes = [seen[n] for n in sorted(seen) if seen[n] >= 0]
    assert sizes == sorted(sizes)
    last = len(chunks) - 1
    # Everything but the final chunk had been written, and was on disk, before it arrived.
    assert seen[last] >= len(data) - 2 * 32_000
    assert 0 < seen[len(chunks) // 2] < len(data)
    assert len(left(uploads)) == 1
    assert not list(uploads.glob(".*.part"))


def test_upload_never_uses_system_tempdir(
    settings: Settings,
    store: JobStore,
    uploads: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system_tmp = tmp_path / "system-tmp"
    system_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_tmp))
    monkeypatch.setenv("TMPDIR", str(system_tmp))

    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError("an upload must not touch the tempfile module")

    for name in (
        "SpooledTemporaryFile",
        "TemporaryFile",
        "NamedTemporaryFile",
        "mkstemp",
        "mkdtemp",
        "TemporaryDirectory",
    ):
        monkeypatch.setattr(tempfile, name, forbidden)
    body = multipart_body([TO_PART, Part("file", audio(CAP - 100), "a.mp3", "audio/mpeg")])
    result = asgi(settings, store, split(body, 200))
    assert result.status == 202
    assert list(system_tmp.iterdir()) == []
    assert len(left(uploads)) == 1


def test_upload_memory_bounded(settings: Settings, store: JobStore, uploads: Path) -> None:
    mib = 1024 * 1024
    block = bytes(64 * 1024)
    count = 16 * mib // len(block)
    prefix = multipart_body([TO_PART], close=False) + (
        b"--" + BOUNDARY + b'\r\nContent-Disposition: form-data; name="file"; filename="a.mp3"'
        b"\r\nContent-Type: audio/mpeg\r\n\r\n"
    )
    tail = b"\r\n--" + BOUNDARY + b"--\r\n"
    chunks = [prefix + block, *([block] * (count - 2)), block + tail]
    app = create_app(settings, store=store, max_upload_bytes=32 * mib)
    headers = {"authorization": f"Bearer {TOKEN}", "content-type": content_type()}
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        base, _ = tracemalloc.get_traced_memory()
        result = call_asgi(app, headers=headers, chunks=chunks)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.status == 202
    [name] = left(uploads)
    assert (uploads / name).stat().st_size == count * len(block)
    assert peak - base < 2 * mib


# -- P3: shape rules -----------------------------------------------------------------------


def detail(r: Any) -> str:
    return str(r.json()["detail"])


def test_unknown_field_422(client: TestClient, uploads: Path) -> None:
    r = post(client, TO_PART, Part("prompt", "x" * 100), FILE)
    assert r.status_code == 422
    assert detail(r) == (
        f"unknown field '{'x' * 0}prompt' (allowed: file, to, instructions, title, timestamp)"
    )
    assert left(uploads) == []


def test_unknown_field_name_truncated(client: TestClient) -> None:
    r = post(client, Part("k" * 100, "v"), FILE)
    assert f"unknown field '{'k' * 40}' (" in detail(r)


def test_url_field_in_form_is_unknown(client: TestClient) -> None:
    r = post(client, TO_PART, Part("url", "https://audio.example.net/a.mp3"), FILE)
    assert r.status_code == 422
    assert detail(r).startswith("unknown field 'url' (allowed: file, to,")


def test_duplicate_field_422(client: TestClient, uploads: Path) -> None:
    r = post(client, TO_PART, Part("to", TO), FILE)
    assert r.status_code == 422
    assert detail(r) == "field 'to' is given more than once"
    assert left(uploads) == []


def test_text_field_with_filename_422(client: TestClient) -> None:
    r = post(client, Part("to", TO, "to.txt"), FILE)
    assert r.status_code == 422
    assert detail(r) == "field 'to' must be a text field"


def test_two_file_parts_422(client: TestClient, uploads: Path) -> None:
    r = post(client, TO_PART, FILE, Part("file", audio(), "b.mp3", "audio/mpeg"))
    assert r.status_code == 422
    assert detail(r) == "the form must have exactly one file field named 'file'"
    assert left(uploads) == []


def test_no_file_part_422(client: TestClient) -> None:
    r = post(client, TO_PART)
    assert r.status_code == 422
    assert detail(r) == "the form must have exactly one file field named 'file'"


def test_empty_file_422(client: TestClient, uploads: Path) -> None:
    r = post(client, TO_PART, Part("file", b"", "a.mp3", "audio/mpeg"))
    assert r.status_code == 422
    assert detail(r) == "the uploaded file is empty"
    assert left(uploads) == []


def test_field_too_long_422(client: TestClient) -> None:
    r = post(client, TO_PART, Part("instructions", "x" * (MAX_FIELD_BYTES + 1)), FILE)
    assert r.status_code == 422
    assert detail(r) == "field 'instructions' is too long"


def test_field_at_the_byte_cap_reaches_validation(client: TestClient) -> None:
    r = post(client, TO_PART, Part("instructions", "x" * MAX_FIELD_BYTES), FILE)
    assert r.status_code == 422
    assert detail(r) == "instructions is too long (at most 4000 characters)"


def test_too_many_parts_guard() -> None:
    # Every valid form has at most five parts and anything else is refused at its headers, so
    # no request reaches this limit; the guard is defence in depth and is checked directly.
    from earmark.api.upload import UploadRejected, _State
    from earmark.staging import Staging

    state = _State(Staging(Path("unused")))
    for _ in range(MAX_PARTS):
        state.on_part_begin()
    with pytest.raises(UploadRejected) as info:
        state.on_part_begin()
    assert (info.value.status, info.value.detail) == (422, "the form has too many parts")


def test_field_not_utf8_422(client: TestClient, uploads: Path) -> None:
    r = post(client, TO_PART, Part("title", b"\xff\xfe bad"), FILE)
    assert r.status_code == 422
    assert detail(r) == "field 'title' is not valid UTF-8"
    assert left(uploads) == []


def test_missing_boundary_422(client: TestClient) -> None:
    r = client.post(
        "/transcriptions",
        content=multipart_body([TO_PART, FILE]),
        headers={"Content-Type": "multipart/form-data"},
    )
    assert r.status_code == 422
    assert detail(r) == "the multipart form has no boundary"


def test_overlong_boundary_422(client: TestClient) -> None:
    boundary = b"b" * 300
    r = client.post(
        "/transcriptions",
        content=multipart_body([TO_PART, FILE], boundary),
        headers={"Content-Type": content_type(boundary)},
    )
    assert r.status_code == 422
    assert detail(r) == "the multipart form is malformed"


def test_part_without_disposition_422(client: TestClient) -> None:
    body = b"--" + BOUNDARY + b"\r\nContent-Type: text/plain\r\n\r\nx\r\n--" + BOUNDARY + b"--\r\n"
    r = client.post("/transcriptions", content=body, headers={"Content-Type": content_type()})
    assert r.status_code == 422
    assert detail(r) == "the multipart form is malformed"


def test_validation_messages_never_echo_field_values(client: TestClient) -> None:
    r = post(client, Part("to", "SECRET-VALUE-77@@"), Part("timestamp", "SECRET-TS-99"), FILE)
    assert r.status_code == 422
    assert "SECRET" not in r.text


def test_json_still_works_alongside_multipart(client: TestClient) -> None:
    r = client.post("/transcriptions", json={"url": "https://audio.example.net/a.mp3", "to": TO})
    assert r.status_code == 202
