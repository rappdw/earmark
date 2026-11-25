"""earmark.transcripts: a job's transcript from the cache."""

from itertools import product
from pathlib import Path
from typing import get_args

import pytest

from earmark.artifacts import ArtifactStore
from earmark.errors import PermanentError
from earmark.models import (
    Episode,
    JobStatus,
    Segment,
    Stage,
    Transcript,
    TranscriptFormat,
    content_key,
)
from earmark.sources import audio_episode
from earmark.transcripts import (
    TranscriptUnavailable,
    episode_for_job,
    has_transcript,
    read_transcript_file,
)
from tests.factories import SHA, make_audio_job, make_delivery_job, make_episode, make_transcript

SHA_OTHER = "cd" * 32


@pytest.mark.parametrize(
    ("status", "stage"), list(product(get_args(JobStatus), (None, *get_args(Stage))))
)
def test_has_transcript_table(status: JobStatus, stage: Stage | None) -> None:
    job = make_delivery_job().model_copy(update={"status": status, "stage": stage})
    assert has_transcript(job) is (status == "done" or stage == "deliver")


def test_episode_for_job_upload_reads_nothing(tmp_path: Path) -> None:
    job, expected = make_audio_job(source="upload")
    store = ArtifactStore(tmp_path / "data")
    assert episode_for_job(job, store) == expected
    assert not (tmp_path / "data").exists()


def test_episode_for_job_url_rebuilds_from_guid(tmp_path: Path) -> None:
    upload, _ = make_audio_job(source="upload", title="Other title")
    url_job, expected = make_audio_job(source="url", title="Own title")
    store = ArtifactStore(tmp_path)
    store.write_episode(audio_episode(upload, SHA))  # the last writer was the upload
    key = content_key(SHA)
    url_job = url_job.model_copy(update={"episode_key": key})
    rebuilt = episode_for_job(url_job, store)
    assert rebuilt == expected
    assert rebuilt is not None
    assert (rebuilt.source, rebuilt.title) == ("url", "Own title")


def test_episode_for_job_url_rejects_non_sha_guid(tmp_path: Path) -> None:
    job, _ = make_audio_job(source="url")
    store = ArtifactStore(tmp_path)
    key = content_key(SHA)
    bad = audio_episode(job, SHA).model_copy(update={"guid": "urn:example:not-a-hash"})
    store.write_episode(bad)
    with pytest.raises(PermanentError):
        episode_for_job(job.model_copy(update={"episode_key": key}), store)


def test_episode_for_job_url_rejects_other_key(tmp_path: Path) -> None:
    job, _ = make_audio_job(source="url")
    store = ArtifactStore(tmp_path)
    key = content_key(SHA)
    other = audio_episode(job, SHA_OTHER).model_copy(update={"key": key})
    store.write_episode(other)
    with pytest.raises(PermanentError):
        episode_for_job(job.model_copy(update={"episode_key": key}), store)


def test_episode_for_job_podcast_is_as_stored(tmp_path: Path) -> None:
    episode: Episode = make_episode()
    store = ArtifactStore(tmp_path)
    store.write_episode(episode)
    job = make_delivery_job().model_copy(update={"episode_key": episode.key})
    assert episode_for_job(job, store) == episode
    assert episode_for_job(make_delivery_job(), store) is None


def test_read_transcript_file_creates_nothing(tmp_path: Path) -> None:
    job = make_delivery_job().model_copy(update={"episode_key": make_episode().key})
    formats: tuple[TranscriptFormat, ...] = ("note", "text", "json")
    for fmt in formats:
        with pytest.raises(TranscriptUnavailable) as caught:
            read_transcript_file(job, ArtifactStore(tmp_path / "data"), fmt)
        assert caught.value.reason == "gone"
    assert not (tmp_path / "data" / "episodes").exists()


def test_no_episode_key_is_gone(tmp_path: Path) -> None:
    with pytest.raises(TranscriptUnavailable) as caught:
        read_transcript_file(make_delivery_job(), ArtifactStore(tmp_path), "note")
    assert caught.value.reason == "gone"
    assert "no episode key" in caught.value.detail


def test_unavailable_is_raised_from_none(tmp_path: Path) -> None:
    transcript = make_transcript()
    store = ArtifactStore(tmp_path)
    store.write_transcript(transcript)
    store.transcript_json_path(transcript.episode_key).write_text('{"episode_key": "SECRET"}')
    job = make_delivery_job().model_copy(update={"episode_key": transcript.episode_key})
    with pytest.raises(TranscriptUnavailable) as caught:
        read_transcript_file(job, store, "json")
    assert caught.value.reason == "unreadable"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert "SECRET" not in caught.value.detail


def test_json_bytes_format(tmp_path: Path) -> None:
    episode = make_episode()
    transcript = Transcript(
        episode_key=episode.key,
        language="de",
        duration_s=5.0,
        model="private-model",
        segments=(Segment(start=0, end=5, text="Grüße"),),
        asr_profile="private-profile",
    )
    store = ArtifactStore(tmp_path)
    store.write_transcript(transcript)
    job = make_delivery_job().model_copy(update={"episode_key": episode.key})
    found = read_transcript_file(job, store, "json")
    text = found.data.decode("utf-8")
    assert text.startswith('{\n  "job_id"')
    assert text.endswith("}\n")
    assert "Grüße" in text
    assert "private" not in text
    assert (found.format, found.filename) == ("json", None)
