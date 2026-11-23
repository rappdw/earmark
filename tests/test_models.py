import math
import subprocess
import sys
import typing
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

import earmark.models
from earmark.models import (
    INGEST_ERROR_TYPES,
    MAX_PROMPT_CHARS,
    MAX_URL_CHARS,
    Episode,
    EpisodeRef,
    IngestRequest,
    Job,
    JobKind,
    JobResult,
    Model,
    Segment,
    ShareRecord,
    Transcript,
    episode_key,
    format_hms,
    is_episode_key,
)
from tests.factories import PST, T0, make_episode, make_job, make_transcript

KEY = "3848d011be9e28df3ac6"


def _minimal_job() -> Job:
    return Job(
        id="0" * 32,
        kind="ingest",
        status="queued",
        url="u",
        prompt=None,
        run_after=T0,
        shared_at=T0,
        created_at=T0,
        updated_at=T0,
    )


def _minimal_episode() -> Episode:
    return Episode(
        key=KEY,
        source="apple_podcasts",
        show_id="1200361736",
        episode_id="1",
        guid="urn:example:guid-1",
        title="t",
        show_title="s",
        published_at=None,
        duration_s=None,
        audio_url="a",
        feed_url=None,
        url="u",
    )


CASES: list[Model] = [
    IngestRequest(url="u"),
    IngestRequest(url="u", prompt="p", timestamp=T0),
    EpisodeRef(source="apple_podcasts", show_id="1", episode_id="2", country="us", url="u"),
    make_episode(),
    _minimal_episode(),
    Segment(start=0.0, end=1.0, text="t"),
    Segment(start=0.0, end=1.0, text="t", speaker="A"),
    make_transcript(),
    Transcript(episode_key=KEY, language=None, duration_s=0.0, model="m"),
    ShareRecord(episode_key=KEY, first_shared_at=T0),
    JobResult(duration_s=0.0),
    JobResult(duration_s=3600.5),
    make_job(),
    _minimal_job(),
]


@pytest.mark.parametrize("model", CASES)
def test_json_round_trip(model: Model) -> None:
    dumped = model.model_dump_json()
    loaded = type(model).model_validate_json(dumped)
    assert loaded == model
    assert loaded.model_dump_json() == dumped


def test_round_trip_covers_every_model() -> None:
    defined = {
        obj
        for obj in vars(earmark.models).values()
        if isinstance(obj, type)
        and issubclass(obj, Model)
        and obj is not Model
        and obj.__module__ == "earmark.models"
    }
    assert defined
    assert defined <= {type(m) for m in CASES}


def test_round_trip_preserves_offset() -> None:
    dumped = make_episode().model_dump_json()
    assert '"published_at":"2026-01-02T03:04:05-08:00"' in dumped


def test_episode_key_golden() -> None:
    assert episode_key("apple_podcasts", "1200361736", "urn:example:guid-1") == KEY


def test_episode_key_shape() -> None:
    key = episode_key("apple_podcasts", "1", "g")
    assert len(key) == 20
    assert is_episode_key(key)
    assert not is_episode_key("0" * 20 + "\n")
    assert not is_episode_key("A" * 20)


def test_episode_key_differs_by_part() -> None:
    base = episode_key("apple_podcasts", "1", "g")
    assert episode_key("apple_podcasts", "2", "g") != base
    assert episode_key("apple_podcasts", "1", "h") != base
    assert episode_key("apple_podcasts", "1", " g ") != base


@pytest.mark.parametrize(
    ("source", "show_id", "guid"),
    [("", "1", "g"), ("s", "", "g"), ("s", "1", ""), ("s", "1:2", "g"), ("s:t", "1", "g")],
)
def test_episode_key_rejects_bad_parts(source: str, show_id: str, guid: str) -> None:
    with pytest.raises(ValueError, match=r"empty|':'"):
        episode_key(source, show_id, guid)


def test_to_text_basic() -> None:
    t = Transcript(
        episode_key=KEY,
        language=None,
        duration_s=70.0,
        model="m",
        segments=(Segment(start=0, end=1, text="Hello"), Segment(start=65, end=66, text="World")),
    )
    assert t.to_text() == "[00:00:00] Hello\n[00:01:05] World\n"


def _transcript_of(*segments: Segment) -> Transcript:
    return Transcript(episode_key=KEY, language=None, duration_s=0.0, model="m", segments=segments)


def test_to_text_over_ten_hours() -> None:
    t = _transcript_of(
        Segment(start=36000.4, end=36001, text="a"), Segment(start=360000, end=360001, text="b")
    )
    assert t.to_text() == "[10:00:00] a\n[100:00:00] b\n"


@pytest.mark.parametrize("text", ["", "  \n "])
def test_to_text_empty_segment_text(text: str) -> None:
    assert _transcript_of(Segment(start=5, end=6, text=text)).to_text() == "[00:00:05]\n"


def test_to_text_no_segments() -> None:
    assert _transcript_of().to_text() == ""


def test_to_text_collapses_newlines() -> None:
    assert _transcript_of(Segment(start=0, end=1, text="a\nb  c")).to_text() == "[00:00:00] a b c\n"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "00:00:00"), (59.9, "00:00:59"), (3725.9, "01:02:05"), (360000, "100:00:00")],
)
def test_format_hms(seconds: float, expected: str) -> None:
    assert format_hms(seconds) == expected


@pytest.mark.parametrize("bad", [-1.0, math.inf, math.nan])
def test_format_hms_rejects(bad: float) -> None:
    with pytest.raises(ValueError, match="seconds"):
        format_hms(bad)


def test_episode_key_must_match() -> None:
    good = make_episode()
    bad = good.model_dump(mode="json") | {"key": "0" * 20}
    with pytest.raises(ValidationError, match="key does not match"):
        Episode.model_validate(bad)
    import json

    with pytest.raises(ValidationError, match="key does not match"):
        Episode.model_validate_json(json.dumps(bad))


def test_segment_end_before_start_rejected() -> None:
    with pytest.raises(ValidationError, match="end must be"):
        Segment(start=2.0, end=1.0, text="t")


@pytest.mark.parametrize("bad", [-1.0, math.nan, math.inf])
def test_negative_and_nan_seconds_rejected(bad: float) -> None:
    with pytest.raises(ValidationError):
        Segment(start=bad, end=10.0, text="t")
    with pytest.raises(ValidationError):
        make_episode(duration_s=bad)


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        IngestRequest(url="u", timestamp=datetime.fromisoformat("2026-01-01T00:00:00"))


def test_extra_field_rejected() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        IngestRequest.model_validate_json('{"url": "u", "surprise": 1}')


def test_models_frozen() -> None:
    req = IngestRequest(url="u")
    with pytest.raises(ValidationError, match="frozen"):
        req.__setattr__("url", "v")


def test_job_kind_requirements() -> None:
    base: dict[str, Any] = _minimal_job().model_dump(mode="json")
    with pytest.raises(ValidationError, match="requires url"):
        Job.model_validate(base | {"url": None})
    assert typing.get_args(JobKind) == ("ingest",)


def test_bad_ids_rejected() -> None:
    data: dict[str, Any] = _minimal_job().model_dump(mode="json")
    with pytest.raises(ValidationError):
        Job.model_validate(data | {"id": "0" * 31})
    with pytest.raises(ValidationError):
        Transcript(episode_key="0" * 19, language=None, duration_s=0.0, model="m")


def test_utc_default_clock_is_aware() -> None:
    assert earmark.models.utc_now().tzinfo is not None
    assert T0.tzinfo is UTC


def test_contract_modules_import_light() -> None:
    code = (
        "import sys, earmark.models, earmark.errors, earmark.artifacts, "
        "earmark.pipeline.stages, earmark.fakes\n"
        "heavy = ['fastapi','uvicorn','httpx2','typer','faster_whisper','ctranslate2']\n"
        "print(sorted(m for m in heavy if m in sys.modules))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "[]"


def _error_types(exc: pytest.ExceptionInfo[ValidationError]) -> set[str]:
    return {e["type"] for e in exc.value.errors()}


@pytest.mark.parametrize("blank", ["", " ", "\n\t", "\N{NO-BREAK SPACE}", "\N{IDEOGRAPHIC SPACE}"])
def test_ingest_blank_prompt_is_none(blank: str) -> None:
    assert IngestRequest(url="u", prompt=blank).prompt is None


def test_ingest_prompt_stripped() -> None:
    assert IngestRequest(url="u", prompt="  why?\n").prompt == "why?"
    assert IngestRequest(url="u", prompt=" a\nb ").prompt == "a\nb"


def test_ingest_prompt_length_cap() -> None:
    ok = "x" * MAX_PROMPT_CHARS
    assert IngestRequest(url="u", prompt=f"  {ok}\n").prompt == ok
    with pytest.raises(ValidationError) as exc:
        IngestRequest(url="u", prompt=ok + "x")
    assert _error_types(exc) == {"prompt_too_long"}
    assert "at most 4000 characters" in exc.value.errors()[0]["msg"]


def test_ingest_url_stripped_and_capped() -> None:
    assert IngestRequest(url="  u \n").url == "u"
    for empty in ("", "  "):
        with pytest.raises(ValidationError) as exc:
            IngestRequest(url=empty)
        assert _error_types(exc) == {"url_empty"}
    assert len(IngestRequest(url="u" * MAX_URL_CHARS).url) == MAX_URL_CHARS
    with pytest.raises(ValidationError) as exc:
        IngestRequest(url="u" * (MAX_URL_CHARS + 1))
    assert _error_types(exc) == {"url_too_long"}


def test_ingest_timestamp_out_of_range() -> None:
    with pytest.raises(ValidationError) as exc:
        IngestRequest.model_validate_json('{"url": "u", "timestamp": "0001-01-01T00:00:00+05:00"}')
    assert _error_types(exc) == {"timestamp_out_of_range"}
    ok = IngestRequest.model_validate_json('{"url": "u", "timestamp": "2026-01-01T00:00:00-08:00"}')
    assert ok.timestamp is not None
    assert ok.timestamp.utcoffset() == PST.utcoffset(None)


def test_ingest_error_types_registry() -> None:
    assert sorted(INGEST_ERROR_TYPES) == [
        "prompt_too_long",
        "timestamp_out_of_range",
        "url_empty",
        "url_too_long",
    ]


def test_ingest_normalised_round_trip() -> None:
    req = IngestRequest(url=" u ", prompt="  padded \n", timestamp=T0)
    assert IngestRequest.model_validate_json(req.model_dump_json()) == req
    assert req.prompt == "padded"


def test_normalize_prompt() -> None:
    from earmark.models import normalize_prompt

    assert normalize_prompt(None) is None
    assert normalize_prompt("  \n ") is None
    assert normalize_prompt("  a b \n") == "a b"
    assert normalize_prompt("x" * MAX_PROMPT_CHARS) == "x" * MAX_PROMPT_CHARS
    with pytest.raises(ValueError, match="at most 4000 characters") as info:
        normalize_prompt("x" * (MAX_PROMPT_CHARS + 1))
    assert str(info.value) == f"prompt is too long (at most {MAX_PROMPT_CHARS} characters)"


def test_lone_surrogates_replaced() -> None:
    bad = "a\ud800b\udfff"
    good = "a\ufffdb\ufffd"
    episode = make_episode(title=bad)
    assert episode.title == good
    assert Segment(start=0, end=1, text=bad).text == good
    job = make_job()
    job2 = Job.model_validate({**job.model_dump(), "warnings": (bad,)})
    assert job2.warnings == (good,)
    for model in (episode, job2):
        dumped = model.model_dump_json()
        assert type(model).model_validate_json(dumped) == model


def test_scrub_keeps_valid_text_identical() -> None:
    from earmark.models import scrub_surrogates

    text = "emoji \U0001f600 pair and caf\u00e9"
    assert scrub_surrogates(text) is text
    assert scrub_surrogates("x\ud800") == "x\ufffd"
