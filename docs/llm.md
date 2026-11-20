# Local LLM: running an analysis server for earmark

Earmark analyzes transcripts with Claude by default. It can instead send the analysis to
an OpenAI-compatible chat completions server you run yourself (vLLM, llama.cpp's
`llama-server`, Ollama, or LM Studio). This guide covers that option
(`EARMARK_LLM_PROVIDER=openai_compat`). It needs no optional extra.

Everything about the servers below comes from their documentation and source as of
2026-11-20. **None of them was run by the earmark build, because the build sandbox has no
local LLM server and no GPU**; where a statement depends on a real run it says "not
verified by the earmark build (no server, no GPU)". The client is tested against mock
transports and a stub server only.

The examples use the placeholder hosts `spark` and `gpu-box`.

## 1. What earmark sends

One non-streaming `POST` to `$EARMARK_LLM_BASE_URL/chat/completions` per request, with
these JSON keys in this order and no others (no `temperature`, `top_p`, `reasoning_effort`,
`tools`, or `stream_options`):

- `model`: `EARMARK_LLM_MODEL`;
- `messages`: a `system` message (the fixed analysis prompt) and a `user` message (the
  episode, the transcript, and the instructions);
- `max_tokens`: the output cap (section 3);
- `stream`: `false`;
- `response_format`: `{"type": "json_schema", "json_schema": {"name": "earmark_analysis",
  "strict": true, "schema": {...}}}` by default (section 8).

It sends `Authorization: Bearer $EARMARK_LLM_API_KEY` only when a key is set. It expects a
chat completion back, with the JSON answer in `choices[0].message.content`. Extra keys
(`reasoning`, `reasoning_content`, `usage`, `timings`) are ignored. A leading
`<think>...</think>` block and one surrounding code fence are stripped from the content;
nothing else is.

To test a server by hand, send a tiny request of the same shape:

```bash
curl -sS "$BASE/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $KEY" \
  -d '{
    "model": "MODEL",
    "messages": [
      {"role": "system", "content": "Answer with the JSON object only."},
      {"role": "user", "content": "Give a greeting."}
    ],
    "max_tokens": 200,
    "stream": false,
    "response_format": {
      "type": "json_schema",
      "json_schema": {
        "name": "greeting",
        "strict": true,
        "schema": {
          "type": "object",
          "properties": {"greeting": {"type": "string"}},
          "required": ["greeting"],
          "additionalProperties": false
        }
      }
    }
  }'
```

The reply should hold `{"greeting": "..."}` as a string in
`choices[0].message.content`. Leave out the `Authorization` header if the server has no key.

## 2. Settings

| Setting | Meaning |
|---|---|
| `EARMARK_LLM_PROVIDER=openai_compat` | Use the local server. |
| `EARMARK_LLM_BASE_URL` | The server's OpenAI-compatible base URL, ending in `/v1`, such as `http://spark:8000/v1`. Required. |
| `EARMARK_LLM_MODEL` | The server's model id. A `claude-*` id is refused (use the `anthropic` provider for Claude). The default is a Claude id, so you must set this. |
| `EARMARK_LLM_CONTEXT_TOKENS` | **Required.** The context window the server runs the model with, at least 8192. If the server handles several requests in parallel, use the per-request window. Read it as described per server below. |
| `EARMARK_LLM_API_KEY` | Sent as a bearer token when set. Optional. |
| `EARMARK_LLM_RESPONSE_FORMAT` | `json_schema` (default) or `json_object` (section 8). |
| `EARMARK_LLM_TIMEOUT_SECONDS` | How long to wait for one answer, default 3600. |

`EARMARK_ALLOW_PRIVATE_FETCH` is not needed: the local server is an operator-configured
endpoint and is reached directly. `EARMARK_LLM_EFFORT` and `EARMARK_LLM_FALLBACKS` are
Claude-only and do not apply.

Changing `EARMARK_LLM_MODEL` creates new cached analyses, because the model is part of the
cache key. Changing the response format, the context, or the timeout does not.

## 3. Long episodes

If the whole transcript, the prompt, the schema, and the output cap fit in
`EARMARK_LLM_CONTEXT_TOKENS`, earmark makes one request. Otherwise it runs map-reduce:

1. The transcript is cut into parts at segment boundaries (a single segment longer than a
   part is split at sentence ends, and the analysis gets a warning). Every segment is in
   exactly one part, and the `[HH:MM:SS]` markers stay absolute.
2. One request per part extracts a partial analysis with the same schema.
3. Entities repeated across parts are merged in code (normalised names, union of
   timestamps), then one request combines the part notes into the final analysis. If the
   notes are too long for one request they are combined in groups first.

Finished parts are kept on disk while the job runs, so a retry after a transient failure
does not redo them. A map-reduced analysis carries a warning that says so, because it can
be coarser than a one-request analysis. A larger context means fewer parts and a better
summary.

Earmark has no common way to count tokens on these servers, so it uses a conservative
estimate: three UTF-8 bytes per token, plus four tokens per transcript line, plus the
schema and a fixed allowance for the chat template. That overestimates modern tokenizers
(Llama 3 and later, Qwen 2 and later, Gemma, Mistral Tekken). Old small-vocabulary
tokenizers and CJK text can exceed it; if the server then rejects a part as too long,
lower `EARMARK_LLM_CONTEXT_TOKENS`.

The output cap is `min(16384, context / 4)`. After a `length` stop, one retry uses
`min(32768, context / 2)`.

## 4. vLLM on the DGX Spark (recommended)

vLLM and llama.cpp are recommended because they fail loudly on an overlong prompt. Not
verified by the earmark build (no server, no GPU).

- Use NVIDIA's arm64 vLLM container for the Spark, and serve the model with:

  ```bash
  vllm serve <model> --max-model-len <N> --api-key <key>
  ```

- For a reasoning model add `--reasoning-parser <name>`; without it the thinking ends up
  in the content (earmark strips a leading `<think>` block, but not other formats).
- Read the window with `curl http://spark:8000/v1/models`: `data[].max_model_len` is the
  value for `EARMARK_LLM_CONTEXT_TOKENS`.
- vLLM supports `json_schema` and `json_object`. A prompt over the window gets an HTTP 400
  ("This model's maximum context length is N tokens..."), which earmark reports.
- If the Spark also runs the ASR server, share the GPU memory with
  `--gpu-memory-utilization`.

## 5. llama.cpp `llama-server`

Not verified by the earmark build (no server, no GPU).

- Start it with `-c <N>`, and note `--parallel`: the context is divided between slots, so
  the per-request window is `-c` divided by `--parallel`. Read the per-slot value from
  `curl http://gpu-box:8080/v1/models`: `data[0].meta.n_ctx`.
- Use `--jinja` so the model's chat template is applied, `--reasoning-format deepseek` for
  reasoning models (the thinking goes to `reasoning_content`), and `--api-key <key>`.
- `json_schema` and `json_object` are both supported; the schema becomes a grammar. A
  context overflow is an HTTP 400 with type `exceed_context_size_error`, which earmark
  reports.

## 6. Ollama

Not verified by the earmark build (no server, no GPU).

**Warning:** through the OpenAI-compatible API, Ollama truncates a prompt that is longer
than its context **without an error**; only a server log line says so. Earmark cannot
detect this, and it would break the rule that a transcript is never truncated. Set
`OLLAMA_CONTEXT_LENGTH` (or `num_ctx` in a Modelfile) to at least
`EARMARK_LLM_CONTEXT_TOKENS`, and check the loaded value with `ollama ps` or
`curl http://gpu-box:11434/api/ps` (`context_length`). The `/v1` API cannot set it.
`OLLAMA_NUM_PARALLEL` affects memory use.

- Base URL: `http://gpu-box:11434/v1`.
- Ollama ignores the API key.
- It maps `json_schema` and `json_object` onto its native `format`.

## 7. LM Studio

Not verified by the earmark build (no server, no GPU).

- It supports `json_schema` (use the default format).
- The loaded context is shown in the app, or with `lms ps`. LM Studio can lower a
  requested context to fit memory, so read the loaded value.
- Base URL: `http://gpu-box:1234/v1`.

## 8. Choosing `json_schema` or `json_object`

Keep `json_schema` unless the server rejects it (an HTTP 400 that names `response_format`;
the job error suggests the switch). With `EARMARK_LLM_RESPONSE_FORMAT=json_object` earmark
sends `{"type": "json_object"}` and appends the schema to the user message, and relies on
the model to follow it. In either mode, an answer that is not valid against the schema gets
exactly one repair request, and a second failure fails the job.

## 9. Security

The server is operator-trusted and reached directly, with no address guard, so point the
URL only at a server you run. The transcript is sent to that server. Prefer the tailnet or
https: over plain http the key is visible on the path. Earmark never follows redirects from
the server, ignores proxy environment variables, keeps no cookies, caps the response size,
and puts only the host in error messages. The key is never logged.

## 10. Troubleshooting

| Job error | Fix |
|---|---|
| HTTP 404 | The base URL does not end in `/v1`, or the model id is wrong. |
| HTTP 401 or 403 | Check `EARMARK_LLM_API_KEY`. |
| HTTP 400 naming `response_format` | Set `EARMARK_LLM_RESPONSE_FORMAT=json_object`. |
| "the model rejected part ... as too long" | Lower `EARMARK_LLM_CONTEXT_TOKENS` (the estimate undercounts for this model), or raise the server's window. |
| "did not answer within ... s" | Raise `EARMARK_LLM_TIMEOUT_SECONDS`; a long episode on a slow model takes a long time. |
| "does not match the analysis schema after one repair attempt" | The model is too small for structured output (LM Studio's docs warn about models below about 7B). Use a larger model. |

## 11. Live test

`tests/test_openai_compat_live.py` runs a one-shot and a map-reduce analysis against a real
server. It is skipped unless `LLM_TEST_BASE_URL` is set (with `LLM_TEST_MODEL`, and
optionally `LLM_TEST_CONTEXT_TOKENS`, `LLM_TEST_API_KEY`, `LLM_TEST_RESPONSE_FORMAT`):

```bash
LLM_TEST_BASE_URL=http://spark:8000/v1 LLM_TEST_MODEL=<model> \
  uv run pytest -m network tests/test_openai_compat_live.py -rs
```
