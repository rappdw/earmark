# Remote ASR: running a transcription server for earmark

Earmark can transcribe locally with faster-whisper, or send the audio to an
OpenAI-compatible speech-to-text server you run yourself. This guide covers the second
option (`EARMARK_ASR_BACKEND=remote`). It needs no optional extra.

On the DGX Spark the worker transcribes on the GPU itself, from the GPU image
(`Dockerfile.gpu`, [docs/deploy.md](deploy.md) section 7). A remote server is for a GPU on
another machine, or for another engine. Everything about the servers below comes from
their documentation and source as of 2026-11-20. **None of them was run by the earmark
build, because the build sandbox has no GPU**; where a statement depends on a real run
it says "not verified by the earmark build (no GPU)".

The examples use the placeholder hosts `gpu-box` and `spark`.

## 1. What earmark sends

A `POST` of `multipart/form-data` to `$EARMARK_ASR_BASE_URL/audio/transcriptions`
(streamed from disk in 64 KiB chunks, never held in memory) with these fields:

- `file`: the audio, named `audio.<ext>` with a matching content type;
- `model`: `EARMARK_ASR_MODEL`;
- `response_format=verbose_json`;
- `timestamp_granularities[]=segment`;
- `language`: only when `EARMARK_ASR_LANGUAGE` is set;
- header `Authorization: Bearer $EARMARK_ASR_API_KEY`: only when the key is set.

It expects JSON back with `text` and `segments[]` having `start`, `end` (seconds) and
`text`. Extra keys are ignored. If the server returns text but no segment timestamps,
earmark builds a one-segment transcript and the job gets a warning. Earmark does not
fall back to `response_format=json`: a server that rejects `verbose_json` fails the job
with the server's message.

To test a server by hand (scalar fields with `--form-string`):

```bash
curl -sS "$EARMARK_ASR_BASE_URL/audio/transcriptions" \
  -H "Authorization: Bearer $EARMARK_ASR_API_KEY" \
  -F file=@episode.mp3 \
  --form-string model=openai/whisper-large-v3-turbo \
  --form-string response_format=verbose_json \
  --form-string 'timestamp_granularities[]=segment'
```

## 2. Settings

| Setting | Meaning |
|---|---|
| `EARMARK_ASR_BACKEND=remote` | Use this backend. |
| `EARMARK_ASR_BASE_URL` | The server's base URL ending in `/v1`, for example `http://spark:8000/v1`. http or https, no credentials, query, or fragment. Required: the worker exits with a configuration error without it. |
| `EARMARK_ASR_MODEL` | The server's model id, for example `openai/whisper-large-v3`. The faster-whisper default `large-v3` usually gets a 404 from a server. |
| `EARMARK_ASR_API_KEY` | Sent as a bearer token when set. Printable ASCII, no spaces. Never logged. |
| `EARMARK_ASR_MAX_UPLOAD_MB` | Set it to the server's own limit so a too-large episode fails before uploading. Unset means no limit. |
| `EARMARK_ASR_TIMEOUT_SECONDS` | How long to wait for the answer (default 10800, three hours). A timeout is retried with backoff. |
| `EARMARK_ASR_LANGUAGE` | Sent as `language` when set. |
| `EARMARK_KEEP_AUDIO` | Keep the downloaded audio after transcription (default `false`). |

`EARMARK_ALLOW_PRIVATE_FETCH` is **not** needed and should stay `false`: the ASR server
is configured by you, so earmark connects to it directly and the private-address guard
that protects feed and enclosure downloads does not apply to it.

Test the setup with `earmark transcribe episode.mp3`, which uses the remote server when
`EARMARK_ASR_BACKEND=remote`.

## 3. vLLM with Whisper

On a GPU host, for example another Spark, vLLM serving Whisper returns `verbose_json` segments with absolute timestamps and has
API-key middleware. Run it from NVIDIA's arm64 CUDA 13 vLLM container
(`nvcr.io/nvidia/vllm:<YY.MM>-py3`; find the current tag on NGC or in the playbook at
build.nvidia.com/spark/vllm).

```bash
vllm serve openai/whisper-large-v3-turbo --api-key "$ASR_KEY" --port 8000
```

Use `openai/whisper-large-v3` for the larger model. Then set
`EARMARK_ASR_BASE_URL=http://spark:8000/v1`, `EARMARK_ASR_MODEL=openai/whisper-large-v3-turbo`
and `EARMARK_ASR_API_KEY` to the same key.

- **Raise the upload and decode limits.** The defaults suit short clips, not podcasts:
  `VLLM_MAX_AUDIO_CLIP_FILESIZE_MB` is 25 MB, `VLLM_MAX_AUDIO_DECODE_DURATION_S` is 600 s,
  and `VLLM_MAX_AUDIO_DECODE_BYTES` is 256 MiB (float32 PCM, about 70 minutes of 16 kHz
  audio). For three-hour episodes set, for example, 1024, 14400, and 4 GiB
  (`4294967296`). An oversized upload comes back as HTTP 400.
- **Whisper only.** In vLLM, `verbose_json` works only for Whisper models. Canary,
  Voxtral, and Parakeet-family models return 400 for it.
- **Chunking.** vLLM splits audio into 30 s chunks with a 1 s overlap and reports
  absolute segment times. Neighbouring segments can overlap slightly; earmark keeps
  starts monotonic and clamps ends.
- **M4A needs ffmpeg** in the container (vLLM decodes with soundfile and falls back to
  ffmpeg for MP4/M4A). Whether NVIDIA's container includes ffmpeg is not verified by the
  earmark build (no GPU).
- **GPU memory is shared** with any LLM on the Spark. Cap vLLM with
  `--gpu-memory-utilization`.
- Container tags change; some releases need a newer driver. Check the NVIDIA forums for
  the tag you pick.

## 4. Alternative: whisper.cpp `whisper-server`

Lighter than vLLM, with segment timestamps, but there are no prebuilt arm64 CUDA
binaries and it has no API key option. Not verified by the earmark build (no GPU).

Build for the Spark's GB10 (a recipe from the NVIDIA forums, 2026-05-30; one later
reply says it broke, so treat it as a starting point):

- base image `docker.io/nvidia/cuda:13.0.3-devel-ubuntu24.04`;
- configure with `-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="120;121"` (`120` alone
  fails on GB10);
- link the CUDA stubs (`/usr/local/cuda/lib64/stubs`, `-lcuda`);
- build only the `whisper-server` target.

Run it with the OpenAI route and a ggml model such as `ggml-large-v3-turbo.bin`:

```bash
whisper-server --host 0.0.0.0 --port 8000 \
  --model ggml-large-v3-turbo.bin \
  --inference-path /v1/audio/transcriptions --convert
```

`--convert` needs ffmpeg in the image (for M4A/AAC; MP3, WAV, and FLAC decode
built in). The server has no API key, so bind it to the tailnet or localhost and leave
`EARMARK_ASR_API_KEY` unset. Its base URL is `http://<host>:8000/v1`. Its README says to
run it sandboxed and without admin rights.

## 5. Alternative: LocalAI

LocalAI ships a CUDA 13 arm64 image
(`quay.io/go-skynet/local-ai:latest-nvidia-l4t-arm64-cuda-13`) with `whisper`
(whisper.cpp) and `parakeet-cpp` backends
(`cuda13-nvidia-l4t-arm64-whisper`, `cuda13-nvidia-l4t-arm64-parakeet-cpp`). Install
the backend you want, configure a model, and set `EARMARK_ASR_MODEL` to the LocalAI
model name. It returns `verbose_json` segments in seconds. This is the practical way to
run Parakeet with segment timestamps on the Spark. Not verified by the earmark build
(no GPU).

## 6. Parakeet notes

- **LocalAI `parakeet-cpp`** builds segments from word timestamps. Without words it
  returns one segment with no times, which earmark treats as "no timestamps": one
  segment and a job warning.
- **NVIDIA Parakeet NIM, HTTP API: not suitable.** It accepts only `json` or `text`
  responses (timestamps are gRPC-only) and WAV, Opus, or FLAC input, not MP3.
- **NVIDIA Nemotron Transcribe NIM: not suitable without conversion.** It returns
  `verbose_json` but accepts only WAV, FLAC, and Ogg Opus (no MP3 or M4A) and caps a
  request at 3600 s. Earmark does not transcode.
- **Community Parakeet servers** that accept `verbose_json` but return no segments work
  with earmark, giving one segment and a job warning.

## 7. x86_64 GPU hosts

speaches (faster-whisper behind `/v1/audio/transcriptions`, with `verbose_json`) works
on x86_64 GPU hosts. On arm64 its CUDA image falls back to CPU (speaches issue #620),
so it is not a GPU option on the Spark.

## 8. Security

- The server is operator-trusted and reached directly, with no private-address guard.
  Point `EARMARK_ASR_BASE_URL` only at a server you run.
- Prefer the tailnet or https. Over plain http the bearer key is visible on the path
  unless the path is a tailnet.
- Earmark never follows redirects from the server, ignores proxy environment variables,
  keeps no cookies, and caps the response size.

## 9. Troubleshooting

| Job error | Likely cause and fix |
|---|---|
| `HTTP 404` | The base URL lacks `/v1`, or `EARMARK_ASR_MODEL` is not a model the server serves. |
| `HTTP 401` / `HTTP 403` | Wrong or missing `EARMARK_ASR_API_KEY`. |
| `HTTP 400` / `HTTP 413` | The server's size or duration limit (see section 3), a model that does not support `verbose_json`, or a format the server cannot decode. Set `EARMARK_ASR_MAX_UPLOAD_MB` to fail early. |
| `did not answer within ... s` | The server is slower than `EARMARK_ASR_TIMEOUT_SECONDS`; raise it. The job is retried. |
| `HTTP 5xx`, `request ... failed` | The server is down or overloaded; the job is retried with backoff. |
| `answered with a redirect` | The base URL redirects (often http to https); use the final URL. |
| warning: `no segment timestamps` | The server returned text without segment times; the transcript is one segment. Use a server from section 3 to 5 for timestamps. |
