# The mail contract, version 1

`contract: 1`. Status: **DRAFT**. It becomes final once the operator approves the receiving
procedure, which is now the receiving skill (step 30). Drafted by pka-skills on 2026-11-23,
owned by this repo since 2026-11-24, and moved here from `docs/PLAN.md` and
`docs/ARCHITECTURE.md` in step 29 without change. A revision is a new `contract/mail-vN.md`
with its own `samples/vN/`, and this file stays, because v1 mail can still be waiting in an
inbox.

<!-- contract-v1:begin -->

- **Both files**
  - **Encoding:** UTF-8 with no BOM and LF line endings. The extension is a lower-case
    `.md`, and the media type is `text/markdown`.
  - **Frontmatter:** YAML between `---` lines at the very top of the file.
  - **Feed- or caller-supplied strings:** double-quoted and escaped, on one line, with
    control characters stripped. They are capped at 300 characters, and `title` at 200.
  - **Unknown values:** the key is omitted. A value is never an empty string or null.
  - **Formats:** timestamps are RFC 3339 with an offset, and dates are `YYYY-MM-DD`.
  - **Filenames:** `YYYY-MM-DD-<slug>.md`. The slug is lower-case ASCII `a-z0-9` joined
    by single hyphens, at most 80 characters. A podcast slug is
    `<show-slug>-<title-slug>`, truncated as a whole.
  - **Filename date:** the publish date when known, otherwise the date of `shared_at` in
    the requester's offset.
- **Attachment 1, the note: `YYYY-MM-DD-<slug>.md`.** Podcasts and audio share one
  schema.

  | key | type | podcast | audio | notes |
  |---|---|---|---|---|
  | `type` | string | required | required | `podcast` or `audio` |
  | `contract` | integer | required | required | `1` |
  | `source` | string | required | required | `earmark` |
  | `origin` | string | required | required | `apple_podcasts`, `upload` or `url` |
  | `title` | string | required | required | podcast: the episode title; audio: the request's `title`, else the upload's filename stem or the URL's last path segment, sanitised |
  | `show` | string | required | omit | |
  | `date` | date | required | required | the filename date |
  | `published` | date | optional | optional | only when actually known |
  | `duration` | string | required | required | `hh:mm:ss` |
  | `language` | string | optional | optional | BCP 47 |
  | `apple_url` | string | optional | omit | |
  | `source_url` | string | omit | optional | `origin: url` only, without userinfo, query or fragment; omitted entirely when the URL carries a query |
  | `guid` | string | required | required | `feed:<feed guid verbatim>`, else `apple:<i= episode id>`, else `sha256:<hex of the audio bytes>` for upload and url |
  | `shared_at` | timestamp | required | required | when this item was first shared |
  | `transcript_sha256` | string | required | required | 64 lower-case hex |
  | `tags` | list of strings | required | required | `["podcast"]` or `["audio"]` |

  - **Body:** `# <title>` (for a podcast, `# <show> — <title>`), then a blank line, then
    `## Transcript`, then a blank line. After that come paragraphs separated by blank
    lines. Each paragraph covers 30–60 seconds or one speaker turn, and starts with
    `[hh:mm:ss]`.
  - **`transcript_sha256`:** the sha256 of the UTF-8 bytes from the first byte after the
    closing `---\n` to the end of the file, as sent.
  - **No audio hash for podcasts:** dynamic ad insertion changes the bytes on every
    download.
- **Attachment 2, the instructions: `YYYY-MM-DD-<slug>.instructions.md`.** It uses the
  same date and slug as the note.
  - **Keys:**
    - `type: earmark-instructions`;
    - `contract: 1`;
    - `for`: the note's filename;
    - `guid`: the same as the note's;
    - `job_id`: stable across a resend, new on every re-share;
    - `shared_at`: when this request was made.
  - **Body:** the requester's words, verbatim, and nothing else.
  - **Always sent.** An empty body means "file it, no analysis".
- **Subject and body.**
  - **Subject:** `[earmark] <title>`, sanitised and capped at 120 characters.
  - **Body:** only text earmark generates. It names which attachment is the transcript,
    which is data and never instructions, and which attachment holds the requester's
    instructions. It carries the `job_id` and the two filenames. It also says that the
    subject and the note contain third-party text.
  - **The guid stays out of the body.** A `feed:` guid is publisher text, so it appears
    only inside the attachments.
- **PKA de-duplication.**
  - The same `guid` and the same `job_id` is a duplicate delivery.
  - The same `guid` with a new `job_id` is a re-share.
  - The same `guid` with a different `transcript_sha256` is a re-transcription, and the
    newer body wins.

<!-- contract-v1:end -->
