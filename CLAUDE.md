# earmark

Self-hosted "share a podcast episode → get a research note" pipeline. An iOS Shortcut
POSTs an Apple Podcasts episode URL (plus an optional analysis prompt) to a small API on
the homelab. A worker resolves the audio, transcribes it locally, analyzes the transcript
with an LLM, and writes a Markdown note plus a searchable index into a personal knowledge
vault.

## Status

Being built step by step from `docs/PLAN.md`. Check the status table there before
assuming a module exists. Until step 01 lands there is no code and the commands below do
not work yet.

- `docs/ARCHITECTURE.md`: the design. Decisions, module layout, contracts, data layout,
  config, security rules. This is the source of truth for how pieces fit; update it in
  the same change whenever a contract changes.
- `docs/PLAN.md`: the ordered steps, their acceptance criteria, and the build loop.
- `docs/steps/NN-<slug>.md`: the detailed plan written for each step when it is dispatched.

## Build loop

Each step runs as plan → implement → verify, with a separate agent per role:
`step-planner` (Opus), `step-implementer` (Sonnet), `step-verifier` (Sonnet), defined in
`.claude/agents/`. `/step NN` runs the loop from step NN onward (`--one` for a single
step); the protocol is in `docs/PLAN.md`. One commit per verified step on the `impl`
branch, pushed by `/step` once verified, with CI green before the next step starts. Do
not push otherwise without being asked.

## Commands

```bash
uv sync                      # install (add --extra transcribe / --extra index for heavy backends)
make check                   # the gate: ruff check + ruff format --check + mypy + pytest
uv run pytest tests/test_jobs.py -k claim     # one test
uv run pytest -m network     # opt-in tests (markers: network, slow, llm, gpu)
uv run earmark serve         # ingest API
uv run earmark worker        # job runner (--once to drain one job)
uv run earmark doctor        # check config and backends
```

`make check` must pass before a step is called done. Do not loosen lint, type, or test
configuration to make it pass.

## Shape of the code

```
POST /ingest ─► jobs table (SQLite) ─► worker ─► Pipeline
  resolve ─► fetch ─► transcribe ─► analyze ─► store ─► index ─► notify
```

- Every stage is a `Protocol` in `earmark/pipeline/stages.py` with a real backend and a
  fake in `earmark/fakes.py`. Backends are chosen by config in
  `earmark/pipeline/factory.py`, the only place that constructs them.
- Every stage is idempotent and cache-aware: outputs live under
  `data/episodes/<episode_key>/`, and a stage whose output already exists returns it
  without redoing the work. Retries and re-shares rely on this.
- The Markdown vault is the system of record. SQLite and the vector index are derived
  and must be rebuildable from the artifacts on disk.

## Rules that are easy to get wrong

- **Tests run with no network, GPU, or API key.** Sockets are blocked in the default
  suite. Use recorded fixtures and the fakes; put anything live behind a marker.
- **Heavy dependencies are optional extras, imported lazily** (faster-whisper, lancedb,
  fastembed). `import earmark` and the default test suite must work without them.
- **Errors carry retry intent.** Raise `TransientError` (retry with backoff),
  `PermanentError` (fail now), or `SkipEpisode` (e.g. subscriber-only episode, no public
  feed). The worker treats anything else as transient.
- **Feeds, enclosure URLs, titles, and transcripts are untrusted.** All third-party
  fetches go through `earmark/fetch.py` (scheme check, private-address block, size cap).
  Anything that becomes a filename or YAML goes through the vault sanitizers. Earmark
  writes only inside `<vault>/<subdir>/` and only inside its managed-region markers.
- **Never log or print the API token or LLM keys.** `Settings` holds them as secrets.
- **Never truncate a transcript to fit a model.** Chunk it or fail loudly.
- **Claude calls use the official `anthropic` SDK.** Load the `claude-api` skill before
  writing or changing them; model ids and request shapes drift.

## Environment

This sandbox is aarch64 Linux with Python 3.13 and uv, and has no GPU, Docker, or ffmpeg.
The deployment target is a homelab with an NVIDIA GPU (DGX Spark, also aarch64). GPU
transcription and container builds cannot be verified here; say so rather than claiming
they work.
