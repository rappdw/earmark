"""Opt-in: sends ONE real message through a real SMTP server. Never run by default.

    SMTP_TEST_HOST=... SMTP_TEST_USERNAME=... SMTP_TEST_PASSWORD=... \
    SMTP_TEST_FROM=... SMTP_TEST_TO=... uv run pytest -m smtp -rs

Optional: SMTP_TEST_PORT, and SMTP_TEST_SECURITY (``starttls``, the default, or ``tls``). The
variables have their own names because ``tests/conftest.py`` strips every ``EARMARK_*`` one.
Without all of the required ones the test is skipped. No workflow runs this marker.
"""

import os
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from earmark.config import Settings
from earmark.mail.message import compose_mail
from earmark.mail.smtp import SmtpMailer, SmtpOptions
from tests.factories import make_delivery_job, make_episode, make_transcript_with
from tests.mail_helpers import parse_mail

pytestmark = pytest.mark.smtp

REQUIRED = (
    "SMTP_TEST_HOST",
    "SMTP_TEST_USERNAME",
    "SMTP_TEST_PASSWORD",
    "SMTP_TEST_FROM",
    "SMTP_TEST_TO",
)


def missing_variables(environ: dict[str, str] | os._Environ[str]) -> list[str]:
    return [name for name in REQUIRED if not environ.get(name)]


def test_send_one_message() -> None:
    missing = missing_variables(os.environ)
    if missing:
        pytest.skip("set " + ", ".join(missing) + " to send one real message")
    port = os.environ.get("SMTP_TEST_PORT")
    settings = Settings(
        _env_file=None,
        smtp_host=os.environ["SMTP_TEST_HOST"],
        smtp_port=int(port) if port else None,
        smtp_security=os.environ.get("SMTP_TEST_SECURITY", "starttls"),  # type: ignore[arg-type]
        smtp_username=os.environ["SMTP_TEST_USERNAME"],
        smtp_password=SecretStr(os.environ["SMTP_TEST_PASSWORD"]),
        smtp_from=os.environ["SMTP_TEST_FROM"],
    )
    mailer = SmtpMailer(SmtpOptions.from_settings(settings))
    report = mailer.check_login()
    assert report.tls_version.startswith("TLSv1.")

    to = Settings(_env_file=None, allowed_recipients=(os.environ["SMTP_TEST_TO"],))
    [recipient] = to.allowed_recipients
    job = make_delivery_job(to=recipient)
    episode = make_episode(title="earmark live SMTP test", feed_guid="earmark-live-smtp-test")
    transcript = make_transcript_with([], episode=episode)
    mail = compose_mail(
        job,
        episode,
        transcript,
        first_shared_at=job.shared_at,
        sender=mailer.sender,
        date=datetime.now(UTC),
        max_attachment_bytes=1024 * 1024,
    )
    parse_mail(mail.data)  # a well-formed message before it leaves
    mailer.send(mail)
