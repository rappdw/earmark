"""The README, Shortcut guide, deployment guide and runbook are tested against the code."""

import hashlib
import itertools
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import SecretStr

from earmark import sources
from earmark.api.app import STORE_UNAVAILABLE_DETAIL, create_app
from earmark.config import Settings, env_var
from earmark.db import open_database
from earmark.jobs import JobStore
from earmark.models import FORM_FIELDS, TranscriptionRequest, UploadFields
from earmark.recipients import RecipientPolicy
from earmark.resolve.apple import UnsupportedUrlError, parse_apple_url
from earmark.staging import Staging
from earmark.submission import Intake
from tests.doc_helpers import (
    ROOT,
    LayoutEntry,
    after_marker,
    anchored_links,
    between_markers,
    fences,
    first_fence,
    heading_anchors,
    layout_entries,
    paragraphs,
    plan_status,
    read_doc,
    relative_links,
    section,
    step_numbers,
    table_rows,
    table_variables,
    without_blocks,
)
from tests.multipart_helpers import Part, audio, content_type, multipart_body

TOKEN = "docs-test-token-91c"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PLANS = [
    "docs/steps/27-docs-runbook-shortcut.md",
    "docs/steps/28-system-scope.md",
    "docs/steps/29-mail-contract-seam.md",
    "docs/steps/30-pka-receiving-skill.md",
]
CONTRACT_DOCS = [
    "contract/README.md",
    "contract/mail-v1.md",
    "contract/samples/v1/README.md",
]
CONTRACT_V1_SHA256 = "7361f3d0fb20fa3edb0c5b98529c0c998532ba035f53ac8392fb47aea9dcd82d"
ARCH = "docs/ARCHITECTURE.md"
DOCS = [
    "README.md",
    "docs/deploy.md",
    "docs/shortcut.md",
    "docs/runbook-spark.md",
    "docs/asr.md",
]
SECRET_FIELDS = ("api_token", "asr_api_key", "ntfy_url", "ntfy_token", "smtp_password")
ACTIONS = [
    "Receive input from Share Sheet",
    "Choose from Menu",
    "Ask for Input",
    "Format Date",
    "Get Type",
    "If",
    "Get Contents of URL",
    "Otherwise",
    "Get URLs from Input",
    "Get Item from List",
    "Get Contents of URL",
    "End If",
    "Show Result",
]
ISO_8601 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")


def _quickstart() -> str:
    block = between_markers(read_doc("README.md"), "quickstart")
    return first_fence(block, "bash").body


def _allowlist() -> tuple[str, ...]:
    text = after_marker(read_doc("docs/deploy.md"), "allowlist-example")
    [line] = first_fence(text, "bash").body.splitlines()
    name, _, value = line.partition("=")
    assert name == "EARMARK_ALLOWED_RECIPIENTS"
    return tuple(a.strip() for a in value.split(","))


ALLOWLIST = _allowlist()


def _system() -> str:
    return section(read_doc(ARCH), "## System: ")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _layout() -> list[LayoutEntry]:
    return layout_entries(first_fence(after_marker(_system(), "repo-layout"), "text").body)


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "api_token": SecretStr(TOKEN),
        "data_dir": tmp_path / "data",
        "allowed_recipients": (*ALLOWLIST, "you@example.org"),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _post(
    body: bytes,
    tmp_path: Path,
    content_type: str = "application/json",
    *,
    settings_overrides: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    max_upload_bytes: int | None = None,
) -> tuple[int, dict[str, Any], JobStore]:
    settings = _settings(tmp_path, **(settings_overrides or {}))
    store = JobStore(open_database(tmp_path / "earmark.db"))
    app = create_app(settings, store=store, max_upload_bytes=max_upload_bytes)
    with TestClient(app) as client:
        response = client.post(
            "/transcriptions",
            content=body,
            headers={"Content-Type": content_type, **(AUTH if headers is None else headers)},
        )
    return response.status_code, response.json(), store


# --- README quickstart --------------------------------------------------------------------


def test_quickstart_block_present() -> None:
    block = between_markers(read_doc("README.md"), "quickstart")
    found = fences(block)
    assert [f.info for f in found] == ["bash"]


def test_quickstart_uses_fake_backends() -> None:
    script = _quickstart()
    fields = [n for n in Settings.model_fields if n.endswith("_backend")]
    assert len(fields) == 5
    for name in fields:
        assert re.search(rf"\b{env_var(name)}=fake\b", script), name


def test_quickstart_exports_only_settings() -> None:
    known = {env_var(name) for name in Settings.model_fields}
    assigned = re.findall(r"\b(EARMARK_[A-Z0-9_]+)=", _quickstart())
    assert assigned
    assert sorted(set(assigned) - known) == []


def test_quickstart_commands_exist() -> None:
    from earmark.cli import app

    registered = {c.name for c in app.registered_commands if c.name} | {"--version"}
    used = re.findall(r"\buv run earmark (\S+)", _quickstart())
    assert {"doctor", "serve", "worker", "jobs"} <= set(used)
    assert sorted(set(used) - registered) == []


def _quickstart_body() -> str:
    match = re.search(r"-d '(?P<body>\{.*\})'", _quickstart())
    assert match
    return match["body"]


def test_quickstart_request_body_valid() -> None:
    body = _quickstart_body()
    request = TranscriptionRequest.model_validate_json(body)
    assert set(json.loads(body)) == {"url", "to", "instructions", "timestamp"}
    assert request.instructions
    assert request.timestamp is not None
    assert request.timestamp.utcoffset() is not None
    assert parse_apple_url(request.url).url.startswith("https://podcasts.apple.com/")
    assert "/transcriptions" in _quickstart()
    assert "/ingest" not in _quickstart()


def _exported_allowlist() -> tuple[str, ...]:
    export = re.search(
        r"^export EARMARK_ALLOWED_RECIPIENTS=(?P<value>\S+)$", _quickstart(), re.MULTILINE
    )
    assert export
    return (export["value"],)


def test_quickstart_upload_valid() -> None:
    script = _quickstart().replace("\\\n", " ")
    [upload] = [line for line in script.splitlines() if " -F " in line or "\t-F " in line]
    words = shlex.split(upload)
    fields: dict[str, str] = {}
    for flag, value in itertools.pairwise(words):
        if flag == "--form-string":
            name, _, text = value.partition("=")
            fields[name] = text
    assert set(fields) <= set(FORM_FIELDS)
    UploadFields.model_validate(fields)
    settings = Settings(_env_file=None, allowed_recipients=_exported_allowlist())
    assert settings.recipient_policy().allows(fields["to"])
    [file_arg] = [w for w in words if w.startswith("file=@")]
    assert sources.upload_extension(Path(file_arg.removeprefix("file=@")).name, None) is not None


def test_quickstart_exports_allowed_recipients() -> None:
    settings = Settings(_env_file=None, allowed_recipients=_exported_allowlist())
    assert settings.recipient_policy().allows(json.loads(_quickstart_body())["to"])


def test_quickstart_reads_outbox() -> None:
    script = _quickstart()
    assert re.search(r"\bEARMARK_MAIL_BACKEND=fake\b", script)
    assert "outbox" in script
    assert ".eml" in script
    posts = script.count("http://127.0.0.1:8787/transcriptions")
    assert posts == 2
    assert script.count("worker --once") == posts


def test_quickstart_runs_no_network_command() -> None:
    script = _quickstart()
    assert "git clone" not in script
    assert "--extra" not in script
    for url in re.findall(r"https?://[^\s'\"]+", script):
        assert url.startswith(("http://127.0.0.1:8787/", "https://podcasts.apple.com/")), url


# --- README settings reference ------------------------------------------------------------


def test_readme_settings_reference_covers_every_field() -> None:
    listed = table_variables(between_markers(read_doc("README.md"), "settings"))
    expected = {env_var(f) for f in Settings.model_fields}
    duplicates = sorted({n for n in listed if listed.count(n) > 1})
    missing = sorted(expected - set(listed))
    extra = sorted(set(listed) - expected)
    assert not duplicates, f"listed twice: {duplicates}"
    assert not missing, f"missing from the README: {missing}"
    assert not extra, f"not Settings fields: {extra}"


def test_readme_settings_reference_marks_secrets() -> None:
    block = between_markers(read_doc("README.md"), "settings")
    for name in SECRET_FIELDS:
        [row] = [ln for ln in block.splitlines() if ln.startswith(f"| `{env_var(name)}` |")]
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert cells[1] == "unset", name
        assert "secret" in cells[2].lower(), name


# --- docs/shortcut.md ---------------------------------------------------------------------


def _request_example() -> str:
    return first_fence(after_marker(read_doc("docs/shortcut.md"), "request-example"), "json").body


def _response_example() -> str:
    text = after_marker(read_doc("docs/shortcut.md"), "response-example")
    return first_fence(text, "json").body


def _upload_table() -> list[list[str]]:
    block = between_markers(read_doc("docs/shortcut.md"), "upload-fields")
    return [[c.strip("`") for c in row] for row in table_rows(block)]


def test_shortcut_request_example_validates() -> None:
    body = _request_example()
    request = TranscriptionRequest.model_validate_json(body)
    example = json.loads(body)
    assert set(example) == {"url", "to", "instructions", "timestamp"}
    assert request.instructions
    parse_apple_url(request.url)
    assert ISO_8601.match(example["timestamp"])


def test_shortcut_request_example_accepted_by_api(tmp_path: Path) -> None:
    example = json.loads(_request_example())
    status, payload, store = _post(_request_example().encode(), tmp_path)
    assert status == 202
    [job] = store.list_recent()
    assert job.status == "queued"
    assert job.url == parse_apple_url(example["url"]).url
    assert job.to == example["to"]
    assert job.instructions == example["instructions"]
    assert job.utc_offset_min == -480
    assert payload["job_id"] == job.id


def test_shortcut_response_example_matches_api(tmp_path: Path) -> None:
    _, payload, _ = _post(_request_example().encode(), tmp_path)
    example = json.loads(_response_example())
    assert set(example) == set(payload)
    assert example["status"] == payload["status"]
    assert example["created"] is payload["created"]
    assert re.fullmatch(r"[0-9a-f]{32}", example["job_id"])


def test_shortcut_upload_fields_match_api() -> None:
    rows = _upload_table()
    assert [r[0] for r in rows] == ["file", "to", "instructions", "timestamp"]
    assert all(r[0] in ("file", *FORM_FIELDS) for r in rows)
    UploadFields.model_validate({r[0]: r[3] for r in rows if r[0] != "file"})


@pytest.mark.parametrize("file_first", [True, False], ids=["file_first", "file_last"])
def test_shortcut_upload_example_accepted_by_api(tmp_path: Path, file_first: bool) -> None:
    rows = {r[0]: r[3] for r in _upload_table()}
    file_part = Part("file", audio(), filename=rows["file"])
    text = [Part(name, value) for name, value in rows.items() if name != "file"]
    parts = [file_part, *text] if file_first else [*text, file_part]
    status, _, store = _post(multipart_body(parts), tmp_path, content_type())
    assert status == 202
    [job] = store.list_recent()
    assert job.source == "upload"
    assert job.upload_name == "Voice memo.m4a"
    assert job.to == rows["to"]
    assert job.instructions == rows["instructions"]
    assert job.utc_offset_min == -480
    assert len(list((tmp_path / "data" / "uploads").iterdir())) == 1


def _action_items() -> list[str]:
    block = between_markers(read_doc("docs/shortcut.md"), "shortcut-actions")
    items = re.split(r"^\d+\. ", block, flags=re.MULTILINE)[1:]
    assert items
    return [" ".join(item.split()) for item in items]


def _item(number: int) -> str:
    return _action_items()[number - 1]


def test_shortcut_actions_in_order() -> None:
    names = [re.match(r"\*\*(.+?)\*\*", item) for item in _action_items()]
    assert [m[1] for m in names if m] == ACTIONS


def test_shortcut_menu_fills_to() -> None:
    menu = _item(2)
    for word in ("Personal", "Work", "Set Variable", "`Recipient`"):
        assert word in menu, word
    addresses = re.findall(r"`([^`@\s]+@[^`\s]+)`", menu)
    assert len(addresses) == 2
    assert set(addresses) <= set(ALLOWLIST)
    assert json.loads(_request_example())["to"] in addresses
    assert {r[0]: r[3] for r in _upload_table()}["to"] in addresses


def test_shortcut_ask_for_input_optional() -> None:
    ask = _item(3)
    assert "optional" in ask.lower()
    assert "Provided Input" in ask


def test_shortcut_formats_timestamp() -> None:
    date = _item(4)
    for word in ("Current Date", "ISO 8601", "Include ISO 8601 Time"):
        assert word in date, word


def test_shortcut_branches_on_type() -> None:
    assert "Shortcut Input" in _item(5)
    assert "`File`" in _item(6)


def test_shortcut_post_actions_send_fields() -> None:
    form, as_json = _item(7), _item(11)
    for item in (form, as_json):
        assert "/transcriptions" in item
        assert "POST" in item
        for word in ("Recipient", "Provided Input", "Formatted Date", "`to`", "`instructions`"):
            assert word in item, word
        assert "`timestamp`" in item
    assert "Form" in form
    assert "`file`" in form
    assert "JSON" in as_json
    assert "`url`" in as_json
    assert not any("/ingest" in item for item in _action_items())


def test_shortcut_examples_match_actions() -> None:
    def names(item: str) -> set[str]:
        return set(re.findall(r"`(\w+)` (?:\([^)]*\) )?= ", item)) - {"Authorization"}

    assert names(_item(11)) == set(json.loads(_request_example()))
    assert names(_item(7)) == {r[0] for r in _upload_table()}


def test_shortcut_sends_only_bearer_header() -> None:
    for item in (_item(7), _item(11)):
        assert "`Authorization` is the only header to add" in item
        assert "Content-Type" in item  # sent by Shortcuts itself, not added by the user
    assert "Bearer" in _item(7)


ERROR_ROWS = [
    (401, "missing or invalid API token"),
    (413, "request body is larger than 64 KiB"),
    (413, "the upload is larger than the <limit> limit (EARMARK_MAX_UPLOAD_MB)"),
    (
        415,
        "send JSON (Content-Type: application/json) or a multipart form "
        "(Content-Type: multipart/form-data)",
    ),
    (415, "the file does not look like audio: <…>"),
    (422, "That is not a valid link. Share an episode from Apple Podcasts."),
    (422, "This links to a whole show. Share a single episode instead."),
    (422, "This Apple link is not a podcast episode."),
    (422, "to must be one email address"),
    (422, "to is not an allowed recipient (EARMARK_ALLOWED_RECIPIENTS)"),
    (422, "no recipients are allowed yet: set EARMARK_ALLOWED_RECIPIENTS on the server"),
    (422, "unknown field 'x' (allowed: url, to, instructions, title, timestamp)"),
    (422, "request body is not valid JSON"),
    (422, "the form must have exactly one file field named 'file'"),
    (503, STORE_UNAVAILABLE_DETAIL),
]


def _error_table() -> list[tuple[int, str]]:
    block = section(read_doc("docs/shortcut.md"), "## 4. ")
    rows = [r for r in table_rows(block) if len(r) == 3]
    return [(int(r[0]), r[1].strip("`")) for r in rows]


def _placeholder_pattern(message: str) -> re.Pattern[str]:
    parts = re.split(r"<[^>]+>", message)
    return re.compile(".+".join(re.escape(p) for p in parts), re.DOTALL)


def _json(**fields: Any) -> bytes:
    return json.dumps({"to": "you@example.org", **fields}).encode()


def _episode(url: str) -> bytes:
    return _json(url=url)


def _error_triggers(tmp_path: Path) -> list[tuple[int, dict[str, Any]]]:
    tiny = 1024
    upload = multipart_body(
        [Part("file", audio(2 * tiny), filename="memo.m4a"), Part("to", "you@example.org")]
    )
    text_file = multipart_body(
        [
            Part("file", b"words", filename="notes.txt", content_type="text/plain"),
            Part("to", "you@example.org"),
        ]
    )
    no_file = multipart_body([Part("to", "you@example.org")])
    show = "https://podcasts.apple.com/us/podcast/id1200361736"
    good = "https://podcasts.apple.com/us/podcast/id1200361736?i=1000802907981"
    cases: list[tuple[bytes, str, dict[str, Any]]] = [
        (_episode(good), "application/json", {"headers": {}}),
        (b'{"pad": "' + b"a" * 65536 + b'"}', "application/json", {}),
        (upload, content_type(), {"max_upload_bytes": tiny}),
        (b"to=x", "application/x-www-form-urlencoded", {}),
        (text_file, content_type(), {}),
        (_episode("hello"), "application/json", {}),
        (_episode(show), "application/json", {}),
        (_episode("https://www.apple.com/podcasts/"), "application/json", {}),
        (_json(url=good, to="not an address"), "application/json", {}),
        (_json(url=good, to="stranger@example.com"), "application/json", {}),
        (_episode(good), "application/json", {"settings_overrides": {"allowed_recipients": ()}}),
        (_json(url=good, x=1), "application/json", {}),
        (b"{", "application/json", {}),
        (no_file, content_type(), {}),
    ]
    results: list[tuple[int, dict[str, Any]]] = []
    for index, (body, ctype, extra) in enumerate(cases):
        status, payload, _ = _post(body, tmp_path / str(index), ctype, **extra)
        results.append((status, payload))
    return results


def test_shortcut_error_table_matches_api(tmp_path: Path) -> None:
    assert _error_table() == ERROR_ROWS
    answers = _error_triggers(tmp_path)
    assert len(answers) == len(ERROR_ROWS) - 1  # the 503 is compared with the constant
    for (status, message), (got_status, payload) in zip(ERROR_ROWS, answers, strict=False):
        assert got_status == status, message
        assert _placeholder_pattern(message).fullmatch(payload["detail"]), (message, payload)
    assert ERROR_ROWS[-1] == (503, STORE_UNAVAILABLE_DETAIL)


# --- shared checks over the documents -----------------------------------------------------


@pytest.mark.parametrize("doc", DOCS)
def test_doc_relative_links_resolve(doc: str) -> None:
    base = (ROOT / doc).parent
    links = relative_links(read_doc(doc))
    assert links, doc
    for target in links:
        assert (base / target).exists(), f"{doc} links to missing {target}"


@pytest.mark.parametrize("doc", DOCS)
def test_doc_anchors_resolve(doc: str) -> None:
    text = read_doc(doc)
    for path, anchor in anchored_links(text):
        target = text if not path else (ROOT / doc).parent.joinpath(path).read_text("utf-8")
        assert anchor in heading_anchors(target), f"{doc} links to missing {path}#{anchor}"


def test_no_cloudflare_tunnel_or_access_headers() -> None:
    for doc in DOCS:
        text = read_doc(doc).lower()
        for banned in ("cf-access", "cloudflared", "cloudflare tunnel"):
            assert banned not in text, f"{doc} mentions {banned}"


REMOVED = [
    r"\bvault\b",
    r"obsidian",
    r"/ingest\b",
    r"reanalyze",
    r"\bdigest",
    r"\bentit(y|ies)\b",
    r"lancedb",
    r"fastembed",
    r"embedding",
    r"EARMARK_(LLM|VAULT|OBSIDIAN|STORE|INDEX|EMBED|TZ)",
    r"anthropic",
    r"\bclaude\b",
    r"asr:8000",
    r"COMPOSE_PROFILES",
    r"profile gpu",
    r"gpu profile",
    r"GET /episodes",
    r"/search\b",
    r"earmark (episodes|search|reindex|analy[sz]e)",
    r"--prompt",
    r'"prompt"',
    r"Note ready",
    r"--extra index",
]


# The plugin tool's own command and settings file, named only in the runbook's phase 11.
PLUGIN_CLI_TERMS = ("claude plugin ", ".claude/settings.json")
RUNBOOK = "docs/runbook-spark.md"


def _phase_11_bounds(text: str) -> tuple[int, int]:
    start, end = text.index("## Phase 11:"), text.index("## Phase 12:")
    assert start < end
    return start, end


def _without_plugin_cli_terms(text: str) -> str:
    start, end = _phase_11_bounds(text)
    phase = text[start:end]
    for term in PLUGIN_CLI_TERMS:
        phase = phase.replace(term, "")
    return text[:start] + phase + text[end:]


def _removed_hits(doc: str, text: str) -> list[str]:
    text = without_blocks(text, "upgrade-v0.1.0")
    if doc == RUNBOOK:
        text = _without_plugin_cli_terms(text)
    patterns = REMOVED if doc == "docs/asr.md" else [*REMOVED, r"\bvllm\b"]
    return [m[0] for pattern in patterns for m in re.finditer(pattern, text, re.IGNORECASE)]


@pytest.mark.parametrize("doc", DOCS)
def test_docs_describe_no_removed_behaviour(doc: str) -> None:
    hits = _removed_hits(doc, read_doc(doc))
    assert not hits, f"{doc} mentions removed behaviour: {hits[:3]!r}"


def test_removed_scan_exception_is_narrow() -> None:
    text = read_doc(RUNBOOK)
    assert _removed_hits(RUNBOOK, text) == []
    start, end = _phase_11_bounds(text)
    inside = text[:start] + text[start:end] + "\nThe Claude assistant.\n" + text[end:]
    assert _removed_hits(RUNBOOK, inside)
    for term in PLUGIN_CLI_TERMS:
        outside = text[:start] + "\nRun " + term + "list here.\n" + text[start:]
        assert _removed_hits(RUNBOOK, outside), term
        assert _removed_hits(
            RUNBOOK, text[:start] + text[start:end].replace(term, "claude ") + text[end:]
        )


def test_plugin_cli_terms_only_in_phase_11() -> None:
    for doc in DOCS:
        text = read_doc(doc)
        for term in PLUGIN_CLI_TERMS:
            if doc != RUNBOOK:
                assert term not in text, (doc, term)
                continue
            start, end = _phase_11_bounds(text)
            assert term in text[start:end], term
            assert term not in text[:start] + text[end:], term


EMAIL = re.compile(r"[\w.+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
IPV4 = re.compile(r"\b(\d{1,3}\.){3}\d{1,3}\b")
SECRET_LINE = re.compile(
    r"^(export )?EARMARK_(API_TOKEN|SMTP_PASSWORD|NTFY_TOKEN|ASR_API_KEY)=(?P<value>.*)$"
)


def _assert_placeholders(text: str, label: str) -> None:
    for match in EMAIL.finditer(text):
        domain = match[0].rsplit("@", 1)[1].lower()
        assert domain in {"example.org", "example.net", "example.com"}, (label, match[0])
    # "0.0.0.0" is the documented example bind address, not a bind made here.
    allowed_ips = {"127.0.0.1", "0.0.0.0", "1.1.1.1", "192.168.1.10", "192.168.1.0"}  # noqa: S104
    for match in IPV4.finditer(text):
        assert match[0] in allowed_ips, (label, match[0])
    for host in re.findall(r"[\w.-]+\.ts\.net\b", text):
        assert host.endswith("tailnet-name.ts.net"), host
    assert re.search(r"tk_[A-Za-z0-9]{8,}", text) is None
    for fence in fences(text):
        for line in fence.body.splitlines():
            secret = SECRET_LINE.match(line.strip())
            if secret:
                value = secret["value"].strip("'\"")
                assert value in ("", "demo-token") or "<" in value, line


@pytest.mark.parametrize("doc", [*DOCS, *PLANS, "CLAUDE.md"])
def test_docs_use_placeholders_only(doc: str) -> None:
    _assert_placeholders(read_doc(doc), doc)


def test_architecture_system_section_uses_placeholders() -> None:
    _assert_placeholders(_system(), "ARCHITECTURE System")


def test_deploy_doc_states_test_status() -> None:
    text = read_doc("docs/deploy.md")
    head = text.split("\n## ")[1]  # the first section, "What is tested"
    flat = _flat(head).lower().replace("`", "")
    for needle in ("receiving skill", "never run in a pka", "phase 12"):
        assert needle in flat, needle
    assert "build-tested in ci" in flat
    assert "not run-tested" in flat
    assert "gpu image is untested" in flat
    assert "only real check" in flat


def test_deploy_doc_gpu_smoke() -> None:
    smoke = between_markers(read_doc("docs/deploy.md"), "gpu-smoke")
    for needle in (
        "docker compose build worker",
        "get_cuda_device_count",
        "ctranslate2.__file__",
        "command -v uv",
        "worker doctor",
        "transcribe",
        "float16",
        "63a4b1e4c1dc655ac70961ffbf518acd249df237e5a0152faae9a4a836949715",
    ):
        assert needle in smoke, needle
    assert "the only real check" in read_doc("docs/deploy.md").lower()
    assert "CT2_VERSION=v4.6.0" in read_doc("docs/deploy.md")


def test_deploy_doc_has_no_gpu_profile() -> None:
    text = read_doc("docs/deploy.md")
    for banned in ("--profile gpu", "COMPOSE_PROFILES", "ASR_GPU_MEMORY_UTILIZATION"):
        assert banned not in without_blocks(text, "upgrade-v0.1.0"), banned


def test_deploy_doc_reachability() -> None:
    text = read_doc("docs/deploy.md")
    for phrase in ("DNS only", "AllowedIPs", "rebind-domain-ok", "private-domain"):
        assert phrase in text, phrase
    assert "Zone:Read" in text or "Zone → Zone → Read" in text
    assert re.search(r"[Nn]ever use `tailscale funnel`", text)
    reach = section(text, "## 6. ")
    assert "/transcriptions" in reach
    assert "/ingest" not in reach
    assert "### 6.3 " in reach
    assert "EARMARK_MAX_UPLOAD_MB" in section(text, "### 6.3 ")


def test_deploy_doc_section_4_complete() -> None:
    four = section(read_doc("docs/deploy.md"), "## 4. ")
    assert "Both images run as the non-root user `earmark`" in four
    stub = "It runs as a non-root user"
    dangling = [ln for ln in four.splitlines() if ln.rstrip().endswith(stub)]
    assert not dangling
    for banned in ("VAULT_PATH", "/vault", "EARMARK_LLM", "EARMARK_TZ"):
        assert banned not in four, banned


def test_deploy_doc_mail_section() -> None:
    mail = section(read_doc("docs/deploy.md"), "## 8. ")
    names = [env_var(n) for n in Settings.model_fields if n.startswith("smtp_")]
    assert len(names) == 7
    for needle in (
        *names,
        "EARMARK_MAX_ATTACHMENT_MB",
        "SPF",
        "DKIM",
        "DMARC",
        "_dmarc.example.org",
        "_domainkey",
        "app password",
        "Authentication-Results",
        "allowlist",
        "DRAFT",
        "docker compose cp",
    ):
        assert needle in mail, needle


def test_deploy_doc_recipients_section(tmp_path: Path) -> None:
    text = section(read_doc("docs/deploy.md"), "## 9. ")
    fence = first_fence(after_marker(text, "allowlist-example"), "bash")
    [line] = fence.body.splitlines()
    assert line.startswith("EARMARK_ALLOWED_RECIPIENTS=")
    env_file = tmp_path / ".env"
    env_file.write_text(line + "\n")
    policy = Settings(_env_file=env_file).recipient_policy()
    assert not policy.allow_all
    assert len(policy.addresses) == 2
    for needle in ("`all`", "API token", "WARNING", "no recipients are allowed yet"):
        assert needle in text, needle


def test_deploy_doc_upgrade_section() -> None:
    block = between_markers(section(read_doc("docs/deploy.md"), "## 14. "), "upgrade-v0.1.0")
    for needle in (
        "earmark.db-wal",
        "earmark.db-shm",
        "analyses",
        "lancedb",
        "models/embed",
        "digests",
        "--remove-orphans",
        "vault is no longer touched",
        "VAULT_PATH",
    ):
        assert needle in block, needle


class _Reset:
    """What `!reset` loads as."""


def _compose_loader() -> type[yaml.SafeLoader]:
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor("!reset", lambda loader, node: _Reset())
    Loader.add_constructor("!override", lambda loader, node: _Reset())
    return Loader


def test_deploy_doc_cpu_worker_override() -> None:
    text = after_marker(read_doc("docs/deploy.md"), "cpu-worker-override")
    loaded = yaml.load(first_fence(text, "yaml").body, Loader=_compose_loader())  # noqa: S506
    worker = loaded["services"]["worker"]
    assert isinstance(worker["gpus"], _Reset)
    assert worker["image"] == "earmark:local"
    assert worker["build"]["dockerfile"] == "Dockerfile"
    assert worker["environment"]["EARMARK_ASR_DEVICE"] == "cpu"
    known = {env_var(name) for name in Settings.model_fields}
    assert {k for k in worker["environment"] if k.startswith("EARMARK_")} <= known


# --- docs/runbook-spark.md ----------------------------------------------------------------


def _phases() -> list[str]:
    text = read_doc("docs/runbook-spark.md")
    titles = re.findall(r"^## (Phase \d+|Send back)\b", text, re.MULTILINE)
    assert titles == [f"Phase {n}" for n in range(13)] + ["Send back"]
    parts = re.split(r"^## ", text, flags=re.MULTILINE)
    return [p for p in parts if p.startswith("Phase ")]


def test_runbook_phases_end_in_checks() -> None:
    phases = _phases()
    assert len(phases) == 13
    for phase in phases:
        title = phase.splitlines()[0]
        assert phase.count("**Check:**") == 1, title
        before, _, after = phase.partition("**Check:**")
        body = before.split("\n", 1)[1].strip()
        assert body, f"{title}: the check is the first paragraph"
        assert "\n### " not in after, title


def test_runbook_real_delivery_check() -> None:
    needles = (
        "you@example.org",
        ".instructions.md",
        "both attachments",
        "body",
        "Authentication-Results",
        "dmarc=pass",
    )
    found = [
        p
        for p in _phases()
        if all(n in p.partition("**Check:**")[2] for n in needles)
        and ("EARMARK_MAIL_BACKEND=smtp" in p or "deploy.md#84-" in p)
    ]
    assert len(found) == 1


def _phase(number: int) -> str:
    [found] = [p for p in _phases() if p.startswith(f"Phase {number}:")]
    return found


def test_runbook_last_phase_is_the_system_check() -> None:
    last = _phases()[-1]
    assert last.startswith("Phase 12: ")
    assert "one episode" in last.splitlines()[0]
    assert "ARCHITECTURE.md#definition-of-done" in last
    check = _flat(last.partition("**Check:**")[2])
    for needle in (
        "job_id",
        "Sent:",
        "cuda",
        "inbox",
        "receiving skill",
        'source: "earmark"',
        "guid",
        "commits nothing",
        "instructions",
        "received.jsonl",
        "reshare",
        "done",
        "earmark-receive",
    ):
        assert needle in check, needle
    assert "step 30 adds" not in check


def test_runbook_skill_install_phase() -> None:
    phase = _phase(11)
    assert "receiving skill" in phase.splitlines()[0]
    body, _, check = phase.partition("**Check:**")
    flat = _flat(body)
    for needle in (
        "claude plugin marketplace add rappdw/earmark --scope project",
        "claude plugin install earmark-pka@earmark --scope project",
        "project scope",
        "never user scope",
        "/reload-plugins",
        "claude plugin update earmark-pka@earmark --scope project",
        "claude plugin marketplace update earmark",
        "1.8.0",
        "/path/to/pka-workspace",
    ):
        assert needle in flat, needle
    for needle in (
        '"scope": "project"',
        "enabledPlugins",
        "~/.claude/settings.json",
        "earmark-pka:earmark-receive",
    ):
        assert needle in _flat(check), needle
    assert "/path/to/pka-workspace" in read_doc(RUNBOOK).split("## Phase 0:")[0]


def test_runbook_phase_6_uses_amap() -> None:
    body, _, check = _flat(_phase(6)).partition("**Check:**")
    for needle in (
        "AMAP",
        "router",
        "allowlist",
        "earmark@example.org",
        "EARMARK_MAX_ATTACHMENT_MB",
        "1.37",
    ):
        assert needle in body, needle
    for needle in ("attachment limit", "EARMARK_MAX_ATTACHMENT_MB", "`clean`"):
        assert needle in check, needle


def test_deploy_doc_pka_side_is_amap() -> None:
    text = read_doc("docs/deploy.md")
    side = _flat(section(text, "### 8.5 "))
    for needle in (
        "AMAP",
        "router",
        "allowlist",
        "outside this repo",
        "`clean`",
        "EARMARK_MAX_ATTACHMENT_MB",
    ):
        assert needle in side, needle
    limit = _flat(section(text, "### 8.4 "))
    assert "AMAP" in limit
    assert "1.37" in limit


def test_runbook_covers_the_brief() -> None:
    text = read_doc("docs/runbook-spark.md")
    for needle in (
        "deploy.md#73-the-spark-smoke-test",
        "SPF",
        "DKIM",
        "DMARC",
        "app password",
        "EARMARK_ALLOWED_RECIPIENTS",
        "`all`",
        "-wal",
        "-shm",
        "analyses/",
        "lancedb/",
        "models/embed/",
        "vault is no longer touched",
        "AMAP",
    ):
        assert needle in text, needle
    assert "Dockerfile.gpu" in text or "GPU image" in text


# --- share-sheet captures -----------------------------------------------------------------

SHARE_KEYS = ("captured_on", "ios", "share_option", "shortcut_input", "url", "canonical")


def check_share_capture(entry: dict[str, Any]) -> list[str]:
    """Problems with one recorded share-sheet capture; an empty list means it is sound."""
    problems = [
        f"{key} must be a string"
        for key in SHARE_KEYS
        if not isinstance(entry.get(key), str) or not entry[key]
    ]
    if problems:
        return problems
    try:
        canonical = parse_apple_url(entry["url"]).url
    except UnsupportedUrlError as exc:
        return [f"url is not accepted: {exc.message}"]
    if canonical != entry["canonical"]:
        problems.append(f"canonical is {canonical!r}, not {entry['canonical']!r}")
    if entry["url"] not in entry["shortcut_input"]:
        problems.append("url does not occur in shortcut_input")
    return problems


def _synthetic_entry() -> dict[str, Any]:
    # Synthetic: built here from the step 04 URL shape, never written to the fixture.
    url = "https://podcasts.apple.com/us/podcast/did-a-i-break-the-internet/id1200361736?i=1000802907981&uo=4"
    return {
        "captured_on": "2026-12-01",
        "ios": "26.2",
        "share_option": "From Start",
        "shortcut_input": f"Did A.I. Break the Internet? {url}",
        "url": url,
        "canonical": "https://podcasts.apple.com/us/podcast/id1200361736?i=1000802907981",
    }


def _fixture() -> dict[str, Any]:
    data = json.loads((ROOT / "tests" / "fixtures" / "share_sheet.json").read_text())
    assert isinstance(data, dict)
    return data


def test_share_sheet_fixture_shape() -> None:
    data = _fixture()
    assert set(data) == {"about", "shares"}
    assert isinstance(data["shares"], list)
    assert "iPhone" in data["about"]
    assert "never" in data["about"].lower()


def test_recorded_share_sheet_captures(tmp_path: Path) -> None:
    for index, entry in enumerate(_fixture()["shares"]):
        assert check_share_capture(entry) == [], f"share {index}"
        body = json.dumps({"url": entry["url"], "to": "you@example.org"}).encode()
        status, _, store = _post(body, tmp_path / str(index))
        assert status == 202, f"share {index}"
        assert [j.url for j in store.list_recent()] == [entry["canonical"]]


def test_share_capture_checker_accepts_known_shape(tmp_path: Path) -> None:
    entry = _synthetic_entry()
    assert check_share_capture(entry) == []
    body = json.dumps({"url": entry["url"], "to": "you@example.org"}).encode()
    status, _, store = _post(body, tmp_path)
    assert status == 202
    assert [j.url for j in store.list_recent()] == [entry["canonical"]]


def test_share_capture_checker_reports_problems() -> None:
    wrong_canonical = {
        **_synthetic_entry(),
        "canonical": "https://podcasts.apple.com/us/podcast/id1?i=2",
    }
    assert any("canonical" in p for p in check_share_capture(wrong_canonical))
    not_in_input = {**_synthetic_entry(), "shortcut_input": "something else"}
    assert any("shortcut_input" in p for p in check_share_capture(not_in_input))
    missing = _synthetic_entry()
    del missing["ios"]
    assert any("ios" in p for p in check_share_capture(missing))
    show = "https://podcasts.apple.com/us/podcast/id1200361736"
    show_link = {**_synthetic_entry(), "url": show, "shortcut_input": show}
    assert any("not accepted" in p for p in check_share_capture(show_link))


# --- README command table and route list ---------------------------------------------------


def _command_words(cell: str) -> str:
    words: list[str] = []
    for word in cell.split():
        if word.startswith(("<", "[")) or word.isupper():
            break
        words.append(word)
    return " ".join(words)


def test_readme_commands_cover_cli() -> None:
    from earmark.cli import app

    rows = [
        line
        for line in read_doc("README.md").splitlines()
        if line.startswith("| `") and not line.startswith("| `EARMARK_")
    ]
    listed: set[str] = set()
    for row in rows:
        match = re.match(r"\| `([^`]+)`", row)
        assert match
        listed.add(_command_words(match[1]))
    assert "" not in listed
    registered = {c.name for c in app.registered_commands if c.name}
    for group in app.registered_groups:
        assert group.typer_instance is not None
        registered |= {
            f"{group.name} {c.name}" for c in group.typer_instance.registered_commands if c.name
        }
    registered.add("--version")
    assert listed == registered


def _readme_routes() -> set[tuple[str, str]]:
    text = read_doc("README.md")
    paragraph = next(p for p in text.split("\n\n") if p.startswith("The HTTP API is"))
    paragraph = " ".join(paragraph.split())
    found: set[tuple[str, str]] = set()
    for span in re.findall(r"`([A-Z|]+ /[^`]*)`", paragraph):
        methods, path = span.split(" ", 1)
        for method in methods.split("|"):
            found.add((method, re.sub(r"\{[^}]*\}", "{}", path)))
    return found


def test_readme_http_api_lists_every_route(tmp_path: Path) -> None:
    from fastapi.routing import APIRoute

    from earmark.api.routes import build_router

    store = JobStore(open_database(tmp_path / "earmark.db"))
    intake = Intake(store, RecipientPolicy.from_setting(()), Staging(tmp_path / "data"))
    router = build_router(intake, max_upload_bytes=1024)
    actual = {
        (method, re.sub(r"\{[^}]*\}", "{}", route.path))
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in (route.methods or ())
    }
    assert actual
    assert _readme_routes() == actual


def test_deploy_doc_hf_telemetry_section() -> None:
    text = read_doc("docs/deploy.md")
    start = text.index("## 17. Hugging Face telemetry and logs")
    assert start > text.index("## 16. Troubleshooting")
    end = text.find("\n## ", start + 1)
    section = text[start : end if end != -1 else len(text)]
    for needle in (
        "HF_HUB_DISABLE_TELEMETRY=1",
        "HF_HUB_DISABLE_TELEMETRY=0",
        "systemctl edit",
        ".env",
        "HF_XET_LOG_DEST",
        "DO_NOT_TRACK",
    ):
        assert needle in section, needle
    architecture = read_doc("docs/ARCHITECTURE.md")
    assert "HF_HUB_DISABLE_TELEMETRY=1" in architecture
    assert "HF_HUB_DISABLE_TELEMETRY=0" in architecture


# --- the system scope (step 28) -------------------------------------------------------------

SYSTEM_HEADING = "## System: three parts and two seams"
DONE_SENTENCE = (
    "One episode shared on the phone is transcribed on the Spark, emailed, read by a PKA "
    "through the receiving skill, and filed, with the instructions acted on."
)
DONE_MEANS = (
    "A step being `done` means its part's code and tests pass; it does not mean the system works."
)


def _table(text: str, heading: str) -> list[list[str]]:
    return table_rows(section(text, heading))


def test_architecture_opens_with_system_section() -> None:
    heads = [p.splitlines()[0] for p in paragraphs(read_doc(ARCH)) if p.startswith("## ")]
    assert heads[:3] == [SYSTEM_HEADING, "## The service", "## Decisions"]


def test_architecture_system_parts() -> None:
    rows = _table(_system(), "### The three parts")
    assert [r[0] for r in rows] == ["1. The Shortcut", "2. The service", "3. The receiving skill"]
    for row, runs_on, lives_in in zip(
        rows,
        ["iPhone", "DGX Spark", "PKA"],
        ["shortcut/earmark.shortcut", "src/earmark/", "plugins/earmark-pka/"],
        strict=True,
    ):
        assert runs_on in row[2], row[0]
        assert lives_in in row[3], row[0]


def test_architecture_system_seams() -> None:
    rows = _table(_system(), "### The two seams")
    assert [r[0] for r in rows] == ["Request", "Mail"]
    request, mail = rows
    assert "POST /transcriptions" in request[2]
    assert "contract: 1" in mail[2]
    assert "contract/samples/v1/" in mail[3]
    assert "29" in step_numbers(mail[3])


def test_architecture_mail_reaches_pka_through_amap() -> None:
    text = _flat(section(_system(), "### How mail reaches a PKA"))
    for needle in (
        "AMAP",
        "outside this repo",
        "AMMP",
        "allowlist",
        "DMARC",
        "router",
        "doorbell",
        "`clean`",
        "inbox",
        "EARMARK_MAX_ATTACHMENT_MB",
        "No earmark code reads, polls or selects mail",
    ):
        assert needle in text, needle


def test_architecture_definition_of_done() -> None:
    text = _flat(section(_system(), "### Definition of done"))
    assert DONE_SENTENCE in text
    assert DONE_MEANS in text
    assert "phase 12" in text


def _planned(entry_step: str | None, path: str) -> bool:
    return entry_step is not None and not (ROOT / path).exists()


def test_architecture_repo_layout_matches_repo() -> None:
    entries = _layout()
    status = plan_status(read_doc("docs/PLAN.md"))
    for entry in entries:
        if (ROOT / entry.path).exists():
            continue
        assert entry.step is not None, f"{entry.path} is missing and names no step"
        assert status.get(entry.step) == "todo", f"{entry.path}: step {entry.step} is not todo"
    assert all((ROOT / e.path).exists() for e in entries if e.step is None)
    paths = {e.path for e in entries}
    brief = {
        ".claude-plugin/marketplace.json",
        "plugins/earmark-pka/skills/earmark-receive/SKILL.md",
        "plugins/earmark-pka/skills/earmark-receive/scripts/",
        "contract/mail-v1.md",
        "contract/samples/v1/",
        "shortcut/earmark.shortcut",
        "src/earmark/",
        "compose.yaml",
    }
    assert brief <= paths, sorted(brief - paths)


IGNORED_DIRECTORIES = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "dist",
    "build",
    "data",
    "htmlcov",
}


def test_architecture_repo_layout_covers_top_level() -> None:
    covered = {e.path.split("/", 1)[0] for e in _layout()}
    present = {
        p.name
        for p in ROOT.iterdir()
        if p.is_dir() and p.name not in IGNORED_DIRECTORIES and not p.name.endswith(".egg-info")
    }
    assert present <= covered, sorted(present - covered)


def test_architecture_mail_contract_provenance() -> None:
    text = read_doc(ARCH)
    headings = [ln for ln in text.splitlines() if ln.startswith("#### The mail contract")]
    assert headings == ["#### The mail contract (`contract: 1`)"]
    preamble = section(text, "#### The mail contract")
    flat = _flat(preamble).lower()
    for needle in (
        "drafted by pka-skills",
        "2026-11-23",
        "2026-11-24",
        "owned by this repo",
        "contract/mail-v1.md",
        "**draft**",
    ):
        assert needle in flat, needle
    assert {"29", "30"} <= step_numbers(preamble)


def test_architecture_parts_say_what_is_unbuilt() -> None:
    shortcut, service, skill = (r[4] for r in _table(_system(), "### The three parts"))
    assert "Never run on a phone" in shortcut
    assert "31" in step_numbers(shortcut)
    assert "never been built" in service
    assert "GPU image" in service
    assert "never run in a PKA" in skill
    assert "30" in step_numbers(skill)
    assert "has never run end to end" in _flat(section(_system(), "### Definition of done"))


def test_readme_system_section_says_what_is_unbuilt() -> None:
    text = _flat(section(read_doc("README.md"), "## The system"))
    for needle in ("never run on a phone", "step 31", "never been built", "never run in a PKA"):
        assert needle in text, needle
    assert {"29", "30"} <= step_numbers(text)


def test_architecture_receiving_skill_section() -> None:
    text = _flat(section(_system(), "### The receiving skill"))
    for needle in (
        "--scope project",
        "/reload-plugins",
        "received.jsonl",
        "1.8.0",
        "PLUGIN_RELEASES",
        "path:",
        "claude plugin validate --strict",
    ):
        assert needle in text, needle


def _planned_names() -> dict[str, str]:
    names = {
        (e.path if e.path.count("/") == 1 and e.path.endswith("/") else e.path.rstrip("/")): e.step
        for e in _layout()
        if e.step is not None and _planned(e.step, e.path)
    }
    aliases = (
        ("earmark-pka", "30", "plugins/earmark-pka/.claude-plugin/plugin.json"),
        ("earmark-receive", "30", "plugins/earmark-pka/skills/earmark-receive/SKILL.md"),
        ("earmark.shortcut", "31", "shortcut/earmark.shortcut"),
        ("marketplace.json", "30", ".claude-plugin/marketplace.json"),
    )
    names.update({t: st for t, st, path in aliases if not (ROOT / path).exists()})
    return names


@pytest.mark.parametrize("doc", [*DOCS, "CLAUDE.md", ARCH])
def test_planned_paths_are_marked_planned(doc: str) -> None:
    names = _planned_names()
    missing = [e for e in _layout() if e.step is not None and not (ROOT / e.path).exists()]
    assert missing, "steps 30 and 31 are still to build"
    for entry in missing:
        derived = (
            entry.path
            if entry.path.count("/") == 1 and entry.path.endswith("/")
            else entry.path.rstrip("/")
        )
        assert derived in names, derived
    for paragraph in paragraphs(read_doc(doc)):
        for name, step in names.items():
            if name in paragraph:
                assert step in step_numbers(paragraph), (
                    f"{doc} names {name} without step {step}: {paragraph[:80]!r}"
                )


ALLOWLIST_AS_PKA_JOB = [
    r"ask (each|the|a) PKAs? to\b",
    r"\bPKAs? (to )?allowlists?\b",
    r"allowlisted at (each|the) PKA",
    r"PKA's runtime",
]


@pytest.mark.parametrize("doc", [*DOCS, "CLAUDE.md", "system"])
def test_sender_allowlist_is_amap_router_config(doc: str) -> None:
    text = _system() if doc == "system" else read_doc(doc)
    patterns = [*ALLOWLIST_AS_PKA_JOB, *([r"mail lane"] if doc in DOCS else [])]
    for pattern in patterns:
        hits = [m[0] for m in re.finditer(pattern, _flat(text), re.IGNORECASE)]
        assert not hits, f"{doc}: {hits[:3]!r}"


def test_readme_and_claude_md_name_the_system() -> None:
    sentence = "The service files nothing; the PKA files, through the receiving skill."
    for doc in ("README.md", "CLAUDE.md"):
        flat = _flat(read_doc(doc))
        assert sentence in flat, doc
        for banned in ("earmark itself files nothing", "earmark files nothing"):
            assert banned not in flat, doc
        for needle in (
            "receiving skill",
            "AMAP",
            "contract: 1",
            "POST /transcriptions",
            "plugins/earmark-pka/",
        ):
            assert needle in flat, (doc, needle)
    claude = _flat(read_doc("CLAUDE.md"))
    assert "Intake is AMAP's job" in claude
    assert "does not mean the system works" in claude


def test_rescope_pka_skill_bullet_superseded_in_both() -> None:
    note = "*Superseded on 2026-11-24:* the receiving skill is part of this repo (step 30)."
    for doc in ("docs/PLAN.md", ARCH):
        assert note in _flat(read_doc(doc)), doc


def _contract_block() -> str:
    text = read_doc("contract/mail-v1.md")
    return between_markers(text, "contract-v1").strip("\n") + "\n"


def test_contract_v1_text_is_pinned() -> None:
    digest = hashlib.sha256(_contract_block().encode("utf-8")).hexdigest()
    assert digest == CONTRACT_V1_SHA256


def test_contract_text_exists_once() -> None:
    lines = {ln.strip() for ln in _contract_block().splitlines() if len(ln.strip()) >= 40}
    assert len(lines) >= 40
    contract = ROOT / "contract" / "mail-v1.md"
    for directory, names, files in os.walk(ROOT):
        names[:] = [n for n in names if n not in {".git", ".venv"}]
        for name in files:
            path = Path(directory) / name
            if path.suffix != ".md" or path == contract:
                continue
            text = path.read_text(encoding="utf-8")
            hits = [ln for ln in lines if ln in text]
            assert not hits, (str(path.relative_to(ROOT)), hits[:2])


def test_plan_and_architecture_point_to_contract() -> None:
    for doc in ("docs/PLAN.md", ARCH):
        assert "contract/mail-v1.md" in read_doc(doc), doc
    mail = section(read_doc(ARCH), "#### The mail contract")
    assert "- **Both files**" not in mail


def test_contract_readme() -> None:
    text = _flat(read_doc("contract/README.md"))
    for needle in (
        "plugins/earmark-pka/skills/earmark-receive/scripts/check_mail.py",
        "python3 -I",
        "mail-v1.md",
        "samples/v1/",
        "DRAFT",
        "make samples",
        "3.9",
        "step 30",
    ):
        assert needle in text, needle
    for code in ("0", "1", "2", "64"):
        assert re.search(rf"\b{code}\b", text), code
    assert "## What the checker cannot check" in read_doc("contract/README.md")


def test_contract_still_draft() -> None:
    versions = section(read_doc("contract/README.md"), "## Versions")
    assert "DRAFT" in versions
    assert "Status: **DRAFT**" in read_doc("contract/mail-v1.md")


@pytest.mark.parametrize("doc", CONTRACT_DOCS)
def test_contract_docs_use_placeholders_only(doc: str) -> None:
    _assert_placeholders(read_doc(doc), doc)
