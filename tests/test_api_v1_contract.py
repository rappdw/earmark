"""contract/api-v1.md: pinned by hash, and every example is a real response of the app."""

import hashlib
import json
import re

import pytest

from earmark.api import upload
from earmark.api.app import STORE_UNAVAILABLE_DETAIL
from earmark.api.auth import UNAUTHORIZED_DETAIL
from earmark.fetch import MIB, limit_text
from earmark.models import (
    MAX_INITIAL_PROMPT_CHARS,
    MAX_SUMMARY_INSTRUCTIONS_CHARS,
    WHISPER_LANGUAGES,
)
from earmark.summarize.base import CONTEXT_PREFIX, NOT_CONFIGURED
from earmark.transcripts import JOB_NOT_FOUND, NOT_YET, NOT_YET_AT_STAGE, SKIPPED
from earmark.v1_jobs import (
    DELETE_FAILED,
    NOT_A_SUMMARY,
    NOT_A_TRANSCRIPTION,
    PURGED,
    STILL_ACTIVE,
    SUMMARIES_OFF,
    SUMMARY_BODY_TOO_LARGE,
    SUMMARY_DELETE_FAILED,
    SUMMARY_FAILED,
    SUMMARY_GONE,
    SUMMARY_MISSING_BODY,
    SUMMARY_NOT_YET,
    SUMMARY_NOT_YET_AT_STAGE,
    SUMMARY_ONLY_JSON,
    SUMMARY_PURGED,
    SUMMARY_SKIPPED,
    SUMMARY_UNREADABLE,
    TRANSCRIPT_TOO_LONG,
    V1_FAILED,
    V1_GONE,
    V1_ONLY_MULTIPART,
    V1_UNREADABLE,
)
from tests.api_v1_examples import EXAMPLE_NAMES, STEP36_EXAMPLES, build_examples
from tests.doc_helpers import between_markers, read_doc
from tests.test_docs import _assert_placeholders

# Version 1.0 (step 36) was 6823155ddaee3f08f01abb72735a7f3390f9a987a8ab619cf891dc9f3fab419d.
API_V1_SHA256 = "89126dfad44ce1aa159548aa9226c3edac474f3c23c37d74c0afddb155ce9669"


def _block() -> str:
    return between_markers(read_doc("contract/api-v1.md"), "api-v1").strip("\n") + "\n"


def test_api_v1_contract_is_pinned() -> None:
    assert hashlib.sha256(_block().encode("utf-8")).hexdigest() == API_V1_SHA256


def _example(name: str) -> tuple[str, str]:
    pattern = rf"<!-- api-v1-example:{re.escape(name)} -->\n\n```(\w+)\n(.*?)\n```"
    found = re.search(pattern, _block(), re.DOTALL)
    assert found is not None, name
    return found[1], found[2]


@pytest.fixture(scope="module")
def examples(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    monkeypatch = pytest.MonkeyPatch()
    try:
        return build_examples(tmp_path_factory.mktemp("examples"), monkeypatch)
    finally:
        monkeypatch.undo()


@pytest.mark.parametrize("name", EXAMPLE_NAMES)
def test_api_v1_example_matches(name: str, examples: dict[str, str]) -> None:
    kind, body = _example(name)
    if name == "transcript-text":
        assert kind == "text"
        assert body + "\n" == examples[name]
    else:
        assert kind == "json"
        assert json.loads(body) == json.loads(examples[name])


def test_api_v1_examples_are_all_in_the_contract() -> None:
    named = set(re.findall(r"<!-- api-v1-example:([a-z-]+) -->", _block()))
    assert named == set(EXAMPLE_NAMES)


def _table_details() -> list[tuple[int, str]]:
    rows = re.findall(r"^\| (\d{3}) \| `(.+?)` \|", _block(), re.MULTILINE)
    return [(int(status), detail) for status, detail in rows]


def _code_messages() -> set[str]:
    allowed = "file, initial_prompt, language"
    limit = limit_text(1024 * MIB)
    too_large = upload.too_large(1024 * MIB).detail.replace(limit, "<limit>")
    return {
        UNAUTHORIZED_DETAIL,
        JOB_NOT_FOUND,
        NOT_YET.format(status="<status>"),
        NOT_YET_AT_STAGE.format(status="<status>", stage="<stage>"),
        V1_FAILED,
        SKIPPED,
        STILL_ACTIVE.format(status="<status>"),
        PURGED,
        V1_GONE,
        too_large,
        V1_ONLY_MULTIPART,
        upload.NOT_AUDIO,
        upload.UNKNOWN_FIELD.format(name="<name>", allowed=allowed),
        upload.ONE_FILE,
        upload.EMPTY_FILE,
        upload.MALFORMED,
        upload.NO_BOUNDARY,
        upload.INCOMPLETE,
        upload.TOO_MANY_PARTS,
        upload.FIELD_REPEATED.format(name="<name>"),
        upload.FIELD_NOT_TEXT.format(name="<name>"),
        upload.FIELD_TOO_LONG.format(name="<name>"),
        upload.FIELD_NOT_UTF8.format(name="<name>"),
        "initial_prompt is too long (at most 1000 characters)",
        "language must be a Whisper language code such as 'en'; "
        "leave it out to detect the language",
        "format: Input should be 'json' or 'text'",
        V1_UNREADABLE,
        DELETE_FAILED,
        STORE_UNAVAILABLE_DETAIL,
        SUMMARIES_OFF,
        SUMMARY_ONLY_JSON,
        SUMMARY_BODY_TOO_LARGE.format(limit="<limit>"),
        SUMMARY_MISSING_BODY,
        TRANSCRIPT_TOO_LONG.format(n="<n>"),
        SUMMARY_NOT_YET.format(status="<status>"),
        SUMMARY_NOT_YET_AT_STAGE.format(status="<status>", stage="<stage>"),
        SUMMARY_FAILED,
        SUMMARY_SKIPPED,
        NOT_A_SUMMARY,
        NOT_A_TRANSCRIPTION,
        SUMMARY_PURGED,
        SUMMARY_GONE,
        SUMMARY_UNREADABLE,
        SUMMARY_DELETE_FAILED,
        "transcript must not be empty",
        "instructions must not be empty",
        "instructions is too long (at most 20000 characters)",
        "unknown field '<name>' (allowed: transcript, instructions)",
        "transcript is required",
        "instructions is required",
        "request body is not valid JSON",
        "request body must be a JSON object sent with Content-Type: application/json",
    }


def test_api_v1_error_table_matches() -> None:
    table = {detail for _, detail in _table_details()}
    messages = _code_messages()
    assert table == messages
    assert len(_table_details()) == len(messages)  # each message once


def test_api_v1_error_statuses() -> None:
    by_detail = {detail: status for status, detail in _table_details()}
    assert by_detail[JOB_NOT_FOUND] == 404
    assert by_detail[PURGED] == 410
    assert by_detail[V1_GONE] == 410
    assert by_detail[V1_UNREADABLE] == 500
    assert by_detail[V1_ONLY_MULTIPART] == 415
    assert by_detail[STILL_ACTIVE.format(status="<status>")] == 409
    assert by_detail[UNAUTHORIZED_DETAIL] == 401
    assert by_detail[SUMMARIES_OFF] == 503
    assert by_detail[SUMMARY_ONLY_JSON] == 415
    assert by_detail[SUMMARY_BODY_TOO_LARGE.format(limit="<limit>")] == 413
    assert by_detail[SUMMARY_MISSING_BODY] == 422
    assert by_detail[TRANSCRIPT_TOO_LONG.format(n="<n>")] == 422
    assert by_detail[SUMMARY_NOT_YET.format(status="<status>")] == 409
    assert by_detail[NOT_A_SUMMARY] == 409
    assert by_detail[NOT_A_TRANSCRIPTION] == 409
    assert by_detail[SUMMARY_PURGED] == 410
    assert by_detail[SUMMARY_GONE] == 410
    assert by_detail[SUMMARY_UNREADABLE] == 500
    assert by_detail[SUMMARY_DELETE_FAILED] == 500


def test_api_v1_languages_listed() -> None:
    found = re.search(r"<!-- api-v1-languages -->\n\n```text\n(.*?)\n```", _block(), re.DOTALL)
    assert found is not None
    assert tuple(found[1].split()) == WHISPER_LANGUAGES


def test_api_v1_cap_stated() -> None:
    block = _block()
    assert MAX_INITIAL_PROMPT_CHARS == 1000
    assert "1000 characters" in block
    assert "223 tokens" in block
    assert "EARMARK_MAX_UPLOAD_MB" in block
    assert "600 s" in block
    assert MAX_SUMMARY_INSTRUCTIONS_CHARS == 20_000
    for needle in (
        "20000 characters",
        "400000 characters",
        "EARMARK_LLM_MAX_TRANSCRIPT_CHARS",
        "EARMARK_LLM_BACKEND",
    ):
        assert needle in block, needle


def test_api_v1_contract_placeholders_only() -> None:
    _assert_placeholders(read_doc("contract/api-v1.md"), "contract/api-v1.md")


def _section(number: int) -> str:
    block = _block()
    start = block.index(f"\n## {number}. ")
    following = re.search(rf"\n## {number + 1}\. ", block)
    assert following is not None
    return block[start : following.start()]


def test_api_v1_summary_failures_stated() -> None:
    text = re.sub(r"\s+", " ", _section(8))
    assert CONTEXT_PREFIX in text
    assert "the summary was cut off" in text
    assert NOT_CONFIGURED in text
    assert "ever stored or served" in text


def test_api_v1_revisions_listed() -> None:
    text = re.sub(r"\s+", " ", _block().split("\n## 13. ")[1])
    assert "**1.0** (step 36" in text
    assert "**1.1** (step 37" in text
    assert "Additive" in text


def test_api_v1_sections_are_numbered_in_order() -> None:
    numbers = [int(n) for n in re.findall(r"^## (\d+)\. ", _block(), re.MULTILINE)]
    assert numbers == list(range(1, 14))


def test_api_v1_step36_examples_still_hold(examples: dict[str, str]) -> None:
    assert set(STEP36_EXAMPLES) == {
        "accepted",
        "job-queued",
        "transcript-json",
        "error",
        "job-done",
        "transcript-text",
        "deleted",
    }
    for name, frozen in STEP36_EXAMPLES.items():
        current = examples[name]
        if name == "transcript-text":
            assert current == frozen
            continue
        old, new = frozen, json.loads(current)
        assert set(new) - set(old) <= {"kind"}, name
        assert {k: new[k] for k in old} == old, name
