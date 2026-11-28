# The /v1 API

This file is the contract of earmark's `/v1` API, which programs on the operator's hardware
use to have audio transcribed without any mail or push: upload audio, fetch the transcript
by job id, delete it. Version 1.1 (step 37, 2026-11-28) added summaries on the operator's
local model, additively. It was written in step 36 (2026-11-28) for earshot, the operator's
meeting transcriber, which lives outside this repo. The text between the two markers below is
the contract; a test pins it by hash. `/v1` changes only additively (new optional fields, new
routes, new values of an informational field). A breaking change is `/v2`, with its own file.

<!-- api-v1:begin -->

## 1. Version and transport

This is version 1 of the API, under the path prefix `/v1`.

- **Authentication.** Every request carries `Authorization: Bearer <token>`. Without it, or
  with a wrong token, the answer is 401 with `WWW-Authenticate: Bearer realm="earmark"`, and
  the body is not read.
- **Reaching the service.** It listens on loopback. A program on another machine reaches it
  over an SSH tunnel to `127.0.0.1:8787`, or through the operator's TLS proxy.
- **Bodies.** Request and response bodies are UTF-8. Every error is
  `{"detail": "<message>"}`.
- **Unknown fields.** A client must ignore fields it does not know in a 200 or 202 body. That
  is what "additive" means here.
- **Nothing is sent anywhere.** A `/v1` job sends no mail and no push. Its audio and
  transcript stay on the service's host, unless the operator chose a remote ASR server, which
  then receives the audio and the `initial_prompt`. A summary job sends its transcript and
  instructions only to the operator's local model server (section 3).
- **Summaries are local.** They are made by a model server that the operator runs on the
  service's host or its private network, never by a cloud model.

## 2. POST /v1/transcriptions

Uploads one audio file and queues a transcription. The body is `multipart/form-data` only;
any other content type, JSON included, is 415.

| Field | Required | Meaning |
|---|---|---|
| `file` | yes, exactly one | The audio, with an audio content type (`audio/*`) or a file name ending in an audio extension such as `.flac`, `.m4a` or `.mp3`. |
| `initial_prompt` | no | Text that primes the decoder with names and terms. At most **1000 characters** after surrounding whitespace is stripped; longer is 422. Empty means none. |
| `language` | no | One of the 100 Whisper language codes listed in section 11, in any case. Empty or absent means the language is detected. The server's own language setting does not apply to `/v1`. |

Any other field, `to` and `title` included, is a 422. The size cap is the server's
`EARMARK_MAX_UPLOAD_MB` (1024 MiB unless the operator changed it); the 413's `detail` names
it.

The answer is 202 `{"job_id", "status"}` with `Location: /v1/jobs/<job_id>`. A request
identical to one that is queued, running, or done within the server's dedupe window (600 s
unless the operator changed it) returns that job's id instead of a new one. Identical means
the same audio bytes, the same `initial_prompt` and the same `language`. A job that was
deleted is never returned. Sending the same request again after the window makes a new job,
which reuses the cached transcript when the audio and options match.

The file's name is used to check that it is audio and is not stored.

## 3. POST /v1/summaries

Queues one summary of a transcript on the operator's local model. The body is JSON only
(`Content-Type: application/json`); anything else is 415.

| Field | Required | Meaning |
|---|---|---|
| `transcript` | yes | The text to summarise. It is sent to the model as the user message, verbatim. |
| `instructions` | yes | What the summary should be. It is sent as the system message, verbatim. |

Both are non-blank strings, and any other field is a 422. Neither is stripped or changed.

- **Caps.** `instructions` is at most **20000 characters**. `transcript` is at most the
  server's `EARMARK_LLM_MAX_TRANSCRIPT_CHARS`, **400000 characters** unless the operator
  changed it, which is about a 4-hour meeting. Characters are Unicode code points. The 422
  names the cap. A body larger than 12 times the two caps is 413 before it is parsed.
- **Off.** When the operator has not configured a model server (`EARMARK_LLM_BACKEND`), the
  answer is 503 and the body is not read.
- **The answer** is 202 `{"job_id", "status"}` with `Location: /v1/jobs/<job_id>`. A request
  identical to one that is queued, running, or done within the server's dedupe window (600 s
  unless the operator changed it) returns that job's id instead of a new one. Identical means
  the same transcript and the same instructions. A job that failed or was deleted is never
  returned.
- **The input is deleted when the job ends.** The transcript and instructions wait on the
  service's disk only while the job is queued or running.
- **One call.** A summary is one model call. earmark does not chunk a transcript, so a client
  that has more text than fits must split it and combine the parts itself.

## 4. The prompt and Whisper's window

Whisper conditions on at most the last 223 tokens of the prompt and ignores what comes
before. 1000 characters is about 223 tokens of English names or plain prose, and more than
that for technical terms (about 630 characters fill the window) and for Chinese or Japanese
text (about 140). Put the most important terms last.

How far into the audio the prompt reaches depends on the server's decoding mode: with
sequential decoding it conditions the first windows and then the transcript's own text takes
over; with batched decoding it is applied to every chunk. The prompt is a hint, not a
guarantee.

## 5. GET /v1/jobs/{job_id}

Answers 200 with the job:

| Field | Meaning |
|---|---|
| `job_id` | The id. |
| `kind` | `transcription` or `summary`. New values may be added; a client must not assume only two. |
| `status` | `queued`, `running`, `done`, `failed` or `skipped`. |
| `stage` | Where the job is or stopped: informational, `null` or a string; new values may be added. A summary job's stage is `summarize`. |
| `error` | Why a job failed or was skipped, or `null`. |
| `created_at`, `updated_at` | ISO 8601 times, UTC. |
| `language` | The requested language until the job is done, then the transcript's language; `null` while the language is detected and unknown. Always `null` for a summary. |
| `duration_s` | The audio's length in seconds once the job is done, else `null`. Always `null` for a summary. |
| `purged_at` | When the job was deleted, else `null`. |

The answer is 404 for an id this API did not create, and the response carries
`Cache-Control: no-store`. A job that fails for a transient reason is retried by the service
with a delay; its `status` goes back to `queued` between attempts.

## 6. GET /v1/jobs/{job_id}/transcript

`?format=json` (the default) or `?format=text`; anything else is 422. A 200 carries
`Cache-Control: no-store`, and a fetch can be repeated until the job is deleted.

- `json`: `{"job_id", "language", "duration_s", "segments", "warnings"}`, where `segments`
  is a list of `{"start", "end", "text"}` in seconds. Segments are in order, `start` never
  decreases and `end >= start`. `warnings` is a list of strings.
- `text`: `text/plain; charset=utf-8`, one `[HH:MM:SS] text` line per segment.

Other states: 404 for an unknown job; 409 when the job is queued, running, failed or
skipped; 410 when it was deleted or its transcript is gone; 500 when the stored transcript
cannot be read. A summary job answers 409 here, naming the summary route. Section 10 lists the
`detail` of each.

## 7. GET /v1/jobs/{job_id}/summary

A 200 carries `Cache-Control: no-store`, and a fetch can be repeated until the job is
deleted. The body is `{"job_id", "model", "text", "warnings"}`:

- `text` is the model's Markdown exactly as the server returned it. It is untrusted text
  that earmark does not interpret: it may contain anything the model wrote.
- `model` is the operator's model name.
- `warnings` is a list of strings.

Other states: 404 for an unknown job; 409 when the job is queued, running, failed or skipped,
or is a transcription job; 410 when it was deleted or its summary is gone; 500 when the stored
summary cannot be read. Section 10 lists the `detail` of each.

## 8. When a summary job fails

A summary job is `done` only when the model server provably read the whole input and the
model stopped on its own. Otherwise it fails with an `error` that earmark wrote, and nothing
is stored. No summary of part of the input is ever stored or served. The texts a client may
act on:

- Every input-fit failure starts with
  `the transcript and instructions do not fit the model's context`. A transcript under the
  character cap can still be too long for the model; the job then fails, and the client
  should send less.
- A summary that the model did not finish starts with `the summary was cut off`.
- A worker with no model server configured says
  `summaries are not configured on the worker (EARMARK_LLM_BACKEND is none)`.

## 9. DELETE /v1/jobs/{job_id}

Deletes a finished job's data: the transcript files, any staged upload, and the stored
`initial_prompt`. It answers 200 with the job as `GET` shows it, now with `purged_at` set.
A repeated `DELETE` answers 200 with the same job, so a client can retry after a lost answer.

- **Shared transcripts.** The transcript is shared by every job with the same audio, the same
  `initial_prompt` and the same `language`. Deleting one deletes the transcript for all of
  them, and every finished job that shared it becomes deleted too. A job of that kind that is
  still queued or running is left alone; if it had already found the transcript it finishes
  and its transcript is then 410, otherwise it makes its own.
- **Active jobs.** A queued or running job is refused with 409. There is no cancel.
- **Summaries.** A summary job's summary file and any input left are removed. A summary is
  never shared between jobs, so deleting one deletes only that job.
- **What remains.** The job's row: its id, status, stage, error, times, language, duration,
  `purged_at`, and a hash of the audio.
- **Not shredding.** Files are unlinked and the database overwrites freed space, but
  filesystem blocks and backups of the server's data directory are outside earmark's reach.

## 10. Errors

Every error is `{"detail": "<message>"}`. Text in angle brackets varies.

| Status | `detail` | When |
|---|---|---|
| 401 | `missing or invalid API token` | Any route, no or wrong token |
| 404 | `job not found` | Unknown job, or a job this API did not create |
| 409 | `this job has no transcript yet; it is <status>` | Transcript of a queued or running job |
| 409 | `this job has no transcript yet; it is <status> (stage: <stage>)` | The same, once a stage is set |
| 409 | `this job has no transcript; it failed` | Transcript of a failed job |
| 409 | `this job has no transcript; it was skipped` | Transcript of a skipped job |
| 409 | `this job is still <status>; delete it once it has finished` | DELETE of a queued or running job |
| 409 | `this job has no summary yet; it is <status>` | Summary of a queued or running job |
| 409 | `this job has no summary yet; it is <status> (stage: <stage>)` | The same, once a stage is set |
| 409 | `this job has no summary; it failed` | Summary of a failed job |
| 409 | `this job has no summary; it was skipped` | Summary of a skipped job |
| 409 | `this job is a transcription; fetch its transcript from /v1/jobs/{job_id}/transcript` | The summary route on a transcription job |
| 409 | `this job is a summary; fetch it from /v1/jobs/{job_id}/summary` | The transcript route on a summary job |
| 410 | `this job was deleted; its transcript is gone` | Transcript of a deleted job |
| 410 | `the transcript of this job is gone; send the audio again` | Its files are missing |
| 410 | `this job was deleted; its summary is gone` | Summary of a deleted job |
| 410 | `the summary of this job is gone; send the transcript again` | Its file is missing |
| 413 | `the upload is larger than the <limit> limit (EARMARK_MAX_UPLOAD_MB)` | The file is over the cap |
| 413 | `the request body is larger than <limit>, the most a transcript at the server's cap can take (EARMARK_LLM_MAX_TRANSCRIPT_CHARS)` | A summary body over 12 times the caps |
| 415 | `send a multipart form (Content-Type: multipart/form-data) with the audio in a field named 'file'` | Not multipart |
| 415 | `send JSON (Content-Type: application/json) with the fields transcript and instructions` | A summary request that is not JSON |
| 415 | `the file does not look like audio: send it with an audio content type such as audio/mpeg, or a file name ending in an audio extension such as .mp3 or .m4a` | Not audio |
| 422 | `unknown field '<name>' (allowed: file, initial_prompt, language)` | Any other field of a transcription |
| 422 | `unknown field '<name>' (allowed: transcript, instructions)` | Any other field of a summary |
| 422 | `the form must have exactly one file field named 'file'` | No file, or two |
| 422 | `the uploaded file is empty` | An empty file |
| 422 | `the multipart form is malformed` | A body that does not parse |
| 422 | `the multipart form has no boundary` | No boundary in the content type |
| 422 | `the multipart form is incomplete` | A truncated body |
| 422 | `the form has too many parts` | More than 8 parts |
| 422 | `field '<name>' is given more than once` | A repeated field |
| 422 | `field '<name>' must be a text field` | A text field sent as a file |
| 422 | `field '<name>' is too long` | A text field over its byte cap |
| 422 | `field '<name>' is not valid UTF-8` | A text field that is not UTF-8 |
| 422 | `initial_prompt is too long (at most 1000 characters)` | A prompt over the cap |
| 422 | `language must be a Whisper language code such as 'en'; leave it out to detect the language` | An unknown language |
| 422 | `format: Input should be 'json' or 'text'` | Another transcript format |
| 422 | `request body is missing; send a JSON object with transcript and instructions` | An empty summary body |
| 422 | `request body is not valid JSON` | A summary body that is not JSON |
| 422 | `request body must be a JSON object sent with Content-Type: application/json` | A summary body that is not an object |
| 422 | `transcript is required` | No `transcript` |
| 422 | `instructions is required` | No `instructions` |
| 422 | `transcript must not be empty` | A blank transcript |
| 422 | `instructions must not be empty` | Blank instructions |
| 422 | `instructions is too long (at most 20000 characters)` | Instructions over the cap |
| 422 | `transcript is too long (at most <n> characters on this server; EARMARK_LLM_MAX_TRANSCRIPT_CHARS)` | A transcript over the server's cap |
| 500 | `the transcript of this job is unreadable; send the audio again` | A stored transcript that does not parse |
| 500 | `the summary of this job is unreadable; send the transcript again` | A stored summary that does not parse |
| 500 | `could not delete the transcript files; try again` | DELETE could not remove a file |
| 500 | `could not delete the summary files; try again` | DELETE could not remove a file of a summary job |
| 503 | `summaries are not configured on this server (EARMARK_LLM_BACKEND)` | `POST /v1/summaries` with no model server |
| 503 | `the job store is unavailable; try again shortly` | With `Retry-After: 5` |

## 11. The language codes

`language` is one of these 100 codes (a code is case-insensitive in a request):

<!-- api-v1-languages -->

```text
af am ar as az ba be bg bn bo br bs ca cs cy da de el en es et eu fa fi fo fr gl gu ha haw
he hi hr ht hu hy id is it ja jw ka kk km kn ko la lb ln lo lt lv mg mi mk ml mn mr ms mt
my ne nl nn no oc pa pl ps pt ro ru sa sd si sk sl sn so sq sr su sv sw ta te tg th tk tl
tr tt uk ur uz vi yi yo zh yue
```

`yue` exists only in the `large-v3` model; with an older model a job asking for it fails.

## 12. Examples

Each block up to `DELETE` is the exact response of a job of 64 bytes of audio uploaded as
`meeting.flac` with `language=en` and no prompt. The summary examples follow them.

`POST /v1/transcriptions`, 202:

<!-- api-v1-example:accepted -->

```json
{ "job_id": "3f9c2a7e5b1d4c8e9a6f0b2d4e6a8c01", "status": "queued" }
```

`GET /v1/jobs/{job_id}` right after:

<!-- api-v1-example:job-queued -->

```json
{
  "job_id": "3f9c2a7e5b1d4c8e9a6f0b2d4e6a8c01",
  "kind": "transcription",
  "status": "queued",
  "stage": null,
  "error": null,
  "created_at": "2026-11-28T09:00:00Z",
  "updated_at": "2026-11-28T09:00:00Z",
  "language": "en",
  "duration_s": null,
  "purged_at": null
}
```

`GET /v1/jobs/{job_id}/transcript` before the job has run, 409:

<!-- api-v1-example:error -->

```json
{ "detail": "this job has no transcript yet; it is queued" }
```

`GET /v1/jobs/{job_id}` once done:

<!-- api-v1-example:job-done -->

```json
{
  "job_id": "3f9c2a7e5b1d4c8e9a6f0b2d4e6a8c01",
  "kind": "transcription",
  "status": "done",
  "stage": "transcribe",
  "error": null,
  "created_at": "2026-11-28T09:00:00Z",
  "updated_at": "2026-11-28T09:10:05Z",
  "language": "en",
  "duration_s": 9.8,
  "purged_at": null
}
```

`GET /v1/jobs/{job_id}/transcript?format=json`:

<!-- api-v1-example:transcript-json -->

```json
{
  "job_id": "3f9c2a7e5b1d4c8e9a6f0b2d4e6a8c01",
  "language": "en",
  "duration_s": 9.8,
  "segments": [
    { "start": 0.0, "end": 4.2, "text": "Good morning, everyone." },
    { "start": 4.2, "end": 9.8, "text": "Let's start with the roadmap." }
  ],
  "warnings": []
}
```

`GET /v1/jobs/{job_id}/transcript?format=text`:

<!-- api-v1-example:transcript-text -->

```text
[00:00:00] Good morning, everyone.
[00:00:04] Let's start with the roadmap.
```

`DELETE /v1/jobs/{job_id}`:

<!-- api-v1-example:deleted -->

```json
{
  "job_id": "3f9c2a7e5b1d4c8e9a6f0b2d4e6a8c01",
  "kind": "transcription",
  "status": "done",
  "stage": "transcribe",
  "error": null,
  "created_at": "2026-11-28T09:00:00Z",
  "updated_at": "2026-11-28T09:11:05Z",
  "language": "en",
  "duration_s": 9.8,
  "purged_at": "2026-11-28T09:11:05Z"
}
```

`POST /v1/summaries` with the transcript `[00:00:00] **Alice:** Good morning, everyone.` and
the instructions `Summarise the decisions as a Markdown list.`, 202:

<!-- api-v1-example:summary-accepted -->

```json
{ "job_id": "7b1e4d9a2c3f4e5a8b6c0d1e2f3a4b5c", "status": "queued" }
```

`GET /v1/jobs/{job_id}/summary` before the job has run, 409:

<!-- api-v1-example:summary-error -->

```json
{ "detail": "this job has no summary yet; it is queued" }
```

`GET /v1/jobs/{job_id}` of the summary job once done:

<!-- api-v1-example:summary-job-done -->

```json
{
  "job_id": "7b1e4d9a2c3f4e5a8b6c0d1e2f3a4b5c",
  "kind": "summary",
  "status": "done",
  "stage": "summarize",
  "error": null,
  "created_at": "2026-11-28T10:00:00Z",
  "updated_at": "2026-11-28T10:02:05Z",
  "language": null,
  "duration_s": null,
  "purged_at": null
}
```

`GET /v1/jobs/{job_id}/summary`:

<!-- api-v1-example:summary -->

```json
{
  "job_id": "7b1e4d9a2c3f4e5a8b6c0d1e2f3a4b5c",
  "model": "example-model",
  "text": "## Decisions\n\n- Ship the roadmap in March.\n",
  "warnings": []
}
```

## 13. Revisions

- **1.0** (step 36, 2026-11-28): transcriptions.
- **1.1** (step 37, 2026-11-28): `kind`, the summary routes, the caps and section 8.
  Additive.

<!-- api-v1:end -->
