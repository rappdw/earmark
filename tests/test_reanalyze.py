"""A reanalyze job through the real vault store: sections, entities, restored notes."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from earmark.db import database_path
from earmark.fakes import FakeAnalyzer, make_fake_backends
from earmark.models import Analysis, Entity, Episode, Job, Transcript, prompt_hash
from earmark.vault.notes import VaultStore
from tests.pipeline_env import CANON, PipelineEnv, make_env

GRACE = Entity(name="Grace Hopper", context="Built a compiler.", timestamps_s=(0.0,))
PROMPT = "Who built the first compiler?"


class PromptedAnalyzer:
    """The fake analyzer, but a prompted analysis also finds Grace Hopper."""

    def __init__(self, inner: FakeAnalyzer) -> None:
        self.inner = inner
        self.computed = 0

    def analyze(self, episode: Episode, transcript: Transcript, prompt: str | None) -> Analysis:
        before = self.inner.computed
        analysis = self.inner.analyze(episode, transcript, prompt)
        if self.inner.computed == before:
            return analysis
        self.computed += 1
        if prompt is not None:
            analysis = analysis.model_copy(
                update={
                    "people": (*analysis.people, GRACE),
                    "created_at": analysis.created_at + timedelta(hours=1),
                }
            )
            self.inner._artifacts.write_analysis(analysis)
        return analysis


def _env(tmp_path: Path) -> tuple[PipelineEnv, PromptedAnalyzer]:
    vault = tmp_path / "vault"
    vault.mkdir()
    fakes = make_fake_backends(tmp_path / "data", vault_dir=vault)
    analyzer = PromptedAnalyzer(fakes.analyzer)
    store = VaultStore(vault, "Earmark", data_dir=tmp_path / "data")
    backends = replace(fakes.backends(), store=store, analyzer=analyzer)
    return make_env(tmp_path, fakes=fakes, backends=backends), analyzer


def _run(env: PipelineEnv, *, key: str | None = None, prompt: str | None = None) -> Job:
    if key is None:
        env.jobs.enqueue(CANON, prompt=prompt)
    else:
        env.jobs.enqueue_reanalyze(key, prompt=prompt)
    job = env.worker.run_once()
    assert job is not None
    assert job.status == "done", job.error
    return job


def _note(env: PipelineEnv, job: Job) -> Path:
    assert job.result is not None
    assert job.result.note_path is not None
    return env.vault / job.result.note_path


def test_second_section_and_entities(tmp_path: Path) -> None:
    env, _ = _env(tmp_path)
    first = _run(env)
    assert first.episode_key is not None
    note = _note(env, first)
    grace = env.vault / "Earmark/Entities/People/Grace Hopper.md"
    assert "Analysis: " not in note.read_text()
    assert not grace.exists()

    second = _run(env, key=first.episode_key, prompt=PROMPT)
    assert _note(env, second) == note
    text = note.read_text()
    assert "Default analysis" in text
    assert f"Analysis: {PROMPT}" in text
    assert text.index(f"Analysis: {PROMPT}") < text.index("Default analysis")  # newest first
    assert "[[Earmark/Entities/People/Grace Hopper|Grace Hopper]]" in text
    assert grace.exists()
    assert note.stem in grace.read_text()
    with closing(sqlite3.connect(database_path(tmp_path / "data"))) as conn:
        hashes = {r[0] for r in conn.execute("SELECT DISTINCT prompt_hash FROM mentions")}
    assert hashes == {prompt_hash(None, "fake-llm"), prompt_hash(PROMPT, "fake-llm")}


def test_same_prompt_restores_deleted_note(tmp_path: Path) -> None:
    env, analyzer = _env(tmp_path)
    first = _run(env, prompt=PROMPT)
    assert first.episode_key is not None
    note = _note(env, first)
    original = note.read_bytes()
    note.unlink()
    again = _run(env, key=first.episode_key, prompt=PROMPT)
    assert _note(env, again) == note
    assert note.read_bytes() == original
    assert analyzer.computed == 1


@pytest.mark.parametrize("prompt", [None, PROMPT])
def test_reanalyze_leaves_intact_note_untouched(tmp_path: Path, prompt: str | None) -> None:
    env, _ = _env(tmp_path)
    first = _run(env, prompt=prompt)
    assert first.episode_key is not None
    note = _note(env, first)
    before = (note.read_bytes(), note.stat().st_mtime_ns)
    _run(env, key=first.episode_key, prompt=prompt)
    assert (note.read_bytes(), note.stat().st_mtime_ns) == before
    assert not list((env.vault / "Earmark").glob("Backups/*"))
