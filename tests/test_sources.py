from pathlib import Path

import pytest

from earmark.models import AUDIO_SHOW_ID, Job, content_key, episode_key
from earmark.resolve.apple import MESSAGES, UnsupportedUrlError, UrlProblem
from earmark.sources import (
    SourceUrl,
    audio_episode,
    audio_title,
    clean_upload_name,
    display_url,
    parse_source_url,
    sha256_file,
    upload_extension,
    url_host,
)
from earmark.transcribe.faster_whisper import file_key
from tests.factories import SHA, T0, TO

APPLE = "https://podcasts.apple.com/us/podcast/id1200361736?i=1000802907981"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (APPLE, SourceUrl("apple_podcasts", APPLE)),
        (
            "  https://podcasts.apple.com/us/podcast/slug/id1200361736?i=1000802907981&uo=4 ",
            SourceUrl("apple_podcasts", APPLE),
        ),
        (
            "podcasts.apple.com/us/podcast/id1200361736?i=1000802907981",
            SourceUrl("apple_podcasts", APPLE),
        ),
        (
            "https://itunes.apple.com/GB/podcast/x/id1200361736?i=1000802907981",
            SourceUrl(
                "apple_podcasts",
                "https://podcasts.apple.com/gb/podcast/id1200361736?i=1000802907981",
            ),
        ),
        (
            "https://audio.example.net/p/clip.mp3?sig=abc#frag",
            SourceUrl("url", "https://audio.example.net/p/clip.mp3?sig=abc"),
        ),
        (
            "HTTP://Audio.Example.net:8080/a.mp3",
            SourceUrl("url", "http://Audio.Example.net:8080/a.mp3"),
        ),
        (
            "https://cdn.example.org/podcast.apple.com/a.mp3",
            SourceUrl("url", "https://cdn.example.org/podcast.apple.com/a.mp3"),
        ),
    ],
)
def test_parse_source_url_cases(text: str, expected: SourceUrl) -> None:
    assert parse_source_url(text) == expected


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("", "malformed"),
        ("   ", "malformed"),
        ("not a url", "malformed"),
        ("audio.example.net/a.mp3", "malformed"),  # no scheme: only an Apple link may omit it
        ("ftp://audio.example.net/a.mp3", "malformed"),
        ("file:///etc/passwd", "malformed"),
        ("javascript:alert(1)", "malformed"),
        ("https://", "malformed"),
        ("https://audio.example.net:notaport/a.mp3", "malformed"),
        ("https://audio.example.net/a b.mp3", "malformed"),
        ("https://audio.example.net/a\x7f.mp3", "malformed"),
        ("https://audio.example.net/" + "a" * 2100, "malformed"),
        ("https://podcasts.apple.com/us/podcast/id1200361736", "show_link"),
        ("https://podcasts.apple.com/us/channel/x/id1?i=2", "not_episode"),
        ("https://music.apple.com/us/album/x/1", "not_episode"),
        ("https://embed.podcasts.apple.com/us/podcast/x/id1?i=2", "not_episode"),
        ("https://apple.com/a.mp3", "not_episode"),
        ("https://user:pw@cdn.example/a.mp3", "credentials"),
        ("https://token@cdn.example/a.mp3", "credentials"),
    ],
)
def test_parse_source_url_refusals(text: str, problem: UrlProblem) -> None:
    with pytest.raises(UnsupportedUrlError) as info:
        parse_source_url(text)
    assert info.value.problem == problem
    assert info.value.message == MESSAGES[problem]
    assert "pw" not in info.value.message


@pytest.mark.parametrize(
    "text",
    [
        "https://youtu.be/jNQXAC9IVRw?si=abc",
        "youtu.be/jNQXAC9IVRw",
        "http://www.youtube.com/watch?v=jNQXAC9IVRw&t=5s",
        "https://m.youtube.com/shorts/jNQXAC9IVRw",
    ],
)
def test_parse_source_url_youtube(text: str) -> None:
    assert parse_source_url(text) == SourceUrl(
        "youtube", "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (APPLE, SourceUrl("apple_podcasts", APPLE)),
        ("https://audio.example.net/a.mp3", SourceUrl("url", "https://audio.example.net/a.mp3")),
        ("https://vimeo.com/1", SourceUrl("url", "https://vimeo.com/1")),
        (
            "https://notyoutube.com/watch?v=jNQXAC9IVRw",
            SourceUrl("url", "https://notyoutube.com/watch?v=jNQXAC9IVRw"),
        ),
        (
            "https://example.org/youtube.com/watch?v=jNQXAC9IVRw",
            SourceUrl("url", "https://example.org/youtube.com/watch?v=jNQXAC9IVRw"),
        ),
    ],
)
def test_non_youtube_urls_unchanged(text: str, expected: SourceUrl) -> None:
    assert parse_source_url(text) == expected


def test_credentials_message() -> None:
    assert MESSAGES["credentials"] == "Links with a user name or password are not supported."


def test_display_url_keeps_scheme_host_port_path_only() -> None:
    url = "https://Audio.Example.NET:8443/private/PATH-TOKEN.mp3?sig=QUERY-TOKEN#frag"
    assert display_url(url) == "https://audio.example.net:8443/private/PATH-TOKEN.mp3"
    assert display_url("http://[::1]:80/a") == "http://[::1]:80/a"
    assert display_url("https://example.org") == "https://example.org"
    assert "user" not in display_url("https://user:pw@example.org/x")
    assert url_host(url) == "audio.example.net:8443"


@pytest.mark.parametrize(
    ("filename", "content_type", "expected"),
    [
        ("clip.m4a", "application/octet-stream", "m4a"),
        ("CLIP.MP3", None, "mp3"),
        (None, "audio/mpeg", "mp3"),
        (None, "audio/mpeg; charset=binary", "mp3"),
        ("noext", "audio/x-m4a", "m4a"),
        ("blob", "audio/x-unknown", "bin"),
        ("notes.txt", "text/plain", None),
        ("clip.mp3", "text/plain", "mp3"),
        ("clip.exe", "application/octet-stream", None),
        (None, None, None),
        ("", "", None),
        ("archive.tar.gz", None, None),
        ("clip.mp3.exe", None, None),
    ],
)
def test_upload_extension(
    filename: str | None, content_type: str | None, expected: str | None
) -> None:
    assert upload_extension(filename, content_type) == expected


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("clip.mp3", "clip.mp3"),
        ("C:\\Users\\x\\clip.mp3", "clip.mp3"),
        ("../../etc/passwd.mp3", "passwd.mp3"),
        ("a/b/", None),
        ("  two   spaces.mp3 ", "two spaces.mp3"),
        ("ctl\x00\x07chars.mp3", "ctlchars.mp3"),
        ("rtl\u202eevil.mp3", "rtlevil.mp3"),
        ("line\u2028sep.mp3", "line sep.mp3"),
        ("zwj\u200dok\u200c.mp3", "zwj\u200dok\u200c.mp3"),
        ("e\u0301.mp3", "é.mp3"),
        ("", None),
        (None, None),
        ("\x00\x01", None),
        ("x" * 400, "x" * 255),
    ],
)
def test_clean_upload_name(raw: str | None, cleaned: str | None) -> None:
    assert clean_upload_name(raw) == cleaned


def _job(**kw: object) -> Job:
    base: dict[str, object] = {
        "id": "a" * 32,
        "kind": "ingest",
        "status": "queued",
        "source": "url",
        "url": "https://audio.example.net/shows/My%20Clip.mp3?sig=Q",
        "to": TO,
        "run_after": T0,
        "shared_at": T0,
        "created_at": T0,
        "updated_at": T0,
    }
    return Job.model_validate(base | kw)


def _upload(**kw: object) -> Job:
    return _job(
        source="upload",
        url=None,
        upload_file="b" * 32 + ".mp3",
        content_sha256=SHA,
        upload_name="memo.final.m4a",
        **kw,
    )


def test_audio_title_precedence() -> None:
    assert audio_title(_job(title="  My title ")) == "My title"
    assert audio_title(_job()) == "My Clip.mp3"
    assert audio_title(_job(url="https://audio.example.net/")) == "audio.example.net"
    assert audio_title(_job(url="https://audio.example.net:9000")) == "audio.example.net:9000"
    assert audio_title(_upload(title="Named")) == "Named"
    assert audio_title(_upload()) == "memo.final"
    assert audio_title(_upload().model_copy(update={"upload_name": None})) == "Uploaded audio"
    assert audio_title(_upload().model_copy(update={"upload_name": "\x07"})) == "Uploaded audio"


def test_audio_title_is_cleaned_and_capped() -> None:
    assert audio_title(_job(title="a\u202eb\nc")) == "ab c"
    assert len(audio_title(_job(title="t" * 500))) == 200
    assert audio_title(_job(title="   ", url="https://audio.example.net/x.mp3")) == "x.mp3"


def test_audio_episode_fields() -> None:
    episode = audio_episode(_upload(title="T"), SHA)
    assert episode.key == content_key(SHA) == episode_key("local", "file", SHA)
    assert (episode.source, episode.show_id, episode.guid) == (
        "upload",
        AUDIO_SHOW_ID,
        f"sha256:{SHA}",
    )
    assert (episode.title, episode.show_title, episode.episode_id) == ("T", "", "")
    assert (episode.published_at, episode.duration_s) == (None, None)
    assert (episode.audio_url, episode.feed_url, episode.url, episode.description) == (
        "",
        None,
        "",
        "",
    )
    assert audio_episode(_job(), SHA).source == "url"


def test_sha256_file_and_content_key_match_file_key(tmp_path: Path) -> None:
    path = tmp_path / "clip.mp3"
    path.write_bytes(b"ID3" + bytes(3 * 1024 * 1024 + 5))
    digest = sha256_file(path)
    assert len(digest) == 64
    assert content_key(digest) == file_key(path)
    other = tmp_path / "other.mp3"
    other.write_bytes(b"different")
    assert file_key(other) != file_key(path)
