# The mail contract, version 2

`contract: 2`. Status: **DRAFT**, like version 1, until the operator approves the receiving
skill after the one-episode check. Written in step 32 (2026-11-25) for YouTube as a source.
Version 1 (`mail-v1.md`) stays, because mail sent under it can still be waiting in an
inbox; the receiving skill accepts both.

<!-- contract-v2:begin -->

Contract 2 is contract 1 (`contract/mail-v1.md`) with the changes below. Every rule of
contract 1 that this list does not change holds unchanged.

- **The version.** Both attachments carry `contract: 2`. A message whose two files carry
  different versions breaks the contract.
- **A third note type, `video`, for a YouTube video.** `type: video` goes with
  `origin: youtube`, and `origin: youtube` with `type: video` only. Its `tags` are
  `["video"]`.
- **The keys of a video note.**

  | key | type | video | notes |
  |---|---|---|---|
  | `type`, `contract`, `source`, `date`, `duration`, `shared_at`, `transcript_sha256`, `tags` | as in contract 1 | required | |
  | `origin` | string | required | `youtube` |
  | `title` | string | required | the video's title, sanitised |
  | `channel` | string | optional | the YouTube channel's name, sanitised, only when known |
  | `published` | date | optional | the video's upload date, only when known |
  | `language` | string | optional | BCP 47 |
  | `source_url` | string | required | `https://youtu.be/<video id>` |
  | `guid` | string | required | `youtube:<video id>`: the 11-character id YouTube gives the video, from `A-Z`, `a-z`, `0-9`, `-` and `_`, case kept |
  | `show`, `apple_url` | | omit | |

- **`channel` is a new key.** Only a video note has it; a podcast or audio note never does.
- **`source_url`** is allowed for `origin: url`, as before, and for `origin: youtube`,
  where it is always the short link built from the guid's video id.
- **The file name and heading of a video note.** With a channel, the slug joins the channel
  and title slugs, truncated as a whole, and the body opens with `# <channel> — <title>`.
  Without one they follow the audio rules. An empty slug becomes `youtube-<video id slug>`.
- **De-duplication is unchanged.** Two links to one video carry one guid.

<!-- contract-v2:end -->
