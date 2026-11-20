"""Builders and stubs for the index tests: synthetic episodes, a recording rebuild target,
and stand-ins for the fastembed and lancedb loaders (so the default suite needs neither)."""

import importlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest

from earmark.index.chunks import EpisodeDocs, NotePaths
from earmark.index.embed import FastEmbedRuntime, ModelInfo
from earmark.models import (
    ANALYSIS_SCHEMA_VERSION,
    Analysis,
    Claim,
    Episode,
    ResearchLead,
    Segment,
    Transcript,
    prompt_hash,
)
from tests.factories import T0, make_episode

BGE = "BAAI/bge-small-en-v1.5"


def make_segments(
    count: int, *, words: int = 8, step: float = 5.0, prefix: str = "seg"
) -> tuple[Segment, ...]:
    return tuple(
        Segment(
            start=i * step,
            end=i * step + step,
            text=" ".join(f"{prefix}{i}w{j}" for j in range(words)),
        )
        for i in range(count)
    )


def make_transcript_of(episode: Episode, segments: Sequence[Segment]) -> Transcript:
    end = max((s.end for s in segments), default=0.0)
    return Transcript(
        episode_key=episode.key,
        language="en",
        duration_s=end,
        model="fake-asr",
        segments=tuple(segments),
    )


def make_analysis_of(
    episode: Episode,
    *,
    prompt: str | None = None,
    summary: str = "A summary of the episode.",
    claims: Sequence[tuple[str, float | None]] = (("A claim.", 65.0),),
    leads: Sequence[tuple[str, str, float | None]] = (("A lead", "because", 70.0),),
    questions: Sequence[str] = ("An open question?",),
    response: str | None = None,
    created_at: datetime = T0,
    model: str = "fake-llm",
) -> Analysis:
    return Analysis(
        episode_key=episode.key,
        prompt=prompt,
        prompt_hash=prompt_hash(prompt, model),
        model=model,
        schema_version=ANALYSIS_SCHEMA_VERSION,
        created_at=created_at,
        summary=summary,
        key_claims=tuple(Claim(text=t, timestamp_s=s) for t, s in claims),
        open_questions=tuple(questions),
        research_leads=tuple(ResearchLead(text=t, rationale=r, timestamp_s=s) for t, r, s in leads),
        prompt_response=response,
    )


def make_docs(
    *,
    guid: str = "urn:index:1",
    title: str = "Episode One",
    segments: Sequence[Segment] | None = None,
    analyses: Sequence[Analysis] | None = None,
) -> EpisodeDocs:
    episode = make_episode(guid=guid, title=title)
    segs = make_segments(6) if segments is None else tuple(segments)
    made = (make_analysis_of(episode),) if analyses is None else tuple(analyses)
    return EpisodeDocs(episode, make_transcript_of(episode, segs), made)


def with_analyses(docs: EpisodeDocs, *analyses: Analysis) -> EpisodeDocs:
    return EpisodeDocs(
        docs.episode,
        docs.transcript,
        tuple(sorted(analyses, key=lambda a: (a.created_at, a.prompt_hash))),
    )


NOTES = NotePaths(
    "Earmark/Podcasts/Show/Episode One.md", "Earmark/Podcasts/Show/Episode One transcript.md"
)


@dataclass
class RecordingTarget:
    """A ``RebuildTarget`` that records calls; rebuild_episode runs the loader like the real one."""

    rows_per_episode: int = 3
    calls: list[str] = field(default_factory=list)
    notes: dict[str, NotePaths] = field(default_factory=dict)
    fail_on: dict[str, Exception] = field(default_factory=dict)

    def reset(self) -> None:
        self.calls.append("reset")

    def rebuild_episode(
        self, key: str, load: Callable[[], EpisodeDocs | None], notes: NotePaths, /
    ) -> int | None:
        self.calls.append(f"rebuild:{key}")
        if key in self.fail_on:
            raise self.fail_on[key]
        self.notes[key] = notes
        docs = load()
        return None if docs is None else self.rows_per_episode

    def optimize(self) -> None:
        self.calls.append("optimize")


@dataclass
class StubModel:
    """Stands in for a loaded fastembed model; counts words as tokens."""

    max_tokens: int = 512
    embedded: list[str] = field(default_factory=list)
    dim: int = 4

    def passage_embed(self, texts: Sequence[str], /) -> list[list[float]]:
        self.embedded.extend(texts)
        return [[1.0] + [0.0] * (self.dim - 1) for _ in texts]

    def query_embed(self, text: str, /) -> list[float]:
        return [1.0] + [0.0] * (self.dim - 1)

    def token_count(self, text: str, /) -> int:
        return len(text.split())


@dataclass
class StubFastEmbed:
    loads: list[tuple[str, Path, bool]] = field(default_factory=list)
    model: StubModel = field(default_factory=StubModel)
    load_error: Exception | None = None

    def runtime(self, models: Sequence[ModelInfo] = (ModelInfo(BGE, 384),)) -> FastEmbedRuntime:
        def load(name: str, cache_dir: Path, allow_download: bool) -> StubModel:
            self.loads.append((name, cache_dir, allow_download))
            if self.load_error is not None:
                raise self.load_error
            return self.model

        return FastEmbedRuntime(supported=lambda: models, load=load)


def stub_fastembed_runtime(
    monkeypatch: pytest.MonkeyPatch,
    models: Sequence[ModelInfo] = (ModelInfo(BGE, 384),),
) -> StubFastEmbed:
    """Make ``load_fastembed_runtime`` return a stub (no fastembed import)."""
    stub = StubFastEmbed()
    runtime = stub.runtime(models)
    monkeypatch.setattr("earmark.index.embed.load_fastembed_runtime", lambda: runtime)
    return stub


def stub_extra_missing(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Make importing any of ``names`` raise ``ModuleNotFoundError`` (as without the extra)."""
    real = importlib.import_module

    def fake(name: str, package: str | None = None) -> object:
        if name.split(".")[0] in names:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return real(name, package)

    monkeypatch.setattr(importlib, "import_module", fake)


def notes_map(keys: Sequence[str], subdir: str = "Earmark") -> Mapping[str, NotePaths]:
    return {k: NotePaths(f"{subdir}/Podcasts/{k}.md", None) for k in keys}
