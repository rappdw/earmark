# Apple resolver fixtures

Real responses captured on 2026-11-20 by `tests/record_fixtures.py` and trimmed. Never edit
them by hand: tests that need a variant (an item without an enclosure, a changed GUID)
derive it at run time with the mutators in `tests/apple_fixtures.py`.

| File | Source | Trim |
|---|---|---|
| `lookup_daily.json` | `https://itunes.apple.com/lookup?id=1200361736&entity=podcastEpisode&limit=200&country=us` | show record and episodes `1000802907981`, `1000802680076`, `1000802062822`, `1000801908793` |
| `feed_daily.xml` | The Daily's feed, `https://feeds.simplecast.com/Sl5CSM3S` | channel header and the four matching items |
| `lookup_business_wars.json` | same lookup for show `1335814741` | show record and episodes `1000799743520`, `1000799743409`, `1000799743555`, `1000802882841` |
| `feed_business_wars.xml` | `https://rss.art19.com/business-wars` | channel header and the four matching items (by GUID or enclosure host and path, because three GUIDs differ) |
| `lookup_planet_money.json` | same lookup for show `290783428` | show record and the newest episode only; the recorder asserts `1000655720058` is not in the full result |
| `feed_planet_money.xml` | `https://feeds.npr.org/510289/podcast.xml` | channel header, the newest episode's item, and the item with GUID `e3153a19-8617-46a1-a9ef-989fe009911c` |
| `page_planet_money_1000655720058.html` | `https://podcasts.apple.com/us/podcast/id290783428?i=1000655720058`, after its redirect | a minimal HTML shell around the `schema:episode` and `serialized-server-data` script elements, verbatim |
| `lookup_no_feed.json` | same lookup for show `1656859371` (a subscription show) | verbatim |
| `lookup_empty.json` | same lookup for show `999999999999` (no such show) | verbatim |

Re-record with `uv run python -m tests.record_fixtures` (add `--out DIR` to write elsewhere
and diff). When the machine's DNS answers private addresses the recorder turns the address
guard off, like the `network` tests do.
