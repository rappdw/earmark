# GPU build recipe: CTranslate2 with CUDA for the GB10 (2026-11-23)

Input for step 26 (GPU transcription inside the image). The earshot agent supplied this
recipe on 2026-11-23 from its `docker/Dockerfile` (earshot v0.4.0+). earshot's image runs
faster-whisper on the DGX Spark's GB10 GPU with it.

Attribution, to keep with any text copied into earmark's Dockerfile:

```
# CUDA CTranslate2 build adapted from earshot (https://github.com/rappdw/earshot,
# docker/Dockerfile), MIT License, Copyright (c) 2026 Dan Rapp.
```

CTranslate2 is MIT (OpenNMT). The NGC base images are under NVIDIA's container licence:
pulling them is fine, but do not publish a built image without checking those terms.

## Facts (from earshot's working file)

- **Base:** `nvcr.io/nvidia/pytorch:25.06-py3`, a single stage. It ships CUDA 12.9,
  cuDNN 9 and cuBLAS 12. earshot needs the full NGC image for pyannote's CUDA torch.
  earmark does not.
- **CTranslate2:** tag `v4.6.0`, stock. There are no source patches, PTX tricks or forks
  for `sm_121`.
- **Build dependencies:** `build-essential cmake git libopenblas-dev`.
- **Build:**

  ```dockerfile
  ARG CT2_VERSION=v4.6.0
  ARG CUDA_ARCH=121
  ENV LD_LIBRARY_PATH=/usr/local/lib:${LD_LIBRARY_PATH:-}
  RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git libopenblas-dev && rm -rf /var/lib/apt/lists/* && \
      git clone --depth 1 --branch "${CT2_VERSION}" --recursive \
        https://github.com/OpenNMT/CTranslate2 /tmp/ct2 && \
      cmake -S /tmp/ct2 -B /tmp/ct2/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DWITH_CUDA=ON -DWITH_CUDNN=ON -DCUDA_DYNAMIC_LOADING=ON \
        -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCH}" \
        -DWITH_MKL=OFF -DWITH_OPENBLAS=ON -DWITH_RUY=ON -DOPENMP_RUNTIME=COMP && \
      cmake --build /tmp/ct2/build -j"$(nproc)" --target install && \
      ldconfig && \
      CTRANSLATE2_ROOT=/usr/local pip install --no-cache-dir /tmp/ct2/python && \
      rm -rf /tmp/ct2
  ```

  `WITH_MKL=OFF` because MKL is x86-only. OpenBLAS plus Ruy is the CPU backend, and
  `OPENMP_RUNTIME=COMP` is GNU libgomp.
- **Runtime libraries:** with `CUDA_DYNAMIC_LOADING=ON`, the runtime must provide
  libcublas and libcudnn for `dlopen`, and also libopenblas and libgomp.
  `libctranslate2.so` installs into `/usr/local/lib`, hence `LD_LIBRARY_PATH`.
- **Keeping the CUDA build:** earshot writes the installed versions into a pip
  constraints file, `ctranslate2==<built version>`, before installing faster-whisper. pip
  then sees the requirement as satisfied and does not fetch the CPU wheel from PyPI.
- **faster-whisper:** earshot pins `>=1.1.0,<2`. 1.1 is the first release with
  `BatchedInferencePipeline` and word timestamps. The `huggingface_hub<1.0` cap is only
  for pyannote.
- **Arch detection on the host:**
  `nvidia-smi --query-gpu=compute_cap --format=csv,noheader | tr -d '. '` prints `121`
  on the GB10.
- **Running:** `--gpus all`, which needs the NVIDIA Container Toolkit.
- **Smoke checks:**
  - At build time, with no GPU: `import ctranslate2, faster_whisper` and
    `from faster_whisper import BatchedInferencePipeline`.
  - On the Spark: `python -c "import ctranslate2; print(ctranslate2.get_cuda_device_count())"`
    must print at least 1. It prints 0 if CTranslate2 was built without CUDA or for the
    wrong architecture.
  - `ctranslate2.get_supported_compute_types("cuda")` contains `float16`.
  - `WhisperModel(device="cuda", compute_type="float16")` constructs. CTranslate2 raises
    on construction if CUDA or float16 is unavailable, so a successful model load is the
    real proof.
- **Host gotcha:** a DGX OS kernel update once left the host without a matching NVIDIA
  kernel module ("nvml error: driver not loaded"). That is a host problem, not an image
  problem.

## Unknowns (never measured by earshot)

- The exact cuDNN and cuBLAS versions in the base. Read them inside the image:

  ```
  nvcc --version | tail -1
  python -c "import torch; print(torch.backends.cudnn.version())"
  ls /usr/local/cuda/lib64/libcublas.so.*
  ```

- Build time, image size and disk use. The CTranslate2 build takes "tens of minutes"
  on the Spark's 20 Arm cores. The NGC base alone is well over 10 GB compressed. A free
  GitHub runner has about 14 GB of free disk, so building the NGC-based image in CI is
  unlikely to fit.
- A slim split, with an `nvidia/cuda` devel image for the build and a cuDNN runtime image
  to run, should work for faster-whisper alone, but nobody has tried it.

## Pitfalls earshot flagged for earmark's build (2026-11-23, not hit by earshot itself)

- **Python 3.13 headers.** Building the CTranslate2 bindings against the venv's 3.13
  interpreter needs the 3.13 development headers (`Python.h`) and pybind11 in the build
  stage. The NGC base likely ships Python 3.12, so check its version first. A uv-managed
  3.13 includes the headers.
- **uv putting the PyPI wheel back.** `uv sync` and `uv run` reconcile the venv to the
  lockfile, so either can replace the swapped-in CUDA bindings with the locked PyPI
  `ctranslate2` wheel. Avoid this with `uv run --no-sync`, with no uv commands at
  runtime (the image already runs the venv's `earmark` directly), or by installing the
  source build from a local path as the locked version. Check
  `ctranslate2.__file__` and `get_cuda_device_count()` after a uv command, not only
  after the swap.

## Differences from earmark, for step 26 to settle

- **Python and the lock.** earmark locks `ctranslate2==4.9.0` and `faster-whisper==1.2.1`
  in `uv.lock`, and installs them with uv into `/app/.venv` on Python 3.13. The NGC
  image's own Python is not 3.13. So the GPU image builds CTranslate2 against the
  venv's Python and swaps the built bindings into the venv
  (`uv pip install --no-deps --reinstall`). It does not use pip constraints.
- **Version.** The locked 4.9.0 has not been built for `sm_121` by anyone yet. v4.6.0 is
  the version proven on the GB10. Step 26 builds the locked version by default, with a
  `CT2_VERSION` build argument. Falling back to `v4.6.0` is the documented remedy if the
  build or the smoke test fails on the Spark.
- **The `transcribe` extra and diarization.** earmark does not need pyannote, torch or
  torchaudio. Speaker diarization stays in the backlog.

## Notes from step 26's planning (2026-11-23)

Checked against upstream sources while planning step 26. See
`docs/steps/26-gpu-image.md` section 2 for the evidence.

- **`get_cuda_device_count()` does not detect a wrong architecture.** It is CUDA's
  `cudaGetDeviceCount` (CTranslate2 `src/cuda/utils.cc`). It prints 0 for a build without
  CUDA or with no device visible. A build without kernels for the GPU still counts the
  device, and fails at the first kernel launch. A real transcription is the proof.
- **v4.6.0 ignores `CMAKE_CUDA_ARCHITECTURES`.** Its CMake uses the old `FindCUDA` module
  with `CUDA_ARCH_LIST`, which defaults to `Auto`. `docker build` has no GPU, so `Auto`
  falls back to CMake's common list, up to `8.6+PTX` in CMake 3.28. So earshot's GB10 run
  most likely JIT-compiled the `compute_86` PTX. That is inferred from the sources, not
  measured. v4.9.0 replaced `FindCUDA` with CMake's native CUDA support (#2067), so there
  `-DCMAKE_CUDA_ARCHITECTURES=121` does build `sm_121` code.
- **The NGC 25.06 base** is Ubuntu 24.04 with CUDA 12.9.1, according to NVIDIA's release
  notes. `nvidia/cuda:12.9.1-cudnn-*-ubuntu24.04` has the same CUDA and OS.
