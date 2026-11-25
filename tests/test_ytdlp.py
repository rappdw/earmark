"""The ``ytdlp`` backend on a fake yt-dlp runtime: recorded metadata, no network, no yt-dlp."""

import io
import math
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from earmark.config import ConfigError, Settings
from earmark.errors import EarmarkError, PermanentError, TransientError
from earmark.log import configure_logging
from earmark.models import youtube_key
from earmark.youtube import ytdlp
from earmark.youtube.links import canonical_youtube_url
from earmark.youtube.ytdlp import (
    AUDIO_FORMAT,
    DOWNLOAD_DEADLINE_S,
    PENDING_MAX_AGE_S,
    REFUSALS,
    MissingYoutubeExtraError,
    YtDlpYoutubeSource,
    build_ytdlp_source,
    classify_error,
    deno_environment,
    load_runtime,
    reason_from_text,
    ytdlp_params,
)
from tests.ytdlp_fakes import (
    FAKE_ERRORS,
    FakeContentTooShortError,
    FakeDownloadCancelled,
    FakeDownloadError,
    FakeExtractorError,
    FakeGeoRestrictedError,
    FakeHTTPError,
    FakeIncompleteRead,
    FakeRuntime,
    FakeTransportError,
    Script,
    fake_runtime,
    public_info,
    refusal_info,
    wrap,
)

ID = "jNQXAC9IVRw"
MIB = 1024 * 1024
Destination = Callable[[str], Path]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_source(
    tmp_path: Path, *scripts: Script, max_bytes: int = 100 * MIB, clock: Clock | None = None
) -> tuple[YtDlpYoutubeSource, FakeRuntime]:
    fake = fake_runtime(tmp_path, *scripts)
    source = YtDlpYoutubeSource(
        fake.runtime,
        max_bytes=max_bytes,
        deno_dir=tmp_path / "data" / "cache" / "deno",
        clock=clock or Clock(),
    )
    return source, fake


def destination(tmp_path: Path) -> Destination:
    def make(ext: str) -> Path:
        return tmp_path / "downloads" / f"job.{ext}"

    return make


def playable(**changes: Any) -> Script:
    return Script(info=public_info(**changes))


def refused(name: str) -> Script:
    info, warnings = refusal_info(name)
    return Script(info=info, warnings=warnings)


def unplayable(reason: str, **changes: Any) -> Script:
    """The metadata of an unplayable video: no formats, the reason as a warning."""
    info = public_info(formats=[], **changes)
    return Script(info=info, warnings=[reason, "No video formats found!"])


# -- success ---------------------------------------------------------------------------


def test_resolve_builds_episode(tmp_path: Path) -> None:
    source, _ = make_source(tmp_path, playable())
    episode = source.resolve(ID)
    assert episode.key == youtube_key(ID)
    assert (episode.source, episode.show_id, episode.episode_id) == ("youtube", "youtube", ID)
    assert episode.title == "Me at the zoo"
    assert episode.show_title == "jawed"
    assert episode.published_at is not None
    assert episode.published_at.isoformat() == "2005-04-24T03:31:52+00:00"
    assert episode.published_date_only is False
    assert episode.duration_s == 19.0
    assert episode.audio_url == ""
    assert episode.description == ""
    assert episode.url == canonical_youtube_url(ID)


def test_download_writes_destination(tmp_path: Path) -> None:
    source, fake = make_source(tmp_path, playable())
    source.resolve(ID)
    path = source.download(ID, destination(tmp_path))
    assert path == tmp_path / "downloads" / "job.webm"
    assert path.read_bytes() == b"fake audio\n"
    assert fake.downloads[0].params["outtmpl"] == {"default": str(path)}


def test_one_extraction_per_job(tmp_path: Path) -> None:
    source, fake = make_source(tmp_path, playable())
    source.resolve(ID)
    source.download(ID, destination(tmp_path))
    assert len(fake.extracts) == 1
    assert len(fake.downloads) == 1
    assert len(fake.sessions) == 2
    assert "outtmpl" not in fake.extracts[0].params
    assert "outtmpl" in fake.downloads[0].params
    assert fake.extracts[0].args == (canonical_youtube_url(ID),)
    assert fake.extracts[0].kwargs == {"download": False, "process": True, "ie_key": "Youtube"}
    assert fake.downloads[0].kwargs == {"download": True}
    assert all(s.closed for s in fake.sessions)


def test_download_without_resolve_extracts(tmp_path: Path) -> None:
    source, fake = make_source(tmp_path, playable())
    source.download(ID, destination(tmp_path))
    assert len(fake.extracts) == 1
    assert len(fake.downloads) == 1


def test_pending_is_used_once(tmp_path: Path) -> None:
    source, fake = make_source(tmp_path, playable())
    source.resolve(ID)
    source.download(ID, destination(tmp_path))
    source.download(ID, destination(tmp_path))
    assert len(fake.extracts) == 2


def test_pending_is_for_one_video(tmp_path: Path) -> None:
    source, fake = make_source(tmp_path, playable())
    source.resolve(ID)
    with pytest.raises(PermanentError):  # the fake returns the first video's info
        source.download("ex_ample-02", destination(tmp_path))
    assert len(fake.extracts) == 2


def test_stale_pending_reextracts(tmp_path: Path) -> None:
    clock = Clock()
    source, fake = make_source(tmp_path, playable(), clock=clock)
    source.resolve(ID)
    clock.now += PENDING_MAX_AGE_S + 1
    source.download(ID, destination(tmp_path))
    assert len(fake.extracts) == 2


def test_outtmpl_doubles_percent(tmp_path: Path) -> None:
    source, fake = make_source(tmp_path, playable())
    source.resolve(ID)
    path = source.download(ID, lambda ext: tmp_path / "a%b" / f"job.{ext}")
    assert fake.downloads[0].params["outtmpl"]["default"] == str(tmp_path / "a%%b" / "job.webm")
    assert path == tmp_path / "a%b" / "job.webm"
    assert path.is_file()


# -- refusals --------------------------------------------------------------------------

REFUSAL_CASES: list[tuple[str, Script, str, str]] = [
    ("private", refused("private.json"), "private", "yZIXLfi8CZQ"),
    ("removed", refused("removed.json"), "removed", "aaaaaaaaaaa"),
    ("tos-removed", refused("tos_removed.json"), "removed", "Cr381pDsSsA"),
    ("age-gated", refused("age_gated.json"), "age", "HtVdAasjOgU"),
    ("post-live", refused("post_live.json"), "post_live", "jfKfPfyJRdk"),
    ("members", playable(availability="subscriber_only"), "members", ID),
    ("premium", playable(availability="premium_only"), "premium", ID),
    ("sign-in", playable(availability="needs_auth"), "sign_in", ID),
    ("live", playable(live_status="is_live"), "live", ID),
    ("upcoming", playable(live_status="is_upcoming"), "upcoming", ID),
    ("playlist", playable(_type="playlist"), "not_video", ID),
    ("other-id", playable(id="aaaaaaaaaaa"), "not_video", ID),
    (
        "bot",
        unplayable("[youtube] Sign in to confirm you\u2019re not a bot. Use cookies."),
        "bot",
        ID,
    ),
    (
        "members-text",
        unplayable("Join this channel to get access to members-only content like this video."),
        "members",
        ID,
    ),
    (
        "geo",
        unplayable("The uploader has not made this video available in your country"),
        "geo",
        ID,
    ),
    ("unknown-reason", unplayable("something new"), "refused", ID),
]


@pytest.mark.parametrize(
    ("script", "key", "video_id"),
    [c[1:] for c in REFUSAL_CASES],
    ids=[c[0] for c in REFUSAL_CASES],
)
def test_refusals(tmp_path: Path, script: Script, key: str, video_id: str) -> None:
    source, fake = make_source(tmp_path, script)
    with pytest.raises(PermanentError) as caught:
        source.resolve(video_id)
    assert caught.value.message == REFUSALS[key]
    assert not fake.downloads


def test_refusal_from_extractor_error(tmp_path: Path) -> None:
    cases = {
        "Private video. Sign in if you've been granted access": "private",
        "This video is only available to Music Premium members": "premium",
        "Sign in to confirm your age. This video may be inappropriate": "age",
        "Sign in to confirm you’re not a bot": "bot",  # noqa: RUF001  # YouTube's apostrophe
        "Join this channel to get access to members-only content": "members",
        "This video is not available in your country": "geo",
        "Video unavailable": "removed",
        "Premieres in 3 hours": "upcoming",
        "something else entirely": "refused",
    }
    for text, key in cases.items():
        source, _ = make_source(
            tmp_path / key, Script(extract_error=wrap(FakeExtractorError(text, expected=True)))
        )
        with pytest.raises(PermanentError) as caught:
            source.resolve(ID)
        assert caught.value.message == REFUSALS[key], text
        assert text not in caught.value.message


def test_geo_restricted_error(tmp_path: Path) -> None:
    source, _ = make_source(
        tmp_path, Script(extract_error=wrap(FakeGeoRestrictedError("x", expected=True)))
    )
    with pytest.raises(PermanentError) as caught:
        source.resolve(ID)
    assert caught.value.message == REFUSALS["geo"]


def test_rate_limited_is_transient(tmp_path: Path) -> None:
    source, _ = make_source(
        tmp_path,
        unplayable("This content isn't available, try again later."),
    )
    with pytest.raises(TransientError) as caught:
        source.resolve(ID)
    assert caught.value.retry_after_s == 3600.0
    assert caught.value.message == REFUSALS["rate_limited"]


@pytest.mark.parametrize(
    "error",
    [
        FakeTransportError("boom"),
        FakeHTTPError(503),
        FakeHTTPError(429),
        FakeHTTPError(408),
        FakeIncompleteRead("short"),
        FakeContentTooShortError("short"),
    ],
    ids=lambda e: type(e).__name__ + getattr(e, "status", "").__str__(),
)
def test_network_errors_are_transient(tmp_path: Path, error: Exception) -> None:
    source, _ = make_source(tmp_path, Script(extract_error=wrap(error)))
    with pytest.raises(TransientError) as caught:
        source.resolve(ID)
    assert caught.value.retry_after_s is None
    assert "://" not in caught.value.message


def test_network_error_in_a_cause_chain(tmp_path: Path) -> None:
    inner = FakeExtractorError("Unable to download", expected=True, cause=FakeTransportError("x"))
    source, _ = make_source(tmp_path, Script(extract_error=wrap(inner)))
    with pytest.raises(TransientError, match="could not reach YouTube"):
        source.resolve(ID)


def test_http_403_names_upgrade(tmp_path: Path) -> None:
    source, _ = make_source(tmp_path, Script(extract_error=wrap(FakeHTTPError(403))))
    with pytest.raises(PermanentError) as caught:
        source.resolve(ID)
    assert caught.value.message == REFUSALS["http_403"]
    assert "section 18" in caught.value.message


def test_http_404_is_permanent(tmp_path: Path) -> None:
    source, _ = make_source(tmp_path, Script(extract_error=wrap(FakeHTTPError(404))))
    with pytest.raises(PermanentError, match="HTTP 404"):
        source.resolve(ID)


def test_extractor_bug_is_out_of_date(tmp_path: Path) -> None:
    bug = wrap(FakeExtractorError("Unable to extract player data; please report this"))
    source, _ = make_source(tmp_path, Script(extract_error=bug))
    with pytest.raises(PermanentError) as caught:
        source.resolve(ID)
    assert caught.value.message == REFUSALS["out_of_date"]
    other, _ = make_source(tmp_path / "k", Script(extract_error=KeyError("formats")))
    with pytest.raises(PermanentError) as caught:
        other.resolve(ID)
    assert caught.value.message == REFUSALS["out_of_date"]


def test_os_error_propagates(tmp_path: Path) -> None:
    """A full disk is not yt-dlp's: it is not mapped, so the runner retries it as transient."""
    plain, _ = make_source(tmp_path / "p", Script(extract_error=PermissionError("denied")))
    with pytest.raises(PermissionError):
        plain.resolve(ID)
    wrapped = wrap(OSError(28, "No space left"))
    source, _ = make_source(tmp_path, Script(extract_error=wrapped))
    with pytest.raises(FakeDownloadError) as caught:
        source.resolve(ID)
    assert caught.value is wrapped
    assert not isinstance(caught.value, EarmarkError)
    assert classify_error(wrapped, FAKE_ERRORS, []) is None


class _Shutdown(BaseException):
    pass


def test_base_exception_propagates(tmp_path: Path) -> None:
    source, fake = make_source(tmp_path, Script(extract_error=_Shutdown()))
    with pytest.raises(_Shutdown):
        source.resolve(ID)
    assert fake.sessions[0].closed
    downloader, fake2 = make_source(tmp_path / "d", playable())
    downloader.resolve(ID)
    fake2.queue(Script(download_error=_Shutdown()))
    with pytest.raises(_Shutdown):
        downloader.download(ID, destination(tmp_path))


@pytest.mark.parametrize(
    "changes",
    [
        {"format_id": None},
        {"format_id": ""},
        {"vcodec": "avc1.640028"},
        {"protocol": "m3u8_native"},
        {"url": None},
        {"ext": "webm/../x"},
        {"ext": ""},
    ],
    ids=str,
)
def test_no_audio_format(tmp_path: Path, changes: dict[str, Any]) -> None:
    source, fake = make_source(tmp_path, playable(**changes))
    source.resolve(ID)
    with pytest.raises(PermanentError) as caught:
        source.download(ID, destination(tmp_path))
    assert caught.value.message == REFUSALS["no_audio"]
    assert not fake.downloads


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/videoplayback?id=x",
        "http://rr1---sn-placeholder.googlevideo.com/videoplayback",
        "https://googlevideo.com.evil.example/videoplayback",
        "https://evilgooglevideo.com/videoplayback",
        "https://user@evil.example/rr.googlevideo.com",
    ],
)
def test_stream_off_host(tmp_path: Path, url: str) -> None:
    source, fake = make_source(tmp_path, playable(url=url))
    source.resolve(ID)
    with pytest.raises(PermanentError) as caught:
        source.download(ID, destination(tmp_path))
    assert caught.value.message == REFUSALS["off_host"]
    assert not fake.downloads


def test_nothing_written(tmp_path: Path) -> None:
    source, _ = make_source(tmp_path, Script(info=public_info(), payload=None))
    source.resolve(ID)
    with pytest.raises(PermanentError) as caught:
        source.download(ID, destination(tmp_path))
    assert caught.value.message == REFUSALS["nothing"]


def test_empty_file_is_transient(tmp_path: Path) -> None:
    source, _ = make_source(tmp_path, Script(info=public_info(), payload=b""))
    source.resolve(ID)
    with pytest.raises(TransientError, match="empty"):
        source.download(ID, destination(tmp_path))


def test_download_error_is_classified(tmp_path: Path) -> None:
    script = Script(info=public_info(), download_error=wrap(FakeHTTPError(403)), payload=None)
    source, _ = make_source(tmp_path, script)
    source.resolve(ID)
    with pytest.raises(PermanentError) as caught:
        source.download(ID, destination(tmp_path))
    assert caught.value.message == REFUSALS["http_403"]


# -- the size cap ----------------------------------------------------------------------


def test_size_cap_declared(tmp_path: Path) -> None:
    source, fake = make_source(
        tmp_path, playable(filesize=11 * MIB, filesize_approx=1), max_bytes=10 * MIB
    )
    source.resolve(ID)
    with pytest.raises(PermanentError, match=r"EARMARK_MAX_AUDIO_MB limit \(10 MiB\)"):
        source.download(ID, destination(tmp_path))
    assert not fake.downloads
    approx, fake2 = make_source(
        tmp_path / "a", playable(filesize=None, filesize_approx=11 * MIB), max_bytes=10 * MIB
    )
    approx.resolve(ID)
    with pytest.raises(PermanentError, match="EARMARK_MAX_AUDIO_MB"):
        approx.download(ID, destination(tmp_path))
    assert not fake2.downloads


def test_size_cap_while_downloading(tmp_path: Path) -> None:
    part = tmp_path / "downloads" / "job.webm.part"

    def write_part(params: dict[str, Any]) -> None:
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"partial")

    script = Script(
        info=public_info(filesize=None, filesize_approx=None),
        progress=[MIB, 6 * MIB, 12 * MIB, 13 * MIB],
        on_download=write_part,
    )
    source, fake = make_source(tmp_path, script, max_bytes=10 * MIB)
    source.resolve(ID)
    with pytest.raises(PermanentError, match="EARMARK_MAX_AUDIO_MB") as caught:
        source.download(ID, destination(tmp_path))
    assert isinstance(caught.value.__cause__, FakeDownloadCancelled)
    assert part.exists()  # left for staging to clear
    assert not (tmp_path / "downloads" / "job.webm").exists()
    assert len(fake.downloads) == 1


def test_size_cap_on_disk(tmp_path: Path) -> None:
    script = Script(info=public_info(filesize=None, filesize_approx=None), payload=b"x" * 2048)
    source, _ = make_source(tmp_path, script, max_bytes=1024)
    source.resolve(ID)
    with pytest.raises(PermanentError, match="EARMARK_MAX_AUDIO_MB"):
        source.download(ID, destination(tmp_path))
    assert not (tmp_path / "downloads" / "job.webm").exists()


def test_deadline_is_transient(tmp_path: Path) -> None:
    clock = Clock()

    def pass_time(params: dict[str, Any]) -> None:
        clock.now += DOWNLOAD_DEADLINE_S + 1

    script = Script(info=public_info(), progress=[1], on_download=pass_time, payload=None)
    source, _ = make_source(tmp_path, script, clock=clock)
    source.resolve(ID)
    with pytest.raises(TransientError, match="longer than 1 hour") as caught:
        source.download(ID, destination(tmp_path))
    assert isinstance(caught.value.__cause__, FakeDownloadCancelled)


# -- live streams ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "key"),
    [
        ({"live_status": "is_live"}, "live"),
        ({"live_status": "is_upcoming"}, "upcoming"),
        ({"live_status": "post_live"}, "post_live"),
    ],
)
def test_live_refused(tmp_path: Path, changes: dict[str, Any], key: str) -> None:
    source, fake = make_source(tmp_path, playable(**changes))
    with pytest.raises(PermanentError) as caught:
        source.resolve(ID)
    assert caught.value.message == REFUSALS[key]
    assert not fake.downloads
    assert not (tmp_path / "downloads").exists()
    with pytest.raises(PermanentError):
        source.download(ID, destination(tmp_path))
    assert not fake.downloads
    assert not (tmp_path / "downloads").exists()


def test_was_live_recording_is_fine(tmp_path: Path) -> None:
    source, _ = make_source(tmp_path, playable(live_status="was_live", was_live=True))
    assert source.resolve(ID).title == "Me at the zoo"


# -- the options -----------------------------------------------------------------------

EXPECTED_KEYS = {
    "quiet",
    "noprogress",
    "color",
    "logger",
    "allowed_extractors",
    "noplaylist",
    "ignore_no_formats_error",
    "format",
    "postprocessors",
    "fixup",
    "cachedir",
    "proxy",
    "socket_timeout",
    "retries",
    "js_runtimes",
    "remote_components",
    "overwrites",
    "continuedl",
    "updatetime",
    "progress_hooks",
}
NEVER_SET = {
    "cookiefile",
    "cookiesfrombrowser",
    "usenetrc",
    "netrc_location",
    "username",
    "password",
    "max_filesize",
    "paths",
    "external_downloader",
    "ffmpeg_location",
    "match_filter",
    "extract_flat",
    "playlist_items",
    "writeinfojson",
    "extractor_args",
}


def _params(tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "deno_path": tmp_path / "bin" / "deno",
        "logger": ytdlp.YtDlpLog(),
        "progress_hook": None,
        "outtmpl": None,
    }
    return ytdlp_params(**(defaults | kwargs))


def test_params_are_exact(tmp_path: Path) -> None:
    log = ytdlp.YtDlpLog()
    params = _params(tmp_path, logger=log)
    assert set(params) == EXPECTED_KEYS
    assert params["quiet"] is True
    assert params["noprogress"] is True
    assert params["color"] == "no_color"
    assert params["logger"] is log
    assert params["format"] == AUDIO_FORMAT
    assert params["format"] == (
        "bestaudio[protocol=https][vcodec=none][audio_channels<=2][abr<=192]"
        "/bestaudio[protocol=https][vcodec=none]"
    )
    assert params["ignore_no_formats_error"] is True
    assert params["socket_timeout"] == 30
    assert params["retries"] == 3
    assert params["overwrites"] is True
    assert params["continuedl"] is False
    assert params["updatetime"] is False
    assert params["progress_hooks"] == []

    def progress(d: dict[str, Any]) -> None:
        del d

    with_hook = _params(tmp_path, progress_hook=progress, outtmpl="/x/y.%(ext)s")
    assert with_hook["progress_hooks"] == [progress]
    assert with_hook["outtmpl"] == {"default": "/x/y.%(ext)s"}
    assert set(with_hook) == EXPECTED_KEYS | {"outtmpl"}


def test_params_youtube_only(tmp_path: Path) -> None:
    params = _params(tmp_path)
    assert params["allowed_extractors"] == ["youtube"]
    assert params["noplaylist"] is True
    source, fake = make_source(tmp_path, playable())
    source.resolve(ID)
    assert all(c.kwargs["ie_key"] == "Youtube" for c in fake.extracts)
    assert fake.extracts[0].params["allowed_extractors"] == ["youtube"]
    assert fake.extracts[0].params["noplaylist"] is True


def test_params_no_cookies_config_plugins(tmp_path: Path) -> None:
    source, fake = make_source(tmp_path, playable())
    source.resolve(ID)
    source.download(ID, destination(tmp_path))
    for session in fake.sessions:
        params = session.params
        assert not NEVER_SET & set(params)
        assert params["cachedir"] is False
        assert params["remote_components"] == []
        assert params["proxy"] == ""


def test_params_no_postprocessing(tmp_path: Path) -> None:
    params = _params(tmp_path)
    assert params["postprocessors"] == []
    assert params["fixup"] == "never"


def test_js_runtime_is_the_venv_deno(tmp_path: Path) -> None:
    deno = tmp_path / "venv" / "bin" / "deno"
    params = _params(tmp_path, deno_path=deno)
    assert params["js_runtimes"] == {"deno": {"path": str(deno)}}
    source, fake = make_source(tmp_path, playable())
    source.resolve(ID)
    assert fake.extracts[0].params["js_runtimes"] == {"deno": {"path": str(fake.runtime.deno_path)}}


# -- untrusted metadata ----------------------------------------------------------------


def test_metadata_sanitised(tmp_path: Path) -> None:
    source, _ = make_source(
        tmp_path,
        playable(
            title="Bad\r\n\ttitle " + "x" * 600,
            channel=["not", "a", "string"],
            uploader="Up\x00loader\nName",
            timestamp=True,
            upload_date="20240229",
            duration=float("nan"),
        ),
    )
    episode = source.resolve(ID)
    assert "\r" not in episode.title
    assert "\n" not in episode.title
    assert "\t" not in episode.title
    assert len(episode.title) == 500
    assert episode.show_title == "Up loader Name"
    assert episode.published_at is not None
    assert episode.published_at.isoformat() == "2024-02-29T00:00:00+00:00"
    assert episode.published_date_only is True
    assert episode.duration_s is None


@pytest.mark.parametrize(
    "changes",
    [
        {"timestamp": -5, "upload_date": "bad"},
        {"timestamp": 4102444801, "upload_date": "20240230"},
        {"timestamp": "1114313512", "upload_date": None},
        {"timestamp": float("inf"), "upload_date": 20050424},
    ],
)
def test_metadata_without_a_date(tmp_path: Path, changes: dict[str, Any]) -> None:
    source, _ = make_source(tmp_path, playable(**changes))
    episode = source.resolve(ID)
    assert episode.published_at is None
    assert episode.published_date_only is False


@pytest.mark.parametrize("duration", ["19", True, -1, None, math.inf])
def test_metadata_bad_duration(tmp_path: Path, duration: object) -> None:
    source, _ = make_source(tmp_path, playable(duration=duration))
    assert source.resolve(ID).duration_s is None


def test_metadata_missing_fields(tmp_path: Path) -> None:
    info = public_info()
    for key in ("title", "channel", "uploader", "timestamp", "upload_date", "duration"):
        info.pop(key)
    source, _ = make_source(tmp_path, Script(info=info))
    episode = source.resolve(ID)
    assert (episode.title, episode.show_title) == ("", "")
    assert (episode.published_at, episode.duration_s) == (None, None)


def test_non_mapping_info_is_not_a_video(tmp_path: Path) -> None:
    source, _ = make_source(tmp_path, Script(info=None))
    with pytest.raises(PermanentError) as caught:
        source.resolve(ID)
    assert caught.value.message == REFUSALS["not_video"]


def test_resolve_rejects_a_bad_id(tmp_path: Path) -> None:
    source, fake = make_source(tmp_path, playable())
    with pytest.raises(PermanentError, match="video id"):
        source.resolve("not an id")
    assert not fake.sessions


def test_messages_carry_no_url_or_ytdlp_text(tmp_path: Path) -> None:
    texts = [REFUSALS[k] for k in REFUSALS]
    for name in ("private.json", "removed.json", "tos_removed.json", "post_live.json"):
        info, warnings = refusal_info(name)
        source, _ = make_source(tmp_path / name, Script(info=info, warnings=warnings))
        with pytest.raises(PermanentError) as caught:
            source.resolve(str(info["id"]))
        texts.append(caught.value.message)
        for warning in warnings:
            assert warning not in caught.value.message
    for error in (FakeHTTPError(503), FakeTransportError("https://x.example/y"), KeyError("k")):
        source, _ = make_source(tmp_path / type(error).__name__, Script(extract_error=wrap(error)))
        with pytest.raises((PermanentError, TransientError)) as caught2:
            source.resolve(ID)
        texts.append(caught2.value.message)
    for text in texts:
        assert "http" not in text.lower().replace("(http 403)", "").replace("http 5", "")
        assert ID not in text
        assert text.isascii()


# -- the environment and the log -------------------------------------------------------


def test_deno_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DENO_DIR", raising=False)
    monkeypatch.delenv("DENO_NO_UPDATE_CHECK", raising=False)
    seen: dict[str, str | None] = {}

    def record(params: dict[str, Any]) -> None:
        seen["dir"] = os.environ.get("DENO_DIR")
        seen["check"] = os.environ.get("DENO_NO_UPDATE_CHECK")

    script = Script(info=public_info(), on_download=record)
    source, _ = make_source(tmp_path, script)
    source.resolve(ID)
    source.download(ID, destination(tmp_path))
    assert seen == {"dir": str(tmp_path / "data" / "cache" / "deno"), "check": "1"}
    assert "DENO_DIR" not in os.environ
    assert "DENO_NO_UPDATE_CHECK" not in os.environ

    monkeypatch.setenv("DENO_DIR", "/operator/choice")
    monkeypatch.setenv("DENO_NO_UPDATE_CHECK", "0")
    with deno_environment(tmp_path / "cache"):
        assert os.environ["DENO_DIR"] == "/operator/choice"
        assert os.environ["DENO_NO_UPDATE_CHECK"] == "1"
    assert os.environ["DENO_DIR"] == "/operator/choice"
    assert os.environ["DENO_NO_UPDATE_CHECK"] == "0"


def test_deno_environment_restored_after_an_error(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), deno_environment(tmp_path):
        raise RuntimeError("boom")
    assert "DENO_DIR" not in os.environ


@pytest.mark.usefixtures("restore_root_logging", "restore_library_loggers")
def test_ytdlp_debug_never_printed(tmp_path: Path) -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    googlevideo = "https://rr1---sn-placeholder.googlevideo.com/videoplayback?ip=192.0.2.7&sig=s"
    script = Script(
        info=public_info(),
        warnings=[f"Skipped formats from {googlevideo}"],
    )
    source, _ = make_source(tmp_path, script)
    log = ytdlp.YtDlpLog()
    debug_line = f"[debug] Invoking http downloader on {googlevideo}"
    info_line = f"[info] Downloading {googlevideo}"
    log.debug(debug_line)
    log.info(info_line)
    source.resolve(ID)
    out = stream.getvalue()
    assert "Invoking http downloader" not in out
    assert "[info] Downloading" not in out
    assert "Skipped formats" in out
    assert "192.0.2.7" not in out
    assert "sig=s" not in out
    assert "googlevideo.com" in out


def test_warnings_are_kept_and_capped() -> None:
    log = ytdlp.YtDlpLog()
    for n in range(40):
        text = f"warning {n} " + "x" * 800
        log.warning(text)
    assert len(log.warnings) == ytdlp.MAX_WARNINGS
    assert all(len(w) <= 500 for w in log.warnings)
    log.error("an error")
    assert len(log.warnings) == ytdlp.MAX_WARNINGS


# -- laziness and the extra ------------------------------------------------------------


def test_import_is_light() -> None:
    code = (
        "import sys\n"
        "import earmark.youtube.ytdlp, earmark.pipeline.factory, earmark.sources\n"
        "bad = [m for m in sys.modules if m.split('.')[0] in ('yt_dlp', 'yt_dlp_ejs')]\n"
        "assert not bad, bad\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_missing_extra_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "yt_dlp", None)
    with pytest.raises(MissingYoutubeExtraError) as caught:
        load_runtime()
    assert isinstance(caught.value, ConfigError)
    text = str(caught.value)
    assert "EARMARK_YOUTUBE_BACKEND=ytdlp" in text
    assert "'youtube' extra" in text
    assert "uv sync --extra youtube" in text


def test_missing_deno_is_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Stub:
        pass

    stub_modules: dict[str, Any] = {}
    for name in (
        "yt_dlp",
        "yt_dlp.utils",
        "yt_dlp.networking",
        "yt_dlp.networking.exceptions",
        "yt_dlp.globals",
        "yt_dlp.version",
        "yt_dlp_ejs",
    ):
        stub_modules[name] = Stub()
    stub_modules["yt_dlp.globals"].plugin_dirs = Stub()
    for name, module in stub_modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr("sysconfig.get_path", lambda name, *a, **k: str(tmp_path / "empty"))
    with pytest.raises(MissingYoutubeExtraError) as caught:
        load_runtime()
    text = str(caught.value)
    assert "Deno" in text
    assert "uv sync --extra youtube" in text
    assert str(tmp_path) not in text


def test_build_makes_no_request_and_sets_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_runtime(tmp_path)
    monkeypatch.setattr(ytdlp, "load_runtime", lambda: fake.runtime)
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", max_audio_mb=7)
    source = build_ytdlp_source(settings)
    assert source.runtime is fake.runtime
    assert not fake.sessions
    assert source._max_bytes == 7 * MIB  # the cap is the setting, in bytes
    assert source._deno_dir == (tmp_path / "data").resolve() / "cache" / "deno"


# -- classification ---------------------------------------------------------------------


def test_reason_from_text_first_match_wins() -> None:
    assert reason_from_text("Private video. Join this channel") == "private"
    assert reason_from_text("") == "refused"
    assert (
        reason_from_text("SIGN IN to confirm you\N{RIGHT SINGLE QUOTATION MARK}re not a bot")
        == "bot"
    )
    assert reason_from_text("Sign in") == "sign_in"


def test_classify_error_without_inner_exception() -> None:
    plain = FAKE_ERRORS.download_error("ERROR: Private video")
    mapped = classify_error(plain, FAKE_ERRORS, [])
    assert isinstance(mapped, PermanentError)
    assert mapped.message == REFUSALS["private"]


def test_classify_error_cause_cycle_terminates() -> None:
    a = FakeExtractorError("a", expected=True)
    b = FakeExtractorError("b", expected=True, cause=a)
    a.cause = b
    mapped = classify_error(wrap(a), FAKE_ERRORS, [])
    assert isinstance(mapped, PermanentError)
