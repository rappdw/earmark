import os
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
    load_fastembed_runtime,
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
