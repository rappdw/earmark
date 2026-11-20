"""The README, Shortcut guide and deployment guide are tested against the code they describe."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from earmark.api.app import create_app
from earmark.config import Settings, env_var
from earmark.db import open_database
from earmark.jobs import JobStore
from earmark.models import IngestRequest
from earmark.resolve.apple import UnsupportedUrlError, parse_apple_url
from tests.doc_helpers import (
    ROOT,
    after_marker,
    between_markers,
    fences,
    first_fence,
    read_doc,
    relative_links,
    table_variables,
)

TOKEN = "docs-test-token-91c"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
DOCS = ["README.md", "docs/deploy.md", "docs/shortcut.md"]
SECRET_FIELDS = ("api_token", "asr_api_key", "llm_api_key", "ntfy_url", "ntfy_token")
ACTIONS = [
    "Receive input from Share Sheet",
    "Get URLs from Input",
    "Get Item from List",
    "Ask for Input",
    "Get Contents of URL",
    "Show Result",
]


def _quickstart() -> str:
    block = between_markers(read_doc("README.md"), "quickstart")
    return first_fence(block, "bash").body


def _post(body: str, tmp_path: Path) -> tuple[int, dict[str, Any], JobStore]:
    settings = Settings(_env_file=None, api_token=SecretStr(TOKEN), data_dir=tmp_path / "data")
    store = JobStore(open_database(tmp_path / "earmark.db"))
    with TestClient(create_app(settings, store=store), headers=AUTH) as client:
        response = client.post(
            "/ingest", content=body.encode(), headers={"Content-Type": "application/json"}
        )
    return response.status_code, response.json(), store


# --- README quickstart --------------------------------------------------------------------


def test_quickstart_block_present() -> None:
    block = between_markers(read_doc("README.md"), "quickstart")
    found = fences(block)
    assert [f.info for f in found] == ["bash"]


def test_quickstart_uses_fake_backends() -> None:
    script = _quickstart()
    fields = [n for n in Settings.model_fields if n.endswith("_backend")] + ["llm_provider"]
    assert len(fields) == 7
    for name in fields:
        assert re.search(rf"\b{env_var(name)}=fake\b", script), name


def test_quickstart_ingest_body_valid() -> None:
    match = re.search(r"-d '(?P<body>\{.*\})'", _quickstart())
    assert match
    request = IngestRequest.model_validate_json(match["body"])
    assert request.prompt
    assert parse_apple_url(request.url).url.startswith("https://podcasts.apple.com/")


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
    return first_fence(
        after_marker(read_doc("docs/shortcut.md"), "ingest-request-example"), "json"
    ).body


def _response_example() -> str:
    text = after_marker(read_doc("docs/shortcut.md"), "ingest-response-example")
    return first_fence(text, "json").body


def test_shortcut_request_example_validates() -> None:
    body = _request_example()
    request = IngestRequest.model_validate_json(body)
    assert set(json.loads(body)) == {"url", "prompt"}
    assert request.prompt is not None
    parse_apple_url(request.url)


def test_shortcut_request_example_accepted_by_api(tmp_path: Path) -> None:
    status, payload, store = _post(_request_example(), tmp_path)
    assert status == 202
    [job] = store.list_recent()
    assert job.status == "queued"
    assert job.url == parse_apple_url(json.loads(_request_example())["url"]).url
    assert payload["job_id"] == job.id


def test_shortcut_response_example_matches_api(tmp_path: Path) -> None:
    _, payload, _ = _post(_request_example(), tmp_path)
    example = json.loads(_response_example())
    assert set(example) == set(payload)
    assert example["status"] == payload["status"]
    assert example["created"] is payload["created"]
    assert re.fullmatch(r"[0-9a-f]{32}", example["job_id"])


def _action_items() -> list[str]:
    block = between_markers(read_doc("docs/shortcut.md"), "shortcut-actions")
    items = re.split(r"^\d+\. ", block, flags=re.MULTILINE)[1:]
    assert items
    return [" ".join(item.split()) for item in items]


def test_shortcut_actions_in_order() -> None:
    names = [re.match(r"\*\*(.+?)\*\*", item) for item in _action_items()]
    assert [m[1] for m in names if m] == ACTIONS


def test_shortcut_post_action_details() -> None:
    items = dict(zip(ACTIONS, _action_items(), strict=True))
    post = items["Get Contents of URL"]
    for word in ("POST", "Authorization", "Bearer", "JSON", "`url`", "`prompt`"):
        assert word in post, word
    assert "optional" in items["Ask for Input"].lower()


def test_shortcut_sends_only_bearer_header() -> None:
    post = _action_items()[ACTIONS.index("Get Contents of URL")]
    assert "`Authorization` is the only header to add" in post
    assert post.count("Authorization") == 2
    assert "Content-Type" in post  # sent by Shortcuts itself, not added by the user


# --- shared checks over all three documents -----------------------------------------------


@pytest.mark.parametrize("doc", DOCS)
def test_doc_relative_links_resolve(doc: str) -> None:
    base = (ROOT / doc).parent
    links = relative_links(read_doc(doc))
    assert links or doc == "docs/shortcut.md"
    for target in links:
        assert (base / target).exists(), f"{doc} links to missing {target}"


def test_no_cloudflare_tunnel_or_access_headers() -> None:
    for doc in DOCS:
        text = read_doc(doc).lower()
        for banned in ("cf-access", "cloudflared", "cloudflare tunnel"):
            assert banned not in text, f"{doc} mentions {banned}"


def test_deploy_doc_states_test_status() -> None:
    text = read_doc("docs/deploy.md")
    head = text.split("\n## ")[1]  # the first section, "What is tested"
    flat = head.lower().replace("`", "")
    assert "build-tested in ci" in flat
    assert "not run-tested" in flat
    assert "gpu profile is untested" in flat


def test_deploy_doc_reachability() -> None:
    text = read_doc("docs/deploy.md")
    for phrase in ("DNS only", "AllowedIPs", "rebind-domain-ok", "private-domain"):
        assert phrase in text, phrase
    assert "Zone:Read" in text or "Zone → Zone → Read" in text
    assert re.search(r"[Nn]ever use `tailscale funnel`", text)


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
        body = json.dumps({"url": entry["url"]})
        status, _, store = _post(body, tmp_path / str(index))
        assert status == 202, f"share {index}"
        assert [j.url for j in store.list_recent()] == [entry["canonical"]]


def test_share_capture_checker_accepts_known_shape(tmp_path: Path) -> None:
    entry = _synthetic_entry()
    assert check_share_capture(entry) == []
    status, _, store = _post(json.dumps({"url": entry["url"]}), tmp_path)
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
