"""Doctor against a real LanceDB and fastembed's model list (``-m index``).

Needs the `index` extra. No model is downloaded and no socket opens: doctor only lists
fastembed's models and looks in the (empty) Hugging Face cache layout.
"""

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from earmark.config import Settings
from earmark.doctor import run_doctor
from earmark.index.lancedb_index import (
    INDEX_VERSION,
    META_DIM,
    META_MODEL,
    META_VERSION,
    load_lance_runtime,
)

pytestmark = pytest.mark.index


def settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    (tmp_path / "vault").mkdir(exist_ok=True)
    values: dict[str, Any] = {
        "api_token": SecretStr("t" * 40),
        "data_dir": tmp_path / "data",
        "vault_dir": tmp_path / "vault",
        "resolver_backend": "fake",
        "fetch_backend": "fake",
        "asr_backend": "fake",
        "llm_provider": "fake",
        "store_backend": "fake",
        "index_backend": "lancedb",
        "notify_backend": "none",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_doctor_index_ok_empty(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    results = {r.name: r for r in run_doctor(settings)}
    assert results["index"].status == "ok"
    assert "BAAI/bge-small-en-v1.5 (384 dimensions)" in results["index"].detail
    assert results["embed model"].status == "warn"  # the cache is empty
    assert "not downloaded yet" in results["embed model"].detail
    assert results["search index"].status == "ok"
    assert results["search index"].detail.startswith("empty")
    assert sorted(p.name for p in tmp_path.rglob("*")) == before  # nothing created


def test_doctor_index_mismatch_fails(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    load_lance_runtime().connect(settings.data_dir / "lancedb").create_table(
        32, {META_VERSION: str(INDEX_VERSION), META_MODEL: "other-model", META_DIM: "32"}
    )
    results = {r.name: r for r in run_doctor(settings)}
    assert results["search index"].status == "fail"
    assert "earmark reindex" in results["search index"].detail
