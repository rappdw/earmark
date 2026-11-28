"""contract/api-v1.md: pinned by hash, and every example is a real response of the app."""

import hashlib
import json
import re

import pytest

from earmark.api import upload
from earmark.api.app import STORE_UNAVAILABLE_DETAIL
from earmark.api.auth import UNAUTHORIZED_DETAIL
from earmark.fetch import MIB, limit_text
from earmark.models import MAX_INITIAL_PROMPT_CHARS, WHISPER_LANGUAGES
from earmark.transcripts import JOB_NOT_FOUND, NOT_YET, NOT_YET_AT_STAGE, SKIPPED
from earmark.v1_jobs import (
    DELETE_FAILED,
    PURGED,
    STILL_ACTIVE,
    V1_FAILED,
    V1_GONE,
    V1_ONLY_MULTIPART,
    V1_UNREADABLE,
)
from tests.api_v1_examples import EXAMPLE_NAMES, build_examples
from tests.doc_helpers import between_markers, read_doc
from tests.test_docs import _assert_placeholders

API_V1_SHA256 = "6823155ddaee3f08f01abb72735a7f3390f9a987a8ab619cf891dc9f3fab419d"


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


def test_api_v1_contract_placeholders_only() -> None:
    _assert_placeholders(read_doc("contract/api-v1.md"), "contract/api-v1.md")
