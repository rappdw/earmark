"""Sample-model builders shared by the test modules."""

from datetime import UTC, datetime, timedelta, timezone

from earmark.models import (
    ANALYSIS_SCHEMA_VERSION,
    Analysis,
    Claim,
    Entity,
    Episode,
    Job,
    JobResult,
    ResearchLead,
    Segment,
    Transcript,
    episode_key,
    prompt_hash,
)

PST = timezone(timedelta(hours=-8))
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def make_episode(
    *,
    guid: str = "urn:example:guid-1",
    title: str = "Épisode 1: «Tests»",
    duration_s: float | None = 3600.0,
) -> Episode:
    return Episode(
        key=episode_key("apple_podcasts", "1200361736", guid),
        source="apple_podcasts",
        show_id="1200361736",
        episode_id="1000700000001",
        guid=guid,
        title=title,
        show_title="Le Podcast ☕",
        published_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=PST),
        duration_s=duration_s,
        audio_url="https://example.com/audio.mp3",
        feed_url="https://example.com/feed.xml",
        url="https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000700000001",
        description="Une description avec des accents: é à ü.",
    )


def make_transcript(*, guid: str = "urn:example:guid-1") -> Transcript:
    return Transcript(
        episode_key=make_episode(guid=guid).key,
        language="fr",
        duration_s=125.5,
        model="fake-asr",
        segments=(
            Segment(start=0.0, end=65.0, text="Bonjour à tous", speaker="A"),
            Segment(start=65.0, end=125.5, text="Merci"),
        ),
        warnings=("a transcript warning",),
    )


def make_analysis(
    *,
    guid: str = "urn:example:guid-1",
    prompt: str | None = "Quels articles sont cités ?",
    model: str = "fake-llm",
    created_at: datetime = T0,
) -> Analysis:
    entity = Entity(name="Zoë", context="Cité en exemple", timestamps_s=(1.0, 2.5))
    return Analysis(
        episode_key=make_episode(guid=guid).key,
        prompt=prompt,
        prompt_hash=prompt_hash(prompt, model),
        model=model,
        schema_version=ANALYSIS_SCHEMA_VERSION,
        created_at=created_at,
        summary="Résumé ☕",
        key_claims=(Claim(text="Une affirmation", timestamp_s=3.0),),
        people=(entity,),
        papers=(entity,),
        companies=(entity,),
        tools=(entity,),
        topics=(entity,),
        open_questions=("Pourquoi ?",),
        research_leads=(ResearchLead(text="Piste", rationale="Parce que", timestamp_s=4.0),),
        prompt_response="Réponse" if prompt is not None else None,
        warnings=("an analysis warning",),
    )


def make_job(*, kind: str = "ingest") -> Job:
    key = make_episode().key
    if kind == "reanalyze":
        return Job(
            id="a" * 32,
            kind="reanalyze",
            status="done",
            stage="notify",
            url=None,
            prompt="p",
            episode_key=key,
            attempts=2,
            run_after=T0,
            error="e",
            warnings=("w",),
            result=JobResult(note_path="Earmark/Podcasts/x.md", analysis_hash="b" * 16),
            shared_at=datetime(2026, 1, 1, 4, 0, tzinfo=PST),
            created_at=T0,
            updated_at=T0,
            heartbeat_at=T0,
        )
    return Job(
        id="a" * 32,
        kind="ingest",
        status="running",
        stage="fetch",
        url="https://podcasts.apple.com/us/podcast/x/id1?i=2",
        prompt="Quoi ?",
        episode_key=key,
        attempts=1,
        run_after=T0,
        error=None,
        warnings=("w",),
        result=JobResult(
            note_path="Earmark/Podcasts/Show/x.md",
            transcript_note_path="Earmark/Podcasts/Show/x transcript.md",
            analysis_hash="b" * 16,
        ),
        shared_at=datetime(2026, 1, 1, 4, 0, tzinfo=PST),
        created_at=T0,
        updated_at=T0,
        heartbeat_at=T0,
    )
