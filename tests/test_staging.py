import hashlib
import logging
import os
import re
from pathlib import Path

import pytest

from earmark.staging import UPLOADS_DIR, Staging
from tests.factories import new_upload_job, new_url_job
from tests.pipeline_env import make_env

UPLOAD_NAME = re.compile(r"[0-9a-f]{32}\.mp3")


@pytest.fixture
def staging(tmp_path: Path) -> Staging:
    return Staging(tmp_path / "data")


def test_directories_are_under_data_dir(staging: Staging, tmp_path: Path) -> None:
    assert staging.uploads_dir == tmp_path / "data" / UPLOADS_DIR == tmp_path / "data" / "uploads"
    assert staging.downloads_dir == tmp_path / "data" / "downloads"
    assert not staging.uploads_dir.exists()  # nothing is created until it is needed


def test_writer_commit_names_and_mode(staging: Staging) -> None:
    data = b"ID3" + bytes(5000)
    writer = staging.begin_upload("mp3")
    partial = list(staging.uploads_dir.iterdir())
    assert len(partial) == 1
    assert partial[0].name.startswith(".")
    assert partial[0].name.endswith(".mp3.part")
    writer.write(data[:100])
    writer.write(data[100:])
    assert writer.size == len(data)
    staged = writer.commit()
    assert UPLOAD_NAME.fullmatch(staged.name)
    assert staged.path == staging.uploads_dir / staged.name
    assert staged.size == len(data)
    assert staged.sha256 == hashlib.sha256(data).hexdigest()
    assert staged.path.read_bytes() == data
    assert staged.path.stat().st_mode & 0o777 == 0o600
    assert staging.uploads_dir.stat().st_mode & 0o777 == 0o700
    assert [p.name for p in staging.uploads_dir.iterdir()] == [staged.name]
    writer.abort()  # a no-op after commit
    assert staged.path.exists()


def test_each_upload_gets_its_own_name(staging: Staging) -> None:
    names = set()
    for _ in range(5):
        with staging.begin_upload("m4a") as writer:
            writer.write(b"x")
            names.add(writer.commit().name)
    assert len(names) == 5
    assert all(n.endswith(".m4a") for n in names)


def test_writer_abort_and_context_exit_remove_part(staging: Staging) -> None:
    writer = staging.begin_upload("mp3")
    writer.write(b"partial")
    writer.abort()
    writer.abort()  # idempotent
    assert list(staging.uploads_dir.iterdir()) == []
    with pytest.raises(ValueError, match="closed"):
        writer.write(b"more")

    def fail_inside_block() -> None:
        with staging.begin_upload("mp3") as inner:
            inner.write(b"partial")
            assert len(list(staging.uploads_dir.iterdir())) == 1
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        fail_inside_block()
    assert list(staging.uploads_dir.iterdir()) == []
    with staging.begin_upload("mp3") as unfinished:
        unfinished.write(b"never committed")
    assert list(staging.uploads_dir.iterdir()) == []


def test_commit_failure_leaves_nothing(staging: Staging, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(self: Path, target: object) -> Path:
        raise OSError("disk gone")

    writer = staging.begin_upload("mp3")
    writer.write(b"data")
    monkeypatch.setattr(Path, "replace", broken)
    with pytest.raises(OSError, match="disk gone"):
        writer.commit()
    monkeypatch.undo()
    assert list(staging.uploads_dir.iterdir()) == []


def test_begin_upload_rejects_bad_extensions(staging: Staging) -> None:
    for bad in ("", "MP3", "mp3/../x", "toolongext", "m p3", ".mp3"):
        with pytest.raises(ValueError, match="extension"):
            staging.begin_upload(bad)
    assert not staging.uploads_dir.exists()


def test_no_tempfile_in_staging_modules() -> None:
    source = Path(__file__).resolve().parent.parent / "src" / "earmark"
    for name in ("staging.py", "submission.py", "api/upload.py"):
        assert "tempfile" not in (source / name).read_text(), name


def test_upload_path_rejects_bad_names(staging: Staging) -> None:
    good = f"{'a' * 32}.mp3"
    assert staging.upload_path(good) == staging.uploads_dir / good
    for bad in (
        "../x.mp3",
        f"../{good}",
        f"{'a' * 31}.mp3",
        f"{'A' * 32}.mp3",
        f"{'a' * 32}.MP3",
        f"{'a' * 32}.mp3/..",
        f".{good}.part",
        "",
        f"{'a' * 32}.toolong",
    ):
        with pytest.raises(ValueError, match="staged upload name"):
            staging.upload_path(bad)
    with pytest.raises(ValueError, match="staged upload name"):
        staging.remove_upload("../etc/passwd")


def test_remove_upload(staging: Staging) -> None:
    with staging.begin_upload("mp3") as writer:
        writer.write(b"x")
        staged = writer.commit()
    assert staging.remove_upload(staged.name) is True
    assert staging.remove_upload(staged.name) is False


def test_download_destination_and_clear(staging: Staging) -> None:
    job_id = "1" * 32
    destination = staging.download_destination(job_id)
    assert not staging.downloads_dir.exists()
    path = destination("mp3")
    assert path == staging.downloads_dir / f"{job_id}.mp3"
    assert staging.downloads_dir.stat().st_mode & 0o777 == 0o700
    path.write_bytes(b"x")
    (staging.downloads_dir / f".{job_id}.mp3.abc123.part").write_bytes(b"p")
    (staging.downloads_dir / f"{'2' * 32}.mp3").write_bytes(b"other job")
    assert staging.clear_download(job_id) == 2
    assert [p.name for p in staging.downloads_dir.iterdir()] == [f"{'2' * 32}.mp3"]
    assert staging.clear_download(job_id) == 0
    assert Staging(staging.downloads_dir.parent / "elsewhere").clear_download(job_id) == 0
    with pytest.raises(ValueError, match="extension"):
        destination("../mp3")
    for bad in ("", "../x", "G" * 32, "1" * 31):
        with pytest.raises(ValueError, match="job id"):
            staging.download_destination(bad)
        with pytest.raises(ValueError, match="job id"):
            staging.clear_download(bad)


def test_sweep_uploads_keeps_active_removes_rest(
    staging: Staging, caplog: pytest.LogCaptureFixture
) -> None:
    assert staging.sweep_uploads(set()) == 0  # no directory yet
    keep = f"{'a' * 32}.mp3"
    staging.uploads_dir.mkdir(parents=True)
    (staging.uploads_dir / keep).write_bytes(b"queued job")
    (staging.uploads_dir / f"{'b' * 32}.mp3").write_bytes(b"orphan")
    (staging.uploads_dir / f".{'c' * 32}.mp3.part").write_bytes(b"crashed")
    (staging.uploads_dir / "stray.txt").write_bytes(b"?")
    (staging.uploads_dir / "subdir").mkdir()
    (staging.uploads_dir / "subdir" / "x").write_bytes(b"left alone")
    with caplog.at_level(logging.INFO, logger="earmark.staging"):
        removed = staging.sweep_uploads({keep})
    assert removed == 3
    assert sorted(p.name for p in staging.uploads_dir.iterdir()) == [keep, "subdir"]
    assert (staging.uploads_dir / "subdir" / "x").exists()
    messages = [r.getMessage() for r in caplog.records]
    assert any("3" in m for m in messages)
    assert not any("b" * 32 in m for m in messages)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="earmark.staging"):
        assert staging.sweep_uploads({keep}) == 0
    assert caplog.records == []


def test_sweep_downloads_keeps_running_only(staging: Staging) -> None:
    assert staging.sweep_downloads(set()) == 0
    running = "1" * 32
    staging.downloads_dir.mkdir(parents=True)
    kept = [f"{running}.mp3", f".{running}.mp3.zzz.part"]
    for name in (*kept, f"{'2' * 32}.mp3", f".{'3' * 32}.mp3.zzz.part", "stray.bin"):
        (staging.downloads_dir / name).write_bytes(b"x")
    assert staging.sweep_downloads({running}) == 3
    assert sorted(p.name for p in staging.downloads_dir.iterdir()) == sorted(kept)
    assert staging.sweep_downloads(set()) == 2
    assert list(staging.downloads_dir.iterdir()) == []


def test_release_per_source(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    staging = env.staging
    with staging.begin_upload("mp3") as writer:
        writer.write(b"upload")
        staged = writer.commit()
    upload = env.jobs.enqueue(new_upload_job(staged.sha256, upload_file=staged.name)).job
    url = env.jobs.enqueue(new_url_job()).job
    podcast_dir = staging.downloads_dir
    staging.download_destination(url.id)("mp3").write_bytes(b"download")
    (podcast_dir / f"{'9' * 32}.mp3").write_bytes(b"someone else's")

    staging.release(upload)
    assert not staged.path.exists()
    assert (podcast_dir / f"{url.id}.mp3").exists()
    staging.release(url)
    assert [p.name for p in podcast_dir.iterdir()] == [f"{'9' * 32}.mp3"]

    from tests.factories import new_podcast_job

    podcast = env.jobs.enqueue(new_podcast_job()).job
    staging.release(podcast)  # nothing to release, nothing raised
    assert [p.name for p in podcast_dir.iterdir()] == [f"{'9' * 32}.mp3"]


def test_release_never_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path)
    with env.staging.begin_upload("mp3") as writer:
        writer.write(b"upload")
        staged = writer.commit()
    job = env.jobs.enqueue(new_upload_job(staged.sha256, upload_file=staged.name)).job

    def denied(self: Path, *, missing_ok: bool = False) -> None:
        raise PermissionError(13, "denied", str(self))

    monkeypatch.setattr(Path, "unlink", denied)
    with caplog.at_level(logging.WARNING, logger="earmark.staging"):
        env.staging.release(job)  # does not raise
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert staged.name not in record.getMessage()
    assert "PermissionError" in record.getMessage()
    monkeypatch.undo()
    assert staged.path.exists()
    assert os.access(staged.path, os.R_OK)
