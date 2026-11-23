"""Delivery through the runner and worker: states, failures, resends, and leaks."""

import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from earmark.errors import MaybeDelivered, PermanentError, TransientError
from earmark.fakes import FakeMailer, make_fake_backends
from earmark.jobs import LeaseLostError
from earmark.mail.smtp import SmtpMailer, SmtpOptions
from earmark.models import Job, JobResult
from earmark.pipeline.runner import Pipeline
from earmark.pipeline.stages import Mailer, OutgoingMail
from earmark.pipeline.worker import Worker
from earmark.recipients import RecipientPolicy
from tests.factories import TO, new_podcast_job
from tests.mail_helpers import assert_note_hash_verifies, parse_mail
from tests.pipeline_env import CANON, PipelineEnv, make_env
from tests.smtp_stub import (
    ECHOED_ADDRESS,
    REPLY_SENTINEL,
    SmtpStub,
    stub_factory,
    trusted_test_context,
)

make_stub = pytest.fixture(stub_factory)
StubMaker = Callable[..., SmtpStub]

PASSWORD = "abcd efgh ijkl mnop"


class SimulatedCrash(BaseException):  # like a SIGKILL: nothing can catch it
    pass


def _enqueue(env: PipelineEnv, instructions: str = "") -> Job:
    return env.jobs.enqueue(new_podcast_job(CANON, instructions=instructions)).job


def _run(env: PipelineEnv) -> Job:
    job = env.worker.run_once()
    assert job is not None
    return job


def _stored(env: PipelineEnv, job: Job) -> Job:
    found = env.jobs.get(job.id)
    assert found is not None
    return found


def _smtp_env(tmp_path: Path, stub: SmtpStub, **kw: Any) -> PipelineEnv:
    options = SmtpOptions(
        host="smtp.test",
        port=587,
        security="starttls",
        username="mailer",
        password=SecretStr(PASSWORD),
        sender="earmark@example.org",
        timeout_s=5.0,
    )
    mailer = SmtpMailer(
        options, socket_factory=stub.socket_factory, context_factory=trusted_test_context
    )
    return _env_with_mailer(tmp_path, mailer, **kw)


def _env_with_mailer(tmp_path: Path, mailer: Mailer, **kw: Any) -> PipelineEnv:
    fakes = make_fake_backends(tmp_path / "data")
    backends = replace(fakes.backends(), mailer=mailer)
    return make_env(tmp_path, fakes=fakes, backends=backends, **kw)


# -- the happy path ----------------------------------------------------------------------


def test_delivery_marks_sent_and_done_together(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    enqueued = _enqueue(env, "Which papers?")
    job = _run(env)
    assert (job.status, job.stage, job.delivery_state) == ("done", "deliver", "sent")
    assert job.sent_at == env.clock.now
    assert job.message_id == f"<earmark.{enqueued.id}@example.org>"
    assert job.resent is False
    [mail] = env.fakes.mailer.calls
    assert mail.job_id == job.id
    assert mail.recipient == TO
    assert mail.message_id == job.message_id
    parsed = parse_mail(mail.data)
    assert_note_hash_verifies(parsed.note)
    assert parsed.instructions.endswith(b"---\nWhich papers?")
    assert parsed.message["Date"] == "Thu, 01 Jan 2026 12:00:00 +0000"  # the injected clock
    [push] = env.fakes.notifier.calls
    assert push.job.delivery_state == "sent"


def test_state_is_sending_while_the_mailer_runs(tmp_path: Path) -> None:
    seen: list[Job] = []
    holder: list[PipelineEnv] = []

    class Peek(FakeMailer):
        def send(self, mail: OutgoingMail) -> None:
            seen.append(_stored(holder[0], holder[0].jobs.list_recent(1)[0]))
            super().send(mail)

    env = _env_with_mailer(tmp_path, Peek())
    holder.append(env)
    job = _enqueue(env)
    done = _run(env)
    [during] = seen
    assert during.id == job.id
    assert (during.status, during.delivery_state, during.stage) == ("running", "sending", "deliver")
    assert during.message_id == f"<earmark.{job.id}@example.org>"
    assert during.sent_at is None
    assert done.delivery_state == "sent"


def test_transient_mail_error_retries_with_state_cleared(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    _enqueue(env)
    env.fakes.mailer.fail_next(TransientError("the server is busy"))
    first = _run(env)
    assert (first.status, first.delivery_state, first.stage, first.resent) == (
        "queued",
        None,
        "deliver",
        False,
    )
    assert env.fakes.notifier.call_count == 0
    env.clock.advance(60)
    second = _run(env)
    assert (second.status, second.delivery_state, second.resent) == ("done", "sent", False)
    assert env.fakes.mailer.call_count == 2
    assert env.fakes.transcriber.computed == 1  # the retry reused the cached transcript


def test_permanent_mail_error_fails_the_job(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    _enqueue(env)
    env.fakes.mailer.fail_next(PermanentError("the server said no"))
    job = _run(env)
    assert (job.status, job.stage, job.delivery_state, job.error) == (
        "failed",
        "deliver",
        None,
        "the server said no",
    )
    [push] = env.fakes.notifier.calls
    assert push.job == job


# -- guards before sending ---------------------------------------------------------------


def test_over_cap_fails_permanently_nothing_sent(tmp_path: Path) -> None:
    env = make_env(tmp_path, max_attachment_bytes=100)
    _enqueue(env)
    job = _run(env)
    assert (job.status, job.stage, job.delivery_state) == ("failed", "deliver", None)
    assert job.error is not None
    assert "EARMARK_MAX_ATTACHMENT_MB" in job.error
    assert env.fakes.mailer.call_count == 0
    assert job.attempts == 1


def test_job_without_to_fails_permanently(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    queued = _enqueue(env)
    with env.jobs._db.connect() as conn:
        conn.execute("UPDATE jobs SET recipient = NULL WHERE id = ?", (queued.id,))
    job = _run(env)
    assert (job.status, job.stage, job.to) == ("failed", "deliver", None)
    assert job.error == "this request has no destination address; share it again"
    assert env.fakes.mailer.call_count == 0


def test_recipient_no_longer_allowed_fails_permanently(tmp_path: Path) -> None:
    policy = RecipientPolicy.from_setting(("someone.else@example.org",))
    env = make_env(tmp_path, recipient_policy=policy)
    _enqueue(env)
    job = _run(env)
    assert (job.status, job.stage, job.delivery_state) == ("failed", "deliver", None)
    assert job.error is not None
    assert "EARMARK_ALLOWED_RECIPIENTS" in job.error
    assert TO not in job.error
    assert env.fakes.mailer.call_count == 0
    allowed = make_env(tmp_path / "ok", recipient_policy=RecipientPolicy.from_setting((TO,)))
    _enqueue(allowed)
    assert _run(allowed).status == "done"
    refusing = make_env(tmp_path / "none", recipient_policy=RecipientPolicy.from_setting(()))
    _enqueue(refusing)
    assert _run(refusing).status == "failed"


# -- the state machine through a real SMTP exchange --------------------------------------


def test_definite_failure_clears_sending(tmp_path: Path, make_stub: StubMaker) -> None:
    stub = make_stub(final_reply=f"550 5.7.1 rejected {ECHOED_ADDRESS}")
    env = _smtp_env(tmp_path, stub)
    _enqueue(env)
    job = _run(env)
    assert (job.status, job.delivery_state, job.resent) == ("failed", None, False)
    assert job.message_id is not None  # kept for the record
    assert job.error == "the SMTP server at smtp.test refused the message (reply 550, 5.7.1)"
    [push] = env.fakes.notifier.calls
    assert push.job.delivery_state is None


def test_ambiguous_failure_keeps_sending_and_flags_resend(
    tmp_path: Path, make_stub: StubMaker
) -> None:
    stub = make_stub(disconnect_at="after_payload")
    env = _smtp_env(tmp_path, stub)
    _enqueue(env)
    first = _run(env)
    assert (first.status, first.delivery_state, first.resent) == ("queued", "sending", False)
    assert first.error is not None
    assert "may get it twice" in first.error
    assert env.fakes.notifier.call_count == 0
    stub.disconnect_at = None
    env.clock.advance(60)
    second = _run(env)
    assert (second.status, second.delivery_state, second.resent) == ("done", "sent", True)
    assert second.message_id == first.message_id
    assert len(stub.messages) == 2  # the server saw it twice, which is what resent warns about
    [push] = env.fakes.notifier.calls
    assert push.job.resent is True


def test_maybe_delivered_on_the_last_attempt_fails_with_state_sending(tmp_path: Path) -> None:
    env = make_env(tmp_path, max_attempts=1)
    _enqueue(env)
    env.fakes.mailer.fail_next(MaybeDelivered("did not confirm"))
    job = _run(env)
    assert (job.status, job.delivery_state) == ("failed", "sending")
    [push] = env.fakes.notifier.calls
    assert push.job.delivery_state == "sending"


def test_crash_after_data_resends_same_message_id(tmp_path: Path) -> None:
    recorded: list[OutgoingMail] = []
    crashes = [True]

    class CrashingMailer:
        sender = "earmark@example.org"

        def send(self, mail: OutgoingMail, /) -> None:
            recorded.append(mail)
            if crashes.pop() if crashes else False:
                raise SimulatedCrash

    env = _env_with_mailer(tmp_path, CrashingMailer())
    queued = _enqueue(env)
    with pytest.raises(SimulatedCrash):
        env.worker.run_once()
    crashed = _stored(env, queued)
    assert (crashed.status, crashed.delivery_state, crashed.resent) == ("running", "sending", False)
    env.clock.advance(env.jobs._lease.total_seconds() + 1)
    done = _run(env)
    assert (done.status, done.delivery_state, done.resent, done.attempts) == (
        "done",
        "sent",
        True,
        2,
    )
    first, second = recorded
    assert first.message_id == second.message_id == crashed.message_id
    assert f"Message-ID: {first.message_id}\r\n".encode() in second.data
    first_parsed, second_parsed = parse_mail(first.data), parse_mail(second.data)
    assert first_parsed.instructions == second_parsed.instructions  # the same job_id inside
    assert f'job_id: "{queued.id}"'.encode() in second_parsed.instructions
    [push] = env.fakes.notifier.calls
    assert push.job.resent is True


# -- a sent job is never sent again ------------------------------------------------------


def test_sent_job_not_resent_on_restart_or_lease_expiry(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    _enqueue(env)
    assert _run(env).delivery_state == "sent"
    pipeline = Pipeline(
        env.fakes.backends(), env.fakes.artifacts, env.jobs, staging=env.staging, clock=env.clock
    )
    restarted = Worker(env.jobs, pipeline, poll_seconds=0.01, heartbeat_seconds=60)
    env.clock.advance(env.jobs._lease.total_seconds() * 3)
    assert restarted.run_once() is None
    assert restarted.run_once() is None
    assert env.fakes.mailer.call_count == 1
    assert env.jobs.requeue_stale() == []


def test_claimed_sent_job_completes_without_sending(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env = make_env(tmp_path)
    queued = _enqueue(env)
    with env.jobs._db.connect() as conn:  # a hand-edited database
        conn.execute(
            "UPDATE jobs SET delivery_state = 'sent', message_id = '<m@example.org>', "
            "sent_at = run_after WHERE id = ?",
            (queued.id,),
        )
    with caplog.at_level(logging.WARNING):
        job = _run(env)
    assert (job.status, job.delivery_state) == ("done", "sent")
    assert env.fakes.mailer.call_count == 0
    assert "already marked sent" in caplog.text


def test_complete_sent_requires_sending(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    queued = _enqueue(env)
    claim = env.jobs.claim_next()
    assert claim is not None
    with pytest.raises(LeaseLostError):
        env.jobs.complete_sent(claim, JobResult(duration_s=1.0))
    assert _stored(env, queued).status == "running"


# -- leaks -------------------------------------------------------------------------------

FAILURES = {
    "reply_4xx": {"final_reply": f"451 4.3.0 later {REPLY_SENTINEL} {ECHOED_ADDRESS}"},
    "reply_5xx": {"final_reply": f"550 5.1.1 no {REPLY_SENTINEL} {ECHOED_ADDRESS}"},
    "rcpt_5xx": {"rcpt_reply": f"550 5.1.1 no {REPLY_SENTINEL} {ECHOED_ADDRESS}"},
    "auth_5xx": {"auth_reply": f"535 5.7.8 bad {REPLY_SENTINEL} {ECHOED_ADDRESS} {{password}}"},
    "auth_4xx": {"auth_reply": f"454 4.7.0 later {REPLY_SENTINEL} {ECHOED_ADDRESS}"},
    "greeting": {"greeting": f"554 5.3.2 go {REPLY_SENTINEL} {ECHOED_ADDRESS}"},
    "after_payload": {"disconnect_at": "after_payload"},
    "mid_connection": {"disconnect_at": "mail"},
    "bad_final": {"final_reply": f"{REPLY_SENTINEL} {ECHOED_ADDRESS}"},
}


@pytest.mark.parametrize("name", FAILURES)
def test_job_errors_carry_codes_not_reply_text(
    tmp_path: Path, make_stub: StubMaker, name: str, caplog: pytest.LogCaptureFixture
) -> None:
    stub = make_stub(**FAILURES[name])
    env = _smtp_env(tmp_path, stub)
    _enqueue(env)
    with caplog.at_level(logging.DEBUG):
        job = _run(env)
    assert job.error is not None
    assert job.error.startswith(("the SMTP server at smtp.test", "lost the connection"))
    if name in {"reply_4xx", "auth_4xx"}:
        assert "reply 4" in job.error
    if name in {"reply_5xx", "rcpt_5xx", "auth_5xx"}:
        assert "reply 5" in job.error
    pushed = " ".join(repr(call.job) for call in env.fakes.notifier.calls)
    for secret in (REPLY_SENTINEL, ECHOED_ADDRESS, PASSWORD):
        assert secret not in job.error
        assert secret not in caplog.text
        assert secret not in pushed
    assert TO not in job.error
