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
from earmark.models import Episode, Job, JobResult
from earmark.notify import ntfy
from earmark.notify.ntfy import (
    Notification,
    NtfyClient,
    NtfyNotifier,
    NtfyOptions,
    build_ntfy_notifier,
    clean_field,
    compose,
    split_topic_url,
)
from tests.factories import make_episode, make_job, new_podcast_job
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


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"ntfy_url": SecretStr(TOPIC_URL)}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _options(*, token: bool = False) -> NtfyOptions:
    settings = _settings(ntfy_token=SecretStr(TOKEN) if token else None)
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
        "instructions": "",
        "warnings": (),
        "result": JobResult(duration_s=125.5),
    }
    values.update(overrides)
    return base.model_copy(update=values)


def _compose(job: Job, *, episode: bool = True) -> Notification:
    result = compose(job, make_episode() if episode else None, secrets=(TOKEN, TOPIC_URL))
    assert result is not None
    return result


# -- 6.1 titles and messages ------------------------------------------------------------


def test_compose_titles_distinct_per_status() -> None:
    done, failed, skipped = (_compose(_job(s)) for s in ("done", "failed", "skipped"))
    assert done.title.startswith("Transcribed: ")
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
    assert "warning" not in plain.message
    assert "Duration: 00:02:05" in plain.message
    job = _job("done", instructions="Who is cited?", warnings=("first problem", "second"))
    message = _compose(job).message
    assert "Prompt:" not in message
    assert "Who is cited?" not in message
    assert "2 warnings: first problem" in message
    assert "second" not in message
    assert "1 warning: only" in _compose(_job("done", warnings=("only",))).message


def test_compose_unknown_episode() -> None:
    job = _job("failed", stage="resolve")
    result = _compose(job, episode=False)
    assert result.title == "Failed: podcast episode"
    assert result.message.splitlines()[0] == "Apple Podcasts episode"
    assert job.url is not None
    assert job.url not in result.message


@pytest.mark.parametrize("status", ["queued", "running"])
def test_compose_non_terminal_is_none(status: str) -> None:
    assert compose(_job(status), make_episode()) is None


def test_notifier_sends_one_request_per_job() -> None:
    client, seen = _client(lambda r: json_reply(200, PUBLISHED))
    NtfyNotifier(client).notify(_job("done"), make_episode())
    [request] = seen
    assert (request.method, str(request.url)) == ("POST", ROOT_URL)
    body = json.loads(request.content)
    assert set(body) == {"topic", "title", "message", "tags", "priority"}
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
    fakes = make_fake_backends(tmp_path / "data")
    backends = replace(fakes.backends(), notifier=NtfyNotifier(client))
    return make_env(tmp_path, fakes=fakes, backends=backends), seen


@pytest.mark.parametrize("fail", ["transcribe", None])
def test_pipeline_ntfy_failure_is_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, fail: str | None
) -> None:
    env, seen = _notifier_env(tmp_path, lambda r: json_reply(500, b'{"error":"boom"}'))
    if fail:
        env.fakes.transcriber.fail_next(PermanentError("bad audio"))
    env.jobs.enqueue(new_podcast_job(CANON))
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
    env.jobs.enqueue(new_podcast_job(CANON))
    job = env.worker.run_once()
    assert job is not None
    stored = env.jobs.get(job.id)
    assert stored is not None
    assert stored.status == "done"
    assert [w.startswith("notify failed: ") for w in stored.warnings] == [True]


def test_pipeline_ntfy_success_no_warning(tmp_path: Path) -> None:
    env, seen = _notifier_env(tmp_path, lambda r: json_reply(200, PUBLISHED))
    env.jobs.enqueue(new_podcast_job(CANON, instructions="secret-prompt-7q"))
    job = env.worker.run_once()
    assert job is not None
    stored = env.jobs.get(job.id)
    assert stored is not None
    assert (stored.status, stored.warnings) == ("done", ())
    [request] = seen
    body = json.loads(request.content)
    assert body["title"].startswith("Transcribed: Fake episode ")
    assert "Duration: 00:15:00" in body["message"]
    for forbidden in (b"://", b"podcasts.apple.com", b"click", b"secret-prompt-7q"):
        assert forbidden not in request.content


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
    job = _job("failed", error=long, instructions=long, stage="x")
    result = compose(job, episode)
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
    assert title == "Transcribed: x X-Evil: 1"
    assert "\r" not in title
    assert request.url.query == b""


def test_compose_redacts_secrets() -> None:
    job = _job("failed", error=f"the key {TOKEN} was rejected")
    result = _compose(job)
    assert TOKEN not in result.message
    assert REDACTED in result.message


def test_payload_shape() -> None:
    note = Notification("t", "m", ("x",), 4)
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
    result = compose(job, make_episode(), secrets=options.secrets)
    assert result is not None
    assert "Reason: HTTP 403 for **********" in result.message
    assert "sentinel-topic-404x" not in result.message


@pytest.mark.parametrize("status", ["done", "failed", "skipped"])
def test_payload_never_has_click(status: str) -> None:
    result = _compose(_job(status))
    assert "click" not in result.payload(TOPIC)


@pytest.mark.parametrize("status", ["done", "failed", "skipped"])
@pytest.mark.parametrize("with_episode", [True, False])
def test_compose_never_includes_url_instructions_or_path(status: str, with_episode: bool) -> None:
    episode = make_episode()
    job = _job(status, url=CANON, instructions="private-question-3k", stage="resolve")
    result = _compose(job, episode=with_episode)
    text = result.title + "\n" + result.message
    assert "://" not in text
    for secret in ("private-question-3k", CANON, episode.audio_url, episode.url):
        assert secret not in text


def test_compose_done_without_result_has_no_duration_line() -> None:
    result = _compose(_job("done", result=None))
    assert "Duration" not in result.message


# -- upload and direct-URL labels ---------------------------------------------------------

DIRECT = "https://audio.example.net/private/PATH-TOKEN-91d2.mp3?sig=QUERY-TOKEN-a77b"


def _audio_job(source: str, status: str = "done", **overrides: Any) -> Job:
    values: dict[str, Any] = {"source": source, "to": "zelda.secret@example.org", **overrides}
    if source == "upload":
        values |= {"url": None, "upload_file": "a" * 32 + ".mp3", "content_sha256": "b" * 64}
    else:
        values |= {"url": DIRECT}
    return _job(status, **values)


def _audio_episode(job: Job, title: str = "From the episode") -> Episode:
    return make_episode().model_copy(
        update={"title": title, "show_title": "", "source": job.source}
    )


def test_compose_upload_labels() -> None:
    job = _audio_job("upload", upload_name="memo.final.m4a")
    result = compose(job, _audio_episode(job, "Episode title"))
    assert result is not None
    assert result.title == "Transcribed: Episode title"
    assert result.message.splitlines()[0] == "Uploaded audio"
    titled = compose(_audio_job("upload", title="My title"), _audio_episode(job))
    assert titled is not None
    assert titled.title == "Transcribed: My title"
    no_episode = compose(_audio_job("upload", upload_name="memo.final.m4a"), None)
    assert no_episode is not None
    assert no_episode.title == "Transcribed: memo.final"
    bare = compose(_audio_job("upload", upload_name=None), None)
    assert bare is not None
    assert bare.title == "Transcribed: uploaded audio"
    failed = compose(_audio_job("upload", "failed"), None)
    assert failed is not None
    assert failed.title.startswith("Failed: ")
    assert failed.message.splitlines()[0] == "Uploaded audio"


def test_compose_url_label_uses_host_not_path() -> None:
    job = _audio_job("url")
    episode = _audio_episode(job, "PATH-TOKEN-91d2.mp3")  # a title built from the URL path
    result = compose(job, episode)
    assert result is not None
    assert result.title == "Transcribed: audio from audio.example.net"
    assert result.message.splitlines()[0] == "Audio from audio.example.net"
    text = result.title + result.message
    for token in ("PATH-TOKEN", "QUERY-TOKEN", "/private/", "://"):
        assert token not in text


def test_compose_url_with_title() -> None:
    job = _audio_job("url", title="Board meeting")
    result = compose(job, _audio_episode(job, "ignored"))
    assert result is not None
    assert result.title == "Transcribed: Board meeting"
    assert result.message.splitlines()[0] == "Audio from audio.example.net"


@pytest.mark.parametrize("source", ["upload", "url"])
def test_compose_failed_before_episode_for_audio_sources(source: str) -> None:
    job = _audio_job(source, "failed", error="audio URL on audio.example.net returned 404")
    result = compose(job, None)
    assert result is not None
    assert result.title.startswith("Failed: ")
    assert "Reason: audio URL on audio.example.net returned 404" in result.message
    assert "Stage: transcribe" in result.message
    assert result.priority == 4


def test_compose_reason_scrubbed() -> None:
    error = "could not read https://cdn.example/a/PATH-TOKEN.mp3?sig=QUERY-TOKEN: reset"
    failed = compose(_job("failed", error=error), make_episode())
    done = compose(_job("done", warnings=(error,)), make_episode())
    for result in (failed, done):
        assert result is not None
        assert "https://cdn.example/…" in result.message
        assert "PATH-TOKEN" not in result.message
        assert "QUERY-TOKEN" not in result.message


def test_compose_carries_no_address() -> None:
    for source in ("upload", "url"):
        for status in ("done", "failed", "skipped"):
            job = _audio_job(source, status, instructions="INSTR-TOKEN")
            result = compose(job, None)
            assert result is not None
            text = result.title + "\n" + result.message
            assert "@" not in text
            assert "zelda" not in text
            assert "INSTR-TOKEN" not in text
