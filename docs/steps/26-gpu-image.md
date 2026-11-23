# Step 26: GPU transcription inside the image

**Read this first.** Nothing about the GPU image can be built or run in the build sandbox,
because it has no Docker and no GPU. CI does not build it either (section 2.9). This step
delivers:

- a Dockerfile;
- static tests that pin it as tightly as text allows;
- a CI lint pass;
- worker, doctor and compose changes, tested on fakes;
- a documented smoke test for the operator.

**The operator's Spark smoke test (section 7.1 here, `docs/deploy.md` 7.3) is the only real check of the GPU path.**
Until it has passed, nobody may say that the GPU image builds, that it sees the GB10, or
that it transcribes. The step can be planned as briefed. No product decision is needed.
The engineering choices the brief left open are in section 8.

## 1. Outcome

- `Dockerfile.gpu` builds a GPU image for the worker:
  - It builds on `nvidia/cuda:12.9.1-cudnn-devel-ubuntu24.04` and runs on
    `nvidia/cuda:12.9.1-cudnn-runtime-ubuntu24.04`.
  - Python is a uv-managed CPython 3.13.18, and the venv is installed from the lock as in
    the CPU image.
  - It compiles CTranslate2 `v4.9.0`, the locked version and the `CT2_VERSION` default,
    with CUDA for `sm_121`, following earshot's recipe and with earshot's attribution.
  - It builds the Python bindings against the venv's interpreter and swaps them into
    `/app/.venv`. After the swap, nothing can put the PyPI wheel back.
  - The runtime stage checks the swap at build time.
- The CPU `Dockerfile` is byte-for-byte unchanged and still built by CI.
- `compose.yaml`:
  - The `asr` vLLM service, its `asr-models` volume and the `gpu` profile are gone.
  - `api` runs the CPU image.
  - `worker` runs the GPU image with `gpus: all`, `EARMARK_ASR_DEVICE=cuda`, and a
    `float16` compute type by default.
- `EARMARK_ASR_BATCH_SIZE` (default 0, off) turns on faster-whisper's
  `BatchedInferencePipeline`. When it is on, it is part of the transcript cache key.
- `earmark doctor` gains an `asr cuda` line: the CUDA device count, and whether `float16`
  is a supported CUDA compute type.
- `container.yml` validates the new compose file, and runs `docker build --check` on
  `Dockerfile.gpu` without building it.
- `docs/deploy.md` section 7 holds the Spark smoke test and its remedies, including the
  `v4.6.0` fallback.

## 2. Findings

Everything below was checked on 2026-11-23. "Verified" means read from the primary
source or run in a scratch venv under the scratchpad, which was deleted afterwards.
Nothing was installed into the repo's `.venv`.

### 2.1 Base images (verified: Docker Hub API and registry manifests)

Both are pinned by their multi-arch index digest, which the registry confirmed:

| Tag | Index digest | arm64 compressed |
|---|---|---|
| `nvidia/cuda:12.9.1-cudnn-devel-ubuntu24.04` | `sha256:a2e1e2360c85298ac47ec2543b406ab1e8cec42e31ee47e4d32140ebc82e1067` | 5.94 GB |
| `nvidia/cuda:12.9.1-cudnn-runtime-ubuntu24.04` | `sha256:d02c4310b6d57ca0b16cd80298bdb33a74187baafe2eccd8a6a16180ddc90802` | 3.09 GB |

- **Contents.** From the image config `Env`:
  - CUDA 12.9.1 (cudart 12.9.79);
  - cuBLAS 12.9.1.4 (`libcublas-12-9`);
  - cuDNN 9.10.2.21 (`libcudnn9-cuda-12`);
  - `NVARCH=sbsa`;
  - `LD_LIBRARY_PATH=/usr/local/cuda/lib64`;
  - `NVIDIA_VISIBLE_DEVICES=all` and `NVIDIA_DRIVER_CAPABILITIES=compute,utility`.
- **Entrypoint and compat package.** The runtime image's entrypoint is
  `/opt/nvidia/nvidia_entrypoint.sh`, which ours overrides. Its history shows that it
  installs `cuda-compat-12-9`.
- **Other arm64 tags.** Tags from 12.6 to 13.5 exist for arm64, the newest being
  `13.5.0-cudnn-devel-ubuntu24.04` (2026-11-05). 12.9.2 exists too (2026-05-20). 12.9.1 was
  chosen because it is the CUDA in earshot's proven base (2.2).
- **sm_121 needs CUDA 12.9.** The CUDA 12.9 release notes, section 1.2.1, say: "CUDA
  Toolkit 12.9 adds compiler target support for SM architecture 10.3 … and 12.1
  (`sm_121`)" (verified). So 12.9 is the minimum for the GB10.
- **The host driver.** `NVIDIA_REQUIRE_CUDA=cuda>=12.9 brand=…` is a list of
  alternatives, so the Spark's newer driver (CUDA 13) satisfies it. This is not verified
  on the Spark.

### 2.2 NGC versus the slim pair

- **What earshot ran on.** NVIDIA's PyTorch 25.06 release notes give Ubuntu 24.04, "NVIDIA
  CUDA 12.9.1", and Python 3.12, inferred from `/usr/local/lib/python3.12/dist-packages/torch`
  (verified). The notes do not state its cuDNN or cuBLAS versions.
- **What upstream does.** Upstream CTranslate2's own `docker/Dockerfile` at v4.9.0 builds
  in `nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04` and runs on a slim `nvidia/cuda` base
  with apt's `libcublas` and `libcudnn9-cuda-12` (verified).
- **Conclusion.** The slim pair has the same CUDA and OS as earshot's base, minus torch. It
  is also the pattern upstream uses. See section 8, D1.

### 2.3 CTranslate2 v4.9.0's build (verified: `CMakeLists.txt`, `python/`, `CHANGELOG.md` at the tag)

- **The tag.** `v4.9.0` is commit `a22ba1c63ee35d1a0d25db149aa2a66d36a6a732`, and
  `python/ctranslate2/version.py` says `"4.9.0"`.
- **CMake.**
  - It needs `cmake_minimum_required(VERSION 3.24)`. Ubuntu 24.04's apt `cmake` is 3.28.3
    (verified on packages.ubuntu.com).
  - 4.9.0 "Replace[s] the deprecated `FindCUDA` CMake module with native CMake CUDA
    support" (#2067, two weeks old). `enable_language(CUDA)` and
    `find_package(CUDAToolkit 11.0)` are used, and the architectures come from
    `CMAKE_CUDA_ARCHITECTURES`. So `-DCMAKE_CUDA_ARCHITECTURES=121` produces `sm_121`
    machine code plus `compute_121` PTX.
  - The configure log prints `-- CUDA architectures: <list>`.
- **cuDNN and cuBLAS.**
  - With `WITH_CUDNN=ON`, `libcudnn` is linked directly even with `CUDA_DYNAMIC_LOADING`.
    The CMake comment says: "libcudnn.so is a shim layer … so we explicitly link against
    it". So the runtime needs `libcudnn.so.9`, which the cudnn-runtime image has.
  - `cublas_stub.cc` loads `libcublas.so.12` with `dlopen("libcublas.so." STR(CUBLAS_VER_MAJOR))`.
  - cudart is linked statically: the CMake CUDA default, with no `CUDA::cudart` when
    dynamic loading is on. So `libcuda.so.1` is expected to be loaded with `dlopen` at
    runtime, not linked. This is inferred, not verified.
- **CCCL.** CCCL 2.7.0 has been the Thrust submodule since 4.8.0. `git clone --recursive`
  is required.
- **Install location.** `include(GNUInstallDirs)` with a non-`/usr` prefix on a Debian
  system installs libraries into `<prefix>/lib`.
- **Python bindings.**
  - `python/pyproject.toml` declares
    `requires = ["setuptools", "wheel", "pybind11==2.13.6"]`.
  - `python/install_requirements.txt` lists `pybind11==2.13.6`, `setuptools` and `wheel`.
  - `setup.py` reads `CTRANSLATE2_ROOT`, adds `<root>/include` and `<root>/lib`, and adds
    `-Wl,-rpath,<root>/lib`, so the extension finds `libctranslate2` without
    `LD_LIBRARY_PATH`.
  - The wheel does not bundle `libctranslate2.so`.
- **What upstream ships.**
  - Linux aarch64 wheels: CPU only (`-DWITH_MKL=OFF -DOPENMP_RUNTIME=COMP
    -DWITH_OPENBLAS=ON -DWITH_RUY=ON`).
  - x86_64 wheels: CUDA 12.8, `WITH_CUDNN=OFF`, `CUDA_DYNAMIC_LOADING=ON`.

  So nobody upstream builds 4.9.0 for aarch64 with CUDA.

### 2.4 CTranslate2 v4.6.0, the fallback (verified: `CMakeLists.txt`, `python/` at the tag; CMake 3.28.3's `FindCUDA/select_compute_arch.cmake`)

- **Architecture selection.**
  - v4.6.0 uses `find_package(CUDA 11.0)`, the old `FindCUDA` module, and
    `cuda_select_nvcc_arch_flags(ARCH_FLAGS ${CUDA_ARCH_LIST})`, with `CUDA_ARCH_LIST`
    defaulting to `Auto`. **It ignores `CMAKE_CUDA_ARCHITECTURES`.**
  - `Auto` tries to detect the build machine's GPU. `docker build` has none, so it prints
    "Automatic GPU detection failed. Building for common architectures." CMake 3.28's
    common list for CUDA ≥ 11.1 ends at `8.6+PTX`.
- **What that means for earshot.** Its "stock v4.6.0, no PTX tricks" build for the GB10
  most likely ran by JIT-compiling the `compute_86` PTX at load. This is inferred from the
  sources; earshot's build log would show it. With `--build-arg CT2_VERSION=v4.6.0`, the
  earmark image will do the same. The first model load then pays the JIT cost, and the JIT
  cache sits in `$HOME/.nv`, which the container does not keep (7.2).
- **Python build files.** `python/pyproject.toml` **does not exist** at v4.6.0, and
  `install_requirements.txt` pins `pybind11==2.11.1`.
- **pybind11 2.11.1 on Python 3.13.** PyPI has `ctranslate2-4.6.0-cp313-…aarch64.whl`,
  built by upstream CI with `pip install -r python/install_requirements.txt`. So that
  pybind11 compiles for 3.13.

### 2.5 Sandbox experiments (verified: scratch venvs under the scratchpad, uv 0.13.9, aarch64)

- **The interpreter.**
  - uv 0.13.9 offers `cpython-3.13.18-linux-aarch64-gnu`, the CPU image's 3.13.18.
  - `uv python install 3.13.18` with `UV_PYTHON_DOWNLOADS=manual` installs it, with
    `include/python3.13/Python.h`.
  - Its sysconfig has `CC=cc -pthread`, so setuptools compiles with the system compiler.
  - uv also creates a minor-version link directory, `cpython-3.13-linux-aarch64-gnu`.
    Copy the whole install directory.
- **The bindings build.** A pybind11 setuptools extension laid out like CTranslate2's
  `setup.py` was built in two ways. Both linked against a shared library in a fake
  `*_ROOT/lib` with an rpath, and both imported and ran:
  - `uv build --wheel --python 3.13.18` (isolated; pybind11 2.13.6);
  - `uv build --wheel --no-build-isolation --python <build venv>/bin/python`, with the
    build venv made by `uv venv` and `uv pip install -r` of the requirements
    (pybind11 2.11.1, no `pyproject.toml`).

  The second form works for both tags, so the plan uses it.
- **The swap** (a stand-in `ctranslate2==4.9.0` wheel over the real PyPI 4.9.0 in a uv
  project):
  - `uv pip install --python <venv> --no-deps --reinstall <wheel>` replaces it, and removes
    the PyPI wheel's `ctranslate2.libs/` directory.
  - `uv run --no-sync` keeps the swap.
  - **`uv sync --locked` and plain `uv run` both reinstall the PyPI wheel**, and
    `ctranslate2.libs/` comes back. This is earshot's pitfall, now confirmed.
- **The PyPI `ctranslate2==4.9.0` aarch64 wheel**, in a scratch venv:
  - `get_cuda_device_count()` is `0`.
  - `get_supported_compute_types("cpu")` is `{float32, int8, int8_float32}`.
  - **`get_supported_compute_types("cuda")` raises `ValueError: This CTranslate2 package
    was not compiled with CUDA support`.**
  - `ctranslate2.models.Whisper` has `device` and `compute_type` properties.
  - The loaded library is
    `…/site-packages/ctranslate2.libs/libctranslate2-4bc7d187.so.4.9.0`, as
    `/proc/self/maps` shows. A source build loads `<prefix>/lib/libctranslate2.so.4…`
    instead, so the maps check in 4.1 tells the two apart.
- **The sandbox toolchain.** The sandbox has gcc, g++ and cmake, but no CUDA toolkit, so
  no part of the CUDA build could be tried.

### 2.6 What `get_cuda_device_count()` proves (verified: `src/cuda/utils.cc` at v4.9.0)

- `get_gpu_count()` is `cudaGetDeviceCount`, and returns 0 on any error. **A build for the
  wrong architecture still reports the device.** It fails at the first kernel ("no kernel
  image is available for execution on the device").
- So the smoke's step 4, a real transcription, is the proof, and step 2's count only shows
  that CUDA and the device are visible. The recipe's "prints 0 for the wrong architecture"
  is corrected in its decision record.
- The Python binding's `get_supported_compute_types` adds `float16` when the device
  supports it.

### 2.7 faster-whisper 1.2.1 `BatchedInferencePipeline` (verified: the locked wheel, sha256 `79a66ad5…26a7`, read in the scratchpad)

- **Construction and signature.**
  - It is exported from `faster_whisper`, and constructed as
    `BatchedInferencePipeline(model)`, which stores the `WhisperModel` as `.model`.
  - `transcribe(audio, language=None, task="transcribe", log_progress=False,
    beam_size=5, …, without_timestamps=True, …, vad_filter=True, vad_parameters=None, …,
    batch_size=8, …)` returns `(generator of Segment, TranscriptionInfo)`, like
    `WhisperModel.transcribe`.
- **Behaviour.**
  - It accepts a numpy waveform, so earmark's pre-decoded samples work.
  - `vad_filter=False` with audio over 30 s raises
    `RuntimeError("No clip timestamps found…")`. earmark always passes `vad_filter=True`.
  - It forces `condition_on_previous_text=False`. Its default `without_timestamps=True`
    gives one segment per VAD chunk of at most 30 s, with chunk-level times restored to
    absolute positions.
  - It builds the features for the whole file before decoding: about 1.5 MB per 30 s
    chunk for `large-v3`'s 128 mels, so about 550 MB for 3 h of speech.
  - `info.duration` is the full duration.
  - An invalid language raises `ValueError("… is not a valid language code …")` in the
    `Tokenizer` constructor, inside the `transcribe(...)` call itself, as in sequential
    mode. So the engine's existing mapping still applies.
- **The model.** `WhisperModel.model` is the `ctranslate2.models.Whisper`.

### 2.8 Compose and BuildKit (partly verified)

- **`gpus` in compose.**
  - The compose-spec schema (compose-go `schema/compose-spec.json`) has a service
    property `gpus` that is `"all"` or a list (verified).
  - A docker/docs pull request (#21980) says it needs Docker Compose 2.30.0 or later. The
    published reference page links to a detail page that returned 404, so this is not
    verified from the published docs.
  - The CI runner's compose validates it on the first push.
  - The Spark's compose version is unknown, so the smoke checks it.
- **`docker build --check`** (verified: docs.docker.com/build/checks):
  - It "only runs the checks" instead of building, and exits non-zero on violations.
  - It needs Buildx 0.15 or later.
  - Its sample output loads base-image metadata, so a wrong digest fails it.
- **Ubuntu 24.04 packages** (verified: packages.ubuntu.com):
  - `libopenblas0-pthread` 0.3.26 ships `…/openblas-pthread/libopenblas.so.0`, registered
    as the `libopenblas.so.0` alternative.
  - `libgomp1` exists.
  - `libopenblas-dev` pulls in the pthread variant.

### 2.9 Why CI does not build the GPU image

- **Disk.** The free `ubuntu-24.04-arm` runner has about 14 GB of free disk. The devel
  base alone is 5.94 GB compressed, and extracted it is roughly twice that. On top of that
  come the CTranslate2 checkout with its submodules, the build tree, the venv and the
  runtime base (3.09 GB compressed). It does not fit without deleting the runner's
  preinstalled toolchains, and that is fragile.
- **Time.** The CUDA compile ran for "tens of minutes" on the Spark's 20 cores, though that
  was the multi-architecture v4.6.0 build (2.4). The runner has 4 cores, and the container
  job's timeout is pinned at 30 minutes or less (`tests/test_ci.py`).
- **Signal.** The runner has no GPU. A CI build could catch only a compile error. The
  checks that matter are the device count, the `float16` list and a model load on the GB10.
  The operator's build on the Spark catches the same compile error.
- **Decision.** CI runs `docker build --check -f Dockerfile.gpu .`, which is cheap:
  Dockerfile checks plus base-image metadata. `make check` runs the static tests. A manual
  `workflow_dispatch` build was considered and rejected for the same disk reason.

### 2.10 Not verified (say so; do not claim otherwise)

- That `Dockerfile.gpu` builds at all, or how long it takes. CTranslate2 4.9.0 has never
  been built for aarch64 with CUDA by anyone known.
- The image size. Expect several GB on top of the 3.09 GB compressed runtime base.
- That `import ctranslate2` succeeds in the runtime stage without a GPU. earshot did it,
  but on NGC. This rests on `libcuda.so.1` not being a `NEEDED` entry (2.3).
- `gpus: all` together with `cap_drop: ALL` and `no-new-privileges` on the Spark. The
  device injection is done by the NVIDIA runtime hook, outside the container, so it should
  work.
- Whether the `ubuntu` user (UID 1000) exists in the `nvidia/cuda` 24.04 images. The plan
  handles both cases.
- Whether Dependabot reads `Dockerfile.gpu`.
- NGC 25.06's cuDNN and cuBLAS versions.
- What `compute_type="auto"` resolves to on the GB10.
- Batched speed on the GB10.

### 2.11 Dead ends

- **Do not use isolated `uv build` for the bindings.** v4.6.0 has no `pyproject.toml`, so
  isolation would lack pybind11.
- **Do not copy CPython from `python:3.13.18-slim-trixie` into the Ubuntu image.** trixie's
  glibc 2.41 is newer than noble's 2.39.
- **No `pip`.** The uv venv has none, and the brief forbids a `pip install` that could pull
  the CPU wheel.
- **Do not set `CUDA_ARCH_LIST` for v4.6.0.** It would change the proven configuration.
- **Do not `COPY --from=build /usr/local/lib/libctranslate2.so*`.** Symlink handling of a
  wildcard copy is uncertain. Install under `/opt/ctranslate2` and copy the `lib`
  directory instead.
- **No `RUN <<EOF` heredocs.** `tests/test_deploy.py`'s line joiner does not understand
  them.
- **Do not treat `get_cuda_device_count() >= 1` as proof of the build.**
- **Do not purge `cuda-compat-12-9`.** Upstream CTranslate2's runtime does, citing
  nvidia-docker#1515. Here the host driver is newer than 12.9, so the toolkit ignores the
  package. It is kept, and the symptom and its remedy are documented (7.2).

## 3. Files

| File | Change |
|---|---|
| `Dockerfile.gpu` | **New.** The GPU image (4.1). |
| `Dockerfile`, `.dockerignore` | **Unchanged.** `.dockerignore` serves both builds. Do not add `Dockerfile.gpu.dockerignore`. |
| `compose.yaml` | Remove `asr`, the `asr-models` volume and the `gpu` profile. `worker` becomes the GPU image (4.2). |
| `.github/workflows/container.yml` | Compose validation without `--profile gpu`, plus a new `docker build --check` step (4.3). |
| `.env.example` | Add `EARMARK_ASR_BATCH_SIZE`. Comment out `EARMARK_ASR_COMPUTE_TYPE`. Remove `ASR_GPU_MEMORY_UTILIZATION`. Fix the telemetry comment (4.5). |
| `README.md` | One new settings row, `EARMARK_ASR_BATCH_SIZE`, in the settings table. Nothing else. |
| `docs/deploy.md` | Sections 1, 4, 7 and 17 (4.6). The rest waits for step 27. |
| `CLAUDE.md` | Two pointers to the "Planned (step 26)" section, which no longer exists, and one new rule (4.7). |
| `src/earmark/config.py` | `asr_batch_size` (4.8). |
| `src/earmark/transcribe/faster_whisper.py` | `WhisperOptions.batch_size`, `WhisperRuntime.supported_compute_types`, `BatchedWhisper`, the profile, the load log, the `check_device` message, `CudaReport` and `cuda_report` (4.9–4.10). |
| `src/earmark/doctor/checks.py` | The `asr cuda` check, and the batch size in the `asr` detail (4.11). |
| `tests/dockerfile_helpers.py` | **New.** `tests/test_deploy.py`'s Dockerfile parsers, moved and parameterised by text, so both test files share them (6.1). |
| `tests/test_deploy.py` | Use the helpers. Update the compose tests (6.2). |
| `tests/test_deploy_gpu.py` | **New.** The static tests for `Dockerfile.gpu` (6.1). |
| `tests/test_ci.py` | Container workflow tests (6.3). |
| `tests/test_docs.py` | The deploy-doc status test, and the GPU smoke doc test (6.4). |
| `tests/whisper_fakes.py` | `FakeRuntime` and `StubExtra` gain CUDA compute types and a batched-pipeline stub (6.5). |
| `tests/test_faster_whisper.py`, `tests/test_doctor.py`, `tests/test_config.py` | New and updated tests (6.1, 6.5). |

`docs/ARCHITECTURE.md` and the recipe record
`docs/decisions/2026-11-23-gpu-build-recipe.md` were already updated by this plan (section
8).

## 4. Interfaces

### 4.1 `Dockerfile.gpu`

Write it instruction by instruction as below. The snippets are the contract the static
tests pin. Comments and line breaks are free, but `\` continuations must join into the
logical lines the tests parse. No heredocs, no `# syntax=` line, and `ENV KEY=value` form
only, because BuildKit's `LegacyKeyValueFormat` check would fail `--check`.

**Header comment, top of file.** Start with a description: the GPU image for the worker,
built and run only by the operator, never by CI or the sandbox, with a pointer to
`docs/deploy.md` section 7. Then the attribution, verbatim from the recipe:

```dockerfile
# CUDA CTranslate2 build adapted from earshot (https://github.com/rappdw/earshot,
# docker/Dockerfile), MIT License, Copyright (c) 2026 Dan Rapp.
```

Then a licence note containing the words "NVIDIA" and "do not publish", for example:
"NVIDIA's CUDA images are under NVIDIA's licence terms; do not publish a built image
without checking them."

**Global build arguments**, before the first `FROM`:

```dockerfile
ARG CT2_VERSION=v4.9.0
ARG CUDA_ARCH=121
```

- `CT2_VERSION` is a CTranslate2 git tag. The default is `v` plus the `ctranslate2`
  version in `uv.lock`.
- `CUDA_ARCH` is a `CMAKE_CUDA_ARCHITECTURES` value. `121` is the GB10, which `nvidia-smi
  --query-gpu=compute_cap` prints as `12.1`.

**Stage `uv`:** the same line as the CPU `Dockerfile`:
`FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0… AS uv`.

**Stage `build`:**

```dockerfile
FROM nvidia/cuda:12.9.1-cudnn-devel-ubuntu24.04@sha256:a2e1e2360c85298ac47ec2543b406ab1e8cec42e31ee47e4d32140ebc82e1067 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_PYTHON_DOWNLOADS=manual \
    UV_PYTHON=3.13.18 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake git libopenblas-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN uv python install 3.13.18
WORKDIR /src
COPY pyproject.toml uv.lock .python-version README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --extra transcribe
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --extra transcribe
ARG CT2_VERSION
ARG CUDA_ARCH
RUN --mount=type=cache,target=/root/.cache/uv \
    git clone --depth 1 --branch "${CT2_VERSION}" --recursive https://github.com/OpenNMT/CTranslate2 /tmp/ct2 \
 && cmake -S /tmp/ct2 -B /tmp/ct2/build \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX=/opt/ctranslate2 \
      -DWITH_CUDA=ON -DWITH_CUDNN=ON -DCUDA_DYNAMIC_LOADING=ON \
      -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCH}" \
      -DWITH_MKL=OFF -DWITH_OPENBLAS=ON -DWITH_RUY=ON -DOPENMP_RUNTIME=COMP \
 && cmake --build /tmp/ct2/build -j"$(nproc)" --target install \
 && uv venv /opt/ct2-build \
 && uv pip install --python /opt/ct2-build/bin/python -r /tmp/ct2/python/install_requirements.txt \
 && CTRANSLATE2_ROOT=/opt/ctranslate2 uv build --wheel --no-build-isolation \
      --python /opt/ct2-build/bin/python --out-dir /tmp/ct2-wheel /tmp/ct2/python \
 && uv pip install --python /app/.venv/bin/python --no-deps --reinstall /tmp/ct2-wheel/ctranslate2-*.whl \
 && rm -rf /tmp/ct2 /tmp/ct2-wheel /opt/ct2-build
```

Rules for this stage:

- **Python.**
  - `UV_PYTHON` and the `uv python install` argument are both `3.13.18`, the patch of the
    CPU image's `python:3.13.18-slim-trixie`.
  - `UV_PYTHON_PREFERENCE=only-managed` keeps uv off any system Python.
  - `UV_PYTHON_DOWNLOADS=manual` allows only the explicit install.
  - `uv python install` may also drop links into `~/.local/bin`, which is harmless in
    this stage. `--no-bin` is allowed if uv 0.13.9 accepts it, but is not required.
- **The venv.** Both `uv sync` calls carry `--locked --no-dev --extra transcribe`, and the
  second one also carries `--no-editable`. There is no `EXTRAS` argument: this image
  always has the `transcribe` extra.
- **The cmake flags.** They are exactly the recipe's, plus `-DCMAKE_INSTALL_PREFIX=/opt/ctranslate2`
  (deviation D2). `CMAKE_CUDA_ARCHITECTURES` is quoted and taken from the build argument.
- **The swap is the last uv command in the file.** Nothing after it runs `uv`, `pip` or
  `python -m pip`.
- **Build arguments.** `ARG CT2_VERSION` and `ARG CUDA_ARCH` are re-declared in the stage,
  without defaults, so the global defaults apply. Without the re-declaration they are
  empty inside the stage.

**Stage `runtime`:**

```dockerfile
FROM nvidia/cuda:12.9.1-cudnn-runtime-ubuntu24.04@sha256:d02c4310b6d57ca0b16cd80298bdb33a74187baafe2eccd8a6a16180ddc90802 AS runtime
ARG UID=1000
ARG GID=1000
ARG CT2_VERSION
RUN test "$UID" -ne 0 && test "$GID" -ne 0 \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates libopenblas0-pthread libgomp1 \
 && rm -rf /var/lib/apt/lists/* \
 && if id -u ubuntu >/dev/null 2>&1; then userdel --remove ubuntu; fi \
 && groupadd --gid "$GID" --non-unique earmark \
 && useradd --uid "$UID" --gid "$GID" --non-unique --create-home \
      --home-dir /home/earmark --shell /usr/sbin/nologin earmark \
 && install -d -o earmark -g earmark -m 0750 /data
COPY --from=build /opt/python /opt/python
COPY --from=build /opt/ctranslate2/lib /opt/ctranslate2/lib
COPY --from=build /app/.venv /app/.venv
RUN echo /opt/ctranslate2/lib > /etc/ld.so.conf.d/ctranslate2.conf \
 && ldconfig \
 && /app/.venv/bin/python -c "import sys, ctranslate2, faster_whisper; from faster_whisper import BatchedInferencePipeline; want = sys.argv[1].removeprefix('v'); assert ctranslate2.__version__ == want, (ctranslate2.__version__, want); assert ctranslate2.__file__.startswith('/app/.venv/'), ctranslate2.__file__; libs = {line.split()[-1] for line in open('/proc/self/maps') if 'libctranslate2' in line}; assert libs and all(p.startswith('/opt/ctranslate2/lib/') for p in libs), libs; print('ctranslate2', ctranslate2.__version__, sorted(libs))" "$CT2_VERSION"
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    EARMARK_HOST=0.0.0.0 \
    EARMARK_DATA_DIR=/data \
    HF_HOME=/data/huggingface \
    HF_HUB_DISABLE_TELEMETRY=1
WORKDIR /home/earmark
USER earmark:earmark
EXPOSE 8787
ENTRYPOINT ["earmark"]
CMD ["serve"]
```

Rules for this stage:

- **The user RUN.** It ends with `install -d -o earmark -g earmark -m 0750 /data`, as in
  the CPU image, so the existing `runtime dirs` test applies to both files. It removes the
  base image's `ubuntu` user if one exists, so `id -un` for UID 1000 is `earmark`.
- **The build-time check.** It runs in the final stage, after the three copies and before
  `USER`. What it proves:
  - the swapped version is `CT2_VERSION` without its `v`;
  - the bindings live in the venv;
  - the loaded `libctranslate2` is the source build (2.5);
  - the bindings, faster-whisper, `BatchedInferencePipeline` and every linked library
    resolve in the runtime base.

  It does not need or touch a GPU. If it fails with a missing `libcuda.so.1`, the
  inference in 2.3 was wrong. Report that; do not paper over it with `LD_LIBRARY_PATH`.
- **No uv at runtime.** The stage has no `COPY --from=uv`, and no instruction in it
  mentions `uv`.
- **Parity with the CPU image.** `ENV`, `WORKDIR`, `USER`, `EXPOSE`, `ENTRYPOINT` and `CMD`
  match the CPU runtime stage. The `EARMARK_*` variables are exactly `EARMARK_HOST` and
  `EARMARK_DATA_DIR`. Do not set `LD_LIBRARY_PATH`: the base's
  `/usr/local/cuda/lib64` must survive, and the rpath plus `ld.so.conf` cover
  `/opt/ctranslate2/lib`. There is no `VOLUME` and no `HEALTHCHECK`.

### 4.2 `compose.yaml`

- **The header comment.** `docker compose up -d` runs the API (CPU image) and the worker
  (GPU image, `Dockerfile.gpu`). The worker needs an NVIDIA GPU, the NVIDIA Container
  Toolkit and Compose 2.30 or later. Point at `docs/deploy.md` section 7.
- **Shared environment.** Keep `x-earmark`, unchanged except that the shared
  `environment` map gets its own anchor (for example `environment: &earmark-env`), so the
  worker can extend it without duplicating it.
- **`api`.** Unchanged: `image: earmark:local`, built from the default `Dockerfile` with
  `EXTRAS`, `UID` and `GID`.
- **`worker`:**

  ```yaml
  worker:
    <<: *earmark
    image: earmark:gpu
    build:
      context: .
      dockerfile: Dockerfile.gpu
      args:
        UID: ${APP_UID:-1000}
        GID: ${APP_GID:-1000}
    command: ["worker"]
    gpus: all
    environment:
      <<: *earmark-env
      # The GPU image exists to run on CUDA; with no GPU visible the worker refuses to start.
      EARMARK_ASR_DEVICE: cuda
      EARMARK_ASR_COMPUTE_TYPE: ${EARMARK_ASR_COMPUTE_TYPE:-float16}
    stop_grace_period: 2m
  ```

  Keep the worker's existing comment about SIGTERM and delivery. The worker has no
  `deploy:` block, no `runtime:`, no `profiles` and no ports.
- **`volumes:`** only `data`.
- **Nowhere in the file:** `asr`, `vllm`, `VLLM_`, `ASR_GPU_MEMORY_UTILIZATION`,
  `asr-models`, `profiles`, or `uv`.

### 4.3 `.github/workflows/container.yml`

- **The header comment.** It builds the CPU image and runs its checks. For
  `Dockerfile.gpu` it runs only `docker build --check`; the GPU image is built and run by
  the operator on the Spark (`docs/deploy.md` section 7).
- **"Validate compose.yaml":**

  ```bash
  cp .env.example .env
  docker compose config --quiet
  check='import json, sys; s = json.load(sys.stdin)["services"]; assert set(s) == {"api", "worker"}, sorted(s); v = {n: s[n]["environment"]["HF_HUB_DISABLE_TELEMETRY"] for n in s}; assert set(v.values()) == {sys.argv[1]}, v; w = s["worker"]["environment"]; assert (w["EARMARK_ASR_DEVICE"], w["EARMARK_ASR_COMPUTE_TYPE"]) == ("cuda", "float16"), w'
  docker compose config --format json | python3 -c "$check" 1
  HF_HUB_DISABLE_TELEMETRY=0 docker compose config --format json | python3 -c "$check" 0
  ```

- **A new step, "Check Dockerfile.gpu (not built here)",** after compose validation and
  before "Build the image": `run: docker build --check -f Dockerfile.gpu .`.
- "Build the image" and "Check the image" are **unchanged**. There are four steps without
  an action, and the checkout. Keep the job timeout and everything else.

### 4.4 Build arguments and how to override them

`docker compose build --build-arg CT2_VERSION=v4.6.0 worker` falls back to the proven
tag. `--build-arg CUDA_ARCH=<n>` targets another GPU. These are not compose variables:
`compose.yaml` does not interpolate them, so the compose-only variable set stays
`{API_BIND, APP_UID, APP_GID, EXTRAS, HF_HUB_DISABLE_TELEMETRY}`.

### 4.5 `.env.example`

In the Transcription block:

```bash
EARMARK_ASR_DEVICE=auto
# Compose's worker forces cuda and uses float16 unless this line is set.
# EARMARK_ASR_COMPUTE_TYPE=auto
# faster-whisper's batched pipeline: chunks decoded together (try 8 or 16 on a GPU); 0 is off.
EARMARK_ASR_BATCH_SIZE=0
```

- `EARMARK_ASR_COMPUTE_TYPE` must stay commented out. If it is set, compose's `float16`
  default never applies (6.2 pins this).
- Delete `# ASR_GPU_MEMORY_UTILIZATION=0.2`.
- Change "in the api, worker, and asr containers" to "in the api and worker containers".

### 4.6 `docs/deploy.md`

Step 27 rewrites this guide. Change only these parts:

- **Section 1, "What is tested."** Replace the `gpu` profile bullet. The new bullet says,
  in these words: "The GPU image is untested: CI only runs `docker build --check` on
  `Dockerfile.gpu`, and it has never been built or run. The Spark smoke test in section 7
  is the only real check."
- **Section 4.**
  - Remove the `ASR_GPU_MEMORY_UTILIZATION` row.
  - Make the `EXTRAS` row say `transcribe`, for the API's CPU image.
  - In the paragraph after the build commands, say that `api` runs the CPU image and
    `worker` the GPU image.
  - Replace "The image is CPU only, so GPU transcription uses the `asr` service (section
    7)" with a sentence pointing at section 7.
- **Section 7**, rewritten as "## 7. Transcription on the DGX Spark" with:
  - **7.1 The GPU image.** What it is, and what the host needs:
    - the NVIDIA driver;
    - the Container Toolkit;
    - Compose 2.30 or later for `gpus: all`;
    - `nvidia-smi --query-gpu=compute_cap --format=csv,noheader` printing `12.1`.

    Also: the compose worker forces `cuda` and defaults to `float16`; batching is
    optional; and the build takes a long time. The CUDA devel base is about 6 GB to
    download, and the build time was not measured.
  - **7.2 Without a GPU.** The CPU image, or the `remote` backend
    ([docs/asr.md](asr.md)) for a server elsewhere. On such a host, `gpus: all` must be
    taken out of the worker. That is one line, and step 27 covers it fully.
  - **7.3 The Spark smoke test**, between `<!-- gpu-smoke:begin -->` and
    `<!-- gpu-smoke:end -->`, as given in section 7.1 of this plan. It must say "the only
    real check of the GPU path".
  - **7.4 If the smoke test fails.** The remedies table from section 7.2 of this plan.
- **Section 17.**
  - "the image, the `api`, `worker`, and `asr` compose services, and the three systemd
    services" becomes "both images, the `api` and `worker` compose services, and the
    systemd services".
  - Delete the "**The `asr` container.**" paragraph.

Do not touch `docs/runbook-spark.md`. Its phase 4 still describes the vLLM service until
step 27 rewrites it; the verifier must not fail this step for that.

### 4.7 `CLAUDE.md`

- **Lines 12–13.** Replace "Step 26 adds GPU transcription inside the image. Its
  contract is in "Planned (step 26)" in `docs/ARCHITECTURE.md`." with "On the Spark the
  worker runs the GPU image (`Dockerfile.gpu`, step 26), which builds CTranslate2 with
  CUDA."
- **Lines 22–23.** Drop "Its "Planned (step 26)" section holds what that step builds
  against."
- **"Rules that are easy to get wrong."** Add one bullet: "**The GPU image's CUDA
  CTranslate2 is fragile** (step 26). In `Dockerfile.gpu` no `uv` command runs after the
  swap, and none at runtime: `uv sync` and `uv run` reinstall the locked CPU wheel. No
  `pip install` anywhere. `get_cuda_device_count() >= 1` does not prove the build; a
  transcription on the Spark does."
- Change nothing else.

### 4.8 `Settings` (`src/earmark/config.py`)

```python
ASR_BATCH_SIZE_MAX: Final = 64

asr_batch_size: int = Field(
    0,
    ge=0,
    le=ASR_BATCH_SIZE_MAX,
    description="Batched faster-whisper decoding: chunks per batch (0 is off).",
)
```

- Place it after `asr_compute_type`.
- An empty value means the default, through `env_ignore_empty`.
- Negative values and values over 64 fail with the usual `EARMARK_ASR_BATCH_SIZE: …`
  message.
- The `remote` and `fake` backends ignore it.
- Add the README row:

  ```
  | `EARMARK_ASR_BATCH_SIZE` | `0` | faster-whisper's batched pipeline: chunks decoded together (8–16 on a GPU, at most 64). `0` is off. Changes the transcript cache key. |
  ```

### 4.9 `src/earmark/transcribe/faster_whisper.py`

- **`WhisperOptions`.** It gains `batch_size: int = 0` as its **last** field, with a
  default, so existing positional constructions stay valid. `from_settings` sets
  `batch_size=settings.asr_batch_size`.
- **`WhisperRuntime`.** It gains a required field after `cuda_device_count`:

  ```python
  supported_compute_types: Callable[[str], frozenset[str]]
  ```

  In `load_runtime()` it wraps
  `frozenset(str(t) for t in ct2.get_supported_compute_types(device))`. It may raise
  whatever CTranslate2 raises (2.5).
- **Batched model.** Add a module-level class:

  ```python
  class BatchedWhisper:
      """A WhisperModelLike over faster-whisper's BatchedInferencePipeline, fixed batch size."""

      def __init__(self, pipeline: object, whisper: object, batch_size: int) -> None: ...
      @property
      def model(self) -> object:  # the CTranslate2 model, read by the load log
          ...
      def transcribe(
          self, audio: object, /, *, language: str | None, beam_size: int, vad_filter: bool
      ) -> tuple[Iterable[SegmentLike], InfoLike]: ...
  ```

  - `transcribe` calls `pipeline.transcribe(audio, language=language,
    beam_size=beam_size, vad_filter=vad_filter, batch_size=self._batch_size)`.
  - `model` returns `whisper.model`, the CTranslate2 model.
  - Type it so mypy strict passes. Prefer small Protocols for the pipeline over `Any`;
    `cast` at the boundary is fine.
- **`load_runtime().load_model(options)`.** It builds `fw.WhisperModel(...)` as today. When
  `options.batch_size > 0`, it returns
  `BatchedWhisper(fw.BatchedInferencePipeline(model=whisper), whisper,
  options.batch_size)`; otherwise it returns the `WhisperModel` unchanged.
- **`FasterWhisperEngine.profile`.**
  - The options dict is exactly today's, and gains `"batch_size": n` only when `n > 0`.
  - The profile for `batch_size=0` must equal, byte for byte, the pre-step-26 string, for
    example
    `faster_whisper {"beam_size":5,"compute_type":"int8","device":"cpu","language":null,"model":"tiny","vad_filter":true}`.
- **`FasterWhisperEngine._load`.** When `batch_size > 0`, the first log line is
  `"loading Whisper model %r (device %s, compute type %s, batch size %d)"`. Otherwise it is
  unchanged. The "loaded … on %s (%s)" line is unchanged, and still reads
  `getattr(model, "model")`, so it works for both model kinds.
- **`check_device` message.** One string. It must contain `EARMARK_ASR_DEVICE`,
  `no CUDA device` and `EARMARK_ASR_BACKEND=remote`:

  ```
  EARMARK_ASR_DEVICE: 'cuda' was requested but CTranslate2 sees no CUDA device. The PyPI
  wheels for linux/aarch64 are CPU-only: run the GPU image (Dockerfile.gpu) with GPU access
  (compose `gpus: all`, or `docker run --gpus all`), or use 'auto' or 'cpu', or
  EARMARK_ASR_BACKEND=remote
  ```

- **The CUDA report:**

  ```python
  FLOAT16: Final = "float16"


  @dataclass(frozen=True, slots=True)
  class CudaReport:
      device_count: int
      compute_types: frozenset[str] | None  # None: no device, or the query raised
      error: str | None  # the exception's type name when the query raised


  def cuda_report(runtime: WhisperRuntime) -> CudaReport: ...
  ```

  - It asks for `supported_compute_types("cuda")` only when `device_count > 0`.
  - An `Exception` from that call becomes `error=type(exc).__name__`. The message is never
    used.
  - It loads no model and makes no network call.

### 4.10 Batched decoding: behaviour

- `batch_size` only reaches faster-whisper through `BatchedWhisper`. The engine's call
  (`language`, `beam_size=5`, `vad_filter=True`) is unchanged for both kinds.
- Decoding, the zero-duration check, `NoSpeechError`, the language `ValueError` mapping,
  progress logging and the heartbeat behave as in sequential mode. The language error is
  raised at the call in both modes (2.7).
- `earmark transcribe` and the worker both get batching from `WhisperOptions.from_settings`.

### 4.11 Doctor (`src/earmark/doctor/checks.py`)

- **Order.** In `run()`, when `asr_backend == "faster_whisper"` and `asr` is `ok`, the
  checks are `asr libraries`, then `asr model`, then the new `asr cuda`.
- **`asr`'s detail.** It gains `, batch size N` only when N > 0. Today's exact string
  stays for 0.
- **`asr cuda`.** It calls `cuda_report(load_runtime())`. With
  `N = device_count` and `devices = f"{N} CUDA device" + ("" if N == 1 else "s")`:

  | Case | Status | Detail |
  |---|---|---|
  | N = 0, device `cpu` | ok | `no CUDA device (EARMARK_ASR_DEVICE=cpu)` |
  | N = 0, device `auto` | ok | `no CUDA device; transcribing on CPU` |
  | N = 0, device `cuda` | fail | `no CUDA device, but EARMARK_ASR_DEVICE=cuda` (defensive: `asr` normally fails first) |
  | N ≥ 1, query raised `E` | fail (warn if device `cpu`) | `{devices}; CTranslate2 could not list the CUDA compute types (E)` |
  | N ≥ 1, `float16` in types | ok | `{devices}, float16 supported`, plus `; EARMARK_ASR_DEVICE=cpu leaves them unused` when the device is `cpu` |
  | N ≥ 1, no `float16` | warn; **fail** if the device is not `cpu` and `ASR_COMPUTE_TYPE` is `float16` | `{devices}, float16 not supported (CUDA compute types: a, b, …)` (sorted, comma-joined), plus `; set EARMARK_ASR_COMPUTE_TYPE to one of them` when failing |

- The check runs inside `guarded` like the others. Offline doctor still loads no model,
  downloads nothing and makes no network call.

## 5. Behaviour and edge cases

- **The swap order is the whole game.** Both `uv sync` runs, then the CTranslate2 build,
  then the swap, then nothing that runs uv.
  - A later `uv sync`, `uv pip install` without `--no-deps`, or `uv run` without
    `--no-sync` silently reinstalls the PyPI CPU wheel (2.5).
  - The runtime stage has no uv binary, so nothing at runtime can do it.
  - The build-time maps check fails if the CPU wheel came back before the copy.
- **Build arguments are per stage.** A stage that uses `CT2_VERSION` or `CUDA_ARCH`
  without re-declaring it sees an empty string. The clone would then fail on
  `--branch ""`, and the check would compare against `""`. Both are loud, which is
  intended.
- **`CT2_VERSION=v4.6.0`.** It builds (2.4), with the common architectures plus PTX JIT.
  The check then expects `4.6.0`, and the venv holds 4.6.0 under a lock that says 4.9.0.
  faster-whisper 1.2.1 requires `ctranslate2>=4.0,<5`, and doctor's `asr libraries` check
  looks only at faster-whisper and av, so nothing complains.
- **The compose worker without a GPU** does not start. `gpus: all` fails at container
  creation, and the forced `cuda` makes `earmark worker` exit with the `check_device`
  `ConfigError`. That is intended: a GPU image silently transcribing `large-v3` on the CPU
  is worse.
- **Doctor under compose belongs in the worker** (`docker compose run --rm worker
  doctor`). The `api` container runs the CPU image with the device from `.env` (`auto`).
- **The cache.** Moving from the CPU image (`device auto`, `compute auto`) to the GPU
  worker (`cuda`, `float16`) changes the profile, so earlier transcripts are not reused.
  That is correct, because they came from another device and compute type. Turning batching
  on, or changing its size, also re-transcribes. Turning it off reuses transcripts made
  before step 26 and since.
- **`compute_type="auto"`** on the GB10 is unverified (2.10). The load log line shows what
  it resolved to.
- **`.env.example` and compose interpolation.** Compose reads the project `.env` both for
  `env_file` and for `${…}`. An operator's `.env` that sets
  `EARMARK_ASR_COMPUTE_TYPE=auto` (copied from an old example) gets `auto` on the worker.
  Step 27's docs cover this; the smoke checks for `float16`.
- **`get_supported_compute_types("cuda")`** raises on the PyPI wheel. It may raise on a
  CUDA build with no visible device too. `cuda_report` never calls it with 0 devices, and
  maps a raise to a type name.
- **Batched mode memory.** About 550 MB of features for 3 h of speech, on top of the
  decoded audio (about 230 MB per hour). That is fine on the Spark's 128 GB, but say so in
  `docs/deploy.md` 7.1.
- **`docker build --check` rules.** Use `ENV KEY=value`, JSON-form `ENTRYPOINT` and `CMD`,
  and `FROM … AS` in matching case. Any violation fails the new CI step. The implementer
  cannot run it locally, so stick to the forms in 4.1.

## 6. Tests

The default suite runs as always, with no network, Docker or GPU. Fixtures are needed
from nowhere: everything is stubs and the files in the repo.

### 6.1 Acceptance criteria mapped to tests

| Criterion (brief) | Tests |
|---|---|
| The CPU image still builds in CI, unchanged in behaviour | **The verifier runs** `git diff --exit-code HEAD -- Dockerfile .dockerignore`. All existing `tests/test_deploy.py` Dockerfile tests stay and pass unchanged in substance, using the shared helpers. `test_container_workflow_checks_image` is unchanged. `test_container_workflow_cpu_build_unchanged` (new) asserts the "Build the image" and "Check the image" steps' `run:` text equals the pre-step text, pinned as literals. |
| Static: the cmake flags from the recipe | `test_gpu_cmake_flags`: the configure line contains each of `-DCMAKE_BUILD_TYPE=Release`, `-DWITH_CUDA=ON`, `-DWITH_CUDNN=ON`, `-DCUDA_DYNAMIC_LOADING=ON`, `-DWITH_MKL=OFF`, `-DWITH_OPENBLAS=ON`, `-DWITH_RUY=ON` and `-DOPENMP_RUNTIME=COMP`. It also has `cmake --build` with `--target install`, and `git clone --depth 1 --branch "${CT2_VERSION}" --recursive https://github.com/OpenNMT/CTranslate2`. |
| Static: `CMAKE_CUDA_ARCHITECTURES` from the build argument | `test_gpu_cuda_arch_from_build_arg`: `-DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCH}"` is present, and no other `CMAKE_CUDA_ARCHITECTURES` value is. There is a global `ARG CUDA_ARCH=121`, and `ARG CUDA_ARCH` is re-declared in `build` before the cmake line. |
| Static: the version default matches `uv.lock` | `test_gpu_ct2_version_matches_lock`: `tomllib` reads `uv.lock`, the `ctranslate2` package's version is found, and the global `ARG CT2_VERSION=v<version>` is checked. `ARG CT2_VERSION` is re-declared in `build` and in `runtime`. |
| Static: the attribution comment | `test_gpu_attribution`: the recipe's two comment lines appear verbatim and adjacent, before the first `FROM`. `test_gpu_licence_note` checks a comment with "NVIDIA" and "do not publish" (case-insensitive). |
| Static: no `pip install` that could pull a CPU `ctranslate2` | `test_gpu_no_pip`: no `pip install`, `pip3 install` or `python -m pip` that is not preceded by `uv `. `test_gpu_uv_pip_installs`: every `uv pip install` either targets `--python /opt/ct2-build/bin/python` with only `-r /tmp/ct2/python/install_requirements.txt`, or is the swap, which targets `--python /app/.venv/bin/python` with `--no-deps --reinstall` and exactly one argument, a local `…/ctranslate2-*.whl`. No requirement string names `ctranslate2`. |
| Build: Python 3.13 headers and pybind11 | `test_gpu_python_matches_cpu_image`: the `uv python install` argument and `UV_PYTHON` both equal the CPU image's Python patch (3.13.18, parsed from `Dockerfile`) and start with `.python-version`, and `UV_PYTHON_PREFERENCE=only-managed` is set. `test_gpu_bindings_build`: `uv build --wheel --no-build-isolation` with `CTRANSLATE2_ROOT=/opt/ctranslate2`, the build venv, and its install from the tag's `install_requirements.txt`. |
| Build: the swap into `/app/.venv` | `test_gpu_venv_from_lock`: two `uv sync` calls, both with `--locked --no-dev --extra transcribe`, the last with `--no-editable`, and `UV_PROJECT_ENVIRONMENT=/app/.venv`. `test_gpu_swap_after_sync`: the swap comes after the last `uv sync`. |
| Build-time check | `test_gpu_build_time_check`: the last stage has a `RUN` with `/app/.venv/bin/python -c`, after all three `COPY --from=build` lines, containing `import sys, ctranslate2, faster_whisper`, `BatchedInferencePipeline`, `__version__`, `/app/.venv/`, `/proc/self/maps` and `/opt/ctranslate2/lib/`, and passing `"$CT2_VERSION"`. |
| Nothing at runtime runs `uv sync` or `uv run` without `--no-sync` | `test_gpu_no_uv_after_swap`: no logical line after the swap's `RUN` mentions `uv` as a word, and the swap `RUN` has no `uv ` after `--reinstall`. `test_gpu_runtime_has_no_uv`: the runtime stage has no `COPY --from=uv` and no word `uv`. `test_deploy_runtime_never_syncs` (in `test_deploy.py`): across both Dockerfiles' `ENTRYPOINT`/`CMD`, compose `command`/`entrypoint`/`healthcheck`, and systemd `ExecStart`, no `uv sync`, and no `uv run` without `--no-sync`. |
| Static: base images | `test_gpu_images_pinned`: every external image is pinned by digest, and the uv image line equals the CPU `Dockerfile`'s. `test_gpu_cuda_bases`: exactly two `nvidia/cuda` images, for stages `build` and `runtime`. Their tags share one version (≥ 12.9, parsed as numbers), one is `-cudnn-devel-ubuntu24.04`, the other `-cudnn-runtime-ubuntu24.04`. Stages are `["uv", "build", "runtime"]`. |
| Runtime parity | Parametrize the existing tests over both Dockerfiles: runtime user not root (with the UID/GID guards), entrypoint and command, no `VOLUME` or `HEALTHCHECK`, `ca-certificates`, copies only from the allow-list, environment (runtime `EARMARK_*` = HOST, DATA_DIR), runtime dirs, HF telemetry. `test_gpu_runtime_libs`: `libopenblas0-pthread`, `libgomp1`, the three `COPY --from=build` sources, the `ld.so.conf.d` line, no `LD_LIBRARY_PATH=`, and `userdel --remove ubuntu` guarded by `id -u ubuntu`. `test_gpu_no_own_dockerignore`: `Dockerfile.gpu.dockerignore` does not exist. |
| Should CI build the GPU image? (decided: no) | `test_container_workflow_checks_gpu_dockerfile`: a step runs exactly `docker build --check -f Dockerfile.gpu .`, and no `docker build` line names `Dockerfile.gpu` without `--check`. |
| Spark smoke documented | `test_deploy_doc_gpu_smoke` (`tests/test_docs.py`), on `between_markers(deploy, "gpu-smoke")`. It contains `docker compose build worker`, `get_cuda_device_count`, `ctranslate2.__file__`, `command -v uv`, `worker doctor`, `transcribe`, `float16`, the clip's sha256 `63a4b1e4…9715`, `CT2_VERSION=v4.6.0`, and "the only real check" (case-insensitive). |
| The worker runs `faster_whisper` with `cuda` | `test_compose_worker_gpu` (6.2). `test_worker_refuses_cuda_without_device`: `factory.build_backends` with `asr_device="cuda"`, `fw.load_runtime` monkeypatched to a fake runtime with 0 devices, raises `ConfigError` containing `no CUDA device` and `Dockerfile.gpu`. |
| Batched pipeline option, 0 or unset is off | `test_batch_size_setting`: default 0, empty means 0, `8` parses, `-1` and `65` fail naming `EARMARK_ASR_BATCH_SIZE`. `test_options_from_settings` is extended with `EARMARK_ASR_BATCH_SIZE=8`. `test_sequential_when_batch_zero` (real `load_runtime` with `StubExtra`): no pipeline is constructed, and `WhisperModel.transcribe` gets no `batch_size`. `test_batched_pipeline_wiring` (`StubExtra`, batch 8): the pipeline is built with the `WhisperModel`, and `transcribe` gets `batch_size=8`, `beam_size=5`, `vad_filter=True` and the language. `test_batched_load_log`: the loading line contains `batch size 8`, and the "loaded … on cuda (float16)" line reads the inner model through `BatchedWhisper.model`. |
| The option feeds the cache key | `test_profile_includes_model_and_options`, extended: `profile(batch_size=8) != base`, `profile(batch_size=16) != profile(batch_size=8)`, `profile(batch_size=0) == base`. `test_profile_unchanged_when_batching_off` pins the exact pre-step-26 string for `_options()`. `test_cached_transcript_kept_when_batching_off`: a stored transcript stamped with that literal profile is a cache hit. `test_cached_transcript_miss_when_batch_changes`: the same transcript misses with `batch_size=8`, and is re-transcribed. |
| Compose: `asr` and the `gpu` profile removed, GPU image and `gpus: all` | 6.2 |
| Settings in `Settings`, `.env.example`, README | Existing: `test_env_example_lists_every_field`, `test_env_example_values_are_defaults` and `test_readme_settings_reference_covers_every_field` fail until the row and line exist. |
| Doctor: device count and `float16` | `test_cuda_report_no_device_skips_query`, `test_cuda_report_types`, `test_cuda_report_query_error_is_type_name`. `test_doctor_asr_cuda` (parametrized over every row of the 4.11 table, through `WhisperStubs`): exact status and detail. `test_doctor_asr_cuda_order`: `asr`, `asr libraries`, `asr model`, `asr cuda` are adjacent in that order. `test_doctor_asr_detail_batch`: `, batch size 8` is shown. `test_doctor_whisper_ok_with_stub_runtime`, with its exact string for 0, is unchanged. |
| With `cuda` and no device the worker refuses to start | Existing `test_cuda_without_device_is_config_error`, extended to assert `Dockerfile.gpu` and `gpus: all` in the message. Existing `test_doctor_cuda_unavailable_fails` is unchanged. `test_worker_refuses_cuda_without_device` above. |

### 6.2 Compose tests (`tests/test_deploy.py`): every test that pins the old service

Update these in place:

1. **`test_compose_services`.** The services are `{"api", "worker"}`. `api` is
   `earmark:local`, with a build of context `.`, no `dockerfile` key, and args `EXTRAS`,
   `UID`, `GID`. `worker` is `earmark:gpu`, with `dockerfile: Dockerfile.gpu` and args
   exactly `UID` and `GID`. Both use the same `UID`/`GID` expressions. `Dockerfile.gpu`
   exists.
2. **`test_compose_worker_and_asr_publish_nothing`** becomes
   `test_compose_worker_publishes_nothing`. It keeps the `0.0.0.0:` check.
3. **`test_compose_shared_data`.** `COMPOSE["volumes"]` is `{"data"}`.
4. **`test_compose_builds_transcribe_extra`.** `api` keeps `EXTRAS`, and `worker` has no
   `EXTRAS`, because `Dockerfile.gpu` always installs `transcribe`.
5. **`test_compose_forces_container_paths`.** `api`'s set is unchanged. `worker`'s is that
   set plus `EARMARK_ASR_DEVICE == "cuda"` and
   `EARMARK_ASR_COMPUTE_TYPE == "${EARMARK_ASR_COMPUTE_TYPE:-float16}"`.
6. **`test_compose_gpu_profile`** is replaced by `test_compose_worker_gpu`.
   - `worker["gpus"] == "all"`.
   - No service has `profiles`, `deploy` or `runtime`.
   - `"asr" not in SERVICES`.
   - The text contains no `vllm` or `VLLM_` (case-insensitive) and no `asr-models`.
7. **`test_compose_only_variables_documented`.** The set loses
   `ASR_GPU_MEMORY_UTILIZATION`.
8. **`test_compose_disables_hf_telemetry`.** Parametrize over `["api", "worker"]`.

Extend these:

9. **`test_proxy_token_stays_out_of_earmark`** and
   **`test_deploy_files_leave_xet_logs_alone`** also scan `Dockerfile.gpu`.

New tests:

10. **`test_env_example_leaves_compute_type_to_compose`.** `.env.example` has no
    uncommented `EARMARK_ASR_COMPUTE_TYPE=` line, and no `ASR_GPU_MEMORY_UTILIZATION`.
11. **`test_deploy_runtime_never_syncs`**, from the table in 6.1.

### 6.3 Workflow tests (`tests/test_ci.py`)

1. **`test_container_workflow_job`.** The step actions become `[CHECKOUT, "", "", "", ""]`.
2. **`test_container_workflow_validates_compose`.** `--profile` does not appear in the
   runs. `docker compose config` and `cp .env.example .env` do. The check names
   `EARMARK_ASR_COMPUTE_TYPE` and `float16`.
3. **`test_container_workflow_checks_hf_telemetry`.** Unchanged; it must still pass.
4. **New: `test_container_workflow_checks_gpu_dockerfile`** and
   **`test_container_workflow_cpu_build_unchanged`**, from the table in 6.1.
5. **`test_container_workflow_builds_without_push`.** Unchanged. `docker build --check`
   contains none of its forbidden words, `buildx` included.

### 6.4 Doc tests (`tests/test_docs.py`)

1. **`test_deploy_doc_states_test_status`.** Replace `"gpu profile is untested"` with
   `"gpu image is untested"` and `"only real check"`.
2. **New: `test_deploy_doc_gpu_smoke`**, from the table in 6.1.
3. **New: `test_deploy_doc_has_no_gpu_profile`.** `docs/deploy.md` contains no
   `--profile gpu`, `COMPOSE_PROFILES` or `ASR_GPU_MEMORY_UTILIZATION`.

### 6.5 Fakes (`tests/whisper_fakes.py`)

- **`FakeRuntime`.**
  - New keyword arguments: `cuda_types: frozenset[str] = frozenset()` and
    `types_error: BaseException | None = None`.
  - `runtime()` passes `supported_compute_types`, which raises `types_error` when it is
    set and records the device it was asked about.
- **`StubExtra`.**
  - New keyword arguments `cuda_types` and `types_error`, so the stub `ctranslate2`
    module has `get_supported_compute_types`.
  - The stub `faster_whisper` gets a `BatchedInferencePipeline` class. It records the
    `model` it was built with and each `transcribe` call's kwargs, and delegates to
    `FakeWhisperModel`.
  - The `WhisperModel` stub's return value gains a `model` attribute with `device` and
    `compute_type`, for the load-log test.
- **`tests/test_doctor.py`'s `WhisperStubs`.** It gains `cuda_types` and `types_error`.
  Existing tests keep passing with the defaults: 0 devices, so the query is never made.

### 6.6 What no test can prove

That the image builds, that CUDA works, that `sm_121` kernels run, and how fast batching
is. These are the operator's smoke test, below.

## 7. Verification

The verifier runs:

```bash
make check
GITHUB_ACTIONS=true make check
uv run pytest tests/test_deploy.py tests/test_deploy_gpu.py tests/test_ci.py tests/test_docs.py \
  tests/test_faster_whisper.py tests/test_doctor.py tests/test_config.py -q
git diff --exit-code HEAD -- Dockerfile .dockerignore          # the CPU image is untouched
git status --porcelain docs/PLAN.md                            # the status table is not this step's to change
grep -rniE 'profile gpu|asr-models|ASR_GPU_MEMORY_UTILIZATION|vllm' \
  compose.yaml .env.example .github src Dockerfile Dockerfile.gpu    # expect no output
grep -n 'Planned (step 26)' CLAUDE.md docs/ARCHITECTURE.md          # expect no output
```

Then a fake-backend live smoke, as in `docs/ARCHITECTURE.md` "Testing". With every
backend `fake`, `EARMARK_ALLOWED_RECIPIENTS` set and `EARMARK_ASR_BATCH_SIZE=8`:

1. `uv run earmark config | grep ASR_BATCH_SIZE` shows `8`.
2. `serve`, a `POST /transcriptions`, then `worker --once`. The job is `done`, which shows
   the setting breaks nothing on other backends.
3. `uv run earmark doctor` with `EARMARK_ASR_BATCH_SIZE=99` exits 2, naming the setting.

The verifier also reads `Dockerfile.gpu` against section 4.1 line by line, since nothing
can execute it. It reads `container.yml`'s new step and compose checks against 4.3; the
container workflow on the pushed commit is the first execution of `docker compose config`
with `gpus` and of `docker build --check`. `/step` waits for that run.

### 7.1 The operator's Spark smoke test (the only real check)

This is the content of `docs/deploy.md` 7.3. Run it in the checkout on the Spark, with a
`.env` from `.env.example`:

```bash
# 0. The host
nvidia-smi --query-gpu=name,compute_cap,driver_version --format=csv,noheader  # NVIDIA GB10, 12.1, …
docker compose version                                                      # 2.30 or later

# 1. Build. The log shows "-- CUDA architectures: 121". It takes a long time (unmeasured).
docker compose build worker

# 2. The CUDA build is the one in the venv, and the image has no uv that could undo it
docker compose run --rm --no-deps --entrypoint python worker -c \
  "import ctranslate2 as c; print(c.__version__, c.__file__); print(c.get_cuda_device_count(), sorted(c.get_supported_compute_types('cuda')))"
#    expect: 4.9.0 /app/.venv/lib/python3.13/site-packages/ctranslate2/__init__.py
#            1 [..., 'float16', ...]
docker compose run --rm --no-deps --entrypoint sh worker -c 'command -v uv || echo "no uv"'
#    expect: no uv

# 3. Doctor
docker compose run --rm worker doctor
#    expect: asr ok "faster_whisper, model large-v3, device cuda, compute float16"
#            asr cuda ok "1 CUDA device, float16 supported"

# 4. A short public clip on cuda (JFK, 1961, public domain; the slow tests' clip)
curl -fsSLo jfk.flac https://raw.githubusercontent.com/SYSTRAN/faster-whisper/65882eee9f5cdbeeb2d877f1131d48cf241b327d/tests/data/jfk.flac
echo "63a4b1e4c1dc655ac70961ffbf518acd249df237e5a0152faae9a4a836949715  jfk.flac" | sha256sum -c
docker compose run --rm --no-deps -v "$PWD/jfk.flac:/tmp/jfk.flac:ro" worker transcribe /tmp/jfk.flac
#    expect on stderr: loaded Whisper model 'large-v3' in … s on cuda (float16)
#    then the text: "And so my fellow Americans, ask not what your country can do for you …"
#    optional: add -e EARMARK_ASR_BATCH_SIZE=8 and compare the "x real time" figure
```

Step 2's device count proves only that CUDA and the device are visible. Step 4 proves the
build, because a wrong architecture fails there (2.6). The operator records the result,
the build time and the image size (`docker image ls earmark:gpu`). Step 27's runbook
carries them on.

### 7.2 If the smoke test fails (`docs/deploy.md` 7.4)

| Symptom | Remedy |
|---|---|
| The CTranslate2 compile or the bindings build fails, or the build-time check fails | `docker compose build --build-arg CT2_VERSION=v4.6.0 worker`, then step 2 again, expecting `4.6.0`. 4.6.0 builds the common architectures and the GB10 JIT-compiles the PTX on the first load, which is slow. `CUDA_CACHE_PATH=/data/cuda-cache` in `.env` keeps the JIT cache on the volume. Report the error: 4.9.0 is the first native-CMake release. |
| `get_cuda_device_count()` prints 0, or the worker exits with "sees no CUDA device" | A host problem. Run `docker compose run --rm --no-deps --entrypoint nvidia-smi worker`. Check the NVIDIA Container Toolkit, and "nvml error: driver not loaded" after a kernel update (the recipe's host gotcha). |
| "no kernel image is available for execution on the device" at load or transcription | Wrong architecture. Check the build log's `CUDA architectures` line and the `CUDA_ARCH` argument, or fall back to `v4.6.0`. |
| `float16` missing from the list, or doctor's `asr cuda` fails | Report it. Set `EARMARK_ASR_COMPUTE_TYPE` to a listed type meanwhile. |
| "Error 804: forward compatibility was attempted on non supported HW" | The base's `cuda-compat-12-9` is being used. Add `apt-get purge -y cuda-compat-12-9` to the runtime stage, as upstream CTranslate2's Dockerfile does, and report it. |
| Compose rejects `gpus` | Compose older than 2.30. Upgrade the Docker Compose plugin. |

## 8. Deviations

- **D1: The base is the slim `nvidia/cuda` pair, not NGC.**
  - The brief left this choice open.
  - The pair has the same CUDA 12.9.1 and Ubuntu 24.04 as earshot's proven NGC 25.06
    (2.2), and it is upstream CTranslate2's own pattern.
  - earmark needs no torch, and NGC's Python 3.12 would need a uv-managed 3.13 anyway.
  - The runtime base is 3.09 GB compressed, against an NGC image over 10 GB.
  - Neither base makes 4.9.0's untested aarch64 CUDA build safer.
- **D2: CTranslate2 installs under `/opt/ctranslate2`**, where the recipe has `/usr/local`
  with `LD_LIBRARY_PATH`. A directory copy keeps the `.so` symlinks intact, the library is
  isolated from the base's `/usr/local`, and the rpath from `CTRANSLATE2_ROOT` plus one
  `ld.so.conf.d` line replaces `LD_LIBRARY_PATH`. Upstream's Dockerfile uses
  `/opt/ctranslate2` too.
- **D3: The bindings are built with `uv build --no-build-isolation`** in a throwaway uv
  venv. The recipe uses `pip install`, and the brief forbids pip pulling the CPU wheel.
  The two tags differ (2.3, 2.4), and this form works for both (2.5).
- **D4: In compose, `api` stays on the CPU image.** The brief says "replace them with the
  GPU image" without naming the API. `earmark serve` never touches CTranslate2, the CPU
  image stays exercised on the Spark, and the API restarts small and fast.
- **D5: The compose worker forces `EARMARK_ASR_DEVICE=cuda`, and defaults
  `EARMARK_ASR_COMPUTE_TYPE` to `float16` through interpolation.** For that,
  `.env.example` comments out its compute-type line. `float16` is what the smoke test and
  doctor look for, and `auto` on the GB10 is unverified. The device is forced because the
  service exists for the GPU.
- **D6: `batch_size` joins the profile only when it is above 0.** Transcripts cached
  before step 26, with batching off, stay valid. The brief says only that the option
  "feeds the cache key", and it does.
- **D7: The doctor check is a new line, `asr cuda`**, not extra text in `asr`. It gets its
  own status, and `asr`'s pinned detail stays stable.
- **D8: CI gets a new step, `docker build --check -f Dockerfile.gpu .`.** The GPU image is
  not built (2.9).
- **D9: `docs/deploy.md` sections 1, 4, 7 and 17 are changed now**, although step 27
  rewrites the docs. The brief requires a documented smoke test and says this step
  "touches compose and the deploy docs", and the old sections describe a service this step
  deletes. The runbook waits for step 27.
- **D10: `CLAUDE.md` gets two pointer fixes and one rule (4.7).** Its references to
  "Planned (step 26)" would otherwise dangle.
- **D11: `docs/ARCHITECTURE.md` and the recipe record changed with this plan.**
  - **ARCHITECTURE.md.** "Planned (step 26)" is gone. Its content moved into "Decisions",
    "Module layout", "Transcribing", "Doctor", "Configuration", "Deployment", "Testing",
    "Continuous integration" and "Known risks" 1 and 4. The settled lists became "Settled
    questions", with step 26's entries, and "Not planned" is a top-level section.
  - **The recipe record** has a dated note appended: the device count does not prove the
    architecture, v4.6.0 ignores `CMAKE_CUDA_ARCHITECTURES`, and NGC 25.06's CUDA version.
    The recipe text itself is unchanged.
- **No dependency is added.** pybind11 and setuptools exist only inside the image's
  throwaway build venv, at the versions the CTranslate2 tag pins.

## 9. Out of scope

- The README, runbook, Shortcut and deploy-guide rewrites. That is step 27, including the
  runbook's phase 4, which still describes vLLM.
- Publishing or pushing any image, under NVIDIA's licence terms.
- A native, systemd GPU install outside the container.
- CUDA 13 bases. CTranslate2's CUDA 13 support (#1933) is open, and CCCL 2.7 targets
  CUDA 12.
- `WITH_CUDNN=OFF` to drop cuDNN for a slimmer runtime. That would depart from the recipe.
- Pinning the CTranslate2 checkout by commit instead of tag.
- A persistent CUDA JIT cache by default.
- Diarization.
- A `gpu`-marked pytest. The image ships no tests, and the smoke test uses the CLI.
- Changing `docs/PLAN.md`'s status table.
