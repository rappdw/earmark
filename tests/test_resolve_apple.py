import subprocess
import sys

import pytest

from earmark.errors import PermanentError
from earmark.models import MAX_URL_CHARS, EpisodeRef
from earmark.resolve.apple import (
    MESSAGES,
    UnsupportedUrlError,
    UrlProblem,
    canonical_episode_url,
    parse_apple_url,
)

SHOW = "1200361736"
EP = "1000802907981"
BASE = f"https://podcasts.apple.com/us/podcast/id{SHOW}?i={EP}"
ARABIC = "\N{ARABIC-INDIC DIGIT ONE}\N{ARABIC-INDIC DIGIT TWO}\N{ARABIC-INDIC DIGIT THREE}"
GB = f"https://podcasts.apple.com/gb/podcast/id{SHOW}?i={EP}"


def _ref(country: str = "us", show: str = SHOW, ep: str = EP) -> EpisodeRef:
    return EpisodeRef(
        source="apple_podcasts",
        show_id=show,
        episode_id=ep,
        country=country,
        url=canonical_episode_url(country, show, ep),
    )


SLUG = "did-a-i-break-the-internet-and-other-things"
CASES: list[tuple[str, EpisodeRef]] = [
    (f"https://podcasts.apple.com/us/podcast/{SLUG}/id{SHOW}?i={EP}&uo=4", _ref()),
    (f"https://podcasts.apple.com/gb/podcast/{SLUG}/id{SHOW}?i={EP}", _ref("gb")),
    (BASE, _ref()),
    (f"https://podcasts.apple.com/podcast/id{SHOW}?i={EP}", _ref()),
    (f"https://podcasts.apple.com/US/podcast/{SLUG}/id{SHOW}?i={EP}", _ref()),
    (
        f"https://podcasts.apple.com/us/podcast/x/id{SHOW}?i={EP}&uo=4&mt=2&at=x&ct=y&l=es&r=754",
        _ref(),
    ),
    (f"https://podcasts.apple.com/us/podcast/x/id{SHOW}?uo=4&mt=2&i={EP}&l=es&r=754", _ref()),
    (f"https://podcasts.apple.com/us/podcast/x/id{SHOW}?uo=4&mt=2&at=x&ct=y&r=754&i={EP}", _ref()),
    (f"https://itunes.apple.com/us/podcast/the-daily/id{SHOW}?i={EP}&mt=2", _ref()),
    (f"http://podcasts.apple.com/us/podcast/id{SHOW}?i={EP}", _ref()),
    (f"https://Podcasts.Apple.COM/us/podcast/id{SHOW}?i={EP}", _ref()),
    (f"https://podcasts.apple.com./us/podcast/id{SHOW}?i={EP}", _ref()),
    (f"https://podcasts.apple.com/us/podcast/x/id{SHOW}/?i={EP}", _ref()),
    (f"https://podcasts.apple.com//us//podcast/x//id{SHOW}?i={EP}", _ref()),
    (f"https://podcasts.apple.com/us/podcast/id{SHOW}?i={EP}#frag", _ref()),
    (f"podcasts.apple.com/us/podcast/id{SHOW}?i={EP}", _ref()),
    (f"https://podcasts.apple.com/fr/podcast/l%C3%A9o-caf%C3%A9/id{SHOW}?i={EP}", _ref("fr")),
    (f"https://podcasts.apple.com/fr/podcast/léo-café/id{SHOW}?i={EP}", _ref("fr")),
    (f"https://podcasts.apple.com/us/podcast/x/ID{SHOW}?i={EP}", _ref()),
    (f"https://podcasts.apple.com/us/podcast/id000{SHOW}?i=000{EP}", _ref()),
    (f"https://podcasts.apple.com/us/podcast/id{SHOW}?i={EP}&i={EP}", _ref()),
    (f"  {BASE}\n", _ref()),
]


@pytest.mark.parametrize(("url", "expected"), CASES)
def test_parses_episode_urls(url: str, expected: EpisodeRef) -> None:
    assert parse_apple_url(url) == expected


@pytest.mark.parametrize(("url", "_"), CASES)
def test_canonical_is_fixed_point(url: str, _: EpisodeRef) -> None:
    ref = parse_apple_url(url)
    assert parse_apple_url(ref.url) == ref


def test_canonical_episode_url() -> None:
    assert canonical_episode_url("gb", "12", "34") == (
        "https://podcasts.apple.com/gb/podcast/id12?i=34"
    )
    assert canonical_episode_url("gb", SHOW, EP) == GB


def _problem(url: str) -> UrlProblem:
    with pytest.raises(UnsupportedUrlError) as exc:
        parse_apple_url(url)
    return exc.value.problem


@pytest.mark.parametrize(
    "url",
    [
        f"https://open.spotify.com/episode/abc?i={EP}",
        f"https://apple.com/us/podcast/id{SHOW}?i={EP}",
        f"https://music.apple.com/us/podcast/id{SHOW}?i={EP}",
        f"https://embed.podcasts.apple.com/us/podcast/id{SHOW}?i={EP}",
        f"https://podcasts.apple.com.evil.example/us/podcast/id{SHOW}?i={EP}",
        f"https://evilpodcasts.apple.com/us/podcast/id{SHOW}?i={EP}",
        "localhost",
    ],
)
def test_rejects_hosts(url: str) -> None:
    assert _problem(url) == "unsupported_host"


@pytest.mark.parametrize("query", ["", "?i=", "?uo=4", "?i=&uo=4"])
def test_rejects_show_links(query: str) -> None:
    assert _problem(f"https://podcasts.apple.com/us/podcast/the-daily/id{SHOW}{query}") == (
        "show_link"
    )


@pytest.mark.parametrize(
    "path",
    ["/us/channel/x/id1", "/us/genre/x/id26", "/us/podcast/slug", "/", "/us/podcast/a/b/id1"],
)
def test_rejects_non_episode_paths(path: str) -> None:
    assert _problem(f"https://podcasts.apple.com{path}?i=2") == "not_episode"
    assert _problem(f"https://podcasts.apple.com{path}") == "not_episode"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "not a url",
        f"ftp://podcasts.apple.com/us/podcast/id{SHOW}?i={EP}",
        f"https://u:p@podcasts.apple.com/us/podcast/id{SHOW}?i={EP}",
        f"https://podcasts.apple.com:8443/us/podcast/id{SHOW}?i={EP}",
        f"https://podcasts.apple.com/us/podcast/id{SHOW}?i=abc",
        f"https://podcasts.apple.com/us/podcast/id{SHOW}?i=1&i=2",
        f"https://podcasts.apple.com/us/podcast/id{SHOW}?i=0",
        f"https://podcasts.apple.com/us/podcast/id0?i={EP}",
        f"https://podcasts.apple.com/us/podcast/id{ARABIC}?i={EP}",
        f"https://podcasts.apple.com/us/podcast/id{SHOW}?i={ARABIC}",
        f"https://podcasts.apple.com/us/podcast/id{'1' * 21}?i={EP}",
        f"https://podcasts.apple.com/us/podcast/id{SHOW}?i={'1' * 21}",
        f"https://podcasts.apple.com/us/podcast/id{SHOW} ?i={EP}",
        f"https://podcasts.apple.com/us/podcast/id{SHOW}?i={EP}\tx",
        f"https://podcasts.apple.com/us/podcast/id{SHOW}?i={EP}\x00",
        "https://podcasts.apple.com/" + "a" * MAX_URL_CHARS,
        "https://[::1",
    ],
)
def test_rejects_malformed(url: str) -> None:
    assert _problem(url) == "malformed"


def test_host_checked_before_path_before_query() -> None:
    assert _problem("https://open.spotify.com/") == "unsupported_host"
    assert _problem("https://podcasts.apple.com/us/channel/x/id1?i=abc") == "not_episode"


def test_messages_short_and_never_echo_input() -> None:
    assert all(len(m) <= 100 for m in MESSAGES.values())
    for url in (
        "sentinel-9c2e",
        "https://sentinel-9c2e.example/x",
        f"https://podcasts.apple.com/us/podcast/sentinel-9c2e/id{SHOW}",
        f"https://podcasts.apple.com/us/podcast/id{SHOW}?i=sentinel-9c2e",
    ):
        with pytest.raises(UnsupportedUrlError) as exc:
            parse_apple_url(url)
        assert "sentinel-9c2e" not in exc.value.message
        assert exc.value.message == MESSAGES[exc.value.problem]
        assert isinstance(exc.value, PermanentError)


def test_apple_module_imports_light() -> None:
    code = (
        "import sys, earmark.resolve.apple, earmark.sources, earmark.staging, earmark.recipients\n"
        "heavy = {'fastapi', 'starlette', 'uvicorn', 'httpx2'}\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in heavy))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "[]"
