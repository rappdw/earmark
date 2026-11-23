# earmark

Self-hosted transcription and delivery service on the homelab's DGX Spark. An iOS
Shortcut sends it a podcast episode or a piece of audio, a worker transcribes it, and the
transcript goes to the operator's PKA instances, which do the analysis and filing, each
with its own context. earmark itself files nothing.

Today (after step 22) a request is an Apple Podcasts episode link. The worker resolves
the audio, transcribes it, caches the transcript, and sends an ntfy push. Steps 24–26 add
uploads and direct audio URLs with a destination address and instructions, delivery by
email as two Markdown attachments, and GPU transcription inside the image. Their
contracts are in "Planned (steps 24–26)" in `docs/ARCHITECTURE.md`.

## Status

Being built step by step from `docs/PLAN.md`. Check the status table there before
assuming a module exists.

- `docs/ARCHITECTURE.md`: the design. Decisions, module layout, contracts, data layout,
  config, security rules. This is the source of truth for how pieces fit; update it in
  the same change whenever a contract changes. Its "Planned (steps 24–26)" section holds
  what those steps build against, including the operator's decisions and the mail
  contract.
- `docs/PLAN.md`: the ordered steps, their acceptance criteria, and the build loop. Its
  "Re-scope" section is the record of the operator's decisions and the mail contract.
- `docs/steps/NN-<slug>.md`: the detailed plan written for each step when it is dispatched.

## Build loop

Each step runs as plan → implement → verify, with a separate agent per role:
`step-planner` (Opus), `step-implementer` (Sonnet), `step-verifier` (Sonnet), defined in
`.claude/agents/`. `/step NN` runs the loop from step NN onward (`--one` for a single
step); the protocol is in `docs/PLAN.md`. One commit per verified step on the `impl`
branch, pushed by `/step` once verified, with CI green before the next step starts; on
green, `main` is fast-forwarded to that commit. Do not push otherwise without being asked.

## Commands

```bash
uv sync                      # install (add --extra transcribe for local Whisper)
make check                   # the gate: ruff check + ruff format --check + mypy + pytest
uv run pytest tests/test_jobs.py -k claim     # one test
uv run pytest -m network     # opt-in tests (markers: network, slow, gpu); publishes to ntfy.sh
uv run earmark serve         # the API
uv run earmark worker        # job runner (--once to drain one job)
uv run earmark doctor        # check config and backends
```

`make check` must pass before a step is called done. Do not loosen lint, type, or test
configuration to make it pass.

## Shape of the code

```
POST /transcriptions ─► jobs table (SQLite) ─► worker ─► Pipeline
  resolve ─► fetch ─► transcribe ─► notify
```

Planned: email delivery runs between transcribe and notify (step 25).

- Every stage is a `Protocol` in `earmark/pipeline/stages.py` with a real backend and a
  fake in `earmark/fakes.py`. Backends are chosen by config in
  `earmark/pipeline/factory.py`, the only place that constructs them.
- Every stage is idempotent and cache-aware: outputs live under
  `data/episodes/<episode_key>/`, and a stage whose output already exists returns it
  without redoing the work. Retries and re-shares rely on this.
- earmark is not a system of record. Its state is the job table and the transcript cache
  under `EARMARK_DATA_DIR`. A database from `v0.1.0-full-pipeline` is refused at startup,
  never migrated.

## Rules that are easy to get wrong

- **Tests run with no network, GPU, or credentials.** Sockets are blocked in the default
  suite. Use recorded fixtures, stubs on unix sockets, and the fakes; put anything live
  behind a marker.
- **Heavy dependencies are optional extras, imported lazily** (faster-whisper and
  CTranslate2, the `transcribe` extra). `import earmark` and the default test suite must
  work without them.
- **Errors carry retry intent.** Raise `TransientError` (retry with backoff),
  `PermanentError` (fail now), or `SkipEpisode` (e.g. subscriber-only episode, no public
  feed). The worker treats anything else as transient.
- **Feeds, enclosure URLs, titles, and transcripts are untrusted**, and from step 24 so
  are client-supplied URLs, uploads, their file names, titles, and instructions. All
  third-party fetches go through `earmark/fetch.py` (scheme check, private-address block,
  size cap); it is also the only path to a direct audio URL, and the resolver takes only
  Apple Podcasts episode links. Anything that becomes a file name or YAML goes through
  `earmark/sanitize.py`.
- **Never log or print a secret**: the API token, the ASR key, the ntfy URL and token,
  and from step 25 the SMTP password. `Settings` holds them as `SecretStr`.
- **Recipients and instructions must not leak** (step 24). Instructions never appear in
  logs or pushes. An address appears in logs and pushes only masked (`j…@example.org`).
  A direct URL appears in logs and pushes host-only.
- **The recipient allowlist is the gate** (step 24). Every `to` is checked against
  `EARMARK_ALLOWED_RECIPIENTS`. Unset refuses every request. `all` is the operator's
  written acceptance of a risk, warned about at startup and by doctor. earmark never
  defaults the recipient or the sender.
- **Mail follows the contract exactly** (step 25): pka-skills `contract: 1`, a draft,
  copied in `docs/ARCHITECTURE.md`. A change to a key, file name, format, or rule is a
  contract revision with a version bump, made in `docs/PLAN.md` and
  `docs/ARCHITECTURE.md` together, never a quiet edit. Delivery is at-least-once: a
  resend keeps its `Message-ID` and `job_id`.
- **Untrusted text in mail** (step 25). The body carries only text earmark generates.
  The subject carries the sanitised title, and the body says that the subject and the
  note contain third-party text. A title, guid, or instructions text can never inject a
  header. In the email, the requester's instructions appear only in the instructions
  attachment, verbatim, and the guid appears only inside the attachments.
- **Never truncate a transcript.** A transcript over the attachment cap fails the job
  with a message naming the setting; nothing is cut or split.
- **Removed code stays removed.** What step 22 deleted is recoverable from the
  `v0.1.0-full-pipeline` tag. Bring nothing back without a plan step.

## Environment

This sandbox is aarch64 Linux with Python 3.13 and uv, and has no GPU, Docker, or ffmpeg.
The deployment target is a homelab with an NVIDIA GPU (DGX Spark, also aarch64). GPU
transcription and container builds cannot be verified here; say so rather than claiming
they work. The GPU image of step 26 is checked only by the operator's smoke test on the
Spark.
