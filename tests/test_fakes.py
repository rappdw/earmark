import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

from earmark.errors import PermanentError, TransientError
from earmark.fakes import (
    FAKE_NOW,
    FAKE_SENDER,
    DownloadCall,
    FakeAudioFetcher,
    FakeBackends,
    FakeMailer,
    FakeNotifier,
    FakeResolver,
    FakeTranscriber,
    FakeYoutubeSource,
    FetchCall,
    ResolveCall,
    TranscribeCall,
    YoutubeDownloadCall,
    YoutubeResolveCall,
    make_fake_backends,
)
from earmark.models import Episode, Transcript, youtube_key
from earmark.pipeline.stages import OutgoingMail
from tests.factories import make_episode, make_job

URL = "https://podcasts.apple.com/us/podcast/x/id1?i=2"


def _run(fb: FakeBackends) -> tuple[Episode, Transcript]:
    ep = fb.resolver.resolve(URL)
    audio = fb.fetcher.fetch(ep)
    return ep, fb.transcriber.transcribe(ep, audio)


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_fakes_deterministic(tmp_path: Path) -> None:
    a = make_fake_backends(tmp_path / "a" / "data")
    b = make_fake_backends(tmp_path / "b" / "data")
    assert _run(a) == _run(b)
    tree_a, tree_b = _tree(tmp_path / "a"), _tree(tmp_path / "b")
    assert tree_a == tree_b
    assert len(tree_a) == 3  # audio, transcript x2


def test_resolver_same_url_same_episode() -> None:
    r = FakeResolver()
    first = r.resolve(URL)
    assert r.resolve(URL) == first
    assert r.resolve(URL + "x") != first
    digest = hashlib.sha256(URL.encode()).hexdigest()[:12]
    assert first.episode_id == digest
    assert first.audio_url.endswith(".mp3")
    assert ".invalid" in first.audio_url


def test_resolver_mapping_wins() -> None:
    ep = make_episode()
    r = FakeResolver({URL: ep})
    assert r.resolve(URL) is ep
    assert r.resolve("other") != ep


def test_call_counts(tmp_path: Path) -> None:
    fb = make_fake_backends(tmp_path)
    ep, _ = _run(fb)
    audio = fb.artifacts.audio_path(ep.key, "mp3")
    job = make_job()
    fb.notifier.notify(job, None)
    assert fb.resolver.calls == [ResolveCall(URL)]
    assert fb.fetcher.calls == [FetchCall(ep)]
    assert fb.transcriber.calls == [TranscribeCall(ep, audio)]
    assert fb.notifier.calls[0].job == job
    assert fb.notifier.calls[0].episode is None
    assert fb.notifier.call_count == 1


def test_fetcher_cache_hit_not_computed(tmp_path: Path) -> None:
    fb = make_fake_backends(tmp_path)
    ep = fb.resolver.resolve(URL)
    first = fb.fetcher.fetch(ep)
    assert fb.fetcher.fetch(ep) == first
    assert (fb.fetcher.call_count, fb.fetcher.computed) == (2, 1)
    assert first.read_bytes() == b"fake audio\n"


def test_transcriber_cache_hit_not_computed(tmp_path: Path) -> None:
    fb = make_fake_backends(tmp_path)
    ep = fb.resolver.resolve(URL)
    audio = fb.fetcher.fetch(ep)
    first = fb.transcriber.transcribe(ep, audio)
    audio.unlink()  # a cache hit needs no audio
    assert fb.transcriber.transcribe(ep, audio) == first
    assert (fb.transcriber.call_count, fb.transcriber.computed) == (2, 1)
    assert first.duration_s == 900.0


def test_fail_next_once_then_succeeds(tmp_path: Path) -> None:
    fb = make_fake_backends(tmp_path)
    ep = fb.resolver.resolve(URL)
    audio = fb.fetcher.fetch(ep)
    fb.transcriber.fail_next(TransientError("later"))
    with pytest.raises(TransientError):
        fb.transcriber.transcribe(ep, audio)
    assert fb.transcriber.computed == 0
    fb.transcriber.transcribe(ep, audio)
    assert (fb.transcriber.call_count, fb.transcriber.computed) == (2, 1)


def test_fail_next_times_and_order() -> None:
    r = FakeResolver()
    first, second = TransientError("1"), PermanentError("2")
    r.fail_next(first, times=2)
    r.fail_next(second)
    for expected in (first, first, second):
        with pytest.raises(type(expected)) as info:
            r.resolve(URL)
        assert info.value is expected
    r.resolve(URL)
    assert r.call_count == 4


def test_fail_next_rejects_zero() -> None:
    with pytest.raises(ValueError, match="times"):
        FakeResolver().fail_next(PermanentError("x"), times=0)


def _stages(
    tmp_path: Path,
) -> list[tuple[Callable[[], int], Callable[[Exception], None], Callable[[], object]]]:
    fb = make_fake_backends(tmp_path)
    ep, _ = _run(fb)
    audio = fb.artifacts.audio_path(ep.key, "mp3")
    return [
        (
            lambda: fb.resolver.call_count,
            fb.resolver.fail_next,
            lambda: fb.resolver.resolve(URL),
        ),
        (lambda: fb.fetcher.call_count, fb.fetcher.fail_next, lambda: fb.fetcher.fetch(ep)),
        (
            lambda: fb.transcriber.call_count,
            fb.transcriber.fail_next,
            lambda: fb.transcriber.transcribe(ep, audio),
        ),
        (
            lambda: fb.notifier.call_count,
            fb.notifier.fail_next,
            lambda: fb.notifier.notify(make_job(), ep),
        ),
    ]


@pytest.mark.parametrize("stage_number", range(4))
def test_every_fake_supports_fail_next(tmp_path: Path, stage_number: int) -> None:
    count, fail_next, invoke = _stages(tmp_path)[stage_number]
    before = count()
    fail_next(PermanentError("boom"))
    with pytest.raises(PermanentError, match="boom"):
        invoke()
    assert count() == before + 1
    invoke()
    assert count() == before + 2


def test_fake_pipeline_writes_artifacts(tmp_path: Path) -> None:
    fb = make_fake_backends(tmp_path / "data")
    ep, _ = _run(fb)
    a = fb.artifacts
    assert not a.episode_path(ep.key).exists()
    assert a.find_audio(ep.key) == a.audio_path(ep.key, "mp3")
    assert a.transcript_json_path(ep.key).is_file()
    assert a.transcript_text_path(ep.key).is_file()
    assert sorted(p.name for p in a.episode_dir(ep.key).iterdir()) == [
        "audio.mp3",
        "transcript.json",
        "transcript.txt",
    ]


def test_transcriber_missing_audio_is_permanent(tmp_path: Path) -> None:
    fb = make_fake_backends(tmp_path)
    ep = fb.resolver.resolve(URL)
    with pytest.raises(PermanentError, match=r"audio file missing: nope\.mp3"):
        fb.transcriber.transcribe(ep, tmp_path / "nope.mp3")
    assert fb.transcriber.computed == 0


def test_fake_warnings_flow(tmp_path: Path) -> None:
    artifacts = make_fake_backends(tmp_path).artifacts
    transcriber = FakeTranscriber(artifacts, warnings=["tw"])
    ep = FakeResolver().resolve(URL)
    audio = FakeAudioFetcher(artifacts).fetch(ep)
    tr = transcriber.transcribe(ep, audio)
    assert tr.warnings == ("tw",)
    assert transcriber.transcribe(ep, audio).warnings == ("tw",)


def test_notifier_records() -> None:
    n = FakeNotifier()
    n.notify(make_job(), None)
    assert n.call_count == 1


def test_fake_backends_as_backends(tmp_path: Path) -> None:
    fb = make_fake_backends(tmp_path)
    b = fb.backends()
    assert b.resolver is fb.resolver
    assert b.fetcher is fb.fetcher
    assert b.transcriber is fb.transcriber
    assert b.notifier is fb.notifier


def test_fake_download_records_writes_and_fails(tmp_path: Path) -> None:
    fetcher = FakeAudioFetcher(
        make_fake_backends(tmp_path).artifacts, payload=b"default", payloads={"u2": b"special"}
    )
    first = fetcher.download("u1", lambda ext: tmp_path / f"one.{ext}")
    second = fetcher.download("u2", lambda ext: tmp_path / f"two.{ext}")
    assert (first.read_bytes(), second.read_bytes()) == (b"default", b"special")
    assert (fetcher.downloads, fetcher.computed) == (2, 0)
    assert fetcher.calls == [DownloadCall("u1"), DownloadCall("u2")]
    fetcher.fail_next(PermanentError("gone"))
    with pytest.raises(PermanentError, match="gone"):
        fetcher.download("u3", lambda ext: tmp_path / f"three.{ext}")
    assert fetcher.downloads == 2
    assert not (tmp_path / "three.mp3").exists()


def test_fake_transcriber_cache_follows_profile(tmp_path: Path) -> None:
    fb = make_fake_backends(tmp_path)
    ep, transcript = _run(fb)
    assert transcript.asr_profile == fb.transcriber.profile
    assert fb.transcriber.cached(ep) == transcript
    other = FakeTranscriber(fb.artifacts, model="other-model")
    assert other.cached(ep) is None
    assert other.profile != fb.transcriber.profile
    legacy = transcript.model_copy(update={"asr_profile": None})
    fb.artifacts.write_transcript(legacy)
    assert fb.transcriber.cached(ep) is None


def _mail(job_id: str = "a" * 32, data: bytes = b"Subject: x\r\n\r\nbody\r\n") -> OutgoingMail:
    return OutgoingMail(
        job_id=job_id,
        message_id=f"<earmark.{job_id}@example.org>",
        sender=FAKE_SENDER,
        recipient="you@example.org",
        data=data,
        note_filename="n.md",
        instructions_filename="n.instructions.md",
    )


def test_fake_mailer_records_and_fails_on_request() -> None:
    mailer = FakeMailer()
    assert mailer.sender == FAKE_SENDER
    mail = _mail()
    mailer.send(mail)
    assert mailer.calls == [mail]
    mailer.fail_next(TransientError("later"))
    with pytest.raises(TransientError, match="later"):
        mailer.send(mail)
    assert mailer.call_count == 2
    mailer.send(mail)
    assert mailer.call_count == 3
    assert FakeMailer(sender="me@example.net").sender == "me@example.net"


def test_fake_mailer_outbox_is_private_and_overwritten_on_resend(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    mailer = FakeMailer(outbox=outbox)
    mailer.send(_mail(data=b"first\r\n"))
    path = outbox / f"{'a' * 32}.eml"
    assert path.read_bytes() == b"first\r\n"
    assert path.stat().st_mode & 0o777 == 0o600
    mailer.send(_mail(data=b"second\r\n"))  # a resend of the same job
    assert path.read_bytes() == b"second\r\n"
    assert [p.name for p in outbox.iterdir()] == [path.name]
    failing = FakeMailer(outbox=outbox)
    failing.fail_next(PermanentError("no"))
    with pytest.raises(PermanentError):
        failing.send(_mail(job_id="b" * 32))
    assert not (outbox / f"{'b' * 32}.eml").exists()  # nothing is written for a failed send


def test_fake_backends_carry_a_mailer(tmp_path: Path) -> None:
    fb = make_fake_backends(tmp_path / "data")
    assert fb.backends().mailer is fb.mailer
    assert fb.mailer.sender == FAKE_SENDER
    assert not (tmp_path / "data" / "outbox").exists()  # the test fake keeps mail in memory only


def test_fake_resolver_sets_feed_guid() -> None:
    episode = FakeResolver().resolve(URL)
    assert episode.feed_guid == episode.guid
    assert episode.published_date_only is False


def test_fake_youtube_source(tmp_path: Path) -> None:
    source = FakeYoutubeSource(payload=b"default", payloads={"ex_ample-02": b"special"})
    episode = source.resolve("jNQXAC9IVRw")
    assert episode.key == youtube_key("jNQXAC9IVRw")
    assert (episode.title, episode.show_title) == ("Fake video jNQXAC9IVRw", "Fake Channel")
    assert (episode.published_at, episode.duration_s) == (FAKE_NOW, 600.0)
    assert source.resolve("jNQXAC9IVRw") == episode  # deterministic
    first = source.download("jNQXAC9IVRw", lambda ext: tmp_path / f"one.{ext}")
    second = source.download("ex_ample-02", lambda ext: tmp_path / f"two.{ext}")
    assert (first.name, first.read_bytes(), second.read_bytes()) == (
        "one.webm",
        b"default",
        b"special",
    )
    assert source.downloads == 2
    assert source.calls == [
        YoutubeResolveCall("jNQXAC9IVRw"),
        YoutubeResolveCall("jNQXAC9IVRw"),
        YoutubeDownloadCall("jNQXAC9IVRw"),
        YoutubeDownloadCall("ex_ample-02"),
    ]
    source.fail_next(PermanentError("gone"))
    with pytest.raises(PermanentError, match="gone"):
        source.download("jNQXAC9IVRw", lambda ext: tmp_path / f"three.{ext}")
    assert source.downloads == 2
    assert not (tmp_path / "three.webm").exists()
    custom = FakeYoutubeSource(episodes={"jNQXAC9IVRw": episode.model_copy(update={"title": "T"})})
    assert custom.resolve("jNQXAC9IVRw").title == "T"


def test_fake_backends_carry_a_youtube_source(tmp_path: Path) -> None:
    fb = make_fake_backends(tmp_path)
    assert isinstance(fb.youtube, FakeYoutubeSource)
    assert fb.backends().youtube is fb.youtube


def test_fake_transcriber_options(tmp_path: Path) -> None:
    from earmark.models import DecodeOptions
    from tests.factories import make_upload_episode

    fb = make_fake_backends(tmp_path)
    episode = make_upload_episode()
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    prompted = DecodeOptions(initial_prompt="Alice", language="de")
    other = DecodeOptions(initial_prompt="Bob")
    assert fb.transcriber.cached(episode, prompted) is None
    first = fb.transcriber.transcribe(episode, audio, prompted)
    second = fb.transcriber.transcribe(episode, audio, other)
    assert fb.transcriber.computed == 2
    assert fb.transcriber.calls[0].options == prompted
    assert (first.language, second.language) == ("de", "en")
    assert first.segments[-1].text != second.segments[-1].text
    assert first.segments[-1].text.startswith("Prompt ")
    assert "Alice" not in first.model_dump_json()
    assert fb.transcriber.cached(episode, prompted) == first
    assert fb.transcriber.transcribe(episode, audio, prompted) == first
    assert fb.transcriber.computed == 2
    assert fb.transcriber.cached(episode) is None  # the mail tree is separate
    assert fb.transcriber.profile_for(None) == fb.transcriber.profile
    assert "Alice" not in fb.transcriber.profile_for(prompted)
    assert len(list((tmp_path / "transcripts").iterdir())) == 2
    assert not (tmp_path / "episodes").exists()
    plain = fb.transcriber.transcribe(episode, audio, DecodeOptions())
    assert [s.text for s in plain.segments][-1] == "Thanks for listening."
    with pytest.raises(PermanentError, match="uploaded audio only"):
        fb.transcriber.cached(make_episode(), prompted)
