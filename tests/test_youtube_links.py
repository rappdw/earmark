"""YouTube link forms, refusals and canonicalisation, and the one identity of a video."""

import urllib.parse

import pytest

from earmark.errors import PermanentError
from earmark.models import youtube_key
from earmark.resolve.apple import MESSAGES, UnsupportedUrlError
from earmark.sources import SourceUrl, parse_source_url
from earmark.youtube.links import (
    canonical_youtube_url,
    is_video_id,
    is_youtube_host,
    short_youtube_url,
    video_id_from_canonical,
    video_id_of,
)

ID = "jNQXAC9IVRw"
CANONICAL = f"https://www.youtube.com/watch?v={ID}"

FORMS = [
    f"https://youtu.be/{ID}",
    f"https://youtu.be/{ID}?si=abc&t=30",
    f"youtu.be/{ID}",
    f"http://youtu.be/{ID}/",
    f"https://www.youtube.com/watch?v={ID}",
    f"https://youtube.com/watch?v={ID}&list=PLx&index=2",
    f"https://m.youtube.com/watch?v={ID}&feature=share",
    f"https://music.youtube.com/watch?v={ID}",
    f"https://www.youtube.com/shorts/{ID}?feature=share",
    f"https://www.youtube.com/live/{ID}?si=x",
    f"https://www.youtube.com/embed/{ID}",
    f"https://www.youtube.com/v/{ID}",
    f"https://www.youtube-nocookie.com/embed/{ID}",
    f"https://WWW.YouTube.com/Watch?v={ID}#t=1",
    f"www.youtube.com/watch?v={ID}",
    f"https://www.youtube.com/watch?v={ID}&v={ID}",
]


@pytest.mark.parametrize("link", FORMS)
def test_video_link_forms(link: str) -> None:
    parsed = parse_source_url(link)
    assert parsed == SourceUrl("youtube", CANONICAL)


def test_ids_keep_case_and_punctuation() -> None:
    for video_id in ("ex_ample-02", "Ab-_Cd12345", "AAAAAAAAAAA"):
        assert parse_source_url(f"https://youtu.be/{video_id}").url == canonical_youtube_url(
            video_id
        )


@pytest.mark.parametrize("link", FORMS)
def test_canonical_drops_parameters(link: str) -> None:
    url = parse_source_url(link).url
    parts = urllib.parse.urlsplit(url)
    assert url == CANONICAL
    assert parts.query == f"v={ID}"
    assert not parts.fragment


@pytest.mark.parametrize(
    "link",
    [
        "https://www.youtube.com/@name",
        "https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx",
        "https://www.youtube.com/c/name",
        "https://www.youtube.com/user/name",
        "https://www.youtube.com/playlist?list=PLxxxxxxxxxxxx",
        "https://www.youtube.com/results?search_query=x",
        "https://www.youtube.com/",
        "https://www.youtube.com/feed/subscriptions",
        f"https://studio.youtube.com/video/{ID}",
        "https://www.youtube.com/embed/videoseries?list=PLxxxxxxxxxxxx",
        "https://www.youtube.com/embed/live_stream?channel=UCxxxxxxxxxxxxxxxxxxxxxx",
        f"https://www.youtube.com/shorts/{ID}/extra",
        "https://youtube.com",
    ],
)
def test_refused_not_video(link: str) -> None:
    with pytest.raises(UnsupportedUrlError) as info:
        parse_source_url(link)
    assert info.value.problem == "youtube_not_video"
    assert info.value.message == MESSAGES["youtube_not_video"]
    assert "channel, playlist or search" in info.value.message


@pytest.mark.parametrize(
    "link",
    [
        "https://youtu.be/",
        "youtu.be/short",
        "https://www.youtube.com/watch",
        "https://www.youtube.com/watch?v=",
        f"https://www.youtube.com/watch?v={ID}&v=exampleVid1",
        f"https://youtu.be/{ID}x",
        "https://www.youtube.com/shorts/",
        "https://www.youtube.com/shorts/abc.defghij",
        "https://youtu.be/has.a.dot1",
        f"https://youtu.be/{ID}/more",
    ],
)
def test_refused_bad_id(link: str) -> None:
    with pytest.raises(UnsupportedUrlError) as info:
        parse_source_url(link)
    assert info.value.problem == "youtube_bad_id"
    assert info.value.message == "This YouTube link has no valid video id."


def test_credentials_and_port_refused() -> None:
    with pytest.raises(UnsupportedUrlError) as info:
        parse_source_url(f"https://user:secret@youtu.be/{ID}")
    assert info.value.problem == "credentials"
    with pytest.raises(UnsupportedUrlError) as info:
        parse_source_url(f"user:secret@youtu.be/{ID}")
    assert info.value.problem == "credentials"
    with pytest.raises(UnsupportedUrlError) as info:
        parse_source_url(f"https://youtu.be:8443/{ID}")
    assert info.value.problem == "malformed"


def test_two_forms_one_key() -> None:
    a = parse_source_url(f"youtu.be/{ID}?si=x")
    b = parse_source_url(f"https://www.youtube.com/shorts/{ID}")
    assert a == b
    assert youtube_key(video_id_from_canonical(a.url)) == youtube_key(
        video_id_from_canonical(b.url)
    )
    other = parse_source_url("https://youtu.be/ex_ample-02")
    assert youtube_key(video_id_from_canonical(other.url)) != youtube_key(ID)


def test_video_id_from_canonical() -> None:
    for video_id in (ID, "ex_ample-02"):
        assert video_id_from_canonical(canonical_youtube_url(video_id)) == video_id
    for bad in (
        f"https://youtu.be/{ID}",
        CANONICAL + "&t=1",
        CANONICAL + "x",
        f"http://www.youtube.com/watch?v={ID}",
        "",
        "https://www.youtube.com/watch?v=short",
    ):
        with pytest.raises(PermanentError):
            video_id_from_canonical(bad)


def test_short_url_and_ids() -> None:
    assert short_youtube_url(ID) == f"https://youtu.be/{ID}"
    assert is_video_id(ID)
    assert not is_video_id(ID + "x")
    assert not is_video_id("")
    assert not is_video_id("has space!!")


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("youtu.be", True),
        ("youtube.com", True),
        ("www.youtube.com", True),
        ("studio.youtube.com", True),
        ("youtube-nocookie.com", True),
        ("YouTube.com.", True),
        ("notyoutube.com", False),
        ("youtube.com.evil.example", False),
        ("youtu.be.example.org", False),
        ("example.com", False),
    ],
)
def test_is_youtube_host(host: str, expected: bool) -> None:
    assert is_youtube_host(host) is expected


def test_video_id_of_takes_split_results() -> None:
    parts = urllib.parse.urlsplit(f"https://youtu.be/{ID}?si=x")
    assert video_id_of(parts) == ID


def test_non_youtube_urls_reach_other_branches() -> None:
    assert parse_source_url("https://notyoutube.com/watch?v=" + ID) == SourceUrl(
        "url", "https://notyoutube.com/watch?v=" + ID
    )
    assert parse_source_url("https://vimeo.com/1").source == "url"
