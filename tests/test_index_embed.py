import os
import sys
import types
from pathlib import Path

import pytest

from earmark.config import ConfigError, Settings
from earmark.errors import TransientError
from earmark.index.embed import (
    DEFAULT_MAX_TOKENS,
    EmbedModelMissing,
    FastEmbedEmbedder,
    IndexExtraMissing,
    ModelInfo,
    build_embedder,
    default_model_dir,
    find_model,
    load_fastembed_runtime,
    model_cached,
)
from tests.index_helpers import BGE, StubFastEmbed, stub_extra_missing, stub_fastembed_runtime


def settings(
    tmp_path: Path, *, embed_model: str = BGE, embed_model_dir: Path | None = None
) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        embed_model=embed_model,
        embed_model_dir=embed_model_dir,
    )


def make(stub: StubFastEmbed, tmp_path: Path, *, allow_download: bool = False) -> FastEmbedEmbedder:
    return FastEmbedEmbedder(
        stub.runtime(), ModelInfo(BGE, 384), tmp_path / "models", allow_download=allow_download
    )


def test_conftest_sets_hf_offline() -> None:
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["FASTEMBED_CACHE_PATH"]


@pytest.mark.parametrize("missing", ["fastembed", "loguru"])
def test_missing_extra_is_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing: str
) -> None:
    stub_extra_missing(monkeypatch, missing)
    with pytest.raises(IndexExtraMissing) as info:
        load_fastembed_runtime()
    assert isinstance(info.value, ConfigError)
    [line] = info.value.errors
    assert line.startswith("EARMARK_INDEX_BACKEND: ")
    assert "uv sync --extra index" in line
    assert "not installed" in line
    with pytest.raises(IndexExtraMissing):
        build_embedder(settings(tmp_path), allow_download=False)
    assert not (tmp_path / "data").exists()


def test_broken_import_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    def broken(name: str, package: str | None = None) -> object:
        raise ImportError("secret detail /some/path")

    monkeypatch.setattr(importlib, "import_module", broken)
    with pytest.raises(IndexExtraMissing) as info:
        load_fastembed_runtime()
    [line] = info.value.errors
    assert "failed to import (ImportError)" in line
    assert "secret detail" not in line


def test_build_embedder_constructs_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stub = stub_fastembed_runtime(monkeypatch)
    embedder = build_embedder(settings(tmp_path), allow_download=True)
    assert embedder.model == BGE
    assert embedder.dim == 384
    assert stub.loads == []
    assert not (tmp_path / "data").exists()


def test_model_name_canonicalised(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stub_fastembed_runtime(monkeypatch)
    embedder = build_embedder(
        settings(tmp_path, embed_model="baai/BGE-small-en-v1.5"), allow_download=False
    )
    assert embedder.model == BGE


def test_unknown_model_is_config_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stub_fastembed_runtime(monkeypatch)
    name = "nope/" + "x" * 300 + "\x1b[31m"
    with pytest.raises(ConfigError) as info:
        build_embedder(settings(tmp_path, embed_model=name), allow_download=False)
    [line] = info.value.errors
    assert line.startswith("EARMARK_EMBED_MODEL: 'nope/xxx")
    assert "x" * 101 not in line
    assert "\x1b" not in line
    assert "not a fastembed text embedding model" in line


def test_embedder_loads_once_lazily(tmp_path: Path) -> None:
    stub = StubFastEmbed()
    embedder = make(stub, tmp_path)
    assert stub.loads == []
    assert embedder.fits("one two")
    assert embedder.fits("three")
    embedder.embed_documents(["a"])
    embedder.embed_query("q")
    assert len(stub.loads) == 1
    assert (tmp_path / "models").is_dir()


def test_embed_documents_empty_loads_nothing(tmp_path: Path) -> None:
    stub = StubFastEmbed()
    assert make(stub, tmp_path).embed_documents([]) == []
    assert stub.loads == []


def test_search_side_never_downloads(tmp_path: Path) -> None:
    stub = StubFastEmbed()
    make(stub, tmp_path, allow_download=False).embed_query("q")
    assert stub.loads == [(BGE, tmp_path / "models", False)]
    stub2 = StubFastEmbed()
    make(stub2, tmp_path, allow_download=True).embed_query("q")
    assert stub2.loads[0][2] is True


def test_fits_uses_max_tokens(tmp_path: Path) -> None:
    stub = StubFastEmbed()
    stub.model.max_tokens = 5
    embedder = make(stub, tmp_path)
    assert embedder.fits("a b c d")
    assert not embedder.fits("a b c d e")  # a truncated text reports exactly the limit
    assert not embedder.fits("a b c d e f")
    assert DEFAULT_MAX_TOKENS == 512


def test_load_errors_mapped(tmp_path: Path) -> None:
    stub = StubFastEmbed()
    stub.load_error = ValueError("Could not load model /secret/path from any source")
    with pytest.raises(EmbedModelMissing) as missing:
        make(stub, tmp_path, allow_download=False).fits("x")
    assert "not downloaded yet" in missing.value.message
    assert "/secret" not in missing.value.message
    assert isinstance(missing.value, TransientError)

    with pytest.raises(TransientError) as downloading:
        make(stub, tmp_path, allow_download=True).fits("x")
    assert not isinstance(downloading.value, EmbedModelMissing)
    assert "could not load or download" in downloading.value.message
    assert "/secret" not in downloading.value.message

    stub.load_error = OSError("disk /secret/full")
    with pytest.raises(TransientError, match="could not load or download") as oserr:
        make(stub, tmp_path, allow_download=True).fits("x")
    assert "/secret" not in oserr.value.message

    stub.load_error = RuntimeError("onnx /secret")
    with pytest.raises(TransientError, match=r"failed to load \(RuntimeError\)") as other:
        make(stub, tmp_path, allow_download=True).fits("x")
    assert "/secret" not in other.value.message


def test_failed_load_is_retried(tmp_path: Path) -> None:
    stub = StubFastEmbed()
    stub.load_error = OSError("x")
    embedder = make(stub, tmp_path, allow_download=True)
    with pytest.raises(TransientError):
        embedder.fits("x")
    stub.load_error = None
    assert embedder.fits("x")


def test_default_model_dir(tmp_path: Path) -> None:
    assert default_model_dir(settings(tmp_path)) == tmp_path / "data" / "models" / "embed"
    custom = settings(tmp_path, embed_model_dir=tmp_path / "m")
    assert default_model_dir(custom) == tmp_path / "m"


# -- find_model and model_cached (doctor) -----------------------------------------------------


def test_find_model_is_case_insensitive_and_names_the_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    info = ModelInfo(BGE, 384, "Qdrant/bge-small-en-v1.5-onnx-Q", "model_optimized.onnx")
    stub = stub_fastembed_runtime(monkeypatch, [info])
    runtime = stub.runtime([info])
    assert find_model(settings(tmp_path, embed_model=BGE.upper()), runtime) == info
    with pytest.raises(ConfigError) as err:
        find_model(settings(tmp_path, embed_model="nope\x1b"), runtime)
    assert err.value.errors[0].startswith("EARMARK_EMBED_MODEL: 'nope?'")


def test_model_cached_looks_in_the_cache_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    answers: list[object] = [None, "/cache/models--x/snapshots/abc/model.onnx"]
    hub = types.ModuleType("huggingface_hub")

    def try_to_load_from_cache(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return answers.pop(0)

    hub.try_to_load_from_cache = try_to_load_from_cache  # type: ignore[attr-defined]  # stub
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    info = ModelInfo(BGE, 384, "Qdrant/x", "model.onnx")
    assert model_cached(info, tmp_path) is False
    assert model_cached(info, tmp_path) is True
    assert calls[0] == (("Qdrant/x", "model.onnx"), {"cache_dir": str(tmp_path)})


def test_model_cached_cannot_tell(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert model_cached(ModelInfo(BGE, 384), tmp_path) is None  # no repo or file known
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)
    assert model_cached(ModelInfo(BGE, 384, "Qdrant/x", "model.onnx"), tmp_path) is None


def test_supported_fills_hf_repo_and_file(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeTextEmbedding:
        @staticmethod
        def list_supported_models() -> list[dict[str, object]]:
            return [
                {
                    "model": BGE,
                    "dim": 384,
                    "sources": {"hf": "Qdrant/bge-small-en-v1.5-onnx-Q"},
                    "model_file": "model_optimized.onnx",
                },
                {"model": "odd", "dim": 8, "sources": {"url": "x"}},
            ]

    fastembed = types.ModuleType("fastembed")
    fastembed.TextEmbedding = FakeTextEmbedding  # type: ignore[attr-defined]  # stub module
    loguru = types.ModuleType("loguru")

    class _Logger:
        def disable(self, name: str) -> None:
            return None

    loguru.logger = _Logger()  # type: ignore[attr-defined]  # stub module
    monkeypatch.setitem(sys.modules, "fastembed", fastembed)
    monkeypatch.setitem(sys.modules, "loguru", loguru)
    found = load_fastembed_runtime().supported()
    assert found[0] == ModelInfo(
        BGE, 384, "Qdrant/bge-small-en-v1.5-onnx-Q", "model_optimized.onnx"
    )
    assert found[1] == ModelInfo("odd", 8, None, None)
