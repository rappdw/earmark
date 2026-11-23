"""Static checks of Dockerfile.gpu. Nothing here builds it: there is no Docker or GPU here.

Only the operator's smoke test on the Spark (docs/deploy.md section 7) shows that the image
builds and transcribes. These tests pin the text as tightly as text allows.
"""

import re
import tomllib
from pathlib import Path

from tests.doc_helpers import ROOT
from tests.dockerfile_helpers import (
    PINNED_IMAGE,
    external_froms,
    froms,
    instructions,
    logical_lines,
    stage_lines,
)

TEXT = (ROOT / "Dockerfile.gpu").read_text()
CPU_TEXT = (ROOT / "Dockerfile").read_text()
LINES = logical_lines(TEXT)
RUNS = instructions(TEXT, "RUN")
BUILD = stage_lines(TEXT, "build")
RUNTIME = stage_lines(TEXT, "runtime")
CTRANSLATE2_ROOT = "/opt/ctranslate2"


def _first_from_index() -> int:
    return next(i for i, line in enumerate(TEXT.splitlines()) if line.startswith("FROM "))


def _configure() -> str:
    [line] = [r for r in RUNS if "cmake -S" in r]
    return line


def _swap() -> str:
    [line] = [r for r in RUNS if "--reinstall" in r]
    return line


def _swap_run_index() -> int:
    return LINES.index(_swap())


def _uv_words(line: str) -> bool:
    return re.search(r"\buv\b", line) is not None


def test_gpu_cmake_flags() -> None:
    configure = _configure()
    for flag in (
        "-DCMAKE_BUILD_TYPE=Release",
        "-DWITH_CUDA=ON",
        "-DWITH_CUDNN=ON",
        "-DCUDA_DYNAMIC_LOADING=ON",
        "-DWITH_MKL=OFF",
        "-DWITH_OPENBLAS=ON",
        "-DWITH_RUY=ON",
        "-DOPENMP_RUNTIME=COMP",
        f"-DCMAKE_INSTALL_PREFIX={CTRANSLATE2_ROOT}",
    ):
        assert flag in configure.split(), flag
    assert "cmake --build" in configure
    assert "--target install" in configure
    assert (
        'git clone --depth 1 --branch "${CT2_VERSION}" --recursive '
        "https://github.com/OpenNMT/CTranslate2"
    ) in configure


def test_gpu_cuda_arch_from_build_arg() -> None:
    configure = _configure()
    assert '-DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCH}"' in configure
    assert re.findall(r"CMAKE_CUDA_ARCHITECTURES\S*", TEXT) == [
        'CMAKE_CUDA_ARCHITECTURES="${CUDA_ARCH}"'
    ]
    assert "ARG CUDA_ARCH=121" in LINES[: LINES.index(BUILD[0])]
    assert BUILD.index("ARG CUDA_ARCH") < BUILD.index(configure)


def _locked_ctranslate2() -> str:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    [version] = [p["version"] for p in lock["package"] if p["name"] == "ctranslate2"]
    return str(version)


def test_gpu_ct2_version_matches_lock() -> None:
    assert f"ARG CT2_VERSION=v{_locked_ctranslate2()}" in LINES[: LINES.index(BUILD[0])]
    assert "ARG CT2_VERSION" in BUILD
    assert "ARG CT2_VERSION" in RUNTIME
    assert BUILD.index("ARG CT2_VERSION") < BUILD.index(_configure())


def test_gpu_attribution() -> None:
    lines = TEXT.splitlines()
    head = lines[: _first_from_index()]
    first = "# CUDA CTranslate2 build adapted from earshot (https://github.com/rappdw/earshot,"
    second = "# docker/Dockerfile), MIT License, Copyright (c) 2026 Dan Rapp."
    assert head.index(first) + 1 == head.index(second)


def test_gpu_licence_note() -> None:
    comments = " ".join(
        line.lstrip("# ")
        for line in TEXT.splitlines()[: _first_from_index()]
        if line.startswith("#")
    ).lower()
    assert "nvidia" in comments
    assert "do not publish" in comments


def test_gpu_no_pip() -> None:
    for line in LINES:
        for match in re.finditer(r"(?:pip3?|-m pip)\s+install", line):
            assert line[: match.start()].rstrip().endswith("uv"), line


def test_gpu_uv_pip_installs() -> None:
    installs = [r for r in RUNS if "uv pip install" in r]
    assert len(installs) == 1  # one RUN holds both, joined with &&
    parts = [p.strip() for p in installs[0].split("&&") if "uv pip install" in p]
    build_venv = (
        "uv pip install --python /opt/ct2-build/bin/python "
        "-r /tmp/ct2/python/install_requirements.txt"
    )
    swap = (
        "uv pip install --python /app/.venv/bin/python --no-deps --reinstall "
        "/tmp/ct2-wheel/ctranslate2-*.whl"
    )
    assert parts == [build_venv, swap]
    for part in parts:
        assert not re.search(r"\bctranslate2[=<>~ ]", part.replace("ctranslate2-*.whl", ""))


def test_gpu_python_matches_cpu_image() -> None:
    [cpu_python] = {
        i.split(":", 1)[1].split("-", 1)[0] for i, _ in froms(CPU_TEXT) if i.startswith("python:")
    }
    assert cpu_python == "3.13.18"
    project = (ROOT / ".python-version").read_text().strip()
    assert cpu_python.startswith(project + ".")
    env = " ".join(instructions(TEXT, "ENV"))
    assert f"UV_PYTHON={cpu_python}" in env.split()
    assert f"RUN uv python install {cpu_python}" in LINES
    assert "UV_PYTHON_PREFERENCE=only-managed" in env.split()
    assert "UV_PYTHON_DOWNLOADS=manual" in env.split()


def test_gpu_bindings_build() -> None:
    swap_run = _swap()
    assert f"CTRANSLATE2_ROOT={CTRANSLATE2_ROOT} uv build --wheel --no-build-isolation" in swap_run
    assert "--python /opt/ct2-build/bin/python" in swap_run
    assert "uv venv /opt/ct2-build" in swap_run
    assert swap_run.index("uv venv /opt/ct2-build") < swap_run.index(
        "-r /tmp/ct2/python/install_requirements.txt"
    )
    assert swap_run.index("uv build") < swap_run.index("--reinstall")


def test_gpu_venv_from_lock() -> None:
    syncs = [r for r in RUNS if "uv sync" in r]
    assert len(syncs) == 2
    for sync in syncs:
        for flag in ("--locked", "--no-dev", "--extra transcribe"):
            assert flag in sync, (flag, sync)
    assert "--no-editable" in syncs[-1]
    assert "UV_PROJECT_ENVIRONMENT=/app/.venv" in " ".join(instructions(TEXT, "ENV")).split()
    assert "EXTRAS" not in TEXT


def test_gpu_swap_after_sync() -> None:
    syncs = [i for i, line in enumerate(LINES) if "uv sync" in line]
    assert syncs
    assert _swap_run_index() > max(syncs)


def test_gpu_swap_is_last_uv_command() -> None:
    swap = _swap()
    assert "uv" not in re.split(r"--reinstall\s+\S+", swap, maxsplit=1)[1].split()
    for line in LINES[_swap_run_index() + 1 :]:
        assert not _uv_words(line), line


def test_gpu_no_uv_after_swap() -> None:
    after = LINES[_swap_run_index() + 1 :]
    assert after
    for line in after:
        assert not _uv_words(line), line


def test_gpu_build_time_check() -> None:
    copies = [i for i, line in enumerate(RUNTIME) if line.startswith("COPY --from=build")]
    assert len(copies) == 3
    [check] = [line for line in RUNTIME if "/app/.venv/bin/python -c" in line]
    assert RUNTIME.index(check) > max(copies)
    assert RUNTIME.index(check) < RUNTIME.index(next(x for x in RUNTIME if x.startswith("USER")))
    for needle in (
        "import sys, ctranslate2, faster_whisper",
        "BatchedInferencePipeline",
        "__version__",
        "/app/.venv/",
        "/proc/self/maps",
        "/opt/ctranslate2/lib/",
    ):
        assert needle in check, needle
    assert check.rstrip().endswith('"$CT2_VERSION"')


def test_gpu_runtime_has_no_uv() -> None:
    assert not any("COPY --from=uv" in line for line in RUNTIME)
    for line in RUNTIME:
        assert not _uv_words(line), line


def test_gpu_images_pinned() -> None:
    images = external_froms(TEXT)
    assert len(images) == 3
    for image in images:
        assert PINNED_IMAGE.match(image), image
    [uv] = [i for i in images if "astral-sh/uv" in i]
    [cpu_uv] = [i for i in external_froms(CPU_TEXT) if "astral-sh/uv" in i]
    assert uv == cpu_uv


def test_gpu_cuda_bases() -> None:
    cuda = [(i, s) for i, s in froms(TEXT) if i.startswith("nvidia/cuda:")]
    assert [s for _, s in cuda] == ["build", "runtime"]
    tags = [i.split(":", 1)[1].split("@", 1)[0] for i, _ in cuda]
    versions = {tuple(int(n) for n in t.split("-", 1)[0].split(".")) for t in tags}
    assert len(versions) == 1
    assert versions.pop() >= (12, 9)
    assert tags[0].endswith("-cudnn-devel-ubuntu24.04")
    assert tags[1].endswith("-cudnn-runtime-ubuntu24.04")
    assert [s for _, s in froms(TEXT)] == ["uv", "build", "runtime"]


def test_gpu_runtime_libs() -> None:
    joined = " ".join(RUNTIME)
    assert "libopenblas0-pthread" in joined
    assert "libgomp1" in joined
    for source in ("/opt/python", "/opt/ctranslate2/lib", "/app/.venv"):
        assert f"COPY --from=build {source} {source}" in RUNTIME
    assert "echo /opt/ctranslate2/lib > /etc/ld.so.conf.d/ctranslate2.conf" in joined
    assert "LD_LIBRARY_PATH=" not in TEXT
    assert "if id -u ubuntu >/dev/null 2>&1; then userdel --remove ubuntu; fi" in joined


def test_gpu_no_own_dockerignore() -> None:
    assert not Path(ROOT / "Dockerfile.gpu.dockerignore").exists()


def test_gpu_header_points_at_smoke_test() -> None:
    head = "\n".join(TEXT.splitlines()[: _first_from_index()])
    assert "docs/deploy.md" in head
    assert "never by CI" in head.replace("\n# ", " ")


def test_gpu_dockerfile_check_friendly() -> None:
    """The BuildKit checks `docker build --check` runs: ENV form, JSON exec form, stage case."""
    for line in instructions(TEXT, "ENV"):
        assert re.match(r"ENV [A-Za-z_]+=", line), line
    assert not re.search(r"<<-?\s*['\"]?\w+", TEXT), "heredocs break the line joiner"
    assert not TEXT.startswith("# syntax=")
    for line in instructions(TEXT, "FROM"):
        assert " AS " in line, line
