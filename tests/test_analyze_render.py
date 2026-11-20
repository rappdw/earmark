import pytest

from earmark.analyze.render import render_analysis
from earmark.models import (
    ANALYSIS_SCHEMA_VERSION,
    Analysis,
    Claim,
    Entity,
    ResearchLead,
    prompt_hash,
)
from tests.factories import T0

KEY = "0123456789abcdef0123"

REFERENCE = """\
## Analysis: What papers are cited? List them.

> What papers are cited?
> List them.

First paragraph.

Second &lt;b> paragraph.

### Answer

Two papers.

### Key claims

- [00:01:05] Claim one
- Claim two

### People

- **Ada Lovelace**: Pioneer. [00:00:05] [01:02:05]

### Papers

- **On Computable Numbers**

### Topics

- **Computing**

### Open questions

- Why?

### Research leads

- [00:01:05] Read the paper — It is cited.
"""


def make(prompt: str | None = "What papers are cited?\nList them.", **kw: object) -> Analysis:
    fields: dict[str, object] = {
        "episode_key": KEY,
        "prompt": prompt,
        "prompt_hash": prompt_hash(prompt, "m"),
        "model": "m",
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "created_at": T0,
        "summary": "First paragraph.\n\nSecond <b>\nparagraph.",
        "prompt_response": "Two papers." if prompt else None,
        "key_claims": (Claim(text="Claim one", timestamp_s=65.0), Claim(text="Claim two")),
        "people": (Entity(name="Ada Lovelace", context="Pioneer.", timestamps_s=(5.0, 3725.0)),),
        "papers": (Entity(name="On Computable Numbers"),),
        "topics": (Entity(name="Computing"),),
        "open_questions": ("Why?",),
        "research_leads": (
            ResearchLead(text="Read the paper", rationale="It is cited.", timestamp_s=65.0),
        ),
    }
    fields.update(kw)
    return Analysis.model_validate(fields)


def test_render_reference_example() -> None:
    assert render_analysis(make()) == REFERENCE


def test_render_default_analysis() -> None:
    out = render_analysis(make(prompt=None))
    assert out.startswith("## Default analysis\n\nFirst paragraph.")
    assert not any(line.startswith(">") for line in out.splitlines())
    assert "Answer" not in out


def test_empty_sections_omitted() -> None:
    out = render_analysis(make())
    for title in ("Companies", "Tools"):
        assert f"### {title}" not in out
    bare = render_analysis(
        make(
            prompt=None,
            key_claims=(),
            people=(),
            papers=(),
            topics=(),
            open_questions=(),
            research_leads=(),
        )
    )
    assert bare == "## Default analysis\n\nFirst paragraph.\n\nSecond &lt;b> paragraph.\n"


def test_heading_level() -> None:
    out = render_analysis(make(), heading_level=3)
    assert out.startswith("### Analysis:")
    assert "\n#### Answer\n" in out
    assert "\n#### Key claims\n" in out
    for bad in (0, 6):
        with pytest.raises(ValueError, match="heading_level"):
            render_analysis(make(), heading_level=bad)


def test_render_is_deterministic() -> None:
    analysis = make()
    assert render_analysis(analysis) == render_analysis(analysis)
    copy = Analysis.model_validate_json(analysis.model_dump_json())
    assert render_analysis(copy) == render_analysis(analysis)


def test_render_escapes_markup() -> None:
    analysis = make(
        prompt=None,
        summary="<!-- earmark:end -->\n\n# x\n\n- y\n\n1. z\n\n---\n\n> q\n\n=",
        people=(Entity(name="<script>alert(1)</script>", context="<i>ctx"),),
    )
    out = render_analysis(analysis)
    assert "&lt;!-- earmark:end -->" in out
    assert "<!--" not in out
    assert "\n\n\\# x\n\n" in out
    assert "\n\n\\- y\n\n" in out
    assert "\n\n\\1. z\n\n" in out
    assert "\n\n\\---\n\n" in out
    assert "\n\n\\> q\n\n" in out
    assert "- **&lt;script>alert(1)&lt;/script>**: &lt;i>ctx" in out


def test_ordinary_text_not_escaped() -> None:
    out = render_analysis(make(prompt=None, summary="-5 degrees is cold. 3.5 is fine."))
    assert "\n-5 degrees" in out
    assert "\\" not in out


def test_long_prompt_heading_capped() -> None:
    prompt = "word " * 40
    out = render_analysis(make(prompt=prompt))
    heading = out.splitlines()[0].removeprefix("## Analysis: ")
    assert len(heading) == 80
    assert heading.endswith("…")
    assert "> " + prompt.strip() in out


def test_prompt_quote_keeps_blank_lines() -> None:
    out = render_analysis(make(prompt="a\n\nb"))
    assert "\n\n> a\n>\n> b\n\n" in out


def test_output_ends_with_one_newline() -> None:
    out = render_analysis(make())
    assert out.endswith("\n")
    assert not out.endswith("\n\n")


def _sample() -> Analysis:
    return Analysis(
        episode_key=KEY,
        prompt=None,
        prompt_hash=prompt_hash(None, "m"),
        model="m",
        schema_version=ANALYSIS_SCHEMA_VERSION,
        created_at=T0,
        summary="S.",
        people=(Entity(name="Ada", context="ctx", timestamps_s=(5.0,)),),
        topics=(Entity(name="Memory"),),
    )


def test_entity_link_hook() -> None:
    calls: list[tuple[str, str]] = []

    def linker(field: str, entity: Entity) -> str | None:
        calls.append((field, entity.name))
        return f"[[P/{entity.name}|{entity.name}]]" if field == "people" else None

    text = render_analysis(_sample(), entity_link=linker)
    assert "- **[[P/Ada|Ada]]**: ctx [00:00:05]" in text
    assert "- **Memory**\n" in text
    assert calls == [("people", "Ada"), ("topics", "Memory")]


def test_no_linker_unchanged() -> None:
    assert render_analysis(_sample()) == render_analysis(_sample(), entity_link=None)
    assert "- **Ada**: ctx [00:00:05]" in render_analysis(_sample())


def test_escape_block_start() -> None:
    from earmark.analyze.render import escape_block_start

    for text in ("- a", "> a", "# a", "1. a", "---", "+ a", "* a", "= ", "2) a"):
        assert escape_block_start(text) == "\\" + text, text
    for text in ("plain", "a - b", "-a", "1.5 a", ""):
        assert escape_block_start(text) == text, text
