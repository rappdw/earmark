# Step 37: Summaries on a local model: /v1/summaries

The brief is in `docs/PLAN.md`, "Step 37". It builds on step 36 (the `/v1` API, delivery-free
jobs, the purge, `contract/api-v1.md`), step 24 (staging and dedupe), step 09 (the rules for a
client of an operator-run endpoint) and step 11 (the local LLM research). Step 11's code was
removed in step 22. Only the shape of its OpenAI-compatible client comes back, rewritten here
(`git show v0.1.0-full-pipeline:src/earmark/analyze/llm/openai_compat.py`). Nothing else from
the tag returns: no analyzer, no schema, no prompts, no map-reduce, no Claude backend.

**Split by the operator (2026-11-28): pass A is step 37a and pass B is step 37b,** each
implemented, verified and committed on its own (see `docs/PLAN.md`, "Step 37"). Where this
plan says "the verifier checks the whole step once", read: 37a's verifier checks phases 1–4
and the criteria they meet, and 37b's checks phases 5–7 and the rest.

**The step can be built as briefed.** It is larger than step 36, so it is planned as **one step
with two implementer passes** on a clean seam (section 3):

- **Pass A** (phases 1–4) covers summary jobs end to end on a fake summarizer: the models, the
  migration, the store, the runner, the routes, the purge, the contract, the offline doctor
  check and the docs.
- **Pass B** (phases 5–7) covers the three real server clients, the local-only transport, the
  per-server fit checks, the online doctor check, and the stub-server tests.

The orchestrator can run one implementer for both passes. If the first runs out of room, it
can stop after pass A (`make check` green at that point) and run a second implementer for
pass B. The verifier checks the whole step once. If the orchestrator would rather have two
commits, the same seam makes 37a and 37b; that needs a `docs/PLAN.md` status row each, and it
is the orchestrator's call.

## Decisions the user should know about

The brief leaves these to the planner. Each changes what a client or the operator sees.

- **P1. Summary jobs are rows in the same `jobs` table.** They have `kind = 'summary'`, and
  their input is staged as `uploads/<id>.json` (`source = 'upload'`, `delivery = 'none'`).
  This reuses the claim, the lease, the heartbeat, retries, `requeue_stale`, shutdown
  release, the staging sweeps and the purge. The `kind` `CHECK` needs a table rebuild
  (migration `0006_summaries`, as `0004` did), so the schema becomes version 6 and an older
  image refuses the database.
- **P2. The input is deleted when the job ends; the summary is kept until `DELETE`.** The
  transcript and the instructions sit in `uploads/<id>.json` (mode 0600) only while the job
  is queued or running, exactly like a staged upload. They never enter the database.
  `summaries/<job_id>/summary.json` holds the output until the client deletes the job.
- **P3. Caps.**
  - `instructions`: at most **20 000 characters**, fixed in the contract.
  - `transcript`: at most `EARMARK_LLM_MAX_TRANSCRIPT_CHARS`. The default is **400 000
    characters**, about a 4-hour, 60 000-word meeting (section 2.5), sized for a 131 072-token
    context.
  - Both are Unicode code points. A 422 states the cap.
  - The request body is also capped in bytes at 12 × (both caps) + 64 KiB, which is JSON's
    worst case for escapes. Over that is a 413.
- **P4. The server type is a setting.** `EARMARK_LLM_BACKEND` is `none` (the default: off,
  and `POST /v1/summaries` answers 503), `ollama`, `llama_cpp`, `vllm` or `fake`. Each type
  has its own way to prove that the whole input was read (section 2), so earmark must know
  which server it is talking to. The name follows the `*_BACKEND` convention. It is
  deliberately not step 11's `EARMARK_LLM_PROVIDER`, so a leftover v0.1.0 `.env` turns
  nothing on.
- **P5. Ollama is called through its native `/api/chat`, not `/v1/chat/completions`.** This
  is a deviation from the brief's "OpenAI-compatible". The reason is in section 2.2: Ollama's
  OpenAI route has no `truncate`, `shift` or `num_ctx`. It cuts the middle out of an over-long
  prompt and leaves only a server log line, and nothing in the response shows it. The native
  route with `truncate: false`, `shift: false` and `options.num_ctx` refuses instead, from
  Ollama **0.40.17**, which earmark checks on every job. llama-server and vLLM are called on
  `/v1/chat/completions`.
- **P6. A summary cut off at the output limit fails the job.** If the model stops at
  `EARMARK_LLM_MAX_OUTPUT_TOKENS` (default **8192**), or at the end of its context, before it
  finishes, the job fails and no partial summary is stored. The message names the setting.
- **P7. Dedupe as for `/v1` transcriptions.** An identical request (the same transcript and
  instructions) that is queued, running, or done within `EARMARK_DEDUPE_WINDOW_S` returns that
  job, so a POST retried after a lost 202 does not run the model twice. A client that wants a
  fresh summary of the same text must wait out the window or delete the job.
- **P8. The text is served exactly as the model returned it.** earmark does not strip a
  `<think>` block or a code fence (unlike step 11). The operator runs the server with the
  model's reasoning parser, so thinking arrives in a separate field, which earmark ignores and
  never stores.
- **P9. The model server's error text is never copied** into a job, a log or a response. A
  server can echo its input. earmark maps status codes, and the known context-error shapes
  (from which it parses integers only), to its own messages.
- **P10. Asking for the wrong resource is a 409.** The summary route on a transcription job,
  or the transcript route on a summary job, answers 409 with a `detail` naming the right route.
  It is not a 404, because the job exists.
- **P11. A read timeout is permanent, not retried.** A generation that outlasts
  `EARMARK_LLM_TIMEOUT_SECONDS` (default 3600 s) would outlast it again, and three retries
  would cost hours. The servers abandon the work when earmark disconnects (section 2.4).
- **P12. Host names.** At settings load, the base URL's host must be one of these, or it is
  refused:
  - an IP literal in 127/8, 10/8, 172.16/12, 192.168/16, `::1` or `fc00::/7`;
  - `localhost`, or a name under `.localhost`;
  - a single-label name, such as a compose service (`ollama`);
  - a name under `.internal` (`host.docker.internal`), `.local`, `.lan` or `.home.arpa`.

  At connect time the worker resolves the name and refuses unless every address is loopback or
  private, then connects to the address it checked. CGNAT (100.64/10, a tailnet), link-local
  and public addresses are refused.
- **P13. A summary job's `language` and `duration_s` are present and `null`**, so `/v1` keeps
  one job shape.
- **P14. `GET /v1/jobs/{id}` gains `kind`** (`transcription` or `summary`), as the brief says.
  The unversioned `JobView` gains `kind` too (`ingest` or `summary`), so `earmark jobs` can
  label a summary.

Rules for the implementer that are not about the code itself:

- **Do not run any `git stash` command.** The operator keeps local work in a stash.
- **Use a `TMPDIR` under `~/.cache/`** for anything that writes temp files:
  `export TMPDIR=/home/sandy/.cache/earmark-step37-tmp && mkdir -p "$TMPDIR"`, and `rm -rf`
  it when done. `/tmp` is a small tmpfs. Never install anything into the repo `.venv`; a
  scratch venv goes under `TMPDIR`. Never run `pytest -m network`.
- **Run both gates:** `make check` and `GITHUB_ACTIONS=true make check`.
- **Placeholders only** in every file this step writes:
  - hosts and addresses at `example.org`, `example.net` or `example.com`
    (`spark.example.com` for the Spark);
  - `127.0.0.1` for loopback and `192.168.1.10` for a LAN address;
  - `ollama` (a compose service name) and `host.docker.internal` for a model server;
  - `<…>` for the token.

  No real domain, LAN address or token. In docs and the contract, the doc tests allow only the
  IPv4 literals `127.0.0.1`, `0.0.0.0`, `1.1.1.1`, `192.168.1.10` and `192.168.1.0`, so write no
  other address there, docker's bridge included. Tests may use the generic addresses of
  section 6.3.
- **`ruff format --check` covers Python code blocks in Markdown.** Keep every
  ```` ```python ```` fence ruff-formatted, or use `bash`, `text` or `json`.
- **The removed-wording scan** (`tests/test_docs.py`, `REMOVED`) still bans these in `README.md`
  and `docs/deploy.md`: `vault`, `\bdigest`, `embedding`, `\bclaude\b`, `anthropic`,
  `--prompt`, `"prompt"`, `analyze`/`analyse` after `earmark `, and step 11's settings. This
  step narrows only the `EARMARK_LLM` and `vllm` bans (section 6.8). Do not write the word
  "digest" in those two files ("hash" is fine).
- **`docs/ARCHITECTURE.md` and `docs/PLAN.md` (the re-scope note) are already updated** by this
  plan (section 8.12). Check the code against them, and change them only where the code had to
  differ, saying so in your report.
- `CLAUDE.md`, `README.md`, `docs/deploy.md`, `.env.example` and `contract/` are yours to change
  (section 4.12).

## 1. Outcome

- **`POST /v1/summaries`** takes JSON `{"transcript", "instructions"}` behind the bearer token.
  Any other field is a 422. Both inputs have caps. It answers 202 `{"job_id", "status"}` with
  `Location: /v1/jobs/{id}`, or 503 when summaries are off. The handler makes no outbound
  request.
- **A summary job** (`kind = 'summary'`, delivery-free) makes one call to the operator's local
  model server, with the instructions as the system message and the transcript as the user
  message.
  - It is `done` only when the server provably read the whole input and the model stopped on
    its own.
  - Otherwise it fails permanently with an earmark message, and nothing is stored.
  - It never mails or pushes. Its logs carry ids, sizes, token counts, timings and error
    classes only.
- **`GET /v1/jobs/{id}`** gains `kind`. **`GET /v1/jobs/{id}/summary`** serves `{job_id,
  model, text, warnings}`, with `text` exactly as the model returned it. **`DELETE`** purges a
  summary job with step 36's tombstone. The transcript and summary routes each refuse the other
  kind with a 409.
- **Backends** `ollama` (native `/api/chat`), `llama_cpp` and `vllm` (`/v1/chat/completions`)
  run behind a local-only HTTP client, each with its own proof that the input fitted. A `fake`
  backend serves tests and smoke runs.
- **Doctor:**
  - offline, it reports the summary settings (off, fake, or the server, model and caps);
  - online, it reports the server's version where it matters, the context window where the
    server exposes it, and whether the model is served.
- **`contract/api-v1.md`** grows additively to version 1.1: `kind`, the summary routes, the
  caps, the failure messages, and examples. The hash pin is updated. A test checks that every
  step-36 example still holds, apart from the added `kind`.
- **Docs:** ARCHITECTURE and PLAN's re-scope note (done here), `CLAUDE.md`, the README,
  `docs/deploy.md` (a new section 21: running a model server next to the GPU worker, its
  context, the memory it costs), `.env.example` and `contract/README.md`.

## 2. Findings

Checked on 2026-11-28. Server sources were read at their latest release tags on GitHub (raw
files fetched into `TMPDIR`, never executed): **Ollama v0.40.19** (2026-11-26), **llama.cpp
v1.1.0** (2026-11-26; daily `bNNNNN` builds are now pre-releases), **vLLM v0.34.0**
(2026-11-16). A research pass read all three. I then re-read the load-bearing parts directly: Ollama's
`api/types.go`, `server/routes.go` and `llm/llama_server.go` (at v0.40.19 and at every v0.40.x
tag), vLLM's context-error text in `renderers/params.py`, and llama-server's
`exceed_context_size_error`, `usage.prompt_tokens` and `input_tokens` handler. Ollama now
runs llama.cpp's `llama-server` as its runner (`llm/llama_server.go`, llama.cpp `b12734`).

### 2.1 How each server reports its context window

| Server | Where | Notes |
|---|---|---|
| llama-server | `GET /props` → `default_generation_settings.n_ctx`, the **per-slot** context; `total_slots` alongside (`server-context.cpp` `get_res_props`). Also `GET /v1/models` → `data[0].meta.n_ctx` (`meta` can be `null` while loading). | With the default `-np -1` (auto, 4 slots) and unified KV, each slot's `n_ctx` is the whole pool, shared. `-c 0` (the default) uses the training context, and `--fit` (on by default) can **lower it to fit memory when `-c` is unset**. The docs say to set `-c` and `-np 1`. |
| vLLM | `GET /v1/models` → `data[].max_model_len`; `POST /tokenize` also returns `max_model_len`. | |
| Ollama | `GET /api/ps` → `models[].context_length`, the runner's actual `NumCtx` (after capping to the training context), only while loaded. `POST /api/show` → `model_info["<arch>.context_length"]`, the **training** context. `/v1/models` has no context. | The default context is `OLLAMA_CONTEXT_LENGTH`, or when unset a VRAM tier (≥ 47 GiB → 262 144, ≥ 23 GiB → 32 768, else 4096), capped to the training context. An auto-sized context that does not fit is **silently stepped down** to 32 768 or 4096. A per-request `options.num_ctx` (what earmark sends) turns auto-sizing off, and is still capped to the training context. |

### 2.2 What each server does with an over-long prompt

- **Ollama, `/v1/chat/completions` (the brief's route).** The request struct has no
  `options`, `num_ctx`, `truncate` or `shift`, and Go's decoder ignores unknown fields
  (`openai/openai.go`). `truncate` and `shift` therefore take their defaults, both true
  (`routes.go`: `req.Truncate == nil || *req.Truncate`).
  - With [system, user], no message can be dropped.
  - `completionPromptForRequest` (`llm/llama_server.go`) keeps the first `num_keep` tokens
    (default 4) and **discards the middle**, down to about half the context.
  - It logs `slog.Warn("truncating input prompt", …)`, and nothing reaches the client.
  - `usage.prompt_tokens` is the count after truncation, and no field signals it. **This
    route cannot be made safe.** That is P5.
- **Ollama, native `/api/chat` with `"truncate": false, "shift": false`** (both fields exist on
  `ChatRequest` in `api/types.go`):
  - If the runner was launched with context shift (the usual case), `requestDisablesShift`
    holds, so Ollama tokenizes the prompt (`noShiftNumPredict`). A prompt of at least `num_ctx`
    tokens is **HTTP 400** with this body:

    ```json
    {
      "error": "the prompt is longer than the context length currently available to the model; shorten the prompt, adjust the context length in settings, or use a model with a longer context length"
    }
    ```

    A shorter prompt gets `num_predict` capped to `num_ctx − prompt`. Generation then stops at
    the context with `done_reason: "length"`, and no shift happens.
  - If the runner was launched without context shift (deepseek2-family models, models with a
    projector), the prompt goes to llama-server unchanged, and llama-server refuses it with its
    own 400 (below).
  - `noShiftNumPredict` and the 400 first appear in **v0.40.17** (2026-11-10). I checked
    `llm/llama_server.go` at every tag from v0.40.1 to v0.40.18: absent through v0.40.16,
    present from v0.40.17. The v0.40.17 release note reads "Changing the context shift setting
    no longer reloads the model". earmark therefore requires `GET /api/version` ≥ 0.40.17 on
    every job (P5). Older code was not analysed.
  - `options.num_ctx` and `options.num_predict` are honoured per request. The response has
    `message.content`, `message.thinking` (a reasoning model's thinking, ignored),
    `done_reason` (`stop` or `length`), `prompt_eval_count` (cached plus new prompt tokens),
    `prompt_eval_cached_count` and `eval_count`.
  - Ollama has **no tokenize endpoint**.
- **llama-server v1.1.0.** A prompt of at least the slot's `n_ctx` is **HTTP 400**:

  ```json
  {
    "error": {
      "code": 400,
      "message": "request (N tokens) exceeds the available context size (M tokens), try increasing it",
      "type": "exceed_context_size_error",
      "n_prompt_tokens": 120000,
      "n_ctx": 65536
    }
  }
  ```

  - There is no prompt-truncation code path at v1.1.0. Older builds truncated; I did not find
    the commit that removed it.
  - **Context shift is off by default.** Generation that reaches `n_ctx` stops with
    `finish_reason: "length"`.
  - With `--context-shift`, it **silently discards** half of what follows `n_keep` and logs
    "slot context shift". Then prompt plus completion tokens exceed `n_ctx`, which is how earmark
    detects it (section 5.4).
  - `prompt + max_tokens > n_ctx` is not refused up front.
  - `usage.prompt_tokens` is the full prompt token count, cache included
    (`server-task.cpp`: `{"prompt_tokens", n_prompt_tokens}`).
  - **`POST /v1/chat/completions/input_tokens`** takes the same body as a chat completion, runs
    the same template and tokenization, and returns `{"input_tokens": N}`
    (`server-context.cpp` `post_chat_completions_tok`, registered in `server.cpp`). That is
    the exact count earmark compares against.
- **vLLM v0.34.0.** A prompt over `max_model_len` is **HTTP 400**:

  ```json
  {
    "error": {
      "message": "This model's maximum context length is X tokens. However, your prompt contains N input tokens, which exceeds this limit even without any output tokens. Please reduce the length of the input prompt.",
      "type": "BadRequestError",
      "param": "input_tokens",
      "code": 400
    }
  }
  ```

  - A character pre-check can fail earlier with "…your prompt contains N characters (more
    than …)" (`renderers/params.py`).
  - Since v0.32.0, `prompt + max_tokens > max_model_len` is no longer refused: `max_tokens` is
    silently capped and the answer ends with `length`.
  - `truncate_prompt_tokens` is opt-in, and earmark never sends it.
  - `POST /tokenize` (at the root, not under `/v1`) with `{"model", "messages",
    "add_generation_prompt": true}` applies the chat template and returns `{"count",
    "max_model_len", "tokens", "token_strs"}`.
  - `usage.prompt_tokens` is the prompt's length.

### 2.3 The reliable check, per server

These rules follow from 2.1 and 2.2. Section 5.4 has the exact algorithm.

- **llama_cpp and vllm (the same code, two probes).**
  1. Before generating, read the context (`/props`, or `/tokenize`'s `max_model_len`) and the
     prompt's exact token count `N` (`/v1/chat/completions/input_tokens`, or `/tokenize`).
     Refuse when `N + max_tokens > context`, before any generation.
  2. Then call `/v1/chat/completions`.
  3. A 400 context error is refused. On a 200, require all three:
     - `usage.prompt_tokens ≥ N − 8`, so the server read the whole prompt (whatever its
       version);
     - `prompt_tokens + completion_tokens ≤ context`, so there was no context shift;
     - `finish_reason == "stop"`.

  The 8-token slack absorbs a BOS or template difference between the two counts. A truncation
  removes thousands of tokens.
- **ollama.**
  1. Require `GET /api/version` ≥ 0.40.17.
  2. Call `/api/chat` with `truncate: false`, `shift: false`, `options.num_ctx =
     EARMARK_LLM_CONTEXT_TOKENS` and `options.num_predict = EARMARK_LLM_MAX_OUTPUT_TOKENS`.
  3. A 400 or 413 context error is refused. On a 200, require `done_reason == "stop"` and
     `prompt_eval_count + eval_count ≤ num_ctx`.

  Without a tokenize endpoint there is no independent count. The guarantee rests on the code
  read in 2.2, gated by the version.
- **Dead end, all servers: a character estimate.** Measured characters per token run from 3.6
  to 3.9 on English transcripts (2.5) and about 1 on CJK. An estimate cannot decide fit; it
  only sizes the API's character cap (P3).

### 2.4 Long generations: the lease and the client timeout

- **All three servers answer a non-streaming request with nothing until it is complete**:
  - llama-server: its own `--timeout` is 3600 s;
  - vLLM: one `JSONResponse` at the end;
  - Ollama: with `stream:false`, one write.

  All three cancel the generation when the client disconnects.
- **httpx2's read timeout is the time without a byte**, so for these requests it bounds the
  whole generation. A scratch probe confirmed it: a unix-socket server that held its answer
  for 3 s; `read=1.0` raised `ReadTimeout` after 1.01 s; `read=10.0` returned after 3.01 s.
  `EARMARK_LLM_TIMEOUT_SECONDS` (default 3600) is therefore a deadline on the generation
  (P11).
- **The heartbeat keeps beating during the request.** The same probe ran a thread ticking every
  50 ms while the main thread sent 2 MB and waited 3 s for the answer. The longest gap was
  **0.064 s** in 57 ticks: socket I/O releases the GIL. The lease (900 s, a heartbeat every
  60 s) therefore survives a generation of any length. No fix is needed. A test pins it
  (section 6.6).
- **Time on the Spark (not measured here).** Prompt processing of about 110k tokens plus up to
  8k output tokens is minutes, not hours, for the models in 2.6. The 3600 s default leaves wide
  room. SIGTERM during the call releases the job (it is inside the interruptible window), and
  the server drops the abandoned generation.

### 2.5 How many tokens a 4-hour meeting is

- **Two measurements:**
  1. Mine, with the real `tokenizer.json` of Qwen3-30B-A3B-Instruct-2507, gpt-oss-120b and
     Llama-3.3-70B (downloaded into `TMPDIR`, run with `tokenizers 0.23.3` in a scratch venv).
     The input was a synthetic 60 020-word transcript in earshot's shape: `[HH:MM:SS]
     **Name:** text`, 8–40 words a line, English prose from this repo's docs. It came to
     **402 058 characters and 2486 lines**, and **104k (gpt-oss, Llama) to 112k (Qwen3)
     tokens**: 3.6–3.9 characters a token. The line prefix alone is 10–13 tokens.
  2. The research pass's, on 60 000 words of a novel in the same shape: **83k–85k tokens**.
- **So a 4-hour meeting is 85k–115k tokens**, plus the instructions (20 000 characters is
  about 5k tokens) and the output.
- **Sizing:** 400 000 characters fits a 131 072-token context with 8192 output tokens at
  3.6 characters a token: 111k + 5k + 8k = 124k. That is P3's default.
- Published rules of thumb agree: OpenAI's "about 4 characters" a token, and Qwen's "3~4
  characters for English".

### 2.6 Memory on the DGX Spark (128 GB unified)

The KV figures are arithmetic from each model's `config.json` (2 × full-attention layers × KV
heads × head dim × 2 bytes), not documented numbers.

| Model | Weights | Native context | KV at 131 072 |
|---|---|---|---|
| gpt-oss-120b (MXFP4) | 65 GB (63 GB GGUF) | 131 072 | 4.5 GiB (18 full-attention layers; the rest use a 128-token window) |
| gpt-oss-20b (MXFP4) | 14 GB (12 GB GGUF) | 131 072 | 3 GiB |
| Qwen3-30B-A3B | 61 GB bf16, 19 GB Q4_K_M | 32 768; 131 072 only with YaRN | 12 GiB |
| Llama-3.3-70B | 141 GB bf16, 43 GB Q4_K_M | 131 072 | 40 GiB |

- **Whisper large-v3 in the worker:**
  - weights 3.1 GB (fp16);
  - about 4.5 GB on the GPU with beam 5, and about 6 GB batched (the faster-whisper README's
    large-v2 figures; same architecture);
  - plus the CPU-side features of a long file, about 12 GB for 4 hours (step 36, 2.4).

  The worker keeps the model loaded once per process.
- **They fit together:**
  - gpt-oss-120b at 131 072 plus Whisper and a 4-hour file's features is about 90 GB of 128,
    with room for the OS.
  - Llama-3.3-70B Q4 at 131 072 is about 90 GB as well, which is tighter.
  - The docs need not say to unload one. They do say to cap the server's memory, because:
    - **vLLM v0.34.0** leaves `gpu_memory_utilization` unset, which resolves to 1.0 with its
      extensible KV cache. Its docstring names unified-memory devices as a reason to set it.
      Without `--gpu-memory-utilization` or `--kv-cache-memory-bytes`, vLLM takes the memory
      Whisper needs.
    - **Ollama** reserves `num_ctx × OLLAMA_NUM_PARALLEL` of KV, so the docs say
      `OLLAMA_NUM_PARALLEL=1`.
    - **llama-server** sizes for `-c × slots` and may auto-fit; the docs say set `-c` and
      `-np 1`.
- **Qwen3** natively holds only 32 768 tokens, and its card advises YaRN only when needed. It is
  not the recommended model for a 4-hour meeting.
- **A 64 GB Spark now exists.** There, the docs suggest gpt-oss-20b.
- **NVIDIA's playbooks:**
  - llama.cpp: built from source with `-DCMAKE_CUDA_ARCHITECTURES=121a-real`.
  - vLLM: NVIDIA containers.
  - There is **no Ollama playbook**.
  - NVIDIA's known issues say `cudaMemGetInfo` "may be smaller than the actual allocatable
    memory".
- **Not verifiable here:** what Ollama detects as VRAM on the GB10, the real throughput, and
  both models resident under load. The operator's smoke (section 7) covers them.

### 2.7 Reaching the model server from the worker container

The GPU worker runs in a bridge-networked container. A server on the Spark's host bound to
`127.0.0.1` is **not reachable** from it, and `tests/test_deploy.py` forbids `network_mode` in
`compose.yaml`. There are two workable routes:

- **(a) Recommended.** The model server runs as a container on earmark's compose network,
  declared in a `compose.override.yaml` that the operator writes. It publishes no port, so
  nothing on the LAN can reach the unauthenticated server, and the base URL is
  `http://ollama:11434` (a single-label name, P12).
- **(b)** A host-installed server, reached as `host.docker.internal`. This needs
  `extra_hosts: ["host.docker.internal:host-gateway"]` in the override, and the server bound to
  the docker bridge's address (not to all interfaces).

`compose.yaml` itself is unchanged, so it still has no model service. Section 4.12 has the doc
text. Neither route was run here. Whether Ollama's arm64 image uses the GB10 is for the
operator's smoke to show.

### 2.8 Code findings

- `jobs.kind` is `CHECK (kind IN ('ingest'))` since `0001`, and `0004` rebuilt the table with the
  same check, so a new kind needs a rebuild. The table has 34 columns after `0005`. Their order
  is `0004`'s 30 followed by `delivery`, `initial_prompt`, `language` and `purged_at`.
- `Staging.begin_upload(ext)` accepts `json` (`[a-z0-9]{1,5}`). `UploadFileName` and
  `active_upload_files()` are kind-agnostic. `Staging.release` deletes `uploads/<upload_file>` for
  any `source == 'upload'` job, so the API's startup sweep and the terminal-state release cover
  a staged summary input unchanged.
- `Pipeline.notify` already returns for `delivery == 'none'`, on every path.
- `mark_purged` purges every finished delivery-free job with the same `dedupe_key`. That is
  right for shared transcripts and wrong for summaries, whose files are per job, so it gains a
  kind branch.
- `read_transcript_file` refuses `delivery == 'none'` with a message that names the transcript
  route, so it needs a summary variant.
- The light-import tests (`tests/test_factory.py`) require that `runner`, `worker` and `factory`
  import no `httpx2`. The real summarizers therefore live behind a builder that the factory
  imports lazily, and `fakes.py` stays light.
- `fetch.NetworkTransport(network)` takes any `httpcore2.NetworkBackend`, and
  `fetch.system_resolve` is reusable. The local-only backend mirrors `GuardedNetworkBackend`
  with the opposite policy.
- `api/app.describe_validation_errors(errors, body_fields=…, missing_body=…)` gives the 422
  wording for unknown fields, missing fields and custom error types (those in
  `REQUEST_ERROR_TYPES`).
- `tests/test_docs.py` `REMOVED` bans `EARMARK_(LLM|…)` everywhere and `\bvllm\b` outside
  `docs/asr.md`. The README's settings table must list every `Settings` field, so the ban has to
  narrow (section 6.8).

### 2.9 Dead ends

- **Ollama's `/v1/chat/completions`** (2.2): it truncates silently and cannot be told not to.
- **Detecting Ollama truncation from `prompt_eval_count`**: it is the post-truncation count, and
  there is no tokenizer to compare it with.
- **`/v1/responses` on Ollama**: since v0.40.16 it defaults to `truncation: "disabled"`, but
  `shift` stays true, and it has no `num_ctx`.
- **A separate `summaries` table**: it would need a second claim loop, a second lease and a
  second heartbeat, or a worker that polls two stores. P1 avoids that.
- **Storing the input in the database**: every heartbeat rewrites the row, so a 400 KB value
  would be written to the WAL every minute, and `GET /jobs` reads whole rows.
- **Copying `<think>` stripping from step 11**: it contradicts "byte for byte" (P8).
- **The `openai` SDK**: rejected for the reasons of step 11 section 2.6, which still hold. The
  native Ollama route needs a hand-written client anyway.
- **Counting tokens at the API**: the API makes no outbound request, and a character cap is all
  it can apply (P3).

## 3. Files

Order of work: phase 1 → 7. Run `make check` after each phase. The existing doc tests pass with
this plan's edits to `docs/ARCHITECTURE.md` and `docs/PLAN.md` (checked); the doc tests that
phase 4 adds (6.7) pin them.

| Phase | File | Change |
|---|---|---|
| 1 | `src/earmark/models.py` | `JobKind` gains `"summary"`; `Stage` gains `"summarize"`; `V1Kind`; `MAX_SUMMARY_INSTRUCTIONS_CHARS`, `SUMMARY_FIELDS`; `SummaryFields`, `SummaryInput`, `SummaryResult`, `SummaryRecord`; `summary_input_bytes`; `REQUEST_ERROR_TYPES` additions; the `Job` kind rules; `Job.decode_options()` is `None` for a summary. |
| 1 | `src/earmark/db.py` | Migration `0006_summaries` (rebuild); `SCHEMA_VERSION` 6. |
| 1 | `src/earmark/jobs.py` | `NewJob.kind`; `_validate` for summaries; the summary dedupe key; `enqueue` writes `kind`; `mark_purged` purges only the named summary job. |
| 1 | `src/earmark/config.py` | `LlmBackend` and the eight `llm_*` settings; `check_local_base_url`, `is_local_ip`, `is_local_host_name`. |
| 1 | `.env.example` | The eight settings (section 4.4). |
| 2 | `src/earmark/pipeline/stages.py` | `Summarizer` Protocol; `Backends.summarizer: Summarizer | None = None`. |
| 2 | `src/earmark/summarize/__init__.py`, `summarize/base.py` (new, light) | Messages, `check_result`, `context_exceeded` and friends. |
| 2 | `src/earmark/fakes.py` | `SummarizeCall`, `FakeSummarizer`; `FakeBackends.summarizer`. |
| 2 | `src/earmark/pipeline/factory.py` | The `SUMMARIZERS` registry; `build_backends` builds it; the backends log line. |
| 2 | `src/earmark/pipeline/runner.py` | The summary path (section 4.7). |
| 2 | `src/earmark/artifacts.py` | `summaries/<job_id>/`: paths, write, read, has, remove. |
| 2 | `src/earmark/staging.py` | `Staging.stage_document(data, ext)`. |
| 2 | `src/earmark/transcripts.py` | `episode_for_job` gives `None` for a summary; `read_transcript_file` refuses a summary job with `SUMMARY_ELSEWHERE`. |
| 3 | `src/earmark/submission.py` | `Intake.submit_summary`. |
| 3 | `src/earmark/v1_jobs.py` | `v1_kind`, `read_v1_summary`, `V1SummaryView`, the summary messages; `read_v1_transcript` refuses a summary; `purge_job` handles both kinds. |
| 3 | `src/earmark/api/schemas.py` | `V1JobView.kind`; `JobView.kind`. |
| 3 | `src/earmark/api/v1.py` | `POST /v1/summaries`, `GET /v1/jobs/{id}/summary`; `add_v1_routes(..., summaries: SummaryLimits | None)`. |
| 3 | `src/earmark/api/routes.py`, `src/earmark/api/app.py` | Pass `SummaryLimits` from `Settings` to the `/v1` routes. |
| 3 | `src/earmark/cli.py` | `_job_lines`: the subject of a summary job is `summary`. |
| 3 | `src/earmark/doctor/checks.py` | The offline `llm` check. |
| 4 | `contract/api-v1.md`, `contract/README.md` | Version 1.1 (section 4.11). |
| 4 | `CLAUDE.md`, `README.md`, `docs/deploy.md` | Section 4.12. |
| 4 | tests | Sections 6.1–6.5, 6.7 and 6.8, on the fake summarizer. |
| 5 | `src/earmark/summarize/http.py` (new) | `LocalNetworkBackend`, `LocalEndpoint` (the shared client). |
| 5 | `src/earmark/summarize/openai_compat.py` (new) | `OpenAICompatSummarizer` with the `llama_cpp` and `vllm` probes. |
| 5 | `src/earmark/summarize/ollama.py` (new) | `OllamaSummarizer`. |
| 5 | `src/earmark/summarize/build.py` (new) | `LlmOptions.from_settings`, `build_summarizer`. |
| 6 | `src/earmark/doctor/online.py`, `src/earmark/doctor/__init__.py` | The online `llm server` check; `Probes.llm_transport`. |
| 7 | tests | `tests/llm_stub.py` and sections 6.1–6.6 against the stub. |

**No new dependency.** httpx2 and httpcore2 are already core dependencies, and the clients use
them as the remote ASR client does. There is no new extra, and no new CLI command.

## 4. Interfaces

Snippets are illustrative. Names, signatures, field names, messages, file names and settings
are binding.

### 4.1 `models.py`

```python
JobKind = Literal["ingest", "summary"]
V1Kind = Literal["transcription", "summary"]  # /v1's name for a job's kind
Stage = Literal["resolve", "fetch", "transcribe", "summarize", "deliver", "notify"]
MAX_SUMMARY_INSTRUCTIONS_CHARS: Final = 20_000
SUMMARY_FIELDS: Final = ("transcript", "instructions")
```

`REQUEST_ERROR_TYPES` gains `"transcript_empty"`, `"instructions_empty"` and
`"summary_instructions_too_long"`.

```python
class SummaryFields(Model):
    """The JSON body of POST /v1/summaries. Both kept verbatim (not stripped)."""

    transcript: str
    instructions: str


class SummaryInput(Model):
    """The staged input file of a summary job: what the worker sends to the model."""

    transcript: str
    instructions: str


class SummaryResult(Model):
    """What a Summarizer returns. The runner stores it only if check_result accepts it."""

    text: str
    model: str
    finish_reason: str | None  # "stop", "length", or the server's other value
    prompt_tokens: int | None = Field(None, ge=0)
    completion_tokens: int | None = Field(None, ge=0)
    context_tokens: int | None = Field(None, gt=0)
    warnings: tuple[str, ...] = ()


class SummaryRecord(Model):
    """summaries/<job_id>/summary.json."""

    job_id: JobId
    backend: str
    model: str
    text: str
    warnings: tuple[str, ...] = ()
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    context_tokens: int | None = None
    created_at: AwareDatetime
```

- `SummaryFields` validators (mode `after`):
  - `transcript`: refused when `not v.strip()` with
    `PydanticCustomError("transcript_empty", "transcript must not be empty")`. Its length cap is
    a setting and is checked by the handler (4.10).
  - `instructions`: refused when blank with `("instructions_empty", "instructions must not be
    empty")`. Over 20 000 code points it is `("summary_instructions_too_long", "instructions is
    too long (at most 20000 characters)")`.
  - Neither is stripped or changed otherwise. The `Model` base still turns lone surrogates into
    U+FFFD; such text cannot be UTF-8 encoded anyway.
- `def summary_input_bytes(fields: SummaryFields) -> bytes`: `json.dumps({"instructions": …,
  "transcript": …}, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")`.
  It is deterministic, so identical requests hash alike.
- `Job`:
  - A new `model_validator(mode="after")` `_summary_rules`. `kind == "summary"` requires
    `delivery == "none"`, `source == "upload"`, `upload_file` ending in `.json`,
    `initial_prompt is None` and `language is None`. The delivery-free rules of step 36 already
    require no `to`, `instructions`, `title`, `upload_name` or delivery state.
  - `decode_options()` returns `None` when `kind == "summary"`.
- `def v1_kind(job: Job) -> V1Kind`: `"summary"` for `kind == "summary"`, else
  `"transcription"`.

### 4.2 `db.py`

Append `Migration("0006_summaries", (...))`, built like `0004`:

1. `CREATE TABLE jobs_0006 (…) STRICT` with the 34 columns of today's table, in today's order
   (`0004`'s 30, then `delivery TEXT NOT NULL DEFAULT 'mail' CHECK (delivery IN ('mail',
   'none'))`, `initial_prompt TEXT`, `language TEXT`, `purged_at TEXT`). The one change is
   `kind TEXT NOT NULL CHECK (kind IN ('ingest', 'summary'))`.
2. `INSERT INTO jobs_0006 (<the 34 names>) SELECT <the 34 names> FROM jobs`.
3. `DROP TABLE jobs`, then `ALTER TABLE jobs_0006 RENAME TO jobs`.
4. Recreate `jobs_active_dedupe` (partial unique), `jobs_by_status` and `jobs_by_dedupe`, exactly
   as `0004` does.

`SCHEMA_VERSION` becomes 6. Everything runs inside `migrate`'s `BEGIN IMMEDIATE`.

### 4.3 `jobs.py`

- `NewJob` gains `kind: JobKind = "ingest"` (last field).
- `_validate(new)`: for `kind == "summary"` it requires `delivery == "none"`,
  `source == "upload"`, `to is None`, `instructions == ""`, `title is None`, `upload_name is
  None`, `url is None`, `upload_file` set and ending in `.json`, `content_sha256` set, and
  `initial_prompt` and `language` `None`. Otherwise `ValueError`. Mail and `/v1` transcription
  rules are unchanged (and `kind` must be `"ingest"` for them).
- **Dedupe key.** A summary's key is `sha256(json.dumps(["summary", "sha256:" +
  content_sha256], separators=(",", ":")))` hex. That list has two elements; mail keys have five
  and `/v1` transcription keys six, so the three can never be equal. The lookup SQL is
  unchanged.
- `enqueue` writes `kind` from `new.kind` (the `INSERT` binds `:kind` instead of the literal
  `'ingest'`), and `Job(kind=new.kind, …)`.
- `mark_purged(job_id)`, step 4: when the row's `kind == 'summary'`, the `UPDATE`'s condition is
  `WHERE id = :id AND purged_at IS NULL` and it returns that one row. A summary's files are its
  own; nothing is shared. Transcription jobs keep step 36's sibling rule. Steps 1–3 (unknown,
  mail, already purged, active) are unchanged.

### 4.4 Settings (`config.py`) and `.env.example`

```python
LlmBackend = Literal["none", "ollama", "llama_cpp", "vllm", "fake"]
LLM_MIN_CONTEXT_TOKENS: Final = 8192
LLM_MAX_OUTPUT_TOKENS_LIMIT: Final = 131_072
LLM_MAX_TRANSCRIPT_CHARS_LIMIT: Final = 10_000_000
```

| Field | Type, default | Rule |
|---|---|---|
| `llm_backend` | `LlmBackend`, `"none"` | Lower-cased before validation. |
| `llm_base_url` | `str \| None`, `None` | `check_local_base_url` (below). Stripped; empty is `None`; a trailing `/` is dropped. |
| `llm_model` | `str \| None`, `None` | Stripped; empty is `None`; at most 200 characters, with no whitespace or control characters (Ollama names such as `gpt-oss:120b` and vLLM ids such as `openai/gpt-oss-120b` pass). |
| `llm_api_key` | `SecretStr \| None`, `None` | Secret. Same rule as `asr_api_key` (add it to `_check_api_key`'s field list). |
| `llm_context_tokens` | `int \| None`, `None` | `ge=LLM_MIN_CONTEXT_TOKENS`. Required for `ollama`, which sends it as `num_ctx`, and then at least `llm_max_output_tokens + 1024` (both checked by `LlmOptions.from_settings`, a `ConfigError` naming the variable). llama_cpp and vllm ignore it (doctor warns when set). |
| `llm_max_output_tokens` | `int`, `8192` | `ge=256`, `le=LLM_MAX_OUTPUT_TOKENS_LIMIT`. |
| `llm_timeout_seconds` | `float`, `3600.0` | `gt=0`. |
| `llm_max_transcript_chars` | `int`, `400_000` | `ge=1000`, `le=LLM_MAX_TRANSCRIPT_CHARS_LIMIT`. |

The pydantic `description`s are as in the table's last column. `secret_values()` picks up
`llm_api_key` automatically (it is a `SecretStr`).

```python
def is_local_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool: ...
def is_local_host_name(name: str) -> bool: ...
def check_local_base_url(url: str) -> str: ...  # 'scheme://netloc'; ValueError otherwise
```

- `is_local_ip`: an IPv4-mapped IPv6 address is checked as its IPv4 address. True for 127/8,
  10/8, 172.16/12, 192.168/16, `::1` and `fc00::/7`; false for everything else (`0.0.0.0`,
  100.64/10, 169.254/16, `fe80::/10`, multicast and public).
- `is_local_host_name` (the input is lower-cased and has no trailing dot):
  - true for `localhost`, `*.localhost`, a single label matching `[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?`,
    and `*.internal`, `*.local`, `*.lan` and `*.home.arpa`, each label matching that same
    pattern;
  - false otherwise, including any name that parses as an IP address.
- `check_local_base_url`:
  - It needs an `http` or `https` scheme and a host, and refuses credentials, a query, a
    fragment, and any path other than `""` or `"/"`.
  - The host must be a local IP literal or a local host name.
  - It returns `f"{scheme}://{netloc}"`.
  - Its messages never contain the input. These are binding:
    - `LLM_URL_SHAPE = "must be the model server's root URL, such as http://127.0.0.1:11434 or http://ollama:11434"`
    - `LLM_URL_CREDENTIALS = "must not contain credentials; use EARMARK_LLM_API_KEY"`
    - `LLM_URL_QUERY = "must not contain a query or fragment"`
    - `LLM_URL_PATH = "must be the server's root URL, without a path (earmark adds /api/chat or /v1/chat/completions)"`
    - `LLM_URL_NOT_LOCAL = "must name a loopback or private-network address or a local host name (localhost, a single-label name such as ollama, or a name under .internal, .local, .lan or .home.arpa); earmark sends transcripts only to a model server on this machine or its private network"`

`.env.example` gains a section after `# --- Transcription ---` (every value the default, as
`test_env_example_values_are_defaults` requires):

```bash
# --- Summaries (POST /v1/summaries; docs/deploy.md section 21) ---
# none turns them off (503). ollama, llama_cpp or vllm: a model server on this machine or
# the private network; the URL is its root, such as http://ollama:11434. fake is for tests.
EARMARK_LLM_BACKEND=none
# EARMARK_LLM_BASE_URL=
# EARMARK_LLM_MODEL=
# EARMARK_LLM_API_KEY=
# ollama only: the context window earmark asks for (num_ctx), such as 131072.
# EARMARK_LLM_CONTEXT_TOKENS=
EARMARK_LLM_MAX_OUTPUT_TOKENS=8192
EARMARK_LLM_TIMEOUT_SECONDS=3600.0
# Longest transcript POST /v1/summaries accepts, in characters.
EARMARK_LLM_MAX_TRANSCRIPT_CHARS=400000
```

### 4.5 Stage, fake and factory

`pipeline/stages.py`:

```python
class Summarizer(Protocol):
    @property
    def backend(self) -> str: ...  # "ollama", "llama_cpp", "vllm", "fake"

    @property
    def model(self) -> str: ...

    @property
    def max_output_tokens(self) -> int: ...

    def summarize(self, request: SummaryInput, /) -> SummaryResult:
        """One model call. Raises PermanentError (context exceeded, refused, malformed
        answer) or TransientError (unreachable, HTTP 5xx, 408, 429). Never puts server
        text, the transcript, the instructions or the output into a message."""
        ...
```

`Backends` gains `summarizer: Summarizer | None = None` as its last field. The docstring says
"the stage implementations".

`fakes.py`:

```python
@dataclass(frozen=True)
class SummarizeCall:
    transcript_chars: int
    instructions_chars: int


class FakeSummarizer(FakeStage[SummarizeCall]):
    def __init__(
        self,
        *,
        text: str | None = None,
        model: str = "fake-llm",
        finish_reason: str | None = "stop",
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        max_output_tokens: int = 8192,
    ) -> None: ...
```

- `backend` is `"fake"`.
- `summarize` records the call (sizes only, never the text) and raises a queued failure, if
  any.
- It increments `computed` and returns `SummaryResult(text=…, model=…,
  finish_reason=…, …)`. The text is the scripted `text`, else
  `f"Summary of {n} characters.\n"` with `n` the transcript's length, so the input never
  appears in it.
- `FakeBackends` gains `summarizer: FakeSummarizer`; `make_fake_backends` builds one, and
  `backends()` passes it.

`pipeline/factory.py`:

```python
def _local_summarizer(ctx: BuildContext) -> Summarizer:
    from earmark.summarize.build import build_summarizer

    return build_summarizer(ctx.settings)


SUMMARIZERS: Final[Mapping[str, Builder[Summarizer | None]]] = {
    "fake": lambda ctx: FakeSummarizer(
        model=ctx.settings.llm_model or "fake-llm",
        max_output_tokens=ctx.settings.llm_max_output_tokens,
    ),
    "llama_cpp": _local_summarizer,
    "none": lambda ctx: None,
    "ollama": _local_summarizer,
    "vllm": _local_summarizer,
}
```

`build_backends` looks it up under `llm_backend`, like the other six, and passes
`summarizer=…`. The `build_worker` log line gains `llm=<backend>`.

### 4.6 `summarize/` (light parts)

`summarize/__init__.py` holds a docstring only. `summarize/base.py` imports the standard library,
`earmark.errors` and `earmark.models` only (the runner imports it).

Messages:

| Constant | Text |
|---|---|
| `CONTEXT_PREFIX` | `the transcript and instructions do not fit the model's context` |
| `CONTEXT_COUNTED` | `{prefix}: {prompt} prompt tokens and {output} for the summary need {need}, but the context is {context} tokens; nothing was summarised` |
| `CONTEXT_REFUSED` | `{prefix}: the model server refused the prompt as longer than its context ({detail}); nothing was summarised` |
| `CONTEXT_UNREAD` | `{prefix}: the model server read {seen} of the prompt's {expected} tokens; nothing was kept` |
| `CONTEXT_OVERRUN` | `{prefix}: {prompt} prompt and {completion} output tokens passed the {context}-token context, so the server dropped part of it; nothing was kept` |
| `CUT_OFF` | `the summary was cut off: the model stopped at its output limit or the end of its context after {completion} tokens; nothing was kept (EARMARK_LLM_MAX_OUTPUT_TOKENS is {limit})` |
| `NO_TEXT` | `the model returned no summary text (finish reason: {reason}); nothing was kept` |
| `STOPPED` | `the model stopped without finishing (finish reason: {reason}); nothing was kept` |
| `NOT_CONFIGURED` | `summaries are not configured on the worker (EARMARK_LLM_BACKEND is none)` |
| `INPUT_MISSING` | `the summary's input is missing; send it again` |
| `INPUT_CHANGED` | `the summary's input changed after it was received` |
| `INPUT_UNREADABLE` | `the summary's input is unreadable; send it again` |

- **Placeholders.** `{prefix}` is `CONTEXT_PREFIX`. `{detail}` is `"<n> prompt tokens, <m>-token
  context"` when both integers were parsed, `"<m>-token context"` with one, else `"no token
  counts given"`. `{reason}` is the finish reason, cut to 20 characters and made up of
  `[a-z_]`, else `unknown`.
- `def check_result(result: SummaryResult, *, max_output_tokens: int) -> None`, in order:
  - `finish_reason == "length"` raises `PermanentError(CUT_OFF…)`, with `completion` the
    completion tokens or `"?"`;
  - any other value than `"stop"` raises `PermanentError(STOPPED…)`;
  - `not result.text.strip()` raises `PermanentError(NO_TEXT…)`.

  The runner calls it for every backend, the fake included.
- `def parse_ollama_version(text: str) -> tuple[int, int, int] | None`: the first
  `(\d+)\.(\d+)\.(\d+)` of the string (pre-release suffixes ignored).
  `MIN_OLLAMA_VERSION: Final = (0, 40, 17)`.

### 4.7 The runner (`pipeline/runner.py`)

`Pipeline.run(claim)` branches first on `job.kind`:

```python
with interruptible():
    if job.kind == "summary":
        self._summarize(claim)
        result = None
    else:
        ...  # unchanged: transcript, result, and mail (None for a /v1 upload)
if job.kind == "summary" or mail is None:
    done = self._jobs.complete(claim, result)
else:
    done = self._deliver(claim, mail, result)
```

(`mail` is set to `None` on the summary branch too, so the names are always bound.)

The exception handling, `_finish` and `finish` are unchanged. A summary has no episode, so
`state.episode` stays `None`.

`_summarize(claim) -> None`:

1. `set_stage(claim, "summarize")`.
2. If `self._b.summarizer` is `None`, raise `PermanentError(NOT_CONFIGURED)`.
3. Read the staged input. The path is `staging.upload_path(job.upload_file)`.
   - A missing file raises `INPUT_MISSING`, and a sha256 other than `content_sha256` raises
     `INPUT_CHANGED`; both are permanent.
   - `SummaryInput.model_validate_json(path.read_bytes())`. A `ValidationError` or
     `UnicodeDecodeError` raises `INPUT_UNREADABLE` (permanent). An `OSError` propagates
     (transient).
4. Time the call with `time.monotonic()`, then `result = summarizer.summarize(request)`.
5. `check_result(result, max_output_tokens=summarizer.max_output_tokens)`.
6. Write `self._artifacts.write_summary(SummaryRecord(job_id=job.id,
   backend=summarizer.backend, model=result.model, text=result.text,
   warnings=result.warnings, prompt_tokens=…, completion_tokens=…, context_tokens=…,
   created_at=self._clock()))`.
7. `self._warn(job.id, result.warnings)`.
8. Log at INFO exactly this line:
   `job <id>: summary of <t> transcript and <i> instructions characters by <backend> model <model>: <p> prompt tokens, <c> output tokens, <s:.1f> s`.
   Unknown counts print as `?`. The model name is operator configuration. Nothing else is
   logged.

`Pipeline.load_episode` already goes through `episode_for_job`, which now returns `None` for a
summary. `Pipeline.finish` then releases the staged input (`Staging.release`, `source ==
'upload'`) and `notify` returns at once (`delivery == 'none'`). That covers success, failure
and a stale lease alike.

### 4.8 Artifacts and staging

`artifacts.py`:

```python
SUMMARIES_DIR: Final = "summaries"
```

`ArtifactStore.__init__` also keeps `data_dir / SUMMARIES_DIR`. Every method checks `job_id`
against `[0-9a-f]{32}` and raises `ValueError` otherwise.

| Method | Does |
|---|---|
| `summary_dir(job_id) -> Path` | `<data_dir>/summaries/<job_id>` |
| `summary_path(job_id) -> Path` | `…/summary.json` |
| `write_summary(record) -> Path` | Creates the directory with mode 0o700 (`mkdir(mode=0o700, parents=True, exist_ok=True)`), then writes `model_dump_json(indent=2) + "\n"` atomically with mode **0o600**. |
| `read_summary(job_id) -> SummaryRecord \| None` | `None` when missing. `PermanentError("corrupt artifact: …")` when it does not parse or its `job_id` differs. |
| `has_summary(job_id) -> bool` | `summary.json` is a file. |
| `remove_summary(job_id) -> int` | As `remove_decoded`: it unlinks `summary.json` and regular files named `.summary.json.*`, never follows a symlink and never uses `rmtree`. It ignores an `rmdir` `OSError`, returns the count, and returns 0 when the directory is absent. |

`staging.py`:

```python
def stage_document(self, data: bytes, ext: str, /) -> StagedUpload:
    """begin_upload(ext), write, commit; the partial file is removed on any failure."""
```

### 4.9 Intake (`submission.py`)

```python
def submit_summary(self, fields: SummaryFields, /) -> Enqueued:
    """Stage the input and enqueue a summary job. Deletes the staged file when this
    raises or finds an existing job. No recipient check: nothing is sent."""
```

- It stages the input with `self.staging.stage_document(summary_input_bytes(fields), "json")`.
- It enqueues `NewJob(kind="summary", source="upload", to=None, upload_file=staged.name,
  upload_name=None, content_sha256=staged.sha256, delivery="none")`.
- It releases the staged file on any exception and on a dedupe hit, as `submit_v1` does.
- It logs exactly: `enqueued job <id>: summary of <t> transcript and <i> instructions characters, no delivery (created=<bool>)`.

### 4.10 HTTP surface (`api/v1.py`, `v1_jobs.py`, `schemas.py`)

`SummaryLimits` (a frozen dataclass in `v1_jobs.py`) holds `max_transcript_chars: int`. Its
property `max_body_bytes` is `12 * (max_transcript_chars + MAX_SUMMARY_INSTRUCTIONS_CHARS) +
64 * 1024`. `create_app` builds `None` when `settings.llm_backend == "none"`, else
`SummaryLimits(settings.llm_max_transcript_chars)`, and passes it through
`build_router(…, summaries=…)` to `add_v1_routes(…, summaries=…)`.

Messages (constants in `v1_jobs.py`; the contract quotes them):

| Constant | Status | Text |
|---|---|---|
| `SUMMARIES_OFF` | 503 | `summaries are not configured on this server (EARMARK_LLM_BACKEND)` |
| `SUMMARY_ONLY_JSON` | 415 | `send JSON (Content-Type: application/json) with the fields transcript and instructions` |
| `SUMMARY_BODY_TOO_LARGE` | 413 | `the request body is larger than {limit}, the most a transcript at the server's cap can take (EARMARK_LLM_MAX_TRANSCRIPT_CHARS)` |
| `SUMMARY_MISSING_BODY` | 422 | `request body is missing; send a JSON object with transcript and instructions` |
| `TRANSCRIPT_TOO_LONG` | 422 | `transcript is too long (at most {n} characters on this server; EARMARK_LLM_MAX_TRANSCRIPT_CHARS)` |
| `SUMMARY_NOT_YET` | 409 | `this job has no summary yet; it is {status}` |
| `SUMMARY_NOT_YET_AT_STAGE` | 409 | `this job has no summary yet; it is {status} (stage: {stage})` |
| `SUMMARY_FAILED` | 409 | `this job has no summary; it failed` |
| `SUMMARY_SKIPPED` | 409 | `this job has no summary; it was skipped` |
| `NOT_A_SUMMARY` | 409 | `this job is a transcription; fetch its transcript from /v1/jobs/{job_id}/transcript` (literal `{job_id}`) |
| `NOT_A_TRANSCRIPTION` | 409 | `this job is a summary; fetch it from /v1/jobs/{job_id}/summary` (literal) |
| `SUMMARY_PURGED` | 410 | `this job was deleted; its summary is gone` |
| `SUMMARY_GONE` | 410 | `the summary of this job is gone; send the transcript again` |
| `SUMMARY_UNREADABLE` | 500 | `the summary of this job is unreadable; send the transcript again` |
| `SUMMARY_DELETE_FAILED` | 500 | `could not delete the summary files; try again` |

`{limit}` is `fetch.limit_text(max_body_bytes)`. `transcripts.py` gains `SUMMARY_ELSEWHERE =
"this job is a summary; fetch it from /v1/jobs/{id}/summary"` (literal `{id}`) for the
unversioned route.

| Route | Answer |
|---|---|
| `POST /v1/summaries` | Checked in this order: `summaries is None` is **503** `SUMMARIES_OFF`, before anything is read. A media type other than `application/json` is **415** `SUMMARY_ONLY_JSON`. A declared `Content-Length` over `max_body_bytes`, or more bytes read than that, is **413**. An empty body is **422** `SUMMARY_MISSING_BODY`. `SummaryFields.model_validate_json(body)` failing is **422** with `describe_validation_errors(…, body_fields=SUMMARY_FIELDS, missing_body=SUMMARY_MISSING_BODY)` (for example `unknown field 'to' (allowed: transcript, instructions)`, `transcript is required`, `transcript: Input should be a valid string`, `request body is not valid JSON`). `len(fields.transcript) > max_transcript_chars` is **422** `TRANSCRIPT_TOO_LONG`. Then `intake.submit_summary` runs in the threadpool, and the answer is **202** `V1Accepted` with `Location: /v1/jobs/{id}`, dedupe hits included. A client disconnect is a 400 with no body. The handler makes no outbound request. |
| `GET /v1/jobs/{job_id}` | As step 36, with `kind`. |
| `GET /v1/jobs/{job_id}/summary` | **200** `V1SummaryView` (`application/json`, `indent=2` plus `"\n"`, `Cache-Control: no-store`). **404** `job not found` for an unknown id or a mail job. **409** `NOT_A_SUMMARY`, and the not-ready states. **410** purged or gone. **500** unreadable. It logs `job <id>: /v1 summary served` or `job <id>: no /v1 summary: <reason>`. |
| `GET /v1/jobs/{job_id}/transcript` | On a summary job: **409** `NOT_A_TRANSCRIPTION`, checked before every other state. Otherwise unchanged. |
| `DELETE /v1/jobs/{job_id}` | Both kinds. On a summary, an `OSError` while deleting is a 500 `SUMMARY_DELETE_FAILED`. |

`v1_jobs.py`:

```python
class V1SummaryView(BaseModel):  # frozen, extra="forbid"
    job_id: JobId
    model: str
    text: str
    warnings: tuple[str, ...]


def read_v1_summary(job: Job, artifacts: ArtifactStore) -> bytes: ...  # TranscriptUnavailable
```

- **`read_v1_summary`** runs these checks in order. The first that matches decides:
  1. not a summary: `not_ready`, `NOT_A_SUMMARY`;
  2. purged: `gone`, `SUMMARY_PURGED`;
  3. queued or running: `not_ready`, `SUMMARY_NOT_YET` (with the stage once one is set);
  4. failed: `not_ready`, `SUMMARY_FAILED`;
  5. skipped: `not_ready`, `SUMMARY_SKIPPED`;
  6. done:
     - `read_summary(job.id)`; a `PermanentError` or `OSError` is `unreadable`,
       `SUMMARY_UNREADABLE`, and `None` is `gone`, `SUMMARY_GONE`;
     - otherwise the `V1SummaryView` of the record.

  It writes nothing and logs nothing. `TranscriptUnavailable` (from `transcripts.py`) is reused
  as the exception: same reasons, same status map.
- **`read_v1_transcript`** first raises `not_ready`, `NOT_A_TRANSCRIPTION` for a summary job.
- **`purge_job`** for a summary runs in this order (files first, as in step 36):
  1. `artifacts.remove_summary(job.id)`;
  2. `staging.remove_upload(job.upload_file)` (normally already gone);
  3. `store.mark_purged(job_id)`, which returns that one job;
  4. `store.checkpoint()`;
  5. the same `purged` log line as step 36.

  A transcription job takes step 36's path unchanged. The API route maps an `OSError` to the
  kind's delete message.

`api/schemas.py`:

- `V1JobView` gains `kind: V1Kind` right after `job_id`, and `v1_job_view` sets it with
  `v1_kind(job)`.
- For a summary, `language` and `duration_s` are `None` (P13). A summary has no `result`, and
  `job.language` is always `None`.
- `JobView` gains `kind: JobKind` after `id`, and `job_view` copies it. `cli._job_lines` uses the
  subject `summary` when `view.kind == "summary"`.

### 4.11 `contract/api-v1.md`, version 1.1

- **Preamble** (above the markers): add one sentence saying that version 1.1 (step 37) added
  summaries additively.
- **Inside the block**, in this order:
  - **§1.** Add a bullet: summaries are made by the operator's local model server, on the
    service's host or its private network, never a cloud model. Amend "Nothing is sent
    anywhere" to name that server.
  - **§2.** Unchanged.
  - **New §3, "POST /v1/summaries".**
    - JSON only. The fields are `transcript` (required; the text to summarise, sent to the
      model as the user message, verbatim) and `instructions` (required; the system message,
      verbatim). Both are non-blank strings, and any other field is a 422.
    - The caps: instructions at most **20000 characters**. The transcript at most the
      server's `EARMARK_LLM_MAX_TRANSCRIPT_CHARS`, **400000 characters** unless the operator
      changed it, which is about a 4-hour meeting. Characters are Unicode code points.
    - The 413 for a body larger than 12 times the caps.
    - The 503 when summaries are off.
    - 202 `{job_id, status}` and `Location`.
    - Dedupe within the window: identical means the same transcript and instructions.
    - The input is deleted when the job ends.
    - A summary is one model call. earmark does not chunk a transcript, so a client that has
      more text than fits must split it and combine the parts itself.
  - **§4, "The prompt and Whisper's window"** (renumbered).
  - **§5, "GET /v1/jobs/{job_id}"** gains the `kind` row: `transcription` or `summary`, new
    values may be added, a client must not assume only two. The table says that for a summary
    `language` and `duration_s` are `null`, and that `stage` can be `summarize`.
  - **§6.** The transcript route, unchanged except that a summary job answers 409.
  - **New §7, "GET /v1/jobs/{job_id}/summary":**
    - the shape `{"job_id", "model", "text", "warnings"}`;
    - `text` is the model's Markdown exactly as the server returned it, untrusted text that
      earmark does not interpret;
    - `model` is the operator's model name;
    - `warnings` is a list of strings;
    - the states: 404, 409 (not yet, failed, skipped, a transcription job), 410 (deleted,
      gone) and 500 (unreadable).
  - **New §8, "When a summary job fails".** It lists the `error` texts a client may act on:
    - every input-fit failure starts with `the transcript and instructions do not fit the
      model's context`;
    - a cut-off summary starts with `the summary was cut off`;
    - a not-configured worker is `NOT_CONFIGURED`.

    It says no summary of part of the input is ever stored or served. It also says a
    transcript under the character cap can still be too long for the model; the job then
    fails, and the client should send less.
  - **§9, DELETE.** A summary job's summary file and any input left are removed. A summary is
    never shared between jobs, so deleting one deletes only that job.
  - **§10, Errors.** The table gains every message of 4.10 (`TRANSCRIPT_TOO_LONG` with
    `<n>`, `SUMMARY_BODY_TOO_LARGE` with `<limit>`), and the validation messages
    `transcript must not be empty`, `instructions must not be empty`, `instructions is too long
    (at most 20000 characters)`, `unknown field '<name>' (allowed: transcript, instructions)`,
    `transcript is required`, `instructions is required`, `request body is not valid JSON`
    and `request body must be a JSON object sent with Content-Type: application/json`. The
    unknown-field row becomes two rows, one per route's allowed list.
  - **§11, languages.** Unchanged.
  - **§12, Examples.** The three job examples gain `"kind": "transcription"`. New examples
    are `summary-accepted`, `summary-job-done`, `summary` and `summary-error` (the 409 before
    it ran). Section 6.5 has their fixed inputs.
  - **New §13, "Revisions".** 1.0 (step 36, 2026-11-28): transcriptions. 1.1 (step 37,
    2026-11-28): `kind`, the summary routes, the caps and §8. Additive.

### 4.12 Docs for the implementer

- **`CLAUDE.md`:**
  - **"The service today":** add "A `/v1` summary request (step 37) sends a transcript and
    instructions to a local model server on the Spark and keeps the result for its client; no
    cloud model, no mail, no push."
  - **Rules.** Add **"Summaries are local only (step 37)":**
    - `EARMARK_LLM_BASE_URL` must name a loopback or private address or a local name, checked
      at load and again at every connect;
    - there is no cloud backend, and none is added without the operator;
    - no silent truncation: a summary is stored only when the server provably read the whole
      input and the model stopped on its own;
    - Ollama only through `/api/chat` with `truncate: false` and `shift: false`, version
      0.40.17 or later;
    - the server's error text is never copied.
  - **The leak rules:** a summary job's transcript, instructions and output never appear in a
    log or in `job.error`.
  - **"Never log or print a secret":** add `EARMARK_LLM_API_KEY` to the list.
  - **"Removed code stays removed":** add that summaries under `/v1` are not the old analysis
    stage. The analyzer, the vault, entities and search stay removed.
- **`README.md`:**
  - "How it works": a `/v1` paragraph sentence naming `POST /v1/summaries`.
  - The HTTP API sentence names `POST /v1/summaries` and `GET /v1/jobs/{id}/summary` (the route
    test reads it).
  - The settings table, between `<!-- settings -->` markers: a `### Summaries` group with all
    eight settings. `EARMARK_LLM_API_KEY` is "unset" and "secret", as the table test requires.
- **`docs/deploy.md`:**
  - **Section 15:** schema 6 on first start; an older image refuses it; back up first.
  - **A new `## 21. Summaries on a local model`:**
    - **21.1 What it is.** `/v1/summaries` for programs. Nothing leaves the Spark. It is off
      until `EARMARK_LLM_BACKEND` is set.
    - **21.2 Choosing a server and its flags:**
      - Ollama 0.40.17 or later, with `OLLAMA_NUM_PARALLEL=1`, and `OLLAMA_KEEP_ALIVE` noted
        for memory. earmark sets `num_ctx` itself from `EARMARK_LLM_CONTEXT_TOKENS`.
      - llama-server with `-c <context>` set explicitly, `-np 1`, never `--context-shift`, and
        its reasoning format for the model.
      - vLLM with `--max-model-len` and an explicit `--gpu-memory-utilization` or
        `--kv-cache-memory-bytes`, and the model's reasoning parser.
      - Mark flags that were not run here as such.
    - **21.3 The context a 4-hour meeting needs.** The measurements of 2.5, the recommendation
      of a 131 072-token context (gpt-oss-120b on a 128 GB Spark), and how
      `EARMARK_LLM_MAX_TRANSCRIPT_CHARS`, `EARMARK_LLM_MAX_OUTPUT_TOKENS` and
      `EARMARK_LLM_CONTEXT_TOKENS` relate. Rough characters per token is 3.6–3.9 for English.
    - **21.4 Memory on the Spark.** The table of 2.6 with Whisper's share. Both stay resident on
      128 GB, but cap the server. On a 64 GB Spark use gpt-oss-20b.
    - **21.5 Reaching the server from the worker.** Route (a) as a `compose.override.yaml`
      snippet: a service on earmark's network with no `ports:`, `gpus: all`, a named volume
      for models, the image pinned by digest (`<digest>` placeholder), and the base URL
      `http://ollama:11434`. Route (b) with `host.docker.internal` and `extra_hosts`. Say
      plainly that neither was run here.
    - **21.6 The settings.**
    - **21.7 An example:**
      - `curl` with `--data-binary @request.json` and `-H 'Content-Type: application/json'`;
      - building `request.json` safely with `python3 -c` or `jq` from the transcript and
        instructions files (`jq -n --rawfile t meeting.md --rawfile i notes-format.md
        '{transcript: $t, instructions: $i}'`);
      - poll the job, fetch the summary, delete.
    - **21.8 What a failed summary means**, with the §8 texts and what to change.
    - **21.9 What DELETE removes.**
    - **21.10 Checking it** with `doctor --online` in the worker container.

## 5. Behaviour and edge cases

### 5.1 Nothing leaves the Spark

- The API handler builds no summarizer and makes no request. It only stages the input and
  enqueues.
- The only outbound connection for a summary is the worker's to `EARMARK_LLM_BASE_URL`, through
  `LocalNetworkBackend`.
  - That backend resolves the host and refuses (`PermanentError`) unless every address passes
    `is_local_ip`: `refusing to send the transcript to <host>: it resolves to <address>, which
    is not a loopback or private address`.
  - It connects to the address it checked, in order, and refuses a unix socket.
  - IP literals are checked again too.
  - A resolution failure is transient: `could not resolve the model server <host>`.
- The client never follows redirects (a 3xx is permanent and names `EARMARK_LLM_BASE_URL`). It
  ignores proxy variables (`trust_env=False`) and clears cookies per request.
- The API key, when set, travels only in a per-request `Authorization: Bearer` header.

### 5.2 What is stored, and for how long

- **The job row**, as for any job: `kind='summary'`, `source='upload'`, `delivery='none'`,
  `upload_file` (`<32 hex>.json`), `content_sha256` (the input file's hash), the status columns,
  `result` `NULL`. There is no text in the database.
- **`uploads/<id>.json`** (0600) while the job is queued or running. It is deleted when the job
  ends, by the API's startup sweep if orphaned, or by a purge.
- **`summaries/<job_id>/summary.json`** (0600, directory 0700) once done, until `DELETE`.
- **Never stored:** the server's `thinking`/`reasoning_content`, its error bodies, and the
  request bodies earmark sent.

### 5.3 Validation order on POST

1. Auth, in the middleware.
2. Configured (503).
3. Media type (415).
4. Declared length (413).
5. The body read, capped (413).
6. Empty (422).
7. JSON and fields (422).
8. The transcript's length (422).
9. Stage, then enqueue (202).

Nothing is written before step 9. A staged file is removed on any failure after it is written.

### 5.4 The per-server algorithms

Every request uses `LocalEndpoint` (5.1). Responses are capped: 16 MiB for a completion,
1 MiB for metadata, and 64 KiB read from an error body. Error bodies are only parsed for the
context-error shapes, never copied.

The request that all three send carries the instructions and the transcript exactly as
received (`SummaryInput`), as these messages:

```json
[
  { "role": "system", "content": "<instructions>" },
  { "role": "user", "content": "<transcript>" }
]
```

**`llama_cpp`:**

1. `GET /props`. `ctx = default_generation_settings.n_ctx`, a positive integer. Anything else
   is permanent: `could not read the context size from llama-server at <host> (GET /props)`.
2. `POST /v1/chat/completions/input_tokens` with the chat body. `N = input_tokens`. A 404 is
   permanent: `llama-server at <host> cannot count a prompt's tokens (POST
   /v1/chat/completions/input_tokens); update llama.cpp (v1.1.0 has it)`. A 400 is checked for
   the context-error shape.
3. If `N + max_output > ctx`, raise `CONTEXT_COUNTED` (prompt `N`, output `max_output`, need,
   context `ctx`), without generating.
4. `POST /v1/chat/completions` with `{"model", "messages", "max_tokens": max_output,
   "stream": false}`.
5. A 400 with `error.type == "exceed_context_size_error"` raises `CONTEXT_REFUSED`, with
   `n_prompt_tokens` and `n_ctx` when they are integers.
6. On a 200, take `choices[0]`: `message.content` (string, else `""`), `finish_reason`, and
   `usage.prompt_tokens` and `usage.completion_tokens`.
   - `prompt_tokens < N − 8` raises `CONTEXT_UNREAD` (seen, expected `N`).
   - `prompt_tokens + completion_tokens > ctx` raises `CONTEXT_OVERRUN`.
   - Missing usage adds the warning `the model server did not report token counts; earmark
     checked the prompt's size before the call only`.
   - The result has `context_tokens=ctx` and `model` set to `EARMARK_LLM_MODEL`.

**`vllm`:** the same steps, with these differences:

1. and 2. are one call: `POST /tokenize` with `{"model", "messages", "add_generation_prompt":
   true}`. `N = count` and `ctx = max_model_len`, both positive integers; anything else is
   permanent: `could not count the prompt's tokens at vLLM on <host> (POST /tokenize)`.
5. A 400 whose `error.message` contains `maximum context length` raises `CONTEXT_REFUSED`.
   Integers are parsed with `maximum context length is (\d+) tokens` and `contains (\d+) input
   tokens`, and nothing else is taken from the message.

**`ollama`:**

1. `GET /api/version`. `parse_ollama_version(version)` below `MIN_OLLAMA_VERSION`, or
   unparseable, is permanent: `Ollama <version> at <host> is older than 0.40.17, the first
   release that refuses an over-long prompt instead of cutting it; upgrade it`. The shown
   version is cleaned to `[0-9A-Za-z.+-]`, at most 40 characters, else `unknown`.
2. `POST /api/chat` with `{"model", "messages", "stream": false, "truncate": false, "shift":
   false, "options": {"num_ctx": EARMARK_LLM_CONTEXT_TOKENS, "num_predict": max_output}}`.
3. A 400 or 413 whose `error` (string, or `error.message`) contains, lower-cased, `longer than
   the context length`, `exceeds the available context` or `context size` raises
   `CONTEXT_REFUSED` (no counts). A 404 is permanent and names `EARMARK_LLM_MODEL` (`model not
   found on <host>; check EARMARK_LLM_MODEL (ollama pull <model>)`).
4. On a 200: `message.content`, `done_reason` as the finish reason, `prompt_eval_count`,
   `eval_count`. `prompt + completion > num_ctx` raises `CONTEXT_OVERRUN`. The result has
   `context_tokens=num_ctx`.

**Shared mapping of everything else** (the context checks above come first):

| Response | Outcome |
|---|---|
| 3xx | Permanent: `the model server at <host> answered with a redirect (HTTP <s>); redirects are not followed, check EARMARK_LLM_BASE_URL` |
| 401, 403 | Permanent: `… refused the request: HTTP <s> (check EARMARK_LLM_API_KEY)` |
| 404 (except Ollama's) | Permanent: `… HTTP 404 (check EARMARK_LLM_BASE_URL, the server's root URL, EARMARK_LLM_BACKEND and EARMARK_LLM_MODEL)` |
| 408, 429, 5xx | Transient (honours `Retry-After`): `the model server at <host> failed: HTTP <s>` |
| Other 4xx | Permanent: `the model server at <host> rejected the request: HTTP <s>` |
| A body that is not JSON, or not the expected shape | Permanent: `the model server at <host> returned a response earmark cannot read` |
| Over the cap | Permanent: `the model server at <host> returned more than <n> MiB` |
| `ReadTimeout` | Permanent (P11): `the model server at <host> did not answer within <t> s (EARMARK_LLM_TIMEOUT_SECONDS)` |
| Other timeouts, `TransportError` | Transient: `the model server at <host> could not be reached: <ExceptionType>` |

`<host>` is `endpoint.host_label(base_url)`. Every message is earmark's own. The runner's
`describe_error` adds redaction of the settings' secrets, the API key included.

**The finish reason.** llama.cpp and vLLM report `finish_reason`; Ollama reports `done_reason`.
`check_result` (runner) turns anything but `stop` into a permanent failure, and `length` into
`CUT_OFF`. vLLM's `abort`, `error` and `repetition` become `STOPPED`.

### 5.5 Edge cases

- **The operator changes `EARMARK_LLM_BACKEND` to `none` while summary jobs are queued.** The
  worker fails them with `NOT_CONFIGURED`. The API answers 503 for new ones once restarted.
- **Retries.**
  - A transient failure (server down, 5xx) requeues the job, and the staged input stays.
  - A permanent failure ends the job, and `finish` deletes the input.
  - A summary is never partially stored, because `write_summary` runs only after
    `check_result`.
  - A summary written just before a SIGTERM is overwritten by the retry.
- **An identical request after the job failed** makes a new job, because dedupe never returns
  a failed job.
- **An identical request after a purge** makes a new job (`purged:<id>`).
- **Dedupe within the window returns the same job.** The second request's staged copy is
  deleted at once.
- **A transcript of exactly the cap** is accepted, and one character more is a 422. The count is
  code points, unstripped.
- **JSON with duplicate keys:** pydantic keeps the last value. The contract need not mention it.
- **A body that is valid JSON but not an object** gets `describe_validation_errors`' "request
  body must be a JSON object …".
- **`text` exactly as returned:** CRLF, U+2028, emoji, a leading `<think>`, trailing
  whitespace and Markdown fences are all kept. The only change is that lone surrogates become
  U+FFFD (the `Model` base), because they cannot be stored as UTF-8.
- **Concurrency with transcriptions.** One worker runs one job at a time, so a long summary
  delays queued transcriptions, and the reverse. The docs say so, and it is fine for one
  operator.
- **The unversioned routes:**
  - `GET /jobs` lists summary jobs with `kind: summary`, `source: upload` and `to: null`.
  - `GET /jobs/{id}/transcript` of a summary is a 409 `SUMMARY_ELSEWHERE`, and `earmark
    transcript` prints it and exits 1.

### 5.6 Error and log text

- Every `job.error` of a summary job is one of the earmark messages above. The one exception is
  the runner's "unexpected error" summary of a non-earmark exception. No code on this path puts
  input or output text into an exception message: pydantic errors for the staged input are
  caught and replaced (`INPUT_UNREADABLE`), and server bodies are never copied.
- INFO lines carry the job id, sizes, token counts, the backend, host and model, and timings.
  The doctor never prints server text either, apart from the model ids that `/v1/models` lists
  (operator configuration, as the ASR probe already does).

## 6. Tests

### 6.0 Fixtures and helpers

None come from outside. There are three new helpers.

- **`tests/llm_stub.py`.** An HTTP/1.1 server on a unix socket in a short temp dir, built like
  `tests/asr_stub.py` (TCP is blocked in the suite; unix sockets are not).
  - **Requests** are recorded as `LlmRequest(method, path, headers, json)`.
  - **Replies** are scripted per `(method, path)` as queues of `StubReply(status, body,
    headers, hold)`. A reply holds on its event, with a 10 s cap.
  - **Builders** make each server's replies:
    - `ollama_version("0.40.19")`, `ollama_chat(content, done_reason, prompt_eval_count,
      eval_count)` and `ollama_too_long()` (the 400 of 2.2);
    - `llama_props(n_ctx)`, `llama_input_tokens(n)` and `llama_exceeds(n_prompt, n_ctx)`;
    - `vllm_tokenize(count, max_model_len)`, `vllm_too_long(x, n)`;
    - `chat_completion(content, finish_reason, prompt_tokens, completion_tokens)` for both
      OpenAI-compatible servers.
  - **Transport.** `transport()` returns `httpx2.HTTPTransport(uds=…)`.
- **`tests/summary_env.py`.** `summary_env(tmp_path, *, backend="fake", stub=None, **settings)`:
  - It wraps `v1_env`, so the mailer and notifier are forbidden.
  - It builds the app with `Settings(llm_backend=…, llm_base_url="http://127.0.0.1:11434",
    llm_model="example-model", llm_context_tokens=…)`.
  - With a stub, the worker's backends get `build_summarizer(settings, transport=stub.transport())`.
  - Helpers: `post(**json)`, `summary(job_id)`.
- **The scratch tokenizer measurement of 2.5** is not a test and adds nothing to the repo.

### 6.1 Brief: "A summary job runs end to end against a fake OpenAI-compatible server on a unix socket, never reaches the mailer or the notifier, and serves the model's text byte for byte."

In `tests/test_api_v1_summaries.py`:

- **`test_summary_end_to_end[llama_cpp]`, `[vllm]` and `[ollama]`:**
  - The flow: POST, `run_once`, then `GET /v1/jobs/{id}` (done, `kind: summary`, stage
    `summarize`, `language`/`duration_s` null), then `GET …/summary`.
  - The served `text` equals the stub's `content` string exactly, decoded from the JSON. The
    content is `TRICKY = "## Notes\r\n\n- café \U0001f600 <think>x</think>\n```\n{\"a\": 1}\n```\n  "`.
  - `model` is `example-model`.
  - The stub's chat request has `messages == [{"role": "system", "content": instructions},
    {"role": "user", "content": transcript}]`, with both strings exact (including leading and
    trailing whitespace).
  - The OpenAI-compatible requests carry `max_tokens == 8192` and `stream is False`. The Ollama
    one carries `truncate is False`, `shift is False`, `options == {"num_ctx": 131072,
    "num_predict": 8192}` and `stream is False`.
  - The preflight requests happened in order (`/props` and `…/input_tokens`; `/tokenize`;
    `/api/version`).
  - The mailer and the notifier are `ForbiddenMailer` and `ForbiddenNotifier`, and no outbox
    file exists.
- **`test_summary_failure_never_mails_or_pushes`**: `FakeSummarizer.fail_next(PermanentError)`,
  then a `TransientError` with `max_attempts=1`, then a stale lease
  (`tests/test_worker.py::test_summary_stale_lease_failure_without_push`).
- **`test_summary_input_deleted_when_job_ends[done|failed]`**: `uploads/` is empty after
  `run_once`, and `summaries/<id>/summary.json` exists only after `done`.

### 6.2 Brief: "An input over the cap is a 422 stating the cap; a test shows the handler makes no outbound request. An over-context input in the worker fails the job with the stated message, for each server's way of reporting it, and never stores a summary of part of the input."

At the API (`tests/test_api_v1_summaries.py`):

- **`test_transcript_over_cap_is_422`:**
  - With `llm_max_transcript_chars=1000`: 1000 characters (multi-byte `é` included) answers 202,
    and 1001 answers 422 with the exact `TRANSCRIPT_TOO_LONG` text naming 1000.
  - With the default: 400 001 characters answers 422 naming 400000.
  - `uploads/` stays empty.
- **`test_summary_handler_makes_no_outbound_request`:**
  - `httpx2.HTTPTransport.handle_request` and `socket.socket.connect` are monkeypatched to
    raise a `CalledError` (`BaseException`).
  - An over-cap POST answers 422, and a valid POST answers 202.
  - The patch was never hit, and the stub of a running `LlmStub` recorded no request.
- **`test_instructions_cap`** (20000 accepted, 20001 refused with the message),
  **`test_summary_body_too_large_is_413`** (declared and streamed), and
  **`test_summary_rejects`**, parametrised over: an unknown field (`to`, `model`,
  `max_tokens`), each field missing, blank values, non-string values, invalid JSON, a JSON
  array, an empty body, and a non-JSON media type (415). Each asserts the exact `detail`.
- **`test_summaries_off_is_503`**: with `llm_backend="none"`, the answer is 503
  `SUMMARIES_OFF`, the body is never read (`call_asgi` counts `receive`), and nothing is staged.

In the worker (`tests/test_summarize_backends.py`, against `LlmStub`, through the runner via
`summary_env`), each case asserts these three things:

- the job is `failed`;
- `error` starts with the stated prefix and equals the exact message;
- there is no `summaries/<id>/` directory, and `uploads/` is empty.

| Test | Stub script | Expected |
|---|---|---|
| `test_llama_cpp_refuses_before_generating` | `/props` n_ctx 16384; `input_tokens` 9000; max output 8192 | `CONTEXT_COUNTED` (9000, 8192, 17192, 16384); no `/v1/chat/completions` request was made |
| `test_llama_cpp_exceed_context_400` | `/props` 65536; `input_tokens` 1000; chat 400 `exceed_context_size_error` (70000, 65536) | `CONTEXT_REFUSED` with "70000 prompt tokens, 65536-token context" |
| `test_llama_cpp_read_less_than_counted` | `input_tokens` 50000; chat 200 with `prompt_tokens` 32000 | `CONTEXT_UNREAD` (32000 of 50000) |
| `test_llama_cpp_context_shift_detected` | ctx 65536; `input_tokens` 50000; chat 200 with 50000 + 20000 | `CONTEXT_OVERRUN` |
| `test_llama_cpp_slack` | `input_tokens` 1000; `prompt_tokens` 993 | done (the 8-token slack) |
| `test_vllm_refuses_before_generating` | `/tokenize` count 130000, max_model_len 131072 | `CONTEXT_COUNTED`; no chat request |
| `test_vllm_context_400` | `/tokenize` ok; chat 400 with vLLM's message | `CONTEXT_REFUSED` with the parsed integers |
| `test_vllm_max_tokens_capped_is_cut_off` | chat 200 `finish_reason: "length"` | `CUT_OFF` |
| `test_ollama_too_long_400` | version 0.40.19; chat 400 `ollama_too_long()` | `CONTEXT_REFUSED` ("no token counts given") |
| `test_ollama_llama_server_400_passthrough` | chat 400 with `{"error": "request (…) exceeds the available context size (…)"}` | `CONTEXT_REFUSED` |
| `test_ollama_capped_output_is_cut_off` | chat 200 `done_reason: "length"` | `CUT_OFF` |
| `test_ollama_overrun_detected` | chat 200, prompt 120000 + eval 20000, num_ctx 131072 | `CONTEXT_OVERRUN` |
| `test_ollama_too_old[0.40.16]`, `[0.12.0]`, `[garbage]` | `/api/version` | the version message; no chat request |
| `test_ollama_new_enough[0.40.17]`, `[0.41.0-rc1]`, `[1.0.0]` | | done |

The light rules have unit tests in `tests/test_summarize_base.py`: `check_result` for `stop`,
`length`, `abort`, `None` and empty or whitespace text; `parse_ollama_version`; and the message
formatting, including that `{reason}` is cleaned.

### 6.3 Brief: "A base URL that is not loopback or private is refused at settings load."

In `tests/test_config.py`:

- **`test_llm_base_url_accepted`**, parametrised over:
  - `http://127.0.0.1:11434`, `http://127.8.9.10`, `http://[::1]:8000`, `http://10.0.0.5`,
    `http://172.16.0.1`, `http://172.31.255.254`, `http://192.168.1.10:8080`, `http://[fd00::1]`;
  - `http://localhost:11434`, `http://ollama:11434`, `http://llm`, `http://host.docker.internal:11434`,
    `http://spark.local`, `http://box.lan`, `http://gpu.home.arpa`, `https://ollama/`.

  The normalised value has no trailing slash.
- **`test_llm_base_url_refused`**, parametrised over:
  - `http://8.8.8.8`, `http://1.1.1.1:11434`, `http://172.32.0.1`, `http://100.64.0.1`,
    `http://169.254.169.254`, `http://[fe80::1]`, `http://0.0.0.0:11434`, `http://[::ffff:8.8.8.8]`;
  - `http://api.example.com`, `https://llm.example.org/v1`, `http://ollama.com`;
  - `http://user:pw@127.0.0.1`, `http://127.0.0.1/v1`, `http://127.0.0.1?x=1`, `ftp://127.0.0.1`,
    `http://`, `http://-bad`.

  Each case asserts that `load_settings` raises `ConfigError` with the exact `LLM_URL_*` message
  and that the input is not in the message.
- **`test_is_local_ip`**: the IPv4-mapped forms, both edges of 172.16/12, and `fc00::/7`.
- **`test_llm_settings_defaults`** (none, 8192, 3600, 400000) and
  **`test_llm_api_key_is_secret`**: it is in `secret_values()` and redacted by `earmark config`.

In `tests/test_summarize_http.py`, the connect-time policy:

- **`test_local_backend_refuses_public_resolution`**: a `StaticResolver` maps `ollama` to
  `8.8.8.8`. The job's error is the refusing message with `8.8.8.8`, and no connect was
  attempted (`ScriptedNetwork.connects == []`).
- **`test_local_backend_refuses_mixed_resolution`**: `10.0.0.5` and `8.8.8.8` together.
- **`test_local_backend_connects_to_checked_address`**: it connects to `10.0.0.5`.
- **`test_local_backend_refuses_unix_socket`**, and **`test_resolution_failure_is_transient`**.
- **`test_client_ignores_proxy_env`**: `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY` are set, and the
  request still goes to the stub.
- **`test_client_never_follows_redirect`**: a 302 to a public URL is a permanent failure, and
  there is no second request.
- **`test_api_key_only_in_header`**: the key appears in no log line or error.
- **`test_response_over_cap_is_permanent`**.

In `tests/test_factory.py`:

- **`test_build_summarizer_config_errors`**: a missing URL, a missing model, and `ollama`
  without `EARMARK_LLM_CONTEXT_TOKENS`, or with a context below `max_output + 1024`, are each a
  `ConfigError` naming the variable. `none` builds `None`, and `fake` builds a `FakeSummarizer`.
- **`test_factory_imports_no_httpx2`** stays green: `summarize.build` is imported only inside
  the builder.

### 6.4 Brief: "No log line carries the transcript, instructions or output; a test feeds marked values through submit, run, fetch and purge and searches the logs."

**`tests/test_leaks.py::test_summary_job_leaks_nothing`** uses the module's `log_stream`
(earmark's real handler, at DEBUG) and the `llama_cpp` stub.

- **The values:** `transcript = "MARKTRANSCRIPT-" + padding` (about 20 KB),
  `instructions = "MARKINSTR-…"`, and the stub's content `"MARKOUTPUT-…"`.
- **The flow:**
  1. POST, `run_once`, GET the job, GET the summary (it contains `MARKOUTPUT`, so the test
     reaches it), then DELETE and GET again.
  2. A second job whose chat reply is a 400 with a body that **echoes** `MARKTRANSCRIPT` and
     `MARKINSTR` (a server that quotes its input).
  3. A third job whose reply is a 500 that echoes them.
- **The asserts:**
  - no marker appears in the log;
  - no marker appears in any job's `error` (through `GET /v1/jobs/{id}`, `GET /jobs/{id}` and
    `earmark jobs`' output);
  - after the purge and the two failures, no marker is in the bytes of any file under the data
    directory (`earmark.db` with its `-wal` and `-shm`, `uploads/`, `summaries/`).

**`test_summary_enqueue_log_line`** pins the exact line of 4.9, and **`test_summary_done_log_line`**
the line of 4.7.

### 6.5 Brief: "`kind` is correct for both job types, and the summary route on a transcription job, and the transcript route on a summary job, each give a clear error." and "`contract/api-v1.md` covers the new routes and caps, and the step-36 examples still hold."

In `tests/test_api_v1_summaries.py`:

- **`test_kind_in_v1_view`**: `transcription` for a `/v1` upload and `summary` for a summary,
  while queued, done and after a purge.
- **`test_kind_in_unversioned_view`**: `ingest`/`summary`, and `earmark jobs` shows the subject
  `summary`.
- **`test_summary_route_on_transcription_job`**: 409 `NOT_A_SUMMARY`, both before and after the
  job is done.
- **`test_transcript_route_on_summary_job`**: 409 `NOT_A_TRANSCRIPTION`, for both formats,
  purged or not.
- **`test_unversioned_transcript_refuses_summary`**: 409 `SUMMARY_ELSEWHERE`, and `earmark
  transcript` exits 1 printing it.
- **`test_summary_states`**: queued 409, running 409 with `stage: summarize`, failed 409, done
  200, the file deleted by hand 410 `SUMMARY_GONE`, corrupt JSON 500, purged 410. Plus
  `test_summary_view_shape` (exact keys) and `test_summary_no_store`.
- **`test_mail_job_hidden_from_summary_route`**: 404.

In `tests/test_api_v1_contract.py` (updated):

- **`API_V1_SHA256`** becomes the new block's hash. A comment keeps the step-36 value
  `6823155d…` as the 1.0 hash.
- **`EXAMPLE_NAMES`** gains the four summary examples. `tests/api_v1_examples.py` builds them
  with the fake summarizer:
  - `FakeSummarizer(text="## Decisions\n\n- Ship the roadmap in March.\n", model="example-model")`;
  - a second fixed job id, `7b1e4d9a2c3f4e5a8b6c0d1e2f3a4b5c` (the `uuid4` patch returns the
    two ids in turn; lease tokens take the calls in between, so script them explicitly);
  - the clock starting at `2026-11-28T10:00:00Z`, with the claim 5 s later and completion 120 s
    after that;
  - transcript `"[00:00:00] **Alice:** Good morning, everyone.\n"`, instructions `"Summarise the
    decisions as a Markdown list."`.
- **`test_api_v1_step36_examples_still_hold`**: `STEP36_EXAMPLES`, a frozen copy of the seven
  step-36 bodies pasted into `tests/api_v1_examples.py` from `git show HEAD:contract/api-v1.md`.
  For each, every key and value of the frozen JSON equals the current response's, and the only
  extra key is `kind` (or none). For `transcript-text`, the bytes are equal.
- **`test_api_v1_error_table_matches`** gains every new message, `test_api_v1_cap_stated` gains
  `20000 characters`, `400000 characters`, `EARMARK_LLM_MAX_TRANSCRIPT_CHARS` and
  `EARMARK_LLM_BACKEND`, and **`test_api_v1_summary_failures_stated`** checks that §8 contains
  `CONTEXT_PREFIX`, `the summary was cut off` and `NOT_CONFIGURED`.
- **`test_api_v1_revisions_listed`**: §13 names 1.0 and 1.1.

### 6.6 Criteria this plan adds

- **Purge** (brief: "`DELETE` purges a summary job as it does a transcription job"), in
  `tests/test_v1_jobs.py`:
  - **`test_purge_summary_job`**: after DELETE, the answer is 200 with `purged_at`;
    `summaries/<id>/` is gone and `uploads/` is empty; GET summary is 410 `SUMMARY_PURGED`; a
    second DELETE answers 200 with the same view.
  - **`test_purge_summary_refuses_active`**: queued and running are 409 `STILL_ACTIVE`.
  - **`test_purge_summary_only_that_job`**: two summary jobs with the same input (window 0), both
    done; deleting one leaves the other's file and view unpurged.
  - **`test_purge_summary_files_first`**: `remove_summary` raising `OSError` gives a 500
    `SUMMARY_DELETE_FAILED`, the row is unchanged, and a retry succeeds.
  - **`test_purge_summary_leaves_only_the_row`**: the exact remaining file set.
- **Dedupe**: `test_summary_identical_within_window_same_job` (the second staged copy is
  deleted), `test_summary_after_window_new_job`, `test_summary_dedupe_key_distinct` (it differs
  from the mail and `/v1` keys for the same hash), and `test_summary_failed_not_deduped`.
- **Long generations**, in `tests/test_worker.py`:
  - **`test_summary_long_generation_keeps_its_lease`**:
    - The setup: `CountingJobStore`, `lease_s=30`, `heartbeat_s=0.01`, the `llama_cpp` stub with
      the chat reply held on an event.
    - A helper thread waits until the stub has the chat request, advances the clock 31 s, waits
      for two more beats, records `requeue_stale()`, then sets the event.
    - The job is `done`, the recorded stale list is `[]`, and the beats are at least 2.
  - **`test_summary_read_timeout_is_permanent`**: `llm_timeout_seconds=0.2` with a held reply
    fails the job at once (`attempts == 1`, status `failed`, the timeout message).
- **Store and schema**, in `tests/test_db.py`, `tests/test_jobs.py` and `tests/test_models.py`:
  - `test_migration_0006_keeps_rows`: a version-5 database with mail, `/v1` and purged rows
    migrates with every column value unchanged, `kind` accepts `summary`, `SCHEMA_VERSION == 6`;
  - `test_migration_0006_keeps_active_dedupe`;
  - `test_migration_0006_rejects_other_kinds`;
  - `test_enqueue_summary_validates`: each forbidden combination of 4.3;
  - `test_mark_purged_summary_only_named`;
  - `test_job_summary_rules`;
  - `test_summary_fields_*`;
  - `test_summary_input_bytes_stable`: a literal for fixed inputs;
  - `typing.get_args(JobKind) == ("ingest", "summary")`.
- **Artifacts and staging:** `test_summary_paths_validated`, `test_write_read_summary_modes`
  (0600 and 0700), `test_remove_summary_spares_other_files`, `test_remove_summary_ignores_symlinks`,
  and `test_stage_document`.
- **Runner:** `test_summary_not_configured_on_worker`, `test_summary_input_missing`,
  `test_summary_input_changed`, `test_summary_input_unreadable`, and
  `test_summary_check_result_applies_to_fake` (`FakeSummarizer(finish_reason="length")` gives
  `CUT_OFF` and no file).
- **Doctor**, in `tests/test_doctor.py`:
  - `test_llm_off`: ok, "off".
  - `test_llm_fake_warns`.
  - `test_llm_configured`: ok, naming the backend, host, model and caps.
  - `test_llm_config_error_fails`.
  - `test_llm_context_ignored_warns` (llama_cpp with a context set).
  - `test_llm_online_ollama`: version, then `/api/show` with the training context and `/api/ps`
    with the loaded context, against a `MockTransport` through `Probes.llm_transport`; a warning
    when `EARMARK_LLM_CONTEXT_TOKENS` exceeds the training context.
  - `test_llm_online_ollama_too_old_fails`.
  - `test_llm_online_llama_cpp`: it reports `n_ctx` and `total_slots`, and warns when `n_ctx <
    max_output + 4096`.
  - `test_llm_online_vllm`: the model listed with `max_model_len`, or a warning when it is not
    listed.
  - `test_llm_online_cap_vs_context_warns`: `max_transcript_chars / 4 + max_output > context`.
  - `test_llm_online_skipped_when_offline_failed`.
  - `test_llm_online_never_prints_server_text`.

### 6.7 Existing tests that must change

- **`tests/test_docs.py`:**
  - `REMOVED`: replace `r"EARMARK_(LLM|VAULT|OBSIDIAN|STORE|INDEX|EMBED|TZ)"` with
    `r"EARMARK_(VAULT|OBSIDIAN|STORE|INDEX|EMBED|TZ)"` and
    `r"EARMARK_LLM_(PROVIDER|EFFORT|FALLBACKS|RESPONSE_FORMAT)"` (step 11's settings that stay
    removed). Drop the `\bvllm\b` special case in `_removed_hits` (vLLM is a supported server
    now). Keep `anthropic` and `\bclaude\b`.
  - `SECRET_FIELDS` gains `llm_api_key`, and `SECRET_LINE` gains `LLM_API_KEY`.
  - The route test reads the new routes from the README.
  - Add `test_rescope_partial_reversal_in_both`, pinning the note of 8.12 in `docs/PLAN.md` and
    `docs/ARCHITECTURE.md`.
  - Add `test_deploy_doc_summaries_section`: `## 21. Summaries on a local model` contains
    `EARMARK_LLM_BACKEND`, `EARMARK_LLM_CONTEXT_TOKENS`, `EARMARK_LLM_MAX_TRANSCRIPT_CHARS`,
    `0.40.17`, `OLLAMA_NUM_PARALLEL`, `--gpu-memory-utilization`, `-np 1`, `compose.override.yaml`
    and `host.docker.internal`.
  - Add a test that `CLAUDE.md` states "Summaries are local only".
  - `test_deploy_doc_section_4_complete` keeps its `EARMARK_LLM` ban for section 4 only.
- **`tests/test_db.py`:** `assert SCHEMA_VERSION == 5` becomes 6, and the column-count pins.
- **`tests/test_models.py`:** the `JobKind` pin.
- **Tests that build `Backends` by hand:** nothing to change, because `summarizer` has a default.
- **`tests/test_fakes.py` and `tests/test_stages.py`:** `FakeSummarizer` conforms to
  `Summarizer`.
- **The CLI and API client tests** that build `JobView` dicts by hand gain `"kind": "ingest"`.
- **`tests/api_v1_examples.py` and `tests/test_api_v1_contract.py`:** as 6.5.
- **`tests/test_deploy.py`:** unchanged, because `compose.yaml` is unchanged (it still has no
  `vllm` and no model service).
- **`tests/test_contract_samples.py` and the mail samples must not change.** The mail path is
  untouched.

### 6.8 Why narrowing `REMOVED` is not loosening

The scan exists so that docs do not describe behaviour that step 22 removed. Summaries on a
local model under `/v1` are new behaviour the operator approved (8.12), and they need
`EARMARK_LLM_*` settings and the word vLLM in the README's required settings table and in
`docs/deploy.md`. The patterns that still describe removed behaviour stay banned: the vault, the
index, `anthropic`, `claude`, and step 11's four settings that do not return.

## 7. Verification

```bash
export TMPDIR=/home/sandy/.cache/earmark-step37-verify && mkdir -p "$TMPDIR"
cd /home/sandy/dev/earmark
make check
GITHUB_ACTIONS=true make check
uv run pytest tests/test_api_v1_summaries.py tests/test_summarize_backends.py \
  tests/test_summarize_http.py tests/test_summarize_base.py tests/test_api_v1_contract.py -q
uv run pytest tests/test_leaks.py -k summary -q
uv run pytest tests/test_worker.py -k "summary" -q
git diff --stat contract/samples   # must be empty
git diff --stat compose.yaml       # must be empty
```

Live smoke on the fakes. All backends are `fake`, including `EARMARK_LLM_BACKEND=fake`:

```bash
cd "$TMPDIR" && export EARMARK_DATA_DIR="$TMPDIR/data" EARMARK_API_TOKEN=smoke-token-0123456789
export EARMARK_RESOLVER_BACKEND=fake EARMARK_FETCH_BACKEND=fake EARMARK_YOUTUBE_BACKEND=fake
export EARMARK_ASR_BACKEND=fake EARMARK_MAIL_BACKEND=fake EARMARK_NOTIFY_BACKEND=fake
export EARMARK_LLM_BACKEND=fake
uv --directory /home/sandy/dev/earmark run earmark serve &   # 127.0.0.1:8787
H="Authorization: Bearer $EARMARK_API_TOKEN"
curl -s -H "$H" -H 'Content-Type: application/json' \
  -d '{"transcript": "[00:00:00] Alice: hello", "instructions": "Summarise."}' \
  http://127.0.0.1:8787/v1/summaries                  # 202 {"job_id": …, "status": "queued"}
curl -s -H "$H" -H 'Content-Type: application/json' \
  -d '{"transcript": "x", "instructions": "y", "to": "a@example.org"}' \
  http://127.0.0.1:8787/v1/summaries                  # 422 unknown field 'to'
uv --directory /home/sandy/dev/earmark run earmark worker --once
curl -s -H "$H" http://127.0.0.1:8787/v1/jobs/<id>          # kind summary, done
curl -s -H "$H" http://127.0.0.1:8787/v1/jobs/<id>/summary  # {"job_id", "model", "text", …}
curl -s -o /dev/null -w '%{http_code}\n' -H "$H" \
  http://127.0.0.1:8787/v1/jobs/<id>/transcript             # 409
curl -s -X DELETE -H "$H" http://127.0.0.1:8787/v1/jobs/<id> # 200, purged_at set
find "$TMPDIR/data" -type f        # earmark.db (+ -wal/-shm) only
uv --directory /home/sandy/dev/earmark run earmark doctor   # llm: warn, fake
kill %1
EARMARK_LLM_BACKEND=none uv --directory /home/sandy/dev/earmark run earmark serve &
curl -s -o /dev/null -w '%{http_code}\n' -H "$H" -H 'Content-Type: application/json' \
  -d '{}' http://127.0.0.1:8787/v1/summaries                # 503
kill %1; rm -rf "$TMPDIR"
```

Check the worker's output for the absence of the transcript and instructions text.

**Not verifiable here:** a real Ollama, llama-server or vLLM on the Spark; the memory of 2.6
under load; whether the model server's container uses the GB10; and the throughput. The
operator's smoke on the Spark, which `docs/deploy.md` 21.10 describes, is:

1. `docker compose run --rm worker doctor --online`, which shows the server's version and
   context;
2. one `POST /v1/summaries` of a real 4-hour transcript, then `GET …/summary`;
3. one deliberately over-long transcript (the same text pasted twice), which must fail with
   `the transcript and instructions do not fit the model's context`.

## 8. Deviations

### 8.1 Product choices P1–P14

As stated at the top. P5 departs from the brief's "OpenAI-compatible" for Ollama. P11 makes a
timeout permanent, whereas the remote ASR backend retries one. P14 adds `kind` to the
unversioned view as well. The rest settle what the brief left to the planner.

### 8.2 Ollama through its native API

The brief names an OpenAI-compatible server, and it requires no silent truncation, "most of all
for Ollama". Section 2.2 shows the two cannot both hold on Ollama's `/v1` route. The native
route can hold the second, so it is used, gated on Ollama 0.40.17 or later.

### 8.3 An explicit server type

The brief names `EARMARK_LLM_BASE_URL` and `EARMARK_LLM_MODEL` "such as". `EARMARK_LLM_BACKEND`
is added because the three servers need three different checks (2.3), and because it follows
the repo's convention that backends are chosen by config in `factory.py`.

### 8.4 `EARMARK_LLM_CONTEXT_TOKENS` is Ollama-only

llama-server and vLLM report their context, and earmark reads it on every job. Ollama takes
`num_ctx` from the request, so the setting is what earmark asks for. A second, possibly stale,
copy for the other two would only add a way to be wrong.

### 8.5 A token preflight for llama_cpp and vllm

The brief asks for a reliable fit check. The preflight is what makes it independent of the
server's version (for llama.cpp, whose older builds truncated), and it refuses an over-long
input before a long generation rather than after.

### 8.6 The input is deleted at the end of the job

The brief says DELETE purges "the transcript, instructions and output". This plan deletes the
transcript and instructions earlier, when the job ends, which keeps less sensitive data at rest.
DELETE still removes anything left and the output.

### 8.7 A cut-off summary fails

The brief forbids a summary of part of the input. A summary cut off at the output limit is not
that, but it is equally incomplete, so it is not stored either (P6).

### 8.8 Stage `summarize` and the 409 for the wrong kind

The contract already allows new stage values. The 409 (P10) follows step 33's use of 409 for
"the job exists but has no such resource".

### 8.9 The `REMOVED` scan narrows

See 6.8. The scan is a doc test, not lint, type or test configuration. Only the patterns that
the restored behaviour contradicts are removed.

### 8.10 No compose service for the model server

The brief leaves it out of scope unless it is simpler. A service in `compose.yaml` would pin
one server's image (untestable here), force the operator's choice among three, and break
`tests/test_deploy.py`'s guard against a model service. A `compose.override.yaml` snippet in the
docs is as simple for the operator and keeps the repo's compose file unchanged.

### 8.11 One step, two passes

The step is planned as one step, with the pass A / pass B seam of section 3, rather than two
steps. That is the orchestrator's choice to change.

### 8.12 `docs/ARCHITECTURE.md` and `docs/PLAN.md` changes made by this plan

- **`docs/PLAN.md`, "Re-scope" → "What changes":** a dated note, *Partly reversed on 2026-11-28
  (step 37), by the operator*. It says that summaries return under `/v1` only, on a local model
  only; that there is still no cloud model; and that analysis, the vault, entities and search
  stay removed. The status table is not touched.
- **`docs/ARCHITECTURE.md`:**
  - "Re-scope decisions → What changes": the same note.
  - "The service" and "Decisions": the Scope row; the State row (`summaries/`, the staged
    input); the Requests row; and a new Summaries row.
  - "Flow": the summary path.
  - "Module layout": `summarize/`.
  - "Domain models": the summary models, `JobKind` and `Stage`.
  - "Stage contracts": `Summarizer`, and `Backends.summarizer`.
  - "Runner and worker": the summary path and the heartbeat finding.
  - A new "Summarizing" subsection: the backends, the local-only rule, the fit checks and the
    messages.
  - "Jobs" and "Database identity": the dedupe key, `mark_purged`, and `0006_summaries` with
    schema 6.
  - "Data layout": `summaries/<job_id>/` and `uploads/<id>.json`.
  - "HTTP API": the routes, and the `/v1` summary rules.
  - "Configuration": the eight settings.
  - "Security": summaries are local only, the leak rules and what is stored.
  - "Testing": the stub.
  - "Known risks": a new risk, the Ollama version gate and the model server's own logs (which
    earmark does not control).
  - "Not planned": map-reduce stays the client's job.

## 9. Out of scope

- Map-reduce or chunking of a transcript; the contract states the maximum input instead.
- Per-request `max_tokens`, `temperature`, model choice or other sampling options; structured
  (JSON) output; tools.
- A summary that references a `/v1` transcription job by id.
- Any cloud model or Claude backend.
- Running the model server from earmark's `compose.yaml`.
- Streaming responses, and progress reporting during a generation.
- Cancelling a job; a delete-after-fetch flag.
- A CLI command for summaries (`earmark jobs` only labels them).
- Reasoning settings (`think`, reasoning effort); they are the server's configuration.
- Unloading Whisper while the model server runs; it is not needed on 128 GB (2.6).
- Speaker labels or any parsing of earshot's transcript; earmark passes it through verbatim.
