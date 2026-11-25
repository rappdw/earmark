"""The real backend against YouTube. Opt-in: ``YOUTUBE_TEST=1 pytest -m network`` with the extra.

CI never sets ``YOUTUBE_TEST``: GitHub's runners get bot checks. These tests download
one 19-second public video; run them from the Spark to check its line to YouTube.
"""

import os
from pathlib import Path

import pytest

from earmark.config import Settings
from earmark.errors import PermanentError
from earmark.youtube.links import youtube_episode
from earmark.youtube.ytdlp import REFUSALS, YtDlpYoutubeSource, build_ytdlp_source

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(os.environ.get("YOUTUBE_TEST") != "1", reason="set YOUTUBE_TEST=1"),
]

ID = "jNQXAC9IVRw"


def _source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> YtDlpYoutubeSource:
    monkeypatch.delenv("DENO_DIR", raising=False)
    return build_ytdlp_source(Settings(_env_file=None, data_dir=tmp_path / "data"))


def test_live_resolve_and_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source(tmp_path, monkeypatch)
    episode = source.resolve(ID)
    assert episode == youtube_episode(
        ID,
        title="Me at the zoo",
        channel="jawed",
        published_at=episode.published_at,
        published_date_only=episode.published_date_only,
        duration_s=19.0,
    )
    assert episode.published_at is not None
    assert episode.published_at.date().isoformat() == "2005-04-24"
    downloads = tmp_path / "downloads"
    path = source.download(ID, lambda ext: downloads / f"job.{ext}")
    assert path.suffix in (".webm", ".m4a")
    assert path.stat().st_size > 100 * 1024
    assert [p.name for p in downloads.iterdir()] == [path.name]
    assert (tmp_path / "data" / "cache" / "deno").is_dir()


def test_live_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source(tmp_path, monkeypatch)
    with pytest.raises(PermanentError) as caught:
        source.resolve("aaaaaaaaaaa")
    assert caught.value.message == REFUSALS["removed"]
