import json
import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from earmark.config import REDACTED, Settings
from earmark.errors import PermanentError, TransientError
from earmark.fakes import make_fake_backends
from earmark.models import Job, JobResult
from earmark.notify import ntfy
from earmark.notify.ntfy import (
    Notification,
    NtfyClient,
    NtfyNotifier,
    NtfyOptions,
    build_ntfy_notifier,
    clean_field,
    compose,
    obsidian_url,
    split_topic_url,
)
from tests.factories import make_episode, make_job
from tests.ntfy_stub import (
    ACCOUNT_ANON,
    HEALTH_OK,
    PUBLISHED,
    ROOT_URL,
    TOKEN,
    TOO_MANY,
    TOPIC,
    TOPIC_URL,
    UNAUTHORIZED,
    NtfyStub,
    json_reply,
)
from tests.pipeline_env import CANON, SECRET, make_env

Handler = Callable[[httpx2.Request], httpx2.Response]
NOTE = "Earmark/Podcasts/Show/Episode.md"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"ntfy_url": SecretStr(TOPIC_URL), "obsidian_vault_name": "My Vault"}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _options(*, token: bool = False, vault: str | None = "My Vault") -> NtfyOptions:
    settings = _settings(ntfy_token=SecretStr(TOKEN) if token else None, obsidian_vault_name=vault)
    options = NtfyOptions.from_settings(settings)
    assert options is not None
    return options


def _client(handler: Handler, *, token: bool = False) -> tuple[NtfyClient, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    client = NtfyClient(_options(token=token), transport=httpx2.MockTransport(record))
    return client, seen


def _job(status: str = "done", **overrides: Any) -> Job:
    base = make_job()
    values: dict[str, Any] = {
        "status": status,
        "stage": "transcribe",
        "attempts": 4,
        "error": "asr server said no",
        "prompt": None,
        "warnings": (),
        "result": JobResult(note_path=NOTE),
    }
    values.update(overrides)
    return base.model_copy(update=values)


def _compose(job: Job, *, vault: str | None = "My Vault", episode: bool = True) -> Notification:
    result = compose(
        job, make_episode() if episode else None, vault_name=vault, secrets=(TOKEN, TOPIC_URL)
    )
    assert result is not None
    return result


# -- 6.1 titles and messages ------------------------------------------------------------


def test_compose_titles_distinct_per_status() -> None:
    done, failed, skipped = (_compose(_job(s)) for s in ("done", "failed", "skipped"))
    assert done.title.startswith("Note ready: ")
    assert failed.title.startswith("Failed: ")
    assert skipped.title.startswith("Skipped: ")
    assert len({done.title, failed.title, skipped.title}) == 3
    assert len({done.message, failed.message, skipped.message}) == 3
    assert (done.tags, failed.tags, skipped.tags) == (
        ("white_check_mark",),
        ("x",),
        ("fast_forward",),
    )
    assert (done.priority, failed.priority, skipped.priority) == (3, 4, 3)


def test_compose_failed_includes_reason_stage_attempts() -> None:
    message = _compose(_job("failed")).message
    assert "Reason: asr server said no" in message
    assert "Stage: transcribe" in message
    assert "Gave up after 4 attempts." in message
    assert message.splitlines()[0] == "Le Podcast ☕"
    assert "Gave up" not in _compose(_job("failed", attempts=1)).message


def test_compose_skipped_includes_reason() -> None:
    message = _compose(_job("skipped", error="subscriber-only episode")).message
    assert "Reason: subscriber-only episode" in message
    assert "Stage:" not in message


def test_compose_done_has_no_reason_and_counts_warnings() -> None:
    plain = _compose(_job("done"))
    assert "Reason" not in plain.message
    assert "Prompt:" not in plain.message
    assert "warning" not in plain.message
    job = _job("done", prompt="Who is cited?", warnings=("first problem", "second"))
    message = _compose(job).message
    assert "Prompt: Who is cited?" in message
    assert "2 warnings: first problem" in message
    assert "second" not in message
    assert "1 warning: only" in _compose(_job("done", warnings=("only",))).message


def test_compose_unknown_episode() -> None:
    job = _job("failed", stage="resolve")
    result = _compose(job, episode=False)
    assert result.title == "Failed: podcast episode"
    assert result.message.splitlines()[0] == job.url


@pytest.mark.parametrize("status", ["queued", "running"])
def test_compose_non_terminal_is_none(status: str) -> None:
    assert compose(_job(status), make_episode(), vault_name=None) is None


def test_notifier_sends_one_request_per_job() -> None:
    client, seen = _client(lambda r: json_reply(200, PUBLISHED))
    NtfyNotifier(client).notify(_job("done"), make_episode())
    [request] = seen
    assert (request.method, str(request.url)) == ("POST", ROOT_URL)
    body = json.loads(request.content)
    assert set(body) == {"topic", "title", "message", "tags", "priority", "click"}
    assert body["topic"] == TOPIC
    assert body["tags"] == ["white_check_mark"]
    assert "markdown" not in body


def test_notifier_skips_non_terminal_job() -> None:
    client, seen = _client(lambda r: json_reply(200, PUBLISHED))
    NtfyNotifier(client).notify(_job("running"), make_episode())
    assert seen == []


def test_notifier_logs_job_and_host_only(caplog: pytest.LogCaptureFixture) -> None:
    client, _ = _client(lambda r: json_reply(200, PUBLISHED))
    with caplog.at_level(logging.INFO, logger="earmark.notify.ntfy"):
        NtfyNotifier(client).notify(_job("done"), make_episode())
    text = caplog.text
    assert "notified job" in text
    assert "ntfy.test" in text
    assert TOPIC not in text
    assert make_episode().title not in text


# -- 6.2 the obsidian link ---------------------------------------------------------------


def test_obsidian_url_golden() -> None:
    assert obsidian_url("My Vault", "Earmark/Podcasts/Hard Fork/2026-11-20 Café & Co?.md") == (
        "obsidian://open?vault=My%20Vault&file=Earmark%2FPodcasts%2FHard%20Fork%2F"
        "2026-11-20%20Caf%C3%A9%20%26%20Co%3F.md"
    )


def test_obsidian_url_encodes_reserved() -> None:
    url = obsidian_url("v", "a+b=c%d~e_f.g-h!'()*/ &?")
    assert url == "obsidian://open?vault=v&file=a%2Bb%3Dc%25d~e_f.g-h%21%27%28%29%2A%2F%20%26%3F"
    assert obsidian_url("v", "🎙.md") == "obsidian://open?vault=v&file=%F0%9F%8E%99.md"
    nfd = "é.md"
    assert obsidian_url("v", nfd) == "obsidian://open?vault=v&file=e%CC%81.md"


def test_obsidian_url_refuses_hash_and_overlong() -> None:
    assert obsidian_url("v", "a#b.md") is None
    assert obsidian_url("v", "a/" + "x" * 2000 + ".md") is None
    assert obsidian_url("", "a.md") is None
    assert obsidian_url("v", "") is None


def test_compose_done_click_with_vault_name() -> None:
    result = _compose(_job("done"))
    assert result.click == obsidian_url("My Vault", NOTE)
    assert result.click is not None
    assert "Note:" not in result.message


def test_compose_done_without_vault_name_has_note_line() -> None:
    result = _compose(_job("done"), vault=None)
    assert result.click is None
    assert f"Note: {NOTE}" in result.message


def test_compose_done_hash_in_path_falls_back_to_note_line() -> None:
    job = _job("done", result=JobResult(note_path="Earmark/a #1.md"))
    result = _compose(job)
    assert result.click is None
    assert "Note: Earmark/a #1.md" in result.message


def test_compose_failed_and_skipped_have_no_click() -> None:
    assert _compose(_job("failed")).click is None
    assert _compose(_job("skipped")).click is None


def test_notifier_click_in_request_body() -> None:
    client, seen = _client(lambda r: json_reply(200, PUBLISHED))
    NtfyNotifier(client).notify(_job("done"), make_episode())
    [request] = seen
    expected = obsidian_url("My Vault", NOTE)
    assert expected is not None
    assert json.loads(request.content)["click"] == expected
    assert all(expected not in v for v in request.headers.values())


# -- 6.3 the token -----------------------------------------------------------------------


def test_publish_without_token_sends_no_authorization() -> None:
    client, seen = _client(lambda r: json_reply(200, PUBLISHED))
    NtfyNotifier(client).notify(_job("done"), make_episode())
    assert all("authorization" not in k.lower() for k in seen[0].headers)


def test_publish_with_token_sends_bearer() -> None:
    client, seen = _client(lambda r: json_reply(200, PUBLISHED), token=True)
    NtfyNotifier(client).notify(_job("done"), make_episode())
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"


def test_health_never_sends_token_but_account_does() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/health":
            return json_reply(200, HEALTH_OK)
        return json_reply(200, ACCOUNT_ANON)

    client, seen = _client(handler, token=True)
    assert client.health().healthy is True
    assert "authorization" not in {k.lower() for k in seen[0].headers}
    account = client.account()
    assert (account.username, account.messages_remaining) == ("*", 250)
    assert seen[1].headers["Authorization"] == f"Bearer {TOKEN}"


def test_ntfy_stub_round_trip() -> None:
    with NtfyStub() as stub:
        options = replace(
            _options(token=True), root_url="http://ntfy.test/"
        )  # no TLS on a unix socket
        client = NtfyClient(options, transport=stub.transport())
        NtfyNotifier(client).notify(_job("failed"), make_episode())
        assert client.health().healthy is True
        client.close()
    post, health = stub.requests
    assert (post.method, post.path) == ("POST", "/")
    assert post.headers["content-type"] == "application/json"
    assert post.headers["authorization"] == f"Bearer {TOKEN}"
    body = post.json
    assert body["topic"] == TOPIC
    assert body["title"].startswith("Failed: ")
    assert body["priority"] == 4
    assert (health.method, health.path) == ("GET", "/v1/health")
    assert "authorization" not in health.headers


# -- 6.4 errors --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "headers", "kind", "needle"),
    [
        (301, {"Location": "https://elsewhere.test/"}, PermanentError, "EARMARK_NTFY_URL"),
        (401, {}, PermanentError, "EARMARK_NTFY_TOKEN"),
        (403, {}, PermanentError, "EARMARK_NTFY_TOKEN"),
        (404, {}, PermanentError, "HTTP 404"),
        (413, {}, PermanentError, "HTTP 413"),
        (429, {"Retry-After": "5"}, TransientError, "HTTP 429"),
        (500, {}, TransientError, "HTTP 500"),
        (507, {}, TransientError, "HTTP 507"),
    ],
)
def test_publish_status_mapping(
    status: int, headers: dict[str, str], kind: type[Exception], needle: str
) -> None:
    body = TOO_MANY if status == 429 else UNAUTHORIZED
    client, _ = _client(lambda r: json_reply(status, body, **headers), token=True)
    with pytest.raises(kind) as info:
        NtfyNotifier(client).notify(_job("done"), make_episode())
    exc = info.value
    assert isinstance(exc, PermanentError | TransientError)
    assert needle in exc.message
    assert "ntfy.test" in exc.message
    assert TOPIC not in exc.message
    assert TOKEN not in exc.message
    if status == 429:
        assert isinstance(exc, TransientError)
        assert exc.retry_after_s == 5


@pytest.mark.parametrize(
    ("raised", "message"),
    [
        (httpx2.ConnectError("boom"), "could not reach ntfy at ntfy.test: ConnectError"),
        (httpx2.ReadTimeout("slow"), "did not answer within 10 s"),
    ],
)
def test_publish_transport_errors_are_transient(raised: Exception, message: str) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise raised

    client, _ = _client(handler)
    with pytest.raises(TransientError) as info:
        NtfyNotifier(client).notify(_job("done"), make_episode())
    assert message in info.value.message
    assert TOPIC not in info.value.message


def test_publish_error_detail_redacted() -> None:
    body = json.dumps({"error": f"bad token {TOKEN} for topic {TOPIC}"}).encode()
    client, _ = _client(lambda r: json_reply(403, body), token=True)
    with pytest.raises(PermanentError) as info:
        NtfyNotifier(client).notify(_job("done"), make_episode())
    assert REDACTED in info.value.message
    assert TOKEN not in info.value.message
    assert TOPIC not in info.value.message


def test_publish_no_redirect_followed() -> None:
    client, seen = _client(
        lambda r: httpx2.Response(302, headers={"Location": "https://evil.test/"})
    )
    with pytest.raises(PermanentError):
        NtfyNotifier(client).notify(_job("done"), make_episode())
    assert len(seen) == 1


def test_health_redirect_raises() -> None:
    client, _ = _client(lambda r: httpx2.Response(301, headers={"Location": "https://x.test/"}))
    with pytest.raises(PermanentError, match="redirect"):
        client.health()


def _notifier_env(tmp_path: Path, handler: Handler) -> tuple[Any, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    options = replace(_options(), secrets=(SECRET, TOPIC_URL))
    client = NtfyClient(options, transport=httpx2.MockTransport(record))
    fakes = make_fake_backends(tmp_path / "data", vault_dir=tmp_path / "vault")
    backends = replace(fakes.backends(), notifier=NtfyNotifier(client))
    return make_env(tmp_path, fakes=fakes, backends=backends), seen


@pytest.mark.parametrize("fail", ["analyze", None])
def test_pipeline_ntfy_failure_is_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, fail: str | None
) -> None:
    env, seen = _notifier_env(tmp_path, lambda r: json_reply(500, b'{"error":"boom"}'))
    if fail:
        env.fakes.analyzer.fail_next(PermanentError("bad transcript"))
    env.jobs.enqueue(CANON)
    with caplog.at_level(logging.WARNING):
        job = env.worker.run_once()
    assert job is not None
    stored = env.jobs.get(job.id)
    assert stored is not None
    assert stored.status == ("failed" if fail else "done")
    [warning] = stored.warnings
    assert warning.startswith("notify failed: ntfy at ntfy.test failed: HTTP 500")
    assert "notifier failed for job" in caplog.text
    assert len(seen) == 1
    for sentinel in (TOPIC, SECRET):
        assert sentinel not in warning
        assert sentinel not in caplog.text


def test_pipeline_unexpected_notifier_error_is_warning(tmp_path: Path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise RuntimeError("unexpected")

    env, _ = _notifier_env(tmp_path, handler)
    env.jobs.enqueue(CANON)
    job = env.worker.run_once()
    assert job is not None
    stored = env.jobs.get(job.id)
    assert stored is not None
    assert stored.status == "done"
    assert [w.startswith("notify failed: ") for w in stored.warnings] == [True]


def test_pipeline_ntfy_success_no_warning(tmp_path: Path) -> None:
    env, seen = _notifier_env(tmp_path, lambda r: json_reply(200, PUBLISHED))
    env.jobs.enqueue(CANON)
    job = env.worker.run_once()
    assert job is not None
    stored = env.jobs.get(job.id)
    assert stored is not None
    assert (stored.status, stored.warnings) == ("done", ())
    [request] = seen
    assert json.loads(request.content)["title"].startswith("Note ready: ")


# -- 6.6 untrusted text and splitting ------------------------------------------------------


def test_clean_field_strips_controls_and_bidi() -> None:
    controls = [0x0D, 0x0A, 0x09, 0x1B, 0x202E, 0x2066, 0x2028, 0x85]
    dirty = "a" + "".join(f"{chr(c)}b" for c in controls) + chr(0x200D) + "z"
    cleaned = clean_field(dirty, 200)
    for c in controls:
        assert chr(c) not in cleaned
    assert chr(0x200D) in cleaned  # ZWJ stays: emoji sequences need it
    assert cleaned.startswith("a")
    assert cleaned.endswith(chr(0x200D) + "z")
    assert "  " not in cleaned
    assert clean_field("x\r\nX-Evil: 1", 200) == "x X-Evil: 1"


def test_clean_field_defuses_encoded_words() -> None:
    cleaned = clean_field("=?UTF-8?B?4pyT?= and =?=?x?=", 200)
    assert "=?" not in cleaned
    assert "= ?UTF-8" in cleaned


def test_compose_title_and_message_caps() -> None:
    long = "é" * 10_000
    episode = make_episode(title=long)
    job = _job("failed", error=long, prompt=long, stage="x")
    result = compose(job, episode, vault_name=None)
    assert result is not None
    assert len(result.title.encode()) <= ntfy.TITLE_MAX_BYTES
    assert len(result.message.encode()) <= ntfy.MESSAGE_MAX_BYTES
    assert result.title.endswith("…")
    assert result.message.encode().decode()  # valid UTF-8: no split character


def test_no_untrusted_text_in_headers() -> None:
    episode = make_episode(title="x\r\nX-Evil: 1")
    client, seen = _client(lambda r: json_reply(200, PUBLISHED))
    NtfyNotifier(client).notify(_job("done"), episode)
    [request] = seen
    assert {k.lower() for k in request.headers} == {
        "host",
        "user-agent",
        "accept",
        "content-type",
        "content-length",
        "accept-encoding",
        "connection",
    }
    assert "evil" not in " ".join(request.headers.values()).lower()
    title = json.loads(request.content)["title"]
    assert title == "Note ready: x X-Evil: 1"
    assert "\r" not in title
    assert request.url.query == b""


def test_compose_redacts_secrets() -> None:
    job = _job("failed", error=f"the key {TOKEN} was rejected")
    result = _compose(job)
    assert TOKEN not in result.message
    assert REDACTED in result.message


def test_payload_shape() -> None:
    note = Notification("t", "m", ("x",), 4, None)
    assert note.payload("topic") == {
        "topic": "topic",
        "title": "t",
        "message": "m",
        "tags": ["x"],
        "priority": 4,
    }


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://ntfy.sh/earmark-x1", ("https://ntfy.sh/", "earmark-x1")),
        ("http://ntfy.tail1234.ts.net:8080/a/", ("http://ntfy.tail1234.ts.net:8080/", "a")),
        ("https://host/ntfy/topic", ("https://host/ntfy/", "topic")),
    ],
)
def test_split_topic_url(url: str, expected: tuple[str, str]) -> None:
    assert split_topic_url(url) == expected


@pytest.mark.parametrize("url", ["https://ntfy.sh", "https://ntfy.sh/", "https://ntfy.sh/a.b"])
def test_split_topic_url_refuses_no_topic(url: str) -> None:
    with pytest.raises(ValueError, match="topic"):
        split_topic_url(url)


# -- the builder ---------------------------------------------------------------------------


def test_build_without_url_is_none_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="earmark.notify.ntfy"):
        assert build_ntfy_notifier(Settings(_env_file=None)) is None
    assert "EARMARK_NTFY_URL is not set" in caplog.text


def test_build_with_url_makes_no_request() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("the build must not make a request")

    notifier = build_ntfy_notifier(_settings(), transport=httpx2.MockTransport(handler))
    assert isinstance(notifier, NtfyNotifier)
    notifier.close()


def test_secret_url_in_settings_secrets() -> None:
    options = _options()
    assert TOPIC_URL in options.secrets
    assert options.host == "ntfy.test"


def test_compose_reason_redacts_bare_topic_from_settings() -> None:
    settings = _settings(ntfy_url=SecretStr("https://ntfy.test/sentinel-topic-404x"))
    options = NtfyOptions.from_settings(settings)
    assert options is not None
    job = _job("failed", error="HTTP 403 for sentinel-topic-404x")
    result = compose(job, make_episode(), vault_name=None, secrets=options.secrets)
    assert result is not None
    assert "Reason: HTTP 403 for **********" in result.message
    assert "sentinel-topic-404x" not in result.message


# -- reanalyze notifications ------------------------------------------------------------


def _reanalyze_job(status: str = "done", **overrides: Any) -> Job:
    values: dict[str, Any] = {"kind": "reanalyze", "url": None, "prompt": "Who is cited?"}
    values.update(overrides)
    return _job(status, **values)


def test_compose_reanalyze_done() -> None:
    n = _compose(_reanalyze_job("done", stage="index"))
    assert n.title == "Analysis ready: Épisode 1: «Tests»"
    assert n.message.splitlines()[:2] == ["Le Podcast ☕", "Prompt: Who is cited?"]
    assert n.click == _compose(_job("done")).click
    assert n.click is not None


def test_compose_reanalyze_default_prompt() -> None:
    n = _compose(_reanalyze_job("done", prompt=None))
    assert n.message.splitlines()[:2] == ["Le Podcast ☕", "Default analysis"]


def test_compose_reanalyze_failed_without_episode() -> None:
    key = make_episode().key
    n = _compose(_reanalyze_job("failed", stage="analyze", prompt=None), episode=False)
    assert n.title == "Analysis failed: podcast episode"
    assert n.message.splitlines() == [
        f"Episode {key}",
        "Default analysis",
        "Stage: analyze",
        "Reason: asr server said no",
        "Gave up after 4 attempts.",
    ]
    with_prompt = _compose(_reanalyze_job("failed", stage="analyze"), episode=False)
    assert with_prompt.message.splitlines()[:2] == [f"Episode {key}", "Prompt: Who is cited?"]


def test_compose_ingest_unchanged_for_kind() -> None:
    done = _compose(_job("done", prompt="p?"), vault=None)
    assert done.title == "Note ready: Épisode 1: «Tests»"
    assert done.message == f"Le Podcast ☕\nPrompt: p?\nNote: {NOTE}"
    failed = _compose(_job("failed"), episode=False)
    assert failed.title == "Failed: podcast episode"
    assert failed.message == (
        "https://podcasts.apple.com/us/podcast/x/id1?i=2\n"
        "Stage: transcribe\nReason: asr server said no\nGave up after 4 attempts."
    )
