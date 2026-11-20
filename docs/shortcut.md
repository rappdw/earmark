# The iOS Shortcut

A Shortcut named "Earmark" appears in the share sheet of Apple Podcasts. It posts the
episode link, and an optional question, to your earmark API. You then get a note in your
vault and, if ntfy is set up, a push notification.

**Not verified by the earmark build.** The build had no iPhone. The action names below
follow Apple's Shortcuts User Guide, and the request is tested against the API, but nobody
has run this Shortcut on a phone yet. Section 6 says what is unknown and how to record
it.

## 1. Before you start

You need three things:

- **The API reachable from the phone over HTTPS.** See
  [docs/deploy.md](deploy.md) section 6: a private DNS name with a WireGuard tunnel, or
  Tailscale Serve. Check it first with Safari on the phone: open
  `https://earmark.example.com/healthz`. It should show `{"status":"ok"}`. The examples
  below use `earmark.example.com`; replace it with your name.
- **The token**, the value of `EARMARK_API_TOKEN` on the server.
- **The Shortcuts app.**

## 2. Build the Shortcut

Create a new Shortcut and name it "Earmark". In its details (the ⓘ button), turn on
**Show in Share Sheet**. Then add these actions, in this order:

<!-- shortcut-actions:begin -->
1. **Receive input from Share Sheet**: the "Receive … input from Share Sheet" row at the
   top. Accept **URLs** and **Text**, so the Shortcut appears whether Podcasts shares a
   link or text that contains one. If there is no input: Stop and Respond. This accepts
   the shared URL.
2. **Get URLs from Input**: the input is Shortcut Input.
3. **Get Item from List**: First Item, from URLs. A share can contain more than one URL,
   and the API takes exactly one.
4. **Ask for Input**: type Text, with the prompt "Question for this episode (optional)".
   Leaving the answer blank gives the default analysis, so this prompt is optional.
   Whether Shortcuts accepts an empty answer is unverified.
5. **Get Contents of URL**: URL `https://earmark.example.com/ingest` (or
   `https://spark.tailnet-name.ts.net/ingest` with Tailscale Serve). Under Show More:
   Method **POST**; Headers: `Authorization` = `Bearer <your token>`; Request Body
   **JSON**, with Text fields `url` = Item from List and `prompt` = Provided Input. This
   posts the JSON with the bearer header. `Authorization` is the only header to add:
   Shortcuts sends `Content-Type: application/json` for a JSON body. No header row may be
   left empty.
6. **Show Result**: Contents of URL. This shows the response, which is the API's JSON
   (`job_id`, or `detail` on an error).
<!-- shortcut-actions:end -->

Variations: use **Show Notification** instead of **Show Result** for a quieter result.
There is no need to send a `timestamp`.

The token is stored inside the Shortcut. Do not share the Shortcut with anyone; if you
do, change `EARMARK_API_TOKEN` and the Shortcut.

## 3. The request

The Shortcut sends this JSON:

<!-- ingest-request-example -->
```json
{
  "url": "https://podcasts.apple.com/us/podcast/the-daily/id1200361736?i=1000802907981",
  "prompt": "Which studies are cited, and who ran them?"
}
```

The API answers `202 Accepted` with the job:

<!-- ingest-response-example -->
```json
{"job_id": "3f0c9a6e2b8d4c1fa7e5d2b9c8a1f0e3", "status": "queued", "created": true}
```

`created` is `false` when you share an episode again while its first job is still active:
you get the same job back instead of a second one.

To test from a laptop, put the request in `body.json` and run:

```bash
curl -s https://earmark.example.com/ingest \
  -H "Authorization: Bearer $EARMARK_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d @body.json
```

## 4. Errors the Shortcut can show

The API's message is shown as is, in `detail`.

| Status | Message | Meaning |
|---|---|---|
| 401 | `missing or invalid API token` | The `Authorization` header is missing or the token is wrong. |
| 422 | `That is not a valid link. Share an episode from Apple Podcasts.` | The shared text is not a URL. |
| 422 | `Only Apple Podcasts episode links are supported.` | A link from another site. |
| 422 | `This links to a whole show. Share a single episode instead.` | The link has no episode (`?i=`). |
| 422 | `This Apple link is not a podcast episode.` | An Apple link of another kind. |
| 422 | `unknown field 'x' (allowed: url, prompt, timestamp)` | The JSON body has a field the API does not know. |
| 422 | `request body is not valid JSON` | The request body is not JSON. |
| 413 | `Content Too Large` | The body is larger than 64 KiB. |
| 503 | `the job store is unavailable; try again shortly` | The database is busy. Try again. |

Problems on the way to the API look different:

- **"The server could not be found"**: the home resolver drops the private answer for
  the name (DNS rebinding protection). See [docs/deploy.md](deploy.md) section 6.1.6.
- **A connection timeout away from home**: the WireGuard tunnel is not active (or
  Tailscale is off).
- **"The network connection was lost"**: usually a header row with an empty name or
  value. Delete empty rows.

## 5. After you share

The job runs in the background. If ntfy is set up
([docs/deploy.md](deploy.md) section 10), a notification arrives when it finishes, and a
failure notification carries the reason. To follow progress, call `GET /jobs/{id}` with
the same `Authorization` header. If nothing works, run `earmark doctor` on the server.

**Asking another question.** Share the same episode again and type the new question.
Earmark does not download or transcribe it again, and the note gains a section. From a
computer, `earmark reanalyze <key> --prompt "…"` (keys from `earmark episodes`) or `POST
/episodes/{key}/analyze` does the same without contacting Apple; see
[the README](../README.md).

## 6. What the share sheet sends (unverified)

**Not captured yet.** The earmark build had no iPhone, so the exact input the Podcasts
share sheet gives a Shortcut has not been recorded. These forms come from Apple's lookup
API and episode pages, and earmark accepts all of them:

- `https://podcasts.apple.com/<cc>/podcast/<slug>/id<show>?i=<episode>`, with or without
  the slug and with extra parameters such as `uo=4` or `&r=<seconds>`;
- the legacy `itunes.apple.com` host.

**Not accepted.** A show link without `?i=` is refused with a message saying so.
`music.apple.com` and `embed.podcasts.apple.com` links are not accepted.

**Capturing a share.**

1. Temporarily add **Show Result** (Shortcut Input) as the first action.
2. Share an episode from Podcasts with "From Start", then again with "From [time]".
3. Write down the text that is shown, and the URL that step 3 of the Shortcut produces.

**Adding it as a test case.**

1. Add an entry to `tests/fixtures/share_sheet.json` with these keys, copied exactly:

   ```json
   {
     "captured_on": "2026-12-01",
     "ios": "26.2",
     "share_option": "From Start",
     "shortcut_input": "<exactly what Show Result displayed for Shortcut Input>",
     "url": "<what Get URLs from Input + Get Item from List produced>",
     "canonical": "https://podcasts.apple.com/<cc>/podcast/id<show>?i=<episode>"
   }
   ```

2. Run `uv run pytest tests/test_docs.py -k share_sheet`.
3. If it fails, the shape is new: report it, or extend `parse_apple_url` with a test.

Never add an entry that was not captured on a phone.
