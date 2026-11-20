"""Publish one notification to a real ntfy server and poll it back (``-m network``).

By default the target is a fresh random topic on public ntfy.sh with no subscriber. Set
``NTFY_TEST_URL`` (and optionally ``NTFY_TEST_TOKEN``) to use another server or topic.
"""

import json
import os
import uuid

import httpx2
import pytest
from pydantic import SecretStr

from earmark.config import Settings
from earmark.fetch import USER_AGENT
from earmark.models import JobResult
from earmark.notify.ntfy import NtfyClient, NtfyNotifier, NtfyOptions, obsidian_url
from tests.factories import make_episode, make_job

pytestmark = pytest.mark.network


def test_ntfy_publish_round_trip() -> None:
    url = os.environ.get("NTFY_TEST_URL") or f"https://ntfy.sh/earmark-test-{uuid.uuid4().hex}"
    token = os.environ.get("NTFY_TEST_TOKEN")
    settings = Settings(
        _env_file=None,
        ntfy_url=SecretStr(url),
        ntfy_token=SecretStr(token) if token else None,
        obsidian_vault_name="My Vault",
    )
    options = NtfyOptions.from_settings(settings)
    assert options is not None
    note = "Earmark/Podcasts/Show/Live test.md"
    job = make_job().model_copy(
        update={
            "status": "done",
            "warnings": (),
            "prompt": None,
            "result": JobResult(note_path=note),
        }
    )
    episode = make_episode(title="Live: Café\r\nX-Evil: 1 =?UTF-8?B?4pyT?=")

    client = NtfyClient(options)
    NtfyNotifier(client).notify(job, episode)
    client.close()

    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx2.Client(trust_env=False, follow_redirects=False, timeout=15.0) as http:
        response = http.get(f"{url}/json?poll=1&since=all", headers=headers)
    assert response.status_code == 200
    messages = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    [message] = [m for m in messages if m.get("event") == "message"]
    assert message["title"] == "Note ready: Live: Café X-Evil: 1 = ?UTF-8?B?4pyT?="
    assert message["message"] == "Le Podcast ☕"
    assert message["tags"] == ["white_check_mark"]
    assert message["priority"] == 3
    assert message["click"] == obsidian_url("My Vault", note)
