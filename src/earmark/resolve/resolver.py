"""The ``apple`` Resolver: Apple Podcasts URL -> iTunes Lookup (or episode page) -> RSS feed.

The only resolve module that imports ``earmark.fetch``: every request goes through the
injected ``GuardedFetcher``. Messages and logs never carry URL paths, queries, titles, or
descriptions, which are untrusted.
"""

import logging
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit

from earmark.config import Settings
from earmark.errors import PermanentError, SkipEpisode, TransientError
from earmark.fetch import MIB, GuardedFetcher, PermanentFetchError, guarded_fetcher
from earmark.models import Episode, EpisodeRef, episode_key
from earmark.resolve.apple import parse_apple_url
from earmark.resolve.apple_page import parse_episode_page
from earmark.resolve.itunes import (
    LOOKUP_URL,
    AppleEpisode,
    AppleShow,
    lookup_params,
    parse_lookup,
    valid_guid,
)
from earmark.resolve.rss import Feed, FeedItem, MatchTier, match_item, parse_feed
from earmark.resolve.text import clean_text, html_to_text

logger = logging.getLogger(__name__)

type ResolveSource = Literal["lookup", "page"]

PAGE_PURPOSE: Final = "episode page"
MAX_TITLE_CHARS: Final = 500
MAX_DESCRIPTION_CHARS: Final = 20_000

NO_FEED_REASON: Final = (
    "This show has no public RSS feed. It may be for subscribers only "
    "or exclusive to Apple Podcasts."
)
NO_AUDIO_REASON: Final = (
    "This episode has no public audio in the show's feed. It may be for subscribers "
    "only or exclusive to Apple Podcasts."
)
UNKNOWN_SHOW: Final = "Apple Podcasts has no show {show_id} in the '{country}' store."
BAD_LOOKUP: Final = "Apple's lookup rejected the request for the '{country}' store (HTTP 400)."
LOOKUP_REFUSED: Final = "Apple's lookup refused the request (HTTP 403), probably rate limiting."
EPISODE_NOT_FOUND: Final = "Apple Podcasts has no public episode {episode_id} in show {show_id}."
NOT_IN_FEED: Final = (
    "The show's feed does not list this episode, and Apple gives no audio URL for it."
)


@dataclass(frozen=True, slots=True)
class Resolution:
    episode: Episode
    source: ResolveSource  # where the episode was identified
    match: MatchTier | None  # None: the feed does not list it; audio came from Apple


class AppleResolver:
    """Not thread-safe: it holds one ``GuardedFetcher``."""

    def __init__(self, fetcher: GuardedFetcher, *, max_bytes: int) -> None:
        self._fetcher = fetcher
        self._max_bytes = max_bytes

    @property
    def fetcher(self) -> GuardedFetcher:
        return self._fetcher

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def close(self) -> None:
        self._fetcher.close()

    def resolve(self, url: str, /) -> Episode:
        return self.resolve_details(url).episode

    def resolve_details(self, url: str, /) -> Resolution:
        ref = parse_apple_url(url)
        show, feed_url, apple, source = self._identify(ref)
        doc = self._fetcher.get(feed_url, max_bytes=self._max_bytes, purpose="feed")
        feed_host = urlsplit(doc.url).hostname
        feed = parse_feed(doc.content, host=feed_host)
        found = match_item(
            feed.items,
            guid=apple.guid,
            audio_url=apple.audio_url,
            title=apple.title,
            published_at=apple.published_at,
            date_only=apple.date_only,
            duration_s=apple.duration_s,
        )
        item = found.item if found else None
        tier = found.tier if found else None
        audio_url = self._audio_url(apple, item, matched=found is not None)
        episode = _build_episode(ref, show, apple, feed, item, audio_url)
        logger.info(
            "resolved %s (show %s, episode %s) from the %s; feed match: %s; feed host %s",
            episode.key,
            ref.show_id,
            ref.episode_id,
            source,
            tier or "none",
            feed_host,
        )
        return Resolution(episode=episode, source=source, match=tier)

    # -- steps -------------------------------------------------------------------------------

    def _identify(self, ref: EpisodeRef) -> tuple[AppleShow, str, AppleEpisode, ResolveSource]:
        try:
            doc = self._fetcher.get(
                LOOKUP_URL,
                params=lookup_params(ref),
                max_bytes=self._max_bytes,
                purpose="lookup",
            )
        except PermanentFetchError as exc:
            if exc.status == 400:
                raise PermanentError(BAD_LOOKUP.format(country=ref.country)) from exc
            if exc.status == 403:
                raise TransientError(LOOKUP_REFUSED) from exc
            raise
        result = parse_lookup(doc.content, ref.show_id, ref.episode_id)
        logger.debug("lookup listed %d episodes", result.episode_count)
        show = result.show
        if show is None:
            raise PermanentError(UNKNOWN_SHOW.format(show_id=ref.show_id, country=ref.country))
        feed_url = show.feed_url
        if feed_url is None:
            raise SkipEpisode(NO_FEED_REASON)
        if result.episode is not None:
            return show, feed_url, result.episode, "lookup"
        return show, feed_url, self._from_page(ref), "page"

    def _from_page(self, ref: EpisodeRef) -> AppleEpisode:
        not_found = EPISODE_NOT_FOUND.format(episode_id=ref.episode_id, show_id=ref.show_id)
        try:
            doc = self._fetcher.get(ref.url, max_bytes=self._max_bytes, purpose=PAGE_PURPOSE)
        except PermanentFetchError as exc:
            if exc.status in (404, 410):
                raise PermanentError(not_found) from exc
            raise
        episode = parse_episode_page(doc.content, ref.show_id, ref.episode_id)
        if episode is None:
            raise PermanentError(not_found)
        return episode

    @staticmethod
    def _audio_url(apple: AppleEpisode, item: FeedItem | None, *, matched: bool) -> str:
        if matched and item is not None:
            if item.enclosure_url is None:
                raise SkipEpisode(NO_AUDIO_REASON)
            return item.enclosure_url
        if apple.audio_url is not None:
            logger.info("the feed does not list the episode; using Apple's audio URL")
            return apple.audio_url
        if apple.subscriber_only:
            raise SkipEpisode(NO_AUDIO_REASON)
        raise PermanentError(NOT_IN_FEED)


def _build_episode(
    ref: EpisodeRef,
    show: AppleShow,
    apple: AppleEpisode,
    feed: Feed,
    item: FeedItem | None,
    audio_url: str,
) -> Episode:
    guid = apple.guid or (valid_guid(item.guid) if item else None)
    guid = guid or f"apple-podcasts:{ref.episode_id}"
    title = (
        clean_text(apple.title, limit=MAX_TITLE_CHARS)
        or (clean_text(item.title, limit=MAX_TITLE_CHARS) if item else "")
        or f"Episode {ref.episode_id}"
    )
    show_title = (
        clean_text(show.title, limit=MAX_TITLE_CHARS)
        or clean_text(feed.title, limit=MAX_TITLE_CHARS)
        or f"Show {ref.show_id}"
    )
    published_at = apple.published_at
    if (
        item is not None
        and item.published_at is not None
        and (published_at is None or apple.date_only)
    ):
        published_at = item.published_at
    duration_s = apple.duration_s
    if duration_s is None and item is not None:
        duration_s = item.duration_s
    description = clean_text(apple.description, limit=MAX_DESCRIPTION_CHARS, keep_newlines=True)
    if not description and item is not None:
        description = clean_text(
            html_to_text(item.description), limit=MAX_DESCRIPTION_CHARS, keep_newlines=True
        )
    return Episode(
        key=episode_key("apple_podcasts", ref.show_id, guid),
        source="apple_podcasts",
        show_id=ref.show_id,
        episode_id=ref.episode_id,
        guid=guid,
        title=title,
        show_title=show_title,
        published_at=published_at,
        duration_s=duration_s,
        audio_url=audio_url,
        feed_url=show.feed_url,
        url=ref.url,
        description=description,
    )


def build_apple_resolver(settings: Settings) -> AppleResolver:
    return AppleResolver(guarded_fetcher(settings), max_bytes=settings.max_feed_mb * MIB)
