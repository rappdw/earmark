# The iOS Shortcut

A Shortcut named "Earmark" appears in the share sheet of Apple Podcasts, the Files app and
other apps that share audio files. It asks where the transcript goes (Personal or Work) and
for instructions, then posts the link or the file to `POST /transcriptions` on your earmark
API. The PKA receives the email, and ntfy pushes "Sent" if it is set up.

**Not verified by the earmark build.** The build had no iPhone. The action names below
follow Apple's Shortcuts User Guide and community references, and the requests are tested
against the API, but nobody has run this Shortcut on a phone yet. These points are unknown:

- what `Get Type` reports for a shared audio file;
- whether a `Form` body is sent as `multipart/form-data`, and with which content type for
  the file part;
- whether `Ask for Input` accepts an empty answer;
- whether the offset in `Format Date` is the phone's current zone;
- how long `Get Contents of URL` waits on a large upload.

Section 6 says how to record them.

## 1. Before you start

You need four things:

- **The API reachable from the phone over HTTPS.** See [docs/deploy.md](deploy.md)
  section 6: a private DNS name with a WireGuard tunnel, or Tailscale Serve. Check it first
  with Safari on the phone: open `https://earmark.example.com/healthz`. It should show
  `{"status":"ok"}`. The examples below use `earmark.example.com`; replace it with your
  name.
- **The token**, the value of `EARMARK_API_TOKEN` on the server.
- **The two destination addresses**, both on the server's allowlist ([docs/deploy.md](deploy.md)
  section 9): `pka-personal@example.net` and `pka-work@example.net`.
- **The Shortcuts app.**

## 2. Build the Shortcut

Create a new Shortcut and name it "Earmark". In its details (the ⓘ button), turn on
**Show in Share Sheet**. Then add these actions, in this order:

<!-- shortcut-actions:begin -->
1. **Receive input from Share Sheet**: the "Receive … input from Share Sheet" row at the
   top. Accept **URLs**, **Text** and **Files**. If there is no input: Stop and Respond.
2. **Choose from Menu**: prompt "Send the transcript to", options **Personal** and
   **Work**.
   - Under Personal: a **Text** action containing `pka-personal@example.net`, then
     **Set Variable**, named `Recipient`.
   - Under Work: the same with `pka-work@example.net`.
   - It is a variable because the menu's own output is not documented, while a variable is
     unambiguous.
   - To add a destination, add an option here and the address to
     `EARMARK_ALLOWED_RECIPIENTS`.
3. **Ask for Input**: type Text, with the prompt "Instructions for this transcript
   (optional)". Its answer is Provided Input.
   - Leaving it blank sends no instructions, which means "file it, no analysis".
   - Whether Shortcuts accepts an empty answer is unverified. If it refuses one, set the
     Default Answer to a single space: earmark strips it.
4. **Format Date**: Date = **Current Date**, Date Format **ISO 8601**, with **Include ISO
   8601 Time** on. The result, Formatted Date, looks like `2026-11-23T21:30:00-08:00`: the
   date, the time and your UTC offset.
   - That offset dates the note and its file name in your time zone. Without `timestamp`,
     every date is UTC.
5. **Get Type**: of Shortcut Input. For a shared file it is expected to give `File`
   (unverified; section 6 shows how to check).
6. **If**: Type **is** `File`.
7. **Get Contents of URL**: URL `https://earmark.example.com/transcriptions`. Under Show
   More:
   - Method **POST**;
   - Headers: `Authorization` = `Bearer <your token>`;
   - Request Body **Form**, with these fields:
     - `file` (type File) = Shortcut Input;
     - `to` (Text) = Recipient;
     - `instructions` (Text) = Provided Input;
     - `timestamp` (Text) = Formatted Date.

   This posts the file as a multipart upload. `Authorization` is the only header to add:
   Shortcuts sets the `Content-Type` itself. No header row may be left empty.
8. **Otherwise**: added with the If. Actions 9 to 11 go here.
9. **Get URLs from Input**: the input is Shortcut Input.
10. **Get Item from List**: First Item, from URLs. A share can hold more than one URL, and
    the API takes one.
11. **Get Contents of URL**: URL `https://earmark.example.com/transcriptions`, Method
    **POST** and the `Authorization` header as in action 7, with Request Body **JSON**, and
    Text fields:
    - `url` = Item from List;
    - `to` = Recipient;
    - `instructions` = Provided Input;
    - `timestamp` = Formatted Date.

    `Authorization` is the only header to add: Shortcuts sends the `Content-Type` for a
    JSON body itself.
12. **End If**: added with the If.
13. **Show Result**: If Result. This shows the API's JSON (`job_id`, or `detail` on an
    error). If "If Result" is not offered, put a Show Result after action 7 and another
    after action 11 instead.
<!-- shortcut-actions:end -->

Variations:

- Use **Show Notification** instead of **Show Result** for a quieter result.
- Add an **Ask for Input** for a `title` on uploads. Podcasts ignore `title`.
- **YouTube.** Share a video from the YouTube app or from Safari. The link goes through the
  same URL branch (actions 8 to 11) with no change to the Shortcut: the server recognises
  `youtu.be/…`, `youtube.com/watch?v=…`, Shorts and the other video forms, keeps only the
  video id, and downloads the audio itself. A channel, a playlist or a search is refused
  (section 4).

The token is stored inside the Shortcut. Do not share the Shortcut with anyone; if you
do, change `EARMARK_API_TOKEN` and the Shortcut.

With Tailscale Serve, the URL is `https://spark.tailnet-name.ts.net/transcriptions`
([docs/deploy.md](deploy.md) section 6.2).

## 3. The requests

**For a link**, the Shortcut sends this JSON:

<!-- request-example -->
```json
{
  "url": "https://podcasts.apple.com/us/podcast/the-daily/id1200361736?i=1000802907981",
  "to": "pka-personal@example.net",
  "instructions": "Which studies are cited, and who ran them?",
  "timestamp": "2026-11-23T21:30:00-08:00"
}
```

The API answers `202 Accepted` with the job:

<!-- response-example -->
```json
{"job_id": "3f0c9a6e2b8d4c1fa7e5d2b9c8a1f0e3", "status": "queued", "created": true}
```

`created` is `false` when you share an episode again while its first job is still active:
you get the same job back instead of a second one. A repeat of a sent job within
`EARMARK_DEDUPE_WINDOW_S` (10 minutes) also returns it.

**For a file**, the Shortcut sends a multipart form with these fields:

<!-- upload-fields:begin -->
| Field | Type in the Form | Value in the Shortcut | Example |
|---|---|---|---|
| `file` | File | Shortcut Input | `Voice memo.m4a` |
| `to` | Text | Recipient | `pka-work@example.net` |
| `instructions` | Text | Provided Input | `File this under ideas.` |
| `timestamp` | Text | Formatted Date | `2026-11-23T21:30:00-08:00` |
<!-- upload-fields:end -->

The API checks the file's name extension or its content type, and an `.m4a` name is
enough. The cap is `EARMARK_MAX_UPLOAD_MB` (default 1024 MiB).

**To test from a laptop**, put the JSON request in `body.json` and run:

```bash
curl -s https://earmark.example.com/transcriptions \
  -H "Authorization: Bearer $EARMARK_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d @body.json
```

and for a file:

```bash
curl -s https://earmark.example.com/transcriptions \
  -H "Authorization: Bearer $EARMARK_API_TOKEN" \
  -F 'file=@Voice memo.m4a' \
  --form-string to=pka-work@example.net \
  --form-string 'instructions=File this under ideas.' \
  --form-string timestamp="$(date -Iseconds)"
```

`--form-string` sends the text values, so a leading `@` or `<` is never read as a file.

## 4. Errors the Shortcut can show

The API's message is shown as is, in `detail`.

| Status | Message | Meaning |
|---|---|---|
| 401 | `missing or invalid API token` | The `Authorization` header is missing or the token is wrong. |
| 413 | `request body is larger than 64 KiB` | A JSON body that large is not a request. |
| 413 | `the upload is larger than the <limit> limit (EARMARK_MAX_UPLOAD_MB)` | The file is over the cap. |
| 415 | `send JSON (Content-Type: application/json) or a multipart form (Content-Type: multipart/form-data)` | The body is neither. |
| 415 | `the file does not look like audio: <…>` | The file's name and content type are not audio. |
| 422 | `That is not a valid link. Share an episode from Apple Podcasts.` | The shared text is not a URL. |
| 422 | `This links to a whole show. Share a single episode instead.` | The link has no episode (`?i=`). |
| 422 | `This Apple link is not a podcast episode.` | An Apple link of another kind. |
| 422 | `This YouTube link is not a single video. Share a video, not a channel, playlist or search.` | The YouTube link is a channel, a playlist, a search or another page. |
| 422 | `This YouTube link has no valid video id.` | The YouTube link has no video id, or one that is not 11 characters. |
| 422 | `to must be one email address` | The `to` is not a single address. |
| 422 | `to is not an allowed recipient (EARMARK_ALLOWED_RECIPIENTS)` | The address is not on the server's allowlist. |
| 422 | `no recipients are allowed yet: set EARMARK_ALLOWED_RECIPIENTS on the server` | The server's allowlist is empty. |
| 422 | `unknown field 'x' (allowed: url, to, instructions, title, timestamp)` | The JSON body has a field the API does not know. |
| 422 | `request body is not valid JSON` | The request body is not JSON. |
| 422 | `the form must have exactly one file field named 'file'` | The form has no file, or more than one. |
| 503 | `the job store is unavailable; try again shortly` | The database is busy. Try again. |

Problems on the way to the API look different:

- **"The server could not be found"**: the home resolver drops the private answer for
  the name (DNS rebinding protection). See [docs/deploy.md](deploy.md) section 6.1.6.
- **A connection timeout away from home**: the WireGuard tunnel is not active (or
  Tailscale is off).
- **"The network connection was lost"**: usually a header row with an empty name or
  value. Delete empty rows.
- **A 415 with a valid file**: the Form was not sent as `multipart/form-data`. Report it
  (section 6).

## 5. After you share

The job runs in the background. The email reaches the chosen PKA, and if ntfy is set up
([docs/deploy.md](deploy.md) section 10), the push says `Sent: <title>` with the masked
address. A failure push carries the reason. To follow progress, call `GET /jobs/{id}` with
the same `Authorization` header: `delivery_state` is `sent` and `sent_at` is set once the
email has gone. If nothing works, run `earmark doctor` on the server.

**A new question.** Share the same item again with new instructions. That is a new job with
a new job id and the same transcript, which is not transcribed again. The PKA keeps one note
and runs the new instructions. A direct audio URL is downloaded again each time, but not
transcribed again. A YouTube video is downloaded again only when it must be transcribed
again; every link form of one video is the same video.

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
3. Write down the text that is shown, and the URL that action 10 of the Shortcut produces.

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

Never add an entry that was not captured on a phone. The fixture is for links only.

**Capturing a file share.**

1. Temporarily add **Show Result** (Type) after action 5.
2. Share a voice memo, and a file from Files.
3. Note what it shows. If it is not `File`, change action 6 to match and send it back
   (runbook "Send back").

There is no fixture for files.
