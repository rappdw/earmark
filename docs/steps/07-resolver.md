# Step 07: Resolver: Apple → iTunes → RSS

## 1. Outcome

`EARMARK_RESOLVER_BACKEND=apple` (the default) is a real backend. `AppleResolver.resolve`
takes the canonical Apple Podcasts episode URL from the job, asks Apple's iTunes Lookup
API for the show and its newest episodes (`entity=podcastEpisode&limit=200`, with the
URL's country), fetches the show's RSS feed, finds the shared episode in the feed, and
returns an `Episode` whose `audio_url` is the feed's enclosure. Every request goes through
step 06's `GuardedFetcher`; feeds are parsed with `defusedxml`.

Matching a lookup record to a feed item tries the GUID, then the enclosure URL (host and
path), then the normalized title, then the publish date (equal to the minute, or within
a day when Apple gives only a date, and never against a contradicting duration). When the
feed no longer lists the episode, the audio URL Apple gives is used instead. A show without a
public feed, or an episode whose feed item has no enclosure, ends the job as `skipped`
with a reason that names subscriber-only and exclusive content.

An episode that is not in the lookup result (Apple lists at most the newest 200, and for
some shows far fewer) is identified from Apple's public episode web page instead: the
page embeds the episode's GUID, title, date, duration, show, and public audio URL as
JSON. This is the fallback chosen after live research (section 2.4).

`earmark resolve <url>` prints the resolved `Episode` as JSON. Real, trimmed responses
from Apple and from three publishers live in `tests/fixtures/apple/`, recorded by
`tests/record_fixtures.py`. An opt-in `network` test resolves a real recent episode and a
real out-of-window one, and a new weekly and manually triggerable workflow
(`.github/workflows/network.yml`) runs `pytest -m network`.

## 2. Findings

Probed on 2026-11-20 with `curl -A 'earmark/0.1.0'` (generic User-Agent, no personal
details, public shows only). `curl` reaches real hosts from this sandbox even though the
sandbox DNS answers `10.200.9.2` for every name; earmark's guarded fetcher refuses that
address, so anything in Python that talks to real hosts here must pass
`allow_private=True` (section 2.10). Installed: Python 3.13.5, expat 2.9.0, httpx2 2.16.2.
Probe scripts and downloads lived in the session scratchpad and are not part of the repo.

### 2.1 The Lookup API (brief research items 1 and 2)

- `https://itunes.apple.com/lookup?id=<show>&entity=podcastEpisode&limit=N&country=cc`
  returns `{"resultCount": n, "results": [show, episode, ...]}`. The first result is the
  show (`wrapperType: "track"`, `kind: "podcast"`, `collectionId == trackId == show id`,
  `collectionName`, `feedUrl`, `trackCount`). The rest are episodes
  (`wrapperType: "podcastEpisode"`, `kind: "podcast-episode"`, `trackId` = the `?i=`
  value, `collectionId`, `episodeGuid`, `trackName`, `releaseDate` ISO with `Z`,
  `trackTimeMillis`, `episodeUrl`, `description`, `shortDescription`, `feedUrl`,
  `trackViewUrl`). The show record's own `trackTimeMillis` is meaningless (1735 for The
  Daily): never read it.
- **Maximum `limit` is 200 episodes**, plus the show record. Tested `limit` 50, 200,
  201, 300, 1000 on Planet Money (`290783428`), Lex Fridman (`1434243584`), and Up First
  (`1222114325`): 200 and above all return `resultCount: 201`. Without `limit` the
  default is 50. Apple's documentation agrees ("1 to 200", "The default is 50") and says
  the API is "limited to approximately 20 calls per minute (subject to change)".
  `offset=200`, `sort=recent`, and `sort=oldest` are ignored: there is no paging.
- **The window can be much smaller than 200.** The Daily (`trackCount` 2785) returns
  only 44 episodes even with `limit=200`; This American Life (518) returns 16. Apple
  seems to list only what is in the current feed. So "outside the window" means "not in
  the lookup result", whatever the count.
- Episodes come newest first.
- **`lookup?id=<episode id>` does not work.** `id=1000802907981` with no entity, with
  `entity=podcastEpisode`, with `media=podcast`, and with `country=us` all return
  `{"resultCount": 0, "results": []}` with HTTP 200.
- Unknown ids (`id=1`, `id=999999999999`) return HTTP 200 with `resultCount: 0` (the body
  starts with three blank lines). An invalid country (`country=zz`) returns **HTTP 400**
  with `{"errorMessage": "Invalid value(s) for key(s): [country]", ...}`; a non-numeric id
  also gives 400. `country=GB` and `country=gb` both work.
- The response is `content-type: text/javascript; charset=utf-8`, gzip-encoded when asked.
  Do not check the content type. Sizes with `limit=200`: 0.5 to 0.9 MB.
- 30 rapid lookups in a row all returned 200; no rate limiting was observed. Community
  reports say Apple answers 403 when the limit is exceeded; this was not observed here.
- Search (`/search?term=...&entity=podcastEpisode`) finds episodes by text only and,
  for The Daily, only episodes still in the lookup window. It cannot look up an episode
  id. Not used.

### 2.2 Lookup records versus feed items (real data)

Compared every lookup episode (`limit=200`) with the show's feed:

| Show | Lookup episodes | Feed items | GUID equal | Notes |
|---|---|---|---|---|
| The Daily `1200361736` | 44 | 70 | 44 | titles also equal |
| Planet Money `290783428` | 200 | 355 (back to 2023-07) | 200 | titles also equal |
| Up First `1222114325` | 200 | 500 | 200 | several episodes per day: a date-only rule would be ambiguous for 160 of 200 |
| Business Wars `1335814741` | 200 | 716 | **8** | see below |

Business Wars (art19) is the case the fallbacks exist for. Apple's `episodeGuid` is a
UUID (`63f0a3d4-…`) while the feed's `<guid>` is `gid://art19-episode-locator/V0/…`; the
feed title appends the part number (`… | A Bloody Business | 1` versus Apple's
`… | A Bloody Business`). The enclosure URL path is identical
(`https://rss.art19.com/episodes/88f2ce7b-….mp3`) but the query is not: art19 embeds the
requesting User-Agent in `?rss_browser=…`, so **compare host and path, never the
query**. With host and path, 200 of 200 match uniquely. The publish dates are identical to
the second, so the date rule also matches all 200.

Feed dates: Business Wars writes `Thu, 19 Nov 2026 08:00:00 -0000`, and
`email.utils.parsedate_to_datetime` returns a **naive** datetime for `-0000` (RFC 2822
"unknown zone"). Treat naive results as UTC, or comparisons with aware datetimes raise
`TypeError`. `itunes:duration` appears as `HH:MM:SS` (Daily, Business Wars) and plain
seconds (NPR). Every item in the four feeds has exactly one `audio/mpeg` enclosure.

The lookup's `episodeUrl` equals the feed's enclosure URL for The Daily and Planet Money
(the podtrac and swap.fm tracker chains of step 06).

**Dates and durations of corresponding items (measured after the implementer found
the date-tier flaw, same captures).** For all 644 lookup records that pair with a feed
item by GUID or enclosure (The Daily 44, Planet Money 200, Business Wars 200, Up First
200), Apple's `releaseDate` equals the feed's `pubDate` **to the second** (difference 0 s
in every case). Apple's `trackTimeMillis / 1000` equals the feed's `itunes:duration` to
the second in 643 of 644; the other differs by 14 s (Up First). Meanwhile neighbouring
feed items are close in time: the smallest gap between consecutive items is 23.95 h for
The Daily (so the previous episode is inside a one-day window; with the target dropped
the 2026-11-18T10:49:08Z item is 23 h 58 m away), 3.6 h for Planet Money, 5 min for Up
First (2 gaps under 1 h), and **0 s** for Business Wars (16 gaps under 1 h: items
published at the same instant). So "nearest within a day" picks a wrong episode whenever
the real one is missing from the feed, which is exactly when the fallback to Apple's audio
URL should apply. A real match is identified by an equal timestamp and an agreeing
duration; time alone cannot separate Business Wars items published at the same instant.

### 2.3 Shows with no public feed

`Beyond The Pearls Premium: Internal Medicine Board Review` (`1656859371`, a
subscription show) returns only the show record, with no `feedUrl` and no episodes. Its
episode pages still show a free trailer and even name a megaphone feed in their embedded
data; we do not use that (the brief says such a show is skipped). Another show found
without `feedUrl` lists episodes whose `episodeUrl` points at Patreon; it has explicit
content and is not used as a fixture.

### 2.4 Episodes outside the window: the episode page (brief research item, the fallback)

`https://podcasts.apple.com/{country}/podcast/id{show}?i={episode}` (our canonical URL)
answers 301 to the slugged URL and then 200 `text/html` (130 to 210 kB for an episode,
1.5 MB for a show page). The page carries two JSON blocks:

- `<script id=schema:episode type="application/ld+json">` (unquoted id): schema.org
  `PodcastEpisode` with `name`, `datePublished` (date only, `2024-05-15`), `duration`
  (`PT18M51S`), `description`, `url` (with `?i=<episode>`), and
  `partOfSeries.url` (with `id<show>`). No GUID, no audio URL.
- `<script type="application/json" id="serialized-server-data">`: Apple's internal page
  model. Several nested dicts (`episodeOffer`, `model`, …) describe the episode with
  `contentId` (the episode id, a string), `guid`, `title`, `releaseDate` (ISO, `Z`),
  `duration` (seconds), `storeUrl`, `streamUrl`, `priceType`, `mediaEnclosures`, and
  `showOffer` (`adamId` = show id, `feedUrl`, `title`). Other dicts on the page (the
  "Play" button, recommendations) have no `guid` or another `contentId`.

Verified for Planet Money's "Why Gold? (Classic)" (`1000655720058`, 2024-05-15, outside
the 200-episode window whose oldest entry is 2025-01-03): the page's `guid`
`e3153a19-8617-46a1-a9ef-989fe009911c` equals the feed item's `<guid>` and the lookup
search record's `episodeGuid`, so an episode resolved from the page gets the same key as
when it was in the window. `mediaEnclosures` has two entries: `priceType: "STDQ"` with the
public `streamUrl` (identical to the feed enclosure), and `priceType: "PSUB"` with an HLS
playlist on `play.itunes.apple.com` (the subscriber stream). Only `STDQ` is usable.

Edge behaviour of the page URL:
- An unknown episode id (`?i=1000000000001`) redirects to the **show page** (1.5 MB, no
  `schema:episode`, no offer with that `contentId`).
- A real episode id under the **wrong show id** (`id290783428?i=1000802907981`) redirects
  to the right show's episode page (The Daily's). So the page's show must be checked.
- `gb` and `us` storefronts both serve the page.
- Python's `html.parser.HTMLParser` (stdlib, `convert_charrefs=True`) extracts both
  script bodies intact (script is a CDATA element), including the unquoted `id`, on all
  three pages fetched. `json.loads` reads them.

**Decision.** When the episode is not in the lookup result, fetch the episode page and
read `serialized-server-data` (GUID, title, date, duration, show, public audio URL),
falling back to the JSON-LD block (title, date, duration, show) if the first is missing
or unreadable. Then match in the feed as usual. This is undocumented page structure, so
it is the fallback only, it fails with a clear `PermanentError` when neither block
identifies the episode, and the weekly network test exercises it so a change shows up.
Alternatives rejected: Apple's `amp-api` (needs a bearer token scraped from page
JavaScript), the Search API (needs the title we do not have), and failing every old
episode with `PermanentError` (old episodes are a normal thing to share for research;
The Daily's window is only about ten months).

### 2.5 Apple URLs the share sheet produces (brief research item 3)

Step 04 (`docs/steps/04-api.md` section 2.9) recorded the shapes on 2026-11-19 and
`parse_apple_url` handles them. Nothing new can be verified without an iPhone; step 16
pastes a real share into a test. What matters here: the resolver only ever sees the
canonical `https://podcasts.apple.com/{cc}/podcast/id{show}?i={episode}`, and that URL
also works as the episode-page URL (section 2.4).

### 2.6 XML parsing: defusedxml

- Python 3.13's `xml.etree.ElementTree` with expat 2.9.0 **expands internal entities**
  (a small "billion laughs" `<!ENTITY lol2 "&lol;&lol;&lol;">` expanded to `lollollol`),
  and silently skips an external parameter entity. expat's amplification guard only
  starts after 8 MiB of output, with a factor of 100, so a 20 MiB feed could still
  expand to gigabytes. Not acceptable for untrusted feeds.
- `defusedxml 0.7.1` (`defusedxml.ElementTree.fromstring(data)`, defaults
  `forbid_dtd=False, forbid_entities=True, forbid_external=True`), probed in a scratch
  venv on Python 3.13.5 with `warnings.simplefilter("error")`: imports without warnings;
  raises `defusedxml.EntitiesForbidden` for the billion-laughs, external-entity
  (`file:///etc/passwd`), and external-parameter-entity documents; accepts a
  `<!DOCTYPE rss PUBLIC "-//Netscape Communications//DTD RSS 0.91//EN" "…">` without
  fetching it; raises `xml.etree.ElementTree.ParseError` (the same class object) for
  malformed XML, an undefined entity (`&nbsp;`), and empty input; decodes UTF-16 with a
  BOM and ISO-8859-1 per the XML declaration; returns standard `Element`s with
  `{namespace}local` tags. `DefusedXmlException` subclasses `ValueError`. Checked with
  `isinstance`: the raised `ParseError` is an instance of both
  `xml.etree.ElementTree.ParseError` and `defusedxml.ElementTree.ParseError` (a
  `SyntaxError` subclass), `EntitiesForbidden` is neither, and returned nodes are
  `xml.etree.ElementTree.Element` instances. Catch `DefusedXmlException` and
  `ParseError` separately; never a bare `SyntaxError` or `ValueError`.
- 0.7.1 (2021) is still the latest stable release (0.8.0rc2 is a pre-release). It is a
  pure `py2.py3-none-any` wheel, so `required-environments` is satisfied. Typeshed's
  `types-defusedxml 0.7.0.20260504` types `fromstring(text, forbid_dtd=..., forbid_entities=...,
  forbid_external=...) -> Element` and the exception classes. In a scratch copy of the
  project, `uv add defusedxml>=0.7.1` and `uv add --dev types-defusedxml>=0.7.0.20260504`
  locked cleanly, adding only those two packages.
- Alternatives: `lxml` with `resolve_entities=False` (a compiled dependency for one
  parser), `feedparser` (large, lenient, and still needs its own XML hardening), and a
  hand-written expat parser that refuses `EntityDeclHandler` (more code to get right than
  a maintained library). defusedxml is the standard choice.

### 2.7 Feed and page sizes

Real feeds: The Daily 566 kB, Planet Money 2.2 MB, Up First 2.0 MB, Business Wars
3.1 MB. A show page (what an unknown episode id redirects to) is 1.5 MB. All fit
`EARMARK_MAX_FEED_MB` (20 MiB), which the resolver uses for the lookup, the page, and the
feed.

### 2.8 The scheduled workflow

- A draft `network.yml` (section 4.8) passes `actionlint -shellcheck= -pyflakes=` from
  the project venv.
- GitHub docs (events that trigger workflows, fetched 2026-11-20): `schedule` and
  `workflow_dispatch` "only trigger a workflow run if the workflow file exists on the
  default branch"; scheduled workflows "run on the latest commit on the default branch";
  in a public repository they are disabled after 60 days without activity; the start of
  every hour is a high-load time, so pick an odd minute. So the weekly run starts once
  `impl` is merged to `main`; until then it can be run with
  `gh workflow run network.yml --ref impl` only after the file exists on `main`.

### 2.9 Dead ends

- Do not call `lookup?id=<episode id>` (always empty).
- Do not compare enclosure URLs including the query (art19 varies it per client).
- Do not compare `parsedate_to_datetime` results without forcing a zone (`-0000` is
  naive).
- Do not use `xml.etree.ElementTree.fromstring` on feeds (entity expansion).
- Do not trust the episode page's show: the URL's show id must match the page's.
- Do not use a `PSUB` media enclosure or any `.m3u8` URL as audio.
- Do not match by "nearest publish date within a day" for a precise Apple timestamp:
  on a daily show the previous episode is about 24 h away and wins when the real item
  has left the feed (2.2). A wrong match transcribes the wrong episode; no match falls
  back safely to Apple's audio URL.
- Do not use the show record's `trackTimeMillis`, and do not require
  `content-type: application/json` from the lookup.

### 2.10 Sandbox DNS and the live checks

As in step 06: the guard refuses every real host here, correctly. The `network` tests
call `dns_intercepted()` and pass `allow_private=True` to their fetchers when it is true;
the fixture recorder does the same. The live smoke (section 7) sets
`EARMARK_ALLOW_PRIVATE_FETCH=true` and first shows the refusal without it. On CI's
GitHub-hosted runner DNS is public, so the guard stays on.

### 2.11 Markdown formatting

`make lint` runs `ruff format --check .`, which formats Python blocks in `docs/`. The
snippets here were checked with `uv run ruff format --check docs/`.

## 3. Files

| File | Change |
|---|---|
| `pyproject.toml`, `uv.lock` | Add `defusedxml>=0.7.1` to `[project.dependencies]` and `types-defusedxml>=0.7.0.20260504` to the `dev` group (`uv add`, `uv add --dev`). The lock gains only those two packages. |
| `src/earmark/resolve/text.py` | New. `clean_text`, `html_to_text`, `normalize_title`. Stdlib only. |
| `src/earmark/resolve/itunes.py` | New. Lookup constants, `AppleShow`, `AppleEpisode`, `LookupResult`, `lookup_params`, `parse_lookup`, `LookupFormatError`. Stdlib and earmark only; no network. |
| `src/earmark/resolve/apple_page.py` | New. `extract_page_json`, `episode_from_server_data`, `episode_from_json_ld`, `parse_episode_page`, `parse_iso_duration`, `WrongShowError`. Stdlib (`html.parser`, `json`) only. |
| `src/earmark/resolve/rss.py` | New. `FeedItem`, `Feed`, `FeedFormatError`, `parse_feed` (defusedxml), `parse_pub_date`, `parse_duration`, `MatchTier`, `Match`, `match_item`. |
| `src/earmark/resolve/resolver.py` | New. `AppleResolver`, `Resolution`, message constants, `build_apple_resolver`. The only resolve module that imports `earmark.fetch`. |
| `src/earmark/pipeline/factory.py` | Register `"apple"` in `RESOLVERS` (builder imports `earmark.resolve.resolver` inside the function). Add `build_resolver(settings)`. |
| `src/earmark/cli.py` | New `earmark resolve <url>` command. |
| `.github/workflows/network.yml` | New. Weekly and manual `pytest -m network`. |
| `tests/fixtures/apple/*` | New. Recorded, trimmed responses (section 6.1) and `README.md` with provenance. |
| `tests/record_fixtures.py` | New. Re-records the fixtures through `GuardedFetcher` (run by hand, never by pytest). |
| `tests/apple_fixtures.py` | New. Fixture loading, a `FixtureServer` MockTransport handler, and the byte and JSON mutators the tests use. |
| `tests/http_helpers.py` | Add `dns_intercepted()`. (`tests/test_fetch.py` may switch to it; behaviour unchanged.) |
| `tests/test_resolve_text.py`, `tests/test_resolve_itunes.py`, `tests/test_resolve_page.py`, `tests/test_resolve_rss.py`, `tests/test_resolver.py` | New. |
| `tests/test_factory.py`, `tests/test_cli.py`, `tests/test_ci.py` | Adjusted and extended (section 6). |
| `docs/ARCHITECTURE.md` | Section 8, item 9. Already edited with this plan. |

No new settings: `.env.example` and `Settings` are unchanged. `docs/PLAN.md`'s status
table is updated by the orchestrator, not the implementer.

## 4. Interfaces

All new dataclasses are `@dataclass(frozen=True, slots=True)`. Datetimes are
timezone-aware UTC. Strings returned by parsers are stripped but otherwise raw; the
resolver cleans them when it builds the `Episode`.

### 4.1 `earmark.resolve.text`

```python
def clean_text(value: str | None, *, limit: int, keep_newlines: bool = False) -> str: ...


def html_to_text(value: str | None) -> str: ...


def normalize_title(value: str | None) -> str: ...
```

- `clean_text`: `None` → `""`. Replace every Unicode `Cc` character (except `\n` when
  `keep_newlines`) with a space; with `keep_newlines`, normalise `\r\n` and `\r` to `\n`,
  collapse runs of spaces and tabs within each line, strip each line, and collapse three
  or more consecutive newlines to two; without it, collapse all whitespace to single
  spaces. Strip. If longer than `limit` characters, cut to `limit - 1` and append `…`.
- `html_to_text`: a small `HTMLParser` subclass (`convert_charrefs=True`) that keeps text
  data, emits `\n` for `br`, and `\n\n` after `p`, `div`, `li`, `ul`, `ol`, `h1`–`h6`,
  and drops `script` and `style` contents. Returns the joined text (not cleaned).
- `normalize_title`: `html.unescape`, NFKC, `casefold`, delete every character whose
  Unicode category starts with `P` or `S` (punctuation and symbols: curly quotes, `.`,
  `|`, `—`, `&`), collapse whitespace, strip. `‘Did A.I. Break the Internet?’` and
  `'Did A.I. Break the Internet?'` both give `did ai break the internet`.

### 4.2 `earmark.resolve.itunes`

```python
LOOKUP_URL: Final = "https://itunes.apple.com/lookup"
LOOKUP_LIMIT: Final = 200


@dataclass(frozen=True, slots=True)
class AppleShow:
    show_id: str
    title: str
    feed_url: str | None  # None when missing, blank, or not an absolute http(s) URL


@dataclass(frozen=True, slots=True)
class AppleEpisode:
    episode_id: str
    guid: str | None
    title: str
    published_at: datetime | None
    date_only: bool  # published_at came from a date without a time
    duration_s: float | None
    audio_url: str | None  # a public http(s) audio URL from Apple, if any
    description: str
    subscriber_only: bool  # Apple lists only a subscriber stream


@dataclass(frozen=True, slots=True)
class LookupResult:
    show: AppleShow | None
    episode: AppleEpisode | None
    episode_count: int  # episode records in the result, for logging


class LookupFormatError(TransientError):
    """Apple's lookup answered with something that is not the expected JSON."""


def lookup_params(ref: EpisodeRef) -> dict[str, str]: ...


def parse_lookup(data: bytes, show_id: str, episode_id: str) -> LookupResult: ...


def http_audio_url(value: object) -> str | None: ...
```

- `lookup_params(ref)` returns exactly `{"id": ref.show_id, "entity": "podcastEpisode",
  "limit": "200", "country": ref.country}`.
- `parse_lookup`: `json.loads(data)` (bytes; JSON detects UTF-8/16/32). Any
  `ValueError`, a top level that is not a dict, or `results` that is not a list →
  `LookupFormatError("Apple's lookup returned a response earmark cannot read.")`.
  Non-dict entries in `results` are ignored. The show is the first entry with
  `wrapperType == "track"` and `collectionId` equal to `show_id` (section 5.2 for id
  comparison); `kind`, if present, must be `"podcast"`. The episode is the first entry
  with `wrapperType == "podcastEpisode"` and `trackId` equal to `episode_id`.
- Field extraction is defensive: a field of the wrong type counts as missing. `title`
  is `collectionName`, else `trackName`. `feed_url` is `feedUrl` if it is a string that
  `urlsplit` gives an `http`/`https` scheme and a host. Episode: `guid` = stripped
  `episodeGuid` if 1 to 1000 characters with no `Cc` characters, else `None`; `title` =
  `trackName`; `published_at` = `releaseDate` via `datetime.fromisoformat` (naive → UTC;
  unparseable → `None`); `duration_s` = `trackTimeMillis / 1000` when it is a positive
  `int` (not `bool`) or positive finite `float`; `audio_url` = `http_audio_url(episodeUrl)`;
  `description` = `description`, else `shortDescription`, else `""`;
  `subscriber_only=False`; `date_only=False`.
- `http_audio_url(value)`: a stripped string with scheme `http`/`https`, a host, and a
  path that does not end in `.m3u8` (case-insensitive), else `None`. Shared with the page
  parser.

### 4.3 `earmark.resolve.apple_page`

```python
SERVER_DATA_ID: Final = "serialized-server-data"
JSON_LD_ID: Final = "schema:episode"
MAX_NODES: Final = 500_000


class WrongShowError(PermanentError):
    def __init__(self, episode_id: str, show_id: str, page_show_id: str) -> None: ...


def extract_page_json(html: str) -> dict[str, object]: ...


def episode_from_server_data(
    data: object, show_id: str, episode_id: str
) -> AppleEpisode | None: ...


def episode_from_json_ld(data: object, show_id: str, episode_id: str) -> AppleEpisode | None: ...


def parse_episode_page(content: bytes, show_id: str, episode_id: str) -> AppleEpisode | None: ...


def parse_iso_duration(value: object) -> float | None: ...
```

- `extract_page_json(html)`: an `HTMLParser` subclass collects the text of each
  `<script>` whose `id` attribute is `serialized-server-data` or `schema:episode`
  (first occurrence of each), then `json.loads` each. Returns `{id: parsed}` for those
  that parsed; a block that fails to parse is left out (logged at DEBUG).
- `episode_from_server_data`: walk `data` iteratively (explicit stack, at most
  `MAX_NODES` nodes visited, no recursion) and take the first dict with `contentId`
  (string, or int not bool) equal to `episode_id` and a non-empty string `guid`. Then:
  - Page show id: `showOffer.adamId` (string or int) if present, else the `id(\d+)` in
    the path of `storeUrl`. If found and different from `show_id` → raise
    `WrongShowError(episode_id, show_id, page_show_id)` (the page show id is included
    only if it is 1 to 20 ASCII digits; otherwise it is reported as `another show`).
  - `guid` (same validity rule as the lookup), `title`, `releaseDate` (ISO),
    `duration` (seconds, positive int or float).
  - Audio: if `mediaEnclosures` is a non-empty list, `audio_url` is
    `http_audio_url(streamUrl)` of the first entry with `priceType == "STDQ"`;
    `subscriber_only` is true when no entry is usable that way and at least one entry
    has `priceType == "PSUB"`. If `mediaEnclosures` is absent or empty, `audio_url` is
    `http_audio_url(streamUrl)` of the offer itself when its `priceType` is absent or
    `"STDQ"`, and `subscriber_only` is `priceType == "PSUB"`.
  - `description=""` (the offer has none), `date_only=False`.
- `episode_from_json_ld`: `data` must be a dict with `@type == "PodcastEpisode"` whose
  `url` has a query `i` equal to `episode_id` (else `None`). If
  `partOfSeries.url` contains an `id(\d+)` path segment different from `show_id` →
  `WrongShowError`. `title` = `name`; `published_at` from `datePublished`: a plain date
  `YYYY-MM-DD` becomes midnight UTC with `date_only=True`, an ISO datetime is used as
  is; `duration_s = parse_iso_duration(duration)`; `description`; `guid=None`;
  `audio_url=None`; `subscriber_only=False`. A record with an empty `title` is `None`.
- `parse_episode_page(content, ...)`: decode UTF-8 with `errors="replace"`,
  `extract_page_json`, try `episode_from_server_data` on the server data, then
  `episode_from_json_ld` on the JSON-LD block. `None` if neither identifies the episode.
  `WrongShowError` propagates.
- `parse_iso_duration("PT18M51S") == 1131.0`; accepts `P[nD]T[nH][nM][n[.n]S]` and
  `PnD`; anything else, or a non-string, → `None`.

`WrongShowError` message: `Apple lists episode {episode_id} under show {page_show_id},
not {show_id}. Share the episode again from Apple Podcasts.`

### 4.4 `earmark.resolve.rss`

```python
ITUNES_NS: Final = "http://www.itunes.com/dtds/podcast-1.0.dtd"
CONTENT_NS: Final = "http://purl.org/rss/1.0/modules/content/"
DATE_TOLERANCE: Final = timedelta(seconds=60)  # precise Apple timestamps
DATE_WINDOW: Final = timedelta(days=1)  # Apple gives only a calendar date
DURATION_TOLERANCE_S: Final = 30.0
DURATION_TOLERANCE_RATIO: Final = 0.02

type MatchTier = Literal["guid", "enclosure", "title", "date"]


@dataclass(frozen=True, slots=True)
class FeedItem:
    index: int  # position in the feed, 0-based
    guid: str | None
    title: str
    published_at: datetime | None
    duration_s: float | None
    enclosure_url: str | None  # first usable enclosure, see 5.4
    description: str  # raw: itunes:summary, else description, else content:encoded


@dataclass(frozen=True, slots=True)
class Feed:
    title: str
    items: tuple[FeedItem, ...]


@dataclass(frozen=True, slots=True)
class Match:
    item: FeedItem
    tier: MatchTier


class FeedFormatError(PermanentError):
    def __init__(self, message: str, *, host: str | None) -> None: ...


def parse_feed(data: bytes, *, host: str | None) -> Feed: ...


def parse_pub_date(value: str | None) -> datetime | None: ...


def parse_duration(value: str | None) -> float | None: ...


def match_item(
    items: Sequence[FeedItem],
    *,
    guid: str | None,
    audio_url: str | None,
    title: str,
    published_at: datetime | None,
    date_only: bool = False,
    duration_s: float | None = None,
) -> Match | None: ...


def durations_agree(apple_s: float, item_s: float) -> bool: ...
```

`durations_agree(a, b)` is `abs(a - b) <= max(DURATION_TOLERANCE_S,
DURATION_TOLERANCE_RATIO * a)`.

`host` is only used in error messages (the final feed host from `Document.url`).

### 4.5 `earmark.resolve.resolver`

```python
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


@dataclass(frozen=True, slots=True)
class Resolution:
    episode: Episode
    source: ResolveSource  # where the episode was identified
    match: MatchTier | None  # None: the feed does not list it; audio came from Apple


class AppleResolver:
    def __init__(self, fetcher: GuardedFetcher, *, max_bytes: int) -> None: ...

    @property
    def fetcher(self) -> GuardedFetcher: ...

    @property
    def max_bytes(self) -> int: ...

    def resolve(self, url: str, /) -> Episode: ...  # resolve_details(url).episode

    def resolve_details(self, url: str, /) -> Resolution: ...

    def close(self) -> None: ...


def build_apple_resolver(settings: Settings) -> AppleResolver:
    return AppleResolver(guarded_fetcher(settings), max_bytes=settings.max_feed_mb * MIB)
```

Other messages, as module constants (format strings) so tests compare exactly:

| Constant | Text | Raised as |
|---|---|---|
| `UNKNOWN_SHOW` | `Apple Podcasts has no show {show_id} in the '{country}' store.` | `PermanentError` |
| `BAD_LOOKUP` | `Apple's lookup rejected the request for the '{country}' store (HTTP 400).` | `PermanentError` |
| `LOOKUP_REFUSED` | `Apple's lookup refused the request (HTTP 403), probably rate limiting.` | `TransientError` |
| `EPISODE_NOT_FOUND` | `Apple Podcasts has no public episode {episode_id} in show {show_id}.` | `PermanentError` |
| `NOT_IN_FEED` | `The show's feed does not list this episode, and Apple gives no audio URL for it.` | `PermanentError` |

`country`, `show_id`, and `episode_id` come from the parsed URL (two ASCII letters and
digits), so messages never carry untrusted text.

### 4.6 Factory

```python
def _apple_resolver(ctx: BuildContext) -> Resolver:
    from earmark.resolve.resolver import build_apple_resolver

    return build_apple_resolver(ctx.settings)


RESOLVERS: Final[Mapping[str, Builder[Resolver]]] = {
    "apple": _apple_resolver,
    "fake": lambda ctx: FakeResolver(),
}


def build_resolver(settings: Settings, *, clock: Clock = utc_now) -> Resolver: ...
```

`build_resolver` looks up `settings.resolver_backend` with `_lookup` (so an unbuilt name
is the same `ConfigError` line), builds a `BuildContext` with
`ArtifactStore(settings.data_dir)` (no I/O), and returns the resolver. It does not build
or check any other backend.

### 4.7 CLI: `earmark resolve URL`

```
earmark resolve URL
```

- Loads settings like the other commands (exit 2 on `ConfigError`), configures logging,
  `build_resolver(settings)` (exit 2 on `ConfigError`, message on stderr).
- Calls `resolve_details` when the resolver is an `AppleResolver` (import inside the
  command), else `resolve`. Closes an `AppleResolver` afterwards (in `finally`).
- Success, exit 0: stdout is `episode.model_dump_json(indent=2)` plus a newline. For an
  `AppleResolver` it also writes one line to stderr:
  `resolved from the {source}; feed match: {tier}` or `resolved from the {source}; the
  feed does not list it, audio URL from Apple`.
- `UnsupportedUrlError` → stderr `earmark resolve: {message}`, exit 2 (caught before
  `PermanentError`, which it subclasses).
- `SkipEpisode` → stderr `earmark resolve: skipped: {reason}`, exit 3.
- `PermanentError` → stderr `earmark resolve: {message}`, exit 1.
- `TransientError` → stderr `earmark resolve: {message} (temporary; try again later)`,
  exit 1.
- Messages printed through the existing `_clean` helper (non-printables replaced).
  Other exceptions propagate (typer shows the traceback), as in the worker.

### 4.8 Workflow `.github/workflows/network.yml`

```yaml
# Opt-in live tests against Apple and real podcast feeds. Not a required check:
# a failure here usually means an outside service changed, not that a commit broke.
name: Network tests

on:
  schedule:
    - cron: "23 5 * * 1"   # Mondays 05:23 UTC, away from the top of the hour
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  network:
    name: pytest -m network (linux/arm64)
    runs-on: ubuntu-24.04-arm
    timeout-minutes: 15
    steps:
      - name: Check out
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - name: Install uv
        uses: astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2 # v10.3.3
        with:
          version: ">=0.13.9,<0.14"
          enable-cache: true
          cache-dependency-glob: uv.lock
          prune-cache: true
          cache-python: true
      - name: Install dependencies
        run: uv sync --locked
      - name: Network tests
        run: uv run --locked pytest -m network -v -rs
```

The action SHAs and setup-uv options are the same as `ci.yml`, so Dependabot bumps
both together. No secrets, no `env`, no Python version (it comes from `.python-version`).

## 5. Behaviour and edge cases

### 5.1 `resolve_details(url)`, in order

1. `ref = parse_apple_url(url)`. `UnsupportedUrlError` (a `PermanentError`) propagates.
   The runner passes the canonical URL, so this only fails for hand-made jobs and the CLI.
2. **Lookup.** `doc = fetcher.get(LOOKUP_URL, params=lookup_params(ref),
   max_bytes=self.max_bytes, purpose="lookup")`. A `PermanentFetchError` with
   `status == 400` → `PermanentError(BAD_LOOKUP)`; with `status == 403` →
   `TransientError(LOOKUP_REFUSED)` (section 2.1: Apple documents a rate limit and is
   reported to answer 403; retrying is cheap). Other fetch errors propagate (5xx, 408,
   429, timeouts and DNS failures are already `TransientFetchError`; 404 and the rest
   `PermanentFetchError`). `result = parse_lookup(doc.content, ref.show_id,
   ref.episode_id)` (`LookupFormatError` is transient).
3. `result.show is None` → `PermanentError(UNKNOWN_SHOW)`. No further request.
4. `show.feed_url is None` → `SkipEpisode(NO_FEED_REASON)`. No further request, even if
   the episode is missing too.
5. **Episode.** If `result.episode` is set, `source = "lookup"`. Otherwise
   `source = "page"`: `fetcher.get(ref.url, max_bytes=..., purpose=PAGE_PURPOSE)` (no
   `Accept` override: the probes used the default `*/*`); a `PermanentFetchError` with status 404 or 410 →
   `PermanentError(EPISODE_NOT_FOUND)`; other fetch errors propagate.
   `parse_episode_page(...)`: `None` → `PermanentError(EPISODE_NOT_FOUND)`;
   `WrongShowError` propagates. The page URL is the canonical URL, built from the
   parsed ids, so no client-controlled URL is fetched.
6. **Feed.** `fetcher.get(show.feed_url, max_bytes=..., purpose="feed")` (fetch errors
   propagate); `feed = parse_feed(doc.content, host=urlsplit(doc.url).hostname)`.
7. **Match.** `match_item(feed.items, guid=apple.guid, audio_url=apple.audio_url,
   title=apple.title, published_at=apple.published_at, date_only=apple.date_only,
   duration_s=apple.duration_s)`.
8. **Audio.**
   - Matched, and `item.enclosure_url` is set → that URL.
   - Matched, no usable enclosure → `SkipEpisode(NO_AUDIO_REASON)`.
   - Not matched, `apple.audio_url` set → that URL (INFO log: the feed does not list the
     episode).
   - Not matched, `apple.subscriber_only` → `SkipEpisode(NO_AUDIO_REASON)`.
   - Otherwise → `PermanentError(NOT_IN_FEED)`.
9. **Build the `Episode`** (5.6) and return `Resolution(episode, source, tier or None)`.
10. Log one INFO line: `resolved %s (show %s, episode %s) from the %s; feed match: %s;
    feed host %s` with the key, ids, source, tier (or `none`), and the feed host. Never a
    URL path or query, a title, or a description.

### 5.2 Ids in JSON

Apple sends `collectionId` and `trackId` as JSON integers and `contentId`/`adamId` as
strings. Compare with a helper `_id_equals(value: object, expected: str) -> bool`:
true for an `int` (not `bool`) whose `str()` equals `expected`, or a `str` equal to
`expected` after stripping. `expected` is already normalised by `parse_apple_url` (no
leading zeros).

### 5.3 Parsing the feed (`parse_feed`)

- `defusedxml.ElementTree.fromstring(data)` with the default flags (DTDs allowed,
  entities and external references forbidden). Bytes are passed as is so the XML
  declaration and BOM pick the encoding.
- `defusedxml.DefusedXmlException` → `FeedFormatError("The show's feed from {host}
  declares XML entities, which earmark refuses.")`.
  `defusedxml.ElementTree.ParseError` → `FeedFormatError("The show's feed from {host}
  is not valid XML.")`. Both chained `from exc` (the `errors.py` convention); the
  message itself never includes the parser's text, which can quote document content.
  `host` shown as `an unknown host` when `None`; shortened to 100 characters.
- The root's local name must be `rss` and it must have a `channel` child (local name,
  any namespace), else `FeedFormatError("The show's feed from {host} is not an RSS
  feed.")`. Atom and RSS 1.0 are not supported (podcast feeds are RSS 2.0).
- `Feed.title` = channel `title` text. Items are the `item` children of `channel`, in
  document order.
- Elements are matched by namespace and local name. For iTunes tags compare the
  namespace case-insensitively (some feeds write `http://www.itunes.com/DTDs/Podcast-1.0.dtd`).
- Item fields: `guid` = stripped `guid` text (empty → `None`); `title` = `title` text,
  else `itunes:title`; `published_at = parse_pub_date(pubDate)`; `duration_s =
  parse_duration(itunes:duration)`; `enclosure_url` per 5.4; `description` =
  `itunes:summary`, else `description`, else `content:encoded` (raw text; the resolver
  converts it).
- `parse_pub_date`: strip; `email.utils.parsedate_to_datetime`; on `ValueError`/
  `TypeError`/`IndexError` try `datetime.fromisoformat`; else `None`. A naive result is
  taken as UTC; return `astimezone(UTC)`.
- `parse_duration`: strip; `SS`, `SS.fff`, `MM:SS`, `HH:MM:SS` (each part ASCII digits,
  the last may have a fraction) → seconds as `float`; anything else, negative, or not
  finite → `None`. `"00:28:55"` → `1735.0`, `"1131"` → `1131.0`, `"1:00"` → `60.0`.

### 5.4 Choosing the enclosure

Among the item's `enclosure` children, in order, take the first whose `url` attribute
passes `http_audio_url` and whose `type` attribute is absent, empty,
`application/octet-stream`, or starts with `audio/` or `video/`. If none qualifies,
`enclosure_url = None`. So an item with no enclosure, an empty `url`, an `ftp:` URL, or
only an `image/jpeg` enclosure counts as having no public audio.

### 5.5 Matching (`match_item`)

Tiers, first hit wins:

1. **guid**: the first item whose `guid == guid` (exact, both stripped). Skipped when
   `guid` is `None`.
2. **enclosure**: key `(hostname.lower(), path)` from `urlsplit`, scheme and query
   ignored. Skipped when `audio_url` is `None` or its path is `""` or `"/"`. Matches only
   when exactly one item's `enclosure_url` has the same key; zero or several → next tier.
3. **title**: items whose `normalize_title(item.title)` equals `normalize_title(title)`,
   which must not be empty. One → it. Several → the one whose `published_at` is nearest
   to `published_at` when that is known (items without a date sort last; ties keep feed
   order), else the first in feed order.
4. **date**: skipped when `published_at` is `None`. An item is a candidate when it has
   a `published_at` and
   - Apple's date is precise (`date_only` false):
     `abs(item.published_at - published_at) <= DATE_TOLERANCE` (60 s, inclusive; covers
     feeds that drop the seconds), and, when both `duration_s` and `item.duration_s` are
     known, `durations_agree(duration_s, item.duration_s)`; or
   - Apple's date is a calendar date only (`date_only` true, the JSON-LD fallback): the
     item's UTC calendar date is within one day of Apple's
     (`abs(item.published_at.date() - published_at.date()) <= DATE_WINDOW`), **and**
     both durations are known and agree. Without a duration there is no date match for a
     date-only target.

   Exactly one candidate → match. Zero → no match. Two or more → no match, logged at
   INFO as ambiguous. There is no "nearest" choice.

`None` if no tier matches. The tiers are ordered from most to least specific: real feeds
reuse titles (`Trailer`, reruns marked `(Classic)`) and publish several episodes a day
(Up First) or several at the same instant (Business Wars). The date tier comes last and
requires corroboration because a false match is worse than none: no match falls back to
Apple's audio URL (5.1 step 8), a false match transcribes another episode. In the
captured data every true pair has equal timestamps and agreeing durations (2.2), so the
tier still catches the case it exists for (Business Wars, where GUID and title differ),
while the previous episode of a daily show (about 24 h away) and a neighbour five minutes
away (Up First) are never taken. The brief's "within a day" survives only for date-only
Apple dates; for precise timestamps a day-wide window cannot be made safe (section 8).

### 5.6 Building the `Episode`

| Field | Value |
|---|---|
| `source` | `"apple_podcasts"` |
| `show_id`, `episode_id`, `url` | from `ref` (`url` is canonical) |
| `guid` | `apple.guid`, else the matched item's `guid` (if 1 to 1000 characters with no `Cc`), else `f"apple-podcasts:{ref.episode_id}"` |
| `key` | `episode_key("apple_podcasts", ref.show_id, guid)` |
| `title` | `clean_text(apple.title, limit=MAX_TITLE_CHARS)`, else the item's title cleaned, else `f"Episode {episode_id}"` |
| `show_title` | `show.title` cleaned, else `feed.title` cleaned, else `f"Show {show_id}"` |
| `published_at` | `apple.published_at` unless it is `None` or `date_only` and the item has a date; then the item's |
| `duration_s` | `apple.duration_s`, else the item's |
| `audio_url` | per 5.1 step 8 |
| `feed_url` | `show.feed_url` (Apple's, not the final URL after redirects) |
| `description` | `clean_text(apple.description, limit=MAX_DESCRIPTION_CHARS, keep_newlines=True)`, else `clean_text(html_to_text(item.description), ...)` with the same options, else `""` |

**Why Apple's GUID first.** The episode key must not change when the same episode is
shared again later. Apple's GUID is tied to the Apple episode id and is the same in the
lookup and on the page (section 2.4), while the feed GUID can differ from it (Business
Wars) and the match tier can change as the feed changes. Only when Apple gives none (the
JSON-LD fallback) does the feed GUID decide; the synthetic `apple-podcasts:` GUID is a
last resort so a feed-less match still has a stable key.

Titles and descriptions are untrusted. Cleaning here only removes control characters and
bounds length; making them safe as filenames and YAML is the vault sanitizer's job (step
12). Truncating a title or description is allowed; this is metadata, not the transcript.

### 5.7 Errors summary

| Situation | Outcome |
|---|---|
| Lookup or feed or page: timeout, DNS failure, 408, 429, 5xx | `TransientError` (from `fetch.py`) |
| Lookup 403 | `TransientError(LOOKUP_REFUSED)` |
| Lookup 400 (bad country) | `PermanentError(BAD_LOOKUP)` |
| Lookup body not JSON / wrong shape | `LookupFormatError` (transient) |
| No show record (unknown show, or not a podcast id) | `PermanentError(UNKNOWN_SHOW)` |
| Show without a usable `feedUrl` | `SkipEpisode(NO_FEED_REASON)` |
| Episode not in lookup and page does not identify it, or page 404/410 | `PermanentError(EPISODE_NOT_FOUND)` |
| Page says the episode belongs to another show | `WrongShowError` (permanent) |
| Feed with entity declarations / malformed / not RSS | `FeedFormatError` (permanent) |
| Feed 404, other 4xx, size cap | `PermanentFetchError` (from `fetch.py`) |
| Matched item without usable enclosure; or unmatched and Apple lists only a subscriber stream | `SkipEpisode(NO_AUDIO_REASON)` |
| Unmatched and no Apple audio URL | `PermanentError(NOT_IN_FEED)` |

A blocked address (sandbox DNS, a Pi-hole answering `0.0.0.0`) is step 06's
`BlockedAddressError`, permanent, naming `EARMARK_ALLOW_PRIVATE_FETCH`.

### 5.8 Requests made

In-window: two requests (lookup, feed). Out-of-window: three (lookup, page, feed). No
feed or unknown show: one. The resolver keeps no cache: the runner writes `episode.json`
after it, and a retry repeats the requests, which is what a retry should do.

### 5.9 Not thread-safe

`AppleResolver` holds one `GuardedFetcher`, which is not thread-safe; each worker process
builds its own through the factory, as for the audio fetcher.

## 6. Tests

### 6.1 Fixtures (`tests/fixtures/apple/`)

Real responses, captured by `tests/record_fixtures.py` and trimmed. **Never edit them by
hand.** Tests that need a variant (an item without an enclosure, a changed GUID) derive
it at run time with the mutators in `tests/apple_fixtures.py`, so every file stays a
faithful (trimmed) copy of what a server sent.

| File | Source | Trim |
|---|---|---|
| `lookup_daily.json` | `lookup?id=1200361736&entity=podcastEpisode&limit=200&country=us` | show record + episodes `1000802907981`, `1000802680076`, `1000802062822`, `1000801908793` |
| `feed_daily.xml` | The Daily's `feedUrl` (`https://feeds.simplecast.com/Sl5CSM3S`) | channel header + the items of those four episodes |
| `lookup_business_wars.json` | `lookup?id=1335814741&…` | show + `1000799743520`, `1000799743409`, `1000799743555`, `1000802882841` |
| `feed_business_wars.xml` | `https://rss.art19.com/business-wars` | header + the four matching items (matched by GUID or enclosure host and path, because three GUIDs differ) |
| `lookup_planet_money.json` | `lookup?id=290783428&…` | show + the first (newest) episode only; recorder asserts `1000655720058` is **not** in the full result |
| `feed_planet_money.xml` | `https://feeds.npr.org/510289/podcast.xml` | header + the newest episode's item + the item with GUID `e3153a19-8617-46a1-a9ef-989fe009911c` |
| `page_planet_money_1000655720058.html` | `https://podcasts.apple.com/us/podcast/id290783428?i=1000655720058` (after its redirect) | `<!DOCTYPE html>\n<html><head>\n` + the `schema:episode` and `serialized-server-data` script elements verbatim, joined by `\n` + `\n</head><body></body></html>\n` |
| `lookup_no_feed.json` | `lookup?id=1656859371&…` | verbatim (1.8 kB, show record only, no `feedUrl`) |
| `lookup_empty.json` | `lookup?id=999999999999&…` | verbatim (`resultCount: 0`) |
| `README.md` | — | What each file is, the source URL, the capture date, the trim rule, how to re-record, and that variants are made in tests |

Trim rules the recorder implements:
- Lookups: `json.loads`, keep the show record (`wrapperType == "track"`) and the wanted
  episode records, set `resultCount` to the kept count, write
  `json.dumps(obj, ensure_ascii=False, indent=1) + "\n"` as UTF-8. Verbatim files are
  written as received. Fail if a wanted episode is missing.
- Feeds: work on the decoded bytes. Find item spans with
  `re.finditer(rb"<item\b.*?</item>", data, re.DOTALL)`; keep the bytes before the first
  item, each wanted item span followed by `b"\n"`, and the bytes after the last item.
  Wanted = items whose `<guid>` text (CDATA allowed) equals a wanted episode's GUID, or
  whose first enclosure `url` has the same host and path as a wanted episode's
  `episodeUrl`. Fail unless exactly the wanted count is found. Then parse the result with
  `rss.parse_feed` as a self-check.
- Page: regex `rb'<script\b[^>]*\bid="?(?:schema:episode|serialized-server-data)"?[^>]*>.*?</script>'`
  (DOTALL); fail unless exactly two matches.

`tests/record_fixtures.py` (run as `uv run python -m tests.record_fixtures`, writes into
`tests/fixtures/apple/`): fetches with `GuardedFetcher(allow_private=dns_intercepted())`
and `max_bytes=20 * MIB`, so it obeys the "all third-party fetches go through
`fetch.py`" rule. It must pass ruff and mypy (it is under `tests/`); it reports progress
with `logging` (ruff `T20` forbids `print`). It is not collected by pytest (no `test_`
prefix) and makes no request on import (`if __name__ == "__main__":`).

The implementer runs it once to create the fixtures. Section 6.4 lists the values the
tests expect, as captured on 2026-11-20 during planning (the planner ran the same trim
rules on its own capture: 30.7 kB, 22.1 kB, and 14.6 kB for the three feeds; 91 kB for
the page). If a recorded value differs (Apple or a publisher edited something), use the
recorded value and say so in the summary; if a wanted episode is gone, stop and report.

### 6.2 Test helpers (`tests/apple_fixtures.py`)

- `fixture_bytes(name) -> bytes`, `fixture_json(name) -> dict[str, Any]`.
- `FixtureServer`: a callable `httpx2.Request -> httpx2.Response` for `mock_fetcher`.
  Routes: `itunes.apple.com` `/lookup` keyed by the `id` param → (status, body);
  feeds keyed by `host + path`; `podcasts.apple.com` pages keyed by the `i` param, served
  as a 301 from the canonical path to `/{cc}/podcast/{slug}/id{show}?i={ep}` and then
  200 `text/html`. Content types as observed: lookup `text/javascript; charset=utf-8`,
  feeds `application/xml`, pages `text/html`. Unknown route → the test fails (raise
  `AssertionError`). Records every request (`requests: list[httpx2.Request]`). Routes can
  be overridden per test with a status, a body, or an exception to raise
  (`httpx2.ReadTimeout(...)`).
- `default_server()` serves every fixture under its real URL.
- Mutators: `drop_item(feed: bytes, guid: str) -> bytes` and
  `strip_enclosure(feed: bytes, guid: str) -> bytes` (byte-level, same item regex as the
  recorder; fail if the GUID is not found); `with_episode_fields(lookup: dict, track_id,
  **changes) -> bytes` (set or, with `None`, delete keys in one episode record and
  re-serialize); `rewrite_server_data(page: bytes, fn) -> bytes` (parse the
  `serialized-server-data` JSON, apply `fn`, put it back with
  `json.dumps(..., ensure_ascii=False).replace("</", "<\\/")` so a string in the data
  cannot close the script element); `drop_script(page, id) -> bytes`.
- `resolver_for(server) -> AppleResolver` = `AppleResolver(mock_fetcher(server),
  max_bytes=20 * MIB)`.

Constants: `DAILY = canonical_episode_url("us", "1200361736", "1000802907981")`,
`BW = …("us", "1335814741", "1000799743520")`, `PM_OLD = …("us", "290783428",
"1000655720058")`, `NO_FEED = …("us", "1656859371", "1000587846633")`, `UNKNOWN = …("us",
"999999999999", "1000000000001")`.

### 6.3 Brief criteria

| # | Criterion | Tests |
|---|---|---|
| A1 | In-window episode resolves to an `Episode` with guid, title, show title, date, duration, audio URL | `test_resolver.py::test_resolves_daily_episode`: `resolve_details(DAILY)` gives `source == "lookup"`, `match == "guid"`, and the full `Episode` of 6.4 (every field asserted, including `key == "f2faca69c9811409f6a6"`, `feed_url`, `url == DAILY`, a non-empty description). Exactly two requests: the lookup, then the feed. `test_resolve_returns_episode` (`resolve(DAILY) == resolve_details(DAILY).episode`). `test_resolver_satisfies_protocol` (annotated `r: Resolver = AppleResolver(...)`; mypy is the proof). |
| A2 | Audio from the RSS enclosure; lookup `episodeUrl` when the feed no longer lists the item | `test_audio_url_comes_from_feed`: serve the lookup with the target's `episodeUrl` changed to `https://cdn.example/other.mp3` (and its GUID unchanged): `audio_url` is still the feed enclosure, `match == "guid"`. `test_feed_without_item_uses_episode_url`: `drop_item(feed_daily, guid)` (the target item only; the previous day's item stays in the feed): `match is None`, `audio_url` equals the lookup's `episodeUrl`. This is also the resolver-level proof that the previous-day episode is not matched. `test_feed_without_item_and_no_episode_url_fails`: also delete `episodeUrl`: `PermanentError` with exactly `NOT_IN_FEED`. |
| A3 | Matching: GUID, then normalized title, then date within a day, one test each | Fixture-based: `test_match_by_guid` (A1 data; also: a feed whose first item shares the title but not the GUID still matches the GUID item). `test_match_by_title`: Daily lookup with the target's `episodeGuid` set to `not-in-feed` and `episodeUrl` deleted → `match == "title"`, the right item's enclosure, and `guid == "not-in-feed"` (Apple's GUID still keys the episode). `test_match_by_date_business_wars`: BW lookup with the target's `episodeUrl` deleted (GUID and title differ in the real data) → `match == "date"`, the `… A Bloody Business | 1` item. `test_match_by_enclosure_business_wars` (real data, nothing changed) → `match == "enclosure"`, `guid == "63f0a3d4-76bb-4236-a39e-10d1b235b3bd"`, `key == "bec3f156fed0cb38a63c"`, title without the ` | 1`. Unit tests on `match_item` with hand-built `FeedItem`s in `test_resolve_rss.py`: `test_tier_order` (an item matching only by date and another matching by GUID → GUID), `test_enclosure_ignores_query_and_scheme`, `test_enclosure_requires_unique_path` (two items with the same host and path → falls through to title), `test_enclosure_skips_root_path`, `test_title_normalization_match` (curly versus straight quotes, `&amp;`, case, extra spaces), `test_title_duplicates_pick_nearest_date`, `test_date_precise_tolerance` (Δ 0 s and exactly 60 s match; 61 s, 1 h, 12 h, 23 h 58 m, 24 h do not), `test_date_previous_day_of_daily_show_not_matched` (real data: parse `feed_daily.xml`, remove the target item from the parsed items, call `match_item` with the target's lookup values but `guid=None`, `audio_url=None`, title `"unrelated"` → `None`, even though the 2026-11-18T10:49:08Z item is 23 h 58 m away), `test_date_duration_must_agree` (Δ 0 s: durations 1735 vs 1735 and 1735 vs 1750 match; 1735 vs 1500 does not; feed duration missing matches; Apple duration missing matches), `test_date_same_instant_duplicates` (two items at the same timestamp: both with agreeing or unknown durations → `None`; only one with an agreeing duration and the other contradicting → that one), `test_date_close_neighbours_not_matched` (items 5 min before and after the target time, target absent → `None`), `test_date_only_within_a_day` (Apple `2024-05-15` date-only with duration 1131: an item at `2024-05-16T02:00Z` with 1131 s matches; `2024-05-17T00:00Z` does not; 1131 vs 900 does not; Apple duration `None` → `None`; two qualifying items → `None`), `test_durations_agree` (table: 30 s absolute floor, 2 % above 1500 s), `test_no_tier_matches_returns_none`, `test_empty_title_never_matches`. |
| A4 | No `feedUrl`, or a matched item with no enclosure → `SkipEpisode` naming subscriber-only or exclusive content | `test_show_without_feed_skips`: `NO_FEED` with `lookup_no_feed.json`: `SkipEpisode`, `reason == NO_FEED_REASON`, exactly one request. `test_item_without_enclosure_skips`: `strip_enclosure(feed_daily, guid)`: `SkipEpisode`, `reason == NO_AUDIO_REASON`, even though the lookup has an `episodeUrl`. `test_skip_reasons_name_subscribers_and_exclusive` (both constants contain `subscribers` and `exclusive`, and are at most 120 characters). `test_unusable_enclosure_counts_as_none` (parametrized in `test_resolve_rss.py`: empty `url`, `ftp:` URL, `.m3u8`, only `type="image/jpeg"` → `enclosure_url is None`; `type` missing or `application/octet-stream` → kept). `test_page_subscriber_only_skips` (6.3 B2). |
| A5 | Episode outside the lookup window follows the chosen fallback | `test_out_of_window_resolves_from_page`: `PM_OLD` → `source == "page"`, `match == "guid"`, the `Episode` of 6.4; requests in order: lookup, page (canonical path, 301, slug path), feed. `test_out_of_window_not_in_feed_uses_page_audio`: `drop_item(feed_pm, "e3153a19-…")` → `match is None`, `audio_url` is the `STDQ` `streamUrl` (host `tracking.swap.fm`), never the `play.itunes.apple.com` HLS URL. `test_out_of_window_json_ld_only`: `drop_script(page, "serialized-server-data")` → `match == "title"`, `title == "Why Gold? (Classic)"`, `guid` the feed's (`e3153a19-…`), `published_at` the feed's full timestamp (not midnight), `duration_s == 1131.0`. `test_unknown_episode_fails`: `PM` show with episode `1000000000001` served the same page → `PermanentError`, message `EPISODE_NOT_FOUND` formatted. `test_page_404_is_episode_not_found`. `test_page_for_other_show_fails`: `canonical_episode_url("us", "1200361736", "1000655720058")` with the Daily lookup (episode absent) and the PM page → `WrongShowError` whose message names `290783428` and `1200361736`. `test_page_with_neither_block_fails` (a page with both scripts dropped → `EPISODE_NOT_FOUND`). |
| A6 | 5xx and timeouts → `TransientError`; unknown show → `PermanentError` | `test_transient_failures`, parametrized over (lookup, page, feed) × (503, `httpx2.ReadTimeout`, `httpx2.ConnectTimeout`), resolving `PM_OLD` so all three requests happen → `TransientError`, and no request after the failing one. `test_lookup_403_is_transient` (`LOOKUP_REFUSED`). `test_lookup_400_is_permanent` (`BAD_LOOKUP` with the country). `test_lookup_unreadable_is_transient` (bodies `b"<html>"`, `b"[]"`, `b'{"results": "x"}'` → `LookupFormatError`, a `TransientError`). `test_unknown_show_fails`: `UNKNOWN` with `lookup_empty.json` → `PermanentError`, message `UNKNOWN_SHOW` formatted, exactly one request. `test_lookup_without_show_record_fails` (the Daily lookup with the show record removed → `UNKNOWN_SHOW`). `test_feed_404_is_permanent`. `test_pipeline_transient_requeues` (B4). |
| A7 | The URL's country is passed to the lookup | `test_lookup_params` (`lookup_params(ref)` for a `gb` ref equals the exact dict of 4.2). `test_country_reaches_lookup_and_page`: resolve `canonical_episode_url("gb", "290783428", "1000655720058")` → the lookup request has `country=gb`, `id=290783428`, `entity=podcastEpisode`, `limit=200`; the page request path starts with `/gb/podcast/`. |
| A8 | Malformed XML handled; parser safe against entity attacks | `test_resolve_rss.py`: `test_malformed_feed` (the Daily feed cut at 5000 bytes; `b""`; an HTML page) → `FeedFormatError`, `not valid XML` or `not an RSS feed`, message names the host and contains no document text. `test_billion_laughs_refused`, `test_external_entity_refused` (`<!ENTITY xxe SYSTEM "file:///etc/passwd">` used in a title: `FeedFormatError` with `entities`; the message contains neither `passwd` nor `root:`), `test_external_parameter_entity_refused`. `test_public_doctype_allowed` (RSS 0.91 DOCTYPE parses; the default suite blocks sockets, so a DTD fetch would fail the test). `test_undefined_entity_is_malformed` (`&nbsp;`). `test_non_rss_root` (Atom `<feed>`). `test_feed_encodings` (ISO-8859-1 declared, and UTF-16 with BOM, parse to the same title). `test_resolver_feed_entities_fail_job_permanently` (resolver level: `PermanentError`). |
| A9 | The feed is fetched through `fetch.py` | `test_resolver_uses_only_the_injected_fetcher`: every request in A1/A5 was seen by the `FixtureServer` (counts asserted), with `User-Agent: earmark/<version>`. `test_resolve_modules_make_no_direct_requests`: parse the AST of every `src/earmark/resolve/*.py`; no import of `httpx2`, `httpcore2`, `urllib.request`, `http.client`, `socket`, `requests`; only `resolver.py` imports `earmark.fetch`; only `rss.py` imports `defusedxml` or anything under `xml`, and from `xml` it imports only the name `Element` (for annotations; parsing goes through `defusedxml.ElementTree.fromstring`, and `ParseError` is taken from `defusedxml.ElementTree`). `test_factory_builds_apple_resolver` (B1) shows the fetcher is `fetch.guarded_fetcher(settings)`. |
| A10 | Opt-in `network` test resolves a real, recent episode | `test_resolver.py`, `@pytest.mark.network`: `test_live_resolves_recent_episode`: `allow = dns_intercepted()`; a raw `GuardedFetcher(allow_private=allow).get(LOOKUP_URL, params={"id": "1200361736", "entity": "podcastEpisode", "limit": "5", "country": "us"}, ...)` picks the newest episode record; `AppleResolver(GuardedFetcher(allow_private=allow), max_bytes=20 * MIB).resolve_details(canonical)` gives `source == "lookup"`, `match is not None`, `guid == episodeGuid.strip()`, `title` non-empty, `audio_url` starting `https://` or `http://`, `published_at` within 60 days of now, `feed_url` set. `test_live_resolves_out_of_window_episode`: `PM_OLD` → `source == "page"`, `guid == "e3153a19-8617-46a1-a9ef-989fe009911c"`, `title == "Why Gold? (Classic)"`. In this sandbox both run with `allow_private=True` and pass. |
| A11 | Weekly and manual workflow runs `pytest -m network`; not a required check | `test_ci.py` (section 6.5). The "not required" part is a repository setting; the plan adds nothing that would make it required, and the workflow's header comment says so. |

### 6.4 Expected values (from the 2026-11-20 capture)

The Daily, `DAILY`:

```python
EXPECTED_DAILY = {
    "key": "f2faca69c9811409f6a6",
    "source": "apple_podcasts",
    "show_id": "1200361736",
    "episode_id": "1000802907981",
    "guid": "7704ac9b-bb66-4e07-a943-d08a0be0c9fb",
    "title": "‘Did A.I. Break the Internet?’ and Other Things We’ve Worried About This Week",
    "show_title": "The Daily",
    "published_at": "2026-11-19T10:47:59Z",
    "duration_s": 1735.0,
    "feed_url": "https://feeds.simplecast.com/Sl5CSM3S",
}
```

`audio_url` is the feed item's enclosure, which starts
`https://dts.podtrac.com/redirect.mp3/pdst.fm/e/pfx.vpixl.com/6qj4J/` and contains
`episodes/e4f0c51f-a7f8-409f-8786-bccd54d2e1a1/audio/128/default.mp3`; assert it equals
the enclosure `url` read from the fixture with a plain regex in the test (an independent
read), and that it starts with that prefix. Feed `itunes:duration` is `00:28:55`.

Business Wars, `BW`: `guid "63f0a3d4-76bb-4236-a39e-10d1b235b3bd"`, `key
"bec3f156fed0cb38a63c"`, `title "Big Beef: The War on Your Wallet | A Bloody Business"`,
`published_at 2026-11-04T08:00:00Z`, `duration_s 2491.0`, `audio_url` host
`rss.art19.com`, path `/episodes/88f2ce7b-0ead-4e39-80f4-fa16a9a4b552.mp3` (do not
assert the query), `feed_url "https://rss.art19.com/business-wars"`, `show_title
"Business Wars"`.

Planet Money, `PM_OLD`: `guid "e3153a19-8617-46a1-a9ef-989fe009911c"`, `key
"06db3587077b5cb1d7a8"`, `title "Why Gold? (Classic)"`, `published_at
2024-05-15T21:56:13Z`, `duration_s 1131.0`, `show_title "Planet Money"`, `feed_url
"https://feeds.npr.org/510289/podcast.xml"`, `audio_url` host `tracking.swap.fm`, ending
`/audio/128/default.mp3?…&size=18107751` (the feed enclosure equals the page's `STDQ`
`streamUrl`).

### 6.5 Added criteria

| # | Criterion | Tests |
|---|---|---|
| B1 | `apple` registered; factory rules hold; heavy modules load lazily | `test_factory.py::test_factory_builds_apple_resolver` (`resolver_backend="apple"`, `allow_private_fetch=True`, `max_feed_mb=3`: an `AppleResolver`, `max_bytes == 3 * MIB`, `fetcher.allow_private` true). `test_default_settings_list_all_unbuilt`: expect **5** lines and none starting with `EARMARK_RESOLVER_BACKEND` (and still none for `EARMARK_FETCH_BACKEND`). `test_unbuilt_backend_names_setting` drops `resolver_backend` automatically (it computes the list). `test_resolver_modules_not_imported_by_factory` (subprocess: after `import earmark.pipeline.factory`, none of `earmark.resolve.resolver`, `earmark.resolve.rss`, `earmark.resolve.itunes`, `earmark.resolve.apple_page`, `defusedxml` is in `sys.modules`). `test_build_resolver_only_builds_resolver` (`resolver_backend="fake"` with every other backend at its unbuilt default: returns a `FakeResolver`, no `ConfigError`, `data_dir` not created). `test_build_resolver_unbuilt_name` (monkeypatch `RESOLVERS` to lack `apple`: `ConfigError` naming `EARMARK_RESOLVER_BACKEND`). The existing `test_pipeline_modules_import_light` and `test_fetch_module_not_imported_by_factory` must still pass. |
| B2 | Page parsing details | `test_resolve_page.py`: `test_extracts_both_blocks` (fixture: both ids present, each parses to a dict). `test_server_data_episode` (fixture → the PM values of 6.4, `audio_url` the STDQ URL, `subscriber_only` false). `test_server_data_ignores_other_content_ids` (asking for another id → `None`). `test_server_data_wrong_show` (asking with show `1200361736` → `WrongShowError`). `test_server_data_subscriber_only` (`rewrite_server_data` setting every `priceType` in `mediaEnclosures` to `PSUB` → `audio_url is None`, `subscriber_only` true). `test_server_data_no_enclosures_uses_offer_stream` (delete `mediaEnclosures` everywhere: the offer's `streamUrl`). `test_server_data_node_budget` (a 600 000-element nested list → returns `None`, does not raise `RecursionError`). `test_json_ld_episode` (title, `date_only` true, midnight UTC, 1131.0 s, `guid is None`). `test_json_ld_wrong_episode_or_show`. `test_parse_iso_duration` table (`PT18M51S` 1131, `PT1H` 3600, `PT0.5S` 0.5, `P1D` 86400, `""`, `"18:51"`, `None`, `5` → `None`). `test_unparseable_blocks_ignored` (a script with invalid JSON is skipped). `test_page_resolver_subscriber_only_skips` in `test_resolver.py` (that rewritten page and the feed without the item → `SkipEpisode(NO_AUDIO_REASON)`). |
| B3 | Lookup parsing details | `test_resolve_itunes.py`: `test_parse_daily_lookup` (show title, `feed_url`, episode fields of 6.4, `episode_count == 4`). `test_show_record_trackTimeMillis_ignored`. `test_ids_compare_as_int_or_string` (`_id_equals` table: `1200361736`, `"1200361736"`, `" 1200361736 "`, `True`, `1.2e9`, `None`). `test_wrong_types_are_missing` (string `trackTimeMillis`, numeric `trackName`, list `feedUrl`). `test_feed_url_must_be_http` (`ftp://…`, `feeds.example/x`, `""` → `None`). `test_guid_validation` (blank, 1001 characters, a control character → `None`). `test_release_date_parsing` (`Z`, `+00:00`, naive, garbage). `test_episode_url_m3u8_rejected`. `test_empty_lookup` (`lookup_empty.json` → `show is None`). |
| B4 | The resolver works in the pipeline | `test_resolver.py::test_pipeline_with_apple_resolver`: `make_env(tmp_path, backends=replace(fakes.backends(), resolver=resolver_for(default_server())))`; enqueue `CANON` (= `DAILY`); run once → `done`; `episode.json` loads to the expected Daily `Episode`; `job.episode_key == "f2faca69c9811409f6a6"`; the fake fetcher was called with that episode. `test_pipeline_skip_no_feed` (`NO_FEED`: job `skipped`, `error == NO_FEED_REASON`, `stage == "resolve"`). `test_pipeline_unknown_show_fails` (`failed`, `stage == "resolve"`). `test_pipeline_transient_requeues` (lookup 503 with `Retry-After: 600`: `queued`, `attempts == 1`, `run_after` honours it). |
| B5 | Text helpers | `test_resolve_text.py`: `test_clean_text` table (control characters, tabs and newlines collapsed, `keep_newlines` paragraphs, the limit with `…`, `None`). `test_html_to_text` (`<p>a</p><p>b<br>c</p><script>x</script>` → paragraphs, no `x`, entities decoded). `test_normalize_title` table (the Daily title in curly and straight quotes, `A &amp; B` versus `A & B`, NFKC full-width letters, `Something Rotten | 2` ≠ `Something Rotten`). |
| B6 | Feed field parsing | `test_resolve_rss.py`: `test_parse_daily_feed` (4 items; the target item's fields: guid, title, `published_at == 2026-11-19T10:47:59Z`, `duration_s == 1735.0`, enclosure; channel title `The Daily`). `test_parse_pub_date` table (RFC 2822 with `+0000`, with `-0000` → aware UTC, with `GMT`, ISO 8601, garbage, `None`). `test_parse_duration` table (5.3). `test_business_wars_dates_are_aware` (every item's `published_at` has `tzinfo`). `test_itunes_namespace_case_insensitive`. `test_title_falls_back_to_itunes_title`. |
| B7 | CLI `earmark resolve` | `test_cli.py`: `test_resolve_prints_episode_json` (`fake_env` with `RESOLVER_BACKEND=fake`: exit 0, stdout parses with `Episode.model_validate_json`, its `url` is the canonical form of `SHARE`). `test_resolve_apple_details_on_stderr` (monkeypatch `earmark.pipeline.factory.build_resolver` to return `resolver_for(default_server())`: exit 0, stderr contains `feed match: guid`, stdout's `guid` is the Daily's). `test_resolve_skip_exits_3`, `test_resolve_permanent_exits_1`, `test_resolve_transient_exits_1` (a `FakeResolver` with `fail_next(...)`: stderr as in 4.7), `test_resolve_unsupported_url_exits_2` (`SHOW_LINK`: the `show_link` message), `test_resolve_unbuilt_backend_exits_2` (monkeypatched `RESOLVERS` without `apple`, default settings). `test_resolve_sanitizes_output` (a fake episode titled `"\x1b[31mx"`: stdout has no raw ESC byte; JSON escapes it). The existing `test_cli_import_is_light` must still pass. |
| B8 | Messages carry no untrusted text and no URL path or query | `test_messages_are_safe`: for each permanent, transient, and skip outcome in A2–A8, the message contains none of: the feed's path (`/Sl5CSM3S`), a query string from the fixtures (`aid=rss_feed`), any episode or show title, `passwd`. `test_resolve_logs_hosts_only` (`caplog` at DEBUG for `earmark.resolve` and `earmark.fetch` during A1 and A5: no record contains `/Sl5CSM3S`, `aid=`, `Did A.I.`, or `Why Gold`). |
| B9 | Fixtures stay faithful | `test_resolve_rss.py::test_fixture_feeds_parse` (each feed fixture parses and has the item count of 6.1). `test_fixture_lookups_shape` (each lookup fixture: `resultCount == len(results)`; first result is the show). `test_fixtures_readme_lists_every_file` (every file in `tests/fixtures/apple/` except the README is named in it). |
| B10 | The network workflow follows the CI rules | `test_ci.py`: the parametrized checks (permissions, no `pull_request_target`, pinned SHAs, actionlint) pick up `network.yml` automatically through `WORKFLOW_PARAMS`. New: `test_network_workflow_triggers` (exactly `schedule` and `workflow_dispatch`; one cron entry; five fields; day-of-month and month `*`; day-of-week a single digit 0–6; minute not `0`). `test_network_workflow_runs_network_marker` (one job on `ubuntu-24.04-arm`, `timeout-minutes` an int between 1 and 30, run steps exactly `["uv sync --locked", "uv run --locked pytest -m network -v -rs"]`, steps' actions `[checkout, setup-uv, "", ""]`). `test_network_workflow_read_only_no_secrets` (`permissions == {"contents": "read"}`, no `secrets` in the text, no `env` anywhere, checkout `persist-credentials: false`). `test_network_workflow_pins_match_ci` (`_remote_uses(network) == _remote_uses(ci)` and the setup-uv `with:` blocks are equal). |
| B11 | `network` tests adapt to the sandbox DNS | `tests/http_helpers.py::dns_intercepted()` = `not all(is_public_address(a) for a in system_resolve("itunes.apple.com", 443))`. Used by the live tests and the recorder. In the default suite `system_resolve` raises `SocketBlockedError`, so it is only called inside `network` tests. |

## 7. Verification

```bash
cd /home/sandy/dev/earmark
make check
uv run pytest tests/test_resolve_text.py tests/test_resolve_itunes.py tests/test_resolve_page.py \
  tests/test_resolve_rss.py tests/test_resolver.py tests/test_factory.py tests/test_cli.py \
  tests/test_ci.py -q
uv run ruff format --check docs/
uv lock --check
git diff --stat uv.lock   # only defusedxml and types-defusedxml added
uv run pytest -m network -v -rs
# In this sandbox: the two resolver live tests pass (allow_private, intercepted DNS);
# test_fetch: 2 pass, 1 skipped (public DNS). On CI's scheduled run: the guard is on.

# Light imports still hold
uv run python -c 'import sys, earmark.pipeline.factory; print(sorted(m for m in ("httpx2", "defusedxml", "earmark.fetch", "earmark.resolve.resolver", "earmark.resolve.rss") if m in sys.modules))'
# expect: []

# Only fetch.py talks to the network; only rss.py parses XML
grep -rn "import httpx2\|from httpx2\|import httpcore2\|from httpcore2" src/earmark   # fetch.py only
grep -rn "defusedxml\|xml.etree" src/earmark   # resolve/rss.py only

# The workflow lints offline
.venv/bin/actionlint -shellcheck= -pyflakes= .github/workflows/network.yml

# Fixtures are trimmed copies, not hand-written: re-record into a scratch dir and diff
uv run python -m tests.record_fixtures --out "$SCRATCH/fx"
diff -r -x README.md tests/fixtures/apple "$SCRATCH/fx"
# differences only where Apple or a publisher changed data since the recording (report them)
```

`tests.record_fixtures` takes `--out DIR` (default `tests/fixtures/apple`). It never
writes `README.md`, which the implementer writes by hand.

Live smoke 1, `earmark resolve` against the real services. `$SCRATCH` is a scratch
directory:

```bash
cd "$SCRATCH"
E=/home/sandy/dev/earmark/.venv/bin/earmark
URL='https://podcasts.apple.com/us/podcast/did-a-i-break-the-internet-and-other-things/id1200361736?i=1000802907981&uo=4'
$E resolve "$URL"; echo "exit $?"
# In this sandbox: exit 1, "earmark resolve: refusing to connect to itunes.apple.com: it
# resolves to a non-public address (10.200.9.2); see EARMARK_ALLOW_PRIVATE_FETCH"
EARMARK_ALLOW_PRIVATE_FETCH=true $E resolve "$URL"; echo "exit $?"
# exit 0; stderr "resolved from the lookup; feed match: guid"; stdout JSON with
# "guid": "7704ac9b-bb66-4e07-a943-d08a0be0c9fb", "show_title": "The Daily",
# "duration_s": 1735.0, "audio_url": "https://dts.podtrac.com/redirect.mp3/...".
EARMARK_ALLOW_PRIVATE_FETCH=true $E resolve 'https://podcasts.apple.com/us/podcast/why-gold-classic/id290783428?i=1000655720058'
# exit 0; "resolved from the page; feed match: guid"; "title": "Why Gold? (Classic)"
EARMARK_ALLOW_PRIVATE_FETCH=true $E resolve 'https://podcasts.apple.com/us/podcast/x/id1656859371?i=1000587846633'; echo "exit $?"
# exit 3; "earmark resolve: skipped: This show has no public RSS feed. ..."
EARMARK_ALLOW_PRIVATE_FETCH=true $E resolve 'https://podcasts.apple.com/us/podcast/x/id999999999999?i=1000000000001'; echo "exit $?"
# exit 1; "Apple Podcasts has no show 999999999999 in the 'us' store."
EARMARK_ALLOW_PRIVATE_FETCH=true $E resolve 'https://podcasts.apple.com/us/podcast/x/id1335814741?i=1000799743520'
# "feed match: enclosure"; "guid": "63f0a3d4-76bb-4236-a39e-10d1b235b3bd"
```

Live smoke 2, the worker with the real resolver and fakes for everything else:

```bash
cd "$SCRATCH"
export EARMARK_DATA_DIR="$SCRATCH/data" EARMARK_VAULT_DIR="$SCRATCH/vault"
for v in FETCH_BACKEND ASR_BACKEND LLM_PROVIDER STORE_BACKEND INDEX_BACKEND NOTIFY_BACKEND; do
  export "EARMARK_$v=fake"
done
export EARMARK_RESOLVER_BACKEND=apple EARMARK_ALLOW_PRIVATE_FETCH=true
$E submit "$URL"
$E worker --once          # job <id> done
$E jobs --json            # status done, episode_key f2faca69c9811409f6a6
cat "$SCRATCH"/data/episodes/f2faca69c9811409f6a6/episode.json   # the real title and guid
ls "$SCRATCH"/vault/Earmark/Podcasts/                             # one note
```

Also check:

- `git status` lists only the files in section 3 and this plan.
- No `# type: ignore` or `noqa` in new code beyond what the existing code already uses,
  no loosened lint, type, or test settings, and no `enable_socket` outside `network`
  tests.
- The fixtures are byte-identical to what `tests.record_fixtures` produced (no hand
  edits), and `tests/fixtures/apple/README.md` matches.
- `docs/ARCHITECTURE.md` matches section 8, item 9.

Not verifiable in the sandbox: the resolver with the address guard on against real public
DNS (it runs on the scheduled `network` workflow once `network.yml` is on `main`), the
weekly schedule itself, and what today's iOS share sheet hands a Shortcut (step 16).

## 8. Deviations

1. **Out-of-window fallback: Apple's episode web page** (the brief leaves the choice to
   the planner). Undocumented page data, used only when the documented lookup does not
   list the episode, checked against the URL's show, and covered by a weekly live test.
   Section 2.4. **This is a product decision the user should know about.**
2. **The date tier is "equal to the minute, with no contradicting duration", not
   "nearest within a day"**, except for date-only Apple dates, which keep the one-day
   window but must also agree on duration; ambiguity is never resolved by picking. The
   literal brief rule matches the previous episode of a daily show whenever the real
   item has left the feed (found by the implementer; measured in 2.2). The brief's
   criterion stays testable and useful (the Business Wars case matches by date), but
   "within a day" now applies only when Apple gives no time of day.
2a. **A fourth matching tier, enclosure host and path**, between GUID and title. Real
   data (Business Wars, section 2.2) has different GUIDs and different titles for the
   same episode; host and path match uniquely. The brief's three tiers keep their order
   and each has its own test.
3. **`Episode.guid` is Apple's GUID when Apple gives one**, not the feed's, so the
   episode key is stable across re-shares and feed changes (5.6). For in-window episodes
   of most shows the two are equal.
4. **Lookup HTTP 403 is transient** (Apple's documented rate limit), and **lookup 400 is
   reported as a store problem**. Other statuses keep `fetch.py`'s mapping.
5. **An unmatched episode whose only Apple stream is subscriber-only is skipped**, and an
   unmatched episode with no Apple audio URL fails permanently; the brief names only the
   two `SkipEpisode` cases.
6. **New dependency `defusedxml`** (and `types-defusedxml` for mypy), section 2.6.
7. **New modules** `resolve/text.py` and `resolve/apple_page.py` beyond the
   architecture's `itunes.py`, `rss.py`, `resolver.py`; and **`factory.build_resolver`**
   for the CLI.
8. **Test tooling beyond the brief**: `tests/record_fixtures.py` so fixtures can be
   re-recorded when the weekly run shows an outside change, and run-time mutators so
   variants never require hand-edited fixtures.
9. **Architecture edits** made with this plan: Decisions (defusedxml); module layout
   (`resolve/` contents); a "Resolving" subsection under Stage contracts; `Episode`
   field meanings (guid, title, audio URL); Configuration (`MAX_FEED_MB` covers lookup,
   page, and feed); Security (XML via defusedxml, the page fetch, no other XML parser);
   Testing and CI (`network.yml`, `dns_intercepted`, default-branch rule); Known risk 2
   rewritten with these findings.

## 9. Out of scope

- Using the page's `showOffer.feedUrl` or `episodeUrl` when the lookup has no `feedUrl`
  (the brief says skip), and following `itunes:new-feed-url`.
- Any other directory (Podcast Index, Spotify), and resolving show links.
- Caching lookups or feeds across jobs, and a client-side rate limiter for Apple.
- Apple's transcripts (`transcriptInfo` on the page).
- Downloading audio (step 06), transcription (step 08), and sanitizing titles for the
  vault (step 12).
- `earmark doctor` checks for Apple reachability (step 15).
- Pasting a real iOS share into a test (step 16).
- Making `network.yml` a required check (it must not be).
