"""Search vocabulary: queries, hits, the ``Searcher`` protocol, and search errors.

Imports only the standard library and ``earmark.models``/``earmark.errors``, so the API and
the CLI can use it without the ``index`` extra.
"""

import typing
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from pydantic import AwareDatetime

from earmark.errors import EarmarkError, PermanentError
from earmark.models import EpisodeKey, Model, PromptHash, Seconds, is_episode_key

RowKind = Literal["transcript", "summary", "claim", "lead", "question", "answer"]
ROW_KINDS: Final[tuple[RowKind, ...]] = typing.get_args(RowKind)
MAX_QUERY_CHARS: Final = 500
MAX_SEARCH_LIMIT: Final = 50
DEFAULT_SEARCH_LIMIT: Final = 10
SNIPPET_CHARS: Final = 300


@dataclass(frozen=True, slots=True)
class SearchQuery:
    text: str  # whitespace-collapsed, 1..MAX_QUERY_CHARS characters
    limit: int = DEFAULT_SEARCH_LIMIT  # 1..MAX_SEARCH_LIMIT
    kinds: tuple[RowKind, ...] = ()  # empty: every kind; deduplicated, in ROW_KINDS order
    episode_key: str | None = None  # 20 lower-case hex characters


class InvalidQuery(ValueError):  # noqa: N818  # name fixed by the plan
    """``str(exc)`` is a short message that never contains the query."""


def parse_query(
    text: str,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
    kinds: Sequence[str] = (),
    episode_key: str | None = None,
) -> SearchQuery:
    collapsed = " ".join(text.split())
    if not collapsed:
        raise InvalidQuery("q must not be blank")
    if len(collapsed) > MAX_QUERY_CHARS:
        raise InvalidQuery(f"q must be at most {MAX_QUERY_CHARS} characters")
    if not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise InvalidQuery(f"limit must be between 1 and {MAX_SEARCH_LIMIT}")
    if any(k not in ROW_KINDS for k in kinds):
        raise InvalidQuery(f"kind must be one of: {', '.join(ROW_KINDS)}")
    if episode_key is not None and not is_episode_key(episode_key):
        raise InvalidQuery("episode must be an episode key (20 hexadecimal characters)")
    wanted = tuple(k for k in ROW_KINDS if k in kinds)
    return SearchQuery(collapsed, limit, wanted, episode_key)


def snippet(text: str, max_chars: int = SNIPPET_CHARS) -> str:
    """Whitespace collapsed; cut at the last space at or before ``max_chars - 1`` plus ``…``."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_chars:
        return collapsed
    head = collapsed[:max_chars]
    cut = head.rfind(" ")
    return (head[:cut] if cut > 0 else collapsed[: max_chars - 1]).rstrip() + "…"


class SearchHit(Model):
    rank: int  # 1-based
    score: float  # cosine similarity, rounded to 4 places
    episode_key: EpisodeKey
    show_title: str
    episode_title: str
    published_at: AwareDatetime | None
    episode_url: str
    kind: RowKind
    prompt_hash: PromptHash | None
    prompt: str | None  # at most 200 characters
    start_s: Seconds | None
    end_s: Seconds | None
    timestamp: str | None  # format_hms(start_s), "HH:MM:SS"
    snippet: str
    note_path: str | None
    transcript_note_path: str | None


class SearchResults(Model):
    hits: tuple[SearchHit, ...] = ()
    index_exists: bool = True  # False when no index has been written yet


class Searcher(Protocol):
    def search(self, query: SearchQuery, /) -> SearchResults: ...


class SearchUnavailableError(EarmarkError):
    """Search cannot run in this process (extra missing, backend none, bad config)."""


class IndexMismatchError(PermanentError):
    """The table was built with another embedding model, dimension, or index version."""


class UnavailableSearcher:
    def __init__(self, reason: str) -> None:
        self._reason = reason

    def search(self, query: SearchQuery, /) -> SearchResults:
        raise SearchUnavailableError(self._reason)
