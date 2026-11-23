import logging
from pathlib import Path
from typing import Any

import pytest

from earmark.db import open_database
from earmark.jobs import JobStore, NewJob
from earmark.models import TranscriptionRequest, UploadFields
from earmark.recipients import RecipientPolicy, RecipientRefused
from earmark.resolve.apple import UnsupportedUrlError
from earmark.staging import StagedUpload, Staging
from earmark.submission import Intake
from tests.factories import T0, TO, MutableClock

SHOW = "1200361736"
EP = "1000802907981"
CANON = f"https://podcasts.apple.com/us/podcast/id{SHOW}?i={EP}"
OTHER = "other@example.org"


class Rig:
    def __init__(self, tmp_path: Path, allowed: tuple[str, ...] = (TO, OTHER)) -> None:
        self.store = JobStore(open_database(tmp_path / "earmark.db"), clock=MutableClock())
        self.staging = Staging(tmp_path / "data")
        self.intake = Intake(self.store, RecipientPolicy.from_setting(allowed), self.staging)

    def url(self, url: str, **kw: Any) -> Any:
        return self.intake.submit_url(TranscriptionRequest(url=url, to=kw.pop("to", TO), **kw))

    def stage(self, data: bytes = b"ID3" + bytes(40)) -> StagedUpload:
        with self.staging.begin_upload("mp3") as writer:
            writer.write(data)
            return writer.commit()

    def upload(self, staged: StagedUpload, **kw: Any) -> Any:
        fields = UploadFields(to=kw.pop("to", TO), **kw)
        return self.intake.submit_upload(fields, staged, upload_name="clip.mp3")

    def staged_files(self) -> list[str]:
        d = self.staging.uploads_dir
        return sorted(p.name for p in d.iterdir()) if d.exists() else []


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_apple_variants_dedupe(rig: Rig) -> None:
    variants = [
        f"https://podcasts.apple.com/us/podcast/the-slug/id{SHOW}?i={EP}&uo=4",
        CANON,
        f"https://itunes.apple.com/us/podcast/x/id{SHOW}?i={EP}&mt=2",
        f"http://podcasts.apple.com/US/podcast/x/id{SHOW}?i={EP}",
    ]
    results = [rig.url(u) for u in variants]
    assert [r.created for r in results] == [True, False, False, False]
    assert len({r.job.id for r in results}) == 1
    assert results[0].job.url == CANON
    assert results[0].job.source == "apple_podcasts"
    assert len(rig.store.list_recent()) == 1


def test_instructions_to_and_country_distinguish(rig: Rig) -> None:
    gb = f"https://podcasts.apple.com/gb/podcast/id{SHOW}?i={EP}"
    ids = [
        rig.url(CANON, instructions="a").job.id,
        rig.url(CANON).job.id,
        rig.url(CANON, instructions="b").job.id,
        rig.url(gb).job.id,
        rig.url(CANON, to=OTHER).job.id,
    ]
    assert len(set(ids)) == 5
    assert rig.url(CANON, instructions="  a ").job.id == ids[0]
    assert rig.url(CANON, title="ignored").job.id == ids[1]


def test_direct_url_job(rig: Rig) -> None:
    job = rig.url("https://audio.example.net/p/clip.mp3?sig=abc#frag", title="Clip").job
    assert (job.source, job.url, job.title, job.to) == (
        "url",
        "https://audio.example.net/p/clip.mp3?sig=abc",
        "Clip",
        TO,
    )


def test_timestamp_becomes_shared_at(rig: Rig) -> None:
    assert rig.url(CANON, timestamp=T0).job.shared_at == T0


def test_unsupported_url_enqueues_nothing(rig: Rig) -> None:
    for bad, problem in (
        (f"https://podcasts.apple.com/us/podcast/id{SHOW}", "show_link"),
        ("ftp://example.org/a.mp3", "malformed"),
    ):
        with pytest.raises(UnsupportedUrlError) as exc:
            rig.url(bad)
        assert exc.value.problem == problem
    assert rig.store.list_recent() == []


def test_recipient_refused_enqueues_nothing(rig: Rig) -> None:
    with pytest.raises(RecipientRefused) as exc:
        rig.url(CANON, to="stranger@example.com")
    assert "stranger" not in exc.value.message
    assert rig.store.list_recent() == []
    nobody = Rig(rig.staging.uploads_dir.parent.parent / "n", allowed=())
    with pytest.raises(RecipientRefused, match="no recipients are allowed yet"):
        nobody.url(CANON)


def test_recipient_is_checked_before_the_url(rig: Rig) -> None:
    with pytest.raises(RecipientRefused):
        rig.url("not even a url", to="stranger@example.com")


def test_upload_job_and_file_kept(rig: Rig) -> None:
    staged = rig.stage()
    enqueued = rig.upload(staged, instructions="Why?", title="T")
    job = enqueued.job
    assert enqueued.created
    assert (job.source, job.url, job.to, job.instructions, job.title) == (
        "upload",
        None,
        TO,
        "Why?",
        "T",
    )
    assert (job.upload_file, job.content_sha256, job.upload_name) == (
        staged.name,
        staged.sha256,
        "clip.mp3",
    )
    assert rig.staged_files() == [staged.name]


def test_upload_dedupe_deletes_second_copy(rig: Rig) -> None:
    first = rig.upload(rig.stage())
    second_staged = rig.stage()  # the same bytes under a new name
    second = rig.upload(second_staged)
    assert not second.created
    assert second.job.id == first.job.id
    assert rig.staged_files() == [first.job.upload_file]
    assert not second_staged.path.exists()
    assert rig.upload(rig.stage(b"different bytes")).created
    assert len(rig.staged_files()) == 2


def test_upload_recipient_refused_deletes_file(rig: Rig) -> None:
    staged = rig.stage()
    with pytest.raises(RecipientRefused):
        rig.upload(staged, to="stranger@example.com")
    assert rig.staged_files() == []
    assert rig.store.list_recent() == []


def test_upload_store_error_deletes_file(tmp_path: Path) -> None:
    class Broken(JobStore):
        def enqueue(self, new: NewJob, /) -> Any:
            raise RuntimeError("store exploded")

    rig = Rig(tmp_path)
    rig.intake = Intake(Broken(open_database(tmp_path / "b.db")), rig.intake.policy, rig.staging)
    staged = rig.stage()
    with pytest.raises(RuntimeError, match="store exploded"):
        rig.upload(staged)
    assert rig.staged_files() == []


def test_logging_is_masked_and_host_only(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="earmark.submission"):
        rig.url(CANON, to=TO, instructions="SECRET-INSTRUCTIONS")
        rig.url("https://audio.example.net/private/PATH-TOKEN.mp3?sig=Q", to=OTHER)
        rig.upload(rig.stage(), title="TITLE-TOKEN")
    texts = [r.getMessage() for r in caplog.records]
    assert len(texts) == 3
    assert texts[0].endswith(", to y…@example.org, instructions given (created=True)")
    assert CANON in texts[0]
    assert "audio URL on audio.example.net" in texts[1]
    assert "instructions none" in texts[1]
    assert "upload of 43 bytes" in texts[2]
    joined = "\n".join(texts)
    for secret in ("SECRET-INSTRUCTIONS", "PATH-TOKEN", "sig=Q", "TITLE-TOKEN", "you@", "other@"):
        assert secret not in joined
