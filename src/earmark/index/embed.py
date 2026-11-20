"""Local text embeddings with fastembed (ONNX, CPU).

fastembed ships no type information and is an optional extra, so it is imported through
``importlib`` inside ``load_fastembed_runtime`` only, and everything else is typed against
the small Protocols below. Building an embedder constructs no model: the model is looked up
(and, when allowed, downloaded) on first use.
"""

import importlib
import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from earmark.config import ConfigError, Settings
from earmark.errors import TransientError

logger = logging.getLogger(__name__)

EXTRA: Final = "index"
INSTALL_HINT: Final = "install it with `uv sync --extra index`"
EMBED_BATCH: Final = 32
DEFAULT_MAX_TOKENS: Final = 512
MAX_NAME_CHARS: Final = 100
SUPPORTED_MODELS_URL: Final = "https://qdrant.github.io/fastembed/examples/Supported_Models/"


class Embedder(Protocol):
    @property
    def model(self) -> str: ...  # canonical id, recorded in the table

    @property
    def dim(self) -> int: ...

    def fits(self, text: str, /) -> bool: ...  # embeddable without truncation

    def embed_documents(self, texts: Sequence[str], /) -> list[list[float]]: ...

    def embed_query(self, text: str, /) -> list[float]: ...


class IndexExtraMissing(ConfigError):  # noqa: N818  # name fixed by the plan
    """lancedb or fastembed (the `index` extra) is not installed or does not import."""


class EmbedModelMissing(TransientError):  # noqa: N818  # name fixed by the plan
    """The model is not in the cache and this process may not download it."""


class TextModelLike(Protocol):  # what FastEmbedEmbedder needs from fastembed.TextEmbedding
    max_tokens: int

    def passage_embed(self, texts: Sequence[str], /) -> list[list[float]]: ...

    def query_embed(self, text: str, /) -> list[float]: ...

    def token_count(self, text: str, /) -> int: ...


@dataclass(frozen=True, slots=True)
class ModelInfo:
    name: str  # canonical, as fastembed lists it
    dim: int


@dataclass(frozen=True, slots=True)
class FastEmbedRuntime:
    supported: Callable[[], Sequence[ModelInfo]]
    load: Callable[[str, Path, bool], TextModelLike]  # (name, cache_dir, allow_download)


def clean_text(value: str, limit: int = MAX_NAME_CHARS) -> str:
    """Printable characters only (others become ``?``), capped at ``limit``."""
    return "".join(c if c.isprintable() else "?" for c in value)[:limit]


def missing_extra(exc: ImportError, *modules: str) -> IndexExtraMissing:
    """The ``ConfigError`` for an import of one of ``modules`` (of the `index` extra) failing."""
    prefix = "EARMARK_INDEX_BACKEND: backend 'lancedb' needs the optional"
    if isinstance(exc, ModuleNotFoundError) and (exc.name or "").split(".")[0] in modules:
        detail = f"which is not installed; {INSTALL_HINT}, or set EARMARK_INDEX_BACKEND=none"
    else:
        detail = (
            f"which failed to import ({type(exc).__name__}); reinstall it with "
            f"`uv sync --extra {EXTRA}`"
        )
    return IndexExtraMissing([f"{prefix} '{EXTRA}' extra, {detail}"])


class _Vector(Protocol):
    def tolist(self) -> list[float]: ...


class _RawModel(Protocol):
    def passage_embed(self, texts: list[str], *, batch_size: int) -> Iterable[_Vector]: ...

    def query_embed(self, query: str) -> Iterable[_Vector]: ...

    def token_count(self, texts: list[str], *, batch_size: int) -> int: ...


class _FastEmbedModel:
    """Adapts ``fastembed.TextEmbedding`` to ``TextModelLike``; numpy stays inside."""

    def __init__(self, raw: _RawModel, max_tokens: int) -> None:
        self._raw = raw
        self.max_tokens = max_tokens

    def passage_embed(self, texts: Sequence[str], /) -> list[list[float]]:
        return [v.tolist() for v in self._raw.passage_embed(list(texts), batch_size=EMBED_BATCH)]

    def query_embed(self, text: str, /) -> list[float]:
        return next(v.tolist() for v in self._raw.query_embed(text))

    def token_count(self, text: str, /) -> int:
        return int(self._raw.token_count([text], batch_size=1))


def _max_tokens(raw: object) -> int:
    inner = getattr(raw, "model", None)
    tokenizer = getattr(inner, "tokenizer", None)
    truncation = getattr(tokenizer, "truncation", None)
    if isinstance(truncation, dict):
        limit = truncation.get("max_length")
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            return limit
    return DEFAULT_MAX_TOKENS


def load_fastembed_runtime() -> FastEmbedRuntime:
    """Import fastembed (and loguru, to silence it). Raises ``IndexExtraMissing``."""
    try:
        fe = importlib.import_module("fastembed")
        loguru = importlib.import_module("loguru")
    except ImportError as exc:
        raise missing_extra(exc, "fastembed", "loguru") from None
    # fastembed logs through loguru, which the redacting formatter never sees.
    loguru.logger.disable("fastembed")

    def supported() -> Sequence[ModelInfo]:
        listed: Iterable[dict[str, object]] = fe.TextEmbedding.list_supported_models()
        found: list[ModelInfo] = []
        for entry in listed:
            name, dim = entry.get("model"), entry.get("dim")
            if isinstance(name, str) and isinstance(dim, int):
                found.append(ModelInfo(name, dim))
        return found

    def load(name: str, cache_dir: Path, allow_download: bool) -> TextModelLike:
        raw: _RawModel = fe.TextEmbedding(
            model_name=name, cache_dir=str(cache_dir), local_files_only=not allow_download
        )
        return _FastEmbedModel(raw, _max_tokens(raw))

    return FastEmbedRuntime(supported, load)


def default_model_dir(settings: Settings) -> Path:
    return settings.embed_model_dir or settings.data_dir / "models" / "embed"


class FastEmbedEmbedder:
    """Loads the model on first use (once per instance) and embeds under a lock."""

    def __init__(
        self,
        runtime: FastEmbedRuntime,
        info: ModelInfo,
        cache_dir: Path,
        *,
        allow_download: bool,
    ) -> None:
        self._runtime = runtime
        self._info = info
        self._cache_dir = cache_dir
        self._allow_download = allow_download
        self._loaded: TextModelLike | None = None
        self._lock = threading.Lock()

    @property
    def model(self) -> str:
        return self._info.name

    @property
    def dim(self) -> int:
        return self._info.dim

    def _model(self) -> TextModelLike:
        # Called with the lock held.
        if self._loaded is not None:
            return self._loaded
        name = self._info.name
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            logger.info("loading embedding model %r", name)
            loaded = self._runtime.load(name, self._cache_dir, self._allow_download)
        except ValueError as exc:
            if not self._allow_download:
                raise EmbedModelMissing(
                    f"the embedding model {name} is not downloaded yet (looked in "
                    "EARMARK_EMBED_MODEL_DIR); it is downloaded when the worker indexes an "
                    "episode or by `earmark reindex`"
                ) from exc
            raise TransientError(
                f"could not load or download the embedding model {name} (check network "
                "access to Hugging Face and EARMARK_EMBED_MODEL_DIR)"
            ) from exc
        except OSError as exc:
            raise TransientError(
                f"could not load or download the embedding model {name} (check network "
                "access to Hugging Face and EARMARK_EMBED_MODEL_DIR)"
            ) from exc
        except Exception as exc:
            raise TransientError(
                f"the embedding model {name} failed to load ({type(exc).__name__})"
            ) from exc
        self._loaded = loaded
        return loaded

    def fits(self, text: str, /) -> bool:
        with self._lock:
            model = self._model()
            return model.token_count(text) < model.max_tokens

    def embed_documents(self, texts: Sequence[str], /) -> list[list[float]]:
        if not texts:
            return []
        with self._lock:
            return self._model().passage_embed(texts)

    def embed_query(self, text: str, /) -> list[float]:
        with self._lock:
            return self._model().query_embed(text)


def build_embedder(settings: Settings, *, allow_download: bool) -> Embedder:
    """Look the model up in fastembed's list. Constructs no model and creates no directory."""
    runtime = load_fastembed_runtime()
    wanted = settings.embed_model.casefold()
    info = next((m for m in runtime.supported() if m.name.casefold() == wanted), None)
    if info is None:
        raise ConfigError(
            [
                f"EARMARK_EMBED_MODEL: '{clean_text(settings.embed_model)}' is not a fastembed "
                f"text embedding model (see {SUPPORTED_MODELS_URL})"
            ]
        )
    return FastEmbedEmbedder(
        runtime, info, default_model_dir(settings), allow_download=allow_download
    )
